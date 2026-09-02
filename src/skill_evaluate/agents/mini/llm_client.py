"""真实的 `MiniLLMClient` 实现（docs/dev/interfaces/05 第 2 节的接入项）。

`executors/mini_backend.py::MiniAgentBackend` 在 docs/dev/07 落地前默认注入
`StubMiniLLMClient`（返回带 `[stub]` 标记的占位文本）。本文件提供真实实现，由
`executors/factory.py::build_backend()` 注入，替换掉那个桩。

注意这里**只是把 `AgentLLMClient` 适配成 `MiniLLMClient` 协议**，不含评审逻辑
——评审逻辑在 `agents/mini/service.py::MiniReviewAgent`。这正是 docs/dev/07 第 2
节强调的正交关系：后端负责"跑"，Agent 负责"审什么"。
"""

from __future__ import annotations

from skill_evaluate.agents.llm import AgentLLMClient, build_default_llm_client
from skill_evaluate.config import get_settings
from skill_evaluate.executors.mini_backend import MiniLLMResult


class RealMiniLLMClient:
    """把 `AgentLLMClient` 适配成 `executors.mini_backend.MiniLLMClient` 协议。"""

    def __init__(self, llm_client: AgentLLMClient | None = None) -> None:
        # 延迟构造：没配 API Key 的环境（本地跑单测、只跑静态检查的 CI）不应该
        # 因为 import 到这个类就失败。
        self._llm_client = llm_client

    @property
    def _client(self) -> AgentLLMClient:
        if self._llm_client is None:
            self._llm_client = build_default_llm_client()
        return self._llm_client

    async def complete(self, *, prompt: str, model: str, temperature: float) -> MiniLLMResult:
        completion = await self._client.complete(
            prompt=prompt,
            model=model or get_settings().llm.mini_agent_model,
            temperature=temperature,
        )
        return MiniLLMResult(
            text=completion.text,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
        )


__all__ = ["RealMiniLLMClient"]
