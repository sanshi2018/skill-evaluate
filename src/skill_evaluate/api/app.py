"""FastAPI app 工厂（docs/dev/05 第 2 节）。承载 Hook 端点与人工审批回调端点。"""

from __future__ import annotations

from fastapi import FastAPI

from skill_evaluate.api.hooks_approval import router as hooks_approval_router
from skill_evaluate.api.hooks_hermes import router as hooks_hermes_router
from skill_evaluate.api.hooks_llama import router as hooks_llama_router
from skill_evaluate.logging import configure_logging


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="skill-evaluate hooks API")
    app.include_router(hooks_hermes_router)
    # docs/dev/19：备用代理 llama_control 的 callback 模式唤醒入口
    app.include_router(hooks_llama_router)
    app.include_router(hooks_approval_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
