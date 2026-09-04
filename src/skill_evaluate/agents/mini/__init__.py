"""Mini Agent 评审框架：低温静态审查 Agent 的统一封装。

实现文档：docs/dev/07_Mini_Agent评审框架.md。

新增一种审查维度只需三步（不改动 `MiniReviewAgent` 本体）：
1. 在 `templates/` 下新增 `.jinja`；
2. 在 `templates/schemas.py` 新增输出 Schema；
3. 调一次 `register_template()`。

接入清单见 docs/dev/interfaces/07_review_template_registry.md。

导入本包即完成内置模板的注册（`templates.builtin` 的首批 7 个 + 各维度自己那份
模板模块的导入副作用，如 docs/dev/13 的 `templates.instruction_control`）。
"""

from skill_evaluate.agents.mini import templates as templates
from skill_evaluate.agents.mini.llm_client import RealMiniLLMClient
from skill_evaluate.agents.mini.service import (
    DEFAULT_REVIEW_TEMPERATURE,
    DetailedReview,
    MiniReviewAgent,
    ReviewRequest,
)
from skill_evaluate.agents.mini.templates import builtin as _builtin  # noqa: F401  # 注册副作用
from skill_evaluate.agents.mini.templates import (  # noqa: F401  # 注册副作用
    instruction_control as _instruction_control,
)
from skill_evaluate.agents.mini.templates.registry import (
    REVIEW_TEMPLATE_REGISTRY,
    ReviewTemplate,
    get_template,
    register_template,
    verdict_field_to_status,
)

__all__ = [
    "DEFAULT_REVIEW_TEMPERATURE",
    "REVIEW_TEMPLATE_REGISTRY",
    "DetailedReview",
    "MiniReviewAgent",
    "RealMiniLLMClient",
    "ReviewRequest",
    "ReviewTemplate",
    "get_template",
    "register_template",
    "verdict_field_to_status",
]
