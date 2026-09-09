"""模块一：触发准确度与泛化能力评测流水线的节点实现（docs/dev/11）。

全项目第一个端到端垂直切片：把第 0/1 层的公共能力（Generator / Executor /
Judge / Optimizer / ReportGenerator / 持久化 / 挂起机制）串成一条真正跑得通的
评测闭环。

```
prepare_test_suite → execute_train_cases → judge_train_cases
      ├─（训练集全过）────────────────────────────────┐
      └─（有失败）→ optimizer_loop ─（收敛/人工采纳）→ ┤
                                                      ↓
      execute_validation_cases → judge_validation_cases → finalize_dimension_report
```

两条贯穿全文件的硬约束：

1. **验证集不参与优化**。图结构上没有"验证集失败 → optimizer_loop"这条边，
   而不是靠运行时校验兜底（docs/dev/11 第 8 节）。`build_failure_context()` 里的
   训练集校验是第二道闸，不是第一道。
2. **所有通过/失败结论都经 `JudgeAgent`**。本文件里没有一处自己写的
   `if rate >= 0.5`——阈值判定在 `rules.py` 里注册成量化规则，由
   `judge.quantitative_verdict()` 调用（docs/dev/interfaces/08 第 0 节铁律）。

## 节点签名用 `TriggerAccuracyState` 而不是 `PipelineState`

LangGraph 会**按节点函数第一个参数的类型注解推导该节点的输入 schema**，并据此把
图状态裁剪一遍再传进来。签名写 `PipelineState` 的话，本维度的私有键（不在
`PipelineState` 的字段表里）会在进入节点前被静默丢掉——节点拿到的用例列表永远是
空的，且不报任何错。所以签名必须用带私有键的 `TriggerAccuracyState`。

同理，装配主图时（docs/dev/24）主图的状态 schema 也必须包含各维度的私有键，
见 docs/dev/interfaces/11_trigger_accuracy_pipeline.md 第 2 节。

## 节点返回值只带增量

LangGraph 会把节点返回的字典按 reducer 合并进图状态。`executed_trace_ids` /
`judge_verdict_ids` 声明的是 `Annotated[list[str], add]`（docs/dev/02），返回
`{**state, "executed_trace_ids": [...]}` 会把状态里**已有的** id 连同新 id 再追加
一遍，跑完两个执行节点后 trace id 就翻倍了。所以本文件所有节点一律只返回本次
产生的增量字段。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import cast

from skill_evaluate.agents.optimizer.loop import LoopResult
from skill_evaluate.agents.optimizer.patch_applier import working_version_ref
from skill_evaluate.agents.optimizer.schema import ROLE_PROMPT_ENGINEER
from skill_evaluate.agents.optimizer.service import build_failure_context
from skill_evaluate.errors import PersistenceError, PipelineSuspended
from skill_evaluate.executors.base import ExecutionRequest
from skill_evaluate.logging import get_logger
from skill_evaluate.nodes.trigger_accuracy import rules
from skill_evaluate.nodes.trigger_accuracy.deps import TriggerAccuracyDeps
from skill_evaluate.nodes.trigger_accuracy.state import (
    DIMENSION,
    KEY_APPLIED_PATCH_ID,
    KEY_CASE_IDS,
    KEY_SUITE_STALENESS_WARNING,
    KEY_TRAIN_CASE_IDS,
    KEY_TRAIN_FAILED_CASE_IDS,
    KEY_VALIDATION_CASE_IDS,
    KEY_VALIDATION_FAILED_CASE_IDS,
    KEY_WORKING_SKILL,
    TriggerAccuracyState,
)
from skill_evaluate.state.enums import DatasetSplit, JudgeVerdictStatus, TestCaseCategory
from skill_evaluate.state.judge import JudgeVerdict
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase
from skill_evaluate.state.trace import RUN_INDEX_REDUNDANT_BASE, ExecutionTrace

logger = get_logger(component=DIMENSION)

# 节点名。docs/dev/03 的 `NODE_BACKEND_ROUTING`、docs/dev/08 的 criticality 声明、
# docs/dev/24 的主图装配都引用这一份，避免三处各写各的字符串。
NODE_NAMES = {
    "prepare_test_suite": f"{DIMENSION}.prepare_test_suite",
    "execute_train_cases": f"{DIMENSION}.execute_train_cases",
    "judge_train_cases": f"{DIMENSION}.judge_train_cases",
    "optimizer_loop": f"{DIMENSION}.optimizer_loop",
    "execute_validation_cases": f"{DIMENSION}.execute_validation_cases",
    "judge_validation_cases": f"{DIMENSION}.judge_validation_cases",
    "finalize_dimension_report": f"{DIMENSION}.finalize_dimension_report",
}

ENTRY_NODE = NODE_NAMES["prepare_test_suite"]
TERMINAL_NODE = NODE_NAMES["finalize_dimension_report"]

# 触发准确度是核心维度：description 唤不醒 Skill，后面所有维度测的都是一份
# 永远不会被用到的技能。判定失败即阻断合并（各维度是否 blocking 由该维度自行声明）。
BLOCKING = True


def _in_given_order(cases: Sequence[TestCase], case_ids: Sequence[str]) -> list[TestCase]:
    """按给定的 id 顺序还原用例顺序。

    仓储层的回读顺序由数据库决定（`WHERE case_id IN (...)` 不保证顺序）。用例顺序
    直接影响日志、报告 findings 与失败清单的排列，稳定下来才能 diff 两次运行的
    结果——否则一次无关的重跑看起来像是"结果变了"。
    """
    order = {case_id: index for index, case_id in enumerate(case_ids)}
    return sorted(cases, key=lambda case: order.get(case.case_id, len(order)))


def _id_list(state: TriggerAccuracyState, key: str) -> list[str]:
    """从图状态里取一串 id，缺键时返回空列表。

    `PipelineState` 是 TypedDict，用**变量**作键时静态类型会退化成 `object`
    （私有键本来也不在 TypedDict 的字段表里）。这里集中收窄一次，好过在每个
    调用点各写一行 `cast`/`type: ignore`。
    """
    value = cast("list[str] | None", state.get(key))
    return [str(item) for item in (value or [])]


class TriggerAccuracyPipeline:
    """模块一的七个节点。做成类是为了让依赖注入只发生一次（构造时），
    而不是每个节点函数各自去拿一遍单例。

    用法（docs/dev/24 装配主图时）见 `graph.py::add_trigger_accuracy_nodes()`。
    """

    def __init__(self, deps: TriggerAccuracyDeps | None = None) -> None:
        self.deps = deps or TriggerAccuracyDeps()

    # ------------------------------------------------------------------ #
    # 1. prepare_test_suite
    # ------------------------------------------------------------------ #

    async def prepare_test_suite(self, state: TriggerAccuracyState) -> dict[str, object]:
        """取回被测 Skill 与测试集，筛出本维度关心的用例并按 split 分组。

        `ensure_test_suite()` 默认走 REUSE：只在"从来没生成过"时才调一次 LLM
        （docs/dev/06）。SKILL.md 版本漂移时**不**自动重新出题，只带回一条 staleness
        告警——自动重生会让"这次改动到底影响了什么"永远无法归因。
        """
        run_id = str(state["run_id"])
        skill = await self._load_base_skill(state)

        result = await self.deps.suite_service().ensure_test_suite(skill)
        suite = result.suite_version
        # 让 `ReportGenerator.build()` 能反查到本次运行用的是哪一版用例集
        # （runs.suite_version_id）。本维度是主图 Phase A 里第一个产出测试集的节点，
        # 因此由它负责回填。
        await self.deps.run_repository.set_suite_version(run_id, suite.suite_version_id)

        cases = _in_given_order(
            await self.deps.test_case_repository.list_by_ids(suite.case_ids), suite.case_ids
        )
        # 模块一只看正向/反向触发用例：ADVERSARIAL（模块五）、MULTI_SKILL（模块十）
        # 有各自的判定语义，混进来会被当成普通触发用例误判。
        trigger_cases = [
            case
            for case in cases
            if case.category in (TestCaseCategory.POSITIVE, TestCaseCategory.NEGATIVE)
        ]
        train_ids = [c.case_id for c in trigger_cases if c.split is DatasetSplit.TRAIN]
        validation_ids = [c.case_id for c in trigger_cases if c.split is DatasetSplit.VALIDATION]

        logger.info(
            "trigger_accuracy_suite_prepared",
            run_id=run_id,
            node_name=NODE_NAMES["prepare_test_suite"],
            suite_version_id=suite.suite_version_id,
            train_cases=len(train_ids),
            validation_cases=len(validation_ids),
            generated=result.generated,
            stale=result.staleness_warning is not None,
        )
        return {
            "active_suite_version_id": suite.suite_version_id,
            KEY_CASE_IDS: [c.case_id for c in trigger_cases],
            KEY_TRAIN_CASE_IDS: train_ids,
            KEY_VALIDATION_CASE_IDS: validation_ids,
            KEY_SUITE_STALENESS_WARNING: result.staleness_warning,
        }

    # ------------------------------------------------------------------ #
    # 2/5. 执行节点（训练集 / 验证集共用同一实现）
    # ------------------------------------------------------------------ #

    async def execute_train_cases(self, state: TriggerAccuracyState) -> dict[str, object]:
        return await self._execute_split(state, DatasetSplit.TRAIN)

    async def execute_validation_cases(self, state: TriggerAccuracyState) -> dict[str, object]:
        """验证集执行。

        与训练集节点结构完全一致，只有两点不同：过滤条件换成 VALIDATION，且判定
        结果**不会**路由回 `optimizer_loop`（那条边在图里根本不存在）。

        用的 Skill 是 `_effective_skill()` 的结果——训练集跑过优化闭环时，这里必须
        是**打了补丁之后**的版本，否则验证集测的还是旧 description，就失去了"验证
        补丁真的解决了问题、且没有过拟合训练集"的意义。
        """
        return await self._execute_split(state, DatasetSplit.VALIDATION)

    async def _execute_split(
        self, state: TriggerAccuracyState, split: DatasetSplit
    ) -> dict[str, object]:
        run_id = str(state["run_id"])
        node_name = (
            NODE_NAMES["execute_train_cases"]
            if split is DatasetSplit.TRAIN
            else NODE_NAMES["execute_validation_cases"]
        )
        cases = await self._cases_for_split(state, split)
        skill = await self._effective_skill(state)

        traces_by_case = await self.run_cases(run_id, skill, cases)

        trace_ids: list[str] = []
        for traces in traces_by_case.values():
            for trace in traces:
                await self.deps.trace_repository.save(trace)
                trace_ids.append(trace.trace_id)

        logger.info(
            "trigger_accuracy_cases_executed",
            run_id=run_id,
            node_name=node_name,
            split=split.value,
            cases=len(cases),
            traces=len(trace_ids),
            skill_version_ref=skill.version_ref,
        )
        # 只回增量：`executed_trace_ids` 的 reducer 是 `operator.add`。
        return {"executed_trace_ids": trace_ids}

    async def run_cases(
        self,
        run_id: str,
        skill: SkillDefinition,
        cases: Sequence[TestCase],
        *,
        run_index_base: int = RUN_INDEX_REDUNDANT_BASE,
    ) -> dict[str, list[ExecutionTrace]]:
        """对每条用例并发跑 `redundant_runs` 次，返回 {case_id: [trace, ...]}。

        冗余执行的理由是 LLM 行为的非确定性：单次"没触发"可能只是一次随机漂移，
        3 次里触发几次才构成一个可判定的比例（架构文档模块一）。

        本方法是**公开**的：docs/dev/19（跨模型矩阵）、docs/dev/20（多技能并发）
        复用"每条用例并发跑 N 次"这套骨架，但各自构造自己的 `ExecutionRequest`
        （`sampling_overrides` / `background_skills` 属于那两个维度的语义，本维度
        刻意不带——触发准确度测的是默认配置下的行为）。

        `run_index_base`（docs/dev/15 追加）：Trace 落在哪个号段起始。默认 0 = 本维度
        自己的冗余执行号段，**已有调用方不受影响**。模块五的强制功能回归要拿一个
        打了安全补丁的 working_skill 重跑同一批用例，必须落在自己的号段里
        （`RUN_INDEX_SEC_REGRESSION_TRIGGER`）——`execution_traces` 的唯一键是
        `(case_id, run_index)`，不换号段的话，一次"为了验证补丁"的重跑会把本维度
        本次运行的真实结果覆盖掉，而那正是被验证的对象。号段分配表见
        `state/trace.py`。

        并发上限由 `ExecutorSettings.max_concurrent_sandboxes` 通过信号量控制：
        `asyncio.gather` 会把 `用例数 × 3` 个沙箱请求一次性打出去，训练集稍大就
        足以压垮 Hermes 的调度器。
        """
        semaphore = asyncio.Semaphore(self.deps.concurrency_limit())
        backend = self.deps.backend()

        async def run_once(case: TestCase, run_index: int) -> ExecutionTrace:
            request = ExecutionRequest(
                skill=skill,
                case=case,
                run_index=run_index,
                run_id=run_id,
                wall_clock_timeout_s=self.deps.execution_timeout_s,
            )
            async with semaphore:
                return await backend.execute(request)

        async def run_case(case: TestCase) -> tuple[str, list[ExecutionTrace]]:
            traces = await asyncio.gather(
                *[
                    run_once(case, run_index_base + i)
                    for i in range(self.deps.redundant_runs)
                ]
            )
            return case.case_id, list(traces)

        # 后端的容错约定（docs/dev/03 第 6 节）：`execute()` 对超时/异常返回失败态
        # Trace，只有"评测系统自身故障"（沙箱建不起来）才抛 ExecutorBackendError。
        # 那类异常这里**不吞**——继续跑只会产出一份基于残缺证据的分数。
        results = await asyncio.gather(*[run_case(case) for case in cases])
        return dict(results)

    # ------------------------------------------------------------------ #
    # 3/6. 判定节点（训练集 / 验证集共用同一实现）
    # ------------------------------------------------------------------ #

    async def judge_train_cases(self, state: TriggerAccuracyState) -> dict[str, object]:
        verdict_ids, failed_ids = await self._judge_split(state, DatasetSplit.TRAIN)
        return {"judge_verdict_ids": verdict_ids, KEY_TRAIN_FAILED_CASE_IDS: failed_ids}

    async def judge_validation_cases(self, state: TriggerAccuracyState) -> dict[str, object]:
        """验证集判定。失败用例只写进报告，**不驱动任何重试**（防过拟合）。"""
        verdict_ids, failed_ids = await self._judge_split(state, DatasetSplit.VALIDATION)
        return {"judge_verdict_ids": verdict_ids, KEY_VALIDATION_FAILED_CASE_IDS: failed_ids}

    async def _judge_split(
        self, state: TriggerAccuracyState, split: DatasetSplit
    ) -> tuple[list[str], list[str]]:
        """按触发率量化规则逐条判定，返回 (verdict_ids, failed_case_ids)。

        判定走 `quantitative_verdict()`：纯算术、同步、不落库、不调 LLM，因此本维度
        不涉及 `Criticality` 声明，也不适用黄金基准盲测——盲测只挂在
        `judgmental_verdict()` 上，确定性规则引擎天然没有"幻觉"风险
        （docs/dev/11 第 10 节）。
        """
        run_id = str(state["run_id"])
        judge = self.deps.judge()
        verdict_ids: list[str] = []
        failed_case_ids: list[str] = []

        for case in await self._cases_for_split(state, split):
            # 只数**本维度自己**的冗余执行（run_index 0..redundant_runs-1）。
            # 同一条用例还会被模块三（docs/dev/13）拿去跑 A/B 对比，其中的基线
            # 分支按定义就是"不加载 Skill"；那些 Trace 落在 100 起的专用号段里
            # （见 `state/trace.py` 的 run_index 分配表），不筛掉的话它们会被
            # 当成"这次没触发"算进触发率，把一份正常的 Skill 判成不达标。
            all_traces = await self.deps.trace_repository.list_by_case(case.case_id)
            traces = [t for t in all_traces if t.run_index < self.deps.redundant_runs]
            inputs = rules.trigger_rate_inputs(traces)
            verdict = judge.quantitative_verdict(
                subject_id=case.case_id,
                rule_name=rules.rule_for_category(case.category),
                inputs=inputs,
            )
            verdict_ids.append(verdict.verdict_id)
            if verdict.status is JudgeVerdictStatus.FAIL:
                failed_case_ids.append(case.case_id)
                # 只归档失败判定：量化判定默认不落库（成百上千条通过判定没人读），
                # 但失败判定是 Optimizer 的输入、也是人工审查时唯一能看的证据，
                # 必须落库——`optimizer_loop` 正是按 subject_id 回读这些记录。
                await self.deps.judge_repository.save_verdict(verdict)

        logger.info(
            "trigger_accuracy_cases_judged",
            run_id=run_id,
            split=split.value,
            judged=len(verdict_ids),
            failed=len(failed_case_ids),
        )
        return verdict_ids, failed_case_ids

    # ------------------------------------------------------------------ #
    # 4. 条件路由
    # ------------------------------------------------------------------ #

    @staticmethod
    def route_after_train_judge(state: TriggerAccuracyState) -> str:
        """训练集有失败 → 进优化闭环；否则直接进验证集。

        返回的是**节点名**，供 `add_conditional_edges` 的映射表使用。
        """
        failed = _id_list(state, KEY_TRAIN_FAILED_CASE_IDS)
        return (
            NODE_NAMES["optimizer_loop"] if failed else NODE_NAMES["execute_validation_cases"]
        )

    # ------------------------------------------------------------------ #
    # 5. optimizer_loop
    # ------------------------------------------------------------------ #

    async def optimizer_loop(self, state: TriggerAccuracyState) -> dict[str, object]:
        """接入 docs/dev/09 的通用闭环：重写 description → 重跑训练集 → 收敛或挂起。

        本节点只提供闭环的两个缺口：
        1. `FailureContext`——哪份 Skill、哪些训练集用例失败了、裁判怎么说的；
        2. `retest_fn`——"补好了"在本维度意味着"这些失败的训练集用例按触发率规则
           重新判定后全部通过"。

        `build_failure_context()` 会拒绝任何非训练集用例，这是第二道闸；第一道闸是
        图结构里根本没有"验证集失败 → 本节点"的边。
        """
        run_id = str(state["run_id"])
        skill = await self._effective_skill(state)
        failed_case_ids = _id_list(state, KEY_TRAIN_FAILED_CASE_IDS)
        failed_cases = await self.deps.test_case_repository.list_by_ids(failed_case_ids)
        verdicts = await self._failed_verdicts(failed_case_ids)

        ctx = build_failure_context(
            skill,
            failed_cases,
            verdicts,
            role=ROLE_PROMPT_ENGINEER,
            extra_instructions=(
                "判定依据是「Agent 在执行过程中是否加载了这份 Skill 的 SKILL.md」，"
                f"每条用例跑 {self.deps.redundant_runs} 次，正向用例触发率需 >= "
                f"{rules.TRIGGER_RATE_THRESHOLD}，反向用例触发率需 < "
                f"{rules.TRIGGER_RATE_THRESHOLD}。"
                "改写 description 时请同时兼顾这两个方向：只把描述写宽，反向用例就会"
                "开始误触发。"
            ),
        )

        # 闭环内部逐轮把补丁叠加到上一轮的产物上，所以"最终生效的那份 Skill"只能
        # 从 retest_fn 收到的参数里捞——`apply_patch(原始skill, 最后一个patch)` 会因为
        # `base_skill_version_ref` 对不上而直接抛 PatchApplyError（第 2 轮之后必然如此）。
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
            return LoopResult(
                passed=True, detail=f"{len(failed_cases)} 条失败训练用例全部恢复通过"
            )

        patch = await self.deps.loop().run(
            run_id=run_id,
            ctx=ctx,
            retest_fn=retest_fn,
            optimizer=self.deps.optimizer(),
        )
        if patch is None:
            # 闭环耗尽重试后已经 `suspend_and_wait()` 挂起过一次；走到这里意味着人工
            # 明确选择了"放弃该补丁"。此时既没有可用的 working_skill，也不该拿旧
            # description 去跑验证集假装无事发生——直接判该 Skill 评测失败。
            raise PipelineSuspended(
                f"{NODE_NAMES['optimizer_loop']}：优化闭环超出最大重试次数且人工未采纳补丁，"
                f"run_id={run_id}，失败训练用例={failed_case_ids}"
            )

        working_skill = self._resolve_working_skill(skill, attempted_skills, patch.patch_id)
        logger.info(
            "trigger_accuracy_patch_adopted",
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
        """用打了补丁的 Skill 重跑失败的训练用例，返回仍未通过的 case_id。

        重跑**不经过 Generator**：闭环需要的不是新题，而是同一批题在新 description
        下的表现（docs/dev/interfaces/06 第 5 节）。

        重跑产生的 Trace 直接落库：`execution_traces` 的唯一键是
        `(case_id, run_index)`，同一条用例的新一轮结果会覆盖旧的——这正是我们要的
        语义，判定永远只看"当前这版 description 的表现"。
        """
        traces_by_case = await self.run_cases(run_id, working_skill, failed_cases)
        judge = self.deps.judge()
        still_failing: list[str] = []
        for case in failed_cases:
            traces = traces_by_case.get(case.case_id, [])
            for trace in traces:
                await self.deps.trace_repository.save(trace)
            verdict = judge.quantitative_verdict(
                subject_id=case.case_id,
                rule_name=rules.rule_for_category(case.category),
                inputs=rules.trigger_rate_inputs(traces),
            )
            if verdict.status is JudgeVerdictStatus.FAIL:
                still_failing.append(case.case_id)
        return still_failing

    async def _failed_verdicts(self, case_ids: Sequence[str]) -> list[JudgeVerdict]:
        """回读失败用例的判定记录，作为 Optimizer 的失败证据。

        `_judge_split()` 只把 FAIL 的量化判定写了库，所以这里再按 status 过滤一次
        主要是为了防御历史遗留记录（同一 case 在更早的运行里可能通过过）。
        """
        verdicts: list[JudgeVerdict] = []
        for case_id in case_ids:
            recorded = await self.deps.judge_repository.list_verdicts(case_id)
            verdicts.extend(v for v in recorded if v.status is JudgeVerdictStatus.FAIL)
        return verdicts

    @staticmethod
    def _resolve_working_skill(
        base_skill: SkillDefinition,
        attempted_skills: Sequence[SkillDefinition],
        patch_id: str,
    ) -> SkillDefinition:
        """在闭环用过的若干工作副本里，认出与最终采纳的补丁对应的那一份。

        `working_version_ref(base, patch_id)` 是补丁应用后版本号的构造规则
        （`<base>+patch:<patch_id>`），拿它反查即可精确命中；没命中时退化为"最后
        一次重测用的那份"——那种情况只可能出现在人工采纳了一个**应用失败**的候选
        补丁时，此时最后一次重测过的副本是我们手上最接近的可用版本，比拿完全没
        打过补丁的原始版本去跑验证集更诚实。
        """
        for skill in reversed(attempted_skills):
            if skill.version_ref == working_version_ref(base_skill.version_ref, patch_id):
                return skill
        return attempted_skills[-1] if attempted_skills else base_skill

    # ------------------------------------------------------------------ #
    # 6. finalize_dimension_report
    # ------------------------------------------------------------------ #

    async def finalize_dimension_report(self, state: TriggerAccuracyState) -> dict[str, object]:
        """把本维度的结论写进 `dimension_results`（docs/dev/05 第 6 节的落地调用）。

        分数只看**验证集**：训练集的失败已经由优化闭环处理过，拿它算分等于把"改过
        之后当然会通过"计入成绩。验证集才是没被优化过的、能反映泛化能力的那一半。
        """
        run_id = str(state["run_id"])
        validation_ids = _id_list(state, KEY_VALIDATION_CASE_IDS)
        failed_ids = _id_list(state, KEY_VALIDATION_FAILED_CASE_IDS)
        total = len(validation_ids)
        failed = len(failed_ids)

        findings: list[str] = []
        if total:
            score: float | None = 1 - failed / total
            status = JudgeVerdictStatus.PASS if failed == 0 else JudgeVerdictStatus.FAIL
            findings.append(f"验证集 {failed}/{total} 条用例触发判定失败")
        else:
            # 一条验证集用例都没有 = 这个维度这次什么都没测到。判 PASS 等于把维度
            # 悄悄关掉（分数好看，但没有任何证据支撑），所以交给人看一眼：
            # `NEEDS_HUMAN_REVIEW` 不会被 `BenchmarkReport.blocking` 当成阻断失败，
            # 但会在总状态里显式暴露出来。
            score = None
            status = JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
            findings.append(
                "验证集为空：本次运行没有可用于泛化能力评估的用例，触发准确度分数不成立。"
                "请检查测试集生成结果与 60/40 划分。"
            )

        train_failed = _id_list(state, KEY_TRAIN_FAILED_CASE_IDS)
        if train_failed:
            patch_id = state.get(KEY_APPLIED_PATCH_ID)
            findings.append(
                f"训练集 {len(train_failed)} 条用例初测失败，已进入 description 优化闭环"
                + (f"，采纳补丁 {patch_id}" if patch_id else "")
            )
        staleness = state.get(KEY_SUITE_STALENESS_WARNING)
        if staleness:
            findings.append(str(staleness))

        await self.deps.reporter().record_dimension_result(
            run_id=run_id,
            dimension=DIMENSION,
            status=status,
            score=score,
            findings=findings,
            blocking=BLOCKING,
        )
        logger.info(
            "trigger_accuracy_dimension_recorded",
            run_id=run_id,
            node_name=TERMINAL_NODE,
            status=status.value,
            score=score,
            validation_failed=failed,
            validation_total=total,
        )
        return {}

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    async def _load_base_skill(self, state: TriggerAccuracyState) -> SkillDefinition:
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

    async def _effective_skill(self, state: TriggerAccuracyState) -> SkillDefinition:
        """当前该拿哪份 Skill 去执行：优化闭环产出的工作副本优先，否则用库里的原版。

        这条优先级在训练集执行节点同样生效——`optimizer_loop` 内部的多轮重试会
        逐轮递进，图层面重跑训练集时应当接着上一轮的结果，而不是回到原始版本。
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

    async def _cases_for_split(
        self, state: TriggerAccuracyState, split: DatasetSplit
    ) -> list[TestCase]:
        key = KEY_TRAIN_CASE_IDS if split is DatasetSplit.TRAIN else KEY_VALIDATION_CASE_IDS
        case_ids = _id_list(state, key)
        cases = await self.deps.test_case_repository.list_by_ids(case_ids)
        return _in_given_order(cases, case_ids)


__all__ = [
    "BLOCKING",
    "ENTRY_NODE",
    "NODE_NAMES",
    "TERMINAL_NODE",
    "TriggerAccuracyPipeline",
]
