"""模块九子图装配（docs/dev/19 第 2 节的节点序列）。

两种用法，同一份边定义（与其余维度的 `graph.py` 对称）：

- `add_cross_model_nodes(builder, deps)`——把六个节点**平铺**进主图，供 docs/dev/24 使用；
- `build_cross_model_subgraph(deps)`——单独编出一张只含本维度的图，供本地调试与集成测试。

## 并行扇出 + 汇合

三条对照实验互不依赖，从 `prepare_cross_model_sample` 扇出并行执行，再用
`add_edge([三条支路], linguistic_smell_check)` 汇合——LangGraph 对"多源一目标"的边
会**等所有源节点都完成**才触发目标节点，因此语言坏味道审查与收尾节点看到的一定是
三条支路都写完的状态。三条支路各写自己的私有结果键（见 `state.py`），不存在并行写
同一个"后写胜"键互相覆盖的问题；`executed_trace_ids` / `judge_verdict_ids` 是 add
reducer，并行追加是安全的。

`INTERRUPT_BEFORE_NODES` 为空：本维度不产生补丁、不进优化闭环。共识门控的挂起点
挂在使用它的那个闭环节点上（例如 `trigger_accuracy.optimizer_loop`），不在这里。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph

from skill_evaluate.nodes.cross_model.deps import CrossModelDeps
from skill_evaluate.nodes.cross_model.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    PROBE_NODES,
    TERMINAL_NODE,
    CrossModelPipeline,
)
from skill_evaluate.nodes.cross_model.state import CrossModelState

INTERRUPT_BEFORE_NODES: list[str] = []

type CrossModelGraph = StateGraph[CrossModelState, Any, CrossModelState, CrossModelState]


def add_cross_model_nodes(
    builder: CrossModelGraph, deps: CrossModelDeps | None = None
) -> CrossModelPipeline:
    """把模块九的节点与内部边加入给定的 `StateGraph`（只加维度内部的边）。

    返回 `CrossModelPipeline`，便于调用方复用同一份依赖——例如模块一/五想给自己的
    优化闭环叠加共识门控时，可以直接拿 `pipeline.deps.secondary()` 当备用代理实例。
    """
    pipeline = CrossModelPipeline(deps)

    builder.add_node(NODE_NAMES["prepare_cross_model_sample"], pipeline.prepare_cross_model_sample)
    builder.add_node(
        NODE_NAMES["heterogeneous_execution_matrix"], pipeline.heterogeneous_execution_matrix
    )
    builder.add_node(
        NODE_NAMES["parameter_perturbation_robustness_probe"],
        pipeline.parameter_perturbation_robustness_probe,
    )
    builder.add_node(
        NODE_NAMES["stochastic_ablation_testing"], pipeline.stochastic_ablation_testing
    )
    builder.add_node(NODE_NAMES["linguistic_smell_check"], pipeline.linguistic_smell_check)
    builder.add_node(NODE_NAMES["finalize_dimension_report"], pipeline.finalize_dimension_report)

    for probe_node in PROBE_NODES:
        builder.add_edge(ENTRY_NODE, probe_node)
    builder.add_edge(list(PROBE_NODES), NODE_NAMES["linguistic_smell_check"])
    builder.add_edge(NODE_NAMES["linguistic_smell_check"], TERMINAL_NODE)
    return pipeline


def build_cross_model_subgraph(deps: CrossModelDeps | None = None) -> CrossModelGraph:
    """独立的模块九子图（未 compile；checkpointer 属于主图的编译期决策，理由同其余维度）。

    ⚠️ 默认主代理是 Hermes（callback 挂起），`llama_control` 的 callback 模式同样挂起——
    两者都要求 `compile(checkpointer=...)`。
    """
    builder: CrossModelGraph = StateGraph(CrossModelState)
    add_cross_model_nodes(builder, deps)
    builder.set_entry_point(ENTRY_NODE)
    builder.set_finish_point(TERMINAL_NODE)
    return builder


__all__ = ["INTERRUPT_BEFORE_NODES", "add_cross_model_nodes", "build_cross_model_subgraph"]
