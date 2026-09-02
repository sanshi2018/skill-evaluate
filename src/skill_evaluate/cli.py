"""本地/CI 入口（`skill-evaluate run ...`，docs/dev/01 第 8 节）。"""

from __future__ import annotations

from pathlib import Path

import typer
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from skill_evaluate.logging import configure_logging, get_logger

app = typer.Typer(name="skill-evaluate")
logger = get_logger(node_name="cli")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


@app.command()
def run(skill_path: str, force_regenerate: bool = False) -> None:
    """对指定 SKILL.md 运行完整评测流水线（graph/ 由 docs/dev/24 接入）。"""
    raise NotImplementedError(
        "主图尚未装配：由 docs/dev/24（主图编排与 CI/CD 落地）接入，"
        f"届时将读取 {skill_path!r}（force_regenerate={force_regenerate}）并调用编译后的图。"
    )


@app.command()
def db_init() -> None:
    """初始化数据库 schema（docs/dev/04）：建扩展 + 跑 Alembic 迁移 + 调用 PostgresSaver.setup()。

    保证"拉起一个空容器到可跑评测"只需一条命令（docs/dev/04 第 7 节）。
    """
    configure_logging()

    from skill_evaluate.persistence.checkpointer import build_checkpointer

    logger.info("db_init_start")

    alembic_cfg = AlembicConfig(str(_ALEMBIC_INI))
    alembic_cfg.set_main_option(
        "script_location", str(_REPO_ROOT / "src" / "skill_evaluate" / "persistence" / "migrations")
    )
    alembic_command.upgrade(alembic_cfg, "head")
    logger.info("db_init_alembic_upgraded")

    with build_checkpointer():
        pass
    logger.info("db_init_checkpointer_ready")

    logger.info("db_init_complete")


if __name__ == "__main__":
    app()
