"""报告生成器：`benchmark.json` / HTML（docs/dev/05 第 3.2 节）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

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
        self, run_id: str, *, test_suite_staleness_warning: str | None = None
    ) -> BenchmarkReport:
        """聚合一次 run 的全部维度结论。

        `test_suite_staleness_warning` 由主图入口节点（docs/dev/24）从
        `agents.generator.service.EnsureTestSuiteResult.staleness_warning` 透传：
        用例集与当前 SKILL.md 版本不匹配时，报告里必须能看到这件事，否则读报告的
        人不知道这份分数是拿旧题跑出来的（docs/dev/06 第 4.1 节）。
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
            coverage_summary={},  # 由 docs/dev/16~18 落库后在此聚合，见待接入说明
            test_suite_staleness_warning=test_suite_staleness_warning,
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
