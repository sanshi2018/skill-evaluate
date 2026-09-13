"""主图编排层节点：入口登记、收尾报告、补丁转 PR、数据飞轮归档、Nightly COLD 回归（docs/dev/24）。

这些节点不属于任何评测维度——它们不做判定，只负责"让十个维度有东西可测"与"把结论交付出去"。

## 节点签名一律写 `MainGraphState`

LangGraph 按节点函数第一个参数的类型注解推导输入 schema 并据此裁剪状态（interfaces/11 第 2 节）。
收尾节点要读前置门禁、模块一/三/五/十的私有键（staleness 告警、补丁 id、工作副本……），写成
`PipelineState` 的话这些键在进入节点前就被裁掉了。

## 收尾节点的失败语义

`finalize.report` 之后的两个节点（`patch_pr` / `rag_archive`）**从不让流水线失败**：此时全部维度结论
已落库、报告已写出，一次 `gh` 鉴权过期或 embedding 通道抖动只影响"交付物"，不影响"评测结论"。
失败原因写进状态与报告尾部，而不是吞掉。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skill_evaluate.agents.generator.service import TestSuiteService
from skill_evaluate.config import get_settings
from skill_evaluate.errors import ConfigurationError, ObservabilityError
from skill_evaluate.graph.cold_suite import ColdSuiteRegression
from skill_evaluate.graph.patch_pr import PatchToPullRequest
from skill_evaluate.graph.state import (
    KEY_ARCHIVE_OUTCOME,
    KEY_MODE,
    KEY_PULL_REQUEST,
    KEY_REPORT_BLOCKING,
    KEY_REPORT_OVERALL_STATUS,
    KEY_REPORT_PATHS,
    KEY_SKILL_PATH,
    MODE_COLD_SUITE,
    NODE_PREFIX_FINALIZE,
    NODE_PREFIX_PIPELINE,
    MainGraphState,
)
from skill_evaluate.ingestion import load_skill
from skill_evaluate.logging import get_logger
from skill_evaluate.memory.rag_archive import archive_successful_run
from skill_evaluate.nodes.instruction_control import state as ic_state
from skill_evaluate.nodes.multi_skill import state as multi_skill_state
from skill_evaluate.nodes.preflight import KEY_CANARY_OUTCOME, KEY_FINGERPRINT_OUTCOME
from skill_evaluate.nodes.security import state as sec_state
from skill_evaluate.nodes.trigger_accuracy import state as trigger_state
from skill_evaluate.observability.langfuse_adapter import LangfuseAdapter
from skill_evaluate.observability.report_generator import ReportGenerator
from skill_evaluate.observability.report_schema import BenchmarkReport
from skill_evaluate.persistence.repository import RunRepository, SkillRepository
from skill_evaluate.state.enums import GenerationMode
from skill_evaluate.state.memory import ArchiveOutcome

logger = get_logger(component="main_graph")

NODE_NAMES = {
    "bootstrap_run": f"{NODE_PREFIX_PIPELINE}.bootstrap_run",
    "report": f"{NODE_PREFIX_FINALIZE}.report",
    "patch_pr": f"{NODE_PREFIX_FINALIZE}.patch_pr",
    "rag_archive": f"{NODE_PREFIX_FINALIZE}.rag_archive",
}

REPORT_JSON_NAME = "benchmark.json"
REPORT_HTML_NAME = "report.html"

# 触发出题/改用例集的维度都会带回一条 staleness 告警（同一份用例集，语义相同）；报告只放一条，
# 按"最先产出测试集的维度"优先（interfaces/11 第 3.2 节、13 第 3.3 节、15 第 3.4 节：任选其一透传）。
STALENESS_WARNING_KEYS: tuple[str, ...] = (
    trigger_state.KEY_SUITE_STALENESS_WARNING,
    sec_state.KEY_SUITE_STALENESS_WARNING,
    ic_state.KEY_SUITE_STALENESS_WARNING,
    multi_skill_state.KEY_SUITE_STALENESS_WARNING,
)

ArchiveFn = Callable[[str], Awaitable[ArchiveOutcome]]


@dataclass(slots=True)
class PipelineDeps:
    """编排层节点的依赖注入点。全部有生产默认值；测试按需替换。"""

    skill_repository: SkillRepository = field(default_factory=SkillRepository)
    run_repository: RunRepository = field(default_factory=RunRepository)
    report_generator: ReportGenerator | None = None
    test_suite_service: TestSuiteService | None = None
    patch_to_pr: PatchToPullRequest | None = None
    archive_fn: ArchiveFn = archive_successful_run
    langfuse_adapter: LangfuseAdapter | None = None
    # None = 读 `PipelineSettings.report_dir`。
    report_dir: str | None = None

    def reporter(self) -> ReportGenerator:
        if self.report_generator is None:
            self.report_generator = ReportGenerator()
        return self.report_generator

    def suite_service(self) -> TestSuiteService:
        if self.test_suite_service is None:
            self.test_suite_service = TestSuiteService()
        return self.test_suite_service

    def pr_converter(self) -> PatchToPullRequest:
        if self.patch_to_pr is None:
            self.patch_to_pr = PatchToPullRequest(
                skill_repository=self.skill_repository, run_repository=self.run_repository
            )
        return self.patch_to_pr

    def langfuse(self) -> LangfuseAdapter:
        if self.langfuse_adapter is None:
            self.langfuse_adapter = LangfuseAdapter()
        return self.langfuse_adapter

    def output_dir(self) -> str:
        return self.report_dir if self.report_dir is not None else get_settings().pipeline.report_dir


# --------------------------------------------------------------------------- #
# 报告写出（节点与 CLI 共用）
# --------------------------------------------------------------------------- #


def preflight_summary_from(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """报告头部的"运行环境证明"摘要（interfaces/21 第 3.1 节建议写进报告头部）。"""
    fingerprint = state.get(KEY_FINGERPRINT_OUTCOME)
    canary = state.get(KEY_CANARY_OUTCOME)
    if fingerprint is None and canary is None:
        return None
    return {"sandbox_fingerprint": fingerprint, "canary_probe": canary}


def staleness_warning_from(state: Mapping[str, Any]) -> str | None:
    for key in STALENESS_WARNING_KEYS:
        value = state.get(key)
        if value:
            return str(value)
    return None


async def write_report_files(
    reporter: ReportGenerator,
    state: Mapping[str, Any],
    out_dir: str,
) -> tuple[BenchmarkReport, dict[str, str]]:
    """从数据库聚合本次运行的维度结论，连同状态里的头尾信息写出 benchmark.json / report.html。

    `ReportGenerator.build()` 以库为唯一数据源，因此任何进程（跑完流水线的 API 进程、在一旁等待的
    CLI 进程）调用本函数得到的是同一份报告——CLI 据此在自己的工作目录重写一份，保证 CI 的
    `upload-artifact` 一定拿得到文件。
    """
    run_id = str(state["run_id"])
    report = await reporter.build(
        run_id,
        test_suite_staleness_warning=staleness_warning_from(state),
        preflight_summary=preflight_summary_from(state),
        pull_request=_as_dict(state.get(KEY_PULL_REQUEST)),
        archive_outcome=_as_dict(state.get(KEY_ARCHIVE_OUTCOME)),
    )
    directory = Path(out_dir)
    paths = {
        "json": str(directory / REPORT_JSON_NAME),
        "html": str(directory / REPORT_HTML_NAME),
    }
    reporter.to_json(report, paths["json"])
    reporter.to_html(report, paths["html"])
    return report, paths


def _as_dict(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #


class PipelineNodes:
    """编排层节点集合。做成类是为了让依赖只注入一次（与各维度的 `*Pipeline` 同一模式）。"""

    def __init__(
        self, deps: PipelineDeps | None = None, *, cold_suite: ColdSuiteRegression | None = None
    ) -> None:
        self.deps = deps or PipelineDeps()
        self.cold_suite = cold_suite

    async def bootstrap_run(self, state: MainGraphState) -> dict[str, object]:
        """入口：被测 Skill 入库 + 创建 runs 记录（+ 可选的强制重新出题）。

        为什么必须是图里的第一个节点而不是运行入口里的一段脚本：
        - 各维度都从 `SkillRepository` 读被测 Skill（interfaces/12 第 3 节），且 Hermes Hook 端点与
          审批服务都按 `runs` 表解析 thread_id（interfaces/21 第 3.4 节、22 第 2 节第 6 条）——金丝雀
          探针挂起前这两件事必须已经发生；
        - 放进图里就受 checkpoint 保护：任何进程（CLI、API、巡检）从断点恢复都不会重复执行，也不会
          被遗漏。三步都是幂等的（skills upsert、runs `ON CONFLICT DO NOTHING`）。
        """
        run_id = str(state["run_id"])
        skill_path = state.get(KEY_SKILL_PATH)
        if not skill_path:
            raise ConfigurationError(f"主图输入缺少 {KEY_SKILL_PATH}（run_id={run_id}）")

        skill = load_skill(str(skill_path))
        # 身份核对：runner 计算 thread_id 用的是它读到的 skill_id/version_ref；若两次读取之间
        # 文件被改（本地开发常见），继续跑会让 checkpoint 线程与 runs 记录指向两个不同的版本。
        if skill.skill_id != state.get("skill_id") or skill.version_ref != state.get("skill_version_ref"):
            raise ConfigurationError(
                f"Skill 身份与运行入参不一致：state={state.get('skill_id')}@{state.get('skill_version_ref')}，"
                f"磁盘上={skill.skill_id}@{skill.version_ref}。评测期间请勿修改被测 Skill，或以新的 run 重跑。"
            )

        await self.deps.skill_repository.save(skill)
        generation_mode = str(state.get("generation_mode") or GenerationMode.REUSE.value)
        await self.deps.run_repository.create(
            run_id=run_id,
            skill_id=skill.skill_id,
            skill_version_ref=skill.version_ref,
            generation_mode=generation_mode,
        )
        # 顶层 Langfuse trace（interfaces/05 第 1 节：由流水线入口产出一次）。未配置时是 no-op。
        self.deps.langfuse().start_run_trace(run_id, skill.skill_id)

        if generation_mode == GenerationMode.FORCE_REGENERATE.value and state.get(KEY_MODE) != MODE_COLD_SUITE:
            # 只有人能触发（CLI `run --force-regenerate` / CI 的 workflow_dispatch 输入），
            # 放在前置门禁之前：出题只依赖 LLM，不依赖沙箱。之后各维度走 REUSE 复用这一版。
            version = await self.deps.suite_service().force_regenerate(
                skill, triggered_by="pipeline_force_regenerate"
            )
            logger.info(
                "pipeline_force_regenerated_suite",
                run_id=run_id,
                suite_version_id=version.suite_version_id,
                case_count=len(version.case_ids),
            )

        logger.info(
            "pipeline_run_bootstrapped",
            run_id=run_id,
            skill_id=skill.skill_id,
            skill_version_ref=skill.version_ref,
            generation_mode=generation_mode,
            mode=state.get(KEY_MODE),
        )
        return {}

    async def finalize_report(self, state: MainGraphState) -> dict[str, object]:
        """Phase E 第一步：聚合全部维度结论并写出 benchmark.json / report.html（docs/dev/05）。

        本节点**会**让流水线失败（与后两个收尾节点不同）：报告就是这条流水线的产品，写不出来
        说明评测没有交付任何东西，CI 必须看得见。
        """
        report, paths = await write_report_files(self.deps.reporter(), state, self.deps.output_dir())
        logger.info(
            "pipeline_report_written",
            run_id=report.run_id,
            overall_status=report.overall_status.value,
            blocking=report.blocking,
            dimensions=len(report.dimensions),
            json_path=paths["json"],
        )
        return {
            KEY_REPORT_PATHS: paths,
            KEY_REPORT_OVERALL_STATUS: report.overall_status.value,
            KEY_REPORT_BLOCKING: report.blocking,
            "final_report_ref": paths["json"],
        }

    async def patch_to_pr(self, state: MainGraphState) -> dict[str, object]:
        """Phase E 第二步：已采纳补丁转 PR（docs/dev/24 第 5 节，实现见 `graph/patch_pr.py`）。"""
        settings = get_settings().pipeline
        paths = _as_dict(state.get(KEY_REPORT_PATHS)) or {}
        report_link = settings.report_url or str(paths.get("json", REPORT_JSON_NAME))
        outcome = await self.deps.pr_converter().run(state, report_link=report_link)
        logger.info(
            "pipeline_patch_pr_outcome",
            run_id=state.get("run_id"),
            status=outcome.status,
            url=outcome.url,
            reason=outcome.reason,
        )
        return {KEY_PULL_REQUEST: outcome.model_dump(mode="json")}

    async def rag_archive(self, state: MainGraphState) -> dict[str, object]:
        """Phase E 最后一步：全部 blocking 维度 PASS 时归档进数据飞轮（interfaces/23 第 3.1 节）。

        门槛判断在 `archive_successful_run()` 内部；这里只兜住基础设施故障。归档完成后**重写一次
        报告**，把 PR 结果与归档结果补进报告尾部——两者都发生在 `finalize.report` 之后。
        """
        run_id = str(state["run_id"])
        try:
            outcome = (await self.deps.archive_fn(run_id)).model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - 归档失败不得让已出的报告变成失败的流水线
            logger.warning("rag_archive_failed", run_id=run_id, error=str(exc)[:300])
            outcome = {
                "run_id": run_id,
                "archived": False,
                "reason": f"archive_error: {str(exc)[:300]}",
            }
        else:
            logger.info("rag_archive_outcome", **outcome)

        update: dict[str, object] = {KEY_ARCHIVE_OUTCOME: outcome}
        try:
            _, paths = await write_report_files(
                self.deps.reporter(), {**state, KEY_ARCHIVE_OUTCOME: outcome}, self.deps.output_dir()
            )
            update[KEY_REPORT_PATHS] = paths
        except (ObservabilityError, OSError) as exc:
            # 第一版报告已经写出，补写尾部失败只记日志。
            logger.warning("pipeline_report_rewrite_failed", run_id=run_id, error=str(exc)[:300])
        return update

    async def cold_suite_regression(self, state: MainGraphState) -> dict[str, object]:
        """Nightly 模式的唯一评测节点（实现见 `graph/cold_suite.py`）。"""
        if self.cold_suite is None:
            raise ConfigurationError("主图未装配 ColdSuiteRegression，无法以 cold_suite 模式运行")
        return await self.cold_suite.run(state)


__all__ = [
    "NODE_NAMES",
    "REPORT_HTML_NAME",
    "REPORT_JSON_NAME",
    "STALENESS_WARNING_KEYS",
    "PipelineDeps",
    "PipelineNodes",
    "preflight_summary_from",
    "staleness_warning_from",
    "write_report_files",
]
