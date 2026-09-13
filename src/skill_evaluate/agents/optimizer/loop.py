"""通用闭环编排器（docs/dev/09 第 5 节）。

架构文档里"失败 → 重写 → 再测试"这个闭环出现在模块一（description 优化）和
模块五（安全补丁）两处。如果各写一套，挂起条件、重试计数、补丁记录格式必然
互相漂移。这里把闭环编排实现一次，各维度只提供两样东西：

1. 一个 `FailureContext`（怎么描述这次失败）；
2. 一个 `retest_fn`（怎么算"补好了"）。

`retest_fn` 是本文档最重要的复用点：`OptimizationLoop` **不知道**"重新测试"具体
意味着什么。模块一的 `retest_fn` 是"用训练集用例重跑 Executor+Judge"；模块五的
`retest_fn` 是"重跑该安全用例 **并且** 触发一次全量功能回归"（架构文档要求安全
补丁必须强制回归，防止修安全把业务修坏）。把这件事交给回调，是这套编排能同时
服务两种语义完全不同的闭环的唯一原因。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from skill_evaluate.agents.optimizer.patch_applier import apply_patch
from skill_evaluate.agents.optimizer.schema import FailureContext
from skill_evaluate.agents.optimizer.service import OptimizerAgent
from skill_evaluate.config import get_settings
from skill_evaluate.errors import PatchApplyError
from skill_evaluate.logging import get_logger
from skill_evaluate.persistence.approval_service import ApprovalService
from skill_evaluate.persistence.repository import (
    HumanApprovalRepository,
    PatchRepository,
    PipelineStateRepository,
)
from skill_evaluate.state.approval import OUTCOME_ADOPT, ApprovalDecisionType
from skill_evaluate.state.patch import Patch, PatchApplicationResult
from skill_evaluate.state.skill import SkillDefinition

logger = get_logger(component="optimization_loop")

# 达到最大重试次数后挂起时，人工侧回传的 resume payload 约定（docs/dev/22 接住）。
# 取值：`adopt` = 采纳当前候选补丁；其余（含 None）= 放弃，判该 Skill 评测最终失败。
RESUME_ADOPT = OUTCOME_ADOPT  # docs/dev/22 起与 state/approval.py 的 outcome 常量同源


class LoopResult(BaseModel):
    """`retest_fn` 的返回值。`detail` 会原样进入 `PatchApplicationResult.detail`，
    最终出现在人工审批卡片上——写清楚"为什么没过"，不要只写 False。"""

    passed: bool
    detail: str = ""


RetestFn = Callable[[SkillDefinition], Awaitable[LoopResult]]


class OptimizationLoop:
    """提补丁 → 应用 → 重测，最多 N 轮，仍不过则**安全挂起**。"""

    def __init__(
        self,
        max_retries: int | None = None,
        *,
        patch_repository: PatchRepository | None = None,
        pipeline_state_repository: PipelineStateRepository | None = None,
        approval_repository: HumanApprovalRepository | None = None,
        approval_service: ApprovalService | None = None,
    ) -> None:
        self.max_retries = (
            get_settings().optimizer.max_retries if max_retries is None else max_retries
        )
        self._patch_repo = patch_repository or PatchRepository()
        self._state_repo = pipeline_state_repository or PipelineStateRepository()
        # docs/dev/22：挂起统一走 ApprovalService（账本 + 工作台卡片 + Discord + 挂起）。
        # 保留 `approval_repository` 参数的向后兼容：只传它时，账本仍写进这个仓储。
        self._approval_service = approval_service or ApprovalService(
            ledger_repository=approval_repository or HumanApprovalRepository()
        )

    async def run(
        self,
        run_id: str,
        ctx: FailureContext,
        retest_fn: RetestFn,
        optimizer: OptimizerAgent,
        *,
        thread_id: str | None = None,
    ) -> Patch | None:
        """跑完整个闭环。

        返回值语义：
        - `Patch`：某一轮的补丁通过了 `retest_fn`，可以进入人工审批/合并流程；
        - `None`：耗尽重试次数后挂起，且人工选择了放弃。

        **达到 `max_retries` 后是挂起而不是判负**（架构文档要求"安全挂起状态机"）：
        自动优化解决不了的问题，往往正是最需要人看一眼的问题；直接判负会把它变成
        一条 CI 红灯，而人看不到候选补丁长什么样。

        `working_skill` 逐轮迭代（第 2 轮的补丁打在第 1 轮的结果上），因为第 1 轮
        改动往往部分有效；每轮都从原始版本重开会让模型反复走同一条死路。
        """
        node_name = f"optimizer:{ctx.role}"
        working_skill = ctx.skill
        last_patch: Patch | None = None

        for attempt in range(self.max_retries):
            attempt_ctx = ctx.model_copy(update={"skill": working_skill})
            patch = await optimizer.propose_patch(attempt_ctx)
            last_patch = patch

            try:
                patched_skill = apply_patch(working_skill, patch)
            except PatchApplyError as exc:
                # 同一个 patch 不重试（diff 对不上，再打一次还是对不上），直接进入
                # 下一轮 propose_patch 让模型基于同样的上下文重出一版。
                await self._patch_repo.save_application_result(
                    PatchApplicationResult(
                        patch_id=patch.patch_id,
                        applied=False,
                        regression_passed=None,
                        detail=f"补丁应用失败：{exc}",
                    )
                )
                logger.warning(
                    "optimizer_patch_apply_failed",
                    run_id=run_id,
                    node_name=node_name,
                    attempt=attempt,
                    patch_id=patch.patch_id,
                    error=str(exc)[:500],
                )
                await self._state_repo.increment_retry(run_id, node_name)
                continue

            result = await retest_fn(patched_skill)
            await self._patch_repo.save_application_result(
                PatchApplicationResult(
                    patch_id=patch.patch_id,
                    applied=True,
                    regression_passed=result.passed,
                    working_skill_version_ref=patched_skill.version_ref,
                    detail=result.detail,
                )
            )

            if result.passed:
                logger.info(
                    "optimizer_loop_succeeded",
                    run_id=run_id,
                    node_name=node_name,
                    attempt=attempt,
                    patch_id=patch.patch_id,
                    working_skill_version_ref=patched_skill.version_ref,
                )
                return patch

            logger.warning(
                "optimizer_retest_failed",
                run_id=run_id,
                node_name=node_name,
                attempt=attempt,
                patch_id=patch.patch_id,
                detail=result.detail,
            )
            await self._state_repo.increment_retry(run_id, node_name)
            working_skill = patched_skill

        return await self._suspend(run_id, node_name, ctx, thread_id, last_patch)

    async def _suspend(
        self,
        run_id: str,
        node_name: str,
        ctx: FailureContext,
        thread_id: str | None,
        last_patch: Patch | None,
    ) -> Patch | None:
        """耗尽重试后挂起等待人工裁决（docs/dev/22 `ACCEPT_PATCH` 卡片）。

        `thread_id` 未显式传入时由 ApprovalService 按 `f"{skill_id}:{run_id}"` 解析——与
        `persistence/checkpointer.py` 及 Hermes Hook 的唤醒口径一致（docs/dev/09 当初默认的
        `thread_id = run_id` 会让人工批准后唤醒一个不存在的 thread）。
        """
        wait_key = f"{run_id}:{node_name}"
        logger.error(
            "optimizer_max_retries_exceeded",
            run_id=run_id,
            node_name=node_name,
            max_retries=self.max_retries,
            wait_key=wait_key,
            candidate_patch_id=last_patch.patch_id if last_patch else None,
        )

        decision: Any = await self._approval_service.request_human_approval(
            run_id=run_id,
            wait_key=wait_key,
            decision_type=ApprovalDecisionType.ACCEPT_PATCH,
            context_summary=(
                f"优化闭环（角色 {ctx.role}）连续 {self.max_retries} 轮补丁均未通过重测，"
                f"Skill `{ctx.skill.skill_id}`。请查看候选补丁 diff 与每轮重测结果，"
                "决定采纳最后一个候选补丁（adopt）或放弃本次评测（abandon）。"
            ),
            # 工作台据此展开 patches / patch_application_results / node_retry_counts。
            context_ref={
                "patch_id": last_patch.patch_id if last_patch else None,
                "role": ctx.role,
                "skill_id": ctx.skill.skill_id,
                "skill_version_ref": ctx.skill.version_ref,
                "max_retries": self.max_retries,
            },
            node_name=node_name,
            skill_id=ctx.skill.skill_id,
            thread_id=thread_id,
            reason=f"optimizer_max_retries_exceeded:{node_name}",
        )
        if _is_adopt(decision):
            logger.warning(
                "optimizer_patch_adopted_by_human",
                run_id=run_id,
                node_name=node_name,
                patch_id=last_patch.patch_id if last_patch else None,
            )
            return last_patch
        return None


def _is_adopt(decision: Any) -> bool:
    """解析人工回传的 resume payload。

    形状约定给 docs/dev/22 用，尽量宽容：`"adopt"`、`{"decision": "adopt"}`、
    `{"adopt_patch": true}` 都算采纳；其余一律算放弃。**默认放弃**是刻意的——
    一个没被明确批准的补丁不该因为 payload 形状没对上就被当成批准。
    """
    if decision is None:
        return False
    if isinstance(decision, str):
        return decision.strip().lower() == RESUME_ADOPT
    if isinstance(decision, dict):
        if decision.get("adopt_patch") is True:
            return True
        value = decision.get("decision")
        return isinstance(value, str) and value.strip().lower() == RESUME_ADOPT
    return False


__all__ = ["RESUME_ADOPT", "LoopResult", "OptimizationLoop", "RetestFn"]
