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

from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver

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
