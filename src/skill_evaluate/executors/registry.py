"""可插拔执行后端注册机制（docs/dev/03 第 4.1 节）。

默认注册 "hermes"；docs/dev/19（跨模型泛化）新增第二个异构后端时，只需
`@register_backend("llama_control")` 注册一个新类，不改动本文件其余部分。
"""

from __future__ import annotations

from collections.abc import Callable

from skill_evaluate.errors import ConfigurationError
from skill_evaluate.executors.base import ExecutorBackend

_BACKEND_REGISTRY: dict[str, type[ExecutorBackend]] = {}
_BACKEND_FACTORIES: dict[str, Callable[[], ExecutorBackend]] = {}


def register_backend(
    name: str, factory: Callable[[], ExecutorBackend] | None = None
) -> Callable[[type[ExecutorBackend]], type[ExecutorBackend]]:
    """类装饰器：注册一个 `ExecutorBackend` 实现。

    `factory` 可选：提供"如何从全局配置构造该后端实例"的零参构造函数。未提供
    时 `get_backend()` 会尝试无参构造类本身，构造失败需调用方改用
    `register_backend_instance_factory()` 补充。
    """

    def _decorator(cls: type[ExecutorBackend]) -> type[ExecutorBackend]:
        _BACKEND_REGISTRY[name] = cls
        if factory is not None:
            _BACKEND_FACTORIES[name] = factory
        return cls

    return _decorator


def register_backend_factory(name: str, factory: Callable[[], ExecutorBackend]) -> None:
    """为已注册的后端补充/覆盖零参构造工厂（供 config 驱动的懒加载场景使用）。"""
    _BACKEND_FACTORIES[name] = factory


def get_backend(name: str) -> ExecutorBackend:
    if name not in _BACKEND_REGISTRY:
        raise ConfigurationError(
            f"未知的 ExecutorBackend 名称: {name!r}，已注册: {sorted(_BACKEND_REGISTRY)}"
        )
    factory = _BACKEND_FACTORIES.get(name)
    if factory is not None:
        return factory()
    return _BACKEND_REGISTRY[name]()


def list_registered_backends() -> list[str]:
    return sorted(_BACKEND_REGISTRY)
