"""模块二子图装配（docs/dev/12 第 2 节的节点序列）。

两种用法，同一份边定义（与模块一 `nodes/trigger_accuracy/graph.py` 对称）：

- `add_context_scoping_nodes(builder, deps)`——把四个节点**平铺**进主图，供
  docs/dev/24 使用；只加维度内部的边，与主图其余部分的连线由主图决定。
- `build_context_scoping_subgraph(deps)`——单独编出一张只含本维度的图，供本地
  调试与集成测试，不必拉起其余九个维度。

**本维度没有条件路由**：四个节点一条直线。架构文档模块二没有失败重试机制——
静态审查发现的问题直接报告给人改，不进 Optimizer 闭环（正文质量的改写涉及作者
意图，不是 description 那种可以由模型闭环收敛的局部改动）。

`INTERRUPT_BEFORE_NODES` 为空同理：本维度不产生需要人工审批才能继续的动作。
仍然导出这个常量，是为了让 docs/dev/24 汇总各维度时能无差别地 `[*A, *B, ...]`，
不必给"这个维度有没有挂起点"写特例。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph

from skill_evaluate.nodes.context_scoping.deps import ContextScopingDeps
from skill_evaluate.nodes.context_scoping.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    TERMINAL_NODE,
    ContextScopingPipeline,
)
from skill_evaluate.nodes.context_scoping.state import ContextScopingState

INTERRUPT_BEFORE_NODES: list[str] = []

# 状态类型别名，省得每个签名里重复写四个泛型参数——LangGraph 1.x 的 `StateGraph`
# 是 `Generic[StateT, ContextT, InputT, OutputT]`。
type ContextScopingGraph = StateGraph[
    ContextScopingState, Any, ContextScopingState, ContextScopingState
]


def add_context_scoping_nodes(
    builder: ContextScopingGraph, deps: ContextScopingDeps | None = None
) -> ContextScopingPipeline:
    """把模块二的节点与内部边加入给定的 `StateGraph`。

    返回 `ContextScopingPipeline` 实例，便于调用方复用同一份依赖（例如 docs/dev/16
    的覆盖率维度想直接调 `static_metrics_scan` 拿指标，而不是再建一套依赖）。
    """
    pipeline = ContextScopingPipeline(deps)

    builder.add_node(NODE_NAMES["static_metrics_scan"], pipeline.static_metrics_scan)
    builder.add_node(
        NODE_NAMES["progressive_disclosure_static_scan"],
        pipeline.progressive_disclosure_static_scan,
    )
    builder.add_node(NODE_NAMES["mini_agent_peer_review"], pipeline.mini_agent_peer_review)
    builder.add_node(
        NODE_NAMES["finalize_dimension_report"], pipeline.finalize_dimension_report
    )

    # 串行而不是并行跑两个扫描节点：`progressive_disclosure_static_scan` 要用前一个
    # 节点算出的 Token 数（判断"体量接近限额却没有 references/"），并行会让它读到
    # 空状态而退化成用库里的旧字段。两个节点都是纯计算，串行的代价可以忽略。
    builder.add_edge(
        NODE_NAMES["static_metrics_scan"], NODE_NAMES["progressive_disclosure_static_scan"]
    )
    builder.add_edge(
        NODE_NAMES["progressive_disclosure_static_scan"], NODE_NAMES["mini_agent_peer_review"]
    )
    builder.add_edge(
        NODE_NAMES["mini_agent_peer_review"], NODE_NAMES["finalize_dimension_report"]
    )
    return pipeline


def build_context_scoping_subgraph(
    deps: ContextScopingDeps | None = None,
) -> ContextScopingGraph:
    """独立的模块二子图（未 compile）。

    不在这里 `compile()`：checkpointer 属于主图的编译期决策（docs/dev/24），子图
    自己 compile 一次会得到一张没有 checkpointer 的图。本维度虽然不挂起，但断点
    恢复仍然依赖 checkpointer——主图跑到一半崩了，本维度已完成的扫描结果不该
    因为它自己那张子图没有 checkpointer 而丢失。
    调用方按需 `build_context_scoping_subgraph().compile(checkpointer=...)`。
    """
    builder: ContextScopingGraph = StateGraph(ContextScopingState)
    add_context_scoping_nodes(builder, deps)
    builder.set_entry_point(ENTRY_NODE)
    builder.set_finish_point(TERMINAL_NODE)
    return builder


__all__ = [
    "INTERRUPT_BEFORE_NODES",
    "add_context_scoping_nodes",
    "build_context_scoping_subgraph",
]
