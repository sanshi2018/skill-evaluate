"""按配置 / 按节点路由构造 `ExecutorBackend` 实例的统一入口。

`nodes/` 下的节点应通过 `get_backend_for_node()` 拿到实例，而不是自行 import
`MiniAgentBackend`/`HermesBackend`（docs/dev/03 第 2 节）。
"""

from __future__ import annotations

from skill_evaluate.config import get_settings

# 触发 `hermes` 的注册（模块导入即执行装饰器）。
from skill_evaluate.executors import hermes_backend as _hermes_backend  # noqa: F401

# 触发 `llama_control` 的注册（docs/dev/19：模块九的备用异构代理）。
from skill_evaluate.executors import llama_backend as _llama_backend  # noqa: F401
from skill_evaluate.executors.base import ExecutorBackend
from skill_evaluate.executors.mini_backend import MiniAgentBackend
from skill_evaluate.executors.registry import get_backend
from skill_evaluate.executors.routing import resolve_backend_type
from skill_evaluate.state.enums import ExecutorBackendType


def build_backend(backend_type: ExecutorBackendType) -> ExecutorBackend:
    if backend_type == ExecutorBackendType.MINI:
        # docs/dev/07 落地后，注入真实的 MiniLLMClient 取代 StubMiniLLMClient
        # （见 docs/dev/interfaces/05_langfuse_hook_and_agent_base.md 第 2 节）。
        # 这里的构造是惰性的：`RealMiniLLMClient` 直到首次 complete() 才会去读
        # API Key，所以没配 Key 的环境仍然可以构造后端。
        from skill_evaluate.agents.mini.llm_client import RealMiniLLMClient

        return MiniAgentBackend(
            llm_client=RealMiniLLMClient(), model=get_settings().llm.mini_agent_model
        )
    settings = get_settings()
    return get_backend(
        settings.executor.backend if settings.executor.backend != "mini" else "hermes"
    )


def get_backend_for_node(node_name: str) -> ExecutorBackend:
    """按 `routing.NODE_BACKEND_ROUTING` 为指定评测维度节点构造对应后端。"""
    return build_backend(resolve_backend_type(node_name))
