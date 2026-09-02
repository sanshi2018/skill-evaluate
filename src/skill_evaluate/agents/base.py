"""`BaseLLMAgent`：docs/dev/06~10 五个 Agent 共用的基类（docs/dev/06 第 10 节）。

存在的理由是消除样板：五个 Agent 都要做"调用 LLM -> 统计 TimingCostMetrics ->
Langfuse 打点 -> 结构化解析 + 校验失败重试"。这四件事在此实现一次，子类只写
Prompt 与业务语义。

本文件同时是 **docs/dev/interfaces/05_langfuse_hook_and_agent_base.md 第 1 节
"Agent 基类挂载 Langfuse 钩子"的正式实现**：`_call_llm()` 末尾调用
`LangfuseAdapter.log_agent_call()`。`trace_handle` 按该接口文档的建议以旁路依赖
注入方式传入（构造参数），**不塞进 `PipelineState`**，以维持"状态里只存 ID/引用"
的约定。未传时打点自动降级为 no-op，Agent 照常工作。
"""

from __future__ import annotations

import time
from abc import ABC
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from skill_evaluate.agents.llm import (
    AgentLLMClient,
    LLMCompletion,
    build_default_llm_client,
    build_response_schema,
    parse_structured_response,
)
from skill_evaluate.config import get_settings
from skill_evaluate.errors import AgentResponseFormatError
from skill_evaluate.logging import get_logger
from skill_evaluate.observability.langfuse_adapter import LangfuseAdapter, LangfuseTraceHandle
from skill_evaluate.state.trace import TimingCostMetrics

TOutput = TypeVar("TOutput", bound=BaseModel)


class BaseLLMAgent(ABC):
    """全部 LLM 业务智能体的基类。

    子类约定：
    - 覆写类属性 `name`（进入 Langfuse generation 名与结构化日志的 agent 字段）。
    - 只调用 `_call_llm()` / `_call_llm_text()`，不直接碰 `AgentLLMClient`。
    """

    name: str = "llm_agent"

    def __init__(
        self,
        *,
        model: str,
        temperature: float,
        llm_client: AgentLLMClient | None = None,
        langfuse_adapter: LangfuseAdapter | None = None,
        trace_handle: LangfuseTraceHandle | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        self._model = model
        self._temperature = temperature
        # 真实客户端的构造会读 API Key 并可能抛 ConfigurationError，因此延迟到
        # 首次调用（`_client` property）——只做静态审查/单测的调用方不应因为
        # "没配 Key"而无法实例化 Agent。
        self._llm_client = llm_client
        self._langfuse = langfuse_adapter if langfuse_adapter is not None else LangfuseAdapter()
        self._trace_handle = trace_handle
        self._max_output_tokens = max_output_tokens
        self._last_usage: TimingCostMetrics | None = None
        self._logger = get_logger(agent=self.name, model=model)

    # ------------------------------------------------------------------ #
    # 只读属性：docs/dev/07 构造 JudgeVerdict 时需要回填 model/temperature
    # ------------------------------------------------------------------ #

    @property
    def model(self) -> str:
        return self._model

    @property
    def temperature(self) -> float:
        return self._temperature

    @property
    def last_usage(self) -> TimingCostMetrics | None:
        """最近一次 `_call_llm()` 的成本指标，供调用方写进报告/共识记录。"""
        return self._last_usage

    @property
    def _client(self) -> AgentLLMClient:
        if self._llm_client is None:
            self._llm_client = build_default_llm_client()
        return self._llm_client

    # ------------------------------------------------------------------ #
    # 核心调用
    # ------------------------------------------------------------------ #

    async def _call_llm(
        self,
        prompt: str,
        response_schema: type[TOutput],
        *,
        system: str | None = None,
    ) -> TOutput:
        """调用 LLM 并把响应解析为 `response_schema` 实例。

        校验失败会重试（次数由 `LLMSettings.max_structured_retries` 控制，
        docs/dev/06 第 5.3 节要求 2 次），重试时把上一轮的错误回灌给模型；
        用尽后抛 `AgentResponseFormatError`——**不返回半成品**。
        """
        json_schema = build_response_schema(response_schema)
        max_retries = get_settings().llm.max_structured_retries
        attempt_prompt = prompt
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            completion = await self._invoke(
                attempt_prompt, system=system, response_schema=json_schema
            )
            try:
                parsed = parse_structured_response(completion.text, response_schema)
            except (ValueError, ValidationError) as exc:
                last_error = exc
                self._logger.warning(
                    "agent_structured_parse_failed",
                    attempt=attempt,
                    max_retries=max_retries,
                    error=str(exc)[:500],
                )
                attempt_prompt = self._repair_prompt(prompt, str(exc))
                continue
            return parsed  # type: ignore[return-value]

        raise AgentResponseFormatError(
            f"{self.name} 连续 {max_retries + 1} 次未能产出符合 "
            f"{response_schema.__name__} 的结构化输出：{last_error}"
        )

    async def _call_llm_text(self, prompt: str, *, system: str | None = None) -> str:
        """无结构化约束的纯文本调用（供不需要 JSON 的场景使用）。"""
        completion = await self._invoke(prompt, system=system, response_schema=None)
        return completion.text

    async def _invoke(
        self,
        prompt: str,
        *,
        system: str | None,
        response_schema: dict[str, Any] | None,
    ) -> LLMCompletion:
        start = time.monotonic()
        completion = await self._client.complete(
            prompt=prompt,
            model=self._model,
            temperature=self._temperature,
            system=system,
            max_tokens=self._max_output_tokens,
            response_schema=response_schema,
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        usage = TimingCostMetrics(
            total_tokens=completion.prompt_tokens + completion.completion_tokens,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            duration_ms=duration_ms,
        )
        self._last_usage = usage

        # docs/dev/interfaces/05 第 1 节要求的挂载点。
        if self._trace_handle is not None:
            self._langfuse.log_agent_call(
                self._trace_handle,
                agent_name=self.name,
                prompt=prompt,
                response=completion.text,
                model=completion.model,
                usage=usage,
            )

        self._logger.info(
            "agent_llm_call",
            duration_ms=duration_ms,
            total_tokens=usage.total_tokens,
            temperature_applied=completion.temperature_applied,
            structured=response_schema is not None,
        )
        return completion

    @staticmethod
    def _repair_prompt(original_prompt: str, error: str) -> str:
        """重试时的自纠正提示：把校验错误原样回灌，比单纯重发更容易收敛。"""
        return (
            f"{original_prompt}\n\n"
            "---\n"
            "上一次回复无法通过结构化校验，错误如下：\n"
            f"{error}\n"
            "请**只**输出修正后的 JSON 对象，不要任何解释性文字。"
        )
