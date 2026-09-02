"""执行引擎适配层（docs/dev/03_执行引擎适配层与Hermes_Hook协议.md）。"""

from skill_evaluate.executors.base import ExecutionRequest, ExecutorBackend
from skill_evaluate.executors.factory import build_backend, get_backend_for_node
from skill_evaluate.executors.hermes_backend import (
    HermesBackend,
    HermesHookPayload,
    map_hermes_payload_to_trace,
)
from skill_evaluate.executors.mini_backend import MiniAgentBackend
from skill_evaluate.executors.registry import get_backend, register_backend
from skill_evaluate.executors.routing import NODE_BACKEND_ROUTING, resolve_backend_type

__all__ = [
    "NODE_BACKEND_ROUTING",
    "ExecutionRequest",
    "ExecutorBackend",
    "HermesBackend",
    "HermesHookPayload",
    "MiniAgentBackend",
    "build_backend",
    "get_backend",
    "get_backend_for_node",
    "map_hermes_payload_to_trace",
    "register_backend",
    "resolve_backend_type",
]
