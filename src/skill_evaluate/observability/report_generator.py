"""报告生成器：`benchmark.json` / HTML（docs/dev/05 第 3.2 节）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from skill_evaluate.errors import ObservabilityError
from skill_evaluate.observability.report_schema import BenchmarkReport, DimensionResult
from skill_evaluate.persistence.repository import (
    DimensionResultRepository,
    RunRepository,
    SecurityFindingRepository,
)
from skill_evaluate.state.enums import JudgeVerdictStatus, SeverityLevel

_TEMPLATE_DIR = Path(__file__).parent / "templates"

# 覆盖率类维度名 → `BenchmarkReport.coverage_summary` 的键（docs/dev/24 接入，interfaces/16 第 6 节
# 第 4 条、interfaces/18 第 8 节第 5 条）。三者的 `DimensionResult.score` 分别是：等权能力覆盖率、
# 组合覆盖率、加权覆盖率。写成显式映射而不是"凡是有 score 的维度都塞进来"：其余维度的 score
# 语义各不相同（或恒为 None），混进"覆盖率摘要"会让读者误读。维度名在这里写字面量而不是导入
# `nodes.*.state.DIMENSION`：observability 是底层包，反向依赖节点包会形成循环导入。
COVERAGE_DIMENSIONS: dict[str, str] = {
    "capability_coverage": "capability_coverage_ratio",
    "test_suite_health": "combinatorial_pair_coverage_ratio",
    "weighted_coverage": "weighted_coverage_ratio",
}


def summarize_coverage(dimensions: list[DimensionResult]) -> dict[str, float]:
    """从覆盖率类维度的 `score` 聚合 `coverage_summary`（docs/dev/24 接入）。

    `score is None` 的维度不写入：None 表示"这次没算出数"（维度会同时报 NEEDS_HUMAN_REVIEW），
    写成 0.0 会被读成"覆盖率为零"。
    """
    summary: dict[str, float] = {}
    for dimension in dimensions:
        key = COVERAGE_DIMENSIONS.get(dimension.dimension)
        if key is not None and dimension.score is not None:
            summary[key] = float(dimension.score)
    return summary


class ReportGenerator:
    def __init__(self) -> None:
        self._dimension_repo = DimensionResultRepository()
        self._run_repo = RunRepository()
        self._security_repo = SecurityFindingRepository()
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=select_autoescape(["html"]),
        )

    async def record_dimension_result(
        self,
        *,
        run_id: str,
        dimension: str,
        status: JudgeVerdictStatus,
        score: float | None,
        findings: list[str],
        blocking: bool,
    ) -> None:
        """docs/dev/05 第 6 节：各维度节点（docs/dev/11~20）在完成判定后调用本方法写入。"""
        await self._dimension_repo.save(
            run_id=run_id,
            dimension=dimension,
            status=status.value,
            score=score,
            findings=findings,
            blocking=blocking,
        )

    async def build(
        self,
        run_id: str,
        *,
        test_suite_staleness_warning: str | None = None,
        preflight_summary: dict[str, Any] | None = None,
        pull_request: dict[str, Any] | None = None,
        archive_outcome: dict[str, Any] | None = None,
    ) -> BenchmarkReport:
        """聚合一次 run 的全部维度结论。

        `test_suite_staleness_warning` 由主图入口节点（docs/dev/24）从
        `agents.generator.service.EnsureTestSuiteResult.staleness_warning` 透传：
        用例集与当前 SKILL.md 版本不匹配时，报告里必须能看到这件事，否则读报告的
        人不知道这份分数是拿旧题跑出来的（docs/dev/06 第 4.1 节）。

        `preflight_summary` / `pull_request` / `archive_outcome`（docs/dev/24 追加）：主图收尾
        节点从图状态透传的头尾信息，本方法只原样放进报告，不参与 overall_status 计算——
        它们描述的是"这次评测怎么跑的、跑完交付了什么"，不是 Skill 质量结论。
        """
        run = await self._run_repo.get(run_id)
        if run is None:
            raise ObservabilityError(f"未找到 run_id={run_id!r} 对应的运行记录，无法生成报告")

        raw_dimensions = await self._dimension_repo.list_by_run(run_id)
        dimensions = [
            DimensionResult(
                dimension=d["dimension"],
                status=JudgeVerdictStatus(str(d["status"])),
                score=d["score"],
                findings=d["findings"],
                blocking=d["blocking"],
            )
            for d in raw_dimensions
        ]

        overall_status = JudgeVerdictStatus.PASS
        if any(d.blocking and d.status == JudgeVerdictStatus.FAIL for d in dimensions):
            overall_status = JudgeVerdictStatus.FAIL
        elif any(d.status == JudgeVerdictStatus.NEEDS_HUMAN_REVIEW for d in dimensions):
            overall_status = JudgeVerdictStatus.NEEDS_HUMAN_REVIEW

        severity_summary_raw = await self._security_repo.summarize_by_severity(run["skill_id"])
        security_findings_summary = {
            SeverityLevel(k): v for k, v in severity_summary_raw.items() if k in set(SeverityLevel)
        }
        # summarize_by_severity 目前是全局统计（docs/dev/15 落库后按 run 维度精细化，
        # 见 docs/dev/interfaces/05_security_findings_summary.md）。

        return BenchmarkReport(
            run_id=run_id,
            skill_id=run["skill_id"],
            skill_version_ref=run["skill_version_ref"],
            generated_at=datetime.now(UTC),
            overall_status=overall_status,
            dimensions=dimensions,
            suite_version_id=run["suite_version_id"] or "",
            security_findings_summary=security_findings_summary,
            coverage_summary=summarize_coverage(dimensions),
            test_suite_staleness_warning=test_suite_staleness_warning,
            preflight_summary=preflight_summary,
            pull_request=pull_request,
            archive_outcome=archive_outcome,
        )

    def to_json(self, report: BenchmarkReport, out_path: str) -> None:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(
            json.dumps(json.loads(report.model_dump_json()), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def to_html(self, report: BenchmarkReport, out_path: str) -> None:
        """HTML 报告是给人看的归档制品，不承担阻断判断——阻断判断只看 JSON 里的 blocking 字段。"""
        template = self._env.get_template("report.html.jinja")
        rendered = template.render(report=report)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(rendered, encoding="utf-8")
