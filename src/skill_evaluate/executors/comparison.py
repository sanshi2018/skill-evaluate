"""对照实验的执行骨架与证据口径（docs/dev/19 落地时新增，供模块九与共识门控共用）。

模块九的四件事——异构矩阵、参数扰动、随机消融、Pareto 共识门控——形状完全一样：
**同一批用例，在两个"臂"（arm）上各跑一遍，比较它们的表现是否一致**。区别只在
两条臂分别是什么（换后端 / 换采样参数 / 换 SKILL.md 文本 / 换补丁前后的版本）。

放在 `executors/` 而不是 `nodes/cross_model/`：共识门控住在
`agents/optimizer/consensus_gate.py`，而 `agents/` 层按项目约定不反向依赖 `nodes/`
（全仓库没有一处 `agents -> nodes` 的 import）。执行骨架与"什么样的 Trace 算有效
证据"本来就是执行层的语义，两边从这里取同一份，判定口径才不会漂移。

## "通过"的口径：触发行为是否符合用例类别的预期

docs/dev/19 正文用 `trace.loaded_skill_md` 直接当"通过"，这只对 POSITIVE 用例成立：
NEGATIVE（近脱靶）用例**加载了**才是失败。验证集里两类用例都有，照抄正文会把
"备用代理正确地没有误触发"读成"备用代理没通过"。因此这里统一用
`trigger_matches_expectation()`：POSITIVE 期望加载、NEGATIVE 期望不加载——与模块一
两条触发率规则的方向完全一致。

## 失败态 Trace 不是证据

后端对超时/沙箱故障返回的是 `loaded_skill_md=False` 的保守失败态 Trace
（docs/dev/03 第 6 节）。直接拿它算"是否符合预期"会出两类错：POSITIVE 用例被记成
"备用代理没触发"（凭空一条代理差异），NEGATIVE 用例被记成"备用代理正确地没触发"
（凭空一次通过）。所以先用 `is_conclusive_trace()` 滤掉，两条臂任一方没有有效证据
的用例记为"证据不足"，交给人看，而不是替它下结论。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from pydantic import BaseModel

from skill_evaluate.executors.base import ExecutionRequest, ExecutorBackend
from skill_evaluate.executors.hermes_backend import (
    ACTION_TYPE_INTERNAL_ERROR,
    ACTION_TYPE_SANDBOX_TIMEOUT,
)
from skill_evaluate.state.enums import TestCaseCategory
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase
from skill_evaluate.state.trace import ExecutionTrace

# 能用"触发行为是否符合预期"来衡量的用例类别。ADVERSARIAL（模块五）、MULTI_SKILL
# （模块十）、渐进式披露探查（模块三）各有各的判定语义，不能拿 loaded_skill_md 凑合。
COMPARABLE_CATEGORIES: tuple[TestCaseCategory, ...] = (
    TestCaseCategory.POSITIVE,
    TestCaseCategory.NEGATIVE,
)

# 失败态 Trace 末尾动作的取值（`hermes_backend.build_failure_trace()` 与
# `MiniAgentBackend` 的异常分支共用这两个值）。
_FAILURE_ACTION_TYPES = frozenset({ACTION_TYPE_INTERNAL_ERROR, ACTION_TYPE_SANDBOX_TIMEOUT})

# 与模块一的 `TRIGGER_RATE_THRESHOLD` 同值同义：多次执行时"符合预期的比例"跨过它
# 才算这条臂在这条用例上表现正确。单次执行（默认）时它退化为"这一次是否符合预期"。
EXPECTATION_RATE_THRESHOLD = 0.5


def is_conclusive_trace(trace: ExecutionTrace) -> bool:
    """这条 Trace 是否是一次**真的跑完了**的执行（而不是评测系统自身故障的占位）。"""
    if not trace.actions:
        return True
    return trace.actions[-1].action_type not in _FAILURE_ACTION_TYPES


def trigger_matches_expectation(trace: ExecutionTrace, category: TestCaseCategory) -> bool:
    """触发行为是否符合用例类别的预期（见模块头"通过的口径"）。"""
    if category is TestCaseCategory.POSITIVE:
        return trace.loaded_skill_md
    if category is TestCaseCategory.NEGATIVE:
        return not trace.loaded_skill_md
    raise ValueError(
        f"对照实验只比较 POSITIVE / NEGATIVE 用例的触发行为，收到 category={category!r}"
    )


class ArmEvidence(BaseModel):
    """一条臂在一条用例上的证据摘要。

    只存计数不存 Trace：它会被拼进量化规则的 `inputs`，而 `quantitative_verdict()`
    把 `inputs` 的 repr 原样写进 `JudgeVerdict.reasoning`（docs/dev/interfaces/11
    第 6 节第 2 条）。
    """

    expected_count: int  # 有效执行中符合预期的次数
    conclusive_count: int  # 有效执行次数（剔除了失败态 Trace）
    total_count: int  # 实际执行次数

    @property
    def rate(self) -> float | None:
        """符合预期的比例；一次有效执行都没有时返回 None（**不是** 0.0）。"""
        if self.conclusive_count <= 0:
            return None
        return self.expected_count / self.conclusive_count

    @property
    def behaved_as_expected(self) -> bool | None:
        rate = self.rate
        return None if rate is None else rate >= EXPECTATION_RATE_THRESHOLD


def summarize_arm(traces: Sequence[ExecutionTrace], category: TestCaseCategory) -> ArmEvidence:
    conclusive = [trace for trace in traces if is_conclusive_trace(trace)]
    return ArmEvidence(
        expected_count=sum(1 for t in conclusive if trigger_matches_expectation(t, category)),
        conclusive_count=len(conclusive),
        total_count=len(traces),
    )


async def run_arm(
    backend: ExecutorBackend,
    *,
    run_id: str,
    skill: SkillDefinition,
    cases: Sequence[TestCase],
    run_index_base: int,
    runs: int,
    timeout_s: int,
    semaphore: asyncio.Semaphore,
    sampling_overrides: dict[str, float] | None = None,
) -> dict[str, list[ExecutionTrace]]:
    """在一条臂上把每条用例跑 `runs` 次，返回 {case_id: [trace, ...]}。

    与模块一 `TriggerAccuracyPipeline.run_cases()` 同形，但**刻意另写一份**而不是复用：
    docs/dev/interfaces/11 第 4.2 节约定"需要 `sampling_overrides` 时不要改本维度的
    run_cases，在自己的维度里按同样的形状写一份"——请求的构造是各维度语义的一部分。

    `semaphore` 由调用方传入：同一个节点里两条臂是 `asyncio.gather` 并发跑的，必须
    共用**一个**信号量，否则并发上限会被悄悄翻倍。

    不吞 `ExecutorBackendError`（沙箱建不起来 = 评测系统自身故障），理由同模块一。
    """
    if runs < 1:
        raise ValueError(f"runs 必须 >= 1，收到 {runs}")

    async def run_once(case: TestCase, run_index: int) -> ExecutionTrace:
        request = ExecutionRequest(
            skill=skill,
            case=case,
            run_index=run_index,
            run_id=run_id,
            sampling_overrides=dict(sampling_overrides) if sampling_overrides else None,
            wall_clock_timeout_s=timeout_s,
        )
        async with semaphore:
            return await backend.execute(request)

    async def run_case(case: TestCase) -> tuple[str, list[ExecutionTrace]]:
        traces = await asyncio.gather(*(run_once(case, run_index_base + i) for i in range(runs)))
        return case.case_id, list(traces)

    results = await asyncio.gather(*(run_case(case) for case in cases))
    return dict(results)


__all__ = [
    "COMPARABLE_CATEGORIES",
    "EXPECTATION_RATE_THRESHOLD",
    "ArmEvidence",
    "is_conclusive_trace",
    "run_arm",
    "summarize_arm",
    "trigger_matches_expectation",
]
