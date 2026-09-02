"""`benchmark.json` Schema（docs/dev/05 第 3.1 节）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from skill_evaluate.state.enums import JudgeVerdictStatus, SeverityLevel


class DimensionResult(BaseModel):
    dimension: str  # "trigger_accuracy" | "context_scoping" | ...（与 NODE_BACKEND_ROUTING 键一致）
    status: JudgeVerdictStatus
    score: float | None = None  # 部分维度有量化分数（如加权覆盖率），部分只有 pass/fail
    findings: list[str] = Field(default_factory=list)  # 人类可读摘要，非结构化细节走各自专表
    blocking: bool  # 是否构成流水线阻断（对应各维度的 Hard/Soft Fail 判定）


class BenchmarkReport(BaseModel):
    """跨模块交互速查表之外唯一允许聚合读取多个维度产出的模型（docs/dev/02 第 12 节）。

    它在流水线收尾节点（docs/dev/24 主图的终节点）组装，从各维度已落库的
    `JudgeVerdict`/`SecurityFinding`/`CapabilityTree` 读取摘要字段拼装而成，本身不
    重复存储明细，保持"单一数据源"原则。
    """

    run_id: str
    skill_id: str
    skill_version_ref: str
    generated_at: datetime
    overall_status: JudgeVerdictStatus
    dimensions: list[DimensionResult] = Field(default_factory=list)
    suite_version_id: str
    security_findings_summary: dict[SeverityLevel, int] = Field(default_factory=dict)
    coverage_summary: dict[str, float] = Field(default_factory=dict)  # 模块六/七/八产出
    # docs/dev/06 第 4.1 节：SKILL.md 版本已漂移但按"手动强制"约定没有自动重新
    # 生成用例集时，把告警原文带进报告，交给人类判断是否需要重新出题。
    # 由主图入口节点（docs/dev/24）从 `EnsureTestSuiteResult.staleness_warning`
    # 透传给 `ReportGenerator.build()`。
    test_suite_staleness_warning: str | None = None

    @property
    def blocking(self) -> bool:
        """CI 阻断判定规则（docs/dev/05 第 3.3 节）：任一 dimensions[].blocking==True 且
        status==FAIL 即阻断。
        """
        return any(d.blocking and d.status == JudgeVerdictStatus.FAIL for d in self.dimensions)
