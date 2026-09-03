"""Validator Agent 与动态断言 Git 工具箱。

实现文档：docs/dev/10_Validator_Agent与动态断言Git工具箱.md。

- `service.py`：`ValidatorAgent.plan_assertion()`——三条策略路径的决策与产出。
- `toolbox.py`：Git 断言工具箱（同步 / 检索 / 渲染），`_semantic_lookup()` 是留给
  docs/dev/23 的混合检索占位钩子。
- `static_check.py`：生成脚本下发沙箱**之前**的本地语法检查（第 6 节安全约束）。
- `evidence.py`：`AssertionResult` -> Judge `content` 字典（第 7 节）。
- `schema.py` / `prompts/`：LLM 输出契约与两条生成路径的 Prompt。

断言脚本的**执行**不在本包内：它发生在 Hermes 沙箱里（`executors/hermes_backend.py`
的 `assertion_specs` 下发 + Hook 回传的 `assertion_executions`），结果由
`api/hooks_hermes.py` 落成 `AssertionResult`。

后续模块（13/15 的证据组合规则、23 的语义检索、15 的 SAST 模板）接入点见
docs/dev/interfaces/10_validator_toolbox_and_assertion_evidence.md。
"""

from skill_evaluate.agents.validator.evidence import (
    all_passed,
    any_failed,
    build_assertion_evidence,
)
from skill_evaluate.agents.validator.schema import AssertionPlanBatch, PlanDecision, ScriptDraft
from skill_evaluate.agents.validator.service import ValidatorAgent
from skill_evaluate.agents.validator.static_check import (
    StaticCheckResult,
    StaticCheckStatus,
    check_script_syntax,
)
from skill_evaluate.agents.validator.toolbox import (
    AssertionToolbox,
    TemplateMatch,
    TemplateMetadata,
    ToolboxError,
    get_default_toolbox,
)

__all__ = [
    "AssertionPlanBatch",
    "AssertionToolbox",
    "PlanDecision",
    "ScriptDraft",
    "StaticCheckResult",
    "StaticCheckStatus",
    "TemplateMatch",
    "TemplateMetadata",
    "ToolboxError",
    "ValidatorAgent",
    "all_passed",
    "any_failed",
    "build_assertion_evidence",
    "check_script_syntax",
    "get_default_toolbox",
]
