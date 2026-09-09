"""强制功能回归（docs/dev/15 第 11.2 节）。

## 这是模块五最重要的一道闸门

架构文档要求安全补丁**必须**跑一次全量功能回归。理由是自动生成的安全约束极容易
"过度杀伤"：为了防路径穿越，模型很可能把路径写死成一个常量，于是穿越确实防住了，
正常的跨目录读取功能也一起废了。补丁跑通了安全重测就采纳，等于用一个安全问题换
了一个功能问题。

因此本模块做两件事，**都通过才算回归通过**：

1. **触发准确度回归**：复用模块一（docs/dev/11）的执行骨架与触发率量化规则，看
   打了补丁的 Skill 还能不能被正常唤醒。安全约束写在正文里一般不影响 description，
   但 `rigid_constraint` 类补丁偶尔会顺手改到 description。
2. **ROI 回归**：复用模块三（docs/dev/13）的 A/B 骨架与 ROI 判定，看它被唤醒之后
   还干不干得了活。这一条才是真正能抓住"过度杀伤"的——一个把路径写死的约束不会
   影响 Skill 被唤醒，只会让它唤醒之后处理不了正常任务。

## 本模块就是 docs/dev/15 第 11.2 节那条实现约束的落地

那一节要求模块 11/13 的判定核心逻辑"应可脱离图节点上下文单独调用，LangGraph 节点
函数只是对它的一层薄包装"。落实方式是给两边各加了一个**公开、不吃 state** 的入口
（都是追加式扩展，原有节点行为一个字没变）：

- `TriggerAccuracyPipeline.run_cases(..., run_index_base=...)`（本来就是公开的，
  只追加了号段参数）+ `trigger_accuracy.rules` 里现成的 `trigger_rate_inputs()` /
  `rule_for_category()`；
- `InstructionControlPipeline.run_ab_pairs()` / `judge_roi()`（新增的公开包装）。

**不复制一份判定逻辑**：判定口径复制两份，早晚会漂移，而漂移的表现是"安全回归说
功能没坏，模块三说坏了"，没人知道该信哪个。

## 号段隔离

回归重跑的 Trace 落在模块五自己申领的号段（`RUN_INDEX_SEC_REGRESSION_*`）。不换
号段的话，一次"为了验证补丁"的重跑会把模块一/三本次运行的真实结果覆盖掉——而那
正是被验证的对象。号段分配表见 `state/trace.py`。
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from skill_evaluate.logging import get_logger
from skill_evaluate.nodes.instruction_control import (
    InstructionControlDeps,
    InstructionControlPipeline,
)
from skill_evaluate.nodes.security.state import DIMENSION
from skill_evaluate.nodes.trigger_accuracy import TriggerAccuracyDeps, TriggerAccuracyPipeline
from skill_evaluate.nodes.trigger_accuracy import rules as trigger_rules
from skill_evaluate.state.enums import DatasetSplit, JudgeVerdictStatus, TestCaseCategory
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase
from skill_evaluate.state.trace import (
    RUN_INDEX_SEC_REGRESSION_AB_LOADED,
    RUN_INDEX_SEC_REGRESSION_TRIGGER,
)

logger = get_logger(component=DIMENSION)


class RegressionOutcome(BaseModel):
    """一次功能回归的结论。

    比裸 bool 多带诊断细节，因为它会原样进 `LoopResult.detail` → 落到
    `patch_application_results.detail` → 出现在人工审批卡片上。人看到"回归失败"
    时第一个问题一定是"哪坏了"，答案必须就在这条记录里。
    """

    passed: bool
    detail: str
    trigger_failed_case_ids: list[str] = []
    roi_failed_case_ids: list[str] = []
    skipped_reason: str | None = None  # 非空 = 这次没真的跑成，见 `passed` 的说明


class FunctionalRegressionRunner:
    """把模块一/三的可复用判定拼成一次功能回归。

    做成类而不是函数，是为了让两条流水线实例（以及它们背后的 Executor / Judge
    单例）只构造一次：闭环最多跑 3 轮，每轮都重新构造一遍 `JudgeAgent` 会让黄金
    盲测的随机状态、健康检查的窗口统计在轮次之间对不上。
    """

    def __init__(
        self,
        *,
        trigger_pipeline: TriggerAccuracyPipeline | None = None,
        instruction_pipeline: InstructionControlPipeline | None = None,
        trigger_deps: TriggerAccuracyDeps | None = None,
        include_roi: bool = True,
        max_cases: int = 0,
    ) -> None:
        # 两条流水线各自带一套 deps。允许注入是为了单测能塞假后端；默认构造会走
        # 各自维度的路由断言（模块一/三都要求 PLUGGABLE），与本维度的要求一致。
        self._trigger = trigger_pipeline or TriggerAccuracyPipeline(trigger_deps)
        self._instruction = instruction_pipeline or InstructionControlPipeline(
            InstructionControlDeps()
        )
        self._include_roi = include_roi
        self._max_cases = max(0, max_cases)

    async def run(
        self, run_id: str, working_skill: SkillDefinition, cases: Sequence[TestCase]
    ) -> RegressionOutcome:
        """跑一次功能回归。`cases` 应当是**训练集**的正/反向用例。

        为什么只跑训练集：验证集不参与优化闭环是全项目的硬约束（防过拟合）。回归
        虽然只是"检查有没有改坏"而不是"用它来优化"，但补丁是否被采纳直接取决于回归
        结果——用验证集决定采不采纳补丁，等于让验证集参与了优化。
        """
        selected = self._select(cases)
        if not selected:
            # 没有可回归的用例时**不能**判 passed=True：那等于"没测就说没坏"。
            # 判 False 会让补丁在这种情况下永远无法被采纳，同样不合理——正确做法是
            # 判 False 并把原因写清楚，让它走到人工挂起那一步由人来定夺
            # （`OptimizationLoop` 耗尽重试后本来就会挂起等人）。
            return RegressionOutcome(
                passed=False,
                detail=(
                    "无法执行强制功能回归：没有可用的训练集正/反向用例。"
                    "安全补丁在未经功能验证的情况下不予自动采纳（架构文档：安全补丁"
                    "必须强制回归），请人工确认该补丁是否会误伤正常功能。"
                ),
                skipped_reason="no_regression_cases",
            )

        trigger_failed = await self._run_trigger_regression(run_id, working_skill, selected)
        roi_failed: list[str] = []
        if self._include_roi:
            roi_failed = await self._run_roi_regression(run_id, working_skill, selected)

        passed = not trigger_failed and not roi_failed
        detail = self._describe(selected, trigger_failed, roi_failed, total=len(cases))
        logger.info(
            "security_functional_regression_completed",
            run_id=run_id,
            skill_version_ref=working_skill.version_ref,
            cases=len(selected),
            trigger_failed=len(trigger_failed),
            roi_failed=len(roi_failed),
            include_roi=self._include_roi,
            passed=passed,
        )
        return RegressionOutcome(
            passed=passed,
            detail=detail,
            trigger_failed_case_ids=trigger_failed,
            roi_failed_case_ids=roi_failed,
        )

    def _select(self, cases: Sequence[TestCase]) -> list[TestCase]:
        """挑出参与回归的用例：训练集 ∩（正向 ∪ 反向），按 `max_cases` 截断。

        排序按 `case_id` 而不是入参顺序：抽样（`max_cases` 非 0）时必须每轮闭环都
        抽到**同一批**用例，否则第 2 轮"通过了"可能只是因为换了几条更容易的题。
        """
        eligible = sorted(
            (
                case
                for case in cases
                if case.split is DatasetSplit.TRAIN
                and case.category in (TestCaseCategory.POSITIVE, TestCaseCategory.NEGATIVE)
            ),
            key=lambda case: case.case_id,
        )
        return eligible[: self._max_cases] if self._max_cases else eligible

    async def _run_trigger_regression(
        self, run_id: str, working_skill: SkillDefinition, cases: Sequence[TestCase]
    ) -> list[str]:
        """触发率回归：复用模块一的冗余执行骨架 + 触发率量化规则。

        判定直接用**这一轮刚跑出来的 Trace**，不走 `TraceRepository.list_by_case()`：
        那个查询会把这条用例在所有维度、所有历史运行里的 Trace 一起捞回来，而我们
        要判的是"打了补丁的这一版现在表现如何"。
        """
        traces_by_case = await self._trigger.run_cases(
            run_id,
            working_skill,
            cases,
            run_index_base=RUN_INDEX_SEC_REGRESSION_TRIGGER,
        )
        judge = self._trigger.deps.judge()
        failed: list[str] = []
        for case in cases:
            traces = traces_by_case.get(case.case_id, [])
            for trace in traces:
                await self._trigger.deps.trace_repository.save(trace)
            verdict = judge.quantitative_verdict(
                # 前缀避免与模块一那边按裸 case_id 存的触发率判定混成一堆
                # （docs/dev/interfaces/13 第 6.1 节的 subject_id 前缀约定）。
                subject_id=f"sec_regression_trigger:{case.case_id}",
                rule_name=trigger_rules.rule_for_category(case.category),
                inputs=trigger_rules.trigger_rate_inputs(traces),
            )
            if verdict.status is JudgeVerdictStatus.FAIL:
                await self._trigger.deps.judge_repository.save_verdict(verdict)
                failed.append(case.case_id)
        return failed

    async def _run_roi_regression(
        self, run_id: str, working_skill: SkillDefinition, cases: Sequence[TestCase]
    ) -> list[str]:
        """ROI 回归：复用模块三的 A/B 骨架 + ROI 判定。

        **只拿正向用例做 A/B**：反向用例的期望行为是"不该被触发"，给它做 A/B 会得到
        "加载与不加载没差别"——那正是它该有的样子，判成 ROI 失败是彻头彻尾的误报。

        黄金盲测占用的那次判定（`skipped_reason` 非空）**不计入失败**：那次请求根本
        没有评到这条用例，把它算成回归失败等于让一次抽检把补丁枪毙了。
        """
        positive = [case for case in cases if case.category is TestCaseCategory.POSITIVE]
        if not positive:
            return []

        failed: list[str] = []
        results = await self._instruction.run_ab_pairs(
            run_id,
            working_skill,
            positive,
            run_index_base=RUN_INDEX_SEC_REGRESSION_AB_LOADED,
        )
        for case, loaded, baseline in results:
            for trace in (*loaded, *baseline):
                await self._instruction.deps.trace_repository.save(trace)
            outcome = await self._instruction.judge_roi(case, loaded, baseline)
            if outcome.skipped_reason:
                continue
            if outcome.status is JudgeVerdictStatus.FAIL:
                failed.append(case.case_id)
        return failed

    def _describe(
        self,
        selected: Sequence[TestCase],
        trigger_failed: Sequence[str],
        roi_failed: Sequence[str],
        *,
        total: int,
    ) -> str:
        scope = f"{len(selected)}/{total} 条训练集用例"
        if self._max_cases and len(selected) < total:
            scope += f"（按 SKILLEVAL_SECURITY_REGRESSION_MAX_CASES={self._max_cases} 抽样）"
        if not self._include_roi:
            scope += "；本次**未**包含 ROI 对比（SKILLEVAL_SECURITY_REGRESSION_INCLUDES_ROI=false）"
        if not trigger_failed and not roi_failed:
            return f"功能回归通过：{scope}，触发率与 ROI 判定均未出现新增失败。"
        parts = [f"功能回归失败：{scope}。"]
        if trigger_failed:
            parts.append(
                f"补丁后触发率不达标的用例 {len(trigger_failed)} 条：{list(trigger_failed)}"
                "（安全约束改坏了 Skill 被唤醒的能力）。"
            )
        if roi_failed:
            parts.append(
                f"补丁后 ROI 判定失败的用例 {len(roi_failed)} 条：{list(roi_failed)}"
                "（安全约束过度杀伤：Skill 仍会被唤醒，但已经干不了正常的活）。"
            )
        return " ".join(parts)


__all__ = ["FunctionalRegressionRunner", "RegressionOutcome"]
