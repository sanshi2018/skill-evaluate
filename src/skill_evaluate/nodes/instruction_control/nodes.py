"""模块三：指令控制度与执行效果评测的节点实现（docs/dev/13）。

```
prepare_cases
   ├────────────────────┬───────────────────────────┐
   ↓                    ↓                           ↓
ab_comparative_      control_calibration_      progressive_disclosure_
execution            static_scan               dynamic_probe
   ↓                    │                           │
trace_efficiency_       │                           │
diagnosis               │                           │
   └────────────────────┴───────────────────────────┘
                        ↓
                  collect_findings
                        ├─（训练集有可优化失败）→ optimizer_loop ─┐
                        └────────────────────────────────────────┤
                                                                 ↓
                                                finalize_dimension_report
```

这是架构文档里最复杂的一个维度：四项相对独立的子评测并行跑，最后统一聚合。

## 四条贯穿本文件的关键决策

1. **A/B 每条分支只跑 1 次**（docs/dev/13 第 4 节）。架构文档模块一要求每用例 3 次
   冗余，但 A/B 是"每用例 2 条分支 × 每分支 N 次"，N=3 会让本维度成本达到基础
   评测的 6 倍。ROI 判定关心的是"存在不存在显著差异"而不是精确的比例统计，因此
   容忍单次执行的噪音——并且**把这件事写进了裁判的 Prompt**，让它在差距不明显时
   倾向 pass，而不是让噪音变成一次"打回重构"的判决。
2. **ROI 判定声明 CRITICAL，其余声明 ROUTINE**（见 `deps.py`）。
3. **渐进式披露探查走量化规则，不走 LLM**（docs/dev/13 第 7 节）：判定依据是对
   `ExecutionTrace.actions` 的确定性扫描，扫描在 `probe.py`，规则在 `rules.py`。
4. **只有 ROI 失败与"漏读"进优化闭环**（docs/dev/13 第 8 节）。效率诊断与控制标定
   的结论更接近"写作风格建议"，自动重写容易引发架构文档反复强调的"过度杀伤力"
   副作用，交给人在报告里看过之后自行决定。

## 节点签名与返回值

与模块一/二同样的两条坑：签名必须写 `InstructionControlState`（否则私有键会被
LangGraph 静默裁掉，本维度的表现会是"跑了但收尾节点什么也看不到"），返回值只带
增量（`executed_trace_ids` / `judge_verdict_ids` 的 reducer 是 `operator.add`，
回抛整个旧状态会让 id 翻倍）。

## 关于 `collect_findings` 这个节点

docs/dev/13 正文的流程图里没有它，它是并行分叉后**汇合**的必要产物：条件路由必须
挂在某个节点上，而"要不要进优化闭环"的判断需要同时看到 A/B 与探查两条支路的结果。
把它做成一个不产生任何副作用的纯汇总节点，好过把路由挂在其中一条支路上（那样另一
条支路的失败信号会被漏掉）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from typing import cast

from pydantic import BaseModel

from skill_evaluate.agents.judge.golden_injector import is_golden_subject
from skill_evaluate.agents.optimizer.loop import LoopResult
from skill_evaluate.agents.optimizer.patch_applier import working_version_ref
from skill_evaluate.agents.optimizer.schema import ROLE_PROMPT_ENGINEER
from skill_evaluate.agents.optimizer.service import build_failure_context
from skill_evaluate.errors import ConfigurationError, PersistenceError, PipelineSuspended
from skill_evaluate.executors.base import ExecutionRequest
from skill_evaluate.logging import get_logger
from skill_evaluate.nodes.instruction_control import rules
from skill_evaluate.nodes.instruction_control.deps import (
    CALIBRATION_CRITICALITY,
    EFFICIENCY_CRITICALITY,
    ROI_CRITICALITY,
    TEMPLATE_CONTROL_CALIBRATION,
    TEMPLATE_ROI_COMPARISON,
    TEMPLATE_TRACE_EFFICIENCY,
    InstructionControlDeps,
)
from skill_evaluate.nodes.instruction_control.probe import (
    ProbeFinding,
    resolve_token_watermark,
    scan_probe_trace,
)
from skill_evaluate.nodes.instruction_control.state import (
    DIMENSION,
    KEY_AB_CASE_IDS,
    KEY_AB_PAIRS,
    KEY_APPLIED_PATCH_ID,
    KEY_CALIBRATION_OUTCOME,
    KEY_EFFICIENCY_OUTCOMES,
    KEY_FAILED_TRAIN_CASE_IDS,
    KEY_PD_CASE_IDS,
    KEY_PD_FINDINGS,
    KEY_ROI_OUTCOMES,
    KEY_SUITE_STALENESS_WARNING,
    KEY_TOKEN_WATERMARK,
    KEY_WORKING_SKILL,
    InstructionControlState,
)
from skill_evaluate.nodes.instruction_control.trace_digest import (
    format_actions_for_review,
    format_final_response,
)
from skill_evaluate.state.enums import DatasetSplit, JudgeVerdictStatus, TestCaseCategory
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase
from skill_evaluate.state.trace import (
    RUN_INDEX_AB_BASELINE,
    RUN_INDEX_AB_LOADED,
    RUN_INDEX_PD_PROBE,
    ExecutionTrace,
)

logger = get_logger(component=DIMENSION)

NODE_NAMES = {
    "prepare_cases": f"{DIMENSION}.prepare_instruction_control_cases",
    "ab_comparative_execution": f"{DIMENSION}.ab_comparative_execution",
    "trace_efficiency_diagnosis": f"{DIMENSION}.trace_efficiency_diagnosis",
    "control_calibration_static_scan": f"{DIMENSION}.control_calibration_static_scan",
    "progressive_disclosure_dynamic_probe": f"{DIMENSION}.progressive_disclosure_dynamic_probe",
    "collect_findings": f"{DIMENSION}.collect_findings",
    "optimizer_loop": f"{DIMENSION}.optimizer_loop",
    "finalize_dimension_report": f"{DIMENSION}.finalize_dimension_report",
}

ENTRY_NODE = NODE_NAMES["prepare_cases"]
TERMINAL_NODE = NODE_NAMES["finalize_dimension_report"]

# A/B 每条分支的重复次数上限。`run_index` 按 `RUN_INDEX_AB_LOADED + 2*i + arm`
# 编号（见 `_ab_run_index()`），号段到 `RUN_INDEX_PD_PROBE`（110）为止，因此最多
# 5 次。超过就会撞上探查用例的号段，把两个维度的 Trace 互相覆盖——与其让它静默
# 发生，不如在节点构造时报错。
MAX_RUN_COUNT_PER_ARM = (RUN_INDEX_PD_PROBE - RUN_INDEX_AB_LOADED) // 2

# 报告 findings 里每条判定摘录的长度。截断而不是全文入库：`JudgeVerdict` 已经带着
# 完整 reasoning 落了库（`judge_verdict_ids` 可回查），findings 是给人扫一眼用的
# 摘要列表，塞进整段推理只会让报告没法读（与模块二同一口径）。
REASONING_EXCERPT_CHARS = 200

# ROI 判定的 subject_id 前缀（docs/dev/13 第 4.1 节）。前缀而不是裸 case_id：同一条
# 用例在模块一那边已经有一条按 case_id 存的触发率判定了，不加前缀两条判定会在
# `JudgeRepository.list_verdicts(subject_id)` 里混成一堆。
SUBJECT_PREFIX_ROI = "roi:"
SUBJECT_PREFIX_EFFICIENCY = "efficiency:"
SUBJECT_PREFIX_PD_PROBE = "pd_probe:"


class JudgmentOutcome(BaseModel):
    """一次裁量判定的结论摘要（进图状态用）。

    存摘要而不是整个 `JudgeVerdict`：verdict 本体已由 Judge 侧落库，状态里再放一份
    只会让每个 Checkpoint 白背几十 KB（`state.py` 的说明）。

    `skipped_reason` 不为空表示这一项**没有产生针对本 Skill/用例的结论**——目前唯一
    的成因是被黄金基准盲测占用。它不计入通过/失败，但必须出现在报告里：少做了一项
    判定，读报告的人有权知道。

    结构与模块二的 `PeerReviewOutcome` 高度相似，但**刻意各自定义**：跨维度共享一个
    模型意味着任何一个维度想加个字段都得去动另一个维度的报告口径。等到第三个维度也
    需要同样的东西时，再考虑上提到公共层。
    """

    subject_id: str
    template_key: str
    case_id: str | None = None
    verdict_id: str | None = None
    status: JudgeVerdictStatus | None = None
    reasoning_excerpt: str = ""
    skipped_reason: str | None = None


class AbPair(BaseModel):
    """一条用例的 A/B 执行结果索引（进图状态用，只存 id 与聚合数字）。"""

    case_id: str
    loaded_trace_id: str
    baseline_trace_id: str


def _id_list(state: InstructionControlState, key: str) -> list[str]:
    """从图状态里取一串 id，缺键时返回空列表。

    `PipelineState` 是 TypedDict，用**变量**作键时静态类型会退化成 `object`
    （私有键本来也不在 TypedDict 的字段表里）。这里集中收窄一次，好过在每个调用点
    各写一行 cast。
    """
    value = cast("list[str] | None", state.get(key))
    return [str(item) for item in (value or [])]


def _dict_list(state: InstructionControlState, key: str) -> list[dict[str, object]]:
    value = cast("list[dict[str, object]] | None", state.get(key))
    return list(value or [])


def _ab_run_index(*, run_index: int, baseline: bool) -> int:
    """A/B 分支的 `run_index`。

    `execution_traces` 的唯一键是 `(case_id, run_index)`，而本维度跑的是**模块一
    用过的同一批用例**。两个维度都从 0 开始编号的话，后跑的会静默覆盖先跑的，
    并且模块一下次统计触发率时会把这里的基线分支（按定义就是"没加载 Skill"）
    算成一次没触发。号段分配表见 `state/trace.py`。
    """
    offset = RUN_INDEX_AB_BASELINE - RUN_INDEX_AB_LOADED if baseline else 0
    return RUN_INDEX_AB_LOADED + 2 * run_index + offset


class InstructionControlPipeline:
    """模块三的八个节点。做成类是为了让依赖注入只发生一次（构造时），而不是每个
    节点函数各自去拿一遍单例。

    用法（docs/dev/24 装配主图时）见 `graph.py::add_instruction_control_nodes()`。
    """

    def __init__(self, deps: InstructionControlDeps | None = None) -> None:
        self.deps = deps or InstructionControlDeps()
        # 装配期就核对路由表与冗余次数上限：这两项配错的后果分别是"拿假 Trace 判
        # ROI"和"两个维度的 Trace 互相覆盖"，都属于跑完才发现就来不及的那类。
        InstructionControlDeps.assert_backend_routing()
        run_count = self.deps.run_count_per_arm()
        if run_count > MAX_RUN_COUNT_PER_ARM:
            raise ConfigurationError(
                f"SKILLEVAL_INSTRUCTION_CONTROL_RUN_COUNT_PER_ARM={run_count} 超出上限 "
                f"{MAX_RUN_COUNT_PER_ARM}：A/B 分支的 run_index 号段是 "
                f"[{RUN_INDEX_AB_LOADED}, {RUN_INDEX_PD_PROBE})，再大就会覆盖渐进式披露"
                "探查用例的 Trace。要跑更多次请先在 state/trace.py 的号段分配表里扩容。"
            )

    # ------------------------------------------------------------------ #
    # 1. prepare_instruction_control_cases
    # ------------------------------------------------------------------ #

    async def prepare_cases(self, state: InstructionControlState) -> dict[str, object]:
        """凑齐本维度要用的两批用例（docs/dev/13 第 3 节）。

        - **A/B 对比用例**：复用模块一已经生成并落库的 POSITIVE 用例，**只取训练集**。
          不重新生成（`ensure_test_suite()` 的 REUSE 语义），也不取验证集：A/B 成本是
          每条用例两条分支，而本维度自己也有优化闭环（第 8 节），闭环只能用训练集。
        - **渐进式披露探查用例**：本维度专属的两个新类别，按 REUSE 语义补生成——只有
          现有用例集里一条都没有时才出题，之后每次运行都复用。

        触发探查题的条数 = **有触发条件的参考文件数**（一个文件一条题）。没有
        `references/` 的 Skill 因此一条都不生成，本维度的探查部分会如实报告"这次
        没有可探查的对象"，而不是硬凑几条无处落地的题。
        """
        run_id = str(state["run_id"])
        skill = await self._load_base_skill(state)

        counts = self._probe_case_counts(skill)
        result = await self.deps.suite_service().ensure_test_suite(
            skill,
            extra_categories=[
                TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
                TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR,
            ],
            category_counts=counts,
        )
        suite = result.suite_version

        positive = await self.deps.test_case_repository.list_by_category(
            suite.suite_version_id, TestCaseCategory.POSITIVE
        )
        ab_case_ids = sorted(case.case_id for case in positive if case.split is DatasetSplit.TRAIN)
        pd_cases = await self.deps.test_case_repository.list_by_categories(
            suite.suite_version_id,
            [
                TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
                TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR,
            ],
        )
        pd_case_ids = sorted(case.case_id for case in pd_cases)

        logger.info(
            "instruction_control_cases_prepared",
            run_id=run_id,
            node_name=ENTRY_NODE,
            suite_version_id=suite.suite_version_id,
            ab_cases=len(ab_case_ids),
            pd_cases=len(pd_case_ids),
            generated=result.generated,
            stale=result.staleness_warning is not None,
        )
        return {
            "active_suite_version_id": suite.suite_version_id,
            KEY_AB_CASE_IDS: ab_case_ids,
            KEY_PD_CASE_IDS: pd_case_ids,
            KEY_SUITE_STALENESS_WARNING: result.staleness_warning,
        }

    @staticmethod
    def _probe_case_counts(skill: SkillDefinition) -> dict[TestCaseCategory, int]:
        """算出两类探查用例各要出几条。

        触发探查：**每个带触发条件的参考文件各一条**。只统计
        `trigger_condition` 非空的文件——正文里根本没提到的参考文件，"条件满足"这件
        事无从定义，为它出题只会得到一条判不了的用例（那类文件本身是模块二要报的
        问题，不是模块三该测的东西）。

        常规对照：固定 3 条，且只在确实存在参考文件时才出。3 是下限而不是估算——
        水位检查要算中位数，样本再少就只是噪音（见
        `probe.resolve_token_watermark()` 的 `min_samples`）。
        """
        with_condition = [ref for ref in skill.reference_files if ref.trigger_condition]
        if not with_condition:
            return {
                TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER: 0,
                TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR: 0,
            }
        return {
            TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER: len(with_condition),
            TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR: 3,
        }

    # ------------------------------------------------------------------ #
    # 2. ab_comparative_execution
    # ------------------------------------------------------------------ #

    async def ab_comparative_execution(self, state: InstructionControlState) -> dict[str, object]:
        """A/B 增值对比：同一批任务分别在"加载 Skill"与"基线"下各跑一遍，判 ROI。

        两条分支用 `asyncio.gather` 并发派生（架构文档模块三第 1 节的"两个并发执行
        分支"），差别只有 `ExecutionRequest.load_skill`——其余参数必须完全一致，
        否则比出来的差异说不清是 Skill 带来的还是配置带来的。
        """
        run_id = str(state["run_id"])
        skill = await self._effective_skill(state)
        cases = await self._cases(_id_list(state, KEY_AB_CASE_IDS))
        if not cases:
            logger.warning(
                "instruction_control_ab_no_cases",
                run_id=run_id,
                node_name=NODE_NAMES["ab_comparative_execution"],
                hint="模块一的 POSITIVE 训练集为空，本次没有可做 A/B 对比的用例",
            )
            return {KEY_AB_PAIRS: [], KEY_ROI_OUTCOMES: []}

        results = await self._run_ab(run_id, skill, cases)

        pairs: list[AbPair] = []
        trace_ids: list[str] = []
        outcomes: list[JudgmentOutcome] = []
        verdict_ids: list[str] = []
        for case, loaded_traces, baseline_traces in results:
            for trace in (*loaded_traces, *baseline_traces):
                await self.deps.trace_repository.save(trace)
                trace_ids.append(trace.trace_id)
            pairs.append(
                AbPair(
                    case_id=case.case_id,
                    loaded_trace_id=loaded_traces[0].trace_id,
                    baseline_trace_id=baseline_traces[0].trace_id,
                )
            )
            outcome = await self._judge_roi(case, loaded_traces, baseline_traces)
            outcomes.append(outcome)
            if outcome.verdict_id:
                verdict_ids.append(outcome.verdict_id)

        logger.info(
            "instruction_control_ab_executed",
            run_id=run_id,
            node_name=NODE_NAMES["ab_comparative_execution"],
            cases=len(cases),
            traces=len(trace_ids),
            roi_failed=[o.case_id for o in outcomes if o.status is JudgeVerdictStatus.FAIL],
        )
        return {
            "executed_trace_ids": trace_ids,
            "judge_verdict_ids": verdict_ids,
            KEY_AB_PAIRS: [p.model_dump() for p in pairs],
            KEY_ROI_OUTCOMES: [o.model_dump() for o in outcomes],
        }

    async def _run_ab(
        self, run_id: str, skill: SkillDefinition, cases: Sequence[TestCase]
    ) -> list[tuple[TestCase, list[ExecutionTrace], list[ExecutionTrace]]]:
        """对每条用例并发跑"加载/基线"两条分支，返回 (用例, 加载侧 Traces, 基线侧 Traces)。

        并发上限由 `ExecutorSettings.max_concurrent_sandboxes` 通过信号量控制：
        `asyncio.gather` 会把 `用例数 × 2 × run_count` 个沙箱请求一次性打出去。
        """
        semaphore = asyncio.Semaphore(self.deps.concurrency_limit())
        backend = self.deps.backend()
        run_count = self.deps.run_count_per_arm()

        async def run_once(case: TestCase, index: int, *, baseline: bool) -> ExecutionTrace:
            request = ExecutionRequest(
                skill=skill,
                case=case,
                run_index=_ab_run_index(run_index=index, baseline=baseline),
                run_id=run_id,
                # 唯一的自变量。`load_skill=False` 就是架构文档要的"完全不加载该
                # Skill 的基线环境"（`ExecutionRequest` 从 docs/dev/03 起就为本维度
                # 预留了这个字段）。
                load_skill=not baseline,
                wall_clock_timeout_s=self.deps.execution_timeout_s,
            )
            async with semaphore:
                return await backend.execute(request)

        async def run_case(
            case: TestCase,
        ) -> tuple[TestCase, list[ExecutionTrace], list[ExecutionTrace]]:
            loaded, baseline = await asyncio.gather(
                asyncio.gather(*[run_once(case, i, baseline=False) for i in range(run_count)]),
                asyncio.gather(*[run_once(case, i, baseline=True) for i in range(run_count)]),
            )
            return case, list(loaded), list(baseline)

        # 后端的容错约定（docs/dev/03 第 6 节）：`execute()` 对超时/异常返回失败态
        # Trace，只有"评测系统自身故障"才抛 ExecutorBackendError。那类异常这里
        # **不吞**——继续跑只会产出一份基于残缺证据的 ROI 结论。
        return list(await asyncio.gather(*[run_case(case) for case in cases]))

    async def _judge_roi(
        self,
        case: TestCase,
        loaded: Sequence[ExecutionTrace],
        baseline: Sequence[ExecutionTrace],
    ) -> JudgmentOutcome:
        """ROI 判定（docs/dev/13 第 4.1 节）。`Criticality.CRITICAL`，走 3 副本共识。

        数字（步数/耗时/Token）取**该分支各次执行的均值**，文本取第一次执行的最终
        答复：多跑几次买到的是数字上的稳定性，文本没法平均。`run_count_per_arm=1`
        时两者等价，这段逻辑只在有人把它调大时才起作用。
        """
        subject_id = f"{SUBJECT_PREFIX_ROI}{case.case_id}"
        result = await self.deps.judge().judgmental_verdict(
            subject_id=subject_id,
            template_key=TEMPLATE_ROI_COMPARISON,
            content={
                "prompt": case.prompt,
                "loaded_final_response": format_final_response(loaded[0]),
                "loaded_actions_count": str(_avg(len(t.actions) for t in loaded)),
                "loaded_duration_ms": str(_avg(t.timing.duration_ms for t in loaded)),
                "loaded_total_tokens": str(_avg(t.timing.total_tokens for t in loaded)),
                "baseline_final_response": format_final_response(baseline[0]),
                "baseline_actions_count": str(_avg(len(t.actions) for t in baseline)),
                "baseline_duration_ms": str(_avg(t.timing.duration_ms for t in baseline)),
                "baseline_total_tokens": str(_avg(t.timing.total_tokens for t in baseline)),
            },
            criticality=ROI_CRITICALITY,
        )
        return self._to_outcome(
            subject_id,
            TEMPLATE_ROI_COMPARISON,
            result,
            case_id=case.case_id,
            node_name=NODE_NAMES["ab_comparative_execution"],
        )

    # ------------------------------------------------------------------ #
    # 3. trace_efficiency_diagnosis
    # ------------------------------------------------------------------ #

    async def trace_efficiency_diagnosis(self, state: InstructionControlState) -> dict[str, object]:
        """效率损耗诊断（docs/dev/13 第 5 节）：通读加载侧的执行轨迹找三种效率损耗。

        **只看加载侧**：本项诊断问的是"这份 Skill 的指令有没有把 Agent 带进沟里"，
        基线侧压根没有指令可言，把它一起诊断只会得到一堆与被测对象无关的结论。

        `Criticality.ROUTINE`：效率诊断是"发现问题供优化参考"，不阻断合并、也不进
        优化闭环（docs/dev/13 第 8 节），没有必要花三倍 Token 投票。
        """
        run_id = str(state["run_id"])
        pairs = [AbPair.model_validate(item) for item in _dict_list(state, KEY_AB_PAIRS)]
        judge = self.deps.judge()

        outcomes: list[JudgmentOutcome] = []
        verdict_ids: list[str] = []
        for pair in pairs:
            trace = await self.deps.trace_repository.get(pair.loaded_trace_id)
            if trace is None:
                # A/B 节点刚落的库，这里取不到只可能是库出了问题；如实记一条而不是
                # 静默跳过——跳过会让报告显示"效率诊断全过"。
                outcomes.append(
                    JudgmentOutcome(
                        subject_id=f"{SUBJECT_PREFIX_EFFICIENCY}{pair.loaded_trace_id}",
                        template_key=TEMPLATE_TRACE_EFFICIENCY,
                        case_id=pair.case_id,
                        skipped_reason=f"取不到 Trace {pair.loaded_trace_id}，本条未做效率诊断",
                    )
                )
                continue
            settings = self.deps.settings()
            subject_id = f"{SUBJECT_PREFIX_EFFICIENCY}{trace.trace_id}"
            result = await judge.judgmental_verdict(
                subject_id=subject_id,
                template_key=TEMPLATE_TRACE_EFFICIENCY,
                content={
                    "actions": format_actions_for_review(
                        trace,
                        max_steps=settings.trace_digest_max_steps,
                        max_output_chars=settings.trace_digest_max_output_chars,
                    ),
                    "final_response": format_final_response(trace),
                },
                criticality=EFFICIENCY_CRITICALITY,
            )
            outcome = self._to_outcome(
                subject_id,
                TEMPLATE_TRACE_EFFICIENCY,
                result,
                case_id=pair.case_id,
                node_name=NODE_NAMES["trace_efficiency_diagnosis"],
            )
            outcomes.append(outcome)
            if outcome.verdict_id:
                verdict_ids.append(outcome.verdict_id)

        logger.info(
            "instruction_control_efficiency_diagnosed",
            run_id=run_id,
            node_name=NODE_NAMES["trace_efficiency_diagnosis"],
            diagnosed=len(outcomes),
            failed=[o.case_id for o in outcomes if o.status is JudgeVerdictStatus.FAIL],
        )
        return {
            "judge_verdict_ids": verdict_ids,
            KEY_EFFICIENCY_OUTCOMES: [o.model_dump() for o in outcomes],
        }

    # ------------------------------------------------------------------ #
    # 4. control_calibration_static_scan
    # ------------------------------------------------------------------ #

    async def control_calibration_static_scan(
        self, state: InstructionControlState
    ) -> dict[str, object]:
        """刚性/柔性控制标定（docs/dev/13 第 6 节）：直接复用 docs/dev/07 的模板 5.7。

        本节点不设计任何新东西，只声明调用方式：整份 SKILL.md 一条判定、
        `Criticality.ROUTINE`、结论不阻断也不进优化闭环。

        判的是**仓库里那份 SKILL.md 的原貌**（`_load_base_skill`），不是优化闭环的
        内存工作副本——控制标定说的是"作者把指令写成什么样"，拿一份机器改过的版本
        去评，报告就与人能打开看的文件对不上了（与模块二同一条原则）。
        """
        run_id = str(state["run_id"])
        skill = await self._load_base_skill(state)
        result = await self.deps.judge().judgmental_verdict(
            subject_id=skill.skill_id,
            template_key=TEMPLATE_CONTROL_CALIBRATION,
            content={"skill_md": skill.body_markdown},
            criticality=CALIBRATION_CRITICALITY,
        )
        outcome = self._to_outcome(
            skill.skill_id,
            TEMPLATE_CONTROL_CALIBRATION,
            result,
            node_name=NODE_NAMES["control_calibration_static_scan"],
        )
        logger.info(
            "instruction_control_calibration_scanned",
            run_id=run_id,
            node_name=NODE_NAMES["control_calibration_static_scan"],
            skill_id=skill.skill_id,
            status=outcome.status.value if outcome.status else None,
            skipped=bool(outcome.skipped_reason),
        )
        return {
            "judge_verdict_ids": [outcome.verdict_id] if outcome.verdict_id else [],
            KEY_CALIBRATION_OUTCOME: outcome.model_dump(),
        }

    # ------------------------------------------------------------------ #
    # 5. progressive_disclosure_dynamic_probe
    # ------------------------------------------------------------------ #

    async def progressive_disclosure_dynamic_probe(
        self, state: InstructionControlState
    ) -> dict[str, object]:
        """渐进式披露**动态**探查（docs/dev/13 第 7 节）。

        与模块二的静态版（`progressive_disclosure_static` 模板）测的不是一回事：
        静态版只看 SKILL.md 里"触发条件写没写清楚"，这里真的把任务跑一遍，看 Agent
        到底有没有按条件去读那个文件。两者都留在报告里，人才能看出"条件写得很清楚
        但它就是不读"这种更难缠的情况。

        判定不经 `judgmental_verdict()`：扫描本身是确定性的（`probe.py`），走
        `quantitative_verdict()` + `rules.py` 里注册的规则统一产出 `JudgeVerdict`，
        保证裁判记录一致落库（docs/dev/13 第 7 节的收口约定）。
        """
        run_id = str(state["run_id"])
        skill = await self._effective_skill(state)
        cases = await self._cases(_id_list(state, KEY_PD_CASE_IDS))
        if not cases:
            logger.info(
                "instruction_control_probe_no_cases",
                run_id=run_id,
                node_name=NODE_NAMES["progressive_disclosure_dynamic_probe"],
                hint="该 Skill 没有带触发条件的 references/ 文件，本次无可探查对象",
            )
            return {KEY_PD_FINDINGS: []}

        traces = await self._run_probe(run_id, skill, cases)
        trace_ids: list[str] = []
        for trace in traces.values():
            await self.deps.trace_repository.save(trace)
            trace_ids.append(trace.trace_id)

        findings, verdict_ids = await self._judge_probe(state, skill, cases, traces)
        logger.info(
            "instruction_control_probe_completed",
            run_id=run_id,
            node_name=NODE_NAMES["progressive_disclosure_dynamic_probe"],
            cases=len(cases),
            severe=sum(1 for f in findings if f.severe),
            minor=sum(1 for f in findings if not f.severe),
        )
        return {
            "executed_trace_ids": trace_ids,
            "judge_verdict_ids": verdict_ids,
            KEY_PD_FINDINGS: [f.model_dump() for f in findings],
        }

    async def _run_probe(
        self, run_id: str, skill: SkillDefinition, cases: Sequence[TestCase]
    ) -> dict[str, ExecutionTrace]:
        """跑一遍探查用例，返回 {case_id: trace}。

        每条只跑一次：探查判定看的是"这次执行里有没有出现读取动作"，是个确定性
        观测，多跑几次并不会让"读了"变得更可信。（相对地，模块一的触发率必须跑
        多次，因为它统计的是一个比例。）
        """
        semaphore = asyncio.Semaphore(self.deps.concurrency_limit())
        backend = self.deps.backend()

        async def run_one(case: TestCase) -> tuple[str, ExecutionTrace]:
            request = ExecutionRequest(
                skill=skill,
                case=case,
                run_index=RUN_INDEX_PD_PROBE,
                run_id=run_id,
                wall_clock_timeout_s=self.deps.execution_timeout_s,
            )
            async with semaphore:
                return case.case_id, await backend.execute(request)

        return dict(await asyncio.gather(*[run_one(case) for case in cases]))

    async def _judge_probe(
        self,
        state: InstructionControlState,
        skill: SkillDefinition,
        cases: Sequence[TestCase],
        traces: dict[str, ExecutionTrace],
    ) -> tuple[list[ProbeFinding], list[str]]:
        """扫描 + 量化判定，返回 (全部发现, 落库的 verdict_id)。"""
        settings = self.deps.settings()
        judge = self.deps.judge()
        known_paths = [ref.path for ref in skill.reference_files]

        # 水位基线只用常规对照那一批算：触发探查题本来就该去读文件，把它们算进
        # 基线等于用"应该更贵的那批"去定义"便宜"的标准。
        regular_traces = [
            traces[case.case_id]
            for case in cases
            if case.category is TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR
            and case.case_id in traces
        ]
        watermark = resolve_token_watermark(
            regular_traces,
            ratio=settings.pd_token_watermark_ratio,
            min_samples=settings.pd_watermark_min_samples,
            override=cast("int | None", state.get(KEY_TOKEN_WATERMARK)),
        )

        all_findings: list[ProbeFinding] = []
        verdict_ids: list[str] = []
        for case in cases:
            trace = traces.get(case.case_id)
            if trace is None:
                continue
            findings = scan_probe_trace(
                case,
                trace,
                known_reference_paths=known_paths,
                token_watermark=watermark,
            )
            all_findings.extend(findings)
            verdict = judge.quantitative_verdict(
                subject_id=f"{SUBJECT_PREFIX_PD_PROBE}{case.case_id}",
                rule_name=rules.RULE_PROGRESSIVE_DISCLOSURE_PROBE,
                inputs=rules.probe_inputs(case.category.value, findings),
            )
            verdict_ids.append(verdict.verdict_id)
            if verdict.status is JudgeVerdictStatus.FAIL:
                # 只归档失败判定（与模块一同一口径）：通过判定没人读，失败判定是
                # Optimizer 的输入、也是人工审查时唯一能看的证据。
                await self.deps.judge_repository.save_verdict(verdict)
        return all_findings, verdict_ids

    # ------------------------------------------------------------------ #
    # 6. collect_findings（并行分支的汇合点）
    # ------------------------------------------------------------------ #

    async def collect_findings(self, state: InstructionControlState) -> dict[str, object]:
        """汇总四条支路的结果，挑出"训练集上可优化的失败信号"。

        本节点**没有副作用**：不发请求、不写库，只是把散在各私有键里的结论汇成一个
        列表供路由判断。做成独立节点是因为条件路由必须挂在某个节点上，而这个判断需要
        同时看到 A/B 与探查两条支路（挂在其中一条上会漏掉另一条的失败信号）。

        只有两类问题进闭环（docs/dev/13 第 8 节）：ROI 判定失败、渐进式"漏读"。
        效率诊断与控制标定的结论不进——那两类更接近"写作风格建议"，自动重写容易引发
        架构文档反复强调的"过度杀伤力"，交给人看过报告后自行决定。

        且**只取训练集用例**：验证集不参与优化（防过拟合），这是全项目的硬约束，
        `build_failure_context()` 里还有第二道闸。
        """
        run_id = str(state["run_id"])
        roi_outcomes = self._roi_outcomes(state)
        findings = self._probe_findings(state)

        train_case_ids = {
            case.case_id
            for case in await self._cases(
                [*_id_list(state, KEY_AB_CASE_IDS), *_id_list(state, KEY_PD_CASE_IDS)]
            )
            if case.split is DatasetSplit.TRAIN
        }
        failed: list[str] = []
        for outcome in roi_outcomes:
            if outcome.status is JudgeVerdictStatus.FAIL and outcome.case_id in train_case_ids:
                failed.append(outcome.case_id)
        for finding in findings:
            if finding.severe and finding.case_id in train_case_ids:
                failed.append(finding.case_id)

        unique_failed = sorted(set(failed))
        logger.info(
            "instruction_control_findings_collected",
            run_id=run_id,
            node_name=NODE_NAMES["collect_findings"],
            roi_failed=sum(1 for o in roi_outcomes if o.status is JudgeVerdictStatus.FAIL),
            severe_probe_findings=sum(1 for f in findings if f.severe),
            optimizable_train_cases=len(unique_failed),
        )
        return {KEY_FAILED_TRAIN_CASE_IDS: unique_failed}

    @staticmethod
    def route_after_collect(state: InstructionControlState) -> str:
        """训练集上有可优化的失败 → 进优化闭环；否则直接收尾。

        返回的是**节点名**，供 `add_conditional_edges` 的映射表使用。
        """
        failed = _id_list(state, KEY_FAILED_TRAIN_CASE_IDS)
        return NODE_NAMES["optimizer_loop"] if failed else NODE_NAMES["finalize_dimension_report"]

    # ------------------------------------------------------------------ #
    # 7. optimizer_loop
    # ------------------------------------------------------------------ #

    async def optimizer_loop(self, state: InstructionControlState) -> dict[str, object]:
        """接入 docs/dev/09 的通用闭环：改写 SKILL.md → 重跑相关子节点 → 收敛或挂起。

        与模块一用的是同一个 `prompt_engineer` 角色，但 `extra_instructions` 完全不同：
        模块一要它改 description（让 Skill 被唤醒），这里要它改**正文**（让 Skill 在
        被唤醒之后真的有用、并且把参考文件的加载条件写到 Agent 会照做的程度）。

        `retest_fn` 里"补好了"的定义 = 这些失败用例重跑一遍之后，ROI 判定通过且不再
        出现漏读。**重跑的是失败的那个子集**，不是整个维度：闭环最多跑 3 轮，每轮都
        把整个 A/B 全量重跑一次的成本没有任何人会接受。
        """
        run_id = str(state["run_id"])
        base_skill = await self._load_base_skill(state)
        failed_case_ids = _id_list(state, KEY_FAILED_TRAIN_CASE_IDS)
        failed_cases = await self._cases(failed_case_ids)
        verdicts = await self._failed_verdicts(failed_cases)

        ctx = build_failure_context(
            base_skill,
            failed_cases,
            verdicts,
            role=ROLE_PROMPT_ENGINEER,
            extra_instructions=(
                "本轮失败来自模块三（指令控制度与执行效果），有两种成因，请对照裁判"
                "结论分别处理：\n"
                "1. ROI 判定未通过——加载这份 Skill 与完全不加载相比，产出质量与执行"
                "效率都没有实质差别。要改的是**正文**：把这类任务里真正容易做错的地方"
                "写成可执行的步骤/约束，而不是把 description 写得更漂亮。\n"
                "2. 渐进式披露漏读——正文声明了某个 references/ 文件的加载条件，但"
                "Agent 在条件满足时并没有去读它。要改的是那句触发条件：写清楚"
                "**在什么情况下必须先读哪个文件再作答**，让它成为一条指令而不是一句"
                "介绍。\n"
                "不要为了通过判定而往正文里堆无关内容：正文体量本身也是被评测的"
                "（模块二有 500 行 / 5000 Token 的硬性上限）。"
            ),
        )

        # 闭环内部逐轮把补丁叠加到上一轮的产物上，所以"最终生效的那份 Skill"只能从
        # retest_fn 收到的参数里捞（与模块一同一个坑，见 docs/dev/interfaces/11 第 6 节）。
        attempted_skills: list[SkillDefinition] = []

        async def retest_fn(working_skill: SkillDefinition) -> LoopResult:
            attempted_skills.append(working_skill)
            still_failing = await self._retest_failed_cases(run_id, working_skill, failed_cases)
            if still_failing:
                return LoopResult(
                    passed=False,
                    detail=(
                        f"{len(still_failing)}/{len(failed_cases)} 条训练用例仍未通过："
                        f"{still_failing}"
                    ),
                )
            return LoopResult(passed=True, detail=f"{len(failed_cases)} 条失败训练用例全部恢复通过")

        patch = await self.deps.loop().run(
            run_id=run_id,
            ctx=ctx,
            retest_fn=retest_fn,
            optimizer=self.deps.optimizer(),
        )
        if patch is None:
            # 闭环耗尽重试后已经挂起过一次；走到这里意味着人工明确选择了"放弃该补丁"。
            # 此时不该拿旧正文去跑收尾节点假装无事发生——直接判该 Skill 本维度失败。
            raise PipelineSuspended(
                f"{NODE_NAMES['optimizer_loop']}：优化闭环超出最大重试次数且人工未采纳补丁，"
                f"run_id={run_id}，失败训练用例={failed_case_ids}"
            )

        working_skill = self._resolve_working_skill(base_skill, attempted_skills, patch.patch_id)
        logger.info(
            "instruction_control_patch_adopted",
            run_id=run_id,
            node_name=NODE_NAMES["optimizer_loop"],
            patch_id=patch.patch_id,
            working_skill_version_ref=working_skill.version_ref,
            attempts=len(attempted_skills),
        )
        return {KEY_WORKING_SKILL: working_skill, KEY_APPLIED_PATCH_ID: patch.patch_id}

    async def _retest_failed_cases(
        self, run_id: str, working_skill: SkillDefinition, failed_cases: Sequence[TestCase]
    ) -> list[str]:
        """用打了补丁的 Skill 重跑失败用例，返回仍未通过的 case_id。

        按用例类别分派重测方式——这正是 docs/dev/13 第 8 节说的"重跑相关子节点"：
        - POSITIVE（A/B 失败的那批）→ 重跑两条分支 + 重判 ROI；
        - 两类探查用例 → 重跑一次 + 重扫描。

        重跑产生的 Trace 直接落库：`execution_traces` 的唯一键是 `(case_id, run_index)`，
        同一条用例的新一轮结果会覆盖旧的——这正是我们要的语义，判定永远只看"当前这
        版正文的表现"。
        """
        ab_cases = [c for c in failed_cases if c.category is TestCaseCategory.POSITIVE]
        probe_cases = [
            c
            for c in failed_cases
            if c.category
            in (
                TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
                TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR,
            )
        ]
        still_failing: list[str] = []

        if ab_cases:
            for case, loaded, baseline in await self._run_ab(run_id, working_skill, ab_cases):
                for trace in (*loaded, *baseline):
                    await self.deps.trace_repository.save(trace)
                outcome = await self._judge_roi(case, loaded, baseline)
                if outcome.status is JudgeVerdictStatus.FAIL:
                    still_failing.append(case.case_id)

        if probe_cases:
            traces = await self._run_probe(run_id, working_skill, probe_cases)
            known_paths = [ref.path for ref in working_skill.reference_files]
            for case in probe_cases:
                # 变量名与上面那段的 `trace` 刻意区分：同名会让类型收窄失效
                # （上面那个来自元组解包，必然非空；这个是 dict.get 的结果）。
                probe_trace = traces.get(case.case_id)
                if probe_trace is None:
                    still_failing.append(case.case_id)
                    continue
                await self.deps.trace_repository.save(probe_trace)
                # 重测**不做**水位检查（`token_watermark=None`）：水位是拿同批常规
                # 用例算出来的中位数，重测子集里往往只剩一两条，算出来的水位没有
                # 统计意义。闭环要判的是"漏读修好了没有"，那是个确定性观测。
                findings = scan_probe_trace(
                    case, probe_trace, known_reference_paths=known_paths, token_watermark=None
                )
                if any(f.severe for f in findings):
                    still_failing.append(case.case_id)

        return still_failing

    async def _failed_verdicts(self, failed_cases: Sequence[TestCase]) -> list[JudgeVerdict]:
        """回读失败用例的判定记录，作为 Optimizer 的失败证据。

        两种 subject_id 前缀都要查（`roi:` 与 `pd_probe:`）：一条用例可能同时因为
        ROI 不达标与漏读进了失败集合，两条证据对模型都有用。只取 FAIL 的记录——
        同一条用例在更早的运行里可能通过过，把那些也喂进去会让 Prompt 自相矛盾。
        """
        verdicts: list[JudgeVerdict] = []
        for case in failed_cases:
            for prefix in (SUBJECT_PREFIX_ROI, SUBJECT_PREFIX_PD_PROBE):
                recorded = await self.deps.judge_repository.list_verdicts(f"{prefix}{case.case_id}")
                verdicts.extend(v for v in recorded if v.status is JudgeVerdictStatus.FAIL)
        return verdicts

    @staticmethod
    def _resolve_working_skill(
        base_skill: SkillDefinition,
        attempted_skills: Sequence[SkillDefinition],
        patch_id: str,
    ) -> SkillDefinition:
        """在闭环用过的若干工作副本里，认出与最终采纳的补丁对应的那一份。

        `working_version_ref(base, patch_id)` 是补丁应用后版本号的构造规则，拿它反查
        即可精确命中；没命中时退化为"最后一次重测用的那份"——那种情况只可能出现在
        人工采纳了一个**应用失败**的候选补丁时，此时最后一次重测过的副本是我们手上
        最接近的可用版本（与模块一同一处理）。
        """
        for skill in reversed(attempted_skills):
            if skill.version_ref == working_version_ref(base_skill.version_ref, patch_id):
                return skill
        return attempted_skills[-1] if attempted_skills else base_skill

    # ------------------------------------------------------------------ #
    # 8. finalize_dimension_report
    # ------------------------------------------------------------------ #

    async def finalize_dimension_report(self, state: InstructionControlState) -> dict[str, object]:
        """把本维度的结论写进 `dimension_results`（docs/dev/13 第 9 节）。

        判定口径：

        | 情形 | status | blocking |
        |---|---|---|
        | ROI 判定失败，或出现"漏读" | FAIL | **True** |
        | 只有效率诊断/控制标定失败，或只有过度抓取/水位告警 | NEEDS_HUMAN_REVIEW | False |
        | 两批用例都为空（什么也没测到） | NEEDS_HUMAN_REVIEW | False |
        | 其余 | PASS | False |

        中间那一档是本实现相对 docs/dev/13 正文第 9 节的一处**收窄**：正文的写法是
        "非阻断问题只进 findings、status 仍为 PASS"，那会让报告出现"结论通过、正文
        里却列着一串问题"的自相矛盾。改判 NEEDS_HUMAN_REVIEW 既保留了"不阻断合并"
        （`blocking=False` 才是阻断与否的唯一依据，见 `ReportGenerator.build()`），
        又让"这里有事情要人看一眼"在总状态里可见——而这恰恰是正文第 8 节对这两类
        问题的处置方式："交给人工在报告中查看后自行决定是否采纳"。

        `score=None`：本维度由四项性质完全不同的检查组成（一个语义对比、一个轨迹
        诊断、一个静态审查、一个确定性探查），硬凑一个"通过项/总项数"会把它们平均
        成一个没有含义的数字。
        """
        run_id = str(state["run_id"])
        roi_outcomes = self._roi_outcomes(state)
        efficiency_outcomes = self._efficiency_outcomes(state)
        calibration = self._calibration_outcome(state)
        probe_findings = self._probe_findings(state)

        ab_case_count = len(_id_list(state, KEY_AB_CASE_IDS))
        pd_case_count = len(_id_list(state, KEY_PD_CASE_IDS))

        roi_failed = [o for o in roi_outcomes if o.status is JudgeVerdictStatus.FAIL]
        severe = [f for f in probe_findings if f.severe]
        minor = [f for f in probe_findings if not f.severe]
        efficiency_failed = [o for o in efficiency_outcomes if o.status is JudgeVerdictStatus.FAIL]

        findings: list[str] = []
        findings.extend(
            f"ROI 判定未通过（用例 {o.case_id}）：加载 Skill 相比基线无显著增值。"
            f"{o.reasoning_excerpt}"
            for o in roi_failed
        )
        findings.extend(f.report_line for f in severe)
        findings.extend(f.report_line for f in minor)
        findings.extend(
            f"[效率诊断] 用例 {o.case_id} 的执行轨迹存在效率损耗（非阻断，供人工参考）："
            f"{o.reasoning_excerpt}"
            for o in efficiency_failed
        )
        if calibration and calibration.status is JudgeVerdictStatus.FAIL:
            findings.append(
                "[控制标定] 指令的刚性/柔性与任务脆弱性不匹配（非阻断，供人工参考）："
                f"{calibration.reasoning_excerpt}"
            )
        findings.extend(
            f"[{o.template_key}] 本次未产生判定结论：{o.skipped_reason}"
            for o in (*roi_outcomes, *efficiency_outcomes, *([calibration] if calibration else []))
            if o.skipped_reason
        )

        needs_human = bool(efficiency_failed or minor) or (
            calibration is not None and calibration.status is JudgeVerdictStatus.FAIL
        )
        if ab_case_count == 0 and pd_case_count == 0:
            # 两批用例都没有 = 这个维度这次什么都没测到。判 PASS 等于把维度悄悄关掉
            # （分数好看，但没有任何证据支撑），所以交给人看一眼。
            needs_human = True
            findings.append(
                "本次运行既没有可做 A/B 对比的 POSITIVE 训练集用例，也没有渐进式披露"
                "探查用例：指令控制度维度的结论不成立。请检查测试集生成结果，以及该 "
                "Skill 是否有带触发条件的 references/ 文件。"
            )
        if patch_id := state.get(KEY_APPLIED_PATCH_ID):
            findings.append(
                f"训练集失败用例已进入优化闭环并采纳补丁 {patch_id}（判定结果为闭环收敛后的表现）"
            )
        if staleness := state.get(KEY_SUITE_STALENESS_WARNING):
            findings.append(str(staleness))

        blocking = bool(roi_failed or severe)
        if blocking:
            status = JudgeVerdictStatus.FAIL
        elif needs_human:
            status = JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        else:
            status = JudgeVerdictStatus.PASS

        await self.deps.reporter().record_dimension_result(
            run_id=run_id,
            dimension=DIMENSION,
            status=status,
            score=None,
            findings=findings,
            # ROI 失败与漏读阻断；过度抓取/效率/控制标定不阻断（docs/dev/13 第 9 节）。
            blocking=blocking,
        )
        logger.info(
            "instruction_control_dimension_recorded",
            run_id=run_id,
            node_name=TERMINAL_NODE,
            status=status.value,
            blocking=blocking,
            roi_failed=len(roi_failed),
            severe_findings=len(severe),
            minor_findings=len(minor),
            findings=len(findings),
        )
        return {}

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    def _to_outcome(
        self,
        subject_id: str,
        template_key: str,
        result: JudgeVerdict | ConsensusResult,
        *,
        node_name: str,
        case_id: str | None = None,
    ) -> JudgmentOutcome:
        """把 Judge 的返回值收敛成状态里存的摘要，并在此处理两种特殊返回。

        1. **黄金基准盲测**：`judgmental_verdict()` 有一定概率把请求整个换成一条人类
           标定过的黄金用例来考核裁判自己。这类结果的 `subject_id` 带 `__golden__:`
           前缀，**必须跳过**——把它当成本 Skill 的结论写进报告，等于用另一份文本的
           判决给这份 Skill 定性（docs/dev/interfaces/08 第 3 节）。
        2. **共识未达成**：ROI 判定声明的是 CRITICAL，3 副本意见不一时
           `NEEDS_HUMAN_REVIEW` **不允许被降级**成 PASS/FAIL（docs/dev/08 的明令禁止
           项）。此处挂起等人工仲裁——尤其因为 ROI 的失败方向是"打回重构"，一个连三
           个裁判都吵不出结果的判决，不该由代码替人做主。
        """
        if isinstance(result, ConsensusResult) and not result.consensus_reached:
            raise PipelineSuspended(
                f"{node_name}：模板 {template_key!r} 的三副本复核未达成共识"
                f"（subject_id={result.subject_id!r}），需人工仲裁。"
            )

        status = result.final_status if isinstance(result, ConsensusResult) else result.status
        verdict = (
            result.verdicts[0]
            if isinstance(result, ConsensusResult) and result.verdicts
            else result
        )

        if is_golden_subject(result.subject_id):
            logger.info(
                "instruction_control_judgment_consumed_by_golden_case",
                node_name=node_name,
                template_key=template_key,
                subject_id=result.subject_id,
            )
            return JudgmentOutcome(
                subject_id=subject_id,
                template_key=template_key,
                case_id=case_id,
                skipped_reason="本次请求被黄金基准盲测占用，未产生针对本 Skill 的判定结论",
            )

        reasoning = getattr(verdict, "reasoning", "")
        return JudgmentOutcome(
            subject_id=subject_id,
            template_key=template_key,
            case_id=case_id,
            verdict_id=getattr(verdict, "verdict_id", None),
            status=status,
            reasoning_excerpt=str(reasoning)[:REASONING_EXCERPT_CHARS],
        )

    async def _load_base_skill(self, state: InstructionControlState) -> SkillDefinition:
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

    async def _effective_skill(self, state: InstructionControlState) -> SkillDefinition:
        """执行时该拿哪份 Skill：本维度优化闭环产出的工作副本优先，否则用库里的原版。

        **只认本维度自己的 `_ic_working_skill`**，不读模块一的 `_working_skill`：
        跨维度读别人的私有键是命名空间约定明令禁止的（`state.py`），而且模块一改的
        是 description（触发相关），与本维度评的"正文写得好不好用"不是一回事。
        """
        working = state.get(KEY_WORKING_SKILL)
        if working is not None:
            # Checkpoint 反序列化后可能是 dict（取决于 serde 实现），统一收敛成模型。
            return (
                working
                if isinstance(working, SkillDefinition)
                else SkillDefinition.model_validate(working)
            )
        return await self._load_base_skill(state)

    async def _cases(self, case_ids: Sequence[str]) -> list[TestCase]:
        """按 id 取用例，并按给定顺序还原。

        仓储层的回读顺序由数据库决定（`WHERE case_id IN (...)` 不保证顺序）。用例
        顺序直接影响日志与报告 findings 的排列，稳定下来才能 diff 两次运行的结果。
        """
        ids = list(case_ids)
        cases = await self.deps.test_case_repository.list_by_ids(ids)
        order = {case_id: index for index, case_id in enumerate(ids)}
        return sorted(cases, key=lambda case: order.get(case.case_id, len(order)))

    @staticmethod
    def _roi_outcomes(state: InstructionControlState) -> list[JudgmentOutcome]:
        return [JudgmentOutcome.model_validate(i) for i in _dict_list(state, KEY_ROI_OUTCOMES)]

    @staticmethod
    def _efficiency_outcomes(state: InstructionControlState) -> list[JudgmentOutcome]:
        return [
            JudgmentOutcome.model_validate(i) for i in _dict_list(state, KEY_EFFICIENCY_OUTCOMES)
        ]

    @staticmethod
    def _calibration_outcome(state: InstructionControlState) -> JudgmentOutcome | None:
        raw = state.get(KEY_CALIBRATION_OUTCOME)
        return None if raw is None else JudgmentOutcome.model_validate(raw)

    @staticmethod
    def _probe_findings(state: InstructionControlState) -> list[ProbeFinding]:
        return [ProbeFinding.model_validate(i) for i in _dict_list(state, KEY_PD_FINDINGS)]


def _avg(values: Iterable[int]) -> int:
    """若干次执行的整数指标取均值（四舍五入到整数）。

    空序列返回 0 而不是抛错：调用点都保证了至少有一次执行，但真到了空序列那一步，
    让 ROI 判定看到一个 0 并在 reasoning 里被质疑，好过让整条流水线在收尾前崩掉。
    """
    items = list(values)
    return round(sum(items) / len(items)) if items else 0


__all__ = [
    "ENTRY_NODE",
    "MAX_RUN_COUNT_PER_ARM",
    "NODE_NAMES",
    "REASONING_EXCERPT_CHARS",
    "SUBJECT_PREFIX_EFFICIENCY",
    "SUBJECT_PREFIX_PD_PROBE",
    "SUBJECT_PREFIX_ROI",
    "TERMINAL_NODE",
    "AbPair",
    "InstructionControlPipeline",
    "JudgmentOutcome",
]
