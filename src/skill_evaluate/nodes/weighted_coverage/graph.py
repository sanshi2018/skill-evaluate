"""模块八子图装配（docs/dev/18 第 2 节的节点序列）。

两种用法，同一份边定义（与模块一~七对称）：

- `add_weighted_coverage_nodes(builder, deps)`——把七个节点**平铺**进主图，供
  docs/dev/24 使用；只加维度内部的边，与主图其余部分的连线由主图决定。
- `build_weighted_coverage_subgraph(deps)`——单独编出一张只含本维度的图，供本地
  调试与集成测试。

## 顺序约束：必须排在模块六、模块七之后

- **模块六**：本维度全部节点读它建好的 `CapabilityTree`（分级是在已有节点上打
  标签，不是重新抽树）。`_load_tree()` 拿不到 `capability_tree_id` 时抛
  `PersistenceError` 并在错误信息里点名这条约束。
- **模块七**：`upgrade_combinatorial_priority` 读 `combinatorial_pairs_covered`，
  那是模块七的产出。顺序反了不会报错——它会读到一个空列表，然后报告"全部组合都
  未覆盖"，一个看起来很刺眼但完全错误的结论。

装配主图时因此必须是：

```python
cov = add_coverage_nodes(builder, deps)                                  # docs/dev/16
prune = add_pruning_nodes(builder, PruningDeps.from_coverage(cov.deps))  # docs/dev/17
wcov = add_weighted_coverage_nodes(                                      # docs/dev/18
    builder, WeightedCoverageDeps.from_coverage(prune.deps)
)
builder.add_edge(coverage.TERMINAL_NODE, pruning.ENTRY_NODE)             # 16 → 17
builder.add_edge(pruning.TERMINAL_NODE, weighted_coverage.ENTRY_NODE)    # 17 → 18
builder.add_edge(weighted_coverage.TERMINAL_NODE, "<下一个维度>")
```

## 与模块六的不同：本子图**无环**

模块六是全项目唯一带回边的子图（补盲 → 重新映射 → 直到覆盖率达标）。模块八与
模块七一样刻意不学它：约束补题之后直接往下走，补出来的反事实用例在**下一轮**
评测里被计入（它们自带 `negative_constraint_ids` 绑定，认它们不花判定调用）。
回环在这里的代价尤其大——每转一圈都是"约束数 × 用例数"次裁判调用。

也因此本维度**不需要** `interrupt_before`：它没有任何挂起点。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph

from skill_evaluate.nodes.weighted_coverage.deps import WeightedCoverageDeps
from skill_evaluate.nodes.weighted_coverage.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    TERMINAL_NODE,
    WeightedCoveragePipeline,
)
from skill_evaluate.nodes.weighted_coverage.state import WeightedCoverageState

# 本维度没有任何挂起点，显式导出一个空列表而不是干脆不定义：docs/dev/24 汇总
# `interrupt_before` 时是逐个维度取这个常量的，缺一个会让"这份文档忘了写"与
# "这份文档确实没有挂起点"分不开（与模块七同一处理）。
INTERRUPT_BEFORE_NODES: list[str] = []

# 状态类型别名，省得每个签名里重复写四个泛型参数——LangGraph 1.x 的 `StateGraph`
# 是 `Generic[StateT, ContextT, InputT, OutputT]`。
type WeightedCoverageGraph = StateGraph[
    WeightedCoverageState, Any, WeightedCoverageState, WeightedCoverageState
]


def add_weighted_coverage_nodes(
    builder: WeightedCoverageGraph, deps: WeightedCoverageDeps | None = None
) -> WeightedCoveragePipeline:
    """把模块八的节点与内部边加入给定的 `StateGraph`。

    只加**维度内部**的边；与主图其余部分的连接（谁指向 `ENTRY_NODE`、
    `TERMINAL_NODE` 指向谁）由 docs/dev/24 决定，本函数不越界替主图连线——这也是
    `docs/dev/interfaces/16` 第 4 节对 17/18 的明确要求（"不要改 graph.py 里的边"）。

    `deps` 建议传 `WeightedCoverageDeps.from_coverage(pipeline.deps)`，与模块六/七
    共用同一批 Agent/仓储实例，理由见 `deps.py` 模块头。
    """
    pipeline = WeightedCoveragePipeline(deps)

    builder.add_node(
        NODE_NAMES["extract_tier_and_negative_constraints"],
        pipeline.extract_tier_and_negative_constraints,
    )
    builder.add_node(
        NODE_NAMES["map_negative_constraint_coverage"],
        pipeline.map_negative_constraint_coverage,
    )
    builder.add_node(
        NODE_NAMES["constraint_feedback_generation"], pipeline.constraint_feedback_generation
    )
    builder.add_node(
        NODE_NAMES["recompute_weighted_coverage"], pipeline.recompute_weighted_coverage
    )
    builder.add_node(
        NODE_NAMES["upgrade_combinatorial_priority"], pipeline.upgrade_combinatorial_priority
    )
    builder.add_node(
        NODE_NAMES["generate_traceability_artifact"], pipeline.generate_traceability_artifact
    )
    builder.add_node(
        NODE_NAMES["finalize_weighted_coverage_report"],
        pipeline.finalize_weighted_coverage_report,
    )

    builder.add_edge(
        NODE_NAMES["extract_tier_and_negative_constraints"],
        NODE_NAMES["map_negative_constraint_coverage"],
    )
    # 唯一的分支：有明确未覆盖的负向约束 → 补反事实用例；否则直接重算覆盖率。
    # 显式映射表而不是让 LangGraph 按返回值猜节点：写出映射能让"这个分支只可能去
    # 这两个地方"在图定义里一眼可见（与模块六/七同一写法）。
    builder.add_conditional_edges(
        NODE_NAMES["map_negative_constraint_coverage"],
        pipeline.route_after_constraint_mapping,
        {
            NODE_NAMES["constraint_feedback_generation"]: NODE_NAMES[
                "constraint_feedback_generation"
            ],
            NODE_NAMES["recompute_weighted_coverage"]: NODE_NAMES["recompute_weighted_coverage"],
        },
    )
    # 补题之后**不回**映射节点：两条分支在重算节点汇合，图上没有环。
    builder.add_edge(
        NODE_NAMES["constraint_feedback_generation"], NODE_NAMES["recompute_weighted_coverage"]
    )
    builder.add_edge(
        NODE_NAMES["recompute_weighted_coverage"], NODE_NAMES["upgrade_combinatorial_priority"]
    )
    builder.add_edge(
        NODE_NAMES["upgrade_combinatorial_priority"],
        NODE_NAMES["generate_traceability_artifact"],
    )
    builder.add_edge(
        NODE_NAMES["generate_traceability_artifact"],
        NODE_NAMES["finalize_weighted_coverage_report"],
    )
    return pipeline


def build_weighted_coverage_subgraph(
    deps: WeightedCoverageDeps | None = None,
) -> WeightedCoverageGraph:
    """独立的模块八子图（未 compile）。

    不在这里 `compile()`：checkpointer 属于主图的编译期决策（docs/dev/24）。
    调用方按需 `build_weighted_coverage_subgraph().compile(checkpointer=...)`。

    单独跑时状态里必须已经有 `active_suite_version_id` 与 `capability_tree_id`
    ——本维度不建能力树，它给模块六建好的那棵补上权重与约束。
    """
    builder: WeightedCoverageGraph = StateGraph(WeightedCoverageState)
    add_weighted_coverage_nodes(builder, deps)
    builder.set_entry_point(ENTRY_NODE)
    builder.set_finish_point(TERMINAL_NODE)
    return builder


__all__ = [
    "INTERRUPT_BEFORE_NODES",
    "add_weighted_coverage_nodes",
    "build_weighted_coverage_subgraph",
]
