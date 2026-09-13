"""统一的人工审批挂起入口（docs/dev/22 第 3 节）。

所有需要人工介入的挂起点**统一**调用 `request_human_approval()`，而不是各自直接调
`suspend_and_wait()`。它比后者多做两步，且返回值语义与后者完全兼容（都是 resume payload）：

1. 落账：`human_approvals`（挂起账本，`resolve_suspension()` 依赖它做幂等唤醒）+
   `pending_approvals`（工作台卡片）；
2. 通知：新卡片发一张 Discord 卡片（通知失败不阻断——卡片本体已落库，工作台照样看得到）；
3. 挂起：`suspend_and_wait()`。

非阻塞模式（docs/dev/22 第 8.1 节）用 `notify_human()`：只写卡片 + 通知，不写账本、不挂起。

## 已接入的挂起点

| 场景 | 调用方 | decision_type |
|---|---|---|
| Optimizer 最大重试 | `agents/optimizer/loop.py::OptimizationLoop._suspend` | ACCEPT_PATCH |
| 能力树规模确认 | `nodes/coverage/nodes.py::_suspend_for_tree_review` | CONFIRM_TREE_REVIEW |
| Judge 冻结 / 连续坍塌 / 共识未达成 | `nodes/approval_guard.py`（节点级 guard） | UNFREEZE_JUDGE / INJECT_NEW_SEED / ABANDON_RUN |
| 深度多技能冲突 | `nodes/multi_skill/nodes.py::deep_conflict_approval_gate` | RESOLVE_DEEP_CONFLICT |
| 前置门禁基础设施故障 | `nodes/approval_guard.py`（仅通知） | ABANDON_RUN（非阻塞） |

## thread_id 口径（修正前序实现的一处不一致）

`persistence/checkpointer.py` 约定主图 `thread_id = f"{skill_id}:{run_id}"`，Hermes Hook
端点也按这个口径唤醒；而 docs/dev/09/16 的两个挂起点当初写的是 `thread_id = run_id`。
两者并存意味着人工批准后会去唤醒一个不存在的 thread。本服务统一解析：显式传入 >
`thread_id_for(skill_id, run_id)` > 查 `runs` 表 > 回落 run_id（记 warning）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from skill_evaluate.logging import get_logger
from skill_evaluate.observability.discord_notifier import ApprovalNotifier, get_approval_notifier
from skill_evaluate.persistence import suspension as _suspension
from skill_evaluate.persistence.checkpointer import thread_id_for
from skill_evaluate.persistence.repository import (
    HumanApprovalRepository,
    PendingApprovalRepository,
    RunRepository,
)
from skill_evaluate.state.approval import (
    ApprovalDecisionType,
    PendingApproval,
    approval_id_for,
)

logger = get_logger(component="approval_service")

SuspendFn = Callable[..., Awaitable[Any]]


class _LedgerRepository(Protocol):
    async def create(
        self, *, run_id: str, node_name: str, thread_id: str, wait_key: str
    ) -> None: ...


class _PendingRepository(Protocol):
    async def save(self, approval: PendingApproval) -> bool: ...


class _RunLookup(Protocol):
    async def get(self, run_id: str) -> Any: ...


async def _default_suspend(*, reason: str, wait_key: str) -> Any:
    """运行时再去 `suspension` 模块取 `suspend_and_wait`（而不是 import 时绑定），
    便于测试 monkeypatch `skill_evaluate.persistence.suspension.suspend_and_wait`。"""
    return await _suspension.suspend_and_wait(reason=reason, wait_key=wait_key)


class ApprovalService:
    """审批挂起与通知。全部依赖可注入（测试不碰库、不发 Webhook、不需要图上下文）。"""

    def __init__(
        self,
        *,
        ledger_repository: _LedgerRepository | None = None,
        pending_repository: _PendingRepository | None = None,
        run_repository: _RunLookup | None = None,
        notifier: ApprovalNotifier | None = None,
        suspend: SuspendFn | None = None,
    ) -> None:
        self._ledger = ledger_repository or HumanApprovalRepository()
        self._pending = pending_repository or PendingApprovalRepository()
        self._runs = run_repository or RunRepository()
        # None = 每次通知时回落到**当前**注册的通道：服务可能在 Discord 注册之前就被构造。
        self._notifier = notifier
        self._suspend = suspend or _default_suspend

    async def request_human_approval(
        self,
        *,
        run_id: str,
        wait_key: str,
        decision_type: ApprovalDecisionType,
        context_summary: str,
        context_ref: dict[str, Any],
        node_name: str,
        skill_id: str | None = None,
        thread_id: str | None = None,
        reason: str | None = None,
    ) -> Any:
        """阻塞式审批：落账 → 通知 → 挂起，返回 resume payload（docs/dev/22 第 3 节）。

        必须在挂载了 checkpointer 的图节点执行上下文里调用（`interrupt()` 的要求）。

        ⚠️ 恢复时节点整体重跑、本方法会被再调一次：两张表都按 wait_key 去重，Discord 卡片
        只在卡片**首次**插入时发送，第二次调用时 `interrupt()` 直接返回人工给出的 payload。

        `reason` 默认取 `decision_type.value`（正文口径）；已有挂起点（09/16）传入自己原来
        的 reason 字符串，保持日志与 `interrupt()` 载荷的连续性。
        """
        approval = await self._record(
            run_id=run_id,
            wait_key=wait_key,
            decision_type=decision_type,
            context_summary=context_summary,
            context_ref=context_ref,
            node_name=node_name,
            skill_id=skill_id,
            thread_id=thread_id,
            blocking=True,
        )
        return await self._suspend(reason=reason or decision_type.value, wait_key=approval.wait_key)

    async def notify_human(
        self,
        *,
        run_id: str,
        wait_key: str,
        decision_type: ApprovalDecisionType,
        context_summary: str,
        context_ref: dict[str, Any],
        node_name: str,
        skill_id: str | None = None,
        thread_id: str | None = None,
    ) -> PendingApproval:
        """非阻塞通知（docs/dev/22 第 8.1 节）：写一张 `blocking=False` 的卡片 + 发通知，不挂起。

        适用判据是项目级通用模式——**是否挂起，取决于"不等待人工确认能否继续产出有意义的
        结果"，而不是"问题是否重要"**。可以在任何上下文调用（不需要图节点）。
        """
        return await self._record(
            run_id=run_id,
            wait_key=wait_key,
            decision_type=decision_type,
            context_summary=context_summary,
            context_ref=context_ref,
            node_name=node_name,
            skill_id=skill_id,
            thread_id=thread_id,
            blocking=False,
        )

    async def _record(
        self,
        *,
        run_id: str,
        wait_key: str,
        decision_type: ApprovalDecisionType,
        context_summary: str,
        context_ref: dict[str, Any],
        node_name: str,
        skill_id: str | None,
        thread_id: str | None,
        blocking: bool,
    ) -> PendingApproval:
        resolved_thread_id = await self._resolve_thread_id(
            run_id=run_id, skill_id=skill_id, thread_id=thread_id
        )
        approval = PendingApproval(
            approval_id=approval_id_for(wait_key),
            run_id=run_id,
            wait_key=wait_key,
            decision_type=decision_type,
            context_summary=context_summary,
            context_ref=context_ref,
            node_name=node_name,
            thread_id=resolved_thread_id,
            blocking=blocking,
            created_at=datetime.now(UTC),
        )
        if blocking:
            # 账本先于卡片写：卡片存在而账本缺失时，人点了批准 resolve_suspension() 会把它
            # 当成"重复回调"静默忽略，图永远醒不过来；反过来（账本在、卡片缺）至少还能
            # 通过 HMAC 回调端点或重跑本方法补齐。
            await self._ledger.create(
                run_id=run_id,
                node_name=node_name,
                thread_id=resolved_thread_id,
                wait_key=wait_key,
            )
        inserted = await self._pending.save(approval)
        if inserted:
            await self._notify(approval)
        logger.warning(
            "human_approval_requested" if blocking else "human_notification_recorded",
            run_id=run_id,
            node_name=node_name,
            approval_id=approval.approval_id,
            decision_type=decision_type.value,
            wait_key=wait_key,
            thread_id=resolved_thread_id,
            first_time=inserted,
        )
        return approval

    async def _notify(self, approval: PendingApproval) -> None:
        """发送卡片通知；失败只记日志（通知是旁路，卡片已落库）。"""
        notifier = self._notifier or get_approval_notifier()
        try:
            await notifier.notify_approval(approval)
        except Exception as exc:  # noqa: BLE001 - 通知通道故障不得阻断审批挂起
            logger.error(
                "approval_notification_failed",
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                error=str(exc)[:500],
            )

    async def _resolve_thread_id(
        self, *, run_id: str, skill_id: str | None, thread_id: str | None
    ) -> str:
        """解析唤醒用的 LangGraph thread_id（口径说明见模块头）。"""
        if thread_id:
            return thread_id
        if skill_id:
            return thread_id_for(skill_id=skill_id, run_id=run_id)
        try:
            run = await self._runs.get(run_id)
        except Exception as exc:  # noqa: BLE001 - 查不到 run 不该让挂起本身失败
            logger.warning("approval_thread_lookup_failed", run_id=run_id, error=str(exc)[:200])
            run = None
        if run:
            return thread_id_for(skill_id=str(run["skill_id"]), run_id=run_id)
        logger.warning(
            "approval_thread_id_fallback_to_run_id",
            run_id=run_id,
            reason="未提供 skill_id 且 runs 表中无该运行记录，唤醒可能找不到主图 thread",
        )
        return run_id


_service: ApprovalService | None = None


def get_approval_service() -> ApprovalService:
    """进程级默认服务（惰性构造，避免 import 期就碰数据库配置）。"""
    global _service
    if _service is None:
        _service = ApprovalService()
    return _service


def set_approval_service(service: ApprovalService | None) -> None:
    """替换进程级默认服务（测试或嵌入方使用；传 None 恢复惰性默认）。"""
    global _service
    _service = service


async def request_human_approval(
    run_id: str,
    wait_key: str,
    decision_type: ApprovalDecisionType,
    context_summary: str,
    context_ref: dict[str, Any],
    *,
    node_name: str,
    skill_id: str | None = None,
    thread_id: str | None = None,
    reason: str | None = None,
) -> Any:
    """docs/dev/22 第 3 节正文签名的模块级入口，委托给进程级默认 `ApprovalService`。"""
    return await get_approval_service().request_human_approval(
        run_id=run_id,
        wait_key=wait_key,
        decision_type=decision_type,
        context_summary=context_summary,
        context_ref=context_ref,
        node_name=node_name,
        skill_id=skill_id,
        thread_id=thread_id,
        reason=reason,
    )


__all__ = [
    "ApprovalService",
    "get_approval_service",
    "request_human_approval",
    "set_approval_service",
]
