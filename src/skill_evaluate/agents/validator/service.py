"""`ValidatorAgent`：在任务执行**之前**规划校验策略（docs/dev/10 第 2、6 节）。

职责边界（很窄，故意的）：**只产出 `AssertionSpec`，不执行任何脚本**。脚本的执行
发生在 Hermes 沙箱里、任务跑完之后、容器销毁之前（第 4 节），执行结果经 Hook 回
调落成 `AssertionResult`。本类连"断言过了算不算通过"都不判——那是 Judge 的事
（第 7 节）。

策略决策顺序（第 2 节原文）：

1. `template_lookup`：Git 断言工具箱命中，且模板不需要额外参数（或参数已由调用方
   提供）——**不调用任何 LLM**，直接渲染模板。
2. `template_inherit`：工具箱里有"形状相似但需要参数化"的模板——把模板作为
   few-shot，要求模型只改写必要部分。
3. `generated_from_scratch`：工具箱无匹配，从头生成。

`expected_output` 为空时直接返回 `strategy=NONE` 的空 spec：不是所有用例都需要
确定性断言，这条用例只依赖 Judge 的语义裁决。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from skill_evaluate.agents.base import BaseLLMAgent
from skill_evaluate.agents.llm import AgentLLMClient
from skill_evaluate.agents.templating import build_prompt_env
from skill_evaluate.agents.validator.schema import AssertionPlanBatch, PlanDecision, ScriptDraft
from skill_evaluate.agents.validator.static_check import (
    LANGUAGE_BASH,
    LANGUAGE_PYTHON,
    StaticCheckResult,
    check_script_syntax,
)
from skill_evaluate.agents.validator.toolbox import (
    AssertionToolbox,
    TemplateMetadata,
    ToolboxError,
    get_default_toolbox,
)
from skill_evaluate.config import get_settings
from skill_evaluate.errors import AgentError
from skill_evaluate.logging import get_logger
from skill_evaluate.observability.langfuse_adapter import LangfuseAdapter, LangfuseTraceHandle
from skill_evaluate.persistence.repository import AssertionRepository
from skill_evaluate.state.assertion import AssertionSpec
from skill_evaluate.state.enums import AssertionStrategy
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase

logger = get_logger(component="validator")

_PROMPT_DIR = Path(__file__).parent / "prompts"
_ENV = build_prompt_env(_PROMPT_DIR)

_SYSTEM_PROMPT = (
    "你写的脚本会在沙箱里以 exit_code 的形式给出一条'确定性证据'，它的权重高于"
    "语义裁判的意见。因此：宁可少验一点、验得准，也不要为了覆盖面写出会误判的"
    "检查。只用标准库，不联网，不改动现场。你只输出 JSON。"
)

# SKILL.md 正文进 Prompt 的截断长度。Validator 需要的是"这份 Skill 大致在干什么"
# 以便理解任务意图，不需要全文——全文会把真正重要的 case.prompt/expected_output
# 淹没在几千 token 的背景里。
_SKILL_BODY_EXCERPT_CHARS = 4000

_NO_EXPECTED_OUTPUT_REASON = (
    "用例未声明 expected_output：按 docs/dev/10 第 2 节，该用例只依赖 Judge Agent "
    "的语义裁决，不生成校验脚本"
)


class ValidatorAgent(BaseLLMAgent):
    """断言规划器。构造不做 IO：工具箱是惰性加载的，没有本地缓存也能实例化。"""

    name = "validator_agent"

    def __init__(
        self,
        *,
        model: str | None = None,
        temperature: float = 0.0,
        llm_client: AgentLLMClient | None = None,
        langfuse_adapter: LangfuseAdapter | None = None,
        trace_handle: LangfuseTraceHandle | None = None,
        toolbox: AssertionToolbox | None = None,
        assertion_repository: AssertionRepository | None = None,
        persist: bool = True,
    ) -> None:
        settings = get_settings()
        super().__init__(
            model=model or settings.llm.validator_model,
            temperature=temperature,
            llm_client=llm_client,
            langfuse_adapter=langfuse_adapter,
            trace_handle=trace_handle,
        )
        self._toolbox = toolbox if toolbox is not None else get_default_toolbox()
        self._repo = assertion_repository or AssertionRepository()
        self._persist = persist

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #

    async def plan_assertion(
        self,
        case: TestCase,
        skill: SkillDefinition,
        *,
        known_params: dict[str, str] | None = None,
    ) -> AssertionSpec:
        """为一条用例规划断言。**不执行脚本**。

        `known_params` 是调用方（评测维度节点）已知的模板参数（例如它自己约定了
        产物落在哪个路径）。全部必需参数齐备时才可能走 `template_lookup` 这条
        零 LLM 成本的路径。
        """
        assertion_id = str(uuid.uuid4())

        if not (case.expected_output or "").strip():
            return await self._finalize_none(
                assertion_id, case, reason=_NO_EXPECTED_OUTPUT_REASON, degraded=False
            )

        decision = await self.decide_strategy(case, skill, known_params=known_params)
        logger.info(
            "validator_strategy_decided",
            case_id=case.case_id,
            skill_id=skill.skill_id,
            strategy=decision.strategy.value,
            template=decision.template.template if decision.template else None,
            score=round(decision.score, 3),
            reason=decision.reason,
        )

        if decision.strategy is AssertionStrategy.TEMPLATE_LOOKUP:
            spec = self._build_from_template(assertion_id, case, decision, known_params or {})
            if spec is not None:
                return await self._finalize(spec)
            # 模板渲染出来的脚本语法都不过，说明工具箱那一份坏了。此时从头生成
            # 严格优于放弃：降级为 NONE 会让这条用例白白失去确定性证据。
            decision = PlanDecision(
                strategy=AssertionStrategy.GENERATED_FROM_SCRATCH,
                reason="模板渲染结果未通过静态检查，回退到从头生成",
            )

        return await self._generate_with_repair(assertion_id, case, skill, decision)

    async def plan_assertions(
        self,
        cases: list[TestCase],
        skill: SkillDefinition,
        *,
        known_params: dict[str, str] | None = None,
    ) -> AssertionPlanBatch:
        """批量规划。逐条串行——断言生成不是流水线瓶颈，并发只会让工具箱与
        Langfuse 打点的日志顺序变得难以排查。
        """
        specs: list[AssertionSpec] = []
        degraded: list[str] = []
        for case in cases:
            spec = await self.plan_assertion(case, skill, known_params=known_params)
            specs.append(spec)
            if spec.strategy is AssertionStrategy.NONE and spec.failure_reason not in (
                None,
                _NO_EXPECTED_OUTPUT_REASON,
            ):
                degraded.append(case.case_id)
        if degraded:
            logger.warning(
                "validator_batch_degraded",
                skill_id=skill.skill_id,
                degraded_case_ids=degraded,
                total=len(cases),
            )
        return AssertionPlanBatch(specs=specs, degraded_case_ids=degraded)

    # ------------------------------------------------------------------ #
    # 策略决策（可独立测试，不触发 LLM）
    # ------------------------------------------------------------------ #

    async def decide_strategy(
        self,
        case: TestCase,
        skill: SkillDefinition,
        *,
        known_params: dict[str, str] | None = None,
    ) -> PlanDecision:
        """选路径：lookup / inherit / from_scratch。此方法不调用 LLM。"""
        if not self._toolbox.available:
            return PlanDecision(
                strategy=AssertionStrategy.GENERATED_FROM_SCRATCH,
                reason="断言工具箱不可用（未配置或未同步）",
            )

        query = self._build_query(case, skill)
        try:
            matches = await self._toolbox.lookup(query)
        except ToolboxError as exc:
            # 工具箱结构坏了不该让整条流水线停下来：记一条 error 后照常从头生成。
            logger.error("validator_toolbox_broken", error=str(exc))
            return PlanDecision(
                strategy=AssertionStrategy.GENERATED_FROM_SCRATCH,
                reason=f"工具箱不可用：{exc}",
            )

        if not matches:
            return PlanDecision(
                strategy=AssertionStrategy.GENERATED_FROM_SCRATCH,
                reason="工具箱无过阈值的匹配模板",
            )

        best = matches[0]
        missing = [p for p in best.template.params if p not in (known_params or {})]
        if not missing:
            return PlanDecision(
                strategy=AssertionStrategy.TEMPLATE_LOOKUP,
                template=best.template,
                score=best.score,
                reason="模板命中且参数齐备，直接引用（不调用 LLM）",
            )
        return PlanDecision(
            strategy=AssertionStrategy.TEMPLATE_INHERIT,
            template=best.template,
            score=best.score,
            reason=f"模板命中但需要参数化，缺少：{missing}",
        )

    # ------------------------------------------------------------------ #
    # 三条生成路径
    # ------------------------------------------------------------------ #

    def _build_from_template(
        self,
        assertion_id: str,
        case: TestCase,
        decision: PlanDecision,
        params: dict[str, str],
    ) -> AssertionSpec | None:
        """`template_lookup`：纯渲染，不调用 LLM。语法不过时返回 None 由调用方回退。"""
        assert decision.template is not None  # decide_strategy 保证
        meta = decision.template
        try:
            script = self._toolbox.render(meta, params)
        except ToolboxError as exc:
            logger.error("validator_template_render_failed", template=meta.template, error=str(exc))
            return None

        check = check_script_syntax(script, meta.language)
        if not check.ok:
            logger.error(
                "validator_template_syntax_failed",
                template=meta.template,
                detail=check.detail,
            )
            return None

        return AssertionSpec(
            assertion_id=assertion_id,
            case_id=case.case_id,
            strategy=AssertionStrategy.TEMPLATE_LOOKUP,
            template_ref=self._toolbox.template_ref(meta.template),
            script_path=self._script_path(assertion_id, meta.language),
            language=meta.language,
            script_content=script,
            created_at=datetime.now(UTC),
        )

    async def _generate_with_repair(
        self,
        assertion_id: str,
        case: TestCase,
        skill: SkillDefinition,
        decision: PlanDecision,
    ) -> AssertionSpec:
        """`template_inherit` / `generated_from_scratch`：生成 -> 静态检查 -> 失败重试。

        重试次数由 `ValidatorSettings.max_script_repair_retries` 控制（第 6 节要求
        最多 2 次），耗尽后降级为 `strategy=NONE`——**不让流水线因为脚本生成失败
        而整体中断**，只在报告里留下"断言生成失败"的标记。
        """
        max_retries = get_settings().validator.max_script_repair_retries
        base_prompt = self._render_prompt(assertion_id, case, skill, decision)
        prompt = base_prompt
        last_detail = ""

        for attempt in range(max_retries + 1):
            try:
                draft = await self._call_llm(prompt, ScriptDraft, system=_SYSTEM_PROMPT)
            except AgentError as exc:
                # 结构化输出耗尽重试也属于"生成失败"，同样走降级而不是把异常抛给
                # 节点层——否则一条用例的断言生成失败会带崩整个维度。
                last_detail = f"结构化输出失败：{exc}"
                break

            check = self._check_draft(draft)
            if check.ok:
                spec = AssertionSpec(
                    assertion_id=assertion_id,
                    case_id=case.case_id,
                    strategy=decision.strategy,
                    template_ref=(
                        self._toolbox.template_ref(decision.template.template)
                        if decision.template is not None
                        else None
                    ),
                    script_path=self._script_path(assertion_id, draft.language),
                    language=draft.language,
                    script_content=draft.script,
                    created_at=datetime.now(UTC),
                )
                logger.info(
                    "validator_script_generated",
                    case_id=case.case_id,
                    assertion_id=assertion_id,
                    strategy=decision.strategy.value,
                    language=draft.language,
                    attempt=attempt,
                    static_check=check.status.value,
                    false_positive_risk=draft.false_positive_risk[:200],
                )
                return await self._finalize(spec)

            last_detail = check.detail
            logger.warning(
                "validator_script_syntax_failed",
                case_id=case.case_id,
                attempt=attempt,
                max_retries=max_retries,
                detail=check.detail[:500],
            )
            prompt = self._repair_script_prompt(base_prompt, draft, check)

        return await self._finalize_none(
            assertion_id,
            case,
            reason=f"断言生成失败（连续 {max_retries + 1} 次未通过静态检查）：{last_detail}",
            degraded=True,
        )

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _check_draft(draft: ScriptDraft) -> StaticCheckResult:
        return check_script_syntax(draft.script, draft.language)

    def _render_prompt(
        self,
        assertion_id: str,
        case: TestCase,
        skill: SkillDefinition,
        decision: PlanDecision,
    ) -> str:
        common = {
            "skill": skill,
            "case": case,
            "skill_body_excerpt": self._excerpt(skill.body_markdown),
            "script_path": self._script_path(assertion_id, LANGUAGE_PYTHON),
        }
        if (
            decision.strategy is AssertionStrategy.TEMPLATE_INHERIT
            and decision.template is not None
        ):
            return _ENV.get_template("inherit_template.jinja").render(
                **common,
                template=decision.template,
                template_source=self._toolbox.read_template(decision.template.template),
                score=decision.score,
            )
        return _ENV.get_template("generate_script.jinja").render(**common)

    @staticmethod
    def _repair_script_prompt(
        base_prompt: str, draft: ScriptDraft, check: StaticCheckResult
    ) -> str:
        """把语法错误连同上一版脚本一起回灌——只说"错了"模型往往原样再写一遍。"""
        return (
            f"{base_prompt}\n\n---\n"
            "你上一次产出的脚本没有通过本地静态语法检查，内容如下：\n\n"
            f"```{draft.language}\n{draft.script}\n```\n\n"
            f"检查器报告：{check.detail}\n\n"
            "请修正后重新输出完整脚本（仍然只输出 JSON）。如果反复出错，"
            "换一种更简单的写法，不要保留导致语法错误的结构。"
        )

    @staticmethod
    def _build_query(case: TestCase, skill: SkillDefinition) -> str:
        """工具箱检索的查询文本：期望产出 + 用例指令 + Skill 意图。

        `expected_output` 放在最前面：模板要匹配的是"要验什么"，用例指令与 Skill
        描述只是补充语境。
        """
        return "\n".join(
            [
                case.expected_output or "",
                case.prompt,
                skill.description,
            ]
        )

    @staticmethod
    def _excerpt(text: str, limit: int = _SKILL_BODY_EXCERPT_CHARS) -> str:
        if len(text) <= limit:
            return text
        return f"{text[:limit]}\n...[已截断，原文共 {len(text)} 字符]"

    @staticmethod
    def _script_path(assertion_id: str, language: str) -> str:
        suffix = ".sh" if language == LANGUAGE_BASH else ".py"
        directory = get_settings().validator.sandbox_script_dir.rstrip("/")
        return f"{directory}/{assertion_id}{suffix}"

    async def _finalize(self, spec: AssertionSpec) -> AssertionSpec:
        if self._persist:
            await self._repo.save_spec(spec)
        return spec

    async def _finalize_none(
        self, assertion_id: str, case: TestCase, *, reason: str, degraded: bool
    ) -> AssertionSpec:
        spec = AssertionSpec(
            assertion_id=assertion_id,
            case_id=case.case_id,
            strategy=AssertionStrategy.NONE,
            failure_reason=reason,
            created_at=datetime.now(UTC),
        )
        log = logger.warning if degraded else logger.info
        log("validator_assertion_none", case_id=case.case_id, degraded=degraded, reason=reason)
        return await self._finalize(spec)


__all__ = ["TemplateMetadata", "ValidatorAgent"]
