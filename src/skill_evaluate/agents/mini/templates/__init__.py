"""评审模板包（docs/dev/07 第 4、5 节）。

`registry.py` 是机制，`builtin.py` 是首批内容，`schemas.py` 是输出契约，
`*.jinja` 是各场景的 Prompt 措辞。
"""

from skill_evaluate.agents.mini.templates.registry import (
    REVIEW_TEMPLATE_REGISTRY,
    ReviewTemplate,
    get_template,
    register_template,
    verdict_field_to_status,
)

__all__ = [
    "REVIEW_TEMPLATE_REGISTRY",
    "ReviewTemplate",
    "get_template",
    "register_template",
    "verdict_field_to_status",
]
