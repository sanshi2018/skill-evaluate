"""用例类别 → 生成模板的注册表（docs/dev/13 第 3.1 节对 docs/dev/06 的正式修订）。

## 为什么要有这张表

docs/dev/06 落地时，`agent.py` 里写的是一个硬编码字典：

```python
_TEMPLATE_BY_CATEGORY = {
    TestCaseCategory.POSITIVE: "positive.jinja",
    TestCaseCategory.NEGATIVE: "negative.jinja",
}
```

docs/dev/13 需要新增两个类别（渐进式披露动态探查），docs/dev/20 要加
MULTI_SKILL——照原样下去，每份文档都要回头改一次 `agent.py`。改成注册表之后，
新增一个类别只需要两步，**不碰生成器本体**：

```python
register_generation_template(TestCaseCategory.MULTI_SKILL, "multi_skill.jinja")
```

**一个例外：ADVERSARIAL 不走本表**。模块五（docs/dev/15）底下有七个攻击面，各有
各的构造要求，共用一个模板会得到一份七种要求混在一起的超长 Prompt。它由
`agents.attacker.AttackerAgent` 用**第二层**注册表（`attacker/playbook.py`）承担，
本表在 `get_generation_template()` 的错误信息里点名了这件事。

与 docs/dev/07 的 `ReviewTemplate` 注册机制同构，包括"重名直接报错"这一条取舍：
静默覆盖会让"这批用例到底是哪套 Prompt 出的"无法追溯。

## 导入即注册

本模块在文件末尾注册内置的四个类别。任何 `import skill_evaluate.agents.generator`
的路径都会经过这里（`agent.py` 导入了本模块），所以调用方不需要额外做什么。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from skill_evaluate.errors import GenerationError
from skill_evaluate.state.enums import TestCaseCategory

# 模板文件所在目录 = 本包目录。注册时按**文件名**给路径（相对本目录），
# 与 Jinja 环境的 loader 根一致。
TEMPLATE_DIR = Path(__file__).parent


@dataclass(frozen=True, slots=True)
class GenerationTemplate:
    """一个用例类别的生成模板定义。"""

    category: TestCaseCategory
    prompt_path: str  # 相对 `agents/generator/prompts/` 的文件名
    description: str = ""
    # 该类别的用例是否必须带一个"探查目标参考文件"（docs/dev/13 的
    # PROGRESSIVE_DISCLOSURE_TRIGGER）。为 True 时 `GeneratorAgent` 会把模型回填的
    # `probe_target_reference` 与 `skill.reference_files` 核对后写进 `TestCase`。
    # 做成模板的属性而不是在 `agent.py` 里 `if category is ...`：这正是这次改注册表
    # 想消灭的那种分支。
    requires_probe_target: bool = False


GENERATION_TEMPLATE_REGISTRY: dict[TestCaseCategory, GenerationTemplate] = {}


def register_generation_template(
    category: TestCaseCategory,
    prompt_path: str,
    *,
    description: str = "",
    requires_probe_target: bool = False,
) -> GenerationTemplate:
    """注册一个类别的生成模板。

    两处**立刻失败**而不是延后到生成时才发现：
    - 重复注册：同一个类别被两份文档各注册一次，说明有人在互相覆盖出题逻辑；
    - 模板文件不存在：拼错文件名在装配期就该炸，而不是等一次真实出题跑到一半。
    """
    if category in GENERATION_TEMPLATE_REGISTRY:
        raise GenerationError(
            f"生成模板重复注册：category={category.value!r}，"
            f"已注册模板 {GENERATION_TEMPLATE_REGISTRY[category].prompt_path!r}"
        )
    if not (TEMPLATE_DIR / prompt_path).is_file():
        raise GenerationError(
            f"category={category.value!r} 的生成模板文件不存在：{TEMPLATE_DIR / prompt_path}"
        )
    template = GenerationTemplate(
        category=category,
        prompt_path=prompt_path,
        description=description,
        requires_probe_target=requires_probe_target,
    )
    GENERATION_TEMPLATE_REGISTRY[category] = template
    return template


def get_generation_template(category: TestCaseCategory) -> GenerationTemplate:
    """取某个类别的模板；未注册时报错并点名该由哪份文档补齐。

    刻意不静默跳过：静默跳过会让"这个类别一条用例都没生成"看起来像"这个 Skill
    天然没有这类场景"，而后者是个完全不同的结论。
    """
    template = GENERATION_TEMPLATE_REGISTRY.get(category)
    if template is None:
        raise GenerationError(
            f"category={category.value!r} 尚无对应的生成模板；已注册："
            f"{sorted(c.value for c in GENERATION_TEMPLATE_REGISTRY)}。"
            "新增类别请新增模板并调用 register_generation_template()，"
            "接入方式见 docs/dev/interfaces/06_generator_extension_points.md 第 3 节。"
            "ADVERSARIAL **不在本表登记**：模块五（docs/dev/15）底下有七个攻击面，"
            "各有各的构造要求，塞进一个模板只会让模型挑最好写的两类反复出题。"
            "对抗用例请改用 `agents.attacker.AttackerAgent`（它是 GeneratorAgent 的"
            "子类，重写了 ADVERSARIAL 这一支，其余类别照常交给父类）。"
        )
    return template


def registered_categories() -> list[TestCaseCategory]:
    return sorted(GENERATION_TEMPLATE_REGISTRY, key=lambda c: c.value)


# --------------------------------------------------------------------------- #
# 内置注册（docs/dev/06 两条 + docs/dev/13 两条）
# --------------------------------------------------------------------------- #

register_generation_template(
    TestCaseCategory.POSITIVE,
    "positive.jinja",
    description="正向触发用例（docs/dev/06）",
)
register_generation_template(
    TestCaseCategory.NEGATIVE,
    "negative.jinja",
    description="反向近脱靶用例（docs/dev/06）",
)
register_generation_template(
    TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
    "pd_trigger.jinja",
    description="渐进式披露动态探查：场景精确命中某个参考文件的触发条件（docs/dev/13）",
    requires_probe_target=True,
)
register_generation_template(
    TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR,
    "pd_regular.jinja",
    description="渐进式披露动态探查：不该读取任何参考文件的常规任务（docs/dev/13）",
)
# docs/dev/20：多技能协同复合用例。放在内置注册区而不是模块十自己的包里做导入副作用：
# 出题与"哪个维度导入了谁"解耦——CLI `generate --force` 重出整套题时不会导入 nodes/。
# 模板需要 `GenerationRequest.background_skills`（干扰包）才有协作对象可写；调用方传空
# 列表时模板仍能渲染，但出不来合格的题，因此模块十在干扰包为空时把条数算成 0（不出题）。
register_generation_template(
    TestCaseCategory.MULTI_SKILL,
    "multi_skill.jinja",
    description="多技能协同复合用例：目标 Skill 与干扰包中某个 Skill 协同完成（docs/dev/20）",
)


__all__ = [
    "GENERATION_TEMPLATE_REGISTRY",
    "TEMPLATE_DIR",
    "GenerationTemplate",
    "get_generation_template",
    "register_generation_template",
    "registered_categories",
]
