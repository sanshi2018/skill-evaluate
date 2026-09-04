"""docs/dev/11：模块一——触发准确度与泛化能力评测流水线。

覆盖：触发率量化规则的阈值与无证据兜底、测试集准备与类别/split 过滤、冗余执行与
并发上限、判定落库口径（只归档失败）、条件路由、优化闭环的接入与工作副本传递、
验证集用打过补丁的版本重跑、报告口径、以及图结构层面"验证集不回优化闭环"的约束。
全部用替身注入，不碰数据库、不发真实请求。
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from skill_evaluate.agents.generator.service import EnsureTestSuiteResult
from skill_evaluate.agents.judge.service import JudgeAgent
from skill_evaluate.agents.optimizer.patch_applier import working_version_ref
from skill_evaluate.errors import PersistenceError, PipelineSuspended
from skill_evaluate.executors.base import ExecutionRequest, ExecutorBackend
from skill_evaluate.nodes.trigger_accuracy import (
    NODE_NAMES,
    TriggerAccuracyDeps,
    TriggerAccuracyPipeline,
    build_trigger_accuracy_subgraph,
    rule_for_category,
    trigger_rate_inputs,
)
from skill_evaluate.nodes.trigger_accuracy.graph import INTERRUPT_BEFORE_NODES
from skill_evaluate.nodes.trigger_accuracy.rules import (
    RULE_TRIGGER_RATE_NEGATIVE,
    RULE_TRIGGER_RATE_POSITIVE,
)
from skill_evaluate.nodes.trigger_accuracy.state import (
    KEY_APPLIED_PATCH_ID,
    KEY_CASE_IDS,
    KEY_SUITE_STALENESS_WARNING,
    KEY_TRAIN_CASE_IDS,
    KEY_TRAIN_FAILED_CASE_IDS,
    KEY_VALIDATION_CASE_IDS,
    KEY_VALIDATION_FAILED_CASE_IDS,
    KEY_WORKING_SKILL,
)
from skill_evaluate.state.enums import (
    DatasetSplit,
    ExecutorBackendType,
    JudgeVerdictStatus,
    PatchType,
    TestCaseCategory,
)
from skill_evaluate.state.judge import JudgeVerdict
from skill_evaluate.state.patch import Patch
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion
from skill_evaluate.state.trace import ExecutionTrace, TimingCostMetrics

SKILL_ID = "csv-cleaner"
RUN_ID = "run-1"
BASE_REF = "v1"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def _skill(description: str = "清洗 CSV 导出文件", version_ref: str = BASE_REF) -> SkillDefinition:
    return SkillDefinition(
        skill_id=SKILL_ID,
        version_ref=version_ref,
        root_path=".",
        description=description,
        body_markdown="# CSV Cleaner\n",
        line_count=1,
        token_count=40,
    )


def _case(
    case_id: str,
    *,
    category: TestCaseCategory = TestCaseCategory.POSITIVE,
    split: DatasetSplit = DatasetSplit.TRAIN,
) -> TestCase:
    return TestCase(
        case_id=case_id,
        skill_id=SKILL_ID,
        category=category,
        split=split,
        prompt=f"帮我处理导出的表格（{case_id}）",
        generator_run_id="gen-1",
        created_at=datetime.now(UTC),
    )


def _trace(case_id: str, run_index: int, *, loaded: bool) -> ExecutionTrace:
    now = datetime.now(UTC)
    return ExecutionTrace(
        trace_id=f"t-{case_id}-{run_index}",
        case_id=case_id,
        run_index=run_index,
        backend_type=ExecutorBackendType.PLUGGABLE.value,
        loaded_skill_md=loaded,
        timing=TimingCostMetrics(
            total_tokens=10, prompt_tokens=6, completion_tokens=4, duration_ms=5
        ),
        final_response="done",
        started_at=now,
        finished_at=now,
    )


class FakeSkillRepo:
    def __init__(self, skill: SkillDefinition | None) -> None:
        self.skill = skill

    async def get(self, skill_id: str, version_ref: str) -> SkillDefinition | None:
        return self.skill


class FakeSuiteService:
    """替代 `TestSuiteService`：不出题，直接给一份既有用例集。"""

    def __init__(self, case_ids: list[str], *, staleness: str | None = None) -> None:
        self.case_ids = case_ids
        self.staleness = staleness
        self.calls = 0

    async def ensure_test_suite(self, skill: SkillDefinition) -> EnsureTestSuiteResult:
        self.calls += 1
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
        self.by_id = {c.case_id: c for c in cases}

    async def list_by_ids(self, case_ids: list[str]) -> list[TestCase]:
        # 刻意打乱返回顺序：节点侧承诺按 prepare 阶段的顺序还原。
        return [self.by_id[cid] for cid in reversed(case_ids) if cid in self.by_id]


class FakeTraceRepo:
    def __init__(self) -> None:
        self.saved: list[ExecutionTrace] = []

    async def save(self, trace: ExecutionTrace) -> None:
        self.saved = [t for t in self.saved if (t.case_id, t.run_index) != (trace.case_id, trace.run_index)]
        self.saved.append(trace)

    async def list_by_case(self, case_id: str) -> list[ExecutionTrace]:
        return [t for t in self.saved if t.case_id == case_id]


class FakeJudgeRepo:
    def __init__(self) -> None:
        self.saved: list[JudgeVerdict] = []

    async def save_verdict(self, verdict: JudgeVerdict) -> None:
        self.saved.append(verdict)

    async def list_verdicts(self, subject_id: str) -> list[JudgeVerdict]:
        return [v for v in self.saved if v.subject_id == subject_id]


class FakeRunRepo:
    def __init__(self) -> None:
        self.suite_version_id: str | None = None

    async def set_suite_version(self, run_id: str, suite_version_id: str) -> None:
        self.suite_version_id = suite_version_id


class FakeReporter:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record_dimension_result(self, **kwargs: Any) -> None:
        self.recorded.append(kwargs)


class RecordingBackend(ExecutorBackend):
    """按 {case_id: [每次执行是否加载 SKILL.md]} 回放，并记录并发峰值与用到的 Skill。"""

    backend_type = ExecutorBackendType.PLUGGABLE

    def __init__(self, loaded_by_case: dict[str, list[bool]]) -> None:
        self.loaded_by_case = loaded_by_case
        self.requests: list[ExecutionRequest] = []
        self.in_flight = 0
        self.peak_in_flight = 0

    async def execute(self, request: ExecutionRequest) -> ExecutionTrace:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            self.requests.append(request)
            flags = self.loaded_by_case.get(request.case.case_id, [True, True, True])
            loaded = flags[request.run_index % len(flags)]
            # 打补丁后的重跑用另一套结果：description 改了，触发行为才会变。这里
            # 模拟"把描述写宽了"——正向用例全部恢复触发（训练集闭环收敛），但反向
            # 用例也开始误触发，于是验证集把这次过拟合抓了出来。
            if request.skill.version_ref != BASE_REF:
                loaded = True
            return _trace(request.case.case_id, request.run_index, loaded=loaded)
        finally:
            self.in_flight -= 1

    async def health_check(self) -> bool:
        return True


class FakeOptimizationLoop:
    """替代 `OptimizationLoop`：调用一次 `retest_fn`，按构造参数决定收敛还是放弃。"""

    def __init__(self, *, patch: Patch | None, new_description: str = "改写后的 description") -> None:
        self.patch = patch
        self.new_description = new_description
        self.retest_results: list[Any] = []
        self.ctx: Any = None

    async def run(self, run_id: str, ctx: Any, retest_fn: Any, optimizer: Any) -> Patch | None:
        self.ctx = ctx
        if self.patch is None:
            # 真实实现会先 suspend_and_wait 再返回 None（人工放弃）。
            return None
        patched = ctx.skill.model_copy(
            update={
                "description": self.new_description,
                "version_ref": working_version_ref(ctx.skill.version_ref, self.patch.patch_id),
            }
        )
        self.retest_results.append(await retest_fn(patched))
        return self.patch


def _patch(patch_id: str = "p-1") -> Patch:
    return Patch(
        patch_id=patch_id,
        skill_id=SKILL_ID,
        base_skill_version_ref=BASE_REF,
        patch_type=PatchType.DESCRIPTION_PATCH,
        target_path="SKILL.md",
        diff="@@ -1 +1 @@\n-旧\n+新\n",
        rationale="失败用例都在说「表格」",
        created_at=datetime.now(UTC),
    )


def _pipeline(
    cases: list[TestCase],
    *,
    loaded_by_case: dict[str, list[bool]] | None = None,
    skill: SkillDefinition | None = None,
    staleness: str | None = None,
    loop: FakeOptimizationLoop | None = None,
    max_concurrent: int | None = None,
) -> tuple[TriggerAccuracyPipeline, dict[str, Any]]:
    backend = RecordingBackend(loaded_by_case or {})
    doubles: dict[str, Any] = {
        "backend": backend,
        "suite": FakeSuiteService([c.case_id for c in cases], staleness=staleness),
        "cases": FakeCaseRepo(cases),
        "traces": FakeTraceRepo(),
        "verdicts": FakeJudgeRepo(),
        "runs": FakeRunRepo(),
        "reporter": FakeReporter(),
        "loop": loop,
    }
    deps = TriggerAccuracyDeps(
        executor_backend=backend,
        # `quantitative_verdict()` 是纯算术、不落库、不调 LLM，因此用真的 JudgeAgent
        # 跑，确保测的是"经 Judge 入口下结论"这条真实路径。
        judge_agent=JudgeAgent(),
        test_suite_service=doubles["suite"],
        report_generator=doubles["reporter"],
        optimization_loop=loop,
        optimizer_agent=object(),  # 由 FakeOptimizationLoop 接住，不会真的被调用
        skill_repository=FakeSkillRepo(skill if skill is not None else _skill()),
        test_case_repository=doubles["cases"],
        trace_repository=doubles["traces"],
        judge_repository=doubles["verdicts"],
        run_repository=doubles["runs"],
        max_concurrent_sandboxes=max_concurrent,
    )
    return TriggerAccuracyPipeline(deps), doubles


def _state(**extra: Any) -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "skill_id": SKILL_ID,
        "skill_version_ref": BASE_REF,
        **extra,
    }


# --------------------------------------------------------------------------- #
# 1. 量化规则
# --------------------------------------------------------------------------- #


class TriggerRateRuleTests:
    def test_inputs_count_loaded_traces(self) -> None:
        traces = [
            _trace("c-1", 0, loaded=True),
            _trace("c-1", 1, loaded=False),
            _trace("c-1", 2, loaded=True),
        ]
        assert trigger_rate_inputs(traces) == {"loaded_count": 2, "run_count": 3}

    @pytest.mark.parametrize(
        ("loaded", "expected"),
        [(3, JudgeVerdictStatus.PASS), (2, JudgeVerdictStatus.PASS), (1, JudgeVerdictStatus.FAIL)],
    )
    def test_positive_rule_threshold(self, loaded: int, expected: JudgeVerdictStatus) -> None:
        """架构文档：3 次运行中至少触发 2 次（>= 0.5）记为通过。"""
        verdict = JudgeAgent().quantitative_verdict(
            subject_id="c-1",
            rule_name=RULE_TRIGGER_RATE_POSITIVE,
            inputs={"loaded_count": loaded, "run_count": 3},
        )
        assert verdict.status is expected
        assert verdict.model == f"rule:{RULE_TRIGGER_RATE_POSITIVE}"
        assert verdict.temperature == 0.0

    @pytest.mark.parametrize(
        ("loaded", "expected"),
        [(0, JudgeVerdictStatus.PASS), (1, JudgeVerdictStatus.PASS), (2, JudgeVerdictStatus.FAIL)],
    )
    def test_negative_rule_threshold(self, loaded: int, expected: JudgeVerdictStatus) -> None:
        verdict = JudgeAgent().quantitative_verdict(
            subject_id="c-1",
            rule_name=RULE_TRIGGER_RATE_NEGATIVE,
            inputs={"loaded_count": loaded, "run_count": 3},
        )
        assert verdict.status is expected

    def test_no_execution_is_a_failure_for_both_directions(self) -> None:
        """一次都没跑成 ≠ 通过：反向用例不能靠"没有执行记录"白捡一个 PASS。"""
        judge = JudgeAgent()
        empty = trigger_rate_inputs([])
        for rule in (RULE_TRIGGER_RATE_POSITIVE, RULE_TRIGGER_RATE_NEGATIVE):
            verdict = judge.quantitative_verdict(subject_id="c-1", rule_name=rule, inputs=empty)
            assert verdict.status is JudgeVerdictStatus.FAIL

    def test_rule_for_category_rejects_other_dimensions(self) -> None:
        assert rule_for_category(TestCaseCategory.POSITIVE) == RULE_TRIGGER_RATE_POSITIVE
        assert rule_for_category(TestCaseCategory.NEGATIVE) == RULE_TRIGGER_RATE_NEGATIVE
        with pytest.raises(ValueError, match="POSITIVE / NEGATIVE"):
            rule_for_category(TestCaseCategory.ADVERSARIAL)


# --------------------------------------------------------------------------- #
# 2. prepare_test_suite
# --------------------------------------------------------------------------- #


class PrepareTestSuiteTests:
    async def test_filters_categories_and_groups_by_split(self) -> None:
        cases = [
            _case("c-pos-train"),
            _case("c-neg-train", category=TestCaseCategory.NEGATIVE),
            _case("c-pos-val", split=DatasetSplit.VALIDATION),
            _case("c-adv", category=TestCaseCategory.ADVERSARIAL),  # 模块五的用例
        ]
        pipeline, doubles = _pipeline(cases)

        update = await pipeline.prepare_test_suite(_state())

        assert update["active_suite_version_id"] == "suite-1"
        assert "c-adv" not in update[KEY_CASE_IDS]
        assert update[KEY_TRAIN_CASE_IDS] == ["c-pos-train", "c-neg-train"]
        assert update[KEY_VALIDATION_CASE_IDS] == ["c-pos-val"]
        # runs.suite_version_id 回填，供 ReportGenerator.build() 反查。
        assert doubles["runs"].suite_version_id == "suite-1"

    async def test_staleness_warning_is_carried_into_state(self) -> None:
        pipeline, _ = _pipeline([_case("c-1")], staleness="用例集绑定的是旧版本")
        update = await pipeline.prepare_test_suite(_state())
        assert update[KEY_SUITE_STALENESS_WARNING] == "用例集绑定的是旧版本"

    async def test_missing_skill_fails_loudly(self) -> None:
        pipeline, _ = _pipeline([_case("c-1")])
        pipeline.deps.skill_repository = FakeSkillRepo(None)
        with pytest.raises(PersistenceError, match="未找到被测 Skill"):
            await pipeline.prepare_test_suite(_state())


# --------------------------------------------------------------------------- #
# 3. 执行节点
# --------------------------------------------------------------------------- #


class ExecuteCasesTests:
    async def test_每条用例跑三次并只回增量(self) -> None:
        cases = [_case("c-1"), _case("c-2")]
        pipeline, doubles = _pipeline(cases)

        update = await pipeline.execute_train_cases(
            _state(**{KEY_TRAIN_CASE_IDS: ["c-1", "c-2"], "executed_trace_ids": ["pre-existing"]})
        )

        assert len(doubles["backend"].requests) == 6  # 2 条用例 × 3 次冗余执行
        assert sorted({r.run_index for r in doubles["backend"].requests}) == [0, 1, 2]
        assert len(doubles["traces"].saved) == 6
        # reducer 是 operator.add：节点只能回本次新增的 id，否则状态里已有的会被再追加一遍。
        assert "pre-existing" not in update["executed_trace_ids"]
        assert len(update["executed_trace_ids"]) == 6

    async def test_并发受信号量上限约束(self) -> None:
        cases = [_case(f"c-{i}") for i in range(6)]  # 6 × 3 = 18 个待执行请求
        pipeline, doubles = _pipeline(cases, max_concurrent=2)

        await pipeline.execute_train_cases(_state(**{KEY_TRAIN_CASE_IDS: [c.case_id for c in cases]}))

        assert doubles["backend"].peak_in_flight <= 2

    async def test_验证集只跑验证集用例(self) -> None:
        cases = [_case("c-train"), _case("c-val", split=DatasetSplit.VALIDATION)]
        pipeline, doubles = _pipeline(cases)

        await pipeline.execute_validation_cases(
            _state(**{KEY_TRAIN_CASE_IDS: ["c-train"], KEY_VALIDATION_CASE_IDS: ["c-val"]})
        )

        assert {r.case.case_id for r in doubles["backend"].requests} == {"c-val"}

    async def test_执行用的是打过补丁的工作副本(self) -> None:
        cases = [_case("c-val", split=DatasetSplit.VALIDATION)]
        pipeline, doubles = _pipeline(cases)
        patched = _skill(description="新描述", version_ref="v1+patch:p-1")

        await pipeline.execute_validation_cases(
            _state(**{KEY_VALIDATION_CASE_IDS: ["c-val"], KEY_WORKING_SKILL: patched})
        )

        assert {r.skill.description for r in doubles["backend"].requests} == {"新描述"}


# --------------------------------------------------------------------------- #
# 4. 判定节点与路由
# --------------------------------------------------------------------------- #


class JudgeCasesTests:
    async def test_按类别选规则并收集失败用例(self) -> None:
        cases = [
            _case("c-pos"),  # 正向：3 次全触发 -> PASS
            _case("c-pos-bad"),  # 正向：只触发 1 次 -> FAIL
            _case("c-neg", category=TestCaseCategory.NEGATIVE),  # 反向：全触发 -> FAIL
        ]
        pipeline, doubles = _pipeline(
            cases,
            loaded_by_case={
                "c-pos": [True, True, True],
                "c-pos-bad": [True, False, False],
                "c-neg": [True, True, True],
            },
        )
        state = _state(**{KEY_TRAIN_CASE_IDS: [c.case_id for c in cases]})
        await pipeline.execute_train_cases(state)

        update = await pipeline.judge_train_cases(state)

        assert update[KEY_TRAIN_FAILED_CASE_IDS] == ["c-pos-bad", "c-neg"]
        assert len(update["judge_verdict_ids"]) == 3
        # 只归档失败判定：通过判定成百上千条，逐条写库既慢又没人读。
        assert {v.subject_id for v in doubles["verdicts"].saved} == {"c-pos-bad", "c-neg"}

    async def test_验证集失败只记录不驱动重试(self) -> None:
        cases = [_case("c-val", split=DatasetSplit.VALIDATION)]
        pipeline, _ = _pipeline(cases, loaded_by_case={"c-val": [False, False, False]})
        state = _state(**{KEY_VALIDATION_CASE_IDS: ["c-val"]})
        await pipeline.execute_validation_cases(state)

        update = await pipeline.judge_validation_cases(state)

        assert update[KEY_VALIDATION_FAILED_CASE_IDS] == ["c-val"]
        assert KEY_TRAIN_FAILED_CASE_IDS not in update

    async def test_其他维度的Trace不参与触发率统计(self) -> None:
        """模块三（docs/dev/13）拿同一批用例跑 A/B，基线分支按定义就是"没加载 Skill"。

        那些 Trace 落在 100 起的专用号段里（`state/trace.py` 的 run_index 分配表）。
        不筛掉的话，一份 3 次里触发 2 次（本该通过）的 Skill 会被算成 5 次里触发
        2 次，触发率 0.4 < 0.5，莫名其妙地判成不达标。
        """
        cases = [_case("c-pos")]
        pipeline, doubles = _pipeline(cases, loaded_by_case={"c-pos": [True, True, False]})
        state = _state(**{KEY_TRAIN_CASE_IDS: ["c-pos"]})
        await pipeline.execute_train_cases(state)
        # 模拟模块三留下的两条 A/B Trace（那次两条分支都没加载成功）。
        await doubles["traces"].save(_trace("c-pos", 100, loaded=False))
        await doubles["traces"].save(_trace("c-pos", 101, loaded=False))

        update = await pipeline.judge_train_cases(state)

        assert update[KEY_TRAIN_FAILED_CASE_IDS] == []


class RoutingTests:
    def test_有训练集失败才进优化闭环(self) -> None:
        route = TriggerAccuracyPipeline.route_after_train_judge
        assert route(_state(**{KEY_TRAIN_FAILED_CASE_IDS: ["c-1"]})) == NODE_NAMES["optimizer_loop"]
        assert route(_state(**{KEY_TRAIN_FAILED_CASE_IDS: []})) == NODE_NAMES[
            "execute_validation_cases"
        ]
        assert route(_state()) == NODE_NAMES["execute_validation_cases"]


# --------------------------------------------------------------------------- #
# 5. 优化闭环
# --------------------------------------------------------------------------- #


class OptimizerLoopTests:
    async def test_收敛后把工作副本交给验证集(self) -> None:
        cases = [_case("c-1"), _case("c-val", split=DatasetSplit.VALIDATION)]
        loop = FakeOptimizationLoop(patch=_patch())
        pipeline, doubles = _pipeline(
            cases, loaded_by_case={"c-1": [False, False, False]}, loop=loop
        )
        state = _state(**{KEY_TRAIN_CASE_IDS: ["c-1"], KEY_TRAIN_FAILED_CASE_IDS: ["c-1"]})
        # 先造一条失败判定，供闭环作为失败证据回读。
        await doubles["verdicts"].save_verdict(
            JudgeVerdict(
                verdict_id="v-1",
                subject_id="c-1",
                status=JudgeVerdictStatus.FAIL,
                reasoning="触发率 0/3",
                temperature=0.0,
                model="rule:trigger_rate_positive",
                created_at=datetime.now(UTC),
            )
        )

        update = await pipeline.optimizer_loop(state)

        # 失败证据确实被喂进了 FailureContext（训练集约束由 build_failure_context 强制）。
        assert loop.ctx.failed_case_ids == ["c-1"]
        assert loop.ctx.verdicts[0].verdict_id == "v-1"
        # retest_fn 用打了补丁的副本重跑失败训练用例，并且这次通过了。
        assert loop.retest_results[0].passed is True
        working = update[KEY_WORKING_SKILL]
        assert working.version_ref == working_version_ref(BASE_REF, "p-1")
        assert update[KEY_APPLIED_PATCH_ID] == "p-1"

    async def test_人工放弃补丁时挂起而不是继续跑验证集(self) -> None:
        pipeline, _ = _pipeline(
            [_case("c-1")],
            loaded_by_case={"c-1": [False, False, False]},
            loop=FakeOptimizationLoop(patch=None),
        )
        state = _state(**{KEY_TRAIN_CASE_IDS: ["c-1"], KEY_TRAIN_FAILED_CASE_IDS: ["c-1"]})

        with pytest.raises(PipelineSuspended, match="人工未采纳补丁"):
            await pipeline.optimizer_loop(state)

    async def test_重跑只针对失败用例(self) -> None:
        cases = [_case("c-ok"), _case("c-bad")]
        loop = FakeOptimizationLoop(patch=_patch())
        pipeline, doubles = _pipeline(
            cases, loaded_by_case={"c-bad": [False, False, False]}, loop=loop
        )
        state = _state(**{KEY_TRAIN_CASE_IDS: ["c-ok", "c-bad"], KEY_TRAIN_FAILED_CASE_IDS: ["c-bad"]})

        await pipeline.optimizer_loop(state)

        assert {r.case.case_id for r in doubles["backend"].requests} == {"c-bad"}


# --------------------------------------------------------------------------- #
# 6. 报告
# --------------------------------------------------------------------------- #


class FinalizeReportTests:
    async def test_分数只看验证集(self) -> None:
        pipeline, doubles = _pipeline([])
        await pipeline.finalize_dimension_report(
            _state(
                **{
                    KEY_VALIDATION_CASE_IDS: ["c-1", "c-2", "c-3", "c-4"],
                    KEY_VALIDATION_FAILED_CASE_IDS: ["c-1"],
                    KEY_TRAIN_FAILED_CASE_IDS: ["c-t"],
                    KEY_APPLIED_PATCH_ID: "p-1",
                }
            )
        )

        recorded = doubles["reporter"].recorded[0]
        assert recorded["dimension"] == "trigger_accuracy"
        assert recorded["status"] is JudgeVerdictStatus.FAIL
        assert recorded["score"] == 0.75
        assert recorded["blocking"] is True
        assert any("验证集 1/4" in f for f in recorded["findings"])
        assert any("p-1" in f for f in recorded["findings"])

    async def test_全部通过时满分(self) -> None:
        pipeline, doubles = _pipeline([])
        await pipeline.finalize_dimension_report(
            _state(**{KEY_VALIDATION_CASE_IDS: ["c-1", "c-2"]})
        )
        recorded = doubles["reporter"].recorded[0]
        assert recorded["status"] is JudgeVerdictStatus.PASS
        assert recorded["score"] == 1.0

    async def test_验证集为空交人工而不是白给一个通过(self) -> None:
        pipeline, doubles = _pipeline([])
        await pipeline.finalize_dimension_report(_state())
        recorded = doubles["reporter"].recorded[0]
        assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        assert recorded["score"] is None

    async def test_staleness_告警进入报告(self) -> None:
        pipeline, doubles = _pipeline([])
        await pipeline.finalize_dimension_report(
            _state(
                **{
                    KEY_VALIDATION_CASE_IDS: ["c-1"],
                    KEY_SUITE_STALENESS_WARNING: "用例集绑定的是旧版本",
                }
            )
        )
        assert "用例集绑定的是旧版本" in doubles["reporter"].recorded[0]["findings"]


# --------------------------------------------------------------------------- #
# 7. 图结构与端到端
# --------------------------------------------------------------------------- #


class SubgraphStructureTests:
    def test_验证集判定不回优化闭环(self) -> None:
        """架构约束落在图结构上，而不只是运行时校验。"""
        graph = build_trigger_accuracy_subgraph().compile().get_graph()
        edges = {(e.source, e.target) for e in graph.edges}

        assert (NODE_NAMES["judge_validation_cases"], NODE_NAMES["optimizer_loop"]) not in edges
        assert (NODE_NAMES["optimizer_loop"], NODE_NAMES["execute_validation_cases"]) in edges
        assert (NODE_NAMES["judge_train_cases"], NODE_NAMES["optimizer_loop"]) in edges

    def test_挂起点清单供主图汇总(self) -> None:
        assert INTERRUPT_BEFORE_NODES == [NODE_NAMES["optimizer_loop"]]

    async def test_端到端_训练集失败走完优化闭环再测验证集(self) -> None:
        cases = [
            _case("c-train-bad"),
            _case("c-val-1", split=DatasetSplit.VALIDATION),
            _case("c-val-2", category=TestCaseCategory.NEGATIVE, split=DatasetSplit.VALIDATION),
        ]
        loop = FakeOptimizationLoop(patch=_patch())
        pipeline, doubles = _pipeline(
            cases,
            loaded_by_case={
                "c-train-bad": [False, False, False],  # 训练集失败 -> 进闭环
                "c-val-1": [True, True, True],  # 正向验证用例通过
                "c-val-2": [False, False, False],  # 基线上不误触发；补丁放宽后会误触发
            },
            loop=loop,
        )
        graph = build_trigger_accuracy_subgraph(pipeline.deps).compile()

        final = await graph.ainvoke(_state())

        assert final[KEY_TRAIN_FAILED_CASE_IDS] == ["c-train-bad"]
        assert final[KEY_APPLIED_PATCH_ID] == "p-1"
        # 验证集用的是**打过补丁**的版本：补丁把 description 写宽了，训练集恢复通过，
        # 但反向验证用例开始误触发——这正是验证集存在的意义（抓过拟合），且它不会
        # 反过来触发另一轮优化。
        assert final[KEY_VALIDATION_FAILED_CASE_IDS] == ["c-val-2"]
        recorded = doubles["reporter"].recorded[-1]
        assert recorded["status"] is JudgeVerdictStatus.FAIL
        assert recorded["score"] == 0.5

    async def test_端到端_训练集全过直接进验证集(self) -> None:
        cases = [_case("c-train"), _case("c-val", split=DatasetSplit.VALIDATION)]
        loop = FakeOptimizationLoop(patch=_patch())
        pipeline, doubles = _pipeline(cases, loop=loop)
        graph = build_trigger_accuracy_subgraph(pipeline.deps).compile()

        final = await graph.ainvoke(_state())

        assert loop.ctx is None  # 优化闭环根本没被触碰
        assert final[KEY_VALIDATION_FAILED_CASE_IDS] == []
        assert doubles["reporter"].recorded[-1]["status"] is JudgeVerdictStatus.PASS
        # 两个执行节点各产出 3 条 trace，reducer 追加后不应出现重复。
        assert len(final["executed_trace_ids"]) == len(set(final["executed_trace_ids"])) == 6
