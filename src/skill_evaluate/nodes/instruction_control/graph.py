"""模块三子图装配（docs/dev/13 第 2 节的节点序列）。

两种用法，同一份边定义（与模块一/二对称）：

- `add_instruction_control_nodes(builder, deps)`——把八个节点**平铺**进主图，供
  docs/dev/24 使用；只加维度内部的边，与主图其余部分的连线由主图决定。
- `build_instruction_control_subgraph(deps)`——单独编出一张只含本维度的图，供本地
  调试与集成测试，不必拉起其余九个维度。

## 并行分叉是本维度的结构特征

`prepare_cases` 之后分出三条互不依赖的支路（A/B 对比、控制标定、渐进式披露探查），
LangGraph 会把同一超步里的它们并发调度。`trace_efficiency_diagnosis` 是唯一的例外
——它消费 A/B 产出的 Trace，因此挂在 A/B 之后串行。

docs/dev/13 正文提到用 `Send` API 并发触发。这里用的是**静态并行边**而不是 `Send`：
`Send` 解决的是"运行时才知道要派生多少个同构分支"（典型是 map-reduce），而这里的
三条支路是三段**结构不同的固定逻辑**，节点数编译期就确定。用静态边的好处是图结构
在 `get_graph().draw()` 里看得见，`interrupt_before` 也能按节点名精确挂载。

三条支路汇合到 `collect_findings`：LangGraph 的超步语义保证它等到三条支路全部完成
才会执行——这正是条件路由需要的前提（要不要进优化闭环，必须同时看到 A/B 与探查两
条支路的结论）。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph

from skill_evaluate.nodes.instruction_control.deps import InstructionControlDeps
from skill_evaluate.nodes.instruction_control.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    TERMINAL_NODE,
    InstructionControlPipeline,
)
from skill_evaluate.nodes.instruction_control.state import InstructionControlState

# docs/dev/24 第 3 节的编译期 `interrupt_before` 列表要汇总本维度这一项。
# 与模块一同样的说明：`OptimizationLoop` 走的是**动态** `interrupt()`（超出重试次数
# 才挂起），不加进静态列表也能正常挂起；列出来是为了让"这个节点可能停在人工审批上"
# 在编译期就是一件显式的事。
INTERRUPT_BEFORE_NODES = [NODE_NAMES["optimizer_loop"]]

# 状态类型别名，省得每个签名里重复写四个泛型参数——LangGraph 1.x 的 `StateGraph`
# 是 `Generic[StateT, ContextT, InputT, OutputT]`。
type InstructionControlGraph = StateGraph[
    InstructionControlState, Any, InstructionControlState, InstructionControlState
]


def add_instruction_control_nodes(
    builder: InstructionControlGraph, deps: InstructionControlDeps | None = None
) -> InstructionControlPipeline:
    """把模块三的节点与内部边加入给定的 `StateGraph`。

    只加**维度内部**的边；与主图其余部分的连接（谁指向 `ENTRY_NODE`、`TERMINAL_NODE`
    指向谁）由 docs/dev/24 决定，本函数不越界替主图连线。

    返回 `InstructionControlPipeline` 实例，便于调用方复用同一份依赖。
    """
    pipeline = InstructionControlPipeline(deps)

    builder.add_node(NODE_NAMES["prepare_cases"], pipeline.prepare_cases)
    builder.add_node(NODE_NAMES["ab_comparative_execution"], pipeline.ab_comparative_execution)
    builder.add_node(NODE_NAMES["trace_efficiency_diagnosis"], pipeline.trace_efficiency_diagnosis)
    builder.add_node(
        NODE_NAMES["control_calibration_static_scan"], pipeline.control_calibration_static_scan
    )
    builder.add_node(
        NODE_NAMES["progressive_disclosure_dynamic_probe"],
        pipeline.progressive_disclosure_dynamic_probe,
    )
    builder.add_node(NODE_NAMES["collect_findings"], pipeline.collect_findings)
    builder.add_node(NODE_NAMES["optimizer_loop"], pipeline.optimizer_loop)
    builder.add_node(NODE_NAMES["finalize_dimension_report"], pipeline.finalize_dimension_report)

    # 分叉：三条支路互不依赖，同一超步内并发调度。
    builder.add_edge(NODE_NAMES["prepare_cases"], NODE_NAMES["ab_comparative_execution"])
    builder.add_edge(NODE_NAMES["prepare_cases"], NODE_NAMES["control_calibration_static_scan"])
    builder.add_edge(
        NODE_NAMES["prepare_cases"], NODE_NAMES["progressive_disclosure_dynamic_probe"]
    )
    # 唯一的串行依赖：效率诊断读的是 A/B 加载侧刚落库的 Trace。
    builder.add_edge(
        NODE_NAMES["ab_comparative_execution"], NODE_NAMES["trace_efficiency_diagnosis"]
    )

    # 汇合：三条支路的末端都指向 collect_findings。
    #
    # ⚠️ docs/dev/24 装配主图时修正：必须用**多起点边**（同步屏障）。A/B 支路比另两条多一跳
    # （ab → trace_efficiency），逐条 `add_edge` 的话，标定与探查在第 N 超步完成时就触发一次
    # collect_findings，效率诊断在第 N+1 超步完成时再触发一次——汇总、路由与收尾节点都会跑两遍，
    # 第一遍收尾写进 `dimension_results` 的是缺了效率诊断的结论，主图的报告汇合点若恰好在两遍之间
    # 触发，报告里就是那份残缺结论。多起点边保证三条支路全部完成后只触发一次。
    builder.add_edge(
        [
            NODE_NAMES["trace_efficiency_diagnosis"],
            NODE_NAMES["control_calibration_static_scan"],
            NODE_NAMES["progressive_disclosure_dynamic_probe"],
        ],
        NODE_NAMES["collect_findings"],
    )

    builder.add_conditional_edges(
        NODE_NAMES["collect_findings"],
        InstructionControlPipeline.route_after_collect,
        # 显式映射表而不是让 LangGraph 按返回值猜节点：路由函数返回的字符串就是节点
        # 名，写出映射能让"这个分支只可能去这两个地方"在图定义里一眼可见。
        {
            NODE_NAMES["optimizer_loop"]: NODE_NAMES["optimizer_loop"],
            NODE_NAMES["finalize_dimension_report"]: NODE_NAMES["finalize_dimension_report"],
        },
    )
    # 闭环收敛（或人工采纳补丁）后直接收尾：重测发生在 `optimizer_loop` 内部的
    # `retest_fn` 里（docs/dev/13 第 8 节的"重跑相关子节点"），不在图上连一条回边。
    # 连回边意味着整条支路重跑一遍，而闭环最多跑 3 轮——每轮全量重跑 A/B 的成本
    # 没有人会接受，而且回边会让这张图出现环，`interrupt_before` 的语义也跟着变复杂。
    builder.add_edge(NODE_NAMES["optimizer_loop"], NODE_NAMES["finalize_dimension_report"])
    return pipeline


def build_instruction_control_subgraph(
    deps: InstructionControlDeps | None = None,
) -> InstructionControlGraph:
    """独立的模块三子图（未 compile）。

    不在这里 `compile()`：checkpointer 与 `interrupt_before` 属于主图的编译期决策
    （docs/dev/24），子图自己 compile 一次会得到一张没有 checkpointer 的图，而
    `HermesBackend.execute()` 与优化闭环的挂起都依赖 checkpointer。
    调用方按需 `build_instruction_control_subgraph().compile(checkpointer=...)`。
    """
    builder: InstructionControlGraph = StateGraph(InstructionControlState)
    add_instruction_control_nodes(builder, deps)
    builder.set_entry_point(ENTRY_NODE)
    builder.set_finish_point(TERMINAL_NODE)
    return builder


__all__ = [
    "INTERRUPT_BEFORE_NODES",
    "add_instruction_control_nodes",
    "build_instruction_control_subgraph",
]
