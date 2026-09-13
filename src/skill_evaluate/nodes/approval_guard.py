"""节点级人工审批 guard：把"会让流水线停下"的异常统一转成审批卡片（docs/dev/22 第 3、8 节）。

## 为什么需要一个节点级 guard

docs/dev/08/15/19/21 等维度约定"**不吞**这些异常，交给 22 接住"：

| 异常 | 来源 | 本 guard 的处理 |
|---|---|---|
| `JudgeFrozenError` | 任何走 `judgmental_verdict()` 的节点 | 阻塞审批 `UNFREEZE_JUDGE`：解冻后重跑节点 |
| `GenerationCollapseError(requires_human_seed=True)` | 调 `TestSuiteService` 出题的节点 | 阻塞审批 `INJECT_NEW_SEED`：补种子后重跑 |
| `InfrastructureEnvironmentError` | 前置门禁（21） | **仅通知**（非阻塞卡片）后原样抛出：修好环境重跑整条流水线即可，没有"批准继续"的语义（interfaces/21 第 3.2 节） |
| `HumanRejectedSuspension` | 人已在卡片上说"不"之后 | 原样抛出，不再问第二遍 |
| 其余 `PipelineSuspended`（共识未达成等） | 各维度 | 阻塞审批 `ABANDON_RUN`：重试节点或确认放弃 |

这些异常可能从**任何**节点冒出来（Judge 冻结尤其如此），逐个节点手写 try/except 必然漏；
而在异常发生处（Judge/Generator 内部）挂起又不对——那里不一定处于图节点上下文，也拿不到
run_id。节点边界是唯一同时满足"在图上下文里"与"知道 run_id/skill_id"的位置。

## 用法（docs/dev/24 装配主图时）

```python
from skill_evaluate.nodes.approval_guard import ApprovalGuardedBuilder
guarded = ApprovalGuardedBuilder(builder)          # 代理 StateGraph，只拦截 add_node
add_trigger_accuracy_nodes(guarded)                # 各维度的 add_*_nodes 一行不改
add_security_nodes(guarded)
```

## "重试"的实现与 LangGraph 恢复语义

动态 `interrupt()` 恢复时节点**整体重跑**，第 k 次 `interrupt()` 调用拿到第 k 个 resume 值。
guard 的循环因此是确定性可重放的：

1. 第一次执行：内层抛错 → 第 0 轮审批（wait_key 带 `:0`）→ 挂起；
2. 人批准重试 → 节点重跑：内层再次执行——若问题已解决（如已解冻）直接返回；若仍抛错，
   第 0 轮 `interrupt()` 立即返回"重试" → 再跑一次内层 → 仍失败则发起第 1 轮（新 wait_key）。

每轮 wait_key 不同，工作台上是独立的卡片；轮数上限 `ApprovalSettings.max_approval_rounds_per_node`。
⚠️ 代价：每次恢复都会把此前每一轮的内层逻辑重放一遍（LLM/沙箱调用），因此上限默认只有 3。
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from skill_evaluate.config import get_settings
from skill_evaluate.errors import (
    GenerationCollapseError,
    HumanRejectedSuspension,
    InfrastructureEnvironmentError,
    JudgeFrozenError,
    PipelineSuspended,
)
from skill_evaluate.logging import get_logger
from skill_evaluate.persistence.approval_service import ApprovalService, get_approval_service
from skill_evaluate.state.approval import (
    OUTCOME_RETRY,
    OUTCOME_SEED_INJECTED,
    OUTCOME_UNFREEZE,
    ApprovalDecisionType,
)

logger = get_logger(component="approval_guard")

NodeFn = Callable[..., Awaitable[Any]]

# 审批卡片里附带的异常文本上限：共识未达成的报错会带 subject_id 等长串，
# 全塞进 context_ref 会让工作台列表接口的响应体失控。
_ERROR_EXCERPT_CHARS = 2000


def resume_decision(payload: Any) -> str | None:
    """从 resume payload 里取出人工的 outcome（`api/hooks_approval.py` 回传 `{"decision": ...}`）。

    兼容裸字符串（与 docs/dev/09/16 既有解析口径一致）。形状对不上返回 None——调用方一律
    按"未批准"处理：没被明确批准的操作不该因为 payload 形状没对上就被当成批准。
    """
    if isinstance(payload, str):
        return payload.strip().lower()
    if isinstance(payload, Mapping):
        value = payload.get("decision")
        if isinstance(value, str):
            return value.strip().lower()
    return None


@dataclass(frozen=True, slots=True)
class _ApprovalSpec:
    """一次捕获到的异常要发起什么样的审批。"""

    kind: str  # 进 wait_key，区分同一节点上不同原因的卡片
    decision_type: ApprovalDecisionType
    continue_outcome: str  # 人选了它 → 重跑内层；其余 → 放弃
    summary: str
    context_ref: dict[str, Any]


class ApprovalGuard:
    """把节点函数包成"遇到可人工恢复的异常就发审批卡片"的版本。"""

    def __init__(
        self,
        service: ApprovalService | None = None,
        *,
        max_rounds: int | None = None,
    ) -> None:
        # None = 每次用到时取进程级默认服务（主图装配早于 Discord 注册时也能用上真实通道）。
        self._service = service
        self._max_rounds = max_rounds

    def service(self) -> ApprovalService:
        return self._service or get_approval_service()

    def max_rounds(self) -> int:
        if self._max_rounds is None:
            return get_settings().approval.max_approval_rounds_per_node
        return self._max_rounds

    def wrap(self, node_name: str, fn: NodeFn) -> NodeFn:
        """返回包装后的节点函数。

        用 `functools.wraps` 保留原函数的类型注解与签名：LangGraph 按节点首参的类型注解
        推断输入 schema（维度私有状态键靠它才不被裁掉），并按签名决定是否注入 `config`。
        包装函数以 `**kwargs` 原样转发这些注入参数。
        """

        @functools.wraps(fn)
        async def guarded(state: Any, **kwargs: Any) -> Any:
            return await self._run(node_name, fn, state, kwargs)

        return guarded

    async def _run(self, node_name: str, fn: NodeFn, state: Any, kwargs: dict[str, Any]) -> Any:
        run_id = str(state.get("run_id", "")) if isinstance(state, Mapping) else ""
        skill_id = state.get("skill_id") if isinstance(state, Mapping) else None
        rounds = 0
        while True:
            error: Exception
            try:
                return await fn(state, **kwargs)
            except HumanRejectedSuspension:
                raise  # 人已经决定过了
            except InfrastructureEnvironmentError as exc:
                await self._notify_infrastructure(node_name, run_id, skill_id, exc)
                raise
            except JudgeFrozenError as exc:
                spec, error = self._judge_frozen_spec(exc), exc
            except GenerationCollapseError as exc:
                if not exc.requires_human_seed:
                    raise  # 未到连续上限：交给调用方按普通 GenerationError 处理
                spec, error = self._collapse_spec(exc), exc
            except PipelineSuspended as exc:
                spec, error = self._suspended_spec(node_name, exc), exc

            # 审批在 except 块**之外**发起：`interrupt()` 抛出的 GraphInterrupt 不该被链到
            # 原异常上，也不该被任何外层的 `except` 误当成业务异常。
            if not run_id or rounds >= self.max_rounds():
                logger.error(
                    "approval_guard_giving_up",
                    run_id=run_id,
                    node_name=node_name,
                    kind=spec.kind,
                    rounds=rounds,
                    reason="state 中缺少 run_id" if not run_id else "已达到人工审批轮数上限",
                )
                raise error

            decision = await self.service().request_human_approval(
                run_id=run_id,
                wait_key=f"{run_id}:{node_name}:{spec.kind}:{rounds}",
                decision_type=spec.decision_type,
                context_summary=spec.summary,
                context_ref={**spec.context_ref, "node_name": node_name, "round": rounds},
                node_name=node_name,
                skill_id=str(skill_id) if skill_id else None,
            )
            rounds += 1
            outcome = resume_decision(decision)
            if outcome == spec.continue_outcome:
                logger.warning(
                    "approval_guard_retrying_node",
                    run_id=run_id,
                    node_name=node_name,
                    kind=spec.kind,
                    round=rounds,
                )
                continue
            raise HumanRejectedSuspension(
                f"{node_name}：人工在 {spec.decision_type.value} 审批中选择放弃"
                f"（outcome={outcome!r}），run_id={run_id}。原始原因：{error}"
            ) from error

    # ------------------------------------------------------------------ #
    # 各类异常 → 审批规格
    # ------------------------------------------------------------------ #

    @staticmethod
    def _judge_frozen_spec(exc: JudgeFrozenError) -> _ApprovalSpec:
        return _ApprovalSpec(
            kind="judge_frozen",
            decision_type=ApprovalDecisionType.UNFREEZE_JUDGE,
            continue_outcome=OUTCOME_UNFREEZE,
            summary=(
                f"Judge 配置因黄金基准失误率超阈值被冻结（model={exc.model!r}），"
                "被冻结的裁判给出的结论不能进报告，流水线已停住。请先调整 Judge Prompt 或更换"
                "模型，再选择 unfreeze（解冻并重跑该节点）或 abandon（放弃本次评测）。"
            ),
            # 解冻 API 需要 model + temperature 才能定位 (model, temperature_bucket)。
            context_ref={
                "model": exc.model,
                "temperature": exc.temperature,
                "error": str(exc)[:_ERROR_EXCERPT_CHARS],
            },
        )

    @staticmethod
    def _collapse_spec(exc: GenerationCollapseError) -> _ApprovalSpec:
        return _ApprovalSpec(
            kind="generation_collapse",
            decision_type=ApprovalDecisionType.INJECT_NEW_SEED,
            continue_outcome=OUTCOME_SEED_INJECTED,
            summary=(
                f"Skill `{exc.skill_id}` 的用例生成已连续 {exc.consecutive_collapses} 次被判定为"
                "语义坍塌，继续让机器重试只会原地打转。请向种子锚点库注入新的真实 Prompt 并运行 "
                "`skill-evaluate sync-seed-anchors`，然后选择 seed_injected（重新生成）或 abandon。"
            ),
            context_ref={
                "skill_id": exc.skill_id,
                "consecutive_collapses": exc.consecutive_collapses,
                "error": str(exc)[:_ERROR_EXCERPT_CHARS],
            },
        )

    @staticmethod
    def _suspended_spec(node_name: str, exc: PipelineSuspended) -> _ApprovalSpec:
        return _ApprovalSpec(
            kind="suspended",
            decision_type=ApprovalDecisionType.ABANDON_RUN,
            continue_outcome=OUTCOME_RETRY,
            summary=(
                f"节点 {node_name} 无法在无人介入的情况下给出可信结论，流水线已停住：{exc}。"
                "请查看证据后选择 retry（重跑该节点）或 abandon（确认放弃本次评测）。"
            )[:_ERROR_EXCERPT_CHARS],
            context_ref={
                "error_type": type(exc).__name__,
                "error": str(exc)[:_ERROR_EXCERPT_CHARS],
            },
        )

    async def _notify_infrastructure(
        self,
        node_name: str,
        run_id: str,
        skill_id: Any,
        exc: InfrastructureEnvironmentError,
    ) -> None:
        """前置门禁失败：非阻塞通知卡片（找运维修环境），通知本身失败不得掩盖原异常。"""
        if not run_id:
            return
        try:
            await self.service().notify_human(
                run_id=run_id,
                wait_key=f"{run_id}:{node_name}:infrastructure:{exc.gate}",
                decision_type=ApprovalDecisionType.ABANDON_RUN,
                context_summary=(
                    f"评测基础设施自检未通过（{exc.gate}），本次运行未产出任何维度结论，"
                    f"与被测 Skill 质量无关。请运维修复环境后重跑整条流水线。{exc}"
                )[:_ERROR_EXCERPT_CHARS],
                context_ref={"gate": exc.gate, "details": exc.details, "node_name": node_name},
                node_name=node_name,
                skill_id=str(skill_id) if skill_id else None,
            )
        except Exception as notify_exc:  # noqa: BLE001 - 旁路通知故障不得替换真正的失败原因
            logger.error(
                "approval_guard_infrastructure_notify_failed",
                run_id=run_id,
                node_name=node_name,
                error=str(notify_exc)[:500],
            )


class ApprovalGuardedBuilder:
    """`StateGraph` 的薄代理：`add_node` 时自动套上 `ApprovalGuard`，其余调用原样转发。

    做成代理而不是改各维度的 `add_*_nodes()`：十几个维度的装配函数只调用 `add_node` /
    `add_edge` / `add_conditional_edges`，代理后一行不用改；独立子图（本地调试）也照旧可以
    不套 guard。
    """

    def __init__(self, builder: Any, guard: ApprovalGuard | None = None) -> None:
        self._builder = builder
        self._guard = guard or ApprovalGuard()

    @property
    def builder(self) -> Any:
        """被代理的原始 `StateGraph`（compile 时用它）。"""
        return self._builder

    def add_node(self, node: Any, action: Any = None, **kwargs: Any) -> Any:
        if isinstance(node, str) and callable(action):
            action = self._guard.wrap(node, action)
        # 其余形态（`add_node(fn)` 以函数名为节点名、子图 Runnable）原样转发：前者本项目
        # 不使用，后者的异常发生在子图内部节点上，应当在子图装配时套 guard。
        return self._builder.add_node(node, action, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._builder, name)


__all__ = [
    "ApprovalGuard",
    "ApprovalGuardedBuilder",
    "resume_decision",
]
