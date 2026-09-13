"""Nightly COLD 用例回归（docs/dev/24 第 6 节 `nightly_cold_suite.yml`；interfaces/17 第 7 节第 5 条）。

模块七（docs/dev/17）把同一能力路径下的冗余用例降级为 `DatasetSplit.COLD`：日常 PR 评测不再
跑它们（按 TRAIN/VALIDATION 过滤的维度自然看不到），架构文档要求它们"仅在周末的 Nightly
Build 中运行"。模块七只负责打标签，调度与执行由本文档落地。

## 口径

- **判什么**：COLD 用例全部是正/反向触发用例（冗余折叠只作用于正向用例，但这里按类别白名单
  取 POSITIVE/NEGATIVE，不假设），因此直接复用模块一的触发率判定——`run_cases()` 冗余执行 +
  `rules.rule_for_category()` 量化规则（interfaces/11 第 4.3 节：判定逻辑本就可脱离图状态调用），
  **不复制一份触发率口径**。
- **号段**：`RUN_INDEX_COLD_SUITE`（240 起），与模块一的 0~2 分开（理由见 `state/trace.py`）。
- **维度名**：`cold_suite_regression`，`blocking=False`——Nightly 不卡任何合并；它的价值是发现
  "被折叠的边界用例其实已经悄悄坏了"，结论进报告与告警通道，由人决定要不要把用例恢复为 TRAIN。
- **跑在主图里**：作为 `nightly.cold_suite_regression` 节点挂在前置门禁之后（`_pipeline_mode=
  cold_suite` 时路由到这里），而不是另编一张图——PLUGGABLE 后端的执行会挂起等 Hook，唤醒它的
  是 API 进程里注册的**主图** GraphResumer（interfaces/04 注意事项：一个进程只持有一个已编译主图）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from skill_evaluate.errors import PersistenceError
from skill_evaluate.graph.state import KEY_COLD_SUITE_SUMMARY, NODE_PREFIX_NIGHTLY
from skill_evaluate.logging import get_logger
from skill_evaluate.nodes.trigger_accuracy import TriggerAccuracyPipeline
from skill_evaluate.nodes.trigger_accuracy import rules as trigger_rules
from skill_evaluate.persistence.repository import TestSuiteRepository
from skill_evaluate.state.enums import DatasetSplit, JudgeVerdictStatus, TestCaseCategory
from skill_evaluate.state.trace import RUN_INDEX_COLD_SUITE

logger = get_logger(component="cold_suite")

DIMENSION = "cold_suite_regression"
NODE_NAME = f"{NODE_PREFIX_NIGHTLY}.cold_suite_regression"
# 判定记录的 subject_id 前缀（interfaces/13 第 6.1 节约定）：同一条用例在模块一有按裸 case_id
# 存的历史判定，不加前缀会被 `JudgeRepository.list_verdicts()` 混成一堆。
SUBJECT_PREFIX = "cold_suite:"
# Nightly 不阻断合并（见模块头）。刻意是常量而不是配置项，理由同其余维度的 BLOCKING。
BLOCKING = False
_COLD_CATEGORIES = (TestCaseCategory.POSITIVE, TestCaseCategory.NEGATIVE)


@dataclass(slots=True)
class ColdSuiteRegression:
    """复用模块一的执行骨架与判定规则，跑一遍当前 active 用例集里的 COLD 用例。"""

    trigger_pipeline: TriggerAccuracyPipeline
    suite_repository: TestSuiteRepository = field(default_factory=TestSuiteRepository)

    async def run(self, state: Mapping[str, Any]) -> dict[str, object]:
        run_id = str(state["run_id"])
        skill_id = str(state["skill_id"])
        version_ref = str(state["skill_version_ref"])
        deps = self.trigger_pipeline.deps
        reporter = deps.reporter()

        skill = await deps.skill_repository.get(skill_id, version_ref)
        if skill is None:
            raise PersistenceError(
                f"未找到被测 Skill {skill_id}@{version_ref}：主图入口 pipeline.bootstrap_run 应已入库"
            )

        # 不传 version_ref：Nightly 跑的是"当前 active 的那一套题"，Skill 版本漂移时照常跑并在
        # findings 里写明——COLD 回归关心的是用例本身还能不能通过，不该因为 SKILL.md 改过就整晚不跑。
        suite = await self.suite_repository.get_active_version(skill_id)
        if suite is None:
            await reporter.record_dimension_result(
                run_id=run_id,
                dimension=DIMENSION,
                status=JudgeVerdictStatus.NEEDS_HUMAN_REVIEW,
                score=None,
                findings=[
                    "[跳过] 该 Skill 没有 active 用例集，本次 Nightly 未执行任何 COLD 用例（这不等于通过）"
                ],
                blocking=BLOCKING,
            )
            return {KEY_COLD_SUITE_SUMMARY: {"status": "no_active_suite", "total": 0, "failed": 0}}

        await deps.run_repository.set_suite_version(run_id, suite.suite_version_id)
        cases = [
            case
            for case in await deps.test_case_repository.list_by_ids(suite.case_ids)
            if case.split is DatasetSplit.COLD and case.category in _COLD_CATEGORIES
        ]
        findings: list[str] = []
        if suite.skill_version_ref != version_ref:
            findings.append(
                f"[提示] active 用例集绑定的 Skill 版本为 {suite.skill_version_ref}，与本次评测版本 "
                f"{version_ref} 不一致"
            )

        if not cases:
            # 与"用例集为空判 NEEDS_HUMAN_REVIEW"不同：没有 COLD 用例是模块七"本轮没有冗余可折叠"
            # 的正常结果，不是流水线出了问题（同模块四"Skill 不带脚本判 PASS"的口径）。
            findings.append("[提示] 当前 active 用例集中没有被降级为 COLD 的用例，无需回归")
            await reporter.record_dimension_result(
                run_id=run_id,
                dimension=DIMENSION,
                status=JudgeVerdictStatus.PASS,
                score=None,
                findings=findings,
                blocking=BLOCKING,
            )
            return {KEY_COLD_SUITE_SUMMARY: {"status": "no_cold_cases", "total": 0, "failed": 0}}

        traces = await self.trigger_pipeline.run_cases(
            run_id, skill, cases, run_index_base=RUN_INDEX_COLD_SUITE
        )
        judge = deps.judge()
        verdict_ids: list[str] = []
        failed: list[str] = []
        trace_ids: list[str] = []
        for case in cases:
            case_traces = traces.get(case.case_id, [])
            trace_ids.extend(t.trace_id for t in case_traces)
            verdict = judge.quantitative_verdict(
                subject_id=f"{SUBJECT_PREFIX}{case.case_id}",
                rule_name=trigger_rules.rule_for_category(case.category),
                inputs=trigger_rules.trigger_rate_inputs(case_traces),
            )
            verdict_ids.append(verdict.verdict_id)
            if verdict.status is JudgeVerdictStatus.FAIL:
                failed.append(case.case_id)
                # 只归档失败判定（interfaces/11 第 5 节口径）。
                await deps.judge_repository.save_verdict(verdict)
                findings.append(
                    f"[COLD 回归失败] {case.case_id}（{case.category.value}）：{verdict.reasoning[:200]}"
                )

        status = JudgeVerdictStatus.FAIL if failed else JudgeVerdictStatus.PASS
        score = 1 - len(failed) / len(cases)
        if failed:
            findings.append(
                "[建议] 被折叠的冗余用例出现失败，说明其代表用例未能覆盖这条边界；请在审查工作台评估是否"
                "把失败用例恢复为 TRAIN（注意需先让其能力路径与代表用例真正不同，见 interfaces/17 第 5.4 节）"
            )
        await reporter.record_dimension_result(
            run_id=run_id,
            dimension=DIMENSION,
            status=status,
            score=score,
            findings=findings,
            blocking=BLOCKING,
        )
        logger.info(
            "cold_suite_regression_finished",
            run_id=run_id,
            skill_id=skill_id,
            total=len(cases),
            failed=len(failed),
        )
        return {
            "executed_trace_ids": trace_ids,
            "judge_verdict_ids": verdict_ids,
            KEY_COLD_SUITE_SUMMARY: {
                "status": status.value,
                "total": len(cases),
                "failed": len(failed),
                "failed_case_ids": failed,
            },
        }


__all__ = ["BLOCKING", "DIMENSION", "NODE_NAME", "SUBJECT_PREFIX", "ColdSuiteRegression"]
