"""按配置 / 按节点路由构造 `ExecutorBackend` 实例的统一入口。

`nodes/` 下的节点应通过 `get_backend_for_node()` 拿到实例，而不是自行 import
`MiniAgentBackend`/`HermesBackend`（docs/dev/03 第 2 节）。
"""

from __future__ import annotations

from skill_evaluate.config import get_settings

# 触发 `hermes` 的注册（模块导入即执行装饰器）。
from skill_evaluate.executors import hermes_backend as _hermes_backend  # noqa: F401
from skill_evaluate.executors.base import ExecutorBackend
from skill_evaluate.executors.mini_backend import MiniAgentBackend
from skill_evaluate.executors.registry import get_backend
from skill_evaluate.executors.routing import resolve_backend_type
from skill_evaluate.state.enums import ExecutorBackendType


def build_backend(backend_type: ExecutorBackendType) -> ExecutorBackend:
    if backend_type == ExecutorBackendType.MINI:
        return MiniAgentBackend()
    settings = get_settings()
    return get_backend(
        settings.executor.backend if settings.executor.backend != "mini" else "hermes"
    )


def get_backend_for_node(node_name: str) -> ExecutorBackend:
    """按 `routing.NODE_BACKEND_ROUTING` 为指定评测维度节点构造对应后端。"""
    return build_backend(resolve_backend_type(node_name))
