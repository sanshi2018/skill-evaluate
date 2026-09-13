"""FastAPI app 工厂（docs/dev/05 第 2 节）。承载 Hook 端点、人工审批回调与审查工作台 API。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from skill_evaluate.api.hooks_approval import router as hooks_approval_router
from skill_evaluate.api.hooks_hermes import router as hooks_hermes_router
from skill_evaluate.api.hooks_llama import router as hooks_llama_router
from skill_evaluate.config import get_settings
from skill_evaluate.logging import configure_logging, get_logger
from skill_evaluate.observability.discord_notifier import configure_notification_channels

logger = get_logger(node_name="api_app")


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    # docs/dev/22：进程启动时按配置注册 Discord 审批卡片 / 告警通道（未配置则保留日志默认实现）。
    # 放在 lifespan 而不是模块导入期：测试导入 `app` 时不应该读取 Webhook 配置。
    configure_notification_channels()
    async with AsyncExitStack() as stack:
        if get_settings().pipeline.register_resumer_in_api:
            await _register_main_graph_resumer(stack)
        yield


async def _register_main_graph_resumer(stack: AsyncExitStack) -> None:
    """装配主图并注册 GraphResumer（interfaces/04_graph_resumer.md，docs/dev/24 接入）。

    Hermes / Llama 的 Hook 回调、审查工作台的审批决策都打到本进程，由这里持有的主图实例把挂起的线程
    继续跑下去。checkpointer 连接在应用整个生命周期内保持打开（`AsyncExitStack` 在关闭时释放）。

    **启动即失败**而不是记日志继续：数据库连不上时注册不了唤醒器，API 进程接住的每一个回调都会以
    500/503 失败——让部署在启动阶段就红，比上线后逐个回调报错更容易发现。确实不需要唤醒职责的部署
    设 `SKILLEVAL_PIPELINE_REGISTER_RESUMER_IN_API=false`。
    """
    # 延迟导入：主图会导入全部十个维度，只在真正需要唤醒职责时才付这个代价。
    from skill_evaluate.graph.main import build_main_graph
    from skill_evaluate.graph.resumer import CompiledGraphResumer
    from skill_evaluate.persistence.checkpointer import build_async_checkpointer
    from skill_evaluate.persistence.suspension import register_graph_resumer

    saver = await stack.enter_async_context(build_async_checkpointer())
    register_graph_resumer(CompiledGraphResumer(build_main_graph(saver)))
    logger.info("api_graph_resumer_registered")


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
