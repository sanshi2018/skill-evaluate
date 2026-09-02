"""裁判判定（docs/dev/02 第 8 节，对应架构文档模块十一子节点一）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from skill_evaluate.state.enums import JudgeVerdictStatus


class JudgeVerdict(BaseModel):
    verdict_id: str
    subject_id: str  # 被评判对象 id（case_id / trace_id / skill_id 视场景而定）
    status: JudgeVerdictStatus
    reasoning: str
    temperature: float
    model: str
    created_at: datetime


class ConsensusResult(BaseModel):
    """高危/低覆盖率判定的 3 副本温度扰动共识结果。"""

    subject_id: str
    verdicts: list[JudgeVerdict] = Field(default_factory=list)
    consensus_reached: bool
    final_status: JudgeVerdictStatus
    dissenting_node: str | None = None  # 未达成共识时，指出 reasoning 分歧指向的 Trace 节点
