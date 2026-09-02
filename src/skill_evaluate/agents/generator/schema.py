"""Generator 的统一生成指令与 LLM 输出契约（docs/dev/06 第 3、5.3 节）。

`GenerationRequest` 是 Generator 唯一的入口参数：模块六/七的覆盖率补盲、模块
九的验证集抽样、模块十的组合矩阵，都是"构造不同的 GenerationRequest"，而不是
"另起一个生成器"。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from skill_evaluate.state.enums import GenerationMode, TestCaseCategory
from skill_evaluate.state.skill import SkillDefinition


class CapabilityFocus(BaseModel):
    """定向补盲区约束。

    模块六/七检测到覆盖率盲区后构造本对象并调用 `incremental_patch()`；模块十
    的组合矩阵盲区走 `combinatorial_pairs`。生产者见
    docs/dev/interfaces/06_generator_extension_points.md。
    """

    capability_ids: list[str] = Field(default_factory=list)
    negative_constraint_ids: list[str] = Field(default_factory=list)
    combinatorial_pairs: list[tuple[str, str]] = Field(default_factory=list)
    # 能力 id -> 人类可读描述。Prompt 里必须给模型看得懂的描述而不是裸 id，
    # 否则"必须覆盖 cap-7"对模型没有任何信息量。缺失的 id 会退化为 id 原文。
    descriptions: dict[str, str] = Field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.capability_ids or self.negative_constraint_ids or self.combinatorial_pairs)

    def describe(self, item_id: str) -> str:
        return self.descriptions.get(item_id, item_id)


class GenerationRequest(BaseModel):
    skill: SkillDefinition
    mode: GenerationMode
    categories: list[TestCaseCategory] = Field(
        default_factory=lambda: [TestCaseCategory.POSITIVE, TestCaseCategory.NEGATIVE]
    )
    positive_count: int = 9  # 架构文档建议 8-10
    negative_count: int = 9
    capability_focus: CapabilityFocus | None = None  # None = 常规发散生成
    seed_anchor_ids: list[str] | None = None  # docs/dev/21 扩展点，当前仅作 few-shot 注入
    triggered_by: str  # "manual_cli" | "auto_bootstrap" | "coverage_gap" | ...

    @model_validator(mode="after")
    def _validate_counts(self) -> GenerationRequest:
        if self.positive_count < 0 or self.negative_count < 0:
            raise ValueError("positive_count / negative_count 不能为负")
        if not self.categories:
            raise ValueError("categories 不能为空，至少要生成一类用例")
        return self

    def count_for(self, category: TestCaseCategory) -> int:
        if category is TestCaseCategory.POSITIVE:
            return self.positive_count
        if category is TestCaseCategory.NEGATIVE:
            return self.negative_count
        # ADVERSARIAL（模块五）/ MULTI_SKILL（模块十）由各自文档决定数量，
        # 走 CapabilityFocus 定向生成时按 focus 内容动态决定，这里给个保守默认。
        return self.positive_count


# --------------------------------------------------------------------------- #
# LLM 结构化输出契约
# --------------------------------------------------------------------------- #


class GeneratedCase(BaseModel):
    """模型为单条用例产出的内容。"""

    prompt: str
    rationale: str  # 为什么这条用例属于目标类别，供人工复核用例质量
    diversity_tag: str  # "colloquial" | "typo" | "implicit" | "complex_context" | ...
    target_capability_ids: list[str] = Field(default_factory=list)
    negative_constraint_ids: list[str] = Field(default_factory=list)
    expected_output: str | None = None


class GeneratedCaseBatch(BaseModel):
    """一次生成调用的完整输出。"""

    cases: list[GeneratedCase]
