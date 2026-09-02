"""docs/dev/05 第 3.3 节 CI 阻断判定规则测试。"""

from datetime import UTC, datetime

from skill_evaluate.observability.report_schema import BenchmarkReport, DimensionResult
from skill_evaluate.state.enums import JudgeVerdictStatus


def _report(dimensions: list[DimensionResult]) -> BenchmarkReport:
    return BenchmarkReport(
        run_id="r1",
        skill_id="s1",
        skill_version_ref="v1",
        generated_at=datetime.now(UTC),
        overall_status=JudgeVerdictStatus.PASS,
        suite_version_id="sv1",
        dimensions=dimensions,
    )


def test_blocking_true_when_blocking_dimension_fails() -> None:
    report = _report(
        [
            DimensionResult(
                dimension="trigger_accuracy", status=JudgeVerdictStatus.FAIL, blocking=True
            )
        ]
    )
    assert report.blocking is True


def test_blocking_false_when_failing_dimension_not_blocking() -> None:
    report = _report(
        [
            DimensionResult(
                dimension="context_scoping", status=JudgeVerdictStatus.FAIL, blocking=False
            )
        ]
    )
    assert report.blocking is False


def test_blocking_false_when_all_pass() -> None:
    report = _report(
        [
            DimensionResult(
                dimension="trigger_accuracy", status=JudgeVerdictStatus.PASS, blocking=True
            )
        ]
    )
    assert report.blocking is False
