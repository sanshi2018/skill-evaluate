"""前置门禁子图装配（docs/dev/21 第 7 节）。

与各维度同样提供两种用法：

- `add_preflight_nodes(builder, deps)`：平铺进主图（docs/dev/24），只加两道门禁之间的边，
  `set_entry_point(ENTRY_NODE)` 与 `TERMINAL_NODE → 各维度入口` 的连线由主图决定；
- `build_preflight_subgraph(deps)`：单独编一张图，供部署后冒烟验证（不必拉起十个维度）。

**串行而不是并行**：金丝雀的跳过判定要用指纹摘要充当镜像标识；并且指纹不一致时继续跑金丝雀
没有意义——环境已经被证明不可信，再在上面跑任务只会多一份噪声。

`INTERRUPT_BEFORE_NODES` 为空：门禁失败走异常挂起（`InfrastructureEnvironmentError`），不是
"执行前等人批准"。仍导出该常量，便于 docs/dev/24 无差别汇总。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph

from skill_evaluate.nodes.preflight.deps import PreflightDeps
from skill_evaluate.nodes.preflight.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    TERMINAL_NODE,
    PreflightPipeline,
)
from skill_evaluate.nodes.preflight.state import PreflightState

INTERRUPT_BEFORE_NODES: list[str] = []

type PreflightGraph = StateGraph[PreflightState, Any, PreflightState, PreflightState]


def add_preflight_nodes(
    builder: PreflightGraph, deps: PreflightDeps | None = None
) -> PreflightPipeline:
    pipeline = PreflightPipeline(deps)
    builder.add_node(NODE_NAMES["sandbox_fingerprint_gate"], pipeline.sandbox_fingerprint_gate)
    builder.add_node(NODE_NAMES["canary_probe_gate"], pipeline.canary_probe_gate)
    builder.add_edge(NODE_NAMES["sandbox_fingerprint_gate"], NODE_NAMES["canary_probe_gate"])
    return pipeline


def build_preflight_subgraph(deps: PreflightDeps | None = None) -> PreflightGraph:
    """独立的前置门禁子图（未 compile；checkpointer 属于调用方的编译期决策）。

    金丝雀在 PLUGGABLE 后端上会挂起等 Hook，单独运行时同样必须
    `build_preflight_subgraph().compile(checkpointer=...)` 并注册 GraphResumer。
    """
    builder: PreflightGraph = StateGraph(PreflightState)
    add_preflight_nodes(builder, deps)
    builder.set_entry_point(ENTRY_NODE)
    builder.set_finish_point(TERMINAL_NODE)
    return builder


__all__ = ["INTERRUPT_BEFORE_NODES", "add_preflight_nodes", "build_preflight_subgraph"]
