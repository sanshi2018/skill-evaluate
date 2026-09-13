"""docs/dev/19：模块九——跨模型泛化与代理绑架防范机制。

覆盖：AI 话术词典（扫描、代码保护、确定性消融、新增命中判定）、对照实验的证据口径
（失败态 Trace 不是证据、NEGATIVE 用例的预期方向）、三条量化规则、验证集确定性抽样、
`llama_control` 后端（轮询/超时/失败/未配置/HTTP 契约）、六个节点与报告口径、子图的并行
扇出与汇合、共识门控与模型怪癖剥离拦截器、`linguistic_smell` 模板的可选词典变量。
全部用替身注入，不碰数据库、不发真实请求、不起沙箱。
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from skill_evaluate.agents.analyzer.ablation_lexicon import (
    LexiconKind,
    ablate,
    ablate_skill_text,
    ablation_seed,
    format_hits_for_review,
    introduced_hits,
    scan_lexicon,
)
from skill_evaluate.agents.judge.golden_injector import golden_subject_id
from skill_evaluate.agents.judge.service import JudgeAgent
from skill_evaluate.agents.mini.templates.registry import get_template
from skill_evaluate.agents.optimizer import (
    AGENT_OVERFITTING_MARKER,
    LoopResult,
    is_agent_overfitting,
    is_model_quirk_rejection,
    with_consensus_gate,
    with_quirk_stripping_gate,
)
from skill_evaluate.config import CrossModelSettings
from skill_evaluate.errors import ConfigurationError, ExecutorBackendError, PipelineSuspended
from skill_evaluate.executors.base import ExecutionRequest, ExecutorBackend
from skill_evaluate.executors.comparison import (
    is_conclusive_trace,
    run_arm,
    summarize_arm,
    trigger_matches_expectation,
)
from skill_evaluate.executors.hermes_backend import (
    ACTION_TYPE_SANDBOX_TIMEOUT,
    build_failure_trace,
)
from skill_evaluate.executors.llama_backend import (
    HttpLlamaControlClient,
    LlamaControlBackend,
    LlamaRunPayload,
    LlamaTaskHandle,
    LlamaTaskStatus,
    UnconfiguredLlamaControlClient,
)
from skill_evaluate.executors.registry import get_backend, list_registered_backends
from skill_evaluate.nodes.cross_model import (
    BLOCKING,
    DIMENSION,
    NODE_NAMES,
    PROBE_NODES,
    RULE_ABLATION_ROBUSTNESS,
    RULE_HETERO_CONSISTENCY,
    CrossModelDeps,
    CrossModelPipeline,
    build_cross_model_subgraph,
    sample_validation_cases,
)
from skill_evaluate.nodes.cross_model.nodes import LinguisticOutcome, ProbeOutcome
from skill_evaluate.nodes.cross_model.state import (
    KEY_ABLATION_OUTCOME,
    KEY_HETERO_OUTCOME,
    KEY_LINGUISTIC_OUTCOME,
    KEY_PERTURBATION_OUTCOME,
    KEY_SAMPLE_CASE_IDS,
    KEY_SAMPLE_NOTE,
)
from skill_evaluate.state.enums import (
    Criticality,
    DatasetSplit,
    ExecutorBackendType,
    JudgeVerdictStatus,
    TestCaseCategory,
)
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion
from skill_evaluate.state.trace import (
    RUN_INDEX_XMODEL_ABLATION_ABLATED,
    RUN_INDEX_XMODEL_GATE_BASELINE,
    RUN_INDEX_XMODEL_GATE_CANDIDATE,
    RUN_INDEX_XMODEL_PERTURB_BASELINE,
    RUN_INDEX_XMODEL_PERTURB_VARIANT,
    RUN_INDEX_XMODEL_PRIMARY,
    RUN_INDEX_XMODEL_SECONDARY,
    ExecutionTrace,
    TimingCostMetrics,
)

SKILL_ID = "csv-cleaner"
RUN_ID = "run-1"
VERSION_REF = "v1"

SPELL_BODY = (
    "# CSV Cleaner\n\n"
    "你是一位世界级的数据清洗专家。请认真仔细地思考，务必先备份原文件！！\n"
    "ALWAYS check the header row.\n"
    "```bash\n"
    "echo 'think step by step' # 代码里的咒语不算\n"
    "```\n"
)
PLAIN_BODY = "# CSV Cleaner\n\n删除前先确认目标路径存在，否则停止并报告路径。\n"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def _skill(
    body: str = PLAIN_BODY, *, version_ref: str = VERSION_REF, description: str = "清洗 CSV"
) -> SkillDefinition:
    return SkillDefinition(
        skill_id=SKILL_ID,
        version_ref=version_ref,
        root_path=".",
        description=description,
        body_markdown=body,
        line_count=len(body.splitlines()),
        token_count=40,
    )


def _case(
    case_id: str,
    *,
    category: TestCaseCategory = TestCaseCategory.POSITIVE,
    split: DatasetSplit = DatasetSplit.VALIDATION,
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
            total_tokens=1, prompt_tokens=1, completion_tokens=0, duration_ms=1
        ),
        final_response="done",
        started_at=now,
        finished_at=now,
    )


def _verdict(
    status: JudgeVerdictStatus, *, subject_id: str = "xmodel_linguistic:csv-cleaner"
) -> JudgeVerdict:
    return JudgeVerdict(
        verdict_id=f"v-{status.value}",
        subject_id=subject_id,
        status=status,
        reasoning="原文片段：「务必先备份」" + "补" * 300,
        temperature=0.1,
        model="anthropic/claude-haiku-4.5",
        created_at=datetime.now(UTC),
    )


class ScriptedBackend(ExecutorBackend):
    """按回调决定每次执行是否加载 SKILL.md（返回 None 表示这次沙箱故障）。"""

    backend_type = ExecutorBackendType.PLUGGABLE

    def __init__(self, decide: Any = None, *, healthy: bool = True) -> None:
        self.decide = decide or (lambda request: True)
        self.healthy = healthy
        self.requests: list[ExecutionRequest] = []
        self.in_flight = 0
        self.peak = 0

    async def execute(self, request: ExecutionRequest) -> ExecutionTrace:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            self.requests.append(request)
            await asyncio.sleep(0)
            loaded = self.decide(request)
            if loaded is None:
                return build_failure_trace(
                    case_id=request.case.case_id,
                    run_index=request.run_index,
                    reason="boom",
                    timed_out=True,
                )
            return _trace(request.case.case_id, request.run_index, loaded=loaded)
        finally:
            self.in_flight -= 1

    async def health_check(self) -> bool:
        return self.healthy


class FakeSkillRepo:
    def __init__(self, skill: SkillDefinition) -> None:
        self.skill = skill

    async def get(self, skill_id: str, version_ref: str) -> SkillDefinition | None:
        return self.skill


class FakeCaseRepo:
    def __init__(self, cases: list[TestCase]) -> None:
        self.cases = cases
        self.category_queries: list[str] = []

    async def list_by_ids(self, case_ids: list[str]) -> list[TestCase]:
        return [c for c in reversed(self.cases) if c.case_id in case_ids]

    async def list_by_categories(
        self, suite_version_id: str, categories: list[TestCaseCategory]
    ) -> list[TestCase]:
        self.category_queries.append(suite_version_id)
        return [c for c in reversed(self.cases) if c.category in categories]


class FakeSuiteRepo:
    def __init__(self, suite_version_id: str | None) -> None:
        self.suite_version_id = suite_version_id

    async def get_active_version(
        self, skill_id: str, skill_version_ref: str | None = None
    ) -> TestSuiteVersion | None:
        if self.suite_version_id is None:
            return None
        return TestSuiteVersion(
            suite_version_id=self.suite_version_id,
            skill_id=skill_id,
            skill_version_ref=VERSION_REF,
            generation_mode="reuse",
            case_ids=[],
            created_at=datetime.now(UTC),
        )


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


class FakeJudge:
    """量化判定走真实 `JudgeAgent`（纯算术），裁量判定回放给定结果并记录调用。"""

    def __init__(self, judgmental: JudgeVerdict | ConsensusResult | None = None) -> None:
        self._real = JudgeAgent()
        self.judgmental = judgmental or _verdict(JudgeVerdictStatus.PASS)
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
        return self.judgmental


def _deps(
    *,
    skill: SkillDefinition | None = None,
    cases: list[TestCase] | None = None,
    primary: ScriptedBackend | None = None,
    secondary: ScriptedBackend | None = None,
    judge: FakeJudge | None = None,
    reporter: FakeReporter | None = None,
    settings: CrossModelSettings | None = None,
    suite_version_id: str | None = "suite-active",
    trace_repo: FakeTraceRepo | None = None,
    judge_repo: FakeJudgeRepo | None = None,
    max_concurrent: int | None = 10,
) -> CrossModelDeps:
    return CrossModelDeps(
        primary_backend=primary or ScriptedBackend(),
        secondary_backend=secondary or ScriptedBackend(),
        judge_agent=judge or FakeJudge(),  # type: ignore[arg-type]  测试替身
        report_generator=reporter or FakeReporter(),  # type: ignore[arg-type]
        skill_repository=FakeSkillRepo(skill or _skill()),  # type: ignore[arg-type]
        test_case_repository=FakeCaseRepo(cases or []),  # type: ignore[arg-type]
        test_suite_repository=FakeSuiteRepo(suite_version_id),  # type: ignore[arg-type]
        trace_repository=trace_repo or FakeTraceRepo(),  # type: ignore[arg-type]
        judge_repository=judge_repo or FakeJudgeRepo(),  # type: ignore[arg-type]
        cross_model_settings=settings or CrossModelSettings(),
        max_concurrent_sandboxes=max_concurrent,
    )


def _state(**extra: Any) -> Any:
    return {"run_id": RUN_ID, "skill_id": SKILL_ID, "skill_version_ref": VERSION_REF, **extra}


# --------------------------------------------------------------------------- #
# 1. AI 话术词典
# --------------------------------------------------------------------------- #


class LexiconTests:
    def test_scans_incantations_outside_code_blocks(self) -> None:
        hits = scan_lexicon(SPELL_BODY)
        kinds = {hit.kind for hit in hits}
        assert {
            LexiconKind.PERSONA_FLATTERY,
            LexiconKind.INCANTATION,
            LexiconKind.EMOTIONAL_PRESSURE,
            LexiconKind.PUNCTUATION_EMPHASIS,
            LexiconKind.CAPS_EMPHASIS,
        } <= kinds
        # 代码块里的 "think step by step" 是 Skill 的真实内容，不算咒语。
        assert all("step" not in hit.text for hit in hits)

    def test_overlapping_matches_keep_the_longest(self) -> None:
        hits = scan_lexicon("请认真仔细地思考一下再回答")
        assert [hit.text for hit in hits] == ["请认真仔细地思考一下"]

    def test_plain_but_firm_constraints_are_not_incantations(self) -> None:
        # "必须/否则停止"是讲道理的强硬约束，把它们删掉会改写 Skill 的领域步骤。
        assert scan_lexicon("删除前必须先确认目标路径存在，否则停止。") == []

    def test_inline_code_is_protected(self) -> None:
        assert scan_lexicon("在提示里写 `务必` 作为反面示例") == []

    def test_ablation_is_deterministic_and_downgrades_caps_instead_of_removing(self) -> None:
        seed = ablation_seed(SKILL_ID)
        first = ablate(SPELL_BODY, seed, drop_probability=1.0)
        second = ablate(SPELL_BODY, seed, drop_probability=1.0)
        assert first.ablated == second.ablated
        assert "世界级" not in first.ablated and "务必" not in first.ablated
        # NEVER/ALWAYS 本身是信息：降级为小写而不是删掉，否则禁令语义会反转。
        assert "always check the header row" in first.ablated
        assert "echo 'think step by step'" in first.ablated  # 代码原样保留
        assert ablate_skill_text(SPELL_BODY, seed, drop_probability=1.0) == first.ablated

    def test_ablation_with_zero_probability_changes_nothing(self) -> None:
        result = ablate(SPELL_BODY, "seed", drop_probability=0.0)
        assert not result.changed and result.hits and result.dropped == []

    def test_partial_ablation_is_reproducible_across_calls(self) -> None:
        outputs = {
            ablate(SPELL_BODY, ablation_seed("x"), drop_probability=0.5).ablated for _ in range(5)
        }
        assert len(outputs) == 1

    def test_seed_is_stable_string_not_process_hash(self) -> None:
        assert ablation_seed(SKILL_ID) == "skill-evaluate:ablation:csv-cleaner"

    def test_introduced_hits_counts_occurrences(self) -> None:
        introduced = introduced_hits("务必先备份。", "务必先备份。务必再校验，一定要快")
        assert [hit.text for hit in introduced] == ["务必", "一定要"]

    def test_model_address_swallows_trailing_would(self) -> None:
        hits = scan_lexicon("Think step by step like a Hermes model would.")
        assert [h.text for h in hits][-1] == "like a Hermes model would"

    def test_review_rendering_caps_length(self) -> None:
        hits = scan_lexicon("务必。" * 40)
        rendered = format_hits_for_review(hits, limit=3)
        assert rendered.count("\n") == 3 and "另有 37 处" in rendered
        assert format_hits_for_review([]) == "（词典未命中任何条目）"


# --------------------------------------------------------------------------- #
# 2. 对照实验的证据口径 + 量化规则
# --------------------------------------------------------------------------- #


class EvidenceTests:
    def test_failure_traces_are_not_evidence(self) -> None:
        failure = build_failure_trace(case_id="c", run_index=0, reason="x", timed_out=True)
        assert failure.actions[-1].action_type == ACTION_TYPE_SANDBOX_TIMEOUT
        assert is_conclusive_trace(failure) is False
        assert is_conclusive_trace(_trace("c", 0, loaded=False)) is True

    def test_negative_cases_expect_no_loading(self) -> None:
        assert trigger_matches_expectation(_trace("c", 0, loaded=False), TestCaseCategory.NEGATIVE)
        assert not trigger_matches_expectation(
            _trace("c", 0, loaded=False), TestCaseCategory.POSITIVE
        )
        with pytest.raises(ValueError):
            trigger_matches_expectation(_trace("c", 0, loaded=True), TestCaseCategory.ADVERSARIAL)

    def test_summarize_arm_excludes_failure_traces(self) -> None:
        traces = [
            _trace("c", 0, loaded=False),
            build_failure_trace(case_id="c", run_index=1, reason="x"),
        ]
        evidence = summarize_arm(traces, TestCaseCategory.NEGATIVE)
        # 失败态 Trace 的 loaded=False 不能被算成"NEGATIVE 用例正确地没触发"。
        assert (evidence.expected_count, evidence.conclusive_count, evidence.total_count) == (
            1,
            1,
            2,
        )
        assert summarize_arm([traces[1]], TestCaseCategory.NEGATIVE).behaved_as_expected is None

    async def test_run_arm_uses_segment_overrides_and_shared_semaphore(self) -> None:
        backend = ScriptedBackend()
        cases = [_case(f"c{i}") for i in range(4)]
        traces = await run_arm(
            backend,
            run_id=RUN_ID,
            skill=_skill(),
            cases=cases,
            run_index_base=RUN_INDEX_XMODEL_PERTURB_VARIANT,
            runs=2,
            timeout_s=33,
            semaphore=asyncio.Semaphore(3),
            sampling_overrides={"temperature": 0.2},
        )
        assert set(traces) == {c.case_id for c in cases}
        assert {r.run_index for r in backend.requests} == {180, 181}
        assert all(
            r.sampling_overrides == {"temperature": 0.2} and r.wall_clock_timeout_s == 33
            for r in backend.requests
        )
        assert backend.peak <= 3

    def test_divergence_rule_semantics(self) -> None:
        judge = JudgeAgent()

        def status(ref: tuple[int, int], var: tuple[int, int]) -> JudgeVerdictStatus:
            inputs = {
                "reference_expected": ref[0],
                "reference_conclusive": ref[1],
                "variant_expected": var[0],
                "variant_conclusive": var[1],
            }
            return judge.quantitative_verdict("s", RULE_HETERO_CONSISTENCY, inputs).status

        assert status((1, 1), (0, 1)) is JudgeVerdictStatus.FAIL  # 主对备错：代理差异
        assert status((1, 1), (1, 1)) is JudgeVerdictStatus.PASS
        # 参照臂自己就不对：模块一的问题，不在本维度重复扣分。
        assert status((0, 1), (0, 1)) is JudgeVerdictStatus.PASS
        assert status((1, 1), (0, 0)) is JudgeVerdictStatus.FAIL  # 无证据不等于通过
        assert (
            judge.quantitative_verdict(
                "s",
                RULE_ABLATION_ROBUSTNESS,
                {
                    "reference_expected": 3,
                    "reference_conclusive": 3,
                    "variant_expected": 2,
                    "variant_conclusive": 3,
                },
            ).status
            is JudgeVerdictStatus.PASS
        )


# --------------------------------------------------------------------------- #
# 3. 抽样
# --------------------------------------------------------------------------- #


class SamplingTests:
    def _pool(self) -> list[TestCase]:
        cases = [_case(f"v{i:02d}") for i in range(10)]
        cases += [_case(f"n{i:02d}", category=TestCaseCategory.NEGATIVE) for i in range(5)]
        cases += [_case(f"t{i:02d}", split=DatasetSplit.TRAIN) for i in range(5)]
        cases += [_case("adv", category=TestCaseCategory.ADVERSARIAL)]
        return cases

    def test_samples_twenty_percent_of_validation_positive_and_negative(self) -> None:
        sampled = sample_validation_cases(self._pool(), skill_id=SKILL_ID, ratio=0.2)
        assert len(sampled) == 3  # round(15 * 0.2)
        assert all(c.split is DatasetSplit.VALIDATION for c in sampled)
        assert all(
            c.category in (TestCaseCategory.POSITIVE, TestCaseCategory.NEGATIVE) for c in sampled
        )

    def test_sampling_is_independent_of_repository_order(self) -> None:
        pool = self._pool()
        first = [c.case_id for c in sample_validation_cases(pool, skill_id=SKILL_ID, ratio=0.2)]
        again = [
            c.case_id
            for c in sample_validation_cases(list(reversed(pool)), skill_id=SKILL_ID, ratio=0.2)
        ]
        assert first == again

    def test_samples_at_least_one_and_none_from_empty_pool(self) -> None:
        assert len(sample_validation_cases([_case("only")], skill_id=SKILL_ID, ratio=0.2)) == 1
        assert (
            sample_validation_cases(
                [_case("t", split=DatasetSplit.TRAIN)], skill_id=SKILL_ID, ratio=0.2
            )
            == []
        )


# --------------------------------------------------------------------------- #
# 4. llama_control 后端
# --------------------------------------------------------------------------- #


def _payload(*, loaded: bool | None = True, tool_path: str | None = None) -> LlamaRunPayload:
    now = datetime.now(UTC)
    trajectory = []
    if tool_path:
        trajectory.append(
            {"tool_name": "read_file", "tool_input": {"path": tool_path}, "ts": now.isoformat()}
        )
    return LlamaRunPayload.model_validate(
        {
            "trajectory": trajectory,
            "final_message": "ok",
            "skill_md_loaded": loaded,
            "started_at": now.isoformat(),
            "finished_at": now.isoformat(),
        }
    )


class ScriptedLlamaClient:
    def __init__(self, statuses: list[LlamaTaskStatus], *, reachable: bool = True) -> None:
        self.statuses = statuses
        self.reachable = reachable
        self.submitted: list[dict[str, Any]] = []
        self.polls = 0

    async def submit_task(
        self, *, request: ExecutionRequest, callback_url: str | None, hook_secret: str | None
    ) -> LlamaTaskHandle:
        self.submitted.append({"request": request, "callback_url": callback_url})
        return LlamaTaskHandle(task_id="task-1")

    async def get_task(self, task_id: str) -> LlamaTaskStatus:
        status = self.statuses[min(self.polls, len(self.statuses) - 1)]
        self.polls += 1
        return status

    async def is_reachable(self) -> bool:
        return self.reachable


def _request(**extra: Any) -> ExecutionRequest:
    return ExecutionRequest(
        skill=_skill(), case=_case("c1"), run_index=RUN_INDEX_XMODEL_SECONDARY, **extra
    )


class LlamaBackendTests:
    def test_registered_under_llama_control(self) -> None:
        assert "llama_control" in list_registered_backends()
        assert isinstance(get_backend("llama_control"), LlamaControlBackend)

    async def test_poll_mode_maps_payload_with_the_same_mapper_as_hermes(self) -> None:
        client = ScriptedLlamaClient(
            [
                LlamaTaskStatus(status="running"),
                LlamaTaskStatus(
                    status="succeeded", result=_payload(loaded=None, tool_path="/skill/SKILL.md")
                ),
            ]
        )
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        backend = LlamaControlBackend(
            client, wait_mode="poll", poll_interval_s=2.0, sleep=fake_sleep
        )
        trace = await backend.execute(_request(sampling_overrides={"temperature": 0.2}))
        # 没有显式信号时走 read_file fallback（docs/dev/03 为第三方 Agent 预留的判定）。
        assert trace.loaded_skill_md is True
        assert trace.run_index == RUN_INDEX_XMODEL_SECONDARY
        assert sleeps == [2.0]
        assert client.submitted[0]["callback_url"] is None
        assert client.submitted[0]["request"].sampling_overrides == {"temperature": 0.2}

    async def test_poll_timeout_returns_sandbox_timeout_failure_trace(self) -> None:
        clock = {"now": 0.0}

        async def fake_sleep(seconds: float) -> None:
            clock["now"] += seconds

        backend = LlamaControlBackend(
            ScriptedLlamaClient([LlamaTaskStatus(status="running")]),
            wait_mode="poll",
            poll_interval_s=10.0,
            sleep=fake_sleep,
            clock=lambda: clock["now"],
        )
        trace = await backend.execute(_request(wall_clock_timeout_s=20))
        assert trace.loaded_skill_md is False
        assert trace.actions[-1].action_type == ACTION_TYPE_SANDBOX_TIMEOUT
        assert trace.final_response.startswith("[LlamaControlBackend fallback]")
        assert is_conclusive_trace(trace) is False

    async def test_runtime_reported_failure_becomes_failure_trace(self) -> None:
        backend = LlamaControlBackend(
            ScriptedLlamaClient([LlamaTaskStatus(status="failed", error="OOM")]), wait_mode="poll"
        )
        trace = await backend.execute(_request())
        assert "OOM" in trace.final_response and not is_conclusive_trace(trace)

    async def test_unconfigured_client_raises_instead_of_faking_success(self) -> None:
        backend = LlamaControlBackend(UnconfiguredLlamaControlClient(), wait_mode="poll")
        with pytest.raises(ExecutorBackendError):
            await backend.execute(_request())
        assert await backend.health_check() is False

    async def test_callback_mode_requires_run_id(self) -> None:
        backend = LlamaControlBackend(ScriptedLlamaClient([]), wait_mode="callback")
        with pytest.raises(ExecutorBackendError):
            await backend.execute(_request())

    def test_unknown_wait_mode_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            LlamaControlBackend(ScriptedLlamaClient([]), wait_mode="webhook")

    async def test_http_client_follows_the_rest_contract(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "POST" and request.url.path == "/v1/tasks":
                return httpx.Response(200, json={"task_id": "t-9"})
            if request.url.path == "/v1/tasks/t-9":
                return httpx.Response(
                    200,
                    json={
                        "status": "succeeded",
                        "result": json.loads(_payload().model_dump_json()),
                    },
                )
            if request.url.path == "/healthz":
                return httpx.Response(200)
            return httpx.Response(503, text="down")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = HttpLlamaControlClient(
                "http://llama.local/", api_key="k", model="m", http_client=http
            )
            handle = await client.submit_task(
                request=_request(sampling_overrides={"top_p": 0.9}),
                callback_url=None,
                hook_secret=None,
            )
            status = await client.get_task(handle.task_id)
            assert await client.is_reachable() is True
            with pytest.raises(ExecutorBackendError):
                await client.get_task("missing")

        body = json.loads(seen[0].content)
        assert body["sampling_overrides"] == {"top_p": 0.9} and body["model"] == "m"
        assert body["skill"]["body_markdown"] == PLAIN_BODY
        assert seen[0].headers["Authorization"] == "Bearer k"
        assert status.status == "succeeded" and status.result is not None


# --------------------------------------------------------------------------- #
# 5. 节点
# --------------------------------------------------------------------------- #


class PrepareSampleTests:
    async def test_uses_active_suite_from_state(self) -> None:
        repo_cases = [_case(f"v{i}") for i in range(10)]
        deps = _deps(cases=repo_cases)
        update = await CrossModelPipeline(deps).prepare_cross_model_sample(
            _state(active_suite_version_id="suite-state")
        )
        assert deps.test_case_repository.category_queries == ["suite-state"]  # type: ignore[attr-defined]
        assert len(update[KEY_SAMPLE_CASE_IDS]) == 2 and update[KEY_SAMPLE_NOTE] is None

    async def test_falls_back_to_active_version_lookup(self) -> None:
        deps = _deps(cases=[_case("v1")], suite_version_id="suite-db")
        update = await CrossModelPipeline(deps).prepare_cross_model_sample(_state())
        assert deps.test_case_repository.category_queries == ["suite-db"]  # type: ignore[attr-defined]
        assert update[KEY_SAMPLE_CASE_IDS] == ["v1"]

    async def test_no_suite_is_reported_not_generated(self) -> None:
        update = await CrossModelPipeline(_deps(suite_version_id=None)).prepare_cross_model_sample(
            _state()
        )
        assert update[KEY_SAMPLE_CASE_IDS] == [] and "active 用例集" in str(update[KEY_SAMPLE_NOTE])


class HeteroMatrixTests:
    async def test_flags_cases_where_only_primary_behaves(self) -> None:
        cases = [_case("pos"), _case("neg", category=TestCaseCategory.NEGATIVE)]
        primary = ScriptedBackend(lambda r: r.case.category is TestCaseCategory.POSITIVE)
        # 备用代理：正向用例没触发（差异），反向用例也没触发（这是对的，不是差异）。
        secondary = ScriptedBackend(lambda r: False)
        judge_repo, trace_repo = FakeJudgeRepo(), FakeTraceRepo()
        deps = _deps(
            cases=cases,
            primary=primary,
            secondary=secondary,
            judge_repo=judge_repo,
            trace_repo=trace_repo,
        )
        update = await CrossModelPipeline(deps).heterogeneous_execution_matrix(
            _state(**{KEY_SAMPLE_CASE_IDS: ["pos", "neg"]})
        )

        outcome = ProbeOutcome.model_validate(update[KEY_HETERO_OUTCOME])
        assert outcome.status == "completed"
        assert outcome.diverged_case_ids == ["pos"]
        assert outcome.compared_case_ids == ["pos", "neg"]
        assert "[代理差异] pos" in outcome.findings[0]
        assert [v.subject_id for v in judge_repo.saved] == ["xmodel_hetero:pos"]  # 只归档 FAIL
        assert {r.run_index for r in primary.requests} == {RUN_INDEX_XMODEL_PRIMARY}
        assert {r.run_index for r in secondary.requests} == {RUN_INDEX_XMODEL_SECONDARY}
        assert len(update["executed_trace_ids"]) == 4 and len(trace_repo.saved) == 4  # type: ignore[arg-type]
        assert len(update["judge_verdict_ids"]) == 2  # type: ignore[arg-type]

    async def test_unhealthy_secondary_skips_without_executing(self) -> None:
        primary, secondary = ScriptedBackend(), ScriptedBackend(healthy=False)
        deps = _deps(cases=[_case("pos")], primary=primary, secondary=secondary)
        update = await CrossModelPipeline(deps).heterogeneous_execution_matrix(
            _state(**{KEY_SAMPLE_CASE_IDS: ["pos"]})
        )
        outcome = ProbeOutcome.model_validate(update[KEY_HETERO_OUTCOME])
        assert outcome.status == "skipped" and "不可用" in str(outcome.note)
        assert primary.requests == [] and secondary.requests == []

    async def test_failure_traces_are_inconclusive_not_divergent(self) -> None:
        deps = _deps(cases=[_case("pos")], secondary=ScriptedBackend(lambda r: None))
        update = await CrossModelPipeline(deps).heterogeneous_execution_matrix(
            _state(**{KEY_SAMPLE_CASE_IDS: ["pos"]})
        )
        outcome = ProbeOutcome.model_validate(update[KEY_HETERO_OUTCOME])
        assert outcome.inconclusive_case_ids == ["pos"] and outcome.diverged_case_ids == []

    async def test_empty_sample_skips(self) -> None:
        update = await CrossModelPipeline(_deps()).heterogeneous_execution_matrix(
            _state(**{KEY_SAMPLE_CASE_IDS: [], KEY_SAMPLE_NOTE: "验证集为空"})
        )
        outcome = ProbeOutcome.model_validate(update[KEY_HETERO_OUTCOME])
        assert outcome.status == "skipped" and "验证集为空" in str(outcome.note)


class PerturbationTests:
    async def test_passes_two_override_sets_on_primary(self) -> None:
        primary = ScriptedBackend(lambda r: (r.sampling_overrides or {}).get("temperature") == 0.0)
        secondary = ScriptedBackend()
        deps = _deps(cases=[_case("pos")], primary=primary, secondary=secondary)
        update = await CrossModelPipeline(deps).parameter_perturbation_robustness_probe(
            _state(**{KEY_SAMPLE_CASE_IDS: ["pos"]})
        )
        outcome = ProbeOutcome.model_validate(update[KEY_PERTURBATION_OUTCOME])
        assert outcome.diverged_case_ids == ["pos"]
        overrides = {r.run_index: r.sampling_overrides for r in primary.requests}
        assert overrides == {
            RUN_INDEX_XMODEL_PERTURB_BASELINE: {"temperature": 0.0},
            RUN_INDEX_XMODEL_PERTURB_VARIANT: {"temperature": 0.2, "top_p": 0.9},
        }
        assert secondary.requests == []
        assert "仅在执行模型支持采样参数时" in outcome.findings[0]


class AblationTests:
    async def test_nothing_to_strip_is_not_applicable_and_runs_no_sandbox(self) -> None:
        primary = ScriptedBackend()
        deps = _deps(skill=_skill(PLAIN_BODY), cases=[_case("pos")], primary=primary)
        update = await CrossModelPipeline(deps).stochastic_ablation_testing(
            _state(**{KEY_SAMPLE_CASE_IDS: ["pos"]})
        )
        outcome = ProbeOutcome.model_validate(update[KEY_ABLATION_OUTCOME])
        assert outcome.status == "not_applicable" and primary.requests == []

    async def test_detects_incantation_dependency(self) -> None:
        primary = ScriptedBackend(lambda r: "世界级" in r.skill.body_markdown)
        settings = CrossModelSettings(ablation_drop_probability=1.0)
        deps = _deps(
            skill=_skill(SPELL_BODY), cases=[_case("pos")], primary=primary, settings=settings
        )
        update = await CrossModelPipeline(deps).stochastic_ablation_testing(
            _state(**{KEY_SAMPLE_CASE_IDS: ["pos"]})
        )
        outcome = ProbeOutcome.model_validate(update[KEY_ABLATION_OUTCOME])
        assert outcome.diverged_case_ids == ["pos"]
        assert "[咒语依赖] pos" in outcome.findings[1] and "随机消融" in outcome.findings[0]
        ablated = [r for r in primary.requests if r.run_index == RUN_INDEX_XMODEL_ABLATION_ABLATED]
        assert ablated[0].skill.version_ref == "v1+ablation"


class LinguisticSmellTests:
    async def test_passes_lexicon_hits_to_the_template_via_judge(self) -> None:
        judge = FakeJudge(_verdict(JudgeVerdictStatus.FAIL))
        deps = _deps(skill=_skill(SPELL_BODY), judge=judge)
        update = await CrossModelPipeline(deps).linguistic_smell_check(_state())
        call = judge.calls[0]
        assert call["template_key"] == "linguistic_smell"
        assert call["criticality"] is Criticality.ROUTINE
        assert "务必" in call["content"]["lexicon_hits"]
        outcome = LinguisticOutcome.model_validate(update[KEY_LINGUISTIC_OUTCOME])
        assert outcome.status is JudgeVerdictStatus.FAIL and outcome.caps_emphasis_count == 1
        assert update["judge_verdict_ids"] == ["v-fail"]

    async def test_golden_injection_is_skipped(self) -> None:
        judge = FakeJudge(_verdict(JudgeVerdictStatus.FAIL, subject_id=golden_subject_id("g1")))
        update = await CrossModelPipeline(_deps(judge=judge)).linguistic_smell_check(_state())
        outcome = LinguisticOutcome.model_validate(update[KEY_LINGUISTIC_OUTCOME])
        assert outcome.skipped_reason and update["judge_verdict_ids"] == []

    async def test_no_consensus_suspends(self) -> None:
        judge = FakeJudge(
            ConsensusResult(
                subject_id="xmodel_linguistic:csv-cleaner",
                consensus_reached=False,
                final_status=JudgeVerdictStatus.NEEDS_HUMAN_REVIEW,
            )
        )
        with pytest.raises(PipelineSuspended):
            await CrossModelPipeline(_deps(judge=judge)).linguistic_smell_check(_state())

    def test_template_renders_with_and_without_lexicon_hits(self) -> None:
        template = get_template("linguistic_smell")
        assert "词典初筛结果" in template.render(
            {"skill_md": "x", "lexicon_hits": "- 第 1 行 '务必'"}
        )
        # 黄金基准用例只带 skill_md：可选变量缺席不能让渲染失败。
        assert "词典初筛结果" not in template.render({"skill_md": "x"})


class FinalizeTests:
    def _completed(self, **extra: Any) -> dict[str, object]:
        return ProbeOutcome(
            probe="p", status="completed", compared_case_ids=["a"], **extra
        ).model_dump()

    def _state(self, **overrides: Any) -> Any:
        base = {
            KEY_SAMPLE_CASE_IDS: ["a"],
            KEY_SAMPLE_NOTE: None,
            KEY_HETERO_OUTCOME: self._completed(),
            KEY_PERTURBATION_OUTCOME: self._completed(),
            KEY_ABLATION_OUTCOME: ProbeOutcome(
                probe="p", status="not_applicable", note="无咒语"
            ).model_dump(),
            KEY_LINGUISTIC_OUTCOME: LinguisticOutcome(
                verdict_id="v", status=JudgeVerdictStatus.PASS
            ).model_dump(),
        }
        base.update(overrides)
        return _state(**base)

    async def _finalize(self, state: Any) -> dict[str, Any]:
        reporter = FakeReporter()
        await CrossModelPipeline(_deps(reporter=reporter)).finalize_dimension_report(state)
        return reporter.recorded[0]

    async def test_all_clean_is_pass_and_never_blocking(self) -> None:
        recorded = await self._finalize(self._state())
        assert recorded["status"] is JudgeVerdictStatus.PASS
        assert (
            recorded["dimension"] == DIMENSION
            and recorded["blocking"] is False
            and BLOCKING is False
        )
        assert recorded["score"] is None

    async def test_divergence_is_fail_but_not_blocking(self) -> None:
        state = self._state(
            **{
                KEY_HETERO_OUTCOME: self._completed(
                    diverged_case_ids=["a"], findings=["[代理差异] a"]
                )
            }
        )
        recorded = await self._finalize(state)
        assert recorded["status"] is JudgeVerdictStatus.FAIL and recorded["blocking"] is False
        assert "[代理差异] a" in recorded["findings"]

    async def test_skipped_probe_or_missing_keys_need_human_review(self) -> None:
        skipped = ProbeOutcome(probe="p", status="skipped", note="备用代理不可用").model_dump()
        assert (await self._finalize(self._state(**{KEY_HETERO_OUTCOME: skipped})))[
            "status"
        ] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        missing = self._state()
        del missing[KEY_PERTURBATION_OUTCOME]
        recorded = await self._finalize(missing)
        assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        assert any("状态 schema" in f for f in recorded["findings"])

    async def test_inconclusive_cases_need_human_review(self) -> None:
        state = self._state(
            **{KEY_PERTURBATION_OUTCOME: self._completed(inconclusive_case_ids=["a"])}
        )
        assert (await self._finalize(state))["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW

    async def test_linguistic_fail_and_caps_warning(self) -> None:
        state = self._state(
            **{
                KEY_LINGUISTIC_OUTCOME: LinguisticOutcome(
                    status=JudgeVerdictStatus.FAIL,
                    reasoning_excerpt="满篇务必",
                    caps_emphasis_count=4,
                ).model_dump()
            }
        )
        recorded = await self._finalize(state)
        assert recorded["status"] is JudgeVerdictStatus.FAIL
        assert any("降级警告" in f for f in recorded["findings"])


class AssemblyTests:
    def test_graph_fans_out_three_probes_and_joins_before_linguistic_check(self) -> None:
        graph = build_cross_model_subgraph(_deps()).compile()
        edges = {(e.source, e.target) for e in graph.get_graph().edges}
        for probe in PROBE_NODES:
            assert (NODE_NAMES["prepare_cross_model_sample"], probe) in edges
            assert (probe, NODE_NAMES["linguistic_smell_check"]) in edges
        assert (
            NODE_NAMES["linguistic_smell_check"],
            NODE_NAMES["finalize_dimension_report"],
        ) in edges
        assert all(name.startswith("cross_model.") for name in NODE_NAMES.values())

    async def test_end_to_end_subgraph_run(self) -> None:
        cases = [_case(f"v{i}") for i in range(5)]
        reporter = FakeReporter()
        deps = _deps(
            skill=_skill(SPELL_BODY),
            cases=cases,
            reporter=reporter,
            secondary=ScriptedBackend(lambda r: False),
        )
        graph = build_cross_model_subgraph(deps).compile()
        final = await graph.ainvoke(_state(active_suite_version_id="s"))
        recorded = reporter.recorded[0]
        assert recorded["status"] is JudgeVerdictStatus.FAIL  # 备用代理全不触发 → 代理差异
        assert any("[代理差异]" in f for f in recorded["findings"])
        # 三条支路并行写 add-reducer 字段：id 不丢、不重复。
        assert len(final["executed_trace_ids"]) == len(set(final["executed_trace_ids"]))

    def test_same_primary_and_secondary_backend_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SKILLEVAL_EXECUTOR_BACKEND", "llama_control")
        from skill_evaluate.config import get_settings

        get_settings.cache_clear()
        try:
            with pytest.raises(ConfigurationError):
                CrossModelDeps(cross_model_settings=CrossModelSettings()).assert_heterogeneous()
        finally:
            get_settings.cache_clear()

    def test_unregistered_secondary_is_rejected(self) -> None:
        deps = CrossModelDeps(cross_model_settings=CrossModelSettings(secondary_backend="nope"))
        with pytest.raises(ConfigurationError):
            deps.assert_heterogeneous()


# --------------------------------------------------------------------------- #
# 6. 共识门控 + 模型怪癖剥离
# --------------------------------------------------------------------------- #


def _train_cases(n: int = 4) -> list[TestCase]:
    return [_case(f"tr{i}", split=DatasetSplit.TRAIN) for i in range(n)]


async def _passing_base(skill: SkillDefinition) -> LoopResult:
    return LoopResult(passed=True, detail="训练集 4/4")


class ConsensusGateTests:
    async def test_base_failure_short_circuits_without_secondary_runs(self) -> None:
        secondary = ScriptedBackend()

        async def failing(skill: SkillDefinition) -> LoopResult:
            return LoopResult(passed=False, detail="仍失败")

        gated = with_consensus_gate(
            failing, _train_cases(), run_id=RUN_ID, secondary_backend=secondary, judge=FakeJudge()
        )
        result = await gated(_skill(version_ref="v1+patch:p1"))
        assert result.detail == "仍失败" and secondary.requests == []

    def test_rejects_validation_cases(self) -> None:
        with pytest.raises(ValueError, match="非训练集"):
            with_consensus_gate(
                _passing_base, [_case("v")], run_id=RUN_ID, secondary_backend=ScriptedBackend()
            )

    async def test_relative_regression_beyond_tolerance_is_agent_overfitting(self) -> None:
        # 基线：备用代理 4/4；补丁后：只剩 2/4 → 降幅 50% > 5%。
        secondary = ScriptedBackend(
            lambda r: r.skill.version_ref == VERSION_REF or r.case.case_id in {"tr0", "tr1"}
        )
        gated = with_consensus_gate(
            _passing_base,
            _train_cases(),
            run_id=RUN_ID,
            baseline_skill=_skill(),
            secondary_backend=secondary,
            judge=FakeJudge(),
        )
        result = await gated(_skill(version_ref="v1+patch:p1"))
        assert result.passed is False and is_agent_overfitting(result)
        assert (
            result.detail.startswith(AGENT_OVERFITTING_MARKER) and "补丁前基线 4/4" in result.detail
        )
        assert {r.run_index for r in secondary.requests} == {
            RUN_INDEX_XMODEL_GATE_BASELINE,
            RUN_INDEX_XMODEL_GATE_CANDIDATE,
        }

    async def test_relative_mode_tolerates_a_weak_baseline(self) -> None:
        # 原版在备用代理上就只有 2/4，补丁后仍是 2/4：没有退化，不该被打回。
        secondary = ScriptedBackend(lambda r: r.case.case_id in {"tr0", "tr1"})
        gated = with_consensus_gate(
            _passing_base,
            _train_cases(),
            run_id=RUN_ID,
            baseline_skill=_skill(),
            secondary_backend=secondary,
            judge=FakeJudge(),
        )
        first = await gated(_skill(version_ref="v1+patch:p1"))
        second = await gated(_skill(version_ref="v1+patch:p2"))
        assert first.passed and second.passed and "共识门控通过" in first.detail
        baseline_runs = [
            r for r in secondary.requests if r.run_index == RUN_INDEX_XMODEL_GATE_BASELINE
        ]
        assert len(baseline_runs) == 4  # 基线只测一次，两轮闭环共用

    async def test_absolute_mode_without_baseline(self) -> None:
        secondary = ScriptedBackend(lambda r: r.case.case_id != "tr0")  # 3/4 = 75% < 95%
        gated = with_consensus_gate(
            _passing_base,
            _train_cases(),
            run_id=RUN_ID,
            secondary_backend=secondary,
            judge=FakeJudge(),
        )
        assert is_agent_overfitting(await gated(_skill(version_ref="v1+patch:p1")))

    async def test_no_secondary_evidence_fails_closed(self) -> None:
        gated = with_consensus_gate(
            _passing_base,
            _train_cases(2),
            run_id=RUN_ID,
            secondary_backend=ScriptedBackend(lambda r: None),
            judge=FakeJudge(),
        )
        result = await gated(_skill(version_ref="v1+patch:p1"))
        assert result.passed is False and "无有效执行证据" in result.detail

    async def test_no_comparable_cases_passes_through_with_note(self) -> None:
        adversarial = [
            _case("adv", category=TestCaseCategory.ADVERSARIAL, split=DatasetSplit.TRAIN)
        ]
        gated = with_consensus_gate(
            _passing_base, adversarial, run_id=RUN_ID, secondary_backend=ScriptedBackend()
        )
        result = await gated(_skill())
        assert result.passed and "共识门控未生效" in result.detail


class QuirkStrippingGateTests:
    async def test_rejects_newly_introduced_incantations_before_running_regression(self) -> None:
        calls: list[str] = []

        async def base(skill: SkillDefinition) -> LoopResult:
            calls.append(skill.version_ref)
            return LoopResult(passed=True)

        gated = with_quirk_stripping_gate(base, baseline_skill=_skill(PLAIN_BODY))
        patched = _skill(
            PLAIN_BODY + "\nThink step-by-step like a Hermes model would.\n",
            version_ref="v1+patch:p",
        )
        result = await gated(patched)
        assert is_model_quirk_rejection(result) and calls == []

    async def test_existing_phrases_and_caps_emphasis_do_not_block(self) -> None:
        async def base(skill: SkillDefinition) -> LoopResult:
            return LoopResult(passed=True, detail="ok")

        gated = with_quirk_stripping_gate(base, baseline_skill=_skill(SPELL_BODY))
        # 原版里本来就有的"务必"不算补丁的锅；新加的 NEVER 只告警不打回。
        patched = _skill(
            SPELL_BODY + "\nNEVER pass user input to a shell.\n", version_ref="v1+patch:p"
        )
        result = await gated(patched)
        assert result.passed and result.detail == "ok"

    async def test_description_patches_are_checked_too(self) -> None:
        gated = with_quirk_stripping_gate(_passing_base, baseline_skill=_skill())
        patched = _skill(
            description="清洗 CSV。你是世界上最强的数据清洗专家", version_ref="v1+patch:p"
        )
        assert is_model_quirk_rejection(await gated(patched))
