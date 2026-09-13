"""模块十（docs/dev/20）新增的三个评审模板的注册。

与 `security.py` / `weighted_coverage.py` 同一种组织方式：按引入它们的文档分文件，
导入本模块即完成注册（`agents/mini/__init__.py` 已导入）。

| key | 判定对象 | 必需 content |
|---|---|---|
| `semantic_flow_friction` | 一次多技能协作执行的轨迹 | `prompt`, `background_skills`, `actions` |
| `role_persona_conflict` | 被测 SKILL.md vs 干扰包的角色设定（静态） | `skill_md`, `noise_pack_descriptions` |
| `negative_constraint_adherence` | 一次执行是否守住某条负向约束 | `constraint_description`, `case_prompt`, `actions`, `final_response` |

docs/dev/20 第 13 节只登记了前两个；第三个是实现时补的——正文第 8 节想复用模块八的
`does_case_probe_constraint` 判"单测/并发两次执行是否遵守约束"，但那个判定看的是
**用例题面**，看不到 Trace，回答不了"这次执行守没守"。

三个模板全部走 `verdict_field_to_status`：判定口径写在模板正文里，列表字段是给报告与
审查工作台的诊断细节，不在代码里再算一遍（与 docs/dev/13/15/18 的模板同一条约定）。
"""

from __future__ import annotations

from skill_evaluate.agents.mini.templates.registry import (
    ReviewTemplate,
    register_template,
    verdict_field_to_status,
)
from skill_evaluate.agents.mini.templates.schemas import (
    NegativeConstraintAdherenceOutput,
    RolePersonaConflictOutput,
    SemanticFlowFrictionOutput,
)

SEMANTIC_FLOW_FRICTION = register_template(
    ReviewTemplate(
        key="semantic_flow_friction",
        prompt_path="semantic_flow_friction.jinja",
        output_schema=SemanticFlowFrictionOutput,
        to_status=verdict_field_to_status,
        required_variables=("prompt", "background_skills", "actions"),
        description="跨技能语义流转的摩擦力诊断（模块十深度一 / docs/dev/20 第 7 节）",
    )
)

ROLE_PERSONA_CONFLICT = register_template(
    ReviewTemplate(
        key="role_persona_conflict",
        prompt_path="role_persona_conflict.jinja",
        output_schema=RolePersonaConflictOutput,
        to_status=verdict_field_to_status,
        required_variables=("skill_md", "noise_pack_descriptions"),
        description="并发 Skill 间角色设定冲突的静态审查（模块十深度二 / docs/dev/20 第 9 节）",
    )
)

NEGATIVE_CONSTRAINT_ADHERENCE = register_template(
    ReviewTemplate(
        key="negative_constraint_adherence",
        prompt_path="negative_constraint_adherence.jinja",
        output_schema=NegativeConstraintAdherenceOutput,
        to_status=verdict_field_to_status,
        required_variables=("constraint_description", "case_prompt", "actions", "final_response"),
        description="一次执行是否遵守负向约束（模块十注意力衰减 / docs/dev/20 第 8 节）",
    )
)

MULTI_SKILL_TEMPLATE_KEYS: tuple[str, ...] = (
    SEMANTIC_FLOW_FRICTION.key,
    ROLE_PERSONA_CONFLICT.key,
    NEGATIVE_CONSTRAINT_ADHERENCE.key,
)

__all__ = [
    "MULTI_SKILL_TEMPLATE_KEYS",
    "NEGATIVE_CONSTRAINT_ADHERENCE",
    "ROLE_PERSONA_CONFLICT",
    "SEMANTIC_FLOW_FRICTION",
]
