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
    # 逐类别的数量覆盖（docs/dev/13 追加）。`positive_count`/`negative_count` 是
    # docs/dev/06 定下的字段，不动；新类别的数量由调用方按自己的语义决定——例如
    # 模块三的"渐进式披露触发探查"要求**每个参考文件各出一条**，条数只有调用方
    # 算得出来。未在此声明的类别回落到 `count_for()` 的默认值。
    category_counts: dict[TestCaseCategory, int] = Field(default_factory=dict)
    # 与被测 Skill 协同出题所需的"其他 Skill"（docs/dev/20 追加，默认空 = 原行为）。
    # MULTI_SKILL 复合用例要求"必须由目标 Skill 与干扰包中某个 Skill 协同完成"，不把
    # 干扰包的 description 给模型看，它只能凭空编一个并不存在的协作对象。
    # 其余类别的模板不渲染这个变量。
    background_skills: list[SkillDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_counts(self) -> GenerationRequest:
        if self.positive_count < 0 or self.negative_count < 0:
            raise ValueError("positive_count / negative_count 不能为负")
        if not self.categories:
            raise ValueError("categories 不能为空，至少要生成一类用例")
        return self

    def count_for(self, category: TestCaseCategory) -> int:
        """该类别这一批要出几条。

        优先级：`category_counts` 显式声明 > POSITIVE/NEGATIVE 的专用字段 >
        保守默认。显式声明排在最前，是为了让调用方能在不碰 `positive_count`
        语义的前提下给新类别定量（docs/dev/13 的渐进式披露探查用例就是这么用的）。
        """
        explicit = self.category_counts.get(category)
        if explicit is not None:
            return explicit
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
    # docs/dev/13：渐进式披露触发探查用例要说明"这条题针对的是哪个参考文件"。
    # 让模型自己回填而不是事后用关键词反推：出题时它是知道自己在瞄准哪个文件的，
    # 事后靠正文相似度去猜，等于把一个确定的事实重新变成一次不可靠的推断。
    # 其余类别的模板不会渲染这个字段，模型不填即为 None。
    probe_target_reference: str | None = None


class GeneratedCaseBatch(BaseModel):
    """一次生成调用的完整输出。"""

    cases: list[GeneratedCase]
