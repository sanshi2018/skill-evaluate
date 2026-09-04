"""模块四子图装配（docs/dev/14 第 3 节的节点序列）。

两种用法，同一份边定义（与模块一/二/三对称）：

- `add_script_usability_nodes(builder, deps)`——把六个节点**平铺**进主图，供
  docs/dev/24 使用；只加维度内部的边，与主图其余部分的连线由主图决定。
- `build_script_usability_subgraph(deps)`——单独编出一张只含本维度的图，供本地
  调试与集成测试，不必拉起其余九个维度。

## 为什么用静态并行边而不是 `Send`

三条探测支路是**结构不同的固定逻辑**（挂起/文档/脏数据），节点数在编译期就确定，
不存在"运行时才知道要派生多少个同构分支"的 map-reduce 场景。静态边的好处是图结构
在 `get_graph().draw()` 里看得见，`interrupt_before` 也能按节点名精确挂载。
逐个脚本的并发发生在**节点内部**（`asyncio.gather` + 信号量），与模块三同一取舍。

`INTERRUPT_BEFORE_NODES` 为空：本维度不产生需要人工审批才能继续的动作（没有优化
闭环——脚本缺陷的修复涉及脚本作者的实现意图，不是模型能闭环收敛的局部改写）。
仍然导出这个常量，是为了让 docs/dev/24 汇总各维度时能无差别地 `[*A, *B, ...]`，
不必给"这个维度有没有挂起点"写特例。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph

from skill_evaluate.nodes.script_usability.deps import ScriptUsabilityDeps
from skill_evaluate.nodes.script_usability.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    TERMINAL_NODE,
    ScriptUsabilityPipeline,
)
from skill_evaluate.nodes.script_usability.state import ScriptUsabilityState

INTERRUPT_BEFORE_NODES: list[str] = []

# 状态类型别名，省得每个签名里重复写四个泛型参数——LangGraph 1.x 的 `StateGraph`
# 是 `Generic[StateT, ContextT, InputT, OutputT]`。
type ScriptUsabilityGraph = StateGraph[
    ScriptUsabilityState, Any, ScriptUsabilityState, ScriptUsabilityState
]

# 三条并行探测支路的节点键。它们共同的上游是 `prepare_scripts`、共同的下游是
# `idempotency_and_safety_guards`，收敛成一个常量避免加边时漏掉某一条。
PARALLEL_PROBE_NODES: tuple[str, ...] = (
    "hard_failure_probing",
    "self_learning_doc_test",
    "constructive_error_and_io_separation_test",
)


def add_script_usability_nodes(
    builder: ScriptUsabilityGraph, deps: ScriptUsabilityDeps | None = None
) -> ScriptUsabilityPipeline:
    """把模块四的节点与内部边加入给定的 `StateGraph`。

    返回 `ScriptUsabilityPipeline` 实例，便于调用方复用同一份依赖（例如 docs/dev/15
    的红队维度想直接用同一个 `ScriptSandboxRunner` 做注入测试）。
    """
    pipeline = ScriptUsabilityPipeline(deps)

    builder.add_node(NODE_NAMES["prepare_scripts"], pipeline.prepare_scripts)
    builder.add_node(NODE_NAMES["hard_failure_probing"], pipeline.hard_failure_probing)
    builder.add_node(NODE_NAMES["self_learning_doc_test"], pipeline.self_learning_doc_test)
    builder.add_node(
        NODE_NAMES["constructive_error_and_io_separation_test"],
        pipeline.constructive_error_and_io_separation_test,
    )
    builder.add_node(
        NODE_NAMES["idempotency_and_safety_guards"], pipeline.idempotency_and_safety_guards
    )
    builder.add_node(NODE_NAMES["finalize_dimension_report"], pipeline.finalize_dimension_report)

    # 分叉：准备完成后三条探测支路并行。
    for key in PARALLEL_PROBE_NODES:
        builder.add_edge(NODE_NAMES["prepare_scripts"], NODE_NAMES[key])
    # 汇合：三条支路都结束后才做幂等性探测。
    #
    # 幂等性排在最后而不是并进那三条，是因为它会**真的让脚本写东西**：与其他探测
    # 并行会让"第二次执行遇到的状态"里混进别的探测留下的副作用（即使各自工作区独立，
    # 共享的容器资源与并发压力也会影响超时判定的可复现性）。
    for key in PARALLEL_PROBE_NODES:
        builder.add_edge(NODE_NAMES[key], NODE_NAMES["idempotency_and_safety_guards"])
    builder.add_edge(
        NODE_NAMES["idempotency_and_safety_guards"], NODE_NAMES["finalize_dimension_report"]
    )
    return pipeline


def build_script_usability_subgraph(
    deps: ScriptUsabilityDeps | None = None,
) -> ScriptUsabilityGraph:
    """独立的模块四子图（未 compile）。

    不在这里 `compile()`：checkpointer 属于主图的编译期决策（docs/dev/24），子图
    自己 compile 一次会得到一张没有 checkpointer 的图。本维度虽然不挂起，但断点
    恢复仍然依赖 checkpointer——主图跑到一半崩了，已经跑完的探测结果不该因为它自己
    那张子图没有 checkpointer 而丢失（重跑一遍是几十个容器的代价）。
    调用方按需 `build_script_usability_subgraph().compile(checkpointer=...)`。
    """
    builder: ScriptUsabilityGraph = StateGraph(ScriptUsabilityState)
    add_script_usability_nodes(builder, deps)
    builder.set_entry_point(ENTRY_NODE)
    builder.set_finish_point(TERMINAL_NODE)
    return builder


__all__ = [
    "INTERRUPT_BEFORE_NODES",
    "PARALLEL_PROBE_NODES",
    "add_script_usability_nodes",
    "build_script_usability_subgraph",
]
