"""`PostgresSaver` 接线（docs/dev/04 第 2 节）。

`saver.setup()` 建的是 LangGraph 官方管理的 Checkpoint 表（`checkpoints`、
`checkpoint_writes` 等），与本包"业务表"（`models.py`）分离，不混用同一套迁移
工具管理——Checkpoint 表结构由 `langgraph-checkpoint-postgres` 库版本决定，
业务表由 Alembic 迁移管理。

`thread_id`（LangGraph 会话标识）约定取值 `f"{skill_id}:{run_id}"`，保证同一
Skill 的历次评测互不干扰，同时同一次 `run_id` 中断后可用相同 `thread_id`
精确恢复。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from skill_evaluate.config import get_settings


def thread_id_for(*, skill_id: str, run_id: str) -> str:
    return f"{skill_id}:{run_id}"


@contextmanager
def build_checkpointer() -> Iterator[PostgresSaver]:
    """构造并初始化 `PostgresSaver`。

    以上下文管理器形式暴露（`PostgresSaver.from_conn_string` 本身返回一个
    context manager），调用方在图编译时使用：

        with build_checkpointer() as checkpointer:
            graph = builder.compile(checkpointer=checkpointer, interrupt_before=[...])
    """
    settings = get_settings()
    with PostgresSaver.from_conn_string(settings.db.dsn) as saver:
        saver.setup()  # 幂等：首次运行建表，之后运行跳过
        yield saver


@asynccontextmanager
async def build_async_checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    """异步版 checkpointer（docs/dev/24 追加）。

    主图的全部节点都是 `async def`，运行入口一律走 `graph.ainvoke()`；而同步的
    `PostgresSaver` 没有实现 `aget_tuple()` / `aput()` 等异步方法，挂到 `ainvoke()` 上会在
    第一次写 checkpoint 时抛 `NotImplementedError`。因此 CLI `run`、API 进程的
    GraphResumer、定时巡检任务都用本函数；同步版 `build_checkpointer()` 保留给
    `db_init` 这类只需要建表的同步入口。

    两者写的是**同一套表**，`thread_id` 口径相同，因此 CLI 进程挂起的线程可以由 API
    进程用异步 saver 唤醒，反之亦然。
    """
    settings = get_settings()
    async with AsyncPostgresSaver.from_conn_string(settings.db.dsn) as saver:
        await saver.setup()  # 幂等
        yield saver
