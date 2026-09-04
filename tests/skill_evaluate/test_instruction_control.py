"""docs/dev/13：模块三——指令控制度与执行效果评测。

覆盖：用例准备（复用模块一 POSITIVE 训练集 + 按 REUSE 语义补探查用例）、A/B 两条
分支的 `load_skill` 取值与 run_index 号段隔离、ROI 判定的 CRITICAL 声明与共识挂起、
效率诊断只看加载侧、控制标定复用模板 5.7、渐进式披露探查的确定性扫描（漏读/过度
抓取/水位/无探查目标）与量化规则、失败信号收集只取训练集且只取两类可优化问题、
优化闭环按类别分派重测、报告口径（阻断与否、状态优先级）、以及图结构（并行分叉、
汇合、条件路由、无回边）。
全部用替身注入，不碰数据库、不发真实请求。
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from skill_evaluate.agents.generator.service import EnsureTestSuiteResult
from skill_evaluate.agents.judge.golden_injector import golden_subject_id
from skill_evaluate.agents.judge.rules import get_rule
from skill_evaluate.agents.optimizer.patch_applier import working_version_ref
from skill_evaluate.config import InstructionControlSettings
from skill_evaluate.errors import ConfigurationError, PersistenceError, PipelineSuspended
from skill_evaluate.executors.base import ExecutionRequest, ExecutorBackend
from skill_evaluate.nodes.instruction_control import (
    DIMENSION,
    NODE_NAMES,
    RULE_PROGRESSIVE_DISCLOSURE_PROBE,
    InstructionControlDeps,
    InstructionControlPipeline,
    build_instruction_control_subgraph,
    format_actions_for_review,
    probe_inputs,
    read_reference_paths,
    resolve_token_watermark,
    scan_probe_trace,
)
from skill_evaluate.nodes.instruction_control.graph import INTERRUPT_BEFORE_NODES
from skill_evaluate.nodes.instruction_control.nodes import (
    MAX_RUN_COUNT_PER_ARM,
    SUBJECT_PREFIX_PD_PROBE,
    SUBJECT_PREFIX_ROI,
)
from skill_evaluate.nodes.instruction_control.probe import (
    KIND_MISSING_READ,
    KIND_NO_PROBE_TARGET,
    KIND_OVER_FETCH,
    KIND_TOKEN_WATERMARK,
)
from skill_evaluate.nodes.instruction_control.state import (
    KEY_AB_CASE_IDS,
    KEY_AB_PAIRS,
    KEY_APPLIED_PATCH_ID,
    KEY_CALIBRATION_OUTCOME,
    KEY_EFFICIENCY_OUTCOMES,
    KEY_FAILED_TRAIN_CASE_IDS,
    KEY_PD_CASE_IDS,
    KEY_PD_FINDINGS,
    KEY_ROI_OUTCOMES,
    KEY_TOKEN_WATERMARK,
    KEY_WORKING_SKILL,
)
from skill_evaluate.state.enums import (
    Criticality,
    DatasetSplit,
    ExecutorBackendType,
    JudgeVerdictStatus,
    PatchType,
    TestCaseCategory,
)
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.patch import Patch
from skill_evaluate.state.skill import SkillDefinition, SkillReferenceFile
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion
from skill_evaluate.state.trace import (
    RUN_INDEX_AB_BASELINE,
    RUN_INDEX_AB_LOADED,
    RUN_INDEX_PD_PROBE,
    ActionStep,
    ExecutionTrace,
    TimingCostMetrics,
)

SKILL_ID = "csv-cleaner"
RUN_ID = "run-1"
BASE_REF = "v1"
ERRORS_REF = "references/errors.md"
SCHEMA_REF = "references/schema.md"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def _skill(
    *,
    version_ref: str = BASE_REF,
    references: list[tuple[str, str | None]] | None = None,
) -> SkillDefinition:
    refs = references if references is not None else [(ERRORS_REF, "执行报错时查阅本文件")]
    return SkillDefinition(
        skill_id=SKILL_ID,
        version_ref=version_ref,
        root_path=".",
        description="清洗并校验 CSV 导出文件",
        body_markdown="# CSV Cleaner\n\n执行报错时请查阅 references/errors.md。\n",
        line_count=3,
        token_count=40,
        reference_files=[
            SkillReferenceFile(path=path, trigger_condition=condition) for path, condition in refs
        ],
    )


def _case(
    case_id: str,
    *,
    category: TestCaseCategory = TestCaseCategory.POSITIVE,
    split: DatasetSplit = DatasetSplit.TRAIN,
    probe_target: str | None = None,
) -> TestCase:
    return TestCase(
        case_id=case_id,
        skill_id=SKILL_ID,
        category=category,
        split=split,
        prompt=f"帮我处理导出的表格（{case_id}）",
        probe_target_reference=probe_target,
        generator_run_id="gen-1",
        created_at=datetime.now(UTC),
    )


def _trace(
    case_id: str,
    run_index: int,
    *,
    actions: list[ActionStep] | None = None,
    total_tokens: int = 100,
    loaded: bool = True,
) -> ExecutionTrace:
    now = datetime.now(UTC)
    return ExecutionTrace(
        trace_id=f"t-{case_id}-{run_index}",
        case_id=case_id,
        run_index=run_index,
        backend_type=ExecutorBackendType.PLUGGABLE.value,
        loaded_skill_md=loaded,
        timing=TimingCostMetrics(
            total_tokens=total_tokens, prompt_tokens=60, completion_tokens=40, duration_ms=500
        ),
        actions=actions or [],
        final_response="done",
        started_at=now,
        finished_at=now,
    )


def _read_action(path: str, *, step_id: int = 1, action_type: str = "read_file") -> ActionStep:
    return ActionStep(
        step_id=step_id,
        timestamp=datetime.now(UTC),
        thought="先看看参考资料",
        action_type=action_type,
        action_input={"path": path},
    )


def _bash_action(command: str, *, step_id: int = 1) -> ActionStep:
    return ActionStep(
        step_id=step_id,
        timestamp=datetime.now(UTC),
        thought="直接跑一条命令",
        action_type="bash",
        action_input={"command": command},
    )


def _verdict(
    status: JudgeVerdictStatus, *, subject_id: str, verdict_id: str | None = None
) -> JudgeVerdict:
    return JudgeVerdict(
        verdict_id=verdict_id or f"v-{subject_id}-{status.value}",
        subject_id=subject_id,
        status=status,
        reasoning="原文片段：……" + "补" * 300,
        temperature=0.1,
        model="anthropic/claude-haiku-4.5",
        created_at=datetime.now(UTC),
    )


class FakeSkillRepo:
    def __init__(self, skill: SkillDefinition | None) -> None:
        self.skill = skill

    async def get(self, skill_id: str, version_ref: str) -> SkillDefinition | None:
        return self.skill


class FakeSuiteService:
    """替代 `TestSuiteService`：不出题，直接给一份既有用例集并记录调用参数。"""

    def __init__(self, case_ids: list[str], *, staleness: str | None = None) -> None:
        self.case_ids = case_ids
        self.staleness = staleness
        self.calls: list[dict[str, Any]] = []

    async def ensure_test_suite(
        self,
        skill: SkillDefinition,
        *,
        extra_categories: list[TestCaseCategory] | None = None,
        category_counts: dict[TestCaseCategory, int] | None = None,
    ) -> EnsureTestSuiteResult:
        self.calls.append(
            {"extra_categories": extra_categories, "category_counts": category_counts}
        )
        version = TestSuiteVersion(
            suite_version_id="suite-1",
            skill_id=skill.skill_id,
            skill_version_ref=skill.version_ref,
            generation_mode="reuse",
            case_ids=self.case_ids,
            created_at=datetime.now(UTC),
        )
        return EnsureTestSuiteResult(suite_version=version, staleness_warning=self.staleness)


class FakeCaseRepo:
    def __init__(self, cases: list[TestCase]) -> None:
        self.cases = cases
        self.by_id = {c.case_id: c for c in cases}

    async def list_by_ids(self, case_ids: list[str]) -> list[TestCase]:
        # 刻意打乱返回顺序：节点侧承诺按传入顺序还原。
        return [self.by_id[cid] for cid in reversed(case_ids) if cid in self.by_id]

    async def list_by_category(
        self, suite_version_id: str, category: TestCaseCategory
    ) -> list[TestCase]:
        return await self.list_by_categories(suite_version_id, [category])

    async def list_by_categories(
        self, suite_version_id: str, categories: list[TestCaseCategory]
    ) -> list[TestCase]:
        return [c for c in self.cases if c.category in categories]


class FakeTraceRepo:
    def __init__(self) -> None:
        self.saved: list[ExecutionTrace] = []

    async def save(self, trace: ExecutionTrace) -> None:
        # 与真实实现一致：唯一键是 (case_id, run_index)，同键覆盖。
        self.saved = [
            t for t in self.saved if (t.case_id, t.run_index) != (trace.case_id, trace.run_index)
        ]
        self.saved.append(trace)

    async def get(self, trace_id: str) -> ExecutionTrace | None:
        return next((t for t in self.saved if t.trace_id == trace_id), None)


class FakeJudgeRepo:
    def __init__(self) -> None:
        self.saved: list[JudgeVerdict] = []

    async def save_verdict(self, verdict: JudgeVerdict) -> None:
        self.saved.append(verdict)

    async def list_verdicts(self, subject_id: str) -> list[JudgeVerdict]:
        return [v for v in self.saved if v.subject_id == subject_id]


class FakeReporter:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record_dimension_result(self, **kwargs: Any) -> None:
        self.recorded.append(kwargs)


class FakeJudge:
    """替代 `JudgeAgent`：按 template_key 回放判定，并记录全部调用参数。

    量化判定走真实的规则表（`get_rule()`），因为那本来就是纯算术，替身反而会掩盖
    规则本身的问题。
    """

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self.results = results or {}
        self.calls: list[dict[str, Any]] = []
        self.quantitative_calls: list[dict[str, Any]] = []

    async def judgmental_verdict(
        self,
        subject_id: str,
        template_key: str,
        content: dict[str, str],
        criticality: Criticality,
    ) -> Any:
        self.calls.append(
            {
                "subject_id": subject_id,
                "template_key": template_key,
                "content": content,
                "criticality": criticality,
            }
        )
        result = self.results.get(template_key)
        if callable(result):
            return result(subject_id)
        if result is not None:
            return result
        return _verdict(JudgeVerdictStatus.PASS, subject_id=subject_id)

    def quantitative_verdict(
        self, subject_id: str, rule_name: str, inputs: dict[str, Any]
    ) -> JudgeVerdict:
        self.quantitative_calls.append(
            {"subject_id": subject_id, "rule_name": rule_name, "inputs": inputs}
        )
        status = get_rule(rule_name)(inputs)
        return _verdict(status, subject_id=subject_id, verdict_id=f"q-{subject_id}")


class RecordingBackend(ExecutorBackend):
    """记录每个请求，并按 {case_id: [ActionStep]} 回放动作序列。"""

    backend_type = ExecutorBackendType.PLUGGABLE

    def __init__(
        self,
        actions_by_case: dict[str, list[ActionStep]] | None = None,
        *,
        tokens_by_case: dict[str, int] | None = None,
        actions_after_patch: dict[str, list[ActionStep]] | None = None,
    ) -> None:
        self.actions_by_case = actions_by_case or {}
        self.tokens_by_case = tokens_by_case or {}
        self.actions_after_patch = actions_after_patch or {}
        self.requests: list[ExecutionRequest] = []
        self.in_flight = 0
        self.peak_in_flight = 0

    async def execute(self, request: ExecutionRequest) -> ExecutionTrace:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            self.requests.append(request)
            case_id = request.case.case_id
            actions = (
                self.actions_after_patch.get(case_id, [])
                if request.skill.version_ref != BASE_REF
                else self.actions_by_case.get(case_id, [])
            )
            return _trace(
                case_id,
                request.run_index,
                actions=list(actions),
                total_tokens=self.tokens_by_case.get(case_id, 100),
                loaded=request.load_skill,
            )
        finally:
            self.in_flight -= 1

    async def health_check(self) -> bool:
        return True


class FakeOptimizationLoop:
    """替代 `OptimizationLoop`：调一次 `retest_fn`，按构造参数决定收敛还是放弃。"""

    def __init__(self, *, patch: Patch | None) -> None:
        self.patch = patch
        self.retest_results: list[Any] = []
        self.ctx: Any = None

    async def run(self, run_id: str, ctx: Any, retest_fn: Any, optimizer: Any) -> Patch | None:
        self.ctx = ctx
        if self.patch is None:
            return None
        patched = ctx.skill.model_copy(
            update={"version_ref": working_version_ref(ctx.skill.version_ref, self.patch.patch_id)}
        )
        self.retest_results.append(await retest_fn(patched))
        return self.patch


def _patch(patch_id: str = "p-1") -> Patch:
    return Patch(
        patch_id=patch_id,
        skill_id=SKILL_ID,
        base_skill_version_ref=BASE_REF,
        patch_type=PatchType.RIGID_CONSTRAINT,
        target_path="SKILL.md",
        diff="@@ -1 +1 @@\n-旧\n+新\n",
        rationale="正文没有说清什么时候必须读 references/errors.md",
        created_at=datetime.now(UTC),
    )


def _pipeline(
    cases: list[TestCase],
    *,
    skill: SkillDefinition | None = None,
    backend: RecordingBackend | None = None,
    judge: FakeJudge | None = None,
    loop: FakeOptimizationLoop | None = None,
    settings: InstructionControlSettings | None = None,
    concurrency: int = 10,
) -> tuple[InstructionControlPipeline, dict[str, Any]]:
    doubles: dict[str, Any] = {
        "skill_repo": FakeSkillRepo(skill or _skill()),
        "suite": FakeSuiteService([c.case_id for c in cases]),
        "case_repo": FakeCaseRepo(cases),
        "trace_repo": FakeTraceRepo(),
        "judge_repo": FakeJudgeRepo(),
        "reporter": FakeReporter(),
        "backend": backend or RecordingBackend(),
        "judge": judge or FakeJudge(),
        "loop": loop or FakeOptimizationLoop(patch=_patch()),
    }
    deps = InstructionControlDeps(
        executor_backend=doubles["backend"],
        judge_agent=doubles["judge"],
        test_suite_service=doubles["suite"],
        report_generator=doubles["reporter"],
        optimizer_agent=object(),
        optimization_loop=doubles["loop"],
        skill_repository=doubles["skill_repo"],
        test_case_repository=doubles["case_repo"],
        trace_repository=doubles["trace_repo"],
        judge_repository=doubles["judge_repo"],
        control_settings=settings or InstructionControlSettings(),
        max_concurrent_sandboxes=concurrency,
    )
    return InstructionControlPipeline(deps), doubles


def _state(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "run_id": RUN_ID,
        "skill_id": SKILL_ID,
        "skill_version_ref": BASE_REF,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# 1. 纯代码扫描器：read_reference_paths
# --------------------------------------------------------------------------- #


class ReadReferencePathsTests:
    def test_explicit_read_action_with_relative_path(self) -> None:
        trace = _trace("c1", 0, actions=[_read_action(ERRORS_REF)])
        assert read_reference_paths(trace, [ERRORS_REF, SCHEMA_REF]) == {ERRORS_REF}

    def test_absolute_path_matches_by_suffix(self) -> None:
        """沙箱里给的往往是绝对路径，按已知相对路径做后缀匹配即可命中。"""
        trace = _trace("c1", 0, actions=[_read_action(f"/work/skill/{ERRORS_REF}")])
        assert read_reference_paths(trace, [ERRORS_REF]) == {ERRORS_REF}

    def test_bash_cat_is_detected(self) -> None:
        """`bash: cat references/errors.md` 也算读取——不然一半的后端都测不出来。"""
        trace = _trace("c1", 0, actions=[_bash_action(f"cat {ERRORS_REF} | head -20")])
        assert read_reference_paths(trace, [ERRORS_REF]) == {ERRORS_REF}

    def test_unknown_path_is_never_reported(self) -> None:
        """只承认已知路径：这条保证了扫描器的误报率为零。"""
        trace = _trace("c1", 0, actions=[_read_action("references/unknown.md")])
        assert read_reference_paths(trace, [ERRORS_REF]) == set()

    def test_no_actions_means_nothing_read(self) -> None:
        assert read_reference_paths(_trace("c1", 0), [ERRORS_REF]) == set()


# --------------------------------------------------------------------------- #
# 2. 纯代码扫描器：scan_probe_trace
# --------------------------------------------------------------------------- #


class ScanProbeTraceTests:
    def test_trigger_case_reading_target_is_clean(self) -> None:
        case = _case(
            "c1",
            category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
            probe_target=ERRORS_REF,
        )
        trace = _trace("c1", 0, actions=[_read_action(ERRORS_REF)])
        assert scan_probe_trace(case, trace, known_reference_paths=[ERRORS_REF]) == []

    def test_trigger_case_missing_read_is_severe(self) -> None:
        case = _case(
            "c1",
            category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
            probe_target=ERRORS_REF,
        )
        findings = scan_probe_trace(case, _trace("c1", 0), known_reference_paths=[ERRORS_REF])
        assert [f.kind for f in findings] == [KIND_MISSING_READ]
        assert findings[0].severe is True
        assert findings[0].report_line.startswith("[漏读]")

    def test_trigger_case_without_target_is_reported_but_not_severe(self) -> None:
        """出题阶段没对上号 = 这条题没测出东西，是评测系统的问题，不该算被测方失败。"""
        case = _case("c1", category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER)
        findings = scan_probe_trace(case, _trace("c1", 0), known_reference_paths=[ERRORS_REF])
        assert [f.kind for f in findings] == [KIND_NO_PROBE_TARGET]
        assert findings[0].severe is False

    def test_regular_case_over_fetch_is_minor(self) -> None:
        case = _case("c2", category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR)
        trace = _trace("c2", 0, actions=[_read_action(ERRORS_REF)])
        findings = scan_probe_trace(case, trace, known_reference_paths=[ERRORS_REF])
        assert [f.kind for f in findings] == [KIND_OVER_FETCH]
        assert findings[0].severe is False

    def test_regular_case_token_watermark(self) -> None:
        case = _case("c2", category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR)
        trace = _trace("c2", 0, total_tokens=900)
        findings = scan_probe_trace(
            case, trace, known_reference_paths=[ERRORS_REF], token_watermark=300
        )
        assert [f.kind for f in findings] == [KIND_TOKEN_WATERMARK]
        assert findings[0].severe is False

    def test_regular_case_without_watermark_skips_that_check(self) -> None:
        case = _case("c2", category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR)
        trace = _trace("c2", 0, total_tokens=99999)
        assert scan_probe_trace(case, trace, known_reference_paths=[ERRORS_REF]) == []


# --------------------------------------------------------------------------- #
# 3. Token 水位与量化规则
# --------------------------------------------------------------------------- #


class TokenWatermarkTests:
    def test_override_wins(self) -> None:
        assert resolve_token_watermark([], ratio=0.5, min_samples=3, override=777) == 777

    def test_too_few_samples_disables_the_check(self) -> None:
        traces = [_trace("c1", 0, total_tokens=100), _trace("c2", 0, total_tokens=110)]
        assert resolve_token_watermark(traces, ratio=0.5, min_samples=3) is None

    def test_median_times_ratio(self) -> None:
        traces = [_trace(f"c{i}", 0, total_tokens=t) for i, t in enumerate([100, 200, 300])]
        assert resolve_token_watermark(traces, ratio=0.5, min_samples=3) == 300


class ProbeRuleTests:
    def test_clean_scan_passes(self) -> None:
        rule = get_rule(RULE_PROGRESSIVE_DISCLOSURE_PROBE)
        assert rule(probe_inputs("progressive_disclosure_regular", [])) is JudgeVerdictStatus.PASS

    def test_any_finding_fails_regardless_of_severity(self) -> None:
        """严重与非严重都判 FAIL：严重程度决定的是阻断与闭环，不是判定本身。"""
        case = _case("c2", category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR)
        trace = _trace("c2", 0, actions=[_read_action(ERRORS_REF)])
        findings = scan_probe_trace(case, trace, known_reference_paths=[ERRORS_REF])
        inputs = probe_inputs(case.category.value, findings)
        assert inputs["severe_finding_count"] == 0
        assert inputs["minor_finding_count"] == 1
        assert get_rule(RULE_PROGRESSIVE_DISCLOSURE_PROBE)(inputs) is JudgeVerdictStatus.FAIL


# --------------------------------------------------------------------------- #
# 4. Trace 摘要
# --------------------------------------------------------------------------- #


class TraceDigestTests:
    def test_empty_trace_says_so_explicitly(self) -> None:
        text = format_actions_for_review(_trace("c1", 0))
        assert "没有任何工具调用" in text

    def test_long_trace_keeps_head_and_tail(self) -> None:
        actions = [_bash_action(f"echo {i}", step_id=i) for i in range(30)]
        text = format_actions_for_review(_trace("c1", 0, actions=actions), max_steps=10)
        assert "[step:0]" in text and "[step:29]" in text
        assert "[step:15]" not in text
        assert "中间 20 步已省略" in text

    def test_secrets_are_redacted_before_reaching_the_model(self) -> None:
        action = _bash_action("curl -H 'Authorization: Bearer " + "a1B2" * 8 + "'")
        text = format_actions_for_review(_trace("c1", 0, actions=[action]))
        assert "REDACTED" in text


# --------------------------------------------------------------------------- #
# 5. prepare_cases
# --------------------------------------------------------------------------- #


class PrepareCasesTests:
    async def test_selects_positive_train_cases_and_pd_cases(self) -> None:
        cases = [
            _case("p-train", split=DatasetSplit.TRAIN),
            _case("p-val", split=DatasetSplit.VALIDATION),
            _case("n-1", category=TestCaseCategory.NEGATIVE),
            _case(
                "pd-trigger",
                category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
                probe_target=ERRORS_REF,
            ),
            _case("pd-regular", category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR),
        ]
        pipeline, _doubles = _pipeline(cases)
        result = await pipeline.prepare_cases(_state())

        # 只有 POSITIVE ∩ 训练集进 A/B：验证集不参与（本维度有优化闭环），
        # NEGATIVE 与探查用例各有各的判定语义。
        assert result[KEY_AB_CASE_IDS] == ["p-train"]
        assert result[KEY_PD_CASE_IDS] == ["pd-regular", "pd-trigger"]
        assert result["active_suite_version_id"] == "suite-1"

    async def test_requests_one_trigger_case_per_reference_with_condition(self) -> None:
        skill = _skill(
            references=[
                (ERRORS_REF, "执行报错时查阅"),
                (SCHEMA_REF, "字段对不上时查阅"),
                ("references/orphan.md", None),  # 正文没提到，无从定义"条件满足"
            ]
        )
        pipeline, doubles = _pipeline([], skill=skill)
        await pipeline.prepare_cases(_state())

        call = doubles["suite"].calls[0]
        assert call["extra_categories"] == [
            TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
            TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR,
        ]
        assert call["category_counts"][TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER] == 2
        assert call["category_counts"][TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR] == 3

    async def test_skill_without_references_requests_zero_probe_cases(self) -> None:
        pipeline, doubles = _pipeline([], skill=_skill(references=[]))
        await pipeline.prepare_cases(_state())
        counts = doubles["suite"].calls[0]["category_counts"]
        assert set(counts.values()) == {0}

    async def test_missing_skill_raises(self) -> None:
        pipeline, doubles = _pipeline([])
        doubles["skill_repo"].skill = None
        with pytest.raises(PersistenceError):
            await pipeline.prepare_cases(_state())


# --------------------------------------------------------------------------- #
# 6. A/B 对比
# --------------------------------------------------------------------------- #


class AbComparativeExecutionTests:
    async def test_two_arms_differ_only_in_load_skill(self) -> None:
        pipeline, doubles = _pipeline([_case("c1")])
        await pipeline.ab_comparative_execution(_state(**{KEY_AB_CASE_IDS: ["c1"]}))

        requests = doubles["backend"].requests
        assert sorted(r.load_skill for r in requests) == [False, True]
        # 除了 load_skill 与 run_index，两条分支的参数必须完全一致，否则比出来的
        # 差异说不清是 Skill 带来的还是配置带来的。
        assert {r.wall_clock_timeout_s for r in requests} == {90}
        assert {r.case.case_id for r in requests} == {"c1"}

    async def test_run_index_is_isolated_from_module_one(self) -> None:
        """A/B 的 Trace 必须落在 100 起的专用号段，不能覆盖模块一的冗余执行。"""
        pipeline, doubles = _pipeline([_case("c1")])
        await pipeline.ab_comparative_execution(_state(**{KEY_AB_CASE_IDS: ["c1"]}))
        indices = sorted(r.run_index for r in doubles["backend"].requests)
        assert indices == [RUN_INDEX_AB_LOADED, RUN_INDEX_AB_BASELINE]
        assert min(indices) >= 100

    async def test_roi_is_judged_as_critical(self) -> None:
        pipeline, doubles = _pipeline([_case("c1")])
        await pipeline.ab_comparative_execution(_state(**{KEY_AB_CASE_IDS: ["c1"]}))
        call = doubles["judge"].calls[0]
        assert call["template_key"] == "roi_comparison"
        assert call["criticality"] is Criticality.CRITICAL
        assert call["subject_id"] == f"{SUBJECT_PREFIX_ROI}c1"
        # 两侧的数字都要给到裁判，否则"效率有没有差距"无从判断。
        assert {"loaded_actions_count", "baseline_actions_count"} <= set(call["content"])

    async def test_roi_failure_is_recorded_in_outcomes(self) -> None:
        judge = FakeJudge(
            {"roi_comparison": lambda sid: _verdict(JudgeVerdictStatus.FAIL, subject_id=sid)}
        )
        pipeline, _doubles = _pipeline([_case("c1")], judge=judge)
        result = await pipeline.ab_comparative_execution(_state(**{KEY_AB_CASE_IDS: ["c1"]}))
        outcomes = result[KEY_ROI_OUTCOMES]
        assert outcomes[0]["status"] == JudgeVerdictStatus.FAIL
        assert outcomes[0]["case_id"] == "c1"

    async def test_consensus_not_reached_suspends(self) -> None:
        """三副本吵不出结果时不允许降级成 PASS/FAIL——ROI 的失败方向是"打回重构"。"""
        consensus = ConsensusResult(
            subject_id=f"{SUBJECT_PREFIX_ROI}c1",
            verdicts=[_verdict(JudgeVerdictStatus.FAIL, subject_id="x")],
            consensus_reached=False,
            final_status=JudgeVerdictStatus.NEEDS_HUMAN_REVIEW,
        )
        pipeline, _ = _pipeline([_case("c1")], judge=FakeJudge({"roi_comparison": consensus}))
        with pytest.raises(PipelineSuspended):
            await pipeline.ab_comparative_execution(_state(**{KEY_AB_CASE_IDS: ["c1"]}))

    async def test_golden_injection_is_skipped_not_counted(self) -> None:
        golden = _verdict(JudgeVerdictStatus.FAIL, subject_id=golden_subject_id("golden-1"))
        pipeline, _ = _pipeline([_case("c1")], judge=FakeJudge({"roi_comparison": golden}))
        result = await pipeline.ab_comparative_execution(_state(**{KEY_AB_CASE_IDS: ["c1"]}))
        outcome = result[KEY_ROI_OUTCOMES][0]
        assert outcome["status"] is None
        assert "黄金基准盲测" in outcome["skipped_reason"]
        # 被盲测占用的那次没有属于本 Skill 的 verdict，不该进 judge_verdict_ids。
        assert result["judge_verdict_ids"] == []

    async def test_no_cases_is_not_an_error(self) -> None:
        pipeline, doubles = _pipeline([])
        result = await pipeline.ab_comparative_execution(_state(**{KEY_AB_CASE_IDS: []}))
        assert result[KEY_AB_PAIRS] == []
        assert doubles["backend"].requests == []

    async def test_concurrency_is_capped(self) -> None:
        cases = [_case(f"c{i}") for i in range(5)]
        pipeline, doubles = _pipeline(cases, concurrency=2)
        await pipeline.ab_comparative_execution(
            _state(**{KEY_AB_CASE_IDS: [c.case_id for c in cases]})
        )
        assert doubles["backend"].peak_in_flight <= 2


# --------------------------------------------------------------------------- #
# 7. 效率诊断与控制标定
# --------------------------------------------------------------------------- #


class DiagnosisTests:
    async def test_efficiency_only_reviews_the_loaded_arm(self) -> None:
        pipeline, doubles = _pipeline([_case("c1")])
        ab = await pipeline.ab_comparative_execution(_state(**{KEY_AB_CASE_IDS: ["c1"]}))
        doubles["judge"].calls.clear()
        await pipeline.trace_efficiency_diagnosis(_state(**{KEY_AB_PAIRS: ab[KEY_AB_PAIRS]}))

        calls = doubles["judge"].calls
        assert len(calls) == 1
        assert calls[0]["template_key"] == "trace_efficiency"
        # ROUTINE：效率诊断不阻断也不进闭环，没必要花三倍 Token 投票。
        assert calls[0]["criticality"] is Criticality.ROUTINE
        loaded_trace_id = ab[KEY_AB_PAIRS][0]["loaded_trace_id"]
        assert calls[0]["subject_id"].endswith(loaded_trace_id)

    async def test_control_calibration_reuses_template_57_on_repository_version(self) -> None:
        """控制标定评的是仓库里那份原貌，不是优化闭环改过的内存副本。"""
        pipeline, doubles = _pipeline([])
        patched = _skill(version_ref="v1+patch:p-1").model_copy(
            update={"body_markdown": "# 被机器改写过的正文\n"}
        )
        result = await pipeline.control_calibration_static_scan(
            _state(**{KEY_WORKING_SKILL: patched})
        )
        call = doubles["judge"].calls[0]
        assert call["template_key"] == "control_calibration"
        assert call["criticality"] is Criticality.ROUTINE
        assert call["subject_id"] == SKILL_ID
        assert "被机器改写过" not in call["content"]["skill_md"]
        assert call["content"]["skill_md"] == _skill().body_markdown
        assert result[KEY_CALIBRATION_OUTCOME]["status"] == JudgeVerdictStatus.PASS


# --------------------------------------------------------------------------- #
# 8. 渐进式披露动态探查
# --------------------------------------------------------------------------- #


class ProgressiveDisclosureProbeTests:
    def _pd_cases(self) -> list[TestCase]:
        return [
            _case(
                "pd-trigger",
                category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
                probe_target=ERRORS_REF,
            ),
            _case("pd-regular", category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR),
        ]

    async def test_missing_read_and_over_fetch_are_both_detected(self) -> None:
        backend = RecordingBackend(
            {
                # 触发题：该读却没读 -> 漏读（严重）
                "pd-trigger": [_bash_action("python clean.py")],
                # 常规题：不该读却读了 -> 过度抓取（非严重）
                "pd-regular": [_read_action(ERRORS_REF)],
            }
        )
        pipeline, _doubles = _pipeline(self._pd_cases(), backend=backend)
        result = await pipeline.progressive_disclosure_dynamic_probe(
            _state(**{KEY_PD_CASE_IDS: ["pd-trigger", "pd-regular"]})
        )
        kinds = {f["kind"]: f["severe"] for f in result[KEY_PD_FINDINGS]}
        assert kinds == {KIND_MISSING_READ: True, KIND_OVER_FETCH: False}

    async def test_probe_runs_in_its_own_run_index_slot(self) -> None:
        pipeline, doubles = _pipeline(self._pd_cases())
        await pipeline.progressive_disclosure_dynamic_probe(
            _state(**{KEY_PD_CASE_IDS: ["pd-trigger", "pd-regular"]})
        )
        assert {r.run_index for r in doubles["backend"].requests} == {RUN_INDEX_PD_PROBE}

    async def test_verdicts_go_through_the_quantitative_rule(self) -> None:
        """探查判定必须经 Judge 产出 JudgeVerdict，而不是节点里直接写 if。"""
        backend = RecordingBackend({"pd-trigger": [], "pd-regular": []})
        pipeline, doubles = _pipeline(self._pd_cases(), backend=backend)
        await pipeline.progressive_disclosure_dynamic_probe(
            _state(**{KEY_PD_CASE_IDS: ["pd-trigger", "pd-regular"]})
        )
        calls = doubles["judge"].quantitative_calls
        assert {c["rule_name"] for c in calls} == {RULE_PROGRESSIVE_DISCLOSURE_PROBE}
        assert {c["subject_id"] for c in calls} == {
            f"{SUBJECT_PREFIX_PD_PROBE}pd-trigger",
            f"{SUBJECT_PREFIX_PD_PROBE}pd-regular",
        }
        # 只归档失败判定（与模块一同一口径）：漏读那条落库，通过那条不落。
        assert [v.subject_id for v in doubles["judge_repo"].saved] == [
            f"{SUBJECT_PREFIX_PD_PROBE}pd-trigger"
        ]

    async def test_external_watermark_override_is_honored(self) -> None:
        backend = RecordingBackend(
            {"pd-trigger": [_read_action(ERRORS_REF)], "pd-regular": []},
            tokens_by_case={"pd-regular": 500},
        )
        pipeline, _ = _pipeline(self._pd_cases(), backend=backend)
        result = await pipeline.progressive_disclosure_dynamic_probe(
            _state(**{KEY_PD_CASE_IDS: ["pd-trigger", "pd-regular"], KEY_TOKEN_WATERMARK: 100})
        )
        assert [f["kind"] for f in result[KEY_PD_FINDINGS]] == [KIND_TOKEN_WATERMARK]

    async def test_no_probe_cases_is_reported_not_crashed(self) -> None:
        pipeline, doubles = _pipeline([])
        result = await pipeline.progressive_disclosure_dynamic_probe(
            _state(**{KEY_PD_CASE_IDS: []})
        )
        assert result[KEY_PD_FINDINGS] == []
        assert doubles["backend"].requests == []


# --------------------------------------------------------------------------- #
# 9. 失败信号收集与路由
# --------------------------------------------------------------------------- #


class CollectFindingsTests:
    async def test_only_train_cases_and_only_optimizable_problems(self) -> None:
        cases = [
            _case("roi-train", split=DatasetSplit.TRAIN),
            _case("roi-val", split=DatasetSplit.VALIDATION),
            _case(
                "pd-severe",
                category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
                split=DatasetSplit.TRAIN,
                probe_target=ERRORS_REF,
            ),
            _case(
                "pd-minor",
                category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR,
                split=DatasetSplit.TRAIN,
            ),
        ]
        pipeline, _ = _pipeline(cases)
        state = _state(
            **{
                KEY_AB_CASE_IDS: ["roi-train", "roi-val"],
                KEY_PD_CASE_IDS: ["pd-severe", "pd-minor"],
                KEY_ROI_OUTCOMES: [
                    {
                        "subject_id": "roi:roi-train",
                        "template_key": "roi_comparison",
                        "case_id": "roi-train",
                        "status": JudgeVerdictStatus.FAIL,
                    },
                    {
                        "subject_id": "roi:roi-val",
                        "template_key": "roi_comparison",
                        "case_id": "roi-val",
                        "status": JudgeVerdictStatus.FAIL,
                    },
                ],
                KEY_PD_FINDINGS: [
                    {
                        "case_id": "pd-severe",
                        "trace_id": "t1",
                        "kind": KIND_MISSING_READ,
                        "severe": True,
                        "message": "漏读",
                    },
                    {
                        "case_id": "pd-minor",
                        "trace_id": "t2",
                        "kind": KIND_OVER_FETCH,
                        "severe": False,
                        "message": "过度抓取",
                    },
                ],
            }
        )
        result = await pipeline.collect_findings(state)
        # 验证集的 ROI 失败不进（防过拟合）；过度抓取不进（不是可自动优化的问题）。
        assert result[KEY_FAILED_TRAIN_CASE_IDS] == ["pd-severe", "roi-train"]

    def test_route_goes_to_optimizer_only_when_there_is_something_to_fix(self) -> None:
        assert (
            InstructionControlPipeline.route_after_collect(
                _state(**{KEY_FAILED_TRAIN_CASE_IDS: ["c1"]})
            )
            == NODE_NAMES["optimizer_loop"]
        )
        assert (
            InstructionControlPipeline.route_after_collect(
                _state(**{KEY_FAILED_TRAIN_CASE_IDS: []})
            )
            == NODE_NAMES["finalize_dimension_report"]
        )


# --------------------------------------------------------------------------- #
# 10. 优化闭环
# --------------------------------------------------------------------------- #


class OptimizerLoopTests:
    async def test_retest_dispatches_by_category_and_converges(self) -> None:
        cases = [
            _case("roi-1", split=DatasetSplit.TRAIN),
            _case(
                "pd-1",
                category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
                split=DatasetSplit.TRAIN,
                probe_target=ERRORS_REF,
            ),
        ]
        backend = RecordingBackend(
            {"pd-1": []},  # 补丁前：漏读
            actions_after_patch={"pd-1": [_read_action(ERRORS_REF)]},  # 补丁后：读了
        )
        loop = FakeOptimizationLoop(patch=_patch())
        pipeline, _doubles = _pipeline(cases, backend=backend, loop=loop)
        result = await pipeline.optimizer_loop(
            _state(**{KEY_FAILED_TRAIN_CASE_IDS: ["roi-1", "pd-1"]})
        )

        assert loop.retest_results[0].passed is True
        assert result[KEY_APPLIED_PATCH_ID] == "p-1"
        # 工作副本按补丁 id 反查命中，供后续节点使用。
        assert result[KEY_WORKING_SKILL].version_ref == working_version_ref(BASE_REF, "p-1")
        # A/B 用例走两条分支重跑，探查用例走单次重跑。
        after_patch = [r for r in backend.requests if r.skill.version_ref != BASE_REF]
        assert sorted(r.case.case_id for r in after_patch) == ["pd-1", "roi-1", "roi-1"]

    async def test_still_failing_is_reported_with_detail(self) -> None:
        cases = [
            _case(
                "pd-1",
                category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
                split=DatasetSplit.TRAIN,
                probe_target=ERRORS_REF,
            )
        ]
        loop = FakeOptimizationLoop(patch=_patch())
        pipeline, _ = _pipeline(cases, backend=RecordingBackend({"pd-1": []}), loop=loop)
        await pipeline.optimizer_loop(_state(**{KEY_FAILED_TRAIN_CASE_IDS: ["pd-1"]}))
        assert loop.retest_results[0].passed is False
        assert "pd-1" in loop.retest_results[0].detail

    async def test_human_abandons_patch_suspends_the_pipeline(self) -> None:
        pipeline, _ = _pipeline([_case("roi-1")], loop=FakeOptimizationLoop(patch=None))
        with pytest.raises(PipelineSuspended):
            await pipeline.optimizer_loop(_state(**{KEY_FAILED_TRAIN_CASE_IDS: ["roi-1"]}))

    async def test_failure_context_carries_both_verdict_kinds(self) -> None:
        cases = [
            _case("roi-1", split=DatasetSplit.TRAIN),
            _case(
                "pd-1",
                category=TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
                split=DatasetSplit.TRAIN,
                probe_target=ERRORS_REF,
            ),
        ]
        loop = FakeOptimizationLoop(patch=_patch())
        pipeline, doubles = _pipeline(cases, loop=loop)
        doubles["judge_repo"].saved = [
            _verdict(JudgeVerdictStatus.FAIL, subject_id=f"{SUBJECT_PREFIX_ROI}roi-1"),
            _verdict(JudgeVerdictStatus.FAIL, subject_id=f"{SUBJECT_PREFIX_PD_PROBE}pd-1"),
            _verdict(JudgeVerdictStatus.PASS, subject_id=f"{SUBJECT_PREFIX_ROI}roi-1"),
        ]
        await pipeline.optimizer_loop(_state(**{KEY_FAILED_TRAIN_CASE_IDS: ["roi-1", "pd-1"]}))
        # 只喂失败证据：混进通过记录会让 Prompt 自相矛盾。
        assert len(loop.ctx.verdicts) == 2
        assert {v.status for v in loop.ctx.verdicts} == {JudgeVerdictStatus.FAIL}

    async def test_validation_cases_are_rejected_by_the_second_gate(self) -> None:
        """图上没有"验证集失败 → 闭环"这条边；`build_failure_context()` 是第二道闸。"""
        pipeline, _ = _pipeline([_case("v-1", split=DatasetSplit.VALIDATION)])
        with pytest.raises(ValueError, match="非训练集"):
            await pipeline.optimizer_loop(_state(**{KEY_FAILED_TRAIN_CASE_IDS: ["v-1"]}))


# --------------------------------------------------------------------------- #
# 11. 报告聚合
# --------------------------------------------------------------------------- #


class FinalizeReportTests:
    async def _record(self, **state_kwargs: Any) -> dict[str, Any]:
        pipeline, doubles = _pipeline([])
        await pipeline.finalize_dimension_report(_state(**state_kwargs))
        return doubles["reporter"].recorded[0]

    async def test_roi_failure_blocks(self) -> None:
        recorded = await self._record(
            **{
                KEY_AB_CASE_IDS: ["c1"],
                KEY_ROI_OUTCOMES: [
                    {
                        "subject_id": "roi:c1",
                        "template_key": "roi_comparison",
                        "case_id": "c1",
                        "status": JudgeVerdictStatus.FAIL,
                        "reasoning_excerpt": "两边没差别",
                    }
                ],
            }
        )
        assert recorded["dimension"] == DIMENSION
        assert recorded["status"] is JudgeVerdictStatus.FAIL
        assert recorded["blocking"] is True
        assert recorded["score"] is None

    async def test_missing_read_blocks(self) -> None:
        recorded = await self._record(
            **{
                KEY_PD_CASE_IDS: ["pd-1"],
                KEY_PD_FINDINGS: [
                    {
                        "case_id": "pd-1",
                        "trace_id": "t1",
                        "kind": KIND_MISSING_READ,
                        "severe": True,
                        "message": "触发条件满足但未读取 references/errors.md",
                    }
                ],
            }
        )
        assert recorded["status"] is JudgeVerdictStatus.FAIL
        assert recorded["blocking"] is True
        assert any("[漏读]" in f for f in recorded["findings"])

    async def test_over_fetch_warns_without_blocking(self) -> None:
        recorded = await self._record(
            **{
                KEY_PD_CASE_IDS: ["pd-1"],
                KEY_PD_FINDINGS: [
                    {
                        "case_id": "pd-1",
                        "trace_id": "t1",
                        "kind": KIND_OVER_FETCH,
                        "severe": False,
                        "message": "常规任务擅自读取了参考文件",
                    }
                ],
            }
        )
        assert recorded["blocking"] is False
        # 不阻断，但也不能报 PASS——报告正文里列着问题、结论却是通过，自相矛盾。
        assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW

    async def test_efficiency_and_calibration_never_block(self) -> None:
        recorded = await self._record(
            **{
                KEY_AB_CASE_IDS: ["c1"],
                KEY_EFFICIENCY_OUTCOMES: [
                    {
                        "subject_id": "efficiency:t1",
                        "template_key": "trace_efficiency",
                        "case_id": "c1",
                        "status": JudgeVerdictStatus.FAIL,
                        "reasoning_excerpt": "[step:3] 反复试错",
                    }
                ],
                KEY_CALIBRATION_OUTCOME: {
                    "subject_id": SKILL_ID,
                    "template_key": "control_calibration",
                    "status": JudgeVerdictStatus.FAIL,
                    "reasoning_excerpt": "甩出了等价工具菜单",
                },
            }
        )
        assert recorded["blocking"] is False
        assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        assert any("[效率诊断]" in f for f in recorded["findings"])
        assert any("[控制标定]" in f for f in recorded["findings"])

    async def test_all_clean_passes(self) -> None:
        recorded = await self._record(**{KEY_AB_CASE_IDS: ["c1"], KEY_PD_CASE_IDS: ["pd-1"]})
        assert recorded["status"] is JudgeVerdictStatus.PASS
        assert recorded["blocking"] is False

    async def test_no_cases_at_all_needs_human_review(self) -> None:
        """两批用例都为空 = 这个维度什么都没测到，判 PASS 等于把它悄悄关掉。"""
        recorded = await self._record()
        assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        assert any("结论不成立" in f for f in recorded["findings"])

    async def test_adopted_patch_is_disclosed(self) -> None:
        recorded = await self._record(**{KEY_AB_CASE_IDS: ["c1"], KEY_APPLIED_PATCH_ID: "p-9"})
        assert any("p-9" in f for f in recorded["findings"])


# --------------------------------------------------------------------------- #
# 12. 装配与配置
# --------------------------------------------------------------------------- #


class GraphAssemblyTests:
    def test_subgraph_structure(self) -> None:
        graph = build_instruction_control_subgraph().compile()
        edges = {(e.source, e.target) for e in graph.get_graph().edges}

        prepare = NODE_NAMES["prepare_cases"]
        collect = NODE_NAMES["collect_findings"]
        # 三条支路并行分叉
        assert (prepare, NODE_NAMES["ab_comparative_execution"]) in edges
        assert (prepare, NODE_NAMES["control_calibration_static_scan"]) in edges
        assert (prepare, NODE_NAMES["progressive_disclosure_dynamic_probe"]) in edges
        # 效率诊断是唯一的串行依赖（它消费 A/B 的 Trace）
        assert (
            NODE_NAMES["ab_comparative_execution"],
            NODE_NAMES["trace_efficiency_diagnosis"],
        ) in edges
        assert (prepare, NODE_NAMES["trace_efficiency_diagnosis"]) not in edges
        # 三条支路汇合
        assert (NODE_NAMES["trace_efficiency_diagnosis"], collect) in edges
        assert (NODE_NAMES["control_calibration_static_scan"], collect) in edges
        assert (NODE_NAMES["progressive_disclosure_dynamic_probe"], collect) in edges
        # 闭环之后直接收尾，图里没有回边（重测在 retest_fn 内部完成）
        assert (
            NODE_NAMES["optimizer_loop"],
            NODE_NAMES["finalize_dimension_report"],
        ) in edges
        assert (NODE_NAMES["optimizer_loop"], NODE_NAMES["ab_comparative_execution"]) not in edges

    def test_interrupt_before_contributes_the_optimizer_loop(self) -> None:
        assert INTERRUPT_BEFORE_NODES == [NODE_NAMES["optimizer_loop"]]

    def test_backend_routing_must_stay_pluggable(self, monkeypatch: Any) -> None:
        from skill_evaluate.executors import routing

        monkeypatch.setitem(routing.NODE_BACKEND_ROUTING, DIMENSION, ExecutorBackendType.MINI)
        with pytest.raises(ConfigurationError, match="PLUGGABLE"):
            InstructionControlPipeline(InstructionControlDeps())

    def test_run_count_per_arm_is_capped_by_the_run_index_budget(self) -> None:
        settings = InstructionControlSettings(run_count_per_arm=MAX_RUN_COUNT_PER_ARM + 1)
        with pytest.raises(ConfigurationError, match="run_index"):
            InstructionControlPipeline(InstructionControlDeps(control_settings=settings))
