"""`GraphResumer` 的正式实现（interfaces/04_graph_resumer.md，docs/dev/24 落地）。

`persistence/suspension.py::resolve_suspension()` 迁完 `pending_hooks` / `human_approvals` 账本后调用
本类，把 resume payload 送回已编译主图里挂起的那个节点。

## 相对接入文档示例的三处增强

1. **按 wait_key 定位中断 id**。主图里常有多个并行节点**同时**挂起（模块一与模块五各自在等 Hermes
   回调、审批卡片与 Hook 并存）。LangGraph 1.x 在存在多个待处理中断时，裸 `Command(resume=value)`
   会报错要求指定中断 id；而 `suspend_and_wait()` 把 `wait_key` 写进了中断载荷，这里据此构造
   `Command(resume={interrupt_id: value})`，只唤醒对应的那一个。
2. **同一线程的唤醒串行化**。两个 Hook 回调几乎同时到达时，两次 `ainvoke` 并发写同一个线程的
   checkpoint，后写的会基于过期的父 checkpoint 覆盖前者的结果。进程内按 thread_id 加锁；跨进程并发
   仍然存在（API 多副本部署时），届时应改为队列投递（接口不变，见 interfaces/04 注意事项）。
3. **可选后台执行**（interfaces/22 第 2 节第 9 条）：唤醒会在当前协程里把图一直跑到下一个挂起点或
   结束，可能远超 HTTP 网关超时。`background=True` 时投递为后台任务立即返回，错误只进日志。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from langgraph.types import Command

from skill_evaluate.config import get_settings
from skill_evaluate.logging import get_logger

logger = get_logger(component="graph_resumer")


def graph_config(thread_id: str, *, recursion_limit: int | None = None) -> dict[str, Any]:
    """主图调用统一使用的 config：thread_id 口径 `f"{skill_id}:{run_id}"` + 显式超步上限。"""
    limit = recursion_limit if recursion_limit is not None else get_settings().pipeline.recursion_limit
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": limit}


def pending_interrupts(snapshot: Any) -> list[Any]:
    """从 `StateSnapshot` 取出所有待处理中断（按任务展开，同一中断 id 只保留一次）。"""
    seen: dict[str, Any] = {}
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            seen.setdefault(str(item.id), item)
    return list(seen.values())


def pending_wait_keys(snapshot: Any) -> list[str]:
    """待处理中断里的 wait_key 列表（CLI 打印"卡在哪里"用）。"""
    keys: list[str] = []
    for item in pending_interrupts(snapshot):
        value = getattr(item, "value", None)
        if isinstance(value, Mapping) and value.get("wait_key"):
            keys.append(str(value["wait_key"]))
    return keys


class CompiledGraphResumer:
    """持有一个已编译主图，负责把外部事件送回挂起的节点。"""

    def __init__(self, graph: Any, *, background: bool | None = None) -> None:
        self._graph = graph
        self._background = (
            get_settings().pipeline.resume_in_background if background is None else background
        )
        self._locks: dict[str, asyncio.Lock] = {}
        # 后台任务必须持有强引用，否则可能在执行途中被垃圾回收（asyncio 文档的明确警告）。
        self._tasks: set[asyncio.Task[None]] = set()

    async def resume(
        self, *, thread_id: str, resume_payload: Any, wait_key: str | None = None
    ) -> None:
        if not self._background:
            await self._resume_serialized(thread_id, resume_payload, wait_key)
            return
        task = asyncio.create_task(self._resume_in_background(thread_id, resume_payload, wait_key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resume_in_background(
        self, thread_id: str, resume_payload: Any, wait_key: str | None
    ) -> None:
        try:
            await self._resume_serialized(thread_id, resume_payload, wait_key)
        except Exception as exc:  # noqa: BLE001 - 后台任务没有调用方可以接异常，只能如实记日志
            logger.error(
                "graph_resume_failed",
                thread_id=thread_id,
                wait_key=wait_key,
                error_type=type(exc).__name__,
                error=str(exc)[:1000],
            )

    async def _resume_serialized(
        self, thread_id: str, resume_payload: Any, wait_key: str | None
    ) -> None:
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            config = graph_config(thread_id)
            command = await self._build_command(config, resume_payload, wait_key)
            logger.info("graph_resume_start", thread_id=thread_id, wait_key=wait_key)
            await self._graph.ainvoke(command, config=config)
            logger.info("graph_resume_returned", thread_id=thread_id, wait_key=wait_key)

    async def _build_command(
        self, config: dict[str, Any], resume_payload: Any, wait_key: str | None
    ) -> Command[Any]:
        """有 wait_key 且能在待处理中断里找到它 → 按中断 id 唤醒；否则退回裸 resume。

        找不到时不报错直接退回：只有一个待处理中断时裸 resume 本来就是对的；存在多个却对不上时
        LangGraph 会自己抛出明确的错误，比这里猜一个更安全。
        """
        if wait_key:
            snapshot = await self._graph.aget_state(config)
            for item in pending_interrupts(snapshot):
                value = getattr(item, "value", None)
                if isinstance(value, Mapping) and value.get("wait_key") == wait_key:
                    return Command(resume={item.id: resume_payload})
            logger.warning(
                "graph_resume_wait_key_not_found",
                thread_id=config["configurable"]["thread_id"],
                wait_key=wait_key,
                pending=pending_wait_keys(snapshot),
            )
        return Command(resume=resume_payload)


__all__ = ["CompiledGraphResumer", "graph_config", "pending_interrupts", "pending_wait_keys"]
