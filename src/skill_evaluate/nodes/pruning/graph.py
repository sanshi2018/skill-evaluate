"""模块七子图装配（docs/dev/17 第 3 节的节点序列）。

两种用法，同一份边定义（与模块一~六对称）：

- `add_pruning_nodes(builder, deps)`——把五个节点**平铺**进主图，供 docs/dev/24
  使用；只加维度内部的边，与主图其余部分的连线由主图决定。
- `build_pruning_subgraph(deps)`——单独编出一张只含本维度的图，供本地调试与集成
  测试。

## 顺序约束：必须排在模块六之后

`docs/dev/interfaces/16` 第 4 节的约定，本文档兑现它：本维度的三个分析节点全部
读取模块六的产出（`CapabilityTree` 与 `TestCase.target_capability_ids`），能力树
没建好、覆盖映射没算完时，"没有冗余、没有组合缺口、没有孤儿"这三个结论都是假的
——而且是**看起来很健康**的假结论。

装配主图时因此必须是：

```python
cov = add_coverage_nodes(builder, deps)                       # docs/dev/16
prune = add_pruning_nodes(builder, PruningDeps.from_coverage(cov.deps))
builder.add_edge(coverage.TERMINAL_NODE, pruning.ENTRY_NODE)  # 16 → 17
builder.add_edge(pruning.TERMINAL_NODE, "<下一个维度>")
```

`_load_tree()` 在拿不到 `capability_tree_id` 时抛 `PersistenceError` 并在错误信息里
点名这条顺序约束，接错了会立刻失败而不是给出一份 0 冗余 0 缺口的报告。

## 与模块六的另一处不同：本子图**无环**

模块六是全项目唯一带回边的子图（补盲 → 重新映射 → 直到覆盖率达标）。模块七刻意
不学：组合缺口首次接入时几乎是全量未覆盖，回环会把
`max_combinatorial_patch_per_round` 想要的"逐步收敛"变成"一次跑到底"。因此
`combinatorial_feedback_generation` 的唯一去处是 `orphan_case_detection`，
两条分支在那里汇合，图上不存在任何一条指回上游的边。

也因此本维度**不需要** `interrupt_before`：它没有任何挂起点。孤儿用例走的是非阻塞
建议队列（`test_case_suggestions` 表），不是 `suspend_and_wait()`——两种人机协作
模式的分界见 `state/suggestion.py` 的模块头。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph

from skill_evaluate.nodes.pruning.deps import PruningDeps
from skill_evaluate.nodes.pruning.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    TERMINAL_NODE,
    PruningPipeline,
)
from skill_evaluate.nodes.pruning.state import PruningState

# 本维度没有任何挂起点，显式导出一个空列表而不是干脆不定义：docs/dev/24 汇总
# `interrupt_before` 时是逐个维度取这个常量的，缺一个会变成"这份文档忘了写"
# 与"这份文档确实没有挂起点"分不开。
INTERRUPT_BEFORE_NODES: list[str] = []

# 状态类型别名，省得每个签名里重复写四个泛型参数——LangGraph 1.x 的 `StateGraph`
# 是 `Generic[StateT, ContextT, InputT, OutputT]`。
type PruningGraph = StateGraph[PruningState, Any, PruningState, PruningState]


def add_pruning_nodes(builder: PruningGraph, deps: PruningDeps | None = None) -> PruningPipeline:
    """把模块七的节点与内部边加入给定的 `StateGraph`。

    只加**维度内部**的边；与主图其余部分的连接（谁指向 `ENTRY_NODE`、`TERMINAL_NODE`
    指向谁）由 docs/dev/24 决定，本函数不越界替主图连线——这也是
    `docs/dev/interfaces/16` 第 4 节对 17/18 的明确要求（"不要改 graph.py 里的边"）。

    `deps` 建议传 `PruningDeps.from_coverage(coverage_pipeline.deps)`，与模块六共用
    同一批 Agent/仓储实例，理由见 `deps.py` 模块头。
    """
    pipeline = PruningPipeline(deps)

    builder.add_node(NODE_NAMES["redundant_case_pruning"], pipeline.redundant_case_pruning)
    builder.add_node(
        NODE_NAMES["combinatorial_matrix_analysis"], pipeline.combinatorial_matrix_analysis
    )
    builder.add_node(
        NODE_NAMES["combinatorial_feedback_generation"],
        pipeline.combinatorial_feedback_generation,
    )
    builder.add_node(NODE_NAMES["orphan_case_detection"], pipeline.orphan_case_detection)
    builder.add_node(NODE_NAMES["finalize_pruning_report"], pipeline.finalize_pruning_report)

    builder.add_edge(
        NODE_NAMES["redundant_case_pruning"], NODE_NAMES["combinatorial_matrix_analysis"]
    )
    # 唯一的分支：有未覆盖组合对且本轮补题预算 > 0 → 定向补题；否则直接去孤儿检测。
    # 显式映射表而不是让 LangGraph 按返回值猜节点：写出映射能让"这个分支只可能去这
    # 两个地方"在图定义里一眼可见（与模块六同一写法）。
    builder.add_conditional_edges(
        NODE_NAMES["combinatorial_matrix_analysis"],
        pipeline.route_after_matrix,
        {
            NODE_NAMES["combinatorial_feedback_generation"]: NODE_NAMES[
                "combinatorial_feedback_generation"
            ],
            NODE_NAMES["orphan_case_detection"]: NODE_NAMES["orphan_case_detection"],
        },
    )
    # 补题之后**不回**矩阵分析：两条分支在孤儿检测汇合，图上没有环。
    builder.add_edge(
        NODE_NAMES["combinatorial_feedback_generation"], NODE_NAMES["orphan_case_detection"]
    )
    builder.add_edge(NODE_NAMES["orphan_case_detection"], NODE_NAMES["finalize_pruning_report"])
    return pipeline


def build_pruning_subgraph(deps: PruningDeps | None = None) -> PruningGraph:
    """独立的模块七子图（未 compile）。

    不在这里 `compile()`：checkpointer 属于主图的编译期决策（docs/dev/24）。
    调用方按需 `build_pruning_subgraph().compile(checkpointer=...)`。

    单独跑时状态里必须已经有 `active_suite_version_id` 与 `capability_tree_id`
    ——本维度不建能力树，它消费模块六建好的那棵。
    """
    builder: PruningGraph = StateGraph(PruningState)
    add_pruning_nodes(builder, deps)
    builder.set_entry_point(ENTRY_NODE)
    builder.set_finish_point(TERMINAL_NODE)
    return builder


__all__ = [
    "INTERRUPT_BEFORE_NODES",
    "add_pruning_nodes",
    "build_pruning_subgraph",
]
