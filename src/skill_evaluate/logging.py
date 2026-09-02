"""结构化日志初始化（`structlog`）。

约定（见 docs/dev/01 第 6 节、docs/dev/05 第 5 节）：
- CI 环境（`env != "local"`）输出 JSON；本地输出彩色控制台。
- 所有 Agent 调用、LangGraph 节点进入/退出必须打点 `run_id`、`skill_id`、`node_name`。
- 禁止在日志中打印完整 SKILL.md 正文/Prompt/API Key，敏感字段经
  `observability.log_sanitize` 脱敏后再落日志（该模块由 05 提供，本文件只负责
  底层 structlog 配置，不做业务级脱敏）。
"""

from __future__ import annotations

import logging
import sys

import structlog

from skill_evaluate.config import get_settings

_CONFIGURED = False


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    is_local = settings.env == "local"

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: structlog.types.Processor
    if is_local:
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(logging.INFO)

    _CONFIGURED = True


def get_logger(**initial_context: object) -> structlog.stdlib.BoundLogger:
    """获取一个绑定了初始上下文（如 run_id/skill_id/node_name）的 logger。"""
    configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger().bind(**initial_context)
    return logger
