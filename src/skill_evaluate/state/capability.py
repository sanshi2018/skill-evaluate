"""能力树（docs/dev/02 第 7 节，对应架构文档模块六/七/八）。

三份文档共享这一棵树，各自负责不同的字段：

| 字段 | 谁写 | 语义 |
|---|---|---|
| `nodes[*].covered` / `covering_case_ids` | 模块六 | 二元覆盖判定 |
| `combinatorial_pairs_covered` | 模块七 | 已被同一条活跃用例同时触发的能力对 |
| `nodes[*].tier` | 模块八 | 能力权重分级（模块六抽取时填占位值） |
| `negative_constraints` | 模块八 | Gotchas/避坑指南的反事实追踪对象 |
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from skill_evaluate.state.enums import CapabilityTier

TIER_WEIGHTS: dict[CapabilityTier, float] = {
    CapabilityTier.P0_CORE: 0.6,
    CapabilityTier.P1_CONDITIONAL: 0.3,
    CapabilityTier.P2_DEFENSIVE: 0.1,
}


class NegativeConstraint(BaseModel):
    """SKILL.md 中的 Gotchas/避坑指南，模块八反事实追踪对象。

    `constraint_id` 与 `capability_id` 同样是**描述文本的确定性哈希**
    （`agents/analyzer/identity.py::build_constraint_id`），理由完全相同：
    `TestCase.negative_constraint_ids` 是长期存活的绑定，id 若随抽取顺序变化，
    历史绑定会在下一次抽取后集体失效，而失效没有任何报错。
    """

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
        """按 `TIER_WEIGHTS` 加权的能力覆盖率（模块八的口径）。

        注意：分级尚未落地时（模块六抽取阶段全部节点都是同一个占位 tier），本方法
        的返回值与"未加权的简单比例"完全相同——那个"相同"是巧合而非结论，因此
        判定记录里另带一个 `tier_weighted` 口径标记（见 `nodes/coverage/rules.py`），
        靠 `tier_grading_applied()` 判断，而不是靠比较数字。
        """
        if not self.nodes:
            return 0.0
        total = sum(TIER_WEIGHTS[n.tier] for n in self.nodes)
        covered = sum(TIER_WEIGHTS[n.tier] for n in self.nodes if n.covered)
        return covered / total if total else 0.0

    def tier_grading_applied(self) -> bool:
        """树上的权重分级是否已经有意义（模块八是否跑过）。

        判据是**树上出现了不止一档 tier**，而不是"是否等于
        `agents.analyzer.service.PLACEHOLDER_TIER`"：前者不依赖占位值具体取哪一档，
        将来若改用别的占位策略，这里也不会悄悄给出错误答案。

        代价是一个边界情形：真实分级后恰好所有能力都是同一档（例如只有一个节点，
        或一份 Skill 的能力确实全是 P0）时本方法仍返回 False，报告会保守地标注
        "未使用权重分级"。宁可保守标注，也不要把"没分过级"说成"分过了"——后者会
        让读报告的人以为那个覆盖率已经体现了能力的重要性差异。

        模块七的组合矩阵截断（`nodes/pruning`）与模块八的判定口径标记共用本方法，
        避免同一个问题在两处各判一次然后慢慢漂移。
        """
        return len({node.tier for node in self.nodes}) > 1

    def negative_constraint_coverage(self) -> float:
        """负向约束覆盖率 = 已被对抗性用例诱导过的约束 / 全部约束（模块八）。

        一条约束都没有时返回 1.0 而不是 0.0：这份 SKILL.md 没写任何"禁止做某事"
        的规则，不等于"该测的都没测"。报告侧靠约束总数区分这两种情形（与模块七
        对"没有组合可分析"的处理同一条理由）。

        它**不并入** `weighted_coverage()`：能力覆盖率的分母是"声明了什么能力"，
        把约束混进去会让同一个百分比同时承载两件语义不同的事，未达标时读报告的人
        无从判断该去补能力用例还是补反事实用例。
        """
        if not self.negative_constraints:
            return 1.0
        covered = sum(1 for c in self.negative_constraints if c.covered)
        return covered / len(self.negative_constraints)
