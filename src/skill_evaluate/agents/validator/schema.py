"""Validator Agent 的 LLM 输出契约与规划中间结果（docs/dev/10 第 2、6 节）。

`ScriptDraft` 的字段是契约，措辞不是：docs/dev/13/15 接入时改 `prompts/*.jinja`
里的措辞与 few-shot，**不要**改这些字段——`AssertionSpec` 落库、沙箱下发、
报告展示都按字段读取。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from skill_evaluate.agents.validator.toolbox import TemplateMetadata
from skill_evaluate.state.assertion import AssertionSpec
from skill_evaluate.state.enums import AssertionStrategy


class ScriptDraft(BaseModel):
    """模型产出的一份校验脚本。`template_inherit` 与 `generated_from_scratch` 共用。

    共用一个 schema 是有意的：两条路径的差别只在 Prompt 里给不给模板做 few-shot，
    产物形态完全一样（一段可直接落盘执行的脚本）。分成两个 schema 只会让下游
    多写一遍同样的落库代码。
    """

    language: Literal["python", "bash"] = "python"
    script: str  # 完整脚本正文，不是 diff、不是片段
    rationale: str = ""  # 这段脚本在验证什么、凭什么认为它能证伪失败
    # 模型对"这段脚本会不会误判"的自评。断言脚本的假阳性比没有断言更危险——它会
    # 以"确定性证据"的身份进入 Judge 的输入。写进 AssertionSpec.failure_reason
    # 之外的地方没有意义，因此只作为日志与人工排查线索。
    false_positive_risk: str = ""


@dataclass(frozen=True, slots=True)
class PlanDecision:
    """`plan_assertion()` 内部的策略决策结果（不落库，仅用于日志与单测断言）。

    把"选哪条路径"与"把脚本做出来"拆开，是为了让检索策略（关键词 -> docs/dev/23
    的混合检索）可以独立替换和独立测试，不必每次都跑一遍 LLM 生成。
    """

    strategy: AssertionStrategy
    template: TemplateMetadata | None = None
    score: float = 0.0
    reason: str = ""


class AssertionPlanBatch(BaseModel):
    """批量规划的返回值（`plan_assertions()`）。

    单独建模而不是裸 `list[AssertionSpec]`，是为了让调用方拿到"这批里有几条降级
    成了 NONE"这个事实——它是报告里"断言生成失败"标记的来源（第 6 节），
    逐条 spec 去数很容易被漏掉。
    """

    specs: list[AssertionSpec] = Field(default_factory=list)
    degraded_case_ids: list[str] = Field(default_factory=list)

    @property
    def executable_count(self) -> int:
        return sum(1 for spec in self.specs if spec.is_executable)


__all__ = ["AssertionPlanBatch", "PlanDecision", "ScriptDraft"]
