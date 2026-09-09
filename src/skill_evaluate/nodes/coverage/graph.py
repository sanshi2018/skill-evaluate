"""模块六子图装配（docs/dev/16 第 3 节的节点序列）。

两种用法，同一份边定义（与模块一~五对称）：

- `add_coverage_nodes(builder, deps)`——把五个节点**平铺**进主图，供 docs/dev/24
  使用；只加维度内部的边，与主图其余部分的连线由主图决定。
- `build_coverage_subgraph(deps)`——单独编出一张只含本维度的图，供本地调试与集成
  测试，不必拉起其余九个维度。

## 本维度是全项目唯一一张**带环**的子图

`feedback_driven_generation → map_case_coverage` 是一条真回边。前五个维度都刻意
避开了回边（模块五的注释写得最直白："回边会让这张图出现环"），本维度反过来必须
有它——架构文档模块六第 3 节要求"直到能力覆盖率达到预设的阈值"，这个"直到"只能
用环表达。

环的出口有**两道**，缺一不可：

1. `route_after_blind_spots`：没有盲区、或补盲次数已达上限 → 直接收尾；
2. `route_after_feedback`：补盲被判定耗尽或失败 → 直接收尾，**不回**映射节点。

只留第 1 道是不够的：`feedback_driven_generation` 自己也会在防御性检查里把状态标
成耗尽（比如 `incremental_patch()` 抛了 `GenerationError`），此时若无条件回边，
就会走回映射 → 盲区依旧 → 再来一轮，而迭代计数因为补盲失败并没有增加——那正是
架构文档警告的"无限重试死锁"。

## 与主图的关系：`coverage` 分区还会长大

docs/dev/16 第 3 节约定模块六/七/八的节点在 docs/dev/24 装配主图时被组织为同一个
`coverage` 分区，本文档只负责其中"能力提取与盲区补全"这一段。文档 17/18 接入时：

- 它们的节点应挂在 `TERMINAL_NODE`（`coverage.finalize_dimension_report`）**之后**
  ——能力树必须先建好、覆盖标记必须先算完，冗余折叠与加权分级才有输入；
- 它们**不要**改本文件的边，各自提供自己的 `add_*_nodes()`，由主图串起来。
  接入细节见 `docs/dev/interfaces/16_capability_coverage.md` 第 4 节。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph

from skill_evaluate.nodes.coverage.deps import CoverageDeps
from skill_evaluate.nodes.coverage.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    TERMINAL_NODE,
    CoveragePipeline,
)
from skill_evaluate.nodes.coverage.state import CoverageState

# docs/dev/24 第 3 节的编译期 `interrupt_before` 列表要汇总本维度这一项。
#
# `extract_capability_tree` 走的是**动态** `interrupt()`（只有能力树规模超阈值才
# 挂起），不加进静态列表也能正常挂起；列出来是为了让"这个节点可能停在人工审核卡片
# 上"在编译期就是一件显式的事（与模块一/三/五对 `OptimizationLoop` 的处理一致）。
INTERRUPT_BEFORE_NODES = [NODE_NAMES["extract_capability_tree"]]

# 状态类型别名，省得每个签名里重复写四个泛型参数——LangGraph 1.x 的 `StateGraph`
# 是 `Generic[StateT, ContextT, InputT, OutputT]`。
type CoverageGraph = StateGraph[CoverageState, Any, CoverageState, CoverageState]


def add_coverage_nodes(
    builder: CoverageGraph, deps: CoverageDeps | None = None
) -> CoveragePipeline:
    """把模块六的节点与内部边加入给定的 `StateGraph`。

    只加**维度内部**的边；与主图其余部分的连接（谁指向 `ENTRY_NODE`、`TERMINAL_NODE`
    指向谁）由 docs/dev/24 决定，本函数不越界替主图连线。

    返回 `CoveragePipeline` 实例，便于调用方复用同一份依赖——文档 17/18 的节点要读
    同一棵能力树、用同一个 `AnalyzerAgent`，共用一个 `CoverageDeps` 比各自实例化
    更省（也避免两个 Agent 实例各自持有不同的 `trace_handle`）。
    """
    pipeline = CoveragePipeline(deps)

    builder.add_node(NODE_NAMES["extract_capability_tree"], pipeline.extract_capability_tree)
    builder.add_node(NODE_NAMES["map_case_coverage"], pipeline.map_case_coverage)
    builder.add_node(NODE_NAMES["blind_spot_detection"], pipeline.blind_spot_detection)
    builder.add_node(NODE_NAMES["feedback_driven_generation"], pipeline.feedback_driven_generation)
    builder.add_node(NODE_NAMES["finalize_dimension_report"], pipeline.finalize_dimension_report)

    builder.add_edge(NODE_NAMES["extract_capability_tree"], NODE_NAMES["map_case_coverage"])
    builder.add_edge(NODE_NAMES["map_case_coverage"], NODE_NAMES["blind_spot_detection"])

    # 出口一：有盲区且还有补盲配额 → 定向出题；否则收尾。
    # 显式映射表而不是让 LangGraph 按返回值猜节点：路由函数返回的字符串就是节点名，
    # 写出映射能让"这个分支只可能去这两个地方"在图定义里一眼可见。
    builder.add_conditional_edges(
        NODE_NAMES["blind_spot_detection"],
        pipeline.route_after_blind_spots,
        {
            NODE_NAMES["feedback_driven_generation"]: NODE_NAMES["feedback_driven_generation"],
            NODE_NAMES["finalize_dimension_report"]: NODE_NAMES["finalize_dimension_report"],
        },
    )
    # 出口二：补盲成功 → 回边重新映射；补盲耗尽/失败 → 收尾。见模块头"两道出口"。
    builder.add_conditional_edges(
        NODE_NAMES["feedback_driven_generation"],
        CoveragePipeline.route_after_feedback,
        {
            NODE_NAMES["map_case_coverage"]: NODE_NAMES["map_case_coverage"],
            NODE_NAMES["finalize_dimension_report"]: NODE_NAMES["finalize_dimension_report"],
        },
    )
    return pipeline


def build_coverage_subgraph(deps: CoverageDeps | None = None) -> CoverageGraph:
    """独立的模块六子图（未 compile）。

    不在这里 `compile()`：checkpointer 与 `interrupt_before` 属于主图的编译期决策
    （docs/dev/24），子图自己 compile 一次会得到一张没有 checkpointer 的图，而本
    维度的人工审核卡片（动态 `interrupt()`）**必须**有 checkpointer 才能挂起恢复。
    调用方按需 `build_coverage_subgraph().compile(checkpointer=...)`。

    ⚠️ 编译带环的图时注意 `recursion_limit`：本维度最坏情况是
    `1 + (max_patch_iterations + 1) × 3 + 1` 个超步（默认配置下约 14），远低于
    LangGraph 默认的 25，但主图把十个维度串起来后总步数会累加，docs/dev/24 需要
    据此设置一个足够的 `recursion_limit`。
    """
    builder: CoverageGraph = StateGraph(CoverageState)
    add_coverage_nodes(builder, deps)
    builder.set_entry_point(ENTRY_NODE)
    builder.set_finish_point(TERMINAL_NODE)
    return builder


__all__ = [
    "INTERRUPT_BEFORE_NODES",
    "add_coverage_nodes",
    "build_coverage_subgraph",
]
