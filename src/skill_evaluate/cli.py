"""本地/CI 入口（`skill-evaluate run ...`，docs/dev/01 第 8 节）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import typer
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from skill_evaluate.logging import configure_logging, get_logger

app = typer.Typer(name="skill-evaluate")
logger = get_logger(node_name="cli")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


def _configure_notifications() -> None:
    """按配置注册 Discord 审批卡片 / 告警通道（docs/dev/22 第 4 节）。

    出题命令可能触发 docs/dev/21 的"连续坍塌"告警；不注册的话告警只进本地日志，
    在 CI 里跑的出题任务等于没人收到。延迟导入：不需要通知的命令不必加载 httpx 等依赖。
    """
    from skill_evaluate.observability.discord_notifier import configure_notification_channels

    configure_notification_channels()


@app.command()
def run(
    skill_path: str = typer.Option(..., "--skill-path", help="SKILL.md 文件或其所在目录"),
    force_regenerate: bool = typer.Option(
        False,
        "--force-regenerate",
        help="评测前强制全量重新出题（旧版本保留为历史）。只有人能触发；CI 默认路径不带它。",
    ),
    run_id: str | None = typer.Option(
        None,
        "--run-id",
        help="指定 run_id。线程已存在时从断点续跑/重新观察，而不是新开一次评测。",
    ),
    wait_timeout: int | None = typer.Option(
        None,
        "--wait-timeout",
        help="挂起后等待其他进程（API 进程的 GraphResumer）继续跑完的秒数；0 = 不等。"
        "默认取 SKILLEVAL_PIPELINE_WAIT_TIMEOUT_S。",
    ),
    report_dir: str | None = typer.Option(
        None, "--report-dir", help="benchmark.json / report.html 输出目录（默认取配置）"
    ),
) -> None:
    """对指定 Skill 运行完整评测流水线（docs/dev/24）。

    退出码：0 通过 / 1 有阻断项 / 2 评测系统自身错误 / 3 等待超时（仍挂起在回调或审批上）/
    4 流水线被停下（人工放弃或基础设施不可信）。详见 `graph/runner.py` 模块头。
    """
    _run_pipeline_command(
        skill_path,
        mode="full",
        force_regenerate=force_regenerate,
        run_id=run_id,
        wait_timeout=wait_timeout,
        report_dir=report_dir,
    )


def _run_pipeline_command(
    skill_path: str,
    *,
    mode: str,
    force_regenerate: bool,
    run_id: str | None,
    wait_timeout: int | None,
    report_dir: str | None,
) -> None:
    """`run` 与 `internal run-cold-suite` 共用：装配主图、发起运行、打印结论、按结论退出。"""
    configure_logging()
    # docs/dev/22 第 2 节第 7 条：图执行进程也要注册通知通道，否则挂起时的 Discord 卡片只写日志。
    _configure_notifications()

    from skill_evaluate.errors import SkillEvaluateError
    from skill_evaluate.graph.runner import EXIT_SYSTEM_ERROR, run_pipeline

    try:
        outcome = asyncio.run(
            run_pipeline(
                skill_path,
                mode=mode,  # type: ignore[arg-type]
                force_regenerate=force_regenerate,
                run_id=run_id,
                wait_timeout_s=wait_timeout,
                report_dir=report_dir,
            )
        )
    except SkillEvaluateError as exc:
        typer.secho(f"评测系统错误（与 Skill 质量无关）：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_SYSTEM_ERROR) from exc

    typer.echo(f"run_id={outcome.run_id} thread_id={outcome.thread_id} status={outcome.status}")
    if outcome.status == "completed":
        from skill_evaluate.graph.state import KEY_PULL_REQUEST, KEY_REPORT_OVERALL_STATUS

        typer.echo(f"overall_status={outcome.values.get(KEY_REPORT_OVERALL_STATUS)} blocking={outcome.blocking}")
        pull_request = outcome.values.get(KEY_PULL_REQUEST) or {}
        if pull_request.get("url"):
            typer.echo(f"auto-fix PR: {pull_request['url']}")
    elif outcome.status == "suspended":
        typer.secho(
            "流水线仍挂起，等待以下外部事件（审查工作台 / Hook 回调）："
            + ", ".join(outcome.pending_wait_keys or ["<无中断载荷>"]),
            fg=typer.colors.YELLOW,
        )
        typer.echo(f"处理完后可用 `skill-evaluate run --skill-path {skill_path} --run-id {outcome.run_id}` 继续观察。")
    else:
        typer.secho(f"流水线已停下：{outcome.stop_reason}", fg=typer.colors.RED)
    raise typer.Exit(code=outcome.exit_code())


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
    _configure_notifications()

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
def generate_attacks(
    skill_path: str = typer.Option(..., "--skill-path", help="SKILL.md 文件或其所在目录"),
    force: bool = typer.Option(
        False,
        "--force",
        help="强制重新生成对抗用例（旧版本保留为历史，不删除）。不加此参数时，"
        "已有对抗用例一律复用，不会调用任何 LLM。",
    ),
    count: int | None = typer.Option(
        None,
        "--count",
        help="出多少条对抗题。不给则按已注册的攻击面数量自动算（当前 7 × 2 = 14），"
        "将来新增攻击面时会自动跟上。",
    ),
) -> None:
    """生成或复用一份 Skill 的**对抗**测试集（模块五 / docs/dev/15 第 2 节）。

    与 `generate` 的分工：那个出正/反向功能用例（模块一），本命令出红队用例。
    分成两条命令而不是加一个 `--adversarial` 开关，是因为两者的更新节奏不同——
    新增一类攻击手法不该让触发准确度的历史分数失去可比性，反过来也一样。

    **`--force` 只重出对抗题**，正/反向用例原样继承（走 `INCREMENTAL_PATCH`，
    旧版本保留为非 active，历史结论仍可回查）。与 `generate --force` 一样，
    **CI 默认调用路径不带 `--force`**：必须由人显式加上。
    """
    configure_logging()
    _configure_notifications()

    from skill_evaluate.agents.attacker import AttackerService, default_adversarial_count
    from skill_evaluate.ingestion import load_skill

    skill = load_skill(skill_path)
    requested = default_adversarial_count() if count is None else count
    logger.info(
        "generate_attacks_start",
        skill_id=skill.skill_id,
        version_ref=skill.version_ref,
        force=force,
        requested_count=requested,
    )

    service = AttackerService()

    async def _run() -> None:
        if force:
            version = await service.force_regenerate(skill, count=requested)
            typer.echo(
                f"已强制重新生成对抗用例：suite_version_id={version.suite_version_id} "
                f"（用例集共 {len(version.case_ids)} 条）"
            )
            return

        result = await service.ensure_adversarial_suite(skill, count=requested)
        if result.staleness_warning:
            typer.secho(result.staleness_warning, fg=typer.colors.YELLOW)
        action = "已生成对抗用例" if result.generated else "复用已有对抗用例"
        typer.echo(
            f"{action}：suite_version_id={result.suite_version.suite_version_id} "
            f"（用例集共 {len(result.suite_version.case_ids)} 条）"
        )

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
        # docs/dev/23 第 3.1 节：把 manifest 的 description + keywords 索引进记忆库，
        # Validator 的 `_semantic_lookup()` 才检索得到（正文里的 `sync_assertion_toolbox` 子命令
        # 并入本命令实现，避免"同步了仓库却忘了建索引"）。
        await _sync_memory_collection("assertion_templates", toolbox=toolbox)

    asyncio.run(_run())


@app.command()
def sync_seed_anchors() -> None:
    """同步种子锚点库到本地缓存目录（docs/dev/21 第 3.1 节，同步模式照抄 sync-toolbox）。

    同样是**独立命令**：出题过程中不隐式拉外部仓库，CI 在跑评测之前单独执行本命令。
    """
    configure_logging()

    from skill_evaluate.agents.generator.seed_anchors import SeedAnchorLibrary
    from skill_evaluate.config import get_settings

    settings = get_settings().generator_trust
    if not settings.seed_repo_url:
        typer.secho(
            "未配置 SKILLEVAL_GENERATOR_TRUST_SEED_REPO_URL：出题将不注入真实种子锚点"
            "（这是合法状态，不是错误）。",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    library = SeedAnchorLibrary()

    async def _run() -> None:
        if not await library.sync():
            typer.secho(f"同步失败：{settings.seed_repo_url}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        typer.echo(
            f"已同步到 {library.root}（commit={library.commit_sha()}，"
            f"{len(library.anchors())} 条锚点）"
        )
        # docs/dev/23 第 3.2 节：锚点索引进 `search_documents(collection="seed_anchors")`，
        # `SeedAnchorResolver` 才会走混合检索（否则回落到进程内单一 embedding 相似度）。
        await _sync_memory_collection("seed_anchors", library=library)

    asyncio.run(_run())


async def _sync_memory_collection(
    collection: str,
    *,
    toolbox: Any | None = None,
    library: Any | None = None,
) -> None:
    """把本地缓存的外部仓库全量同步进记忆库（docs/dev/23）。`sync-*` 与 `memory-index` 共用。

    记忆库关闭时只提示不报错；索引失败（数据库 / embedding 通道故障）以退出码 1 结束——仓库
    本身可能已同步成功，但检索侧仍停在旧索引，CI 必须看得见。
    """
    from skill_evaluate.config import get_settings

    if not get_settings().memory.enabled:
        typer.secho(
            f"SKILLEVAL_MEMORY_ENABLED=false：跳过 {collection} 索引（检索将走降级路径）。",
            fg=typer.colors.YELLOW,
        )
        return

    from skill_evaluate.memory.hybrid_search import get_default_hybrid_search
    from skill_evaluate.memory.indexers import sync_assertion_template_index, sync_seed_anchor_index

    search = get_default_hybrid_search()
    try:
        if collection == "assertion_templates":
            assert toolbox is not None, "assertion_templates 需要传入 toolbox"
            stats = await sync_assertion_template_index(toolbox, search)
        else:
            assert library is not None, "seed_anchors 需要传入 library"
            stats = await sync_seed_anchor_index(library, search)
    except Exception as exc:  # CLI 边界：把任何故障转成可读信息与退出码
        typer.secho(f"{collection} 索引失败：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    if stats is None:
        typer.secho(f"{collection}：本地缓存不可用，未改动已有索引。", fg=typer.colors.YELLOW)
        return
    typer.echo(
        f"{collection} 索引完成：新写入 {stats.indexed}，未变化 {stats.skipped}，删除 {stats.deleted}"
    )


@app.command()
def memory_index(
    collection: str = typer.Option(
        "all",
        "--collection",
        help="要重建索引的集合：assertion_templates | seed_anchors | all",
    ),
) -> None:
    """按**本地缓存**重建记忆库索引，不拉取远程仓库（docs/dev/23 第 3.1、3.2 节）。

    用于数据库重建之后、换了 embedding 模型之后、或无网络但本地已有缓存的环境。
    `successful_skill_archive` / `optimizer_patch_history` 是只增不删的历史归档，没有"源"可以
    重建，不在本命令范围内。
    """
    configure_logging()

    from skill_evaluate.agents.generator.seed_anchors import SeedAnchorLibrary
    from skill_evaluate.agents.validator import AssertionToolbox

    valid = {"assertion_templates", "seed_anchors", "all"}
    if collection not in valid:
        typer.secho(f"未知集合 {collection!r}，可选：{sorted(valid)}", fg=typer.colors.RED)
        raise typer.Exit(code=2)

    async def _run() -> None:
        if collection in ("assertion_templates", "all"):
            await _sync_memory_collection("assertion_templates", toolbox=AssertionToolbox())
        if collection in ("seed_anchors", "all"):
            await _sync_memory_collection("seed_anchors", library=SeedAnchorLibrary())

    asyncio.run(_run())


@app.command()
def preflight_fingerprint(
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="把探测到的指纹写到该文件（用于首次生成/更新 golden_fingerprint.json）。"
            "不传则只打印，并与当前黄金指纹比对。",
        ),
    ] = None,
) -> None:
    """在目标沙箱里探测当前环境指纹（docs/dev/21 第 4 节）。

    黄金指纹**只能由人**通过本命令生成并在代码评审里确认后提交——流水线里不存在自动更新它的
    路径，"不允许静默漂移"正是指纹门禁的价值。不需要图上下文：探测走同步的
    `run_environment_probe()`，不经 Hook 挂起。
    """
    configure_logging()

    from skill_evaluate.config import get_settings
    from skill_evaluate.errors import ConfigurationError, ExecutorBackendError
    from skill_evaluate.nodes.preflight import (
        PreflightDeps,
        diff_fingerprint,
        dump_fingerprint,
        load_golden_fingerprint,
        probe_current_fingerprint,
    )

    settings = get_settings().preflight

    async def _run() -> None:
        try:
            fingerprint = await probe_current_fingerprint(
                PreflightDeps().probe_runner(), timeout_s=settings.fingerprint_probe_timeout_s
            )
        except (ExecutorBackendError, ConfigurationError) as exc:
            typer.secho(f"指纹探测失败：{exc}", fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc

        rendered = dump_fingerprint(fingerprint)
        if output is not None:
            output.write_text(rendered, encoding="utf-8")
            typer.echo(f"已写入 {output}，请人工审核 `git diff` 后提交。")
            return

        typer.echo(rendered)
        golden = load_golden_fingerprint(settings.golden_fingerprint_path)
        if golden is None:
            typer.secho(f"未找到黄金指纹 {settings.golden_fingerprint_path}", fg=typer.colors.YELLOW)
            return
        mismatches = diff_fingerprint(fingerprint, golden)
        if mismatches:
            typer.secho("与黄金指纹不一致：", fg=typer.colors.RED)
            for line in mismatches:
                typer.echo(f"  - {line}")
            raise typer.Exit(code=1)
        typer.secho("与黄金指纹一致。", fg=typer.colors.GREEN)

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


# --------------------------------------------------------------------------- #
# docs/dev/24：运维巡检与 CI 专用子命令（`skill-evaluate internal ...`）
# --------------------------------------------------------------------------- #

internal_app = typer.Typer(
    name="internal",
    help="运维巡检与 CI 专用子命令（定时任务 / workflow 调用，不面向日常使用）。",
)
app.add_typer(internal_app, name="internal")


@internal_app.command("reap-pending-hooks")
def internal_reap_pending_hooks(
    older_than: int | None = typer.Option(
        None,
        "--older-than",
        help="waiting 超过多少秒视为超时（默认取 SKILLEVAL_EXECUTOR_SANDBOX_WALL_CLOCK_TIMEOUT_S）",
    ),
) -> None:
    """巡检超时未回调的 pending_hooks 并以保守失败态唤醒（docs/dev/04 第 5 节，每 5 分钟一次）。

    不依赖某次具体运行的图状态，因此是独立定时任务而不是主图节点（docs/dev/24 第 4 节）。
    """
    configure_logging()
    _configure_notifications()

    from skill_evaluate.config import get_settings
    from skill_evaluate.graph.runner import reap_pending_hooks_with_graph

    threshold = older_than if older_than is not None else get_settings().executor.sandbox_wall_clock_timeout_s
    count = asyncio.run(reap_pending_hooks_with_graph(threshold))
    typer.echo(f"reaped={count}")


@internal_app.command("judge-health-check")
def internal_judge_health_check(
    window_size: int = typer.Option(50, "--window-size", help="黄金基准滑动窗口大小"),
) -> None:
    """检查默认 Judge 配置的黄金基准失误率（docs/dev/08 第 3.3 节 `check_judge_health()`，每 6 小时一次）。

    超阈值时 `JudgeHealthMonitor` 会冻结该配置并经告警通道发 `judge_frozen`；本命令以退出码 1 让定时
    作业显示为失败，值班的人在 Actions 页面也能看见。
    """
    configure_logging()
    _configure_notifications()

    from skill_evaluate.agents.judge.health import check_judge_health

    healthy = asyncio.run(check_judge_health(window_size=window_size))
    typer.echo(f"judge_healthy={healthy}")
    if not healthy:
        raise typer.Exit(code=1)


@internal_app.command("run-cold-suite")
def internal_run_cold_suite(
    skill_path: str = typer.Option(..., "--skill-path", help="SKILL.md 文件或其所在目录"),
    wait_timeout: int | None = typer.Option(None, "--wait-timeout"),
    report_dir: str | None = typer.Option(None, "--report-dir"),
) -> None:
    """Nightly：只重跑被模块七降级为 COLD 的用例（docs/dev/24 第 6 节；interfaces/17 第 7 节第 5 条）。

    与 `run` 共用同一张主图（`_pipeline_mode=cold_suite`），同样先过前置门禁。结论维度
    `cold_suite_regression` 不阻断合并，因此退出码只在评测系统故障 / 被停下 / 等待超时时非 0。
    """
    _run_pipeline_command(
        skill_path,
        mode="cold_suite",
        force_regenerate=False,
        run_id=None,
        wait_timeout=wait_timeout,
        report_dir=report_dir,
    )


@internal_app.command("changed-skills")
def internal_changed_skills(
    base: str = typer.Option(..., "--base", help="对比基线（PR 的 base sha）"),
    head: str = typer.Option("HEAD", "--head", help="对比终点（PR 的 head sha）"),
    skills_root: str = typer.Option("skills", "--skills-root", help="Skill 目录根（相对仓库根）"),
    all_skills: bool = typer.Option(False, "--all", help="忽略 diff，列出全部 Skill（Nightly 用）"),
) -> None:
    """输出 JSON：`{"skills": [...], "base_image_changed": bool}`，供 workflow 写进步骤输出。

    不加载任何 LLM/数据库依赖，可以在安装完包的最早一步运行。
    """
    import json
    import subprocess

    from skill_evaluate.graph.ci_support import (
        list_all_skills,
        resolve_changed_skills,
        touches_base_image,
    )

    repo_root = Path.cwd()
    if all_skills:
        typer.echo(json.dumps({"skills": list_all_skills(repo_root=repo_root, skills_root=skills_root), "base_image_changed": False}))
        return
    # 三点 diff：只看 head 相对合并基线的改动，base 分支上别人后来合入的提交不算本 PR 的改动。
    result = subprocess.run(  # 固定参数列表，无 shell
        ["git", "diff", "--name-only", f"{base}...{head}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        typer.secho(f"git diff 失败：{result.stderr.strip()}", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    files = result.stdout.splitlines()
    payload = {
        "skills": resolve_changed_skills(files, repo_root=repo_root, skills_root=skills_root),
        "base_image_changed": touches_base_image(files),
    }
    typer.echo(json.dumps(payload))


if __name__ == "__main__":
    app()
