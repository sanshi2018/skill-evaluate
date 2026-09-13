"""Optimizer Agent：补丁生成与闭环重试策略。

实现文档：docs/dev/09_Optimizer_Agent与闭环重试策略.md。

- `service.py`：`OptimizerAgent`（按角色出补丁）、`build_failure_context()`
  （训练集约束的唯一强制入口）、角色注册表。
- `loop.py`：`OptimizationLoop`——提补丁 → 应用 → 重测 → 超限挂起的通用编排。
- `patch_applier.py`：unified diff 的内存/临时工作副本应用。
- `consensus_gate.py`：`retest_fn` 的可组合门控——模型怪癖剥离拦截器与异构共识
  门控（docs/dev/19 第 7 节，可选增强，由各闭环按需叠加）。
- `schema.py`：`FailureContext` 与 `PatchProposal` 契约。
- `prompts/`：各角色的 Prompt 模板。

后续模块（11 训练集重跑、15 安全+功能双重回归、22 人工裁决、24 补丁转 PR）的
接入点见 docs/dev/interfaces/09_optimizer_retest_and_patch.md。
"""

from skill_evaluate.agents.optimizer.consensus_gate import (
    AGENT_OVERFITTING_MARKER,
    MODEL_QUIRK_MARKER,
    is_agent_overfitting,
    is_model_quirk_rejection,
    with_consensus_gate,
    with_quirk_stripping_gate,
)
from skill_evaluate.agents.optimizer.loop import (
    RESUME_ADOPT,
    LoopResult,
    OptimizationLoop,
    RetestFn,
)
from skill_evaluate.agents.optimizer.patch_applier import (
    apply_patch,
    apply_unified_diff,
    cleanup_working_copy,
    working_version_ref,
)
from skill_evaluate.agents.optimizer.schema import (
    ROLE_APPSEC_EXPERT,
    ROLE_PROMPT_ENGINEER,
    FailureContext,
    PatchProposal,
)
from skill_evaluate.agents.optimizer.service import (
    ROLE_REGISTRY,
    OptimizerAgent,
    RoleSpec,
    build_failure_context,
    get_role,
    register_role,
)

__all__ = [
    "AGENT_OVERFITTING_MARKER",
    "MODEL_QUIRK_MARKER",
    "RESUME_ADOPT",
    "ROLE_APPSEC_EXPERT",
    "ROLE_PROMPT_ENGINEER",
    "ROLE_REGISTRY",
    "FailureContext",
    "LoopResult",
    "OptimizationLoop",
    "OptimizerAgent",
    "PatchProposal",
    "RetestFn",
    "RoleSpec",
    "apply_patch",
    "apply_unified_diff",
    "build_failure_context",
    "cleanup_working_copy",
    "get_role",
    "is_agent_overfitting",
    "is_model_quirk_rejection",
    "register_role",
    "with_consensus_gate",
    "with_quirk_stripping_gate",
    "working_version_ref",
]
