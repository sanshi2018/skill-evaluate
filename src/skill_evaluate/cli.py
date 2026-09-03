"""本地/CI 入口（`skill-evaluate run ...`，docs/dev/01 第 8 节）。"""

from __future__ import annotations

import asyncio
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
def generate(
    skill_path: str = typer.Option(..., "--skill-path", help="SKILL.md 文件或其所在目录"),
    force: bool = typer.Option(
        False,
        "--force",
        help="强制全量重新生成测试集（旧版本保留为历史，不删除）。不加此参数时，"
        "已有测试集一律复用，不会调用任何 LLM。",
    ),
    positive_count: int = typer.Option(9, help="正向触发用例数量（架构建议 8-10）"),
    negative_count: int = typer.Option(9, help="反向近脱靶用例数量（架构建议 8-10）"),
) -> None:
    """生成或复用一份 Skill 的测试集（docs/dev/06 第 4.2 节）。

    **CI 默认调用路径不带 `--force`**：必须由人在 CI 触发参数或本地命令里显式加
    上，从物理层面防止"CI 每跑一次就重新出一套题"这种误用——那会让每次运行的
    分数失去可比性。
    """
    configure_logging()

    from skill_evaluate.agents.generator import GeneratorAgent, TestSuiteService
    from skill_evaluate.ingestion import load_skill

    skill = load_skill(skill_path)
    logger.info(
        "generate_start",
        skill_id=skill.skill_id,
        version_ref=skill.version_ref,
        force=force,
    )

    service = TestSuiteService(generator=GeneratorAgent())

    async def _run() -> None:
        if force:
            version = await service.force_regenerate(
                skill,
                triggered_by="manual_cli",
                positive_count=positive_count,
                negative_count=negative_count,
            )
            typer.echo(
                f"已强制重新生成：suite_version_id={version.suite_version_id} "
                f"（{len(version.case_ids)} 条用例）"
            )
            return

        result = await service.ensure_test_suite(skill)
        if result.staleness_warning:
            typer.secho(result.staleness_warning, fg=typer.colors.YELLOW)
        action = "首次生成" if result.generated else "复用已有测试集"
        typer.echo(
            f"{action}：suite_version_id={result.suite_version.suite_version_id} "
            f"（{len(result.suite_version.case_ids)} 条用例）"
        )

    # 说明：positive_count / negative_count 只在 --force 路径下生效——复用路径
    # 按定义不出题，改数量没有意义。
    asyncio.run(_run())


@app.command()
def sync_toolbox() -> None:
    """同步 Git 断言工具箱到本地缓存目录（docs/dev/10 第 3.3 节）。

    刻意做成一条**独立命令**而不是在 `plan_assertion()` 里隐式触发：评测过程中途
    去拉一次外部仓库，会让"这次评测用的是哪一版模板"变得不确定，也会把一次网络
    故障变成一次评测失败。CI 里应当在跑评测**之前**单独执行本命令。
    """
    configure_logging()

    from skill_evaluate.agents.validator import AssertionToolbox
    from skill_evaluate.config import get_settings

    settings = get_settings().validator
    if not settings.toolbox_repo_url:
        typer.secho(
            "未配置 SKILLEVAL_VALIDATOR_TOOLBOX_REPO_URL：Validator 将全部走 "
            "generated_from_scratch（这是合法状态，不是错误）。",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    toolbox = AssertionToolbox()

    async def _run() -> None:
        if not await toolbox.sync():
            typer.secho(f"同步失败：{settings.toolbox_repo_url}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        typer.echo(
            f"已同步到 {toolbox.root}（commit={toolbox.commit_sha()}，"
            f"{len(toolbox.manifest())} 个模板）"
        )

    asyncio.run(_run())


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
