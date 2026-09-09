"""模块五子图装配（docs/dev/15 第 3 节的节点序列）。

两种用法，同一份边定义（与模块一~四对称）：

- `add_security_nodes(builder, deps)`——把九个节点**平铺**进主图，供 docs/dev/24
  使用；只加维度内部的边，与主图其余部分的连线由主图决定。
- `build_security_subgraph(deps)`——单独编出一张只含本维度的图，供本地调试与集成
  测试，不必拉起其余九个维度。

## 五条并行支路

`prepare_adversarial_suite` 之后分出五条互不依赖的探测支路，LangGraph 会把同一超步
里的它们并发调度，全部完成后才进 `security_posture_scoring`——这正是定级需要的前提
（严重性裁定必须看到全部发现才能开始，否则先跑完的那批会被单独定级两次）。

与 docs/dev/13 一样用**静态并行边**而不是 `Send`：`Send` 解决的是"运行时才知道要
派生多少个同构分支"（map-reduce），而这里是五段**结构不同的固定逻辑**，节点数编译期
就确定。静态边的好处是图结构在 `get_graph().draw()` 里看得见，`interrupt_before`
也能按节点名精确挂载。

（docs/dev/15 正文的流程图把 `artifact_sast_review` 画在四条支路之后；这里把它做成
第五条并行支路，理由见 `nodes.py` 的模块文档——它不消费其余支路的任何产物，串在
后面纯粹是被当成汇合点用了，而真正的汇合点是定级节点。）
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph

from skill_evaluate.nodes.security.deps import SecurityDeps
from skill_evaluate.nodes.security.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    PARALLEL_PROBE_NODES,
    TERMINAL_NODE,
    SecurityPipeline,
)
from skill_evaluate.nodes.security.state import SecurityState

# docs/dev/24 第 3 节的编译期 `interrupt_before` 列表要汇总本维度这一项。
# 与模块一/三同样的说明：`OptimizationLoop` 走的是**动态** `interrupt()`（超出重试
# 次数才挂起），不加进静态列表也能正常挂起；列出来是为了让"这个节点可能停在人工
# 审批上"在编译期就是一件显式的事。
#
# 本维度还有第二处挂起点：安全判定的三副本共识未达成时，`_unwrap()` 抛
# `PipelineSuspended`。那个可能发生在任何一个走 `judgmental_verdict()` 的节点上
# （注入探测、定级），**不适合**进静态列表——把整条支路都标成"可能停在审批上"，
# 这个列表就失去了指示意义。
INTERRUPT_BEFORE_NODES = [NODE_NAMES["appsec_optimizer_loop"]]

# 状态类型别名，省得每个签名里重复写四个泛型参数——LangGraph 1.x 的 `StateGraph`
# 是 `Generic[StateT, ContextT, InputT, OutputT]`。
type SecurityGraph = StateGraph[SecurityState, Any, SecurityState, SecurityState]


def add_security_nodes(
    builder: SecurityGraph, deps: SecurityDeps | None = None
) -> SecurityPipeline:
    """把模块五的节点与内部边加入给定的 `StateGraph`。

    只加**维度内部**的边；与主图其余部分的连接（谁指向 `ENTRY_NODE`、`TERMINAL_NODE`
    指向谁）由 docs/dev/24 决定，本函数不越界替主图连线。

    返回 `SecurityPipeline` 实例，便于调用方复用同一份依赖。
    """
    pipeline = SecurityPipeline(deps)

    builder.add_node(NODE_NAMES["prepare_adversarial_suite"], pipeline.prepare_adversarial_suite)
    builder.add_node(
        NODE_NAMES["direct_prompt_injection_probe"], pipeline.direct_prompt_injection_probe
    )
    builder.add_node(NODE_NAMES["data_poisoning_probe"], pipeline.data_poisoning_probe)
    builder.add_node(NODE_NAMES["env_and_traversal_probe"], pipeline.env_and_traversal_probe)
    builder.add_node(
        NODE_NAMES["dos_context_exhaustion_probe"], pipeline.dos_context_exhaustion_probe
    )
    builder.add_node(NODE_NAMES["artifact_sast_review"], pipeline.artifact_sast_review)
    builder.add_node(NODE_NAMES["security_posture_scoring"], pipeline.security_posture_scoring)
    builder.add_node(NODE_NAMES["appsec_optimizer_loop"], pipeline.appsec_optimizer_loop)
    builder.add_node(NODE_NAMES["finalize_dimension_report"], pipeline.finalize_dimension_report)

    # 分叉：五条支路互不依赖，同一超步内并发调度。
    for probe_node in PARALLEL_PROBE_NODES:
        builder.add_edge(NODE_NAMES["prepare_adversarial_suite"], probe_node)
    # 汇合：五条支路全部完成才进定级（LangGraph 的超步语义保证这一点）。
    for probe_node in PARALLEL_PROBE_NODES:
        builder.add_edge(probe_node, NODE_NAMES["security_posture_scoring"])

    builder.add_conditional_edges(
        NODE_NAMES["security_posture_scoring"],
        SecurityPipeline.route_after_scoring,
        # 显式映射表而不是让 LangGraph 按返回值猜节点：路由函数返回的字符串就是节点
        # 名，写出映射能让"这个分支只可能去这两个地方"在图定义里一眼可见。
        {
            NODE_NAMES["appsec_optimizer_loop"]: NODE_NAMES["appsec_optimizer_loop"],
            NODE_NAMES["finalize_dimension_report"]: NODE_NAMES["finalize_dimension_report"],
        },
    )
    # 闭环收敛（或人工采纳补丁）后直接收尾：安全重测与强制功能回归都发生在
    # `appsec_optimizer_loop` 内部的 `retest_fn` 里，不在图上连一条回边。
    # 连回边意味着五条探测支路全部重跑一遍，而闭环最多跑 3 轮——本维度是全项目最贵
    # 的一个，每轮全量重跑的成本没有人会接受，而且回边会让这张图出现环。
    builder.add_edge(
        NODE_NAMES["appsec_optimizer_loop"], NODE_NAMES["finalize_dimension_report"]
    )
    return pipeline


def build_security_subgraph(deps: SecurityDeps | None = None) -> SecurityGraph:
    """独立的模块五子图（未 compile）。

    不在这里 `compile()`：checkpointer 与 `interrupt_before` 属于主图的编译期决策
    （docs/dev/24），子图自己 compile 一次会得到一张没有 checkpointer 的图，而
    `HermesBackend.execute()` 与优化闭环的挂起都依赖 checkpointer。
    调用方按需 `build_security_subgraph().compile(checkpointer=...)`。
    """
    builder: SecurityGraph = StateGraph(SecurityState)
    add_security_nodes(builder, deps)
    builder.set_entry_point(ENTRY_NODE)
    builder.set_finish_point(TERMINAL_NODE)
    return builder


__all__ = [
    "INTERRUPT_BEFORE_NODES",
    "add_security_nodes",
    "build_security_subgraph",
]
