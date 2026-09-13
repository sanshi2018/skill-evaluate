"""通用告警分发器的接口与默认实现（docs/dev/20 第 12 节声明调用方，docs/dev/22 实现真实通道）。

## 为什么先在这里放一个接口

docs/dev/20 是第一个**主动发告警**的维度（深度多技能冲突），而通用告警通道
（Discord Webhook 卡片、审查工作台深度链接）属于 docs/dev/22 的职责。照 docs/dev/20
正文直接写 `await alert_dispatcher.send(...)` 会引用一个不存在的对象；在节点里写
"if 22 做好了就发"又会把一个跨模块依赖藏进 if 分支里。

因此在这里固定**调用契约**（`AlertDispatcher` 协议 + 进程级注册点），默认实现
`LoggingAlertDispatcher` 只打一条结构化日志——与 docs/dev/08 冻结告警"先只有结构化
日志，由 22 把 Webhook 挂上去"是同一条路线。22 落地时：

```python
from skill_evaluate.observability.alerts import set_alert_dispatcher
set_alert_dispatcher(DiscordAlertDispatcher(...))   # 应用启动时注册一次
```

调用方（各维度节点）一行不用改。

## 两条约定

1. **发送失败不得中断评测**。告警是旁路：一次 Webhook 超时不该让已经跑完的十个
   维度的结论作废。`dispatch_alert()` 捕获异常并记日志；需要"不发出去就停下来"的
   场景（阻塞式人工审批）走 `suspend_and_wait()`，那不是告警。
2. **payload 只放 JSON 可序列化的值**（str/int/float/bool/list/dict）。Discord 卡片、
   工作台链接、审计表都要能直接存/发它，不要塞 Pydantic 对象。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from skill_evaluate.logging import get_logger

logger = get_logger(component="alerts")


@runtime_checkable
class AlertDispatcher(Protocol):
    """告警分发器协议。`alert_type` 是稳定的机器可读键（22 按它选卡片模板）。"""

    async def send(self, *, alert_type: str, run_id: str, payload: dict[str, Any]) -> None: ...


class LoggingAlertDispatcher:
    """默认实现：只写一条结构化日志 `alert_dispatched`。

    **不是**"假装发出去了"：日志事件名固定、字段完整，22 接入前运维可以直接基于日志
    平台的告警规则把它转发出去，接入后它仍可作为审计旁路保留。
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []  # 便于本地调试 / 测试断言

    async def send(self, *, alert_type: str, run_id: str, payload: dict[str, Any]) -> None:
        record = {"alert_type": alert_type, "run_id": run_id, "payload": payload}
        self.sent.append(record)
        logger.warning("alert_dispatched", alert_type=alert_type, run_id=run_id, payload=payload)


_dispatcher: AlertDispatcher = LoggingAlertDispatcher()


def get_alert_dispatcher() -> AlertDispatcher:
    """当前进程注册的告警分发器（未注册时是 `LoggingAlertDispatcher`）。"""
    return _dispatcher


def set_alert_dispatcher(dispatcher: AlertDispatcher) -> None:
    """注册真实的告警通道（docs/dev/22 在应用启动时调用一次）。"""
    global _dispatcher
    _dispatcher = dispatcher


async def dispatch_alert(
    dispatcher: AlertDispatcher, *, alert_type: str, run_id: str, payload: dict[str, Any]
) -> bool:
    """发送告警并吞掉通道异常（见模块头约定 1），返回是否发送成功。"""
    try:
        await dispatcher.send(alert_type=alert_type, run_id=run_id, payload=payload)
    except Exception as exc:  # noqa: BLE001 - 告警是旁路，通道故障不得中断评测
        logger.error("alert_dispatch_failed", alert_type=alert_type, run_id=run_id, error=str(exc))
        return False
    return True


__all__ = [
    "AlertDispatcher",
    "LoggingAlertDispatcher",
    "dispatch_alert",
    "get_alert_dispatcher",
    "set_alert_dispatcher",
]
