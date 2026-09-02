"""Langfuse 双写适配器（docs/dev/05 第 4 节）。

可选组件：未配置 `SKILLEVAL_LANGFUSE_*`（`LangfuseSettings.enabled=False`）时
整个适配器降级为 no-op，不影响 `benchmark.json`/HTML 主链路——可观测性增强
能力不应成为流水线单点故障。

双写触发点采用统一的"调用后钩子"模式，不要求每个 Agent/Node 手写
`langfuse_adapter.log_xxx(...)`。

**接入状态**：`log_agent_call()` 的挂载点已由 docs/dev/06 落地——
`agents/base.py::BaseLLMAgent._invoke()` 在每次 LLM 调用后统一打点，全部 Agent
（06~10）继承即可。`log_execution_trace()` 仍待各评测维度节点（docs/dev/11~20）
在拿到 `ExecutionTrace` 后自行调用一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from skill_evaluate.config import get_settings
from skill_evaluate.observability.log_sanitize import sanitize_for_log
from skill_evaluate.state.trace import ExecutionTrace, TimingCostMetrics


@dataclass
class LangfuseTraceHandle:
    """一次 run 对应的 Langfuse 顶层 Trace 句柄。no-op 模式下 `native` 恒为 None。"""

    run_id: str
    native: Any | None = None


class _LangfuseClientProtocol(Protocol):
    """真实 `langfuse` SDK client 需要满足的最小接口，供依赖注入/测试替身使用。"""

    def trace(self, **kwargs: Any) -> Any: ...


class LangfuseAdapter:
    def __init__(self, client: _LangfuseClientProtocol | None = None) -> None:
        settings = get_settings().langfuse
        self._enabled = settings.enabled
        self._client = client
        if self._enabled and self._client is None:
            self._client = self._build_default_client()

    @staticmethod
    def _build_default_client() -> _LangfuseClientProtocol | None:
        try:
            from langfuse import Langfuse  # type: ignore[import-not-found]
        except ImportError:
            # 未安装可选依赖组 `skill-evaluate[langfuse]`，降级为 no-op 而非报错。
            return None

        settings = get_settings().langfuse
        client: _LangfuseClientProtocol = Langfuse(
            public_key=settings.public_key,
            secret_key=settings.secret_key.get_secret_value(),
            host=settings.host,
        )
        return client

    @property
    def enabled(self) -> bool:
        return self._enabled and self._client is not None

    def start_run_trace(self, run_id: str, skill_id: str) -> LangfuseTraceHandle:
        if not self.enabled or self._client is None:
            return LangfuseTraceHandle(run_id=run_id, native=None)
        native = self._client.trace(
            id=run_id, name="skill-evaluate-run", metadata={"skill_id": skill_id}
        )
        return LangfuseTraceHandle(run_id=run_id, native=native)

    def log_agent_call(
        self,
        trace_handle: LangfuseTraceHandle,
        agent_name: str,
        prompt: str,
        response: str,
        model: str,
        usage: TimingCostMetrics,
    ) -> None:
        """对应 Generator/Judge/Optimizer/Validator/Analyzer/Attacker/Mini Agent 的每次 LLM 调用。"""
        if not self.enabled or trace_handle.native is None:
            return
        trace_handle.native.generation(
            name=agent_name,
            model=model,
            input=sanitize_for_log(prompt),
            output=sanitize_for_log(response),
            usage={
                "input": usage.prompt_tokens,
                "output": usage.completion_tokens,
                "total": usage.total_tokens,
            },
        )

    def log_execution_trace(self, trace_handle: LangfuseTraceHandle, trace: ExecutionTrace) -> None:
        """把 `ExecutionTrace.actions` 映射为 Langfuse 的嵌套 Span 序列：每个 `ActionStep`
        对应一个 Span，`thought` 作为 span 的 metadata，`stdout`/`stderr` 经与
        docs/dev/03 第 8 节相同的截断/脱敏规则后作为 span output。
        """
        if not self.enabled or trace_handle.native is None:
            return
        for action in trace.actions:
            trace_handle.native.span(
                name=action.action_type,
                metadata={"thought": sanitize_for_log(action.thought), "step_id": action.step_id},
                input=action.action_input,
                output={
                    "stdout": sanitize_for_log(action.stdout),
                    "stderr": sanitize_for_log(action.stderr),
                    "exit_code": action.exit_code,
                },
            )
