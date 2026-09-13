"""模块十：多技能并发加载与上下文冲突防范评测的节点实现（docs/dev/20）。

```
prepare_multi_skill_context（加载干扰包 + 基石 Skill 引用，补齐 MULTI_SKILL 用例）
      ↓
namespace_pollution_static_scan（纯代码：工具命名冲突）
      ↓
   ┌──────────────────────────┬───────────────────────────────┬──────────────────────────────┐
   ↓                          ↓                               ↓
cross_trigger_interference_  instruction_antagonism_and_     context_exhaustion_attention_
probe（单测 vs 并发）          semantic_flow_probe（复合用例）   decay_probe（Gotchas 探针）
   └──────────────────────────┴───────────────────────────────┴──────────────────────────────┘
                                      ↓
                  role_collision_and_temporal_static_scan（角色冲突 + 时序扰动）
                                      ↓
                  core_skill_regression_gate（基石回归熔断，唯一阻断项）
                                      ↓
                  finalize_dimension_report（聚合 + 深度冲突告警）
```

本维度把评测环境从"单一技能的理想真空"切换到"多技能共存的嘈杂环境"。组合爆炸由
**固定的基准干扰包**控制（每次只与这一个包对抗，O(1)），每个真实起沙箱的探测另有
独立条数上限（`MultiSkillSettings`）。

## 相对 docs/dev/20 正文的实现修正（照抄正文会踩坑）

1. **判定经 Judge**：正文在节点里 if/else 追加 findings，违反 docs/dev/interfaces/08
   第 0 节铁律；这里七条确定性判定注册为量化规则（`rules.py`），三条语义判定走
   `judgmental_verdict()`。
2. **劫持要有单测基线**：正文只跑并发一臂，"没触发"可能本来就是模块一的问题。这里
   同一条用例在号段 230/231 上各跑一次单测与并发，只有"单测触发、并发不触发"才算劫持。
3. **"加载了哪个 Skill"要归因**：挂载干扰包后 `loaded_skill_md` 的兜底判定会被背景技能
   的 SKILL.md 误导（`executors/skill_attribution.py`）；背景过触发也靠同一份归因观测。
4. **失败态 Trace 不是证据**：沙箱超时/故障返回的 `loaded_skill_md=False` 会凭空制造
   劫持与熔断；这里记为"证据不足"交人工。唯一例外是时序扰动——参照臂在同一环境下
   健康时，打乱顺序后崩溃本身就是被测现象（报告里注明需人工排除基础设施故障）。
5. **注意力衰减要判"这次执行守没守"**：正文想复用模块八的 `does_case_probe_constraint`，
   但那个方法判的是题面、看不到 Trace；这里新增 `negative_constraint_adherence` 模板。
6. **基石熔断要比基线**：正文只看"并发触发率 < 0.8"，会把核心 Skill 的既有弱点算到
   本次提交头上；这里要求"跌破下限且比独立执行更差"（`rules.py` 模块头）。
7. **节点只返回增量**：正文 `{**state, ...}` 会让 add-reducer 字段翻倍。
8. **干扰包 / 基石未配置时如实报告**：探测记 `skipped`，维度记 NEEDS_HUMAN_REVIEW——
   不是 PASS（那等于悄悄关掉维度），也不抛异常（其余维度照常出结论）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Literal, cast

from pydantic import BaseModel, Field

from skill_evaluate.agents.analyzer.ablation_lexicon import LexiconKind, scan_lexicon
from skill_evaluate.agents.judge.golden_injector import is_golden_subject
from skill_evaluate.errors import GenerationError, PersistenceError, PipelineSuspended
from skill_evaluate.executors.base import ExecutionRequest
from skill_evaluate.executors.comparison import is_conclusive_trace
from skill_evaluate.executors.skill_attribution import attribute_skill_loads
from skill_evaluate.logging import get_logger
from skill_evaluate.nodes.instruction_control.trace_digest import (
    format_actions_for_review,
    format_final_response,
)
from skill_evaluate.nodes.multi_skill import probes, rules
from skill_evaluate.nodes.multi_skill.deps import (
    ALERT_TYPE_DEEP_CONFLICT,
    MULTI_SKILL_CRITICALITY,
    TEMPLATE_NEGATIVE_CONSTRAINT_ADHERENCE,
    TEMPLATE_ROLE_PERSONA_CONFLICT,
    TEMPLATE_SEMANTIC_FLOW_FRICTION,
    MultiSkillDeps,
)
from skill_evaluate.nodes.multi_skill.state import (
    DIMENSION,
    KEY_ALERT_DISPATCHED,
    KEY_ANTAGONISM_OUTCOME,
    KEY_ATTENTION_OUTCOME,
    KEY_CASE_IDS,
    KEY_CONTEXT_NOTES,
    KEY_CORE_REGRESSION_OUTCOME,
    KEY_CORE_SKILL_REFS,
    KEY_HIJACK_OUTCOME,
    KEY_NAMESPACE_OUTCOME,
    KEY_NOISE_PACK_REFS,
    KEY_ROLE_OUTCOME,
    KEY_SUITE_STALENESS_WARNING,
    KEY_TEMPORAL_OUTCOME,
    NODE_PREFIX,
    MultiSkillState,
)
from skill_evaluate.observability.alerts import dispatch_alert
from skill_evaluate.state.capability import NegativeConstraint
from skill_evaluate.state.enums import DatasetSplit, JudgeVerdictStatus, TestCaseCategory
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase
from skill_evaluate.state.trace import (
    RUN_INDEX_MULTI_SKILL_ANTAGONISM,
    RUN_INDEX_MULTI_SKILL_ATTENTION_CROWDED,
    RUN_INDEX_MULTI_SKILL_ATTENTION_SOLO,
    RUN_INDEX_MULTI_SKILL_CORE_BASELINE,
    RUN_INDEX_MULTI_SKILL_CORE_CROWDED,
    RUN_INDEX_MULTI_SKILL_HIJACK_CROWDED,
    RUN_INDEX_MULTI_SKILL_HIJACK_SOLO,
    RUN_INDEX_MULTI_SKILL_TEMPORAL,
    ExecutionTrace,
)

logger = get_logger(component=DIMENSION)

NODE_NAMES = {
    "prepare_multi_skill_context": f"{NODE_PREFIX}.prepare_multi_skill_context",
    "namespace_pollution_static_scan": f"{NODE_PREFIX}.namespace_pollution_static_scan",
    "cross_trigger_interference_probe": f"{NODE_PREFIX}.cross_trigger_interference_probe",
    "instruction_antagonism_and_semantic_flow_probe": (
        f"{NODE_PREFIX}.instruction_antagonism_and_semantic_flow_probe"
    ),
    "context_exhaustion_attention_decay_probe": (
        f"{NODE_PREFIX}.context_exhaustion_attention_decay_probe"
    ),
    "role_collision_and_temporal_static_scan": (
        f"{NODE_PREFIX}.role_collision_and_temporal_static_scan"
    ),
    "core_skill_regression_gate": f"{NODE_PREFIX}.core_skill_regression_gate",
    "finalize_dimension_report": f"{NODE_PREFIX}.finalize_dimension_report",
}

ENTRY_NODE = NODE_NAMES["prepare_multi_skill_context"]
TERMINAL_NODE = NODE_NAMES["finalize_dimension_report"]
# 三条并行动态探测支路（主图装配时 fan-out / fan-in 用）。
PROBE_NODES: tuple[str, ...] = (
    NODE_NAMES["cross_trigger_interference_probe"],
    NODE_NAMES["instruction_antagonism_and_semantic_flow_probe"],
    NODE_NAMES["context_exhaustion_attention_decay_probe"],
)

# **只有基石回归熔断阻断合并**（docs/dev/20 第 11 节）：它的因果方向清晰——新 Skill 的
# 引入让既有核心 Skill 变差，责任明确。其余冲突现象（劫持、拮抗、衰减、拓扑脆弱、命名
# 污染）大多需要人判断"是干扰包的问题还是被测 Skill 的问题"，走告警 + 人工审查。
# 刻意是常量而不是配置项：改阻断策略应当留下代码评审记录。
BLOCKING_OUTCOME_KEYS: frozenset[str] = frozenset({KEY_CORE_REGRESSION_OUTCOME})

# 架构文档"强制并发加载 3 到 5 个标准的干扰技能"。规模不在此区间只提示，不阻止运行：
# 运维侧暂时只维护了 2 个干扰技能时，跑出一份"覆盖冲突类型不全"的报告仍好过不跑。
RECOMMENDED_NOISE_PACK_SIZE = (3, 5)

REASONING_EXCERPT_CHARS = 200

ProbeStatus = Literal["completed", "not_applicable", "skipped"]


class ProbeOutcome(BaseModel):
    """一条探测（或一项静态审查）的结论摘要（进图状态用）。

    `status` 三态决定维度级状态（与模块九同一口径）：

    - `completed`：跑完了，冲突发现在 `findings` 里；
    - `not_applicable`：这项检测对这份 Skill 没有意义（没有负向约束、没有可打乱的
      多步任务）——正常结论，不需要人看；
    - `skipped`：本该跑但没跑成（干扰包未配置、用例为空、能力树缺失）——必须让人看见。
    """

    probe: str
    status: ProbeStatus
    note: str | None = None
    findings: list[str] = Field(default_factory=list)  # 冲突发现：计入 FAIL（基石熔断计入阻断）
    details: list[str] = Field(default_factory=list)  # 说明行：统计、参照臂失败、建议项，不影响状态
    inconclusive_ids: list[str] = Field(default_factory=list)  # 证据不足的用例 / 核心 Skill
    verdict_ids: list[str] = Field(default_factory=list)  # 本探测归档的判定
    # 指令拮抗专用：原顺序下执行健康（有效执行、目标已加载、无死锁）的用例，时序扰动只从这里取。
    healthy_case_ids: list[str] = Field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        return self.status == "skipped" or bool(self.inconclusive_ids)


class _JudgmentalResult(BaseModel):
    """一次裁量判定收敛后的结果。`status is None` = 被黄金盲测占用，本次没有结论。"""

    status: JudgeVerdictStatus | None
    verdict_id: str | None = None
    reasoning_excerpt: str = ""


def _skill_ref(skill: SkillDefinition) -> dict[str, str]:
    return {"skill_id": skill.skill_id, "version_ref": skill.version_ref}


def _eligible(case: TestCase) -> bool:
    """冷数据区（模块七降级）的用例不参与本维度：它们已经不在活跃用例集的语义里。"""
    return case.split is not DatasetSplit.COLD


class MultiSkillPipeline:
    """模块十的八个节点。做成类是为了让依赖注入只发生一次（构造时）。"""

    def __init__(self, deps: MultiSkillDeps | None = None) -> None:
        self.deps = deps or MultiSkillDeps()
        MultiSkillDeps.assert_backend_routing()

    # ------------------------------------------------------------------ #
    # 1. prepare_multi_skill_context
    # ------------------------------------------------------------------ #

    async def prepare_multi_skill_context(self, state: MultiSkillState) -> dict[str, object]:
        """加载干扰包与基石 Skill 的引用，补齐 MULTI_SKILL 复合用例（docs/dev/20 第 4 节）。

        - 干扰包 / 基石 Skill 按 skill_id 取**最新入库版本**；未入库的忽略并写说明，
          与被测 Skill 同名的剔除（拿自己当自己的干扰背景没有意义）。
        - MULTI_SKILL 用例走 `ensure_test_suite(extra_categories=...)` 的 **REUSE** 语义：
          一条都没有才出题。干扰包为空时条数算成 0（没有协作对象可写，出不来合格的题）。
        - 出题失败（`GenerationError`）不中断：本维度除熔断外都不阻断，为补一批复合用例
          掀掉整条流水线不成比例。拮抗/时序两项因此记 `skipped`，原因进报告。
        """
        run_id = str(state["run_id"])
        target = await self._load_target(state)
        settings = self.deps.settings()
        notes: list[str] = []

        noise_pack = await self._load_library_skills(
            settings.noise_pack_skill_ids, target=target, label="基准干扰包", notes=notes
        )
        core_skills = await self._load_library_skills(
            settings.core_skill_ids, target=target, label="基石 Skill", notes=notes
        )
        low, high = RECOMMENDED_NOISE_PACK_SIZE
        if noise_pack and not low <= len(noise_pack) <= high:
            notes.append(
                f"基准干扰包当前包含 {len(noise_pack)} 个技能，建议 {low}~{high} 个以覆盖"
                "功能混淆 / 角色冲突 / 输出格式冲突等典型冲突类型。"
            )

        update: dict[str, object] = {}
        suite_version_id = state.get("active_suite_version_id")
        staleness: str | None = None
        count = settings.multi_skill_case_count if noise_pack else 0
        try:
            result = await self.deps.suite_service().ensure_test_suite(
                target,
                extra_categories=[TestCaseCategory.MULTI_SKILL],
                category_counts={TestCaseCategory.MULTI_SKILL: count},
                extra_triggered_by="multi_skill_bootstrap",
                background_skills=noise_pack,
            )
            suite_version_id = result.suite_version.suite_version_id
            staleness = result.staleness_warning
            update["active_suite_version_id"] = suite_version_id
        except GenerationError as exc:
            notes.append(f"MULTI_SKILL 复合用例生成失败，指令拮抗与时序扰动探测将跳过：{exc}")
            if not suite_version_id:
                active = await self.deps.test_suite_repository.get_active_version(target.skill_id)
                suite_version_id = active.suite_version_id if active else None

        case_ids: list[str] = []
        if suite_version_id:
            cases = await self.deps.test_case_repository.list_by_categories(
                suite_version_id, [TestCaseCategory.MULTI_SKILL]
            )
            case_ids = sorted(case.case_id for case in cases if _eligible(case))

        logger.info(
            "multi_skill_context_prepared",
            run_id=run_id,
            node_name=ENTRY_NODE,
            noise_pack=[s.skill_id for s in noise_pack],
            core_skills=[s.skill_id for s in core_skills],
            suite_version_id=suite_version_id,
            multi_skill_cases=len(case_ids),
        )
        update.update(
            {
                KEY_NOISE_PACK_REFS: [_skill_ref(s) for s in noise_pack],
                KEY_CORE_SKILL_REFS: [_skill_ref(s) for s in core_skills],
                KEY_CONTEXT_NOTES: notes,
                KEY_CASE_IDS: case_ids,
                KEY_SUITE_STALENESS_WARNING: staleness,
            }
        )
        return update

    async def _load_library_skills(
        self,
        skill_ids: Sequence[str],
        *,
        target: SkillDefinition,
        label: str,
        notes: list[str],
    ) -> list[SkillDefinition]:
        loaded: list[SkillDefinition] = []
        for skill_id in dict.fromkeys(skill_ids):  # 去重且保序
            if skill_id == target.skill_id:
                notes.append(f"{label}中包含被测 Skill 自身（{skill_id}），已剔除。")
                continue
            skill = await self.deps.skill_repository.get_latest(skill_id)
            if skill is None:
                notes.append(f"{label}中的 {skill_id!r} 尚未入库（SkillRepository），已忽略。")
                continue
            loaded.append(skill)
        return loaded

    # ------------------------------------------------------------------ #
    # 2. namespace_pollution_static_scan
    # ------------------------------------------------------------------ #

    async def namespace_pollution_static_scan(self, state: MultiSkillState) -> dict[str, object]:
        """工具命名冲突（纯代码，docs/dev/20 第 5 节）。成本最低，排在全部动态探测之前。

        若命名已冲突，后续动态探测里的很多现象（劫持、交替报错）本质上是它的直接后果；
        先暴露出来，报告里就能把"根因"和"症状"关联着看。

        只有**涉及被测 Skill** 的冲突计入发现；干扰包内部彼此撞名不是本次提交的责任，
        写进说明行。未前缀化的工具名只作建议，不判 FAIL。
        """
        probe = NODE_NAMES["namespace_pollution_static_scan"]
        target = await self._load_target(state)
        noise_pack = await self._load_refs(state, KEY_NOISE_PACK_REFS)
        if not noise_pack:
            return {KEY_NAMESPACE_OUTCOME: self._no_noise_pack(probe).model_dump()}

        collisions = probes.find_tool_name_collisions([target, *noise_pack])
        involving = [c for c in collisions if c.involves(target.skill_id)]
        verdict = self.deps.judge().quantitative_verdict(
            subject_id=f"multi_skill_namespace:{target.skill_id}",
            rule_name=rules.RULE_NAMESPACE_COLLISION,
            inputs={
                "colliding_tool_count": len(involving),
                "colliding_tools": [c.tool_name for c in involving],
            },
        )
        outcome = ProbeOutcome(probe=probe, status="completed")
        if verdict.status is JudgeVerdictStatus.FAIL:
            await self._archive(outcome, verdict)
            for collision in involving:
                others = [sid for sid in collision.skill_ids if sid != target.skill_id]
                outcome.findings.append(
                    f"[命名污染] 工具名 {collision.tool_name!r} 同时被被测 Skill 与 {others} 暴露，"
                    "Agent 调用时实际命中哪个取决于加载顺序（建议前缀化，如 "
                    f"{probes.namespace_prefixes(target)[0]}_{collision.tool_name}）"
                )
        for collision in collisions:
            if not collision.involves(target.skill_id):
                outcome.details.append(
                    f"干扰包内部工具名冲突（不计入本 Skill）：{collision.tool_name!r} ← "
                    f"{list(collision.skill_ids)}"
                )
        unprefixed = probes.unprefixed_tools(target)
        if unprefixed:
            outcome.details.append(
                f"建议项：以下工具名未带命名空间前缀 {list(probes.namespace_prefixes(target))}，"
                f"技能库扩张后易发生冲突：{unprefixed}"
            )
        return {
            "judge_verdict_ids": list(outcome.verdict_ids),
            KEY_NAMESPACE_OUTCOME: outcome.model_dump(),
        }

    # ------------------------------------------------------------------ #
    # 3. cross_trigger_interference_probe
    # ------------------------------------------------------------------ #

    async def cross_trigger_interference_probe(self, state: MultiSkillState) -> dict[str, object]:
        """触发劫持与背景过触发（docs/dev/20 第 6 节）。

        取 POSITIVE 用例前 N 条（按 case_id 排序，确定性、非全量），每条跑单测与并发
        两臂（并发执行，共用信号量）：

        - **劫持**：单测加载了目标、并发时没加载；
        - **背景过触发**：并发执行里干扰技能被加载——这是目标 Skill 自己的正向任务，
          干扰技能本不该被卷进来（与单测结果无关，单独判）。
        """
        probe = NODE_NAMES["cross_trigger_interference_probe"]
        noise_pack = await self._load_refs(state, KEY_NOISE_PACK_REFS)
        if not noise_pack:
            return {KEY_HIJACK_OUTCOME: self._no_noise_pack(probe).model_dump()}
        cases = await self._suite_cases(state, [TestCaseCategory.POSITIVE])
        cases = cases[: self.deps.settings().max_hijack_probe_cases]
        if not cases:
            outcome = ProbeOutcome(
                probe=probe,
                status="skipped",
                note="活跃用例集中没有可用的 POSITIVE 用例，触发劫持探测未执行。",
            )
            return {KEY_HIJACK_OUTCOME: outcome.model_dump()}

        run_id = str(state["run_id"])
        target = await self._load_target(state)
        solo, crowded = await asyncio.gather(
            self._execute_all(
                run_id, target, cases, run_index=RUN_INDEX_MULTI_SKILL_HIJACK_SOLO, background=[]
            ),
            self._execute_all(
                run_id,
                target,
                cases,
                run_index=RUN_INDEX_MULTI_SKILL_HIJACK_CROWDED,
                background=noise_pack,
            ),
        )
        judge = self.deps.judge()
        outcome = ProbeOutcome(probe=probe, status="completed")
        reference_failed: list[str] = []
        for case in cases:
            solo_trace, crowded_trace = solo[case.case_id], crowded[case.case_id]
            solo_attr = attribute_skill_loads(solo_trace, target=target, background=[])
            crowded_attr = attribute_skill_loads(
                crowded_trace, target=target, background=noise_pack
            )
            if not (
                is_conclusive_trace(solo_trace)
                and is_conclusive_trace(crowded_trace)
                and not crowded_attr.contradictory
            ):
                outcome.inconclusive_ids.append(case.case_id)
                continue

            hijack = judge.quantitative_verdict(
                subject_id=f"multi_skill_hijack:{case.case_id}",
                rule_name=rules.RULE_TRIGGER_HIJACK,
                inputs={
                    "reference_ok": int(solo_attr.target_loaded),
                    "variant_ok": int(crowded_attr.target_loaded),
                },
            )
            if hijack.status is JudgeVerdictStatus.FAIL:
                await self._archive(outcome, hijack)
                outcome.findings.append(
                    f"[劫持] {case.case_id} 单测能触发目标 Skill，但挂载基准干扰包后未能触发"
                )
            elif not solo_attr.target_loaded:
                reference_failed.append(case.case_id)

            overtrigger = judge.quantitative_verdict(
                subject_id=f"multi_skill_overtrigger:{case.case_id}",
                rule_name=rules.RULE_BACKGROUND_OVERTRIGGER,
                inputs={
                    "background_loaded_count": len(crowded_attr.background_loaded_ids),
                    "background_loaded_ids": list(crowded_attr.background_loaded_ids),
                },
            )
            if overtrigger.status is JudgeVerdictStatus.FAIL:
                await self._archive(outcome, overtrigger)
                outcome.findings.append(
                    f"[背景过触发] {case.case_id} 执行中干扰技能 "
                    f"{list(crowded_attr.background_loaded_ids)} 被意外激活（description 语义重叠）"
                )

        outcome.details.append(
            f"触发劫持：探测 {len(cases)} 条正向用例，证据不足 {len(outcome.inconclusive_ids)} 条"
        )
        if reference_failed:
            outcome.details.append(
                f"以下用例单测时就未触发目标 Skill（属于触发准确度问题，不计入本维度）：{reference_failed}"
            )
        if outcome.inconclusive_ids:
            outcome.details.append(
                f"以下用例至少一臂没有有效执行证据（沙箱超时/故障，或加载归因矛盾），请人工确认："
                f"{outcome.inconclusive_ids}"
            )
        return self._probe_update(KEY_HIJACK_OUTCOME, outcome, solo, crowded)

    # ------------------------------------------------------------------ #
    # 4. instruction_antagonism_and_semantic_flow_probe
    # ------------------------------------------------------------------ #

    async def instruction_antagonism_and_semantic_flow_probe(
        self, state: MultiSkillState
    ) -> dict[str, object]:
        """指令拮抗（死锁）与语义断层（docs/dev/20 第 7 节）。

        同一条并发 Trace 走两条判定路径——"量化优先、裁量兜底"：

        - **死锁**：高频交替报错是可计数的轨迹形状（`probes.count_error_ping_pong`），走量化规则；
        - **语义断层**："是否为了格式转换浪费了大量步骤"需要语义理解，走 `semantic_flow_friction`。

        顺带记下"原顺序下执行健康"的用例，供时序扰动挑选参照（第 9 节）。
        """
        probe = NODE_NAMES["instruction_antagonism_and_semantic_flow_probe"]
        noise_pack = await self._load_refs(state, KEY_NOISE_PACK_REFS)
        if not noise_pack:
            return {KEY_ANTAGONISM_OUTCOME: self._no_noise_pack(probe).model_dump()}
        settings = self.deps.settings()
        cases = (await self._cases_by_ids(state, KEY_CASE_IDS))[
            : settings.max_antagonism_probe_cases
        ]
        if not cases:
            outcome = ProbeOutcome(
                probe=probe,
                status="skipped",
                note="没有可用的 MULTI_SKILL 复合用例（生成失败或条数被配置为 0），指令拮抗探测未执行。",
            )
            return {KEY_ANTAGONISM_OUTCOME: outcome.model_dump()}

        run_id = str(state["run_id"])
        target = await self._load_target(state)
        traces = await self._execute_all(
            run_id, target, cases, run_index=RUN_INDEX_MULTI_SKILL_ANTAGONISM, background=noise_pack
        )
        judge = self.deps.judge()
        background_text = probes.format_background_skills(noise_pack)
        outcome = ProbeOutcome(probe=probe, status="completed")
        absent: list[str] = []
        for case in cases:
            trace = traces[case.case_id]
            if not is_conclusive_trace(trace):
                # 超时本身可能就是死锁的表现（无穷尽重试直到墙钟），但失败态 Trace 已丢失
                # 轨迹，无从计数——交人工，不替它下结论。
                outcome.inconclusive_ids.append(case.case_id)
                continue

            ping_pong = probes.count_error_ping_pong(trace.actions)
            deadlock = judge.quantitative_verdict(
                subject_id=f"multi_skill_deadlock:{case.case_id}",
                rule_name=rules.RULE_INSTRUCTION_DEADLOCK,
                inputs={"ping_pong_count": ping_pong, "threshold": settings.deadlock_min_ping_pong},
            )
            if deadlock.status is JudgeVerdictStatus.FAIL:
                await self._archive(outcome, deadlock)
                outcome.findings.append(
                    f"[死锁] {case.case_id} 检测到 {ping_pong} 次高频交替报错，疑似指令拮抗导致的验证反馈循环"
                )

            friction = await self._judgmental(
                subject_id=f"multi_skill_flow:{case.case_id}",
                template_key=TEMPLATE_SEMANTIC_FLOW_FRICTION,
                content={
                    "prompt": case.prompt,
                    "background_skills": background_text,
                    "actions": format_actions_for_review(trace),
                },
                node_name=probe,
            )
            if friction.verdict_id:
                outcome.verdict_ids.append(friction.verdict_id)
            if friction.status is JudgeVerdictStatus.FAIL:
                outcome.findings.append(
                    f"[语义断层] {case.case_id}：{friction.reasoning_excerpt}"
                    "（建议在 SKILL.md 中补充标准化的中间态数据模板）"
                )
            elif friction.status is None:
                outcome.details.append(
                    f"{case.case_id} 的语义断层判定被黄金基准盲测占用，本次无结论"
                )

            attribution = attribute_skill_loads(trace, target=target, background=noise_pack)
            if not attribution.target_loaded:
                absent.append(case.case_id)
            elif deadlock.status is JudgeVerdictStatus.PASS and not attribution.contradictory:
                outcome.healthy_case_ids.append(case.case_id)

        outcome.details.append(
            f"指令拮抗：并发执行 {len(cases)} 条复合用例，证据不足 {len(outcome.inconclusive_ids)} 条"
        )
        if absent:
            outcome.details.append(
                f"以下复合用例执行中目标 Skill 未被加载（协作缺席，劫持探测会给出因果结论）：{absent}"
            )
        if outcome.inconclusive_ids:
            outcome.details.append(
                "以下复合用例没有有效执行证据（沙箱超时/故障；超时可能正是死锁的表现），请人工查看："
                f"{outcome.inconclusive_ids}"
            )
        return self._probe_update(KEY_ANTAGONISM_OUTCOME, outcome, traces)

    # ------------------------------------------------------------------ #
    # 5. context_exhaustion_attention_decay_probe
    # ------------------------------------------------------------------ #

    async def context_exhaustion_attention_decay_probe(
        self, state: MultiSkillState
    ) -> dict[str, object]:
        """上下文挤兑与注意力衰减（docs/dev/20 第 8 节）。

        **复用模块八的负向约束覆盖映射**挑一条现成的 Gotchas 探针用例，不重新设计探针；
        单测与并发用的是同一条用例，对比才有意义。两次执行分别经
        `negative_constraint_adherence` 判"守没守"，再由量化规则判"是否并发才违反"。

        Token 水位只做证据强度标注（见 `MultiSkillSettings.context_flood_target_tokens`）。
        """
        probe = NODE_NAMES["context_exhaustion_attention_decay_probe"]
        noise_pack = await self._load_refs(state, KEY_NOISE_PACK_REFS)
        if not noise_pack:
            return {KEY_ATTENTION_OUTCOME: self._no_noise_pack(probe).model_dump()}

        target = await self._load_target(state)
        tree = await self.deps.capability_repository.get(target.skill_id, target.version_ref)
        if tree is None:
            outcome = ProbeOutcome(
                probe=probe,
                status="skipped",
                note="被测 Skill 当前版本没有能力树（模块六/八尚未运行），无从选取 Gotchas 探针用例。",
            )
            return {KEY_ATTENTION_OUTCOME: outcome.model_dump()}
        if not tree.negative_constraints:
            outcome = ProbeOutcome(
                probe=probe,
                status="not_applicable",
                note="SKILL.md 中没有抽取到任何负向约束（Gotchas），无需做注意力衰减探测。",
            )
            return {KEY_ATTENTION_OUTCOME: outcome.model_dump()}

        picked = await self._pick_gotcha_case(state, tree.negative_constraints)
        if picked is None:
            outcome = ProbeOutcome(
                probe=probe,
                status="skipped",
                note=(
                    f"存在 {len(tree.negative_constraints)} 条负向约束，但活跃用例集里没有任何诱导"
                    "它们的正向用例（模块八的反事实补题尚未生效？），注意力衰减探测未执行。"
                ),
            )
            return {KEY_ATTENTION_OUTCOME: outcome.model_dump()}
        constraint, case = picked

        run_id = str(state["run_id"])
        solo, crowded = await asyncio.gather(
            self._execute_all(
                run_id,
                target,
                [case],
                run_index=RUN_INDEX_MULTI_SKILL_ATTENTION_SOLO,
                background=[],
            ),
            self._execute_all(
                run_id,
                target,
                [case],
                run_index=RUN_INDEX_MULTI_SKILL_ATTENTION_CROWDED,
                background=noise_pack,
            ),
        )
        solo_trace, crowded_trace = solo[case.case_id], crowded[case.case_id]
        outcome = ProbeOutcome(probe=probe, status="completed")
        outcome.details.append(
            f"注意力衰减：以负向约束 {constraint.constraint_id}（{constraint.description}）的探针用例 "
            f"{case.case_id} 做单测/并发对照"
        )
        self._append_watermark(outcome, target, noise_pack, crowded_trace)
        update = self._probe_update(KEY_ATTENTION_OUTCOME, outcome, solo, crowded)
        if not (is_conclusive_trace(solo_trace) and is_conclusive_trace(crowded_trace)):
            outcome.inconclusive_ids.append(case.case_id)
            outcome.details.append(
                f"{case.case_id} 至少一臂没有有效执行证据（沙箱超时/故障），请人工确认"
            )
            update[KEY_ATTENTION_OUTCOME] = outcome.model_dump()
            return update

        results: list[_JudgmentalResult] = []
        for arm, trace in (("solo", solo_trace), ("crowded", crowded_trace)):
            result = await self._judgmental(
                subject_id=f"multi_skill_attention_{arm}:{case.case_id}",
                template_key=TEMPLATE_NEGATIVE_CONSTRAINT_ADHERENCE,
                content={
                    "constraint_description": constraint.description,
                    "case_prompt": case.prompt,
                    "actions": format_actions_for_review(trace),
                    "final_response": format_final_response(trace),
                },
                node_name=probe,
            )
            if result.verdict_id:
                outcome.verdict_ids.append(result.verdict_id)
            results.append(result)
        solo_result, crowded_result = results

        if solo_result.status is None or crowded_result.status is None:
            outcome.inconclusive_ids.append(case.case_id)
            outcome.details.append(f"{case.case_id} 的遵守判定被黄金基准盲测占用，本次无结论")
        else:
            decay = self.deps.judge().quantitative_verdict(
                subject_id=f"multi_skill_attention:{case.case_id}",
                rule_name=rules.RULE_ATTENTION_DECAY,
                inputs={
                    "reference_ok": int(solo_result.status is JudgeVerdictStatus.PASS),
                    "variant_ok": int(crowded_result.status is JudgeVerdictStatus.PASS),
                },
            )
            if decay.status is JudgeVerdictStatus.FAIL:
                await self._archive(outcome, decay)
                outcome.findings.append(
                    f"[注意力衰减] 单测遵守负向约束 {constraint.constraint_id}，但多技能并发时被忽略"
                    f"（Token 水位 {crowded_trace.timing.total_tokens}）：{crowded_result.reasoning_excerpt}"
                    "。建议精简该 Skill 的 Token 占用或实施渐进式披露"
                )
            elif solo_result.status is JudgeVerdictStatus.FAIL:
                outcome.details.append(
                    f"单测时就违反了负向约束 {constraint.constraint_id}（属于指令控制问题，不计入本维度）"
                )
        update["judge_verdict_ids"] = list(outcome.verdict_ids)
        update[KEY_ATTENTION_OUTCOME] = outcome.model_dump()
        return update

    async def _pick_gotcha_case(
        self, state: MultiSkillState, constraints: Sequence[NegativeConstraint]
    ) -> tuple[NegativeConstraint, TestCase] | None:
        """从模块八的覆盖映射里挑一条探针用例（确定性：约束按 id、用例按 case_id 取最小）。

        两个来源取并集：能力树上的 `covering_case_ids`（模块八判定过的），以及用例自带的
        `negative_constraint_ids` 绑定（Generator 出题时回填的）。只取活跃用例集里的
        POSITIVE 用例——反事实用例本来就是正向类别（docs/dev/interfaces/06 第 1 节）。
        """
        available = {
            case.case_id: case
            for case in await self._suite_cases(state, [TestCaseCategory.POSITIVE])
        }
        for constraint in sorted(constraints, key=lambda c: c.constraint_id):
            candidates = set(constraint.covering_case_ids) | {
                case_id
                for case_id, case in available.items()
                if constraint.constraint_id in case.negative_constraint_ids
            }
            usable = sorted(candidates & available.keys())
            if usable:
                return constraint, available[usable[0]]
        return None

    def _append_watermark(
        self,
        outcome: ProbeOutcome,
        target: SkillDefinition,
        noise_pack: Sequence[SkillDefinition],
        crowded_trace: ExecutionTrace,
    ) -> None:
        """Token 水位的证据强度标注（不影响状态）。"""
        settings = self.deps.settings()
        static_tokens = target.token_count + sum(s.token_count for s in noise_pack)
        observed = crowded_trace.timing.total_tokens
        outcome.details.append(
            f"上下文水位：并发执行实测 {observed} tokens，Skill 正文静态合计 {static_tokens} tokens，"
            f"挤兑目标阈值 {settings.context_flood_target_tokens} tokens"
        )
        if observed < settings.context_flood_target_tokens * settings.context_flood_near_ratio:
            outcome.details.append(
                "水位未逼近挤兑阈值：本次未发现注意力衰减的结论强度有限（扩充干扰包或选用更长的探针用例可提高压力）"
            )

    # ------------------------------------------------------------------ #
    # 6. role_collision_and_temporal_static_scan
    # ------------------------------------------------------------------ #

    async def role_collision_and_temporal_static_scan(
        self, state: MultiSkillState
    ) -> dict[str, object]:
        """角色分裂（静态）与时序脆弱性（动态）（docs/dev/20 第 9 节）。

        - **角色冲突**：`role_persona_conflict` 模板，输入被测 SKILL.md 与干扰包的描述 +
          从正文抽出的角色/风格预设句。词典（模块九 `ablation_lexicon`）命中的身份抬举措辞
          一并列为"风格强制降级"建议。
        - **时序扰动**：只从指令拮抗里"原顺序下执行健康"的用例中取——参照臂在同一环境
          下健康，打乱顺序后才出问题，才能归因于"顺序"。
        """
        noise_pack = await self._load_refs(state, KEY_NOISE_PACK_REFS)
        target = await self._load_target(state)
        role = await self._role_collision(state, target, noise_pack)
        temporal, traces = await self._temporal_scan(state, target, noise_pack)
        return {
            "executed_trace_ids": [trace.trace_id for trace in traces],
            "judge_verdict_ids": [*role.verdict_ids, *temporal.verdict_ids],
            KEY_ROLE_OUTCOME: role.model_dump(),
            KEY_TEMPORAL_OUTCOME: temporal.model_dump(),
        }

    async def _role_collision(
        self, state: MultiSkillState, target: SkillDefinition, noise_pack: Sequence[SkillDefinition]
    ) -> ProbeOutcome:
        probe = f"{NODE_NAMES['role_collision_and_temporal_static_scan']}#role"
        if not noise_pack:
            return self._no_noise_pack(probe)
        outcome = ProbeOutcome(probe=probe, status="completed")
        result = await self._judgmental(
            subject_id=f"multi_skill_role:{target.skill_id}",
            template_key=TEMPLATE_ROLE_PERSONA_CONFLICT,
            content={
                "skill_md": target.body_markdown,
                "noise_pack_descriptions": probes.format_noise_pack_for_review(noise_pack),
            },
            node_name=NODE_NAMES["role_collision_and_temporal_static_scan"],
        )
        if result.verdict_id:
            outcome.verdict_ids.append(result.verdict_id)
        if result.status is None:
            outcome.inconclusive_ids.append(target.skill_id)
            outcome.details.append("角色冲突审查被黄金基准盲测占用，本次无结论")
        elif result.status is JudgeVerdictStatus.FAIL:
            outcome.findings.append(f"[角色冲突] {result.reasoning_excerpt}")
        persona_hits = [
            hit
            for hit in scan_lexicon(target.body_markdown)
            if hit.kind is LexiconKind.PERSONA_FLATTERY
        ]
        if persona_hits:
            preview = "、".join(repr(hit.text) for hit in persona_hits[:5])
            outcome.details.append(
                f"风格强制降级建议：正文中有 {len(persona_hits)} 处身份抬举式预设（{preview}），"
                "多技能并发时易污染 Agent 的底层逻辑，建议改写为客观的过程指导"
            )
        return outcome

    async def _temporal_scan(
        self, state: MultiSkillState, target: SkillDefinition, noise_pack: Sequence[SkillDefinition]
    ) -> tuple[ProbeOutcome, list[ExecutionTrace]]:
        probe = f"{NODE_NAMES['role_collision_and_temporal_static_scan']}#temporal"
        if not noise_pack:
            return self._no_noise_pack(probe), []
        antagonism = _model_from_state(state, KEY_ANTAGONISM_OUTCOME, ProbeOutcome)
        if antagonism is None or antagonism.status != "completed":
            reason = antagonism.note if antagonism and antagonism.note else "指令拮抗探测未产出结果"
            return ProbeOutcome(probe=probe, status="skipped", note=f"时序扰动未执行：{reason}"), []

        settings = self.deps.settings()
        healthy = await self.deps.test_case_repository.list_by_ids(antagonism.healthy_case_ids)
        healthy = sorted(healthy, key=lambda c: c.case_id)
        shuffled_cases: list[TestCase] = []
        unshufflable: list[str] = []
        for case in healthy:
            if len(shuffled_cases) >= settings.max_temporal_probe_cases:
                break
            shuffled = probes.shuffle_step_order(
                case.prompt, seed=f"multi-skill-temporal:{case.case_id}"
            )
            if shuffled is None:
                unshufflable.append(case.case_id)
                continue
            shuffled_cases.append(case.model_copy(update={"prompt": shuffled}))

        if not shuffled_cases:
            note = (
                "原顺序下执行健康的复合用例中没有可拆分出多个步骤的题面，时序扰动不适用。"
                if healthy
                else "没有原顺序下执行健康的复合用例可作参照，时序扰动不适用。"
            )
            return ProbeOutcome(probe=probe, status="not_applicable", note=note), []

        traces = await self._execute_all(
            str(state["run_id"]),
            target,
            shuffled_cases,
            run_index=RUN_INDEX_MULTI_SKILL_TEMPORAL,
            background=noise_pack,
        )
        judge = self.deps.judge()
        outcome = ProbeOutcome(probe=probe, status="completed")
        for case in shuffled_cases:
            trace = traces[case.case_id]
            attribution = attribute_skill_loads(trace, target=target, background=noise_pack)
            if attribution.contradictory:
                outcome.inconclusive_ids.append(case.case_id)
                continue
            crashed = not is_conclusive_trace(trace)
            deadlocked = (
                probes.count_error_ping_pong(trace.actions) >= settings.deadlock_min_ping_pong
            )
            shuffled_ok = not crashed and attribution.target_loaded and not deadlocked
            verdict = judge.quantitative_verdict(
                subject_id=f"multi_skill_temporal:{case.case_id}",
                rule_name=rules.RULE_TEMPORAL_FRAGILITY,
                inputs={"reference_ok": 1, "variant_ok": int(shuffled_ok)},
            )
            if verdict.status is JudgeVerdictStatus.FAIL:
                await self._archive(outcome, verdict)
                symptom = (
                    "执行崩溃/超时"
                    if crashed
                    else "陷入交替报错"
                    if deadlocked
                    else "未触发目标 Skill"
                )
                suffix = "（若为沙箱基础设施故障请人工排除）" if crashed else ""
                outcome.findings.append(
                    f"[拓扑脆弱] {case.case_id} 打乱业务步骤顺序后{symptom}{suffix}；"
                    "建议在 SKILL.md 中补充显式的多步骤工作流清单（Checklist）约束执行顺序"
                )
        outcome.details.append(
            f"时序扰动：打乱顺序重跑 {len(shuffled_cases)} 条原顺序健康的复合用例"
        )
        if unshufflable:
            outcome.details.append(f"以下复合用例题面拆不出多个步骤，未做时序扰动：{unshufflable}")
        return outcome, [trace for trace in traces.values()]

    # ------------------------------------------------------------------ #
    # 7. core_skill_regression_gate
    # ------------------------------------------------------------------ #

    async def core_skill_regression_gate(self, state: MultiSkillState) -> dict[str, object]:
        """基石 Skill 增量回归熔断（docs/dev/20 第 10 节，架构文档"熔断拦截"）。

        角色与其他节点**相反**：被测对象是核心 Skill，`background_skills=[本 Skill]`。
        测的不是本 Skill 好不好，而是"引入它是否伤害了系统里已有的核心 Skill"。

        核心 Skill 的用例取**它自己的** active 用例集（只读，不为核心 Skill 出题——那不是
        本次提交该触发的事）；没有用例集的核心 Skill 记证据不足。
        """
        probe = NODE_NAMES["core_skill_regression_gate"]
        core_skills = await self._load_refs(state, KEY_CORE_SKILL_REFS)
        if not core_skills:
            outcome = ProbeOutcome(
                probe=probe,
                status="skipped",
                note=(
                    "未配置可用的基石 Skill（SKILLEVAL_MULTISKILL_CORE_SKILL_IDS 为空或全部未入库），"
                    "增量回归熔断未执行——这是模块十唯一的阻断闸门，请运维侧补齐。"
                ),
            )
            return {KEY_CORE_REGRESSION_OUTCOME: outcome.model_dump()}

        settings = self.deps.settings()
        target = await self._load_target(state)
        run_id = str(state["run_id"])
        outcome = ProbeOutcome(probe=probe, status="completed")

        samples: list[tuple[SkillDefinition, list[TestCase]]] = []
        for core in core_skills:
            active = await self.deps.test_suite_repository.get_active_version(core.skill_id)
            cases: list[TestCase] = []
            if active is not None:
                listed = await self.deps.test_case_repository.list_by_categories(
                    active.suite_version_id, [TestCaseCategory.POSITIVE]
                )
                cases = sorted((c for c in listed if _eligible(c)), key=lambda c: c.case_id)
                cases = cases[: settings.core_regression_sample_size]
            if not cases:
                outcome.inconclusive_ids.append(core.skill_id)
                outcome.details.append(
                    f"基石 Skill {core.skill_id!r} 没有可用的正向用例（未生成用例集？），未参与熔断判定"
                )
                continue
            samples.append((core, cases))

        arms = await asyncio.gather(
            *(
                asyncio.gather(
                    self._execute_all(
                        run_id,
                        core,
                        cases,
                        run_index=RUN_INDEX_MULTI_SKILL_CORE_BASELINE,
                        background=[],
                    ),
                    self._execute_all(
                        run_id,
                        core,
                        cases,
                        run_index=RUN_INDEX_MULTI_SKILL_CORE_CROWDED,
                        background=[target],
                    ),
                )
                for core, cases in samples
            )
        )
        trace_maps: list[dict[str, ExecutionTrace]] = []
        judge = self.deps.judge()
        for (core, cases), (baseline, crowded) in zip(samples, arms, strict=True):
            trace_maps.extend([baseline, crowded])
            counts = {
                "baseline_loaded": 0,
                "baseline_conclusive": 0,
                "crowded_loaded": 0,
                "crowded_conclusive": 0,
            }
            for case in cases:
                for arm, traces, background in (
                    ("baseline", baseline, []),
                    ("crowded", crowded, [target]),
                ):
                    trace = traces[case.case_id]
                    attribution = attribute_skill_loads(trace, target=core, background=background)
                    if not is_conclusive_trace(trace) or attribution.contradictory:
                        continue
                    counts[f"{arm}_conclusive"] += 1
                    counts[f"{arm}_loaded"] += int(attribution.target_loaded)
            if not counts["baseline_conclusive"] or not counts["crowded_conclusive"]:
                outcome.inconclusive_ids.append(core.skill_id)
                outcome.details.append(
                    f"基石 Skill {core.skill_id!r} 至少一臂没有有效执行证据（沙箱超时/故障），熔断判定无法给出，请人工确认"
                )
                continue

            baseline_rate = counts["baseline_loaded"] / counts["baseline_conclusive"]
            crowded_rate = counts["crowded_loaded"] / counts["crowded_conclusive"]
            verdict = judge.quantitative_verdict(
                subject_id=f"multi_skill_core:{core.skill_id}:{target.skill_id}",
                rule_name=rules.RULE_CORE_REGRESSION,
                inputs={**counts, "min_rate": settings.core_regression_min_rate},
            )
            outcome.details.append(
                f"基石 Skill {core.skill_id!r}：独立执行触发率 {baseline_rate:.0%}，以本 Skill 为背景 "
                f"{crowded_rate:.0%}（样本 {len(cases)} 条，安全下限 {settings.core_regression_min_rate:.0%}）"
            )
            if verdict.status is JudgeVerdictStatus.FAIL:
                await self._archive(outcome, verdict)
                outcome.findings.append(
                    f"[基石熔断] 引入本 Skill 导致核心 Skill {core.skill_id!r} 触发率由 {baseline_rate:.0%} "
                    f"跌至 {crowded_rate:.0%}（低于安全下限 {settings.core_regression_min_rate:.0%}）"
                )
            elif crowded_rate < settings.core_regression_min_rate:
                outcome.details.append(
                    f"基石 Skill {core.skill_id!r} 独立执行时触发率就低于安全下限，不归因于本 Skill（未熔断）"
                )
        return self._probe_update(KEY_CORE_REGRESSION_OUTCOME, outcome, *trace_maps)

    # ------------------------------------------------------------------ #
    # 8. finalize_dimension_report
    # ------------------------------------------------------------------ #

    async def finalize_dimension_report(self, state: MultiSkillState) -> dict[str, object]:
        """聚合全部探测，写 `dimension_results`，按需发深度冲突告警（docs/dev/20 第 11、12 节）。

        | 情形 | status | blocking |
        |---|---|---|
        | 基石熔断 | FAIL | True |
        | 其余任一冲突发现 | FAIL | False |
        | 有探测被跳过 / 证据不足 / 结果缺失 | NEEDS_HUMAN_REVIEW | False |
        | 其余 | PASS | False |

        告警条件：有基石熔断，或软性冲突发现 ≥ `deep_conflict_alert_threshold`。告警是旁路，
        通道故障不影响落库（`dispatch_alert`）；payload 带 `blocking`，docs/dev/22 据此
        决定"阻塞挂起"还是"只发通知"。`score=None`：七类性质不同的检测硬凑分数没有含义。
        """
        run_id = str(state["run_id"])
        settings = self.deps.settings()
        hard: list[str] = []
        soft: list[str] = []
        details: list[str] = []
        note_labels: dict[str, list[str]] = {}
        needs_human = False

        for note in cast("list[str] | None", state.get(KEY_CONTEXT_NOTES)) or []:
            details.append(f"准备阶段：{note}")
        staleness = cast("str | None", state.get(KEY_SUITE_STALENESS_WARNING))
        if staleness:
            details.append(staleness)

        for key, label in (
            (KEY_NAMESPACE_OUTCOME, "命名空间扫描"),
            (KEY_HIJACK_OUTCOME, "触发劫持探测"),
            (KEY_ANTAGONISM_OUTCOME, "指令拮抗探测"),
            (KEY_ATTENTION_OUTCOME, "注意力衰减探测"),
            (KEY_ROLE_OUTCOME, "角色冲突审查"),
            (KEY_TEMPORAL_OUTCOME, "时序扰动探测"),
            (KEY_CORE_REGRESSION_OUTCOME, "基石回归熔断"),
        ):
            outcome = _model_from_state(state, key, ProbeOutcome)
            if outcome is None:
                needs_human = True
                details.append(
                    f"{label}：未取到结果，请确认主图状态 schema 包含本维度私有键（{key}），"
                    "见 docs/dev/interfaces/20_multi_skill_conflict.md 第 2 节。"
                )
                continue
            (hard if key in BLOCKING_OUTCOME_KEYS else soft).extend(outcome.findings)
            needs_human = needs_human or outcome.needs_human
            if outcome.note:
                note_labels.setdefault(outcome.note, []).append(label)
            details.extend(outcome.details)

        # 同一条说明（典型是"干扰包为空"，六项探测都会写）合并成一行，列出受影响的检测项。
        details.extend(f"{' / '.join(labels)}：{note}" for note, labels in note_labels.items())
        details = list(dict.fromkeys(details))
        blocking = bool(hard)
        status = (
            JudgeVerdictStatus.FAIL
            if hard or soft
            else JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
            if needs_human
            else JudgeVerdictStatus.PASS
        )
        await self.deps.reporter().record_dimension_result(
            run_id=run_id,
            dimension=DIMENSION,
            status=status,
            score=None,
            findings=[*hard, *soft, *details],
            blocking=blocking,
        )

        alert_sent = False
        if hard or len(soft) >= settings.deep_conflict_alert_threshold:
            alert_sent = await dispatch_alert(
                self.deps.alerts(),
                alert_type=ALERT_TYPE_DEEP_CONFLICT,
                run_id=run_id,
                payload={
                    "dimension": DIMENSION,
                    "skill_id": str(state["skill_id"]),
                    "skill_version_ref": str(state["skill_version_ref"]),
                    "blocking": blocking,
                    "hard_findings": hard,
                    "soft_findings": soft,
                    "noise_pack": [
                        ref["skill_id"]
                        for ref in cast(
                            "list[dict[str, str]]", state.get(KEY_NOISE_PACK_REFS) or []
                        )
                    ],
                },
            )
        logger.info(
            "multi_skill_dimension_recorded",
            run_id=run_id,
            node_name=TERMINAL_NODE,
            status=status.value,
            blocking=blocking,
            hard=len(hard),
            soft=len(soft),
            alert_sent=alert_sent,
        )
        return {KEY_ALERT_DISPATCHED: alert_sent}

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    async def _execute_all(
        self,
        run_id: str,
        skill: SkillDefinition,
        cases: Sequence[TestCase],
        *,
        run_index: int,
        background: Sequence[SkillDefinition],
    ) -> dict[str, ExecutionTrace]:
        """把一批用例在同一个号段上并发各跑一次（共用信号量），落库 Trace。

        `background_skills` 由本维度自己构造请求而不是给 `run_arm()` 加参数——
        docs/dev/interfaces/11 第 4.2 节 / 19 第 5.1 节的约定：请求的构造是各维度语义的一部分。
        不吞 `ExecutorBackendError`（沙箱建不起来 = 评测系统自身故障）。
        """
        semaphore = self.deps.semaphore()
        timeout_s = self.deps.settings().execution_timeout_s

        async def run(case: TestCase) -> ExecutionTrace:
            request = ExecutionRequest(
                skill=skill,
                case=case,
                run_index=run_index,
                run_id=run_id,
                background_skills=list(background),
                wall_clock_timeout_s=timeout_s,
            )
            async with semaphore:
                return await self.deps.backend().execute(request)

        traces = await asyncio.gather(*(run(case) for case in cases))
        for trace in traces:
            await self.deps.trace_repository.save(trace)
        return {case.case_id: trace for case, trace in zip(cases, traces, strict=True)}

    async def _judgmental(
        self, *, subject_id: str, template_key: str, content: dict[str, str], node_name: str
    ) -> _JudgmentalResult:
        """裁量判定 + 收敛共识未达成与黄金盲测两种特殊情形（同模块九口径）。

        不吞 `JudgeFrozenError`：被冻结的裁判给出的任何结论都不该进报告。
        """
        result: JudgeVerdict | ConsensusResult = await self.deps.judge().judgmental_verdict(
            subject_id=subject_id,
            template_key=template_key,
            content=content,
            criticality=MULTI_SKILL_CRITICALITY,
        )
        if isinstance(result, ConsensusResult) and not result.consensus_reached:
            # 本维度声明 ROUTINE，正常拿不到 ConsensusResult；有人把重要度调成 CRITICAL 时
            # 这条路径会活过来。NEEDS_HUMAN_REVIEW 不允许被降级（docs/dev/08 明令禁止）。
            raise PipelineSuspended(
                f"{node_name}：{template_key} 的三副本复核未达成共识（subject_id={subject_id!r}），需人工仲裁。"
            )
        if is_golden_subject(result.subject_id):
            return _JudgmentalResult(status=None)
        if isinstance(result, ConsensusResult):
            verdict = result.verdicts[0] if result.verdicts else None
            return _JudgmentalResult(
                status=result.final_status,
                verdict_id=verdict.verdict_id if verdict else None,
                reasoning_excerpt=(verdict.reasoning if verdict else "")[:REASONING_EXCERPT_CHARS],
            )
        return _JudgmentalResult(
            status=result.status,
            verdict_id=result.verdict_id,
            reasoning_excerpt=result.reasoning[:REASONING_EXCERPT_CHARS],
        )

    async def _archive(self, outcome: ProbeOutcome, verdict: JudgeVerdict) -> None:
        """归档一条量化 FAIL 判定并记下 id。

        只归档 FAIL（与模块一/九同一口径）：通过判定没人读；且只把**已落库**的 id 写进
        `judge_verdict_ids`，免得状态里出现一批库里查不到的 id。
        """
        await self.deps.judge_repository.save_verdict(verdict)
        outcome.verdict_ids.append(verdict.verdict_id)

    @staticmethod
    def _probe_update(
        key: str, outcome: ProbeOutcome, *trace_maps: dict[str, ExecutionTrace]
    ) -> dict[str, object]:
        """探测节点的状态增量：本次产生的 trace/verdict id（add reducer）+ 自己的结果键。"""
        return {
            "executed_trace_ids": [t.trace_id for traces in trace_maps for t in traces.values()],
            "judge_verdict_ids": list(outcome.verdict_ids),
            key: outcome.model_dump(),
        }

    @staticmethod
    def _no_noise_pack(probe: str) -> ProbeOutcome:
        return ProbeOutcome(
            probe=probe,
            status="skipped",
            note=(
                "基准干扰包为空（SKILLEVAL_MULTISKILL_NOISE_PACK_SKILL_IDS 未配置或其中技能全部未入库），"
                "多技能并发探测未执行。"
            ),
        )

    async def _load_target(self, state: MultiSkillState) -> SkillDefinition:
        """取被测 Skill 的**原版**（理由同模块九：报告要与仓库里的文件对得上）。"""
        skill = await self.deps.skill_repository.get(
            str(state["skill_id"]), str(state["skill_version_ref"])
        )
        if skill is None:
            raise PersistenceError(
                f"未找到被测 Skill：skill_id={state['skill_id']!r} "
                f"version_ref={state['skill_version_ref']!r}。"
                "请先经 `ingestion.load_skill()` + `SkillRepository.save()` 入库。"
            )
        return skill

    async def _load_refs(self, state: MultiSkillState, key: str) -> list[SkillDefinition]:
        """按状态里的 (skill_id, version_ref) 引用回库取技能；回库取不到的静默跳过。

        准备节点刚确认过它们已入库，这里取不到只可能是运行中途被删——不值得为此中断
        整个维度，少一个干扰技能照样能出结论。
        """
        refs = cast("list[dict[str, str]] | None", state.get(key)) or []
        skills: list[SkillDefinition] = []
        for ref in refs:
            skill = await self.deps.skill_repository.get(ref["skill_id"], ref["version_ref"])
            if skill is not None:
                skills.append(skill)
        return skills

    async def _suite_cases(
        self, state: MultiSkillState, categories: list[TestCaseCategory]
    ) -> list[TestCase]:
        suite_version_id = state.get("active_suite_version_id")
        if not suite_version_id:
            return []
        cases = await self.deps.test_case_repository.list_by_categories(
            suite_version_id, categories
        )
        return sorted((c for c in cases if _eligible(c)), key=lambda c: c.case_id)

    async def _cases_by_ids(self, state: MultiSkillState, key: str) -> list[TestCase]:
        case_ids = [str(item) for item in cast("list[str] | None", state.get(key)) or []]
        if not case_ids:
            return []
        cases = await self.deps.test_case_repository.list_by_ids(case_ids)
        return sorted(cases, key=lambda c: c.case_id)


def _model_from_state[T: BaseModel](state: MultiSkillState, key: str, model: type[T]) -> T | None:
    """从图状态取 Pydantic 模型（Checkpoint 反序列化后可能是 dict，统一 model_validate）。"""
    value = state.get(key)
    return None if value is None else model.model_validate(value)


__all__ = [
    "BLOCKING_OUTCOME_KEYS",
    "ENTRY_NODE",
    "NODE_NAMES",
    "PROBE_NODES",
    "TERMINAL_NODE",
    "MultiSkillPipeline",
    "ProbeOutcome",
]
