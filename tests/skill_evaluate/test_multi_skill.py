"""docs/dev/20：模块十——多技能并发加载与上下文冲突防范评测。

覆盖：多技能 Trace 的加载归因、告警分发器、纯函数探测件（命名冲突 / 交替报错 / 步骤打乱 /
角色预设抽取）、七条量化规则、三个评审模板的注册与渲染、八个节点与报告口径（含唯一阻断项
基石熔断与深度冲突告警）、子图的并行扇出与汇合。
全部用替身注入，不碰数据库、不发真实请求、不起沙箱。
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from skill_evaluate.agents.generator.service import EnsureTestSuiteResult
from skill_evaluate.agents.judge.golden_injector import golden_subject_id
from skill_evaluate.agents.judge.service import JudgeAgent
from skill_evaluate.agents.mini.templates.registry import get_template
from skill_evaluate.config import MultiSkillSettings, Settings
from skill_evaluate.errors import ConfigurationError, GenerationError, PipelineSuspended
from skill_evaluate.executors.base import ExecutionRequest, ExecutorBackend
from skill_evaluate.executors.hermes_backend import build_failure_trace
from skill_evaluate.executors.skill_attribution import attribute_skill_loads, skill_mount_markers
from skill_evaluate.nodes.multi_skill import (
    DIMENSION,
    ENTRY_NODE,
    NODE_NAMES,
    PROBE_NODES,
    RULE_ATTENTION_DECAY,
    RULE_CORE_REGRESSION,
    RULE_INSTRUCTION_DEADLOCK,
    RULE_TRIGGER_HIJACK,
    MultiSkillDeps,
    MultiSkillPipeline,
    ProbeOutcome,
    build_multi_skill_subgraph,
    probes,
)
from skill_evaluate.nodes.multi_skill.state import (
    KEY_ALERT_DISPATCHED,
    KEY_ANTAGONISM_OUTCOME,
    KEY_ATTENTION_OUTCOME,
    KEY_CASE_IDS,
    KEY_CONTEXT_NOTES,
    KEY_CORE_REGRESSION_OUTCOME,
    KEY_CORE_SKILL_REFS,
    KEY_HIJACK_OUTCOME,
    KEY_NAMESPACE_OUTCOME,
    KEY_NOISE_PACK_REFS,
    KEY_ROLE_OUTCOME,
    KEY_TEMPORAL_OUTCOME,
)
from skill_evaluate.observability.alerts import (
    LoggingAlertDispatcher,
    dispatch_alert,
    get_alert_dispatcher,
    set_alert_dispatcher,
)
from skill_evaluate.state.capability import CapabilityTree, NegativeConstraint
from skill_evaluate.state.enums import (
    Criticality,
    DatasetSplit,
    ExecutorBackendType,
    JudgeVerdictStatus,
    TestCaseCategory,
)
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.skill import SkillDefinition, SkillScript
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion
from skill_evaluate.state.trace import (
    RUN_INDEX_MULTI_SKILL_ATTENTION_CROWDED,
    RUN_INDEX_MULTI_SKILL_ATTENTION_SOLO,
    RUN_INDEX_MULTI_SKILL_CORE_BASELINE,
    RUN_INDEX_MULTI_SKILL_CORE_CROWDED,
    RUN_INDEX_MULTI_SKILL_HIJACK_CROWDED,
    RUN_INDEX_MULTI_SKILL_HIJACK_SOLO,
    RUN_INDEX_MULTI_SKILL_TEMPORAL,
    ActionStep,
    ExecutionTrace,
    TimingCostMetrics,
)

TARGET_ID = "csv-cleaner"
RUN_ID = "run-1"
VERSION_REF = "v1"
SUITE_ID = "suite-active"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def _skill(
    skill_id: str = TARGET_ID,
    *,
    version_ref: str = VERSION_REF,
    body: str = "# Skill\n\n删除前先确认目标路径存在。\n",
    description: str | None = None,
    tools: tuple[str, ...] = (),
    token_count: int = 100,
) -> SkillDefinition:
    return SkillDefinition(
        skill_id=skill_id,
        version_ref=version_ref,
        root_path=f"/repo/skills/{skill_id}",
        description=description or f"{skill_id} 的描述",
        body_markdown=body,
        line_count=len(body.splitlines()),
        token_count=token_count,
        scripts=[SkillScript(path=f"scripts/{t}.py", exposed_tool_name=t) for t in tools],
    )


def _case(
    case_id: str,
    *,
    category: TestCaseCategory = TestCaseCategory.POSITIVE,
    split: DatasetSplit = DatasetSplit.TRAIN,
    skill_id: str = TARGET_ID,
    prompt: str | None = None,
    constraint_ids: list[str] | None = None,
) -> TestCase:
    return TestCase(
        case_id=case_id,
        skill_id=skill_id,
        category=category,
        split=split,
        prompt=prompt or f"帮我处理一下这个导出文件（{case_id}）",
        negative_constraint_ids=constraint_ids or [],
        generator_run_id="gen-1",
        created_at=datetime.now(UTC),
    )


def _read(skill_id: str, step_id: int = 0) -> ActionStep:
    """一次读取 `skills/<skill_id>/SKILL.md` 的动作（挂载目录约定）。"""
    return ActionStep(
        step_id=step_id,
        timestamp=datetime.now(UTC),
        action_type="read_file",
        action_input={"path": f"/sandbox/skills/{skill_id}/SKILL.md"},
    )


def _err(script: str, step_id: int) -> ActionStep:
    return ActionStep(
        step_id=step_id,
        timestamp=datetime.now(UTC),
        action_type="bash",
        action_input={"command": f"python scripts/{script}.py out.json"},
        exit_code=1,
    )


def _trace(
    case_id: str,
    run_index: int,
    *,
    loaded: bool,
    actions: list[ActionStep] | None = None,
    tokens: int = 1000,
) -> ExecutionTrace:
    now = datetime.now(UTC)
    return ExecutionTrace(
        trace_id=f"t-{case_id}-{run_index}",
        case_id=case_id,
        run_index=run_index,
        backend_type=ExecutorBackendType.PLUGGABLE.value,
        loaded_skill_md=loaded,
        timing=TimingCostMetrics(
            total_tokens=tokens, prompt_tokens=tokens, completion_tokens=0, duration_ms=1
        ),
        actions=actions or [],
        final_response="done",
        started_at=now,
        finished_at=now,
    )


def _verdict(
    status: JudgeVerdictStatus, *, subject_id: str = "s", verdict_id: str | None = None
) -> JudgeVerdict:
    return JudgeVerdict(
        verdict_id=verdict_id or f"v-{subject_id}-{status.value}",
        subject_id=subject_id,
        status=status,
        reasoning="原文片段：「只输出 JSON」" + "补" * 300,
        temperature=0.1,
        model="anthropic/claude-haiku-4.5",
        created_at=datetime.now(UTC),
    )


# 执行决策：请求 -> Trace；返回 None 表示沙箱故障（失败态 Trace）。
type Decide = Callable[[ExecutionRequest], ExecutionTrace | None]


def _loads_target_when(predicate: Callable[[ExecutionRequest], bool]) -> Decide:
    """按谓词决定是否读取了**请求里的目标 Skill**（路径归因可识别）。"""

    def decide(request: ExecutionRequest) -> ExecutionTrace:
        loaded = predicate(request)
        actions = [_read(request.skill.skill_id)] if loaded else []
        return _trace(request.case.case_id, request.run_index, loaded=loaded, actions=actions)

    return decide


class ScriptedBackend(ExecutorBackend):
    backend_type = ExecutorBackendType.PLUGGABLE

    def __init__(self, decide: Decide | None = None) -> None:
        self.decide = decide or _loads_target_when(lambda r: True)
        self.requests: list[ExecutionRequest] = []

    async def execute(self, request: ExecutionRequest) -> ExecutionTrace:
        self.requests.append(request)
        await asyncio.sleep(0)
        trace = self.decide(request)
        if trace is None:
            return build_failure_trace(
                case_id=request.case.case_id,
                run_index=request.run_index,
                reason="boom",
                timed_out=True,
            )
        return trace

    async def health_check(self) -> bool:
        return True


class FakeSkillRepo:
    def __init__(self, skills: list[SkillDefinition]) -> None:
        self.skills = {(s.skill_id, s.version_ref): s for s in skills}

    async def get(self, skill_id: str, version_ref: str) -> SkillDefinition | None:
        return self.skills.get((skill_id, version_ref))

    async def get_latest(self, skill_id: str) -> SkillDefinition | None:
        matches = [s for (sid, _), s in self.skills.items() if sid == skill_id]
        return matches[-1] if matches else None


class FakeCaseRepo:
    def __init__(self, cases: list[TestCase]) -> None:
        self.cases = cases

    async def list_by_ids(self, case_ids: list[str]) -> list[TestCase]:
        return [c for c in reversed(self.cases) if c.case_id in case_ids]

    async def list_by_categories(
        self, suite_version_id: str, categories: list[TestCaseCategory]
    ) -> list[TestCase]:
        owner = suite_version_id.removeprefix("suite-of-")
        skill_id = TARGET_ID if suite_version_id == SUITE_ID else owner
        return [
            c for c in reversed(self.cases) if c.category in categories and c.skill_id == skill_id
        ]


class FakeSuiteRepo:
    async def get_active_version(
        self, skill_id: str, skill_version_ref: str | None = None
    ) -> TestSuiteVersion | None:
        return TestSuiteVersion(
            suite_version_id=SUITE_ID if skill_id == TARGET_ID else f"suite-of-{skill_id}",
            skill_id=skill_id,
            skill_version_ref=VERSION_REF,
            generation_mode="reuse",
            case_ids=[],
            created_at=datetime.now(UTC),
        )


class FakeSuiteService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def ensure_test_suite(
        self, skill: SkillDefinition, **kwargs: Any
    ) -> EnsureTestSuiteResult:
        self.calls.append({"skill": skill, **kwargs})
        if self.error:
            raise self.error
        version = await FakeSuiteRepo().get_active_version(skill.skill_id)
        assert version is not None
        return EnsureTestSuiteResult(suite_version=version)


class FakeCapabilityRepo:
    def __init__(self, tree: CapabilityTree | None) -> None:
        self.tree = tree

    async def get(self, skill_id: str, skill_version_ref: str) -> CapabilityTree | None:
        return self.tree


class FakeTraceRepo:
    def __init__(self) -> None:
        self.saved: list[ExecutionTrace] = []

    async def save(self, trace: ExecutionTrace) -> None:
        self.saved.append(trace)


class FakeJudgeRepo:
    def __init__(self) -> None:
        self.saved: list[JudgeVerdict] = []

    async def save_verdict(self, verdict: JudgeVerdict) -> None:
        self.saved.append(verdict)


class FakeReporter:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record_dimension_result(self, **kwargs: Any) -> None:
        self.recorded.append(kwargs)


type Judgmental = Callable[[str, str, dict[str, str]], JudgeVerdict | ConsensusResult]


class FakeJudge:
    """量化判定走真实 `JudgeAgent`（纯算术），裁量判定按回调给结果并记录调用。"""

    def __init__(self, judgmental: Judgmental | None = None) -> None:
        self._real = JudgeAgent()
        self.judgmental = judgmental or (
            lambda subject, key, content: _verdict(JudgeVerdictStatus.PASS, subject_id=subject)
        )
        self.calls: list[dict[str, Any]] = []
        self.quantitative_calls: list[dict[str, Any]] = []

    def quantitative_verdict(
        self, subject_id: str, rule_name: str, inputs: dict[str, Any]
    ) -> JudgeVerdict:
        self.quantitative_calls.append(
            {"subject_id": subject_id, "rule_name": rule_name, "inputs": inputs}
        )
        return self._real.quantitative_verdict(
            subject_id=subject_id, rule_name=rule_name, inputs=inputs
        )

    async def judgmental_verdict(
        self, subject_id: str, template_key: str, content: dict[str, str], criticality: Criticality
    ) -> JudgeVerdict | ConsensusResult:
        self.calls.append(
            {
                "subject_id": subject_id,
                "template_key": template_key,
                "content": content,
                "criticality": criticality,
            }
        )
        return self.judgmental(subject_id, template_key, content)


class FakeAlerts:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict[str, Any]] = []

    async def send(self, *, alert_type: str, run_id: str, payload: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("webhook down")
        self.sent.append({"alert_type": alert_type, "run_id": run_id, "payload": payload})


NOISE = [_skill("excel-helper"), _skill("report-writer"), _skill("json-only")]
CORE = [_skill("sql-runner")]


def _deps(
    *,
    target: SkillDefinition | None = None,
    library: list[SkillDefinition] | None = None,
    cases: list[TestCase] | None = None,
    backend: ScriptedBackend | None = None,
    judge: FakeJudge | None = None,
    reporter: FakeReporter | None = None,
    alerts: FakeAlerts | None = None,
    tree: CapabilityTree | None = None,
    suite_service: FakeSuiteService | None = None,
    settings: MultiSkillSettings | None = None,
    trace_repo: FakeTraceRepo | None = None,
    judge_repo: FakeJudgeRepo | None = None,
) -> MultiSkillDeps:
    skills = [target or _skill(), *(NOISE + CORE if library is None else library)]
    return MultiSkillDeps(
        executor_backend=backend or ScriptedBackend(),
        judge_agent=judge or FakeJudge(),  # type: ignore[arg-type]  测试替身
        test_suite_service=suite_service or FakeSuiteService(),  # type: ignore[arg-type]
        report_generator=reporter or FakeReporter(),  # type: ignore[arg-type]
        alert_dispatcher=alerts or FakeAlerts(),
        skill_repository=FakeSkillRepo(skills),  # type: ignore[arg-type]
        test_case_repository=FakeCaseRepo(cases or []),  # type: ignore[arg-type]
        test_suite_repository=FakeSuiteRepo(),  # type: ignore[arg-type]
        trace_repository=trace_repo or FakeTraceRepo(),  # type: ignore[arg-type]
        judge_repository=judge_repo or FakeJudgeRepo(),  # type: ignore[arg-type]
        capability_repository=FakeCapabilityRepo(tree),  # type: ignore[arg-type]
        multi_skill_settings=settings
        or MultiSkillSettings(
            noise_pack_skill_ids=[s.skill_id for s in NOISE],
            core_skill_ids=[s.skill_id for s in CORE],
        ),
        max_concurrent_sandboxes=10,
    )


def _refs(skills: list[SkillDefinition]) -> list[dict[str, str]]:
    return [{"skill_id": s.skill_id, "version_ref": s.version_ref} for s in skills]


def _state(**extra: Any) -> Any:
    return {
        "run_id": RUN_ID,
        "skill_id": TARGET_ID,
        "skill_version_ref": VERSION_REF,
        "active_suite_version_id": SUITE_ID,
        KEY_NOISE_PACK_REFS: _refs(NOISE),
        KEY_CORE_SKILL_REFS: _refs(CORE),
        **extra,
    }


# --------------------------------------------------------------------------- #
# 1. 加载归因
# --------------------------------------------------------------------------- #


class AttributionTests:
    def test_mount_markers_use_skill_id_and_root_dir(self) -> None:
        skill = _skill().model_copy(update={"root_path": "/repo/cleaners/csv"})
        assert skill_mount_markers(skill) == {"csv-cleaner", "csv"}
        assert skill_mount_markers(skill.model_copy(update={"root_path": "."})) == {"csv-cleaner"}

    def test_background_read_is_not_mistaken_for_target(self) -> None:
        """兜底判定 loaded=True 被干扰技能的 SKILL.md 误导时，记为证据矛盾。"""
        trace = _trace("c", 0, loaded=True, actions=[_read("excel-helper")])
        result = attribute_skill_loads(trace, target=_skill(), background=NOISE)
        assert result.contradictory is True
        assert result.target_loaded is False
        assert result.background_loaded_ids == ("excel-helper",)

    def test_target_and_background_reads_are_both_attributed(self) -> None:
        trace = _trace("c", 0, loaded=False, actions=[_read(TARGET_ID), _read("json-only", 1)])
        result = attribute_skill_loads(trace, target=_skill(), background=NOISE)
        assert result.target_loaded is True  # 路径证据最具体
        assert result.background_loaded_ids == ("json-only",)
        assert result.contradictory is False

    def test_preloaded_target_without_read_actions_trusts_the_flag(self) -> None:
        trace = _trace("c", 0, loaded=True)
        assert attribute_skill_loads(trace, target=_skill(), background=NOISE).target_loaded is True

    def test_nested_example_skill_md_is_not_attributed(self) -> None:
        step = _read(TARGET_ID).model_copy(
            update={"action_input": {"path": "/sandbox/skills/csv-cleaner/examples/x/SKILL.md"}}
        )
        result = attribute_skill_loads(
            _trace("c", 0, loaded=False, actions=[step]), target=_skill(), background=[]
        )
        assert result.target_loaded is False
        assert result.unattributed_reads == 1


# --------------------------------------------------------------------------- #
# 2. 告警分发器
# --------------------------------------------------------------------------- #


class AlertTests:
    async def test_channel_failure_is_swallowed(self) -> None:
        assert (
            await dispatch_alert(FakeAlerts(fail=True), alert_type="x", run_id="r", payload={})
            is False
        )
        ok = FakeAlerts()
        assert await dispatch_alert(ok, alert_type="x", run_id="r", payload={"a": 1}) is True
        assert ok.sent[0]["payload"] == {"a": 1}

    async def test_registry_defaults_to_logging_and_can_be_replaced(self) -> None:
        original = get_alert_dispatcher()
        try:
            assert isinstance(original, LoggingAlertDispatcher)
            replacement = FakeAlerts()
            set_alert_dispatcher(replacement)
            deps = MultiSkillDeps(multi_skill_settings=MultiSkillSettings())
            assert deps.alerts() is replacement  # 未注入时每次回落到当前注册的通道
        finally:
            set_alert_dispatcher(original)


# --------------------------------------------------------------------------- #
# 3. 纯函数探测件
# --------------------------------------------------------------------------- #


class ProbeHelperTests:
    def test_tool_collisions_are_case_insensitive_and_cross_skill_only(self) -> None:
        target = _skill(tools=("parse_data", "csv_export"))
        other = _skill("excel-helper", tools=("Parse_Data",))
        dup_inside = _skill("json-only", tools=("fmt", "fmt"))
        collisions = probes.find_tool_name_collisions([target, other, dup_inside])
        assert [(c.tool_name, c.skill_ids) for c in collisions] == [
            ("parse_data", ("csv-cleaner", "excel-helper"))
        ]

    def test_unprefixed_tools_are_suggestions(self) -> None:
        target = _skill(tools=("parse_data", "csv_export", "csv_cleaner_fix"))
        assert probes.namespace_prefixes(target) == ("csv_cleaner", "csv")
        assert probes.unprefixed_tools(target) == ["parse_data"]

    def test_ping_pong_counts_alternation_not_retries(self) -> None:
        alternating = [
            _err("validate_json", 0),
            _err("lint_md", 1),
            _err("validate_json", 2),
            _err("lint_md", 3),
            _err("validate_json", 4),
        ]
        assert probes.count_error_ping_pong(alternating) == 3
        retries = [_err("validate_json", i) for i in range(6)]
        assert probes.count_error_ping_pong(retries) == 0  # 反复试错是模块三的问题
        blocks = [_err("a", 0), _err("a", 1), _err("b", 2), _err("b", 3), _err("a", 4)]
        assert probes.count_error_ping_pong(blocks) == 1

    def test_shuffle_is_deterministic_really_shuffled_and_drops_order_markers(self) -> None:
        prompt = "先把接口返回的订单数据拉下来，然后清洗成扁平的表格，最后给财务出一份汇总报告"
        first = probes.shuffle_step_order(prompt, "seed")
        assert first == probes.shuffle_step_order(prompt, "seed")
        assert first is not None and first != prompt
        assert not any(marker in first for marker in ("先把", "然后", "最后"))
        assert probes.shuffle_step_order("帮我清洗一下这个CSV", "seed") is None
        english = probes.shuffle_step_order(
            "Download data.csv first. Then clean it. Finally report.", "s"
        )
        assert english is not None and "data.csv" in english

    def test_persona_lines_skip_code_blocks(self) -> None:
        body = "# DBA\n你是一个严谨的 DBA，拒绝任何猜测。\n```\nyou are a shell\n```\n只输出 JSON。\n普通步骤\n"
        assert probes.extract_persona_lines(body) == [
            "你是一个严谨的 DBA，拒绝任何猜测。",
            "只输出 JSON。",
        ]
        rendered = probes.format_noise_pack_for_review([_skill("mentor", body="你是发散思维导师")])
        assert "mentor" in rendered and "发散思维导师" in rendered


# --------------------------------------------------------------------------- #
# 4. 量化规则
# --------------------------------------------------------------------------- #


class RuleTests:
    def _status(self, rule: str, **inputs: Any) -> JudgeVerdictStatus:
        return (
            JudgeAgent().quantitative_verdict(subject_id="s", rule_name=rule, inputs=inputs).status
        )

    def test_reference_vs_variant_rules(self) -> None:
        fail, ok = JudgeVerdictStatus.FAIL, JudgeVerdictStatus.PASS
        for rule in (RULE_TRIGGER_HIJACK, RULE_ATTENTION_DECAY):
            assert self._status(rule, reference_ok=1, variant_ok=0) is fail
            assert self._status(rule, reference_ok=0, variant_ok=0) is ok  # 参照臂就不对：不计入
            assert self._status(rule, reference_ok=1, variant_ok=1) is ok

    def test_deadlock_threshold(self) -> None:
        assert (
            self._status(RULE_INSTRUCTION_DEADLOCK, ping_pong_count=3, threshold=3)
            is JudgeVerdictStatus.FAIL
        )
        assert (
            self._status(RULE_INSTRUCTION_DEADLOCK, ping_pong_count=2, threshold=3)
            is JudgeVerdictStatus.PASS
        )

    def test_core_regression_requires_drop_below_floor_and_below_baseline(self) -> None:
        base = {"baseline_conclusive": 5, "crowded_conclusive": 5, "min_rate": 0.8}
        assert (
            self._status(RULE_CORE_REGRESSION, baseline_loaded=5, crowded_loaded=3, **base)
            is JudgeVerdictStatus.FAIL
        )
        # 独立执行就只有 60%：不归因于本 Skill。
        assert (
            self._status(RULE_CORE_REGRESSION, baseline_loaded=3, crowded_loaded=3, **base)
            is JudgeVerdictStatus.PASS
        )
        assert (
            self._status(RULE_CORE_REGRESSION, baseline_loaded=5, crowded_loaded=4, **base)
            is JudgeVerdictStatus.PASS
        )
        # 没有证据绝不能读成"没有回归"。
        no_evidence = {**base, "crowded_conclusive": 0}
        assert (
            self._status(RULE_CORE_REGRESSION, baseline_loaded=5, crowded_loaded=0, **no_evidence)
            is JudgeVerdictStatus.FAIL
        )


# --------------------------------------------------------------------------- #
# 5. 模板
# --------------------------------------------------------------------------- #


class TemplateTests:
    def test_three_templates_registered_and_render(self) -> None:
        flow = get_template("semantic_flow_friction").render(
            {
                "prompt": "先拉数据再清洗",
                "background_skills": "- report-writer：写周报",
                "actions": "[step:0] bash",
            }
        )
        assert "report-writer" in flow and "[step:0]" in flow
        role = get_template("role_persona_conflict").render(
            {"skill_md": "你是 DBA", "noise_pack_descriptions": "### mentor"}
        )
        assert "### mentor" in role
        adherence = get_template("negative_constraint_adherence").render(
            {
                "constraint_description": "删除前确认路径",
                "case_prompt": "删掉旧文件",
                "actions": "-",
                "final_response": "ok",
            }
        )
        assert "删除前确认路径" in adherence

    def test_settings_are_wired(self) -> None:
        settings = Settings().multi_skill
        assert settings.core_regression_min_rate == 0.8
        assert settings.deep_conflict_alert_threshold == 3
        assert settings.noise_pack_skill_ids == []


# --------------------------------------------------------------------------- #
# 6. 节点
# --------------------------------------------------------------------------- #


class PrepareTests:
    async def test_loads_library_excludes_self_and_missing_and_requests_cases(self) -> None:
        service = FakeSuiteService()
        settings = MultiSkillSettings(
            noise_pack_skill_ids=["excel-helper", TARGET_ID, "ghost", "excel-helper"],
            core_skill_ids=["sql-runner"],
        )
        cases = [
            _case("m2", category=TestCaseCategory.MULTI_SKILL),
            _case("m1", category=TestCaseCategory.MULTI_SKILL),
            _case("m0", category=TestCaseCategory.MULTI_SKILL, split=DatasetSplit.COLD),
        ]
        deps = _deps(settings=settings, suite_service=service, cases=cases)
        update = await MultiSkillPipeline(deps).prepare_multi_skill_context(_state())

        assert update[KEY_NOISE_PACK_REFS] == [
            {"skill_id": "excel-helper", "version_ref": VERSION_REF}
        ]
        assert update[KEY_CORE_SKILL_REFS] == _refs(CORE)
        notes = " ".join(update[KEY_CONTEXT_NOTES])
        assert "被测 Skill 自身" in notes and "ghost" in notes and "建议 3~5 个" in notes
        call = service.calls[0]
        assert call["extra_categories"] == [TestCaseCategory.MULTI_SKILL]
        assert call["category_counts"] == {
            TestCaseCategory.MULTI_SKILL: settings.multi_skill_case_count
        }
        assert [s.skill_id for s in call["background_skills"]] == ["excel-helper"]
        assert update[KEY_CASE_IDS] == ["m1", "m2"]  # 冷数据区排除、按 id 排序
        assert update["active_suite_version_id"] == SUITE_ID

    async def test_empty_noise_pack_requests_zero_cases(self) -> None:
        service = FakeSuiteService()
        deps = _deps(settings=MultiSkillSettings(), suite_service=service)
        update = await MultiSkillPipeline(deps).prepare_multi_skill_context(_state())
        assert update[KEY_NOISE_PACK_REFS] == []
        assert service.calls[0]["category_counts"] == {TestCaseCategory.MULTI_SKILL: 0}

    async def test_generation_failure_is_reported_not_raised(self) -> None:
        deps = _deps(suite_service=FakeSuiteService(error=GenerationError("LLM down")))
        update = await MultiSkillPipeline(deps).prepare_multi_skill_context(_state())
        assert any("生成失败" in n for n in update[KEY_CONTEXT_NOTES])
        assert "active_suite_version_id" not in update


class NamespaceTests:
    async def test_collision_involving_target_is_a_finding(self) -> None:
        target = _skill(tools=("parse_data",))
        noise = [
            _skill("excel-helper", tools=("parse_data", "fmt")),
            _skill("json-only", tools=("fmt",)),
        ]
        judge_repo = FakeJudgeRepo()
        deps = _deps(target=target, library=noise, judge_repo=judge_repo)
        update = await MultiSkillPipeline(deps).namespace_pollution_static_scan(
            _state(**{KEY_NOISE_PACK_REFS: _refs(noise)})
        )
        outcome = ProbeOutcome.model_validate(update[KEY_NAMESPACE_OUTCOME])
        assert len(outcome.findings) == 1 and "[命名污染]" in outcome.findings[0]
        assert "csv_cleaner_parse_data" in outcome.findings[0]
        assert any("干扰包内部" in d and "fmt" in d for d in outcome.details)
        assert any("未带命名空间前缀" in d for d in outcome.details)
        assert update["judge_verdict_ids"] == [judge_repo.saved[0].verdict_id]

    async def test_no_noise_pack_is_skipped(self) -> None:
        update = await MultiSkillPipeline(_deps()).namespace_pollution_static_scan(
            _state(**{KEY_NOISE_PACK_REFS: []})
        )
        assert ProbeOutcome.model_validate(update[KEY_NAMESPACE_OUTCOME]).status == "skipped"


class HijackTests:
    async def test_hijack_overtrigger_reference_failure_and_inconclusive(self) -> None:
        cases = [_case(f"p{i}") for i in range(4)]

        def decide(request: ExecutionRequest) -> ExecutionTrace | None:
            crowded = bool(request.background_skills)
            cid = request.case.case_id
            if cid == "p0":  # 劫持：单测触发，并发不触发
                return _loads_target_when(lambda r: not crowded)(request)
            if cid == "p1":  # 背景过触发：并发时额外读了干扰技能
                actions = [_read(TARGET_ID)] + ([_read("excel-helper", 1)] if crowded else [])
                return _trace(cid, request.run_index, loaded=True, actions=actions)
            if cid == "p2":  # 单测就不触发：模块一的问题
                return _trace(cid, request.run_index, loaded=False)
            return None if crowded else _loads_target_when(lambda r: True)(request)  # p3 沙箱故障

        backend = ScriptedBackend(decide)
        judge_repo, trace_repo = FakeJudgeRepo(), FakeTraceRepo()
        deps = _deps(
            backend=backend,
            cases=cases,
            judge_repo=judge_repo,
            trace_repo=trace_repo,
            settings=MultiSkillSettings(max_hijack_probe_cases=10),
        )
        update = await MultiSkillPipeline(deps).cross_trigger_interference_probe(_state())
        outcome = ProbeOutcome.model_validate(update[KEY_HIJACK_OUTCOME])

        assert any(f.startswith("[劫持] p0") for f in outcome.findings)
        assert any(
            f.startswith("[背景过触发] p1") and "excel-helper" in f for f in outcome.findings
        )
        assert not any("p2" in f for f in outcome.findings)
        assert any("p2" in d and "触发准确度" in d for d in outcome.details)
        assert outcome.inconclusive_ids == ["p3"]
        assert {r.run_index for r in backend.requests} == {
            RUN_INDEX_MULTI_SKILL_HIJACK_SOLO,
            RUN_INDEX_MULTI_SKILL_HIJACK_CROWDED,
        }
        assert all(
            (r.run_index == RUN_INDEX_MULTI_SKILL_HIJACK_CROWDED) == bool(r.background_skills)
            for r in backend.requests
        )
        assert len(trace_repo.saved) == 8 and len(update["executed_trace_ids"]) == 8
        assert update["judge_verdict_ids"] == [
            v.verdict_id for v in judge_repo.saved
        ]  # 只回报已归档的 id

    async def test_sample_is_capped(self) -> None:
        backend = ScriptedBackend()
        deps = _deps(
            backend=backend,
            cases=[_case(f"p{i}") for i in range(9)],
            settings=MultiSkillSettings(max_hijack_probe_cases=2),
        )
        await MultiSkillPipeline(deps).cross_trigger_interference_probe(_state())
        assert sorted({r.case.case_id for r in backend.requests}) == ["p0", "p1"]


class AntagonismTests:
    async def test_deadlock_friction_and_healthy_cases(self) -> None:
        cases = [_case(f"m{i}", category=TestCaseCategory.MULTI_SKILL) for i in range(4)]

        def decide(request: ExecutionRequest) -> ExecutionTrace | None:
            cid = request.case.case_id
            read = [_read(TARGET_ID)]
            if cid == "m0":
                errors = [
                    _err("validate_json", i + 1) if i % 2 == 0 else _err("lint_md", i + 1)
                    for i in range(5)
                ]
                return _trace(cid, request.run_index, loaded=True, actions=read + errors)
            if cid == "m3":
                return None
            return _trace(cid, request.run_index, loaded=True, actions=read)

        def judgmental(subject: str, key: str, content: dict[str, str]) -> JudgeVerdict:
            if subject.endswith("m2"):
                return _verdict(JudgeVerdictStatus.PASS, subject_id=golden_subject_id("g1"))
            status = JudgeVerdictStatus.FAIL if subject.endswith("m1") else JudgeVerdictStatus.PASS
            return _verdict(status, subject_id=subject)

        judge = FakeJudge(judgmental)
        deps = _deps(backend=ScriptedBackend(decide), cases=cases, judge=judge)
        update = await MultiSkillPipeline(deps).instruction_antagonism_and_semantic_flow_probe(
            _state(**{KEY_CASE_IDS: [c.case_id for c in cases]})
        )
        outcome = ProbeOutcome.model_validate(update[KEY_ANTAGONISM_OUTCOME])
        assert any(f.startswith("[死锁] m0") for f in outcome.findings)
        assert any(f.startswith("[语义断层] m1") for f in outcome.findings)
        assert outcome.inconclusive_ids == ["m3"]
        assert outcome.healthy_case_ids == ["m1", "m2"]  # m0 死锁不健康
        assert any("m2" in d and "黄金基准" in d for d in outcome.details)
        call = judge.calls[0]
        assert call["template_key"] == "semantic_flow_friction"
        assert call["criticality"] is Criticality.ROUTINE
        assert "report-writer" in call["content"]["background_skills"]
        assert "[step:" in call["content"]["actions"]

    async def test_no_cases_is_skipped(self) -> None:
        update = await MultiSkillPipeline(_deps()).instruction_antagonism_and_semantic_flow_probe(
            _state(**{KEY_CASE_IDS: []})
        )
        assert ProbeOutcome.model_validate(update[KEY_ANTAGONISM_OUTCOME]).status == "skipped"

    async def test_no_consensus_suspends(self) -> None:
        cases = [_case("m0", category=TestCaseCategory.MULTI_SKILL)]
        judge = FakeJudge(
            lambda s, k, c: ConsensusResult(
                subject_id=s,
                consensus_reached=False,
                final_status=JudgeVerdictStatus.NEEDS_HUMAN_REVIEW,
            )
        )
        deps = _deps(cases=cases, judge=judge)
        with pytest.raises(PipelineSuspended):
            await MultiSkillPipeline(deps).instruction_antagonism_and_semantic_flow_probe(
                _state(**{KEY_CASE_IDS: ["m0"]})
            )


def _tree(constraints: list[NegativeConstraint]) -> CapabilityTree:
    return CapabilityTree(
        skill_id=TARGET_ID, skill_version_ref=VERSION_REF, negative_constraints=constraints
    )


class AttentionTests:
    async def test_decay_detected_when_only_crowded_run_violates(self) -> None:
        constraint = NegativeConstraint(
            constraint_id="neg-b",
            description="删除前先确认路径存在",
            covering_case_ids=["p9", "p5"],
        )
        other = NegativeConstraint(constraint_id="neg-a", description="无用例的约束")
        cases = [_case("p5"), _case("p9"), _case("p1", constraint_ids=["neg-z"])]

        def judgmental(subject: str, key: str, content: dict[str, str]) -> JudgeVerdict:
            status = JudgeVerdictStatus.FAIL if "crowded" in subject else JudgeVerdictStatus.PASS
            return _verdict(status, subject_id=subject)

        backend = ScriptedBackend(
            lambda r: _trace(
                r.case.case_id,
                r.run_index,
                loaded=True,
                tokens=90000 if r.background_skills else 1000,
            )
        )
        judge, judge_repo = FakeJudge(judgmental), FakeJudgeRepo()
        deps = _deps(
            backend=backend,
            cases=cases,
            judge=judge,
            tree=_tree([constraint, other]),
            judge_repo=judge_repo,
        )
        update = await MultiSkillPipeline(deps).context_exhaustion_attention_decay_probe(_state())
        outcome = ProbeOutcome.model_validate(update[KEY_ATTENTION_OUTCOME])

        assert {r.case.case_id for r in backend.requests} == {
            "p5"
        }  # 约束按 id、用例按 case_id 取最小
        assert {r.run_index for r in backend.requests} == {
            RUN_INDEX_MULTI_SKILL_ATTENTION_SOLO,
            RUN_INDEX_MULTI_SKILL_ATTENTION_CROWDED,
        }
        assert (
            len(outcome.findings) == 1
            and "[注意力衰减]" in outcome.findings[0]
            and "90000" in outcome.findings[0]
        )
        assert [c["template_key"] for c in judge.calls] == ["negative_constraint_adherence"] * 2
        assert not any("未逼近" in d for d in outcome.details)  # 90000 ≥ 80000*0.5
        assert judge_repo.saved[0].verdict_id in update["judge_verdict_ids"]
        assert len(update["executed_trace_ids"]) == 2

    async def test_low_watermark_is_annotated(self) -> None:
        constraint = NegativeConstraint(
            constraint_id="neg-a", description="d", covering_case_ids=["p1"]
        )
        deps = _deps(cases=[_case("p1")], tree=_tree([constraint]))
        update = await MultiSkillPipeline(deps).context_exhaustion_attention_decay_probe(_state())
        outcome = ProbeOutcome.model_validate(update[KEY_ATTENTION_OUTCOME])
        assert outcome.findings == [] and any("未逼近挤兑阈值" in d for d in outcome.details)

    async def test_missing_tree_no_constraints_and_no_probe_case(self) -> None:
        pipeline = MultiSkillPipeline(_deps(tree=None))
        outcome = ProbeOutcome.model_validate(
            (await pipeline.context_exhaustion_attention_decay_probe(_state()))[
                KEY_ATTENTION_OUTCOME
            ]
        )
        assert outcome.status == "skipped"
        pipeline = MultiSkillPipeline(_deps(tree=_tree([])))
        outcome = ProbeOutcome.model_validate(
            (await pipeline.context_exhaustion_attention_decay_probe(_state()))[
                KEY_ATTENTION_OUTCOME
            ]
        )
        assert outcome.status == "not_applicable"
        tree = _tree([NegativeConstraint(constraint_id="neg-a", description="d")])
        pipeline = MultiSkillPipeline(_deps(tree=tree, cases=[_case("p1")]))
        outcome = ProbeOutcome.model_validate(
            (await pipeline.context_exhaustion_attention_decay_probe(_state()))[
                KEY_ATTENTION_OUTCOME
            ]
        )
        assert outcome.status == "skipped" and "没有任何诱导" in (outcome.note or "")


class RoleAndTemporalTests:
    async def test_role_conflict_and_topological_fragility(self) -> None:
        prompt = "先把接口返回的订单数据拉下来，然后清洗成扁平的表格，最后给财务出一份汇总报告"
        cases = [
            _case("m1", category=TestCaseCategory.MULTI_SKILL, prompt=prompt),
            _case("m2", category=TestCaseCategory.MULTI_SKILL, prompt="清洗一下"),
        ]
        antagonism = ProbeOutcome(probe="a", status="completed", healthy_case_ids=["m1", "m2"])

        def judgmental(subject: str, key: str, content: dict[str, str]) -> JudgeVerdict:
            return _verdict(JudgeVerdictStatus.FAIL, subject_id=subject)

        backend = ScriptedBackend(lambda r: None)  # 打乱顺序后崩溃
        target = _skill(body="你是世界级的数据清洗专家。\n只输出 JSON。\n")
        judge = FakeJudge(judgmental)
        deps = _deps(target=target, backend=backend, cases=cases, judge=judge)
        update = await MultiSkillPipeline(deps).role_collision_and_temporal_static_scan(
            _state(**{KEY_ANTAGONISM_OUTCOME: antagonism.model_dump()})
        )
        role = ProbeOutcome.model_validate(update[KEY_ROLE_OUTCOME])
        temporal = ProbeOutcome.model_validate(update[KEY_TEMPORAL_OUTCOME])

        assert role.findings and role.findings[0].startswith("[角色冲突]")
        assert any("风格强制降级" in d for d in role.details)
        assert "只输出 JSON" in judge.calls[0]["content"]["skill_md"]
        assert "excel-helper" in judge.calls[0]["content"]["noise_pack_descriptions"]
        assert len(temporal.findings) == 1 and temporal.findings[0].startswith("[拓扑脆弱] m1")
        assert any("m2" in d for d in temporal.details)  # 拆不出步骤
        request = backend.requests[0]
        assert request.run_index == RUN_INDEX_MULTI_SKILL_TEMPORAL and request.background_skills
        assert request.case.prompt != prompt
        assert len(update["executed_trace_ids"]) == 1

    async def test_temporal_skipped_when_antagonism_did_not_complete(self) -> None:
        skipped = ProbeOutcome(probe="a", status="skipped", note="没有复合用例")
        update = await MultiSkillPipeline(_deps()).role_collision_and_temporal_static_scan(
            _state(**{KEY_ANTAGONISM_OUTCOME: skipped.model_dump()})
        )
        temporal = ProbeOutcome.model_validate(update[KEY_TEMPORAL_OUTCOME])
        assert temporal.status == "skipped" and "没有复合用例" in (temporal.note or "")


class CoreRegressionTests:
    def _cases(self, skill_id: str, n: int = 5) -> list[TestCase]:
        return [_case(f"{skill_id}-p{i}", skill_id=skill_id) for i in range(n)]

    async def test_drop_attributable_to_target_breaks_the_gate(self) -> None:
        cores = [_skill("sql-runner"), _skill("weak-core"), _skill("no-suite-core")]

        def decide(request: ExecutionRequest) -> ExecutionTrace:
            core = request.skill.skill_id
            index = int(request.case.case_id.rsplit("p", 1)[1])
            crowded = bool(request.background_skills)
            if core == "sql-runner":  # 独立 5/5，以本 Skill 为背景 2/5
                loaded = not crowded or index < 2
            else:  # weak-core：两边都是 3/5
                loaded = index < 3
            return _loads_target_when(lambda r: loaded)(request)

        backend = ScriptedBackend(decide)
        cases = self._cases("sql-runner") + self._cases("weak-core")
        judge_repo = FakeJudgeRepo()
        deps = _deps(backend=backend, cases=cases, library=[*NOISE, *cores], judge_repo=judge_repo)
        update = await MultiSkillPipeline(deps).core_skill_regression_gate(
            _state(**{KEY_CORE_SKILL_REFS: _refs(cores)})
        )
        outcome = ProbeOutcome.model_validate(update[KEY_CORE_REGRESSION_OUTCOME])

        assert (
            len(outcome.findings) == 1
            and "[基石熔断]" in outcome.findings[0]
            and "sql-runner" in outcome.findings[0]
        )
        assert "100%" in outcome.findings[0] and "40%" in outcome.findings[0]
        assert any("weak-core" in d and "不归因" in d for d in outcome.details)
        assert outcome.inconclusive_ids == ["no-suite-core"]
        crowded = [r for r in backend.requests if r.run_index == RUN_INDEX_MULTI_SKILL_CORE_CROWDED]
        assert crowded and all(
            [s.skill_id for s in r.background_skills] == [TARGET_ID] for r in crowded
        )
        assert {r.run_index for r in backend.requests} == {
            RUN_INDEX_MULTI_SKILL_CORE_BASELINE,
            RUN_INDEX_MULTI_SKILL_CORE_CROWDED,
        }
        assert update["judge_verdict_ids"] == [v.verdict_id for v in judge_repo.saved]

    async def test_no_evidence_is_inconclusive_not_pass(self) -> None:
        backend = ScriptedBackend(
            lambda r: (
                None if r.background_skills else _trace(r.case.case_id, r.run_index, loaded=True)
            )
        )
        deps = _deps(backend=backend, cases=self._cases("sql-runner", 2))
        update = await MultiSkillPipeline(deps).core_skill_regression_gate(_state())
        outcome = ProbeOutcome.model_validate(update[KEY_CORE_REGRESSION_OUTCOME])
        assert outcome.findings == [] and outcome.inconclusive_ids == ["sql-runner"]

    async def test_unconfigured_gate_is_skipped(self) -> None:
        update = await MultiSkillPipeline(_deps()).core_skill_regression_gate(
            _state(**{KEY_CORE_SKILL_REFS: []})
        )
        outcome = ProbeOutcome.model_validate(update[KEY_CORE_REGRESSION_OUTCOME])
        assert outcome.status == "skipped" and "唯一的阻断闸门" in (outcome.note or "")


class FinalizeTests:
    def _outcomes(self, **overrides: ProbeOutcome) -> dict[str, Any]:
        keys = [
            KEY_NAMESPACE_OUTCOME,
            KEY_HIJACK_OUTCOME,
            KEY_ANTAGONISM_OUTCOME,
            KEY_ATTENTION_OUTCOME,
            KEY_ROLE_OUTCOME,
            KEY_TEMPORAL_OUTCOME,
            KEY_CORE_REGRESSION_OUTCOME,
        ]
        values = {key: ProbeOutcome(probe=key, status="completed").model_dump() for key in keys}
        values.update({key: outcome.model_dump() for key, outcome in overrides.items()})
        return values

    async def _finalize(
        self, state: Any, alerts: FakeAlerts | None = None
    ) -> tuple[dict[str, Any], dict[str, Any], FakeAlerts]:
        reporter, alerts = FakeReporter(), alerts or FakeAlerts()
        update = await MultiSkillPipeline(
            _deps(reporter=reporter, alerts=alerts)
        ).finalize_dimension_report(state)
        return reporter.recorded[0], update, alerts

    async def test_all_clean_is_pass(self) -> None:
        recorded, update, alerts = await self._finalize(_state(**self._outcomes()))
        assert recorded["dimension"] == DIMENSION
        assert recorded["status"] is JudgeVerdictStatus.PASS and recorded["blocking"] is False
        assert recorded["score"] is None
        assert update[KEY_ALERT_DISPATCHED] is False and alerts.sent == []

    async def test_core_breach_blocks_and_always_alerts(self) -> None:
        core = ProbeOutcome(probe="c", status="completed", findings=["[基石熔断] x"])
        recorded, update, alerts = await self._finalize(
            _state(**self._outcomes(**{KEY_CORE_REGRESSION_OUTCOME: core}))
        )
        assert recorded["status"] is JudgeVerdictStatus.FAIL and recorded["blocking"] is True
        assert recorded["findings"][0] == "[基石熔断] x"
        assert update[KEY_ALERT_DISPATCHED] is True
        payload = alerts.sent[0]["payload"]
        assert alerts.sent[0]["alert_type"] == "deep_multi_skill_conflict"
        assert payload["blocking"] is True and payload["hard_findings"] == ["[基石熔断] x"]
        assert payload["noise_pack"] == [s.skill_id for s in NOISE]

    async def test_soft_findings_fail_without_blocking_and_alert_only_at_threshold(self) -> None:
        two = ProbeOutcome(probe="h", status="completed", findings=["[劫持] a", "[劫持] b"])
        recorded, update, alerts = await self._finalize(
            _state(**self._outcomes(**{KEY_HIJACK_OUTCOME: two}))
        )
        assert recorded["status"] is JudgeVerdictStatus.FAIL and recorded["blocking"] is False
        assert alerts.sent == []
        three = two.model_copy(update={"findings": [*two.findings, "[劫持] c"]})
        _, update, alerts = await self._finalize(
            _state(**self._outcomes(**{KEY_HIJACK_OUTCOME: three}))
        )
        assert (
            update[KEY_ALERT_DISPATCHED] is True and alerts.sent[0]["payload"]["blocking"] is False
        )

    async def test_alert_channel_failure_does_not_break_reporting(self) -> None:
        core = ProbeOutcome(probe="c", status="completed", findings=["[基石熔断] x"])
        recorded, update, _ = await self._finalize(
            _state(**self._outcomes(**{KEY_CORE_REGRESSION_OUTCOME: core})),
            alerts=FakeAlerts(fail=True),
        )
        assert recorded["blocking"] is True and update[KEY_ALERT_DISPATCHED] is False

    async def test_skipped_inconclusive_or_missing_need_human_review(self) -> None:
        skipped = ProbeOutcome(probe="n", status="skipped", note="基准干扰包为空")
        recorded, _, _ = await self._finalize(
            _state(
                **self._outcomes(**{KEY_HIJACK_OUTCOME: skipped, KEY_ANTAGONISM_OUTCOME: skipped})
            )
        )
        assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        assert sum("基准干扰包为空" in f for f in recorded["findings"]) == 1  # 去重
        inconclusive = ProbeOutcome(probe="c", status="completed", inconclusive_ids=["sql-runner"])
        recorded, _, _ = await self._finalize(
            _state(**self._outcomes(**{KEY_CORE_REGRESSION_OUTCOME: inconclusive}))
        )
        assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        state = _state(**self._outcomes())
        del state[KEY_TEMPORAL_OUTCOME]
        recorded, _, _ = await self._finalize(state)
        assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        assert any(KEY_TEMPORAL_OUTCOME in f for f in recorded["findings"])

    async def test_not_applicable_does_not_need_human(self) -> None:
        na = ProbeOutcome(probe="a", status="not_applicable", note="没有负向约束")
        recorded, _, _ = await self._finalize(
            _state(**self._outcomes(**{KEY_ATTENTION_OUTCOME: na}), **{KEY_CONTEXT_NOTES: ["n1"]})
        )
        assert recorded["status"] is JudgeVerdictStatus.PASS
        assert "准备阶段：n1" in recorded["findings"]


class AssemblyTests:
    def test_graph_shape(self) -> None:
        graph = build_multi_skill_subgraph(_deps()).compile()
        edges = {(e.source, e.target) for e in graph.get_graph().edges}
        assert (ENTRY_NODE, NODE_NAMES["namespace_pollution_static_scan"]) in edges
        for probe in PROBE_NODES:
            assert (NODE_NAMES["namespace_pollution_static_scan"], probe) in edges
            assert (probe, NODE_NAMES["role_collision_and_temporal_static_scan"]) in edges
        assert (
            NODE_NAMES["role_collision_and_temporal_static_scan"],
            NODE_NAMES["core_skill_regression_gate"],
        ) in edges
        assert (
            NODE_NAMES["core_skill_regression_gate"],
            NODE_NAMES["finalize_dimension_report"],
        ) in edges
        assert all(name.startswith("multi_skill.") for name in NODE_NAMES.values())

    async def test_end_to_end_subgraph_run(self) -> None:
        """干扰包劫持全部正向用例 → FAIL；基石健康 → 不阻断；三条以上软发现 → 告警。"""
        target_cases = [_case(f"p{i}") for i in range(3)] + [
            _case(
                "m1", category=TestCaseCategory.MULTI_SKILL, prompt="先拉数据，然后清洗，最后出报告"
            )
        ]
        core_cases = [_case(f"sql-runner-p{i}", skill_id="sql-runner") for i in range(2)]

        def decide(request: ExecutionRequest) -> ExecutionTrace:
            hijacked = (
                request.skill.skill_id == TARGET_ID
                and request.case.category is TestCaseCategory.POSITIVE
            )
            return _loads_target_when(lambda r: not (hijacked and r.background_skills))(request)

        reporter, alerts = FakeReporter(), FakeAlerts()
        deps = _deps(
            backend=ScriptedBackend(decide),
            cases=target_cases + core_cases,
            reporter=reporter,
            alerts=alerts,
            tree=_tree([]),
        )
        final = (
            await build_multi_skill_subgraph(deps)
            .compile()
            .ainvoke({"run_id": RUN_ID, "skill_id": TARGET_ID, "skill_version_ref": VERSION_REF})
        )
        recorded = reporter.recorded[0]
        assert recorded["status"] is JudgeVerdictStatus.FAIL
        assert recorded["blocking"] is False
        assert sum(f.startswith("[劫持]") for f in recorded["findings"]) == 3
        assert final[KEY_ALERT_DISPATCHED] is True and alerts.sent
        # 三条支路并行写 add-reducer 字段：id 不丢、不重复。
        assert len(final["executed_trace_ids"]) == len(set(final["executed_trace_ids"]))

    def test_non_pluggable_routing_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skill_evaluate.executors import routing

        monkeypatch.setitem(routing.NODE_BACKEND_ROUTING, DIMENSION, ExecutorBackendType.MINI)
        with pytest.raises(ConfigurationError):
            MultiSkillPipeline(_deps())
