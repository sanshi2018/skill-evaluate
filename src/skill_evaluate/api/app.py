"""FastAPI app 工厂（docs/dev/05 第 2 节）。承载 Hook 端点、人工审批回调与审查工作台 API。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from skill_evaluate.api.hooks_approval import router as hooks_approval_router
from skill_evaluate.api.hooks_hermes import router as hooks_hermes_router
from skill_evaluate.api.hooks_llama import router as hooks_llama_router
from skill_evaluate.logging import configure_logging
from skill_evaluate.observability.discord_notifier import configure_notification_channels


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    # docs/dev/22：进程启动时按配置注册 Discord 审批卡片 / 告警通道（未配置则保留日志默认实现）。
    # 放在 lifespan 而不是模块导入期：测试导入 `app` 时不应该读取 Webhook 配置。
    configure_notification_channels()
    yield


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="skill-evaluate hooks API", lifespan=_lifespan)
    app.include_router(hooks_hermes_router)
    # docs/dev/19：备用代理 llama_control 的 callback 模式唤醒入口
    app.include_router(hooks_llama_router)
    # docs/dev/22：审查工作台 API（/api/approvals、/api/suggestions）+ HMAC 审批回调
    app.include_router(hooks_approval_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
