"""`OptimizerAgent`：把失败证据变成一份候选补丁（docs/dev/09 第 3、4、6 节）。

角色（`FailureContext.role`）决定用哪套 Prompt 模板、允许产出哪类补丁：

| role | 模板 | 允许的 patch_type | 由谁使用 |
|---|---|---|---|
| `prompt_engineer` | `description_patch.jinja` | `description_patch` / `rigid_constraint` | docs/dev/11 |
| `appsec_expert` | `appsec_patch.jinja` | `rigid_constraint` / `code_patch` | docs/dev/15 |

角色做成注册表而不是 if/else：docs/dev/15 之后若再出现第三种优化场景（例如
模块十的"精简 Token 占用"），新增一个 `.jinja` + 一次 `register_role()` 即可，
不改本文件——与 docs/dev/07 的模板注册表同一种扩展模式。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from skill_evaluate.agents.base import BaseLLMAgent
from skill_evaluate.agents.llm import AgentLLMClient
from skill_evaluate.agents.optimizer.schema import (
    ROLE_APPSEC_EXPERT,
    ROLE_PROMPT_ENGINEER,
    FailureContext,
    PatchProposal,
)
from skill_evaluate.agents.templating import build_prompt_env
from skill_evaluate.config import get_settings
from skill_evaluate.errors import AgentError
from skill_evaluate.logging import get_logger
from skill_evaluate.observability.langfuse_adapter import LangfuseAdapter, LangfuseTraceHandle
from skill_evaluate.persistence.repository import PatchRepository
from skill_evaluate.state.enums import DatasetSplit, PatchType
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.patch import Patch
from skill_evaluate.state.security import SecurityFinding
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase

logger = get_logger(component="optimizer")

_PROMPT_DIR = Path(__file__).parent / "prompts"
_ENV = build_prompt_env(_PROMPT_DIR)

_SYSTEM_PROMPT = (
    "你产出的补丁会进入人工审查，并可能被合入真实代码仓库。因此：只改与本次失败"
    "直接相关的地方，改动越小越好；不确定的地方写进 rationale，而不是自作主张地"
    "多改一处。你只输出 JSON。"
)


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """一个优化角色的完整定义。"""

    role: str
    prompt_path: str
    allowed_patch_types: frozenset[PatchType]
    description: str = ""


ROLE_REGISTRY: dict[str, RoleSpec] = {}


def register_role(spec: RoleSpec) -> RoleSpec:
    """注册一个优化角色。重名直接报错（与模板/规则注册表同样的取舍）。"""
    if spec.role in ROLE_REGISTRY:
        raise AgentError(f"Optimizer 角色重复注册：{spec.role!r}")
    if not (_PROMPT_DIR / spec.prompt_path).is_file():
        raise AgentError(
            f"Optimizer 角色 {spec.role!r} 指向的模板不存在：{_PROMPT_DIR / spec.prompt_path}"
        )
    ROLE_REGISTRY[spec.role] = spec
    return spec


def get_role(role: str) -> RoleSpec:
    spec = ROLE_REGISTRY.get(role)
    if spec is None:
        raise AgentError(f"未注册的 Optimizer 角色 role={role!r}；已注册：{sorted(ROLE_REGISTRY)}")
    return spec


register_role(
    RoleSpec(
        role=ROLE_PROMPT_ENGINEER,
        prompt_path="description_patch.jinja",
        allowed_patch_types=frozenset({PatchType.DESCRIPTION_PATCH, PatchType.RIGID_CONSTRAINT}),
        description="触发准确度闭环：重写 description / 追加正文约束（模块一 / docs/dev/11）",
    )
)
register_role(
    RoleSpec(
        role=ROLE_APPSEC_EXPERT,
        prompt_path="appsec_patch.jinja",
        allowed_patch_types=frozenset({PatchType.RIGID_CONSTRAINT, PatchType.CODE_PATCH}),
        description="安全加固闭环：刚性约束 / 代码补丁（模块五 / docs/dev/15）",
    )
)


# --------------------------------------------------------------------------- #
# 训练集约束的强制执行（docs/dev/09 第 4 节）
# --------------------------------------------------------------------------- #


def build_failure_context(
    skill: SkillDefinition,
    failed_cases: list[TestCase],
    verdicts: list[JudgeVerdict] | list[ConsensusResult],
    *,
    role: str = ROLE_PROMPT_ENGINEER,
    triggered_by_finding_id: str | None = None,
    target_path: str = "SKILL.md",
    extra_instructions: str = "",
    security_findings: list[SecurityFinding] | None = None,
) -> FailureContext:
    """构造 `FailureContext` 的**唯一合法入口**。

    在类型层面强制"验证集不参与优化"：架构文档要求失败日志仅限训练集，防止对
    验证集过拟合。靠调用方自觉是不够的——真正拿验证集去优化的那次，恰恰是最想
    走捷径的那次。

    `verdicts` 允许直接传 `ConsensusResult`：共识判定的三份 verdict 会被展开，
    调用方不需要为"这次是 ROUTINE 还是 CRITICAL"写两套构造代码。

    `security_findings`（docs/dev/15 第 11.1 节）是模块五专用的失败证据。模块五的
    很多判定走的是确定性规则而不是 LLM 裁决，此时 `verdicts` 里的 reasoning 只是
    一句规则名 + inputs，对修补丁的模型没有信息量；真正有用的是 `SecurityFinding`
    里那段具体证据。因此模块五传的是 `verdicts=[]` + 非空的 `security_findings`。
    """
    non_train = [case for case in failed_cases if case.split is not DatasetSplit.TRAIN]
    if non_train:
        raise ValueError(
            f"Optimizer 不得接收非训练集用例: {[case.case_id for case in non_train]}"
            "（架构文档：验证集不参与优化以防止过拟合）"
        )

    flat_verdicts: list[JudgeVerdict] = []
    for item in verdicts:
        if isinstance(item, ConsensusResult):
            flat_verdicts.extend(item.verdicts)
        else:
            flat_verdicts.append(item)

    get_role(role)  # 角色不存在时立刻失败，而不是等到 propose_patch 才发现
    return FailureContext(
        skill=skill,
        failed_case_ids=[case.case_id for case in failed_cases],
        verdicts=flat_verdicts,
        role=role,
        failed_case_prompts=[case.prompt for case in failed_cases],
        triggered_by_finding_id=triggered_by_finding_id,
        target_path=target_path,
        extra_instructions=extra_instructions,
        security_findings=list(security_findings or []),
    )


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #


class OptimizerAgent(BaseLLMAgent):
    """按角色产出一份候选 `Patch`。不应用、不重测、不落回归结论——那是 `OptimizationLoop`。"""

    name = "optimizer_agent"

    def __init__(
        self,
        *,
        model: str | None = None,
        temperature: float | None = None,
        llm_client: AgentLLMClient | None = None,
        langfuse_adapter: LangfuseAdapter | None = None,
        trace_handle: LangfuseTraceHandle | None = None,
        patch_repository: PatchRepository | None = None,
        persist: bool = True,
    ) -> None:
        settings = get_settings()
        super().__init__(
            model=model or settings.llm.optimizer_model,
            temperature=(settings.optimizer.temperature if temperature is None else temperature),
            llm_client=llm_client,
            langfuse_adapter=langfuse_adapter,
            trace_handle=trace_handle,
        )
        self._patch_repo = patch_repository or PatchRepository()
        self._persist = persist

    async def propose_patch(self, ctx: FailureContext) -> Patch:
        """按 `ctx.role` 选模板生成补丁并落库。"""
        spec = get_role(ctx.role)
        prompt = _ENV.get_template(spec.prompt_path).render(
            skill=ctx.skill,
            failed_case_prompts=ctx.failed_case_prompts,
            verdicts=ctx.verdicts,
            extra_instructions=ctx.extra_instructions,
            target_path=ctx.target_path,
            # docs/dev/15：只有 appsec_patch.jinja 渲染它，其余模板不引用。
            # 模板环境是 StrictUndefined，"用到未定义变量"才报错，多传无害。
            security_findings=ctx.security_findings,
        )
        proposal = await self._call_llm(prompt, PatchProposal, system=_SYSTEM_PROMPT)
        patch = self._to_patch(ctx, spec, proposal)

        if self._persist:
            await self._patch_repo.save(patch)

        logger.info(
            "optimizer_patch_proposed",
            skill_id=ctx.skill.skill_id,
            role=ctx.role,
            patch_id=patch.patch_id,
            patch_type=patch.patch_type.value,
            target_path=patch.target_path,
            failed_case_count=len(ctx.failed_case_ids),
        )
        return patch

    def _to_patch(self, ctx: FailureContext, spec: RoleSpec, proposal: PatchProposal) -> Patch:
        patch_type = PatchType(proposal.patch_type)
        if patch_type not in spec.allowed_patch_types:
            # 角色越界不是"模型有创意"，而是这次产出没法被下游正确处理：
            # 例如 prompt_engineer 交回一个 code_patch，安全回归根本不会被触发。
            raise AgentError(
                f"角色 {spec.role!r} 不允许产出 {patch_type.value!r} 类型的补丁；"
                f"允许：{sorted(t.value for t in spec.allowed_patch_types)}"
            )

        rationale = proposal.rationale
        if proposal.functional_risk:
            # 功能误伤评估必须跟着补丁一起走到人工审查那一步，不能只留在模型的
            # 中间输出里（架构文档模块五"过度杀伤力"风险的缓解手段）。
            rationale = f"{rationale}\n\n【功能误伤评估】{proposal.functional_risk}"

        return Patch(
            patch_id=str(uuid.uuid4()),
            skill_id=ctx.skill.skill_id,
            base_skill_version_ref=ctx.skill.version_ref,
            patch_type=patch_type,
            target_path=proposal.target_path or ctx.target_path,
            diff=proposal.diff,
            rationale=rationale,
            triggered_by_finding_id=ctx.triggered_by_finding_id,
            created_at=datetime.now(UTC),
        )


__all__ = [
    "ROLE_APPSEC_EXPERT",
    "ROLE_PROMPT_ENGINEER",
    "ROLE_REGISTRY",
    "FailureContext",
    "OptimizerAgent",
    "RoleSpec",
    "build_failure_context",
    "get_role",
    "register_role",
]
