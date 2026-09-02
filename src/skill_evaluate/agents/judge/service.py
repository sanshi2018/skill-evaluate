"""`JudgeAgent`：全项目唯一的判定入口（docs/dev/08 第 2、6 节）。

## 一条铁律

**凡是"通过/失败"的结论，都必须经过本类**。评测维度节点不允许自己写 if/else 下
结论，也不允许绕开本类直接调 `MiniReviewAgent` 做判定用途——不是为了洁癖，而是
因为黄金基准盲测、共识投票、失误率冻结这三件事都挂在这个入口上：绕过入口 =
绕过全部可信度机制，而报告读者看不出哪条判定绕过了。

（`MiniReviewAgent` 本身仍可被非判定场景直接使用，例如只想拿结构化输出做统计。）

## 两类职责，两个方法

| 方法 | 判定依据 | 用不用 LLM | 可信度机制 |
|---|---|---|---|
| `quantitative_verdict()` | 结构化数据的确定性聚合（触发率、覆盖率阈值） | 否 | 不需要——算术没有幻觉 |
| `judgmental_verdict()` | 文本/Trace 的语义理解（是否真的被注入绕过） | 是 | 黄金盲测 + 可选共识投票 |

## 为什么 `JudgeAgent` 不继承 `BaseLLMAgent`

`docs/dev/interfaces/06_llm_client_and_sampling.md` 给的样例是继承 `BaseLLMAgent`
自己发 Prompt。但裁量判定的 Prompt 就是 docs/dev/07 的评审模板，模板执行是
`MiniReviewAgent` 的职责（interfaces/07 第 5 节说得很明确：投票是 Judge 的职责，
模板执行是 Mini Agent 的职责，两层不重叠）。让 `JudgeAgent` 再继承一次 LLM 底座，
等于凭空多出一条"Judge 自己那套 Prompt"，与模板注册表分叉。所以本类是纯编排：
它持有若干 `MiniReviewAgent` 副本，自己一个 Prompt 都不写。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from random import Random
from typing import Any

from skill_evaluate.agents.judge.consensus import (
    ReplicaSpec,
    build_replica_specs,
    evaluate_consensus,
)
from skill_evaluate.agents.judge.golden_injector import (
    GoldenInjection,
    is_golden_subject,
    maybe_inject_golden_case,
)
from skill_evaluate.agents.judge.health import JudgeHealthMonitor
from skill_evaluate.agents.judge.rules import get_rule
from skill_evaluate.agents.mini.service import (
    DEFAULT_REVIEW_TEMPERATURE,
    MiniReviewAgent,
    ReviewRequest,
)
from skill_evaluate.config import get_settings
from skill_evaluate.logging import get_logger
from skill_evaluate.observability.langfuse_adapter import LangfuseAdapter, LangfuseTraceHandle
from skill_evaluate.persistence.repository import GoldenCaseRepository, JudgeRepository
from skill_evaluate.state.enums import Criticality
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict

logger = get_logger(component="judge")

QUANTITATIVE_MODEL_PREFIX = "rule:"


class JudgeAgent:
    """裁判核心。各评测维度节点通过依赖注入拿到同一个实例。"""

    name = "judge_agent"

    def __init__(
        self,
        *,
        model: str | None = None,
        temperature: float = DEFAULT_REVIEW_TEMPERATURE,
        judge_repository: JudgeRepository | None = None,
        health_monitor: JudgeHealthMonitor | None = None,
        langfuse_adapter: LangfuseAdapter | None = None,
        trace_handle: LangfuseTraceHandle | None = None,
        review_agent_factory: Any | None = None,
        golden_inject_rate: float | None = None,
        golden_repository: GoldenCaseRepository | None = None,
        golden_rng: Random | None = None,
    ) -> None:
        settings = get_settings()
        self._settings = settings.judge
        # 默认走 Mini 通道（成本）：模板体系本来就在那边，Judge 只负责投票。
        # `consensus_uses_mini_model=False` 时改用 `judge_model`（能力优先）。
        self._model = model or (
            settings.llm.mini_agent_model
            if self._settings.consensus_uses_mini_model
            else settings.llm.judge_model
        )
        self._temperature = temperature
        self._judge_repo = judge_repository or JudgeRepository()
        self._health = health_monitor or JudgeHealthMonitor()
        self._langfuse = langfuse_adapter
        self._trace_handle = trace_handle
        self._golden_inject_rate = golden_inject_rate
        self._golden_repo = golden_repository
        self._golden_rng = golden_rng
        # 测试与 docs/dev/19（跨模型）用的注入点：给定 spec 造一个评审副本。
        self._review_agent_factory = review_agent_factory or self._default_review_agent

    # ------------------------------------------------------------------ #
    # 1. 量化判定
    # ------------------------------------------------------------------ #

    def quantitative_verdict(
        self, subject_id: str, rule_name: str, inputs: dict[str, Any]
    ) -> JudgeVerdict:
        """按 `judge/rules.py` 中注册的确定性规则产出判定。**同步、不调 LLM、不落库**。

        - 同步：纯算术，没有 IO，硬套 async 只会逼调用方在非 async 上下文里绕路。
        - 不落库：量化判定往往在一个循环里成百上千次地产生（每个用例每个规则一
          次），逐条写库既慢又没人读；需要归档的调用方自行 `JudgeRepository.save_verdict()`，
          需要进报告的走 `ReportGenerator.record_dimension_result()`。
        - `model` 字段填 `rule:<rule_name>`：报告里能一眼看出这条判定不是模型给的，
          `temperature` 恒为 0.0（没有采样这回事）。
        """
        rule = get_rule(rule_name)
        status = rule(inputs)
        verdict = JudgeVerdict(
            verdict_id=str(uuid.uuid4()),
            subject_id=subject_id,
            status=status,
            reasoning=f"quantitative rule {rule_name!r} over inputs={inputs!r}",
            temperature=0.0,
            model=f"{QUANTITATIVE_MODEL_PREFIX}{rule_name}",
            created_at=datetime.now(UTC),
        )
        logger.info(
            "judge_quantitative_verdict",
            subject_id=subject_id,
            rule_name=rule_name,
            status=status.value,
        )
        return verdict

    # ------------------------------------------------------------------ #
    # 2. 裁量判定
    # ------------------------------------------------------------------ #

    async def judgmental_verdict(
        self,
        subject_id: str,
        template_key: str,
        content: dict[str, str],
        criticality: Criticality,
    ) -> JudgeVerdict | ConsensusResult:
        """走 LLM 的语义裁决。

        - `criticality=ROUTINE` -> 单副本，返回 `JudgeVerdict`；
        - `criticality=CRITICAL` -> 3 副本背靠背复核，返回 `ConsensusResult`。

        黄金基准注入对调用方**完全透明**：返回值形状不变，但当本次被注入为黄金
        用例时，`subject_id` 会带 `__golden__:` 前缀。调用方按约定用
        `golden_injector.is_golden_subject()` 识别并**跳过**，不计入报告
        （docs/dev/08 第 3.2 节）。

        `criticality` 由调用方显式声明，本类不猜——见 `Criticality` 的文档字符串。
        """
        await self._health.ensure_not_frozen(model=self._model, temperature=self._temperature)

        request = ReviewRequest(
            subject_id=subject_id, template_key=template_key, content=dict(content)
        )
        injection = await maybe_inject_golden_case(
            request,
            rate=self._golden_inject_rate,
            repository=self._golden_repo,
            rng=self._golden_rng,
        )

        if criticality is Criticality.CRITICAL:
            result: JudgeVerdict | ConsensusResult = await self._consensus_vote(injection.request)
        else:
            result = await self._single_verdict(injection.request)

        await self._account_for_golden(injection, result)
        return result

    async def _single_verdict(self, request: ReviewRequest) -> JudgeVerdict:
        agent = self._review_agent_factory(
            ReplicaSpec(
                label="routine",
                model=None,
                temperature=self._temperature,
                system_suffix="",
            )
        )
        verdict = await agent.review(request)
        await self._judge_repo.save_verdict(verdict)
        logger.info(
            "judge_routine_verdict",
            subject_id=request.subject_id,
            template_key=request.template_key,
            status=verdict.status.value,
        )
        return verdict

    async def _consensus_vote(self, request: ReviewRequest) -> ConsensusResult:
        """3 副本背靠背独立复核（docs/dev/08 第 4.2 节）。

        `asyncio.gather` 是"背靠背独立"的实现方式：副本之间不共享中间结果，谁也
        看不到别人的结论——串行执行时很容易被后来者写成"参考上一份判决"，那就
        不是独立复核而是自我确认了。
        """
        specs = build_replica_specs(
            strategy=self._settings.consensus_strategy,
            base_temperature=self._temperature,
            temperatures=self._settings.consensus_temperatures,
            models=self._settings.consensus_models,
        )
        agents = [self._review_agent_factory(spec) for spec in specs]
        verdicts = await asyncio.gather(*(agent.review(request) for agent in agents))

        result = evaluate_consensus(request.subject_id, list(verdicts))
        for verdict in result.verdicts:
            await self._judge_repo.save_verdict(verdict)
        await self._judge_repo.save_consensus(result)

        log = logger.info if result.consensus_reached else logger.warning
        log(
            "judge_consensus_completed",
            subject_id=request.subject_id,
            template_key=request.template_key,
            strategy=self._settings.consensus_strategy,
            replicas=[spec.label for spec in specs],
            consensus_reached=result.consensus_reached,
            final_status=result.final_status.value,
            dissenting_node=result.dissenting_node,
        )
        return result

    # ------------------------------------------------------------------ #
    # 3. 黄金用例记账与健康检查
    # ------------------------------------------------------------------ #

    async def _account_for_golden(
        self, injection: GoldenInjection, result: JudgeVerdict | ConsensusResult
    ) -> None:
        """本次是黄金注入时，比对人类标定并记账；顺带跑一次健康检查。

        健康检查放在这里而不是等 docs/dev/24 的定时任务：失误是在这一刻发生的，
        当场检查才能让**下一次**判定就被冻结拦住。docs/dev/24 仍可另外挂定时
        巡检，两者不冲突（`check()` 是幂等的统计+upsert）。
        """
        if injection.golden is None:
            return

        status = result.final_status if isinstance(result, ConsensusResult) else result.status
        await self._health.record_outcome(
            golden=injection.golden,
            judge_status=status,
            model=self._model,
            temperature=self._temperature,
        )
        await self._health.check(model=self._model, temperature=self._temperature)

    async def check_judge_health(self, window_size: int = 50) -> bool:
        """本实例所用配置的健康检查（docs/dev/08 第 3.3 节）。返回 True 表示未冻结。"""
        health = await self._health.check(
            model=self._model, temperature=self._temperature, window_size=window_size
        )
        return health.healthy

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _default_review_agent(self, spec: ReplicaSpec) -> MiniReviewAgent:
        # persist=False：由本类统一决定哪些 verdict 入库（interfaces/07 第 5 节的约定）。
        return MiniReviewAgent(
            model=spec.model or self._model,
            temperature=spec.temperature,
            langfuse_adapter=self._langfuse,
            trace_handle=self._trace_handle,
            judge_repository=self._judge_repo,
            persist=False,
            system_suffix=spec.system_suffix or None,
        )


__all__ = ["QUANTITATIVE_MODEL_PREFIX", "JudgeAgent", "is_golden_subject"]
