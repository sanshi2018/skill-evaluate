"""能力树（docs/dev/02 第 7 节，对应架构文档模块六/七/八）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from skill_evaluate.state.enums import CapabilityTier

TIER_WEIGHTS: dict[CapabilityTier, float] = {
    CapabilityTier.P0_CORE: 0.6,
    CapabilityTier.P1_CONDITIONAL: 0.3,
    CapabilityTier.P2_DEFENSIVE: 0.1,
}


class NegativeConstraint(BaseModel):
    """SKILL.md 中的 Gotchas/避坑指南，模块八反事实追踪对象。"""

    constraint_id: str
    description: str
    covered: bool = False
    covering_case_ids: list[str] = Field(default_factory=list)


class CapabilityNode(BaseModel):
    capability_id: str
    skill_id: str
    description: str
    tier: CapabilityTier
    covered: bool = False
    covering_case_ids: list[str] = Field(default_factory=list)


class CapabilityTree(BaseModel):
    skill_id: str
    skill_version_ref: str
    nodes: list[CapabilityNode] = Field(default_factory=list)
    negative_constraints: list[NegativeConstraint] = Field(default_factory=list)
    combinatorial_pairs_covered: list[tuple[str, str]] = Field(
        default_factory=list
    )  # 模块七组合矩阵

    def weighted_coverage(self) -> float:
        if not self.nodes:
            return 0.0
        total = sum(TIER_WEIGHTS[n.tier] for n in self.nodes)
        covered = sum(TIER_WEIGHTS[n.tier] for n in self.nodes if n.covered)
        return covered / total if total else 0.0
