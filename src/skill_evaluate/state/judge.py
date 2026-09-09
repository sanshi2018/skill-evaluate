"""裁判判定（docs/dev/02 第 8 节，对应架构文档模块十一子节点一）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from skill_evaluate.state.enums import JudgeVerdictStatus, SeverityLevel


class JudgeVerdict(BaseModel):
    verdict_id: str
    subject_id: str  # 被评判对象 id（case_id / trace_id / skill_id 视场景而定）
    status: JudgeVerdictStatus
    reasoning: str
    temperature: float
    model: str
    created_at: datetime
    # docs/dev/15 第 10.1 节落地 docs/dev/07 预留的 `ReviewTemplate.to_severity`：
    # 有些判定（当前只有模块五的严重性定级）的结论不是"通过/失败"，而是"多严重"。
    # 由 `MiniReviewAgent.review_detailed()` 在模板声明了 `to_severity` 时回填，
    # 其余模板恒为 None。
    #
    # 为什么加在 JudgeVerdict 上而不是让调用方自己走 review_detailed()：
    # `judgmental_verdict()` 是全项目唯一的判定入口（docs/dev/interfaces/08 铁律），
    # 它只返回 JudgeVerdict / ConsensusResult。要拿严重级别就绕过入口，等于绕过
    # 黄金盲测与共识投票——而安全定级恰恰是最不该绕过它们的那一类判定。
    severity: SeverityLevel | None = None


class ConsensusResult(BaseModel):
    """高危/低覆盖率判定的 3 副本温度扰动共识结果。"""

    subject_id: str
    verdicts: list[JudgeVerdict] = Field(default_factory=list)
    consensus_reached: bool
    final_status: JudgeVerdictStatus
    dissenting_node: str | None = None  # 未达成共识时，指出 reasoning 分歧指向的 Trace 节点
