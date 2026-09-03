"""模块二的私有状态命名空间（docs/dev/12；命名约定沿用 docs/dev/11 第 3 节）。

三条约定，与模块一一致：

1. 私有键一律以下划线 + 维度前缀开头，表示"本维度临时状态"，不属于 docs/dev/02
   `PipelineState` 的正式字段；
2. 各维度**不得**读写其他维度的私有键；
3. **不得**省略前缀直接复用公共字段名——LangGraph 的默认 reducer 是"后写胜"，
   主图里多个维度并行写同名键时不会报错，只会让某个维度悄悄拿到别人的数据。

⚠️ 主图（docs/dev/24）的状态 schema 必须包含本文件声明的私有键，否则它们会在
进入节点前被 LangGraph **静默裁掉**：本维度的表现会是"扫描结果永远是空的、但
一条错都不报"，然后给出一份看起来通过了的报告。见
`docs/dev/interfaces/12_context_scoping_static_pipeline.md` 第 2 节。

## 关于"存对象还是存 ID"

docs/dev/02 的原则是状态里只存引用。本维度有三个刻意的例外，理由与模块一的
`_working_skill` 相同——**这些东西不在库里，除了随状态走没有第二条路**：

- `_ctx_static_metrics` / `_ctx_disclosure_scan`：纯代码扫描的中间结果，本来就不
  该为了传给下一个节点而专门建一张表；
- `_ctx_peer_review_outcomes`：Mini Agent 三项审查的结论摘要。注意存的是**摘要**
  （`PeerReviewOutcome`）而不是整个 `JudgeVerdict`：verdict 已经由 Judge 侧落库，
  状态里再放一份整块 reasoning 只会让每个 Checkpoint 多背几十 KB。

三者都是 KB 级的小对象，Checkpoint 体积可接受。
"""

from __future__ import annotations

from typing import TypedDict

from skill_evaluate.state.pipeline_state import PipelineState

# 维度名。同时是 `NODE_BACKEND_ROUTING` 的键、`DimensionResult.dimension` 的取值、
# 以及各节点名的前缀——三处必须一致，因此收敛成一个常量。
DIMENSION = "context_scoping"

# ---- 私有状态键（字符串常量化，避免各节点各写各的字面量拼错） ----
KEY_STATIC_METRICS = "_ctx_static_metrics"
KEY_DISCLOSURE_SCAN = "_ctx_disclosure_scan"
KEY_PEER_REVIEW_OUTCOMES = "_ctx_peer_review_outcomes"


class ContextScopingState(PipelineState, total=False):
    """`PipelineState` + 模块二私有键的类型视图。

    节点签名必须用本类型而不是 `PipelineState`：LangGraph 按节点函数第一个参数的
    类型注解推导输入 schema，写 `PipelineState` 会让私有键在进入节点前被裁掉
    （见模块头的 ⚠️）。

    值的类型写 `dict` 而不是对应的 Pydantic 模型：Checkpoint 反序列化后拿回来的
    可能是 dict（取决于 serde 实现），节点侧统一用 `model_validate()` 收敛，
    不在类型注解上假装它一定还是模型实例。
    """

    _ctx_static_metrics: dict[str, object]  # StaticMetricsResult 的 dump
    _ctx_disclosure_scan: dict[str, object]  # ProgressiveDisclosureScan 的 dump
    _ctx_peer_review_outcomes: list[dict[str, object]]  # list[PeerReviewOutcome] 的 dump


__all__ = [
    "DIMENSION",
    "KEY_DISCLOSURE_SCAN",
    "KEY_PEER_REVIEW_OUTCOMES",
    "KEY_STATIC_METRICS",
    "ContextScopingState",
]
