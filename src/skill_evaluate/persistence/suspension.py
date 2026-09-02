"""挂起-外部事件唤醒通用机制（docs/dev/04 第 5 节）。

同一套机制供 Hermes 异步回调（docs/dev/03/05）与人工审批场景（docs/dev/22）
复用，区别只在于唤醒来源（Webhook vs. 人工在审查工作台点击"批准"后调用的
内部 API）。`pending_hooks` 与 `human_approvals` 两张表本质上都是
`wait_key -> resume_payload` 的具体化。

**待接入说明**：`resolve_suspension()` 最终需要调用"已编译主图"的
`graph.invoke(Command(resume=...), config={"configurable": {"thread_id": ...}})`
来真正唤醒挂起的节点，但"已编译主图"由 docs/dev/24（主图编排与 CI/CD 落地）
才会产出。这里通过 `GraphResumer` 协议 + `register_graph_resumer()` 注册点
留出接口：docs/dev/24 在装配主图完成 `graph.compile(...)` 后，需调用
`register_graph_resumer(...)` 注入一个真正会调用 `graph.invoke(Command(resume=...))`
的实现。在注册之前调用 `resolve_suspension()` 会抛出 `ConfigurationError`，
不会静默失败——见 docs/dev/interfaces/04_graph_resumer.md。
"""

from __future__ import annotations

from typing import Any, Protocol

from langgraph.types import interrupt

from skill_evaluate.errors import ConfigurationError
from skill_evaluate.persistence.repository import HumanApprovalRepository, PendingHookRepository


class GraphResumer(Protocol):
    async def resume(self, *, thread_id: str, resume_payload: Any) -> None: ...


_graph_resumer: GraphResumer | None = None


def register_graph_resumer(resumer: GraphResumer) -> None:
    """docs/dev/24 在主图 `compile()` 完成后调用，注入真正的 `graph.invoke(Command(resume=...))`。"""
    global _graph_resumer
    _graph_resumer = resumer


def _get_graph_resumer() -> GraphResumer:
    if _graph_resumer is None:
        raise ConfigurationError(
            "尚未注册 GraphResumer：请在主图编译完成后调用 "
            "skill_evaluate.persistence.suspension.register_graph_resumer(...) "
            "（见 docs/dev/interfaces/04_graph_resumer.md，由 docs/dev/24 接入）。"
        )
    return _graph_resumer


async def suspend_and_wait(reason: str, wait_key: str) -> Any:
    """记录挂起原因，调用 LangGraph 的动态中断原语 `interrupt()`，返回值由外部
    `resolve_suspension()` 触发的 `Command(resume=...)` 传入。

    必须在已挂载 checkpointer 的图节点执行上下文中调用。
    """
    return interrupt({"reason": reason, "wait_key": wait_key})


async def resolve_suspension(wait_key: str, resume_payload: Any, thread_id: str) -> None:
    """由 Hook 端点（docs/dev/05）或人工审批 API（docs/dev/22）调用，唤醒对应挂起。

    重复回调忽略逻辑（docs/dev/04 第 6 节）：内部检查 `pending_hooks`/`human_approvals`
    的 `status`，非 `waiting` 状态的回调直接忽略（返回而不报错），调用方无需重复判断。
    """
    resolved = await PendingHookRepository().mark_resolved(wait_key, str(resume_payload))
    if not resolved:
        resolved = await HumanApprovalRepository().mark_resolved(wait_key, str(resume_payload))

    if not resolved:
        # 记录不存在或已是非 waiting 状态：视为重复/过期回调，安全忽略。
        return

    resumer = _get_graph_resumer()
    await resumer.resume(thread_id=thread_id, resume_payload=resume_payload)
