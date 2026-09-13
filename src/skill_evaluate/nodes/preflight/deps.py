"""前置门禁的依赖容器（与各维度同一套写法：惰性构造、全部字段有默认、测试按需覆盖）。

装配期硬校验：路由表上 `preflight` 必须是 PLUGGABLE。拿 Mini 后端做金丝雀永远健康
（`loaded_skill_md` 恒为 True、不真正读文件），证明不了评测维度将要用的那个沙箱可信。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from skill_evaluate.config import PreflightSettings, get_settings
from skill_evaluate.errors import ConfigurationError
from skill_evaluate.executors.base import ExecutorBackend
from skill_evaluate.executors.factory import get_backend_for_node
from skill_evaluate.executors.routing import resolve_backend_type
from skill_evaluate.nodes.preflight.fingerprint import EnvironmentProbeRunner
from skill_evaluate.nodes.preflight.state import ROUTING_KEY
from skill_evaluate.persistence.repository import CanaryProbeHistoryRepository
from skill_evaluate.state.enums import ExecutorBackendType


@dataclass(slots=True)
class PreflightDeps:
    executor_backend: ExecutorBackend | None = None
    # None = 用 `executor_backend`（HermesBackend 同时实现了探测接口）。单独可注入是为了让
    # 真实部署把指纹探测交给一个独立的轻量通道（例如镜像构建流水线），而不必改节点代码。
    environment_probe_runner: EnvironmentProbeRunner | None = None
    canary_history_repository: CanaryProbeHistoryRepository = field(
        default_factory=CanaryProbeHistoryRepository
    )
    preflight_settings: PreflightSettings | None = None
    # 时钟可注入：金丝雀"24 小时内成功过"的跳过判定要能在单测里毫秒级验证。
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def settings(self) -> PreflightSettings:
        if self.preflight_settings is None:
            self.preflight_settings = get_settings().preflight
        return self.preflight_settings

    def backend(self) -> ExecutorBackend:
        if self.executor_backend is None:
            self.executor_backend = get_backend_for_node(ROUTING_KEY)
        return self.executor_backend

    def probe_runner(self) -> EnvironmentProbeRunner:
        if self.environment_probe_runner is not None:
            return self.environment_probe_runner
        backend = self.backend()
        if not isinstance(backend, EnvironmentProbeRunner):
            raise ConfigurationError(
                f"执行后端 {type(backend).__name__} 不支持沙箱环境指纹探测"
                "（需要实现 run_environment_probe()，见 docs/dev/interfaces/03_hermes_sandbox_client.md）；"
                "请注入 PreflightDeps.environment_probe_runner，或在无真实沙箱的本地环境设置 "
                "SKILLEVAL_PREFLIGHT_FINGERPRINT_CHECK_MODE=off。"
            )
        return backend


def assert_backend_routing() -> None:
    """装配期校验：前置门禁必须路由到真实沙箱后端。"""
    if resolve_backend_type(ROUTING_KEY) is not ExecutorBackendType.PLUGGABLE:
        raise ConfigurationError(
            "NODE_BACKEND_ROUTING['preflight'] 必须是 PLUGGABLE：前置门禁要证明的是评测维度将要"
            "使用的真实沙箱，Mini 后端上的金丝雀永远健康，证明不了任何事（docs/dev/21）。"
        )


__all__ = ["PreflightDeps", "assert_backend_routing"]
