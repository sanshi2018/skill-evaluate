"""评审模板注册表（docs/dev/07 第 4 节）。

新增一种审查维度 = 新增一个 `.jinja` + 一个输出 Schema + 一次
`register_template()`。**不需要改动 `MiniReviewAgent` 本体**，这是本框架存在的
全部意义。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from jinja2 import Environment
from pydantic import BaseModel, ConfigDict, Field

from skill_evaluate.agents.templating import build_prompt_env
from skill_evaluate.errors import ReviewTemplateError
from skill_evaluate.state.enums import JudgeVerdictStatus, SeverityLevel

TEMPLATE_DIR = Path(__file__).parent
_ENV: Environment = build_prompt_env(TEMPLATE_DIR)


class ReviewTemplate(BaseModel):
    """一个审查场景的完整定义。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    key: str
    prompt_path: str  # 相对 `templates/` 的 jinja 文件名
    output_schema: type[BaseModel]
    to_status: Callable[[Any], JudgeVerdictStatus]
    required_variables: tuple[str, ...] = ()  # 渲染所需的 content 键，缺失即报错
    # docs/dev/15（模块五严重性定级）预留：该场景的产出需要映射到 SeverityLevel
    # 而不是 pass/fail 时，注册时一并给出本函数。`MiniReviewAgent.review()` 仍然
    # 只产出 JudgeVerdict（保持返回类型稳定），需要严重级别的调用方走
    # `MiniReviewAgent.review_detailed()` 拿到原始结构化输出后自行调用它。
    # 这样 docs/dev/15 接入时**不需要修改 ReviewTemplate 或 MiniReviewAgent**。
    to_severity: Callable[[Any], SeverityLevel] | None = None
    description: str = ""

    def render(self, content: dict[str, str]) -> str:
        missing = [name for name in self.required_variables if name not in content]
        if missing:
            raise ReviewTemplateError(f"评审模板 {self.key!r} 缺少必需的 content 变量：{missing}")
        return _ENV.get_template(self.prompt_path).render(**content)


REVIEW_TEMPLATE_REGISTRY: dict[str, ReviewTemplate] = {}


def register_template(template: ReviewTemplate) -> ReviewTemplate:
    """注册模板。重复 key 直接报错——静默覆盖会让"到底跑的是哪份 Prompt"无法追溯。"""
    if template.key in REVIEW_TEMPLATE_REGISTRY:
        raise ReviewTemplateError(f"评审模板 key 重复注册：{template.key!r}")
    if not (TEMPLATE_DIR / template.prompt_path).is_file():
        raise ReviewTemplateError(
            f"评审模板 {template.key!r} 指向的文件不存在：{TEMPLATE_DIR / template.prompt_path}"
        )
    REVIEW_TEMPLATE_REGISTRY[template.key] = template
    return template


def get_template(key: str) -> ReviewTemplate:
    template = REVIEW_TEMPLATE_REGISTRY.get(key)
    if template is None:
        raise ReviewTemplateError(
            f"未注册的评审模板 key={key!r}；已注册：{sorted(REVIEW_TEMPLATE_REGISTRY)}"
        )
    return template


def verdict_field_to_status(output: Any) -> JudgeVerdictStatus:
    """默认映射：直接读结构化输出里的 `verdict` 字段。

    首批 7 个模板共用本函数。需要更复杂判定（例如"三个布尔项有两个为 False 才
    算 fail"）的场景，在注册时传入自己的 `to_status`。
    """
    return JudgeVerdictStatus.PASS if output.verdict == "pass" else JudgeVerdictStatus.FAIL


class TemplateContent(BaseModel):
    """`ReviewRequest.content` 的类型别名载体（仅用于文档化，运行时用普通 dict）。"""

    values: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "REVIEW_TEMPLATE_REGISTRY",
    "TEMPLATE_DIR",
    "ReviewTemplate",
    "TemplateContent",
    "get_template",
    "register_template",
    "verdict_field_to_status",
]
