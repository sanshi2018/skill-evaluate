"""安全发现（docs/dev/02 第 9 节，对应架构文档模块五）。"""

from __future__ import annotations

from pydantic import BaseModel

from skill_evaluate.state.enums import SecurityFindingCategory, SeverityLevel


class SecurityFinding(BaseModel):
    finding_id: str
    case_id: str
    category: SecurityFindingCategory
    severity: SeverityLevel
    evidence: str  # 定位到具体 Trace 节点或 SAST 扫描输出片段
    remediation_patch_id: str | None = None  # 关联 Optimizer 生成的补丁
