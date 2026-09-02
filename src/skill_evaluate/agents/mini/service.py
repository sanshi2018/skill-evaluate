"""`MiniReviewAgent`：通用低温结构化评审能力（docs/dev/07 第 3 节）。

**与 `MiniAgentBackend`（docs/dev/03）的边界**（docs/dev/07 第 2 节）：
`MiniAgentBackend` 回答"用什么后端跑"（产出 `ExecutionTrace`），
`MiniReviewAgent` 回答"跑什么审查逻辑"（产出 `JudgeVerdict`）。两者正交。模块二
这类纯静态审查直接用本类，不需要 `ExecutionTrace` 那一层。

**为什么直接返回并落库 `JudgeVerdict`**：纯静态审查场景不需要 docs/dev/08 的多
副本共识（那是留给"高危/低覆盖率"这类重大负面判定的），单次输出就是最终判定。
若某个审查点后续被升级为需要共识投票，由 Judge Agent 在其共识流程内部**多次
调用同一个 `review()`**，而不是让本类自己实现投票——投票是 Judge 的职责。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from skill_evaluate.agents.base import BaseLLMAgent
from skill_evaluate.agents.llm import AgentLLMClient, model_supports_sampling
from skill_evaluate.agents.mini.templates.registry import ReviewTemplate, get_template
from skill_evaluate.config import get_settings
from skill_evaluate.logging import get_logger
from skill_evaluate.observability.langfuse_adapter import LangfuseAdapter, LangfuseTraceHandle
from skill_evaluate.persistence.repository import JudgeRepository
from skill_evaluate.state.judge import JudgeVerdict

logger = get_logger(component="mini_review")

DEFAULT_REVIEW_TEMPERATURE = 0.1  # 架构文档模块二："以极低的温度值进行逻辑审计"

_SYSTEM_PROMPT = (
    "你是一位苛刻但公允的审查员。你的判定会进入 CI/CD 流水线，可能阻断一次合并"
    "请求，因此每一个 fail 都必须有原文依据。你只输出 JSON。"
)


class ReviewRequest(BaseModel):
    """一次评审请求。"""

    subject_id: str  # 落到 JudgeVerdict.subject_id（case_id / skill_id / script_path 视场景而定）
    template_key: str  # 见 templates/registry.py 的注册表
    content: dict[str, str] = Field(default_factory=dict)  # 模板变量


@dataclass(slots=True)
class DetailedReview:
    """`review_detailed()` 的返回值：判定 + 原始结构化输出。

    需要读取模板专有字段（如 `OmissionAuditOutput.common_sense_statements`）或需要
    走 `ReviewTemplate.to_severity`（docs/dev/15）的调用方用这个；只关心
    pass/fail 的调用方用 `review()` 即可。
    """

    verdict: JudgeVerdict
    output: BaseModel
    template: ReviewTemplate


class MiniReviewAgent(BaseLLMAgent):
    """执行注册表中任意一个评审模板。"""

    name = "mini_review_agent"

    def __init__(
        self,
        *,
        model: str | None = None,
        temperature: float = DEFAULT_REVIEW_TEMPERATURE,
        llm_client: AgentLLMClient | None = None,
        langfuse_adapter: LangfuseAdapter | None = None,
        trace_handle: LangfuseTraceHandle | None = None,
        judge_repository: JudgeRepository | None = None,
        persist: bool = True,
    ) -> None:
        resolved_model = model or get_settings().llm.mini_agent_model
        super().__init__(
            model=resolved_model,
            temperature=temperature,
            llm_client=llm_client,
            langfuse_adapter=langfuse_adapter,
            trace_handle=trace_handle,
        )
        self._judge_repo = judge_repository or JudgeRepository()
        # 允许关闭落库：docs/dev/08 的共识流程会对同一 subject 连打多次，
        # 由 Judge 侧统一决定哪些 verdict 值得入库。
        self._persist = persist

        if not model_supports_sampling(resolved_model):
            # 如实告警而不是假装温度生效了：架构文档对本 Agent 的"Temperature=0.1"
            # 要求在这类模型上物理不成立，判定的确定性此时只能靠 Prompt 约束。
            logger.warning(
                "mini_review_temperature_ignored",
                model=resolved_model,
                requested_temperature=temperature,
                hint="该模型已移除采样参数，temperature 不会被发送；见 "
                "docs/dev/interfaces/06_llm_client_and_sampling.md",
            )

    async def review(self, request: ReviewRequest) -> JudgeVerdict:
        """执行评审，返回（并按配置落库）`JudgeVerdict`。"""
        return (await self.review_detailed(request)).verdict

    async def review_detailed(self, request: ReviewRequest) -> DetailedReview:
        template = get_template(request.template_key)
        prompt = template.render(request.content)

        raw = await self._call_llm(prompt, template.output_schema, system=_SYSTEM_PROMPT)

        verdict = JudgeVerdict(
            verdict_id=str(uuid.uuid4()),
            subject_id=request.subject_id,
            status=template.to_status(raw),
            # 首批模板都继承 BaseReviewOutput（必带 reasoning）；用 getattr 取值是为了
            # 让后续文档新增的、结构不同的模板也能落库，而不是在这里崩掉。
            reasoning=str(getattr(raw, "reasoning", "")),
            temperature=self.temperature,
            model=self.model,
            created_at=datetime.now(UTC),
        )
        if self._persist:
            await self._judge_repo.save_verdict(verdict)

        logger.info(
            "mini_review_completed",
            template_key=request.template_key,
            subject_id=request.subject_id,
            status=verdict.status.value,
        )
        return DetailedReview(verdict=verdict, output=raw, template=template)


__all__ = [
    "DEFAULT_REVIEW_TEMPERATURE",
    "DetailedReview",
    "MiniReviewAgent",
    "ReviewRequest",
]
