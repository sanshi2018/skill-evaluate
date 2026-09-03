"""模块一子图装配（docs/dev/11 第 2 节的节点序列）。

提供两种用法，同一份边定义：

- `add_trigger_accuracy_nodes(builder, deps)`——把七个节点**平铺**进主图。
  docs/dev/24 的主图正是按平铺方式引用本维度的节点名（例如 Phase B 的
  `builder.add_edge("trigger_accuracy.prepare_test_suite", ...)` 依赖本维度产出的
  测试集），所以这是给主图用的入口。
- `build_trigger_accuracy_subgraph(deps)`——单独编出一张只含本维度的图，供本地
  调试与集成测试使用，不需要拉起其余九个维度。

图结构本身承担一条架构约束：**没有**"验证集判定失败 → optimizer_loop"这条边。
"验证集不参与优化以防止过拟合"因此是图结构层面的事实，而不只是
`build_failure_context()` 的运行时校验（docs/dev/11 第 8 节）。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph

from skill_evaluate.nodes.trigger_accuracy.deps import TriggerAccuracyDeps
from skill_evaluate.nodes.trigger_accuracy.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    TERMINAL_NODE,
    TriggerAccuracyPipeline,
)
from skill_evaluate.nodes.trigger_accuracy.state import TriggerAccuracyState

# docs/dev/24 第 3 节的编译期 `interrupt_before` 列表要汇总本维度这一项。
# 说明：`OptimizationLoop` 走的是**动态** `interrupt()`（超出重试次数时才挂起），
# 不加进静态列表也能正常挂起；列出来是为了让"这个节点可能停在人工审批上"在编译
# 期就是一件显式的事，而不是运行到那一刻才发现。
INTERRUPT_BEFORE_NODES = [NODE_NAMES["optimizer_loop"]]

# 主图与本子图的状态类型都是 `PipelineState`（docs/dev/02：全项目只有一张状态表，
# 各维度用私有键前缀共存，见 `state.py`）。别名一处定义，省得每个签名里重复写四个
# 泛型参数——LangGraph 1.x 的 `StateGraph` 是 `Generic[StateT, ContextT, InputT, OutputT]`。
type TriggerAccuracyGraph = StateGraph[
    TriggerAccuracyState, Any, TriggerAccuracyState, TriggerAccuracyState
]


def add_trigger_accuracy_nodes(
    builder: TriggerAccuracyGraph, deps: TriggerAccuracyDeps | None = None
) -> TriggerAccuracyPipeline:
    """把模块一的节点与内部边加入给定的 `StateGraph`。

    只加**维度内部**的边；与主图其余部分的连接（谁指向 `ENTRY_NODE`、
    `TERMINAL_NODE` 指向谁）由 docs/dev/24 决定，本函数不越界替主图连线。

    返回 `TriggerAccuracyPipeline` 实例，便于调用方复用同一份依赖（例如 docs/dev/19
    想借用本维度的执行逻辑跑跨模型矩阵）。
    """
    pipeline = TriggerAccuracyPipeline(deps)

    builder.add_node(NODE_NAMES["prepare_test_suite"], pipeline.prepare_test_suite)
    builder.add_node(NODE_NAMES["execute_train_cases"], pipeline.execute_train_cases)
    builder.add_node(NODE_NAMES["judge_train_cases"], pipeline.judge_train_cases)
    builder.add_node(NODE_NAMES["optimizer_loop"], pipeline.optimizer_loop)
    builder.add_node(NODE_NAMES["execute_validation_cases"], pipeline.execute_validation_cases)
    builder.add_node(NODE_NAMES["judge_validation_cases"], pipeline.judge_validation_cases)
    builder.add_node(
        NODE_NAMES["finalize_dimension_report"], pipeline.finalize_dimension_report
    )

    builder.add_edge(NODE_NAMES["prepare_test_suite"], NODE_NAMES["execute_train_cases"])
    builder.add_edge(NODE_NAMES["execute_train_cases"], NODE_NAMES["judge_train_cases"])
    builder.add_conditional_edges(
        NODE_NAMES["judge_train_cases"],
        TriggerAccuracyPipeline.route_after_train_judge,
        # 显式映射表而不是让 LangGraph 按返回值猜节点：路由函数返回的字符串就是
        # 节点名，写出映射能让"这个分支只可能去这两个地方"在图定义里一眼可见。
        {
            NODE_NAMES["optimizer_loop"]: NODE_NAMES["optimizer_loop"],
            NODE_NAMES["execute_validation_cases"]: NODE_NAMES["execute_validation_cases"],
        },
    )
    # 优化闭环收敛后回到验证集：此时状态里已带上 `_working_skill`，验证集会用打了
    # 补丁的版本重跑（docs/dev/11 第 7 节）。
    builder.add_edge(NODE_NAMES["optimizer_loop"], NODE_NAMES["execute_validation_cases"])
    builder.add_edge(
        NODE_NAMES["execute_validation_cases"], NODE_NAMES["judge_validation_cases"]
    )
    builder.add_edge(
        NODE_NAMES["judge_validation_cases"], NODE_NAMES["finalize_dimension_report"]
    )
    return pipeline


def build_trigger_accuracy_subgraph(
    deps: TriggerAccuracyDeps | None = None,
) -> TriggerAccuracyGraph:
    """独立的模块一子图（未 compile）。

    不在这里 `compile()`：checkpointer 与 `interrupt_before` 属于主图的编译期决策
    （docs/dev/24），子图自己 compile 一次会得到一个没有 checkpointer 的图，
    而 `HermesBackend.execute()` 与优化闭环的挂起都依赖 checkpointer。
    调用方按需 `build_trigger_accuracy_subgraph().compile(checkpointer=...)`。
    """
    builder: TriggerAccuracyGraph = StateGraph(TriggerAccuracyState)
    add_trigger_accuracy_nodes(builder, deps)
    builder.set_entry_point(ENTRY_NODE)
    builder.set_finish_point(TERMINAL_NODE)
    return builder


__all__ = [
    "INTERRUPT_BEFORE_NODES",
    "add_trigger_accuracy_nodes",
    "build_trigger_accuracy_subgraph",
]
