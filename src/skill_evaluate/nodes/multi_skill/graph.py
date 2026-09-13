"""模块十子图装配（docs/dev/20 第 3 节的节点序列）。

两种用法，同一份边定义（与其余维度的 `graph.py` 对称）：

- `add_multi_skill_nodes(builder, deps)`——把九个节点**平铺**进主图，供 docs/dev/24 使用；
- `build_multi_skill_subgraph(deps)`——单独编出一张只含本维度的图，供本地调试与集成测试。

## 并行扇出 + 汇合

三条动态探测互不依赖，从 `namespace_pollution_static_scan` 扇出并行执行，再用
`add_edge([三条支路], role_collision_and_temporal_static_scan)` 汇合——LangGraph 对
"多源一目标"的边会等所有源节点完成才触发目标节点。汇合点必须等齐：时序扰动要读
指令拮抗产出的 `healthy_case_ids`。三条支路各写自己的私有结果键，`executed_trace_ids` /
`judge_verdict_ids` 是 add reducer，并行追加安全。

基石回归熔断排在最后一个执行节点而不是并入扇出：它跑的是**核心 Skill 自己的**用例，
沙箱消耗与前面三条支路叠加会让峰值并发翻倍；串在后面，共享信号量的排队更可预期。

`INTERRUPT_BEFORE_NODES` 为空：本维度不产生补丁、不进闭环。基石熔断的"阻断"通过
`dimension_results.blocking=True` 表达；docs/dev/22 追加的终点节点
`deep_conflict_approval_gate` 依据告警 payload 的 `blocking` 走**动态** `interrupt()`
（只在基石熔断时挂起），因此同样不进静态列表——静态中断会让每次运行都停在闸门前。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph

from skill_evaluate.nodes.multi_skill.deps import MultiSkillDeps
from skill_evaluate.nodes.multi_skill.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    PROBE_NODES,
    TERMINAL_NODE,
    MultiSkillPipeline,
)
from skill_evaluate.nodes.multi_skill.state import MultiSkillState

INTERRUPT_BEFORE_NODES: list[str] = []

type MultiSkillGraph = StateGraph[MultiSkillState, Any, MultiSkillState, MultiSkillState]


def add_multi_skill_nodes(
    builder: MultiSkillGraph, deps: MultiSkillDeps | None = None
) -> MultiSkillPipeline:
    """把模块十的节点与内部边加入给定的 `StateGraph`（只加维度内部的边）。"""
    pipeline = MultiSkillPipeline(deps)

    builder.add_node(ENTRY_NODE, pipeline.prepare_multi_skill_context)
    builder.add_node(
        NODE_NAMES["namespace_pollution_static_scan"], pipeline.namespace_pollution_static_scan
    )
    builder.add_node(
        NODE_NAMES["cross_trigger_interference_probe"], pipeline.cross_trigger_interference_probe
    )
    builder.add_node(
        NODE_NAMES["instruction_antagonism_and_semantic_flow_probe"],
        pipeline.instruction_antagonism_and_semantic_flow_probe,
    )
    builder.add_node(
        NODE_NAMES["context_exhaustion_attention_decay_probe"],
        pipeline.context_exhaustion_attention_decay_probe,
    )
    builder.add_node(
        NODE_NAMES["role_collision_and_temporal_static_scan"],
        pipeline.role_collision_and_temporal_static_scan,
    )
    builder.add_node(NODE_NAMES["core_skill_regression_gate"], pipeline.core_skill_regression_gate)
    builder.add_node(NODE_NAMES["finalize_dimension_report"], pipeline.finalize_dimension_report)
    builder.add_node(TERMINAL_NODE, pipeline.deep_conflict_approval_gate)

    builder.add_edge(ENTRY_NODE, NODE_NAMES["namespace_pollution_static_scan"])



    for probe_node in PROBE_NODES:
        builder.add_edge(NODE_NAMES["namespace_pollution_static_scan"], probe_node)
    builder.add_edge(list(PROBE_NODES), NODE_NAMES["role_collision_and_temporal_static_scan"])
    builder.add_edge(
        NODE_NAMES["role_collision_and_temporal_static_scan"],
        NODE_NAMES["core_skill_regression_gate"],
    )
    builder.add_edge(
        NODE_NAMES["core_skill_regression_gate"], NODE_NAMES["finalize_dimension_report"]
    )
    # docs/dev/22：收尾之后过人工介入闸门（阻塞挂起或仅通知，见节点文档）。
    builder.add_edge(NODE_NAMES["finalize_dimension_report"], TERMINAL_NODE)
    return pipeline


def build_multi_skill_subgraph(deps: MultiSkillDeps | None = None) -> MultiSkillGraph:
    """独立的模块十子图（未 compile；checkpointer 属于主图的编译期决策，理由同其余维度）。

    ⚠️ 默认后端是 Hermes（callback 挂起），要求 `compile(checkpointer=...)`。
    """
    builder: MultiSkillGraph = StateGraph(MultiSkillState)
    add_multi_skill_nodes(builder, deps)
    builder.set_entry_point(ENTRY_NODE)
    builder.set_finish_point(TERMINAL_NODE)
    return builder


__all__ = ["INTERRUPT_BEFORE_NODES", "add_multi_skill_nodes", "build_multi_skill_subgraph"]
