"""docs/dev/08：Judge Agent 核心框架与裁判可信度机制。

覆盖：量化规则注册表、共识投票（含 `[step:N]` 一致性判定）、黄金基准盲测的
透明注入与记账、失误率冻结。全部用替身注入，不碰数据库、不发真实请求。
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from skill_evaluate.agents.judge import (
    JudgeAgent,
    JudgeHealthMonitor,
    build_replica_specs,
    evaluate_consensus,
    extract_cited_step_ids,
    get_rule,
    is_golden_subject,
    reasoning_points_to_same_trace_node,
    register_rule,
    temperature_bucket,
)
from skill_evaluate.agents.judge.consensus import STEP_CITATION_RULE
from skill_evaluate.agents.judge.rules import QUANTITATIVE_RULE_REGISTRY
from skill_evaluate.agents.judge.service import QUANTITATIVE_MODEL_PREFIX
from skill_evaluate.errors import JudgeFrozenError, JudgeRuleError
from skill_evaluate.state.enums import Criticality, JudgeVerdictStatus
from skill_evaluate.state.golden import GoldenCase
from skill_evaluate.state.judge import JudgeVerdict

SAMPLING_MODEL = "anthropic/claude-haiku-4.5"  # 仍接受 temperature
NO_SAMPLING_MODEL = "anthropic/claude-sonnet-5"  # 已移除采样参数


def _verdict(
    status: JudgeVerdictStatus, reasoning: str, subject_id: str = "case-1"
) -> JudgeVerdict:
    return JudgeVerdict(
        verdict_id=f"v-{reasoning[:8]}-{status.value}",
        subject_id=subject_id,
        status=status,
        reasoning=reasoning,
        temperature=0.1,
        model=SAMPLING_MODEL,
        created_at=datetime.now(UTC),
    )


class FakeReviewAgent:
    """替身评审副本：按预设脚本返回判定，并记录自己拿到了什么扰动配置。"""

    def __init__(self, spec: Any, script: list[tuple[JudgeVerdictStatus, str]]) -> None:
        self.spec = spec
        self._script = script
        self.requests: list[Any] = []

    async def review(self, request: Any) -> JudgeVerdict:
        self.requests.append(request)
        status, reasoning = self._script.pop(0)
        return _verdict(status, reasoning, subject_id=request.subject_id)


class FakeJudgeRepo:
    def __init__(self) -> None:
        self.verdicts: list[JudgeVerdict] = []
        self.consensus: list[Any] = []

    async def save_verdict(self, verdict: JudgeVerdict) -> None:
        self.verdicts.append(verdict)

    async def save_consensus(self, result: Any) -> None:
        self.consensus.append(result)


class FakeGoldenRepo:
    def __init__(self, cases: list[GoldenCase]) -> None:
        self._cases = cases
        self.queried_template_keys: list[str | None] = []

    async def list_active(self, template_key: str | None = None) -> list[GoldenCase]:
        self.queried_template_keys.append(template_key)
        return [c for c in self._cases if template_key is None or c.template_key == template_key]


class FakeMissRepo:
    def __init__(self, window: list[bool] | None = None) -> None:
        self.window = window or []
        self.recorded: list[Any] = []

    async def record(self, record: Any, *, temperature_bucket: str) -> None:
        self.recorded.append((record, temperature_bucket))

    async def recent_window(
        self, *, model: str, temperature_bucket: str, window_size: int
    ) -> list[bool]:
        return self.window[:window_size]


class FakeHealthRepo:
    def __init__(self, frozen: bool = False, reason: str | None = None) -> None:
        self.state: dict[str, Any] | None = (
            {"frozen": frozen, "reason": reason, "miss_rate": 1.0} if frozen else None
        )
        self.upserts: list[dict[str, Any]] = []

    async def get(self, *, model: str, temperature_bucket: str) -> dict[str, Any] | None:
        return self.state

    async def is_frozen(self, *, model: str, temperature_bucket: str) -> bool:
        return bool(self.state and self.state["frozen"])

    async def upsert(self, **kwargs: Any) -> None:
        self.upserts.append(kwargs)
        self.state = {
            "frozen": kwargs["frozen"],
            "reason": kwargs["reason"],
            "miss_rate": kwargs["miss_rate"],
        }


def _monitor(
    *, window: list[bool] | None = None, frozen: bool = False, threshold: float = 0.05
) -> tuple[JudgeHealthMonitor, FakeMissRepo, FakeHealthRepo]:
    miss_repo = FakeMissRepo(window)
    health_repo = FakeHealthRepo(frozen=frozen, reason="预置冻结")
    monitor = JudgeHealthMonitor(
        miss_repository=miss_repo,  # type: ignore[arg-type]
        health_repository=health_repo,  # type: ignore[arg-type]
        miss_rate_threshold=threshold,
        window_size=50,
    )
    return monitor, miss_repo, health_repo


def _judge(
    *,
    script: list[list[tuple[JudgeVerdictStatus, str]]],
    monitor: JudgeHealthMonitor | None = None,
    judge_repo: FakeJudgeRepo | None = None,
    golden_repo: FakeGoldenRepo | None = None,
    golden_rate: float = 0.0,
) -> tuple[JudgeAgent, FakeJudgeRepo, list[FakeReviewAgent]]:
    repo = judge_repo or FakeJudgeRepo()
    built: list[FakeReviewAgent] = []
    scripts = list(script)

    def factory(spec: Any) -> FakeReviewAgent:
        agent = FakeReviewAgent(spec, scripts.pop(0))
        built.append(agent)
        return agent

    judge = JudgeAgent(
        model=SAMPLING_MODEL,
        judge_repository=repo,  # type: ignore[arg-type]
        health_monitor=monitor or _monitor()[0],
        review_agent_factory=factory,
        golden_inject_rate=golden_rate,
        golden_repository=golden_repo,  # type: ignore[arg-type]
    )
    return judge, repo, built


# --------------------------------------------------------------------------- #
# 量化判定
# --------------------------------------------------------------------------- #


class QuantitativeRuleTests:
    def test_registry_starts_without_dimension_rules(self) -> None:
        # docs/dev/08 第 8 节：规则由 11/16~18 各自注册，框架层不预置——预置等于
        # 替还没写的文档决定了"多少算通过"。
        assert not [name for name in QUANTITATIVE_RULE_REGISTRY if name.startswith("trigger_")]

    def test_register_and_run_a_rule(self) -> None:
        @register_rule("test_trigger_rate_positive")
        def _rule(inputs: dict[str, Any]) -> JudgeVerdictStatus:
            rate = inputs["loaded_count"] / inputs["run_count"]
            return JudgeVerdictStatus.PASS if rate >= 0.5 else JudgeVerdictStatus.FAIL

        try:
            assert get_rule("test_trigger_rate_positive") is _rule
            judge, _, _ = _judge(script=[])
            verdict = judge.quantitative_verdict(
                "case-1", "test_trigger_rate_positive", {"loaded_count": 2, "run_count": 3}
            )
            assert verdict.status is JudgeVerdictStatus.PASS
            # 报告里要能一眼看出这条不是模型给的判定。
            assert verdict.model == f"{QUANTITATIVE_MODEL_PREFIX}test_trigger_rate_positive"
            assert verdict.temperature == 0.0
        finally:
            QUANTITATIVE_RULE_REGISTRY.pop("test_trigger_rate_positive", None)

    def test_duplicate_registration_is_rejected(self) -> None:
        @register_rule("test_dup_rule")
        def _rule(inputs: dict[str, Any]) -> JudgeVerdictStatus:
            return JudgeVerdictStatus.PASS

        try:
            with pytest.raises(JudgeRuleError, match="重复注册"):
                register_rule("test_dup_rule")(_rule)
        finally:
            QUANTITATIVE_RULE_REGISTRY.pop("test_dup_rule", None)

    def test_unknown_rule_is_not_silently_passed(self) -> None:
        # 找不到规则就默认放行，等于把一个评测维度悄悄关掉。
        with pytest.raises(JudgeRuleError, match="未注册"):
            get_rule("no_such_rule")


# --------------------------------------------------------------------------- #
# 共识投票
# --------------------------------------------------------------------------- #


class ConsensusTests:
    def test_step_markers_are_extracted(self) -> None:
        assert extract_cited_step_ids("依据 [step:3] 与 [step: 7]，判定失败") == {3, 7}
        assert extract_cited_step_ids("没有引用任何步骤") == set()

    def test_same_node_requires_intersecting_step_ids(self) -> None:
        intersecting = [
            _verdict(JudgeVerdictStatus.FAIL, "见 [step:3]"),
            _verdict(JudgeVerdictStatus.FAIL, "见 [step:3] 和 [step:5]"),
        ]
        disjoint = [
            _verdict(JudgeVerdictStatus.FAIL, "见 [step:3]"),
            _verdict(JudgeVerdictStatus.FAIL, "见 [step:9]"),
        ]
        assert reasoning_points_to_same_trace_node(intersecting)
        assert not reasoning_points_to_same_trace_node(disjoint)

    def test_static_review_without_any_step_citation_still_reaches_consensus(self) -> None:
        # 纯静态文本审查没有 Trace 可引；把"无法引用"判成"没有共识"会让模块二
        # 这类维度永远拿不到 CRITICAL 判定。
        verdicts = [
            _verdict(JudgeVerdictStatus.PASS, "原文写了……"),
            _verdict(JudgeVerdictStatus.PASS, "同上"),
        ]
        assert reasoning_points_to_same_trace_node(verdicts)

    def test_partial_citation_does_not_block_consensus(self) -> None:
        verdicts = [
            _verdict(JudgeVerdictStatus.FAIL, "见 [step:2]"),
            _verdict(JudgeVerdictStatus.FAIL, "同样问题，见 [step:2]"),
            _verdict(JudgeVerdictStatus.FAIL, "没引用具体步骤"),
        ]
        assert reasoning_points_to_same_trace_node(verdicts)

    def test_disagreement_yields_needs_human_review(self) -> None:
        result = evaluate_consensus(
            "case-1",
            [
                _verdict(JudgeVerdictStatus.FAIL, "见 [step:2]"),
                _verdict(JudgeVerdictStatus.PASS, "见 [step:2]"),
                _verdict(JudgeVerdictStatus.FAIL, "见 [step:2]"),
            ],
        )
        assert not result.consensus_reached
        assert result.final_status is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        assert result.dissenting_node and "结论不一致" in result.dissenting_node

    def test_same_status_but_different_nodes_is_not_consensus(self) -> None:
        result = evaluate_consensus(
            "case-1",
            [
                _verdict(JudgeVerdictStatus.FAIL, "见 [step:2]"),
                _verdict(JudgeVerdictStatus.FAIL, "见 [step:8]"),
            ],
        )
        assert not result.consensus_reached
        assert result.dissenting_node and "指向不同的 Trace 步骤" in result.dissenting_node

    def test_perspective_strategy_is_the_default_and_carries_step_rule(self) -> None:
        specs = build_replica_specs(
            strategy="perspective", base_temperature=0.1, temperatures=[], models=[]
        )
        assert len(specs) == 3
        assert len({spec.label for spec in specs}) == 3
        # 三副本同温度：本次扰动不来自温度，报告里也不该显得像做过温度扰动。
        assert {spec.temperature for spec in specs} == {0.1}
        assert all(STEP_CITATION_RULE in spec.system_suffix for spec in specs)

    def test_temperature_strategy_still_available_for_sampling_models(self) -> None:
        specs = build_replica_specs(
            strategy="temperature", base_temperature=0.1, temperatures=[0.1, 0.3, 0.5], models=[]
        )
        assert [spec.temperature for spec in specs] == [0.1, 0.3, 0.5]

    def test_model_strategy_without_models_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="CONSENSUS_MODELS"):
            build_replica_specs(strategy="model", base_temperature=0.1, temperatures=[], models=[])

    def test_unknown_strategy_is_not_silently_defaulted(self) -> None:
        with pytest.raises(ValueError, match="未知的 consensus_strategy"):
            build_replica_specs(
                strategy="coin_flip", base_temperature=0.1, temperatures=[], models=[]
            )


class JudgeAgentDispatchTests:
    async def test_routine_runs_one_replica(self) -> None:
        judge, repo, built = _judge(script=[[(JudgeVerdictStatus.PASS, "原文依据……")]])
        result = await judge.judgmental_verdict(
            "case-1", "omission_audit", {"skill_md": "# demo"}, Criticality.ROUTINE
        )
        assert isinstance(result, JudgeVerdict)
        assert len(built) == 1
        assert len(repo.verdicts) == 1
        assert repo.consensus == []

    async def test_critical_runs_three_replicas_and_persists_consensus(self) -> None:
        judge, repo, built = _judge(
            script=[
                [(JudgeVerdictStatus.FAIL, "见 [step:2]")],
                [(JudgeVerdictStatus.FAIL, "同样见 [step:2]")],
                [(JudgeVerdictStatus.FAIL, "还是 [step:2]")],
            ]
        )
        result = await judge.judgmental_verdict(
            "case-1", "omission_audit", {"skill_md": "# demo"}, Criticality.CRITICAL
        )
        assert len(built) == 3
        assert result.consensus_reached  # type: ignore[union-attr]
        assert result.final_status is JudgeVerdictStatus.FAIL  # type: ignore[union-attr]
        assert len(repo.verdicts) == 3  # 三份都入库，供人工回溯
        assert len(repo.consensus) == 1

    async def test_frozen_config_refuses_to_judge(self) -> None:
        monitor, _, _ = _monitor(frozen=True)
        judge, _, built = _judge(script=[[(JudgeVerdictStatus.PASS, "x")]], monitor=monitor)
        with pytest.raises(JudgeFrozenError):
            await judge.judgmental_verdict(
                "case-1", "omission_audit", {"skill_md": "# demo"}, Criticality.ROUTINE
            )
        assert built == []  # 冻结后一个请求都不该发出去


# --------------------------------------------------------------------------- #
# 黄金基准盲测
# --------------------------------------------------------------------------- #


class GoldenBlindTestTests:
    def _golden(self, status: JudgeVerdictStatus) -> GoldenCase:
        return GoldenCase(
            golden_id="g-1",
            template_key="omission_audit",
            content={"skill_md": "# 黄金用例"},
            human_labeled_status=status,
            human_labeled_reasoning="人类标定：这里确实有常识堆砌",
        )

    async def test_injection_replaces_request_and_marks_subject(self) -> None:
        monitor, miss_repo, _ = _monitor()
        golden_repo = FakeGoldenRepo([self._golden(JudgeVerdictStatus.FAIL)])
        judge, repo, built = _judge(
            script=[[(JudgeVerdictStatus.FAIL, "确有常识堆砌")]],
            monitor=monitor,
            golden_repo=golden_repo,
            golden_rate=1.0,
        )

        result = await judge.judgmental_verdict(
            "case-real", "omission_audit", {"skill_md": "# 真实"}, Criticality.ROUTINE
        )

        # 对调用方透明：形状不变，只是 subject_id 带前缀，调用方据此跳过。
        assert isinstance(result, JudgeVerdict)
        assert is_golden_subject(result.subject_id)
        assert not is_golden_subject("case-real")
        # 送给模型的是黄金用例的内容，且模板 key 一致（否则渲染就露馅了）。
        assert built[0].requests[0].content == {"skill_md": "# 黄金用例"}
        assert golden_repo.queried_template_keys == ["omission_audit"]
        # 命中也记账：没有分母就算不出失误率。
        assert len(miss_repo.recorded) == 1
        assert miss_repo.recorded[0][0].is_miss is False
        assert repo.verdicts  # 黄金判决同样入库，带 __golden__ 前缀便于过滤

    async def test_mismatch_with_human_label_is_recorded_as_miss(self) -> None:
        monitor, miss_repo, health_repo = _monitor()
        judge, _, _ = _judge(
            script=[[(JudgeVerdictStatus.PASS, "看起来没问题")]],
            monitor=monitor,
            golden_repo=FakeGoldenRepo([self._golden(JudgeVerdictStatus.FAIL)]),
            golden_rate=1.0,
        )
        await judge.judgmental_verdict(
            "case-real", "omission_audit", {"skill_md": "# 真实"}, Criticality.ROUTINE
        )
        assert miss_repo.recorded[0][0].is_miss is True
        assert health_repo.upserts  # 当场跑了一次健康检查

    async def test_zero_rate_never_injects(self) -> None:
        golden_repo = FakeGoldenRepo([self._golden(JudgeVerdictStatus.FAIL)])
        judge, _, _built = _judge(
            script=[[(JudgeVerdictStatus.PASS, "x")]], golden_repo=golden_repo, golden_rate=0.0
        )
        result = await judge.judgmental_verdict(
            "case-real", "omission_audit", {"skill_md": "# 真实"}, Criticality.ROUTINE
        )
        assert result.subject_id == "case-real"  # type: ignore[union-attr]
        assert golden_repo.queried_template_keys == []


# --------------------------------------------------------------------------- #
# 失误率与冻结
# --------------------------------------------------------------------------- #


class JudgeHealthTests:
    def test_temperature_bucket_reports_na_when_sampling_is_unavailable(self) -> None:
        # 该模型上所有请求温度物理同档，硬分桶会把统计样本切碎，冻结机制形同虚设。
        assert temperature_bucket(NO_SAMPLING_MODEL, 0.1) == "n/a"
        assert temperature_bucket(NO_SAMPLING_MODEL, 0.5) == "n/a"
        assert temperature_bucket(SAMPLING_MODEL, 0.1) == "low"
        assert temperature_bucket(SAMPLING_MODEL, 0.3) == "mid"
        assert temperature_bucket(SAMPLING_MODEL, 0.9) == "high"

    async def test_empty_window_is_healthy(self) -> None:
        monitor, _, _ = _monitor(window=[])
        health = await monitor.check(model=SAMPLING_MODEL, temperature=0.1)
        assert health.healthy and not health.frozen and health.miss_rate == 0.0

    async def test_miss_rate_above_threshold_freezes_the_config(self) -> None:
        window = [True, False, False, False, False, False, False, False, False, False]  # 10%
        monitor, _, health_repo = _monitor(window=window, threshold=0.05)
        health = await monitor.check(model=SAMPLING_MODEL, temperature=0.1)
        assert health.miss_rate == pytest.approx(0.1)
        assert health.frozen and not health.healthy
        assert health_repo.upserts[-1]["frozen"] is True
        assert "失误率" in health_repo.upserts[-1]["reason"]

        # 冻结后不再发请求，而不是降级放行。
        with pytest.raises(JudgeFrozenError):
            await monitor.ensure_not_frozen(model=SAMPLING_MODEL, temperature=0.1)

    async def test_miss_rate_below_threshold_stays_healthy(self) -> None:
        window = [False] * 40
        monitor, _, _ = _monitor(window=window, threshold=0.05)
        health = await monitor.check(model=SAMPLING_MODEL, temperature=0.1)
        assert health.healthy

    async def test_unfreeze_keeps_history(self) -> None:
        monitor, miss_repo, health_repo = _monitor(window=[True] * 10, frozen=True)
        await monitor.unfreeze(model=SAMPLING_MODEL, temperature=0.1, operator="alice")
        assert health_repo.upserts[-1]["frozen"] is False
        # 历史记录没有被清空：想让口径变干净只能靠新的黄金判决把旧记录挤出窗口。
        assert miss_repo.window == [True] * 10
