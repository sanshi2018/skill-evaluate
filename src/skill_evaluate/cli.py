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
def lint(
    skill_path: str = typer.Option(..., "--skill-path", help="SKILL.md 文件或其所在目录"),
) -> None:
    """只跑模块二的**纯代码**扫描：行数/Token 卡线 + 渐进式披露初筛（docs/dev/12）。

    为什么单独开一条命令，而不是让 CI 去跑那条子图：架构文档模块二要求这部分能
    "类似传统的 Linter 运行模式"集成进 CI，而完整子图需要 Postgres（读被测 Skill、
    写维度结论）和 LLM（三项同行评审）。本命令**不碰数据库、不发任何请求**，
    在一个只 checkout 了代码的 CI 作业里就能跑。

    退出码即判定：硬性指标超标返回 1（阻断），其余返回 0。渐进式披露的初筛结果
    只打印不影响退出码——它是允许误报的启发式，真正的定性由子图里的 Mini Agent
    同行评审给出（docs/dev/12 第 6 节：硬性数字用 Error，主观判断用 Warning）。
    """
    configure_logging()

    from skill_evaluate.config import get_settings
    from skill_evaluate.ingestion import load_skill
    from skill_evaluate.nodes.context_scoping import (
        scan_progressive_disclosure,
        scan_static_metrics,
    )

    settings = get_settings().context_scoping
    skill = load_skill(skill_path)
    metrics = scan_static_metrics(
        skill,
        line_limit=settings.line_limit,
        token_limit=settings.token_limit,
        estimate_uncertainty_ratio=settings.estimate_uncertainty_ratio,
    )
    scan = scan_progressive_disclosure(
        skill,
        line_limit=settings.line_limit,
        token_limit=settings.token_limit,
        bulk_inline_ratio=settings.bulk_inline_ratio,
        token_count=metrics.token_count,
    )

    typer.echo(
        f"{skill.skill_id}：{metrics.line_count}/{metrics.line_limit} 行，"
        f"{metrics.token_count}/{metrics.token_limit} Token"
        f"（计数口径 {metrics.token_count_method}"
        f"{'' if metrics.token_count_exact else '，估算值'}）"
    )
    if scan.bulk_inline_without_references:
        typer.secho(
            "warning: 正文体量接近限额但没有 references/ 参考文件，疑似未做渐进式披露。",
            fg=typer.colors.YELLOW,
        )
    for candidate in scan.candidates:
        typer.secho(
            f"warning: {candidate.path} 疑似缺少按需加载触发条件（{candidate.reason}）。",
            fg=typer.colors.YELLOW,
        )

    if metrics.needs_human_confirmation:
        # 估算值判超标：不返回 1。用一个 ±15% 的估算值阻断合并，是
        # docs/dev/interfaces/06 明确警告过的误判来源。
        typer.secho(
            f"warning: Token 数 {metrics.token_count} 超过限额，但本次是估算值，"
            "需人工确认（安装 tiktoken 可获得离线精确计数）。",
            fg=typer.colors.YELLOW,
        )
    if metrics.hard_fail:
        typer.secho("error: 硬性指标超标，请精简正文或拆分到 references/。", fg=typer.colors.RED)
        raise typer.Exit(code=1)


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
