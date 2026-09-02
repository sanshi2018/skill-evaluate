"""异步 SQLAlchemy engine/session 工厂（docs/dev/04 第 4 节）。

所有 Repository 方法为 async，统一走 `sqlalchemy.ext.asyncio` + `psycopg`
异步驱动，与 LangGraph 节点本身的异步执行模型保持一致，避免同步 DB 调用
阻塞事件循环。
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from skill_evaluate.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(settings.db.async_dsn, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


def new_session() -> AsyncSession:
    return get_sessionmaker()()
