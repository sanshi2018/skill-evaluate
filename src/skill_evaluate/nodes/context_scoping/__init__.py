"""模块二：上下文利用率与范围界定静态评测。

实现文档：docs/dev/12_模块二_上下文利用率与范围界定静态评测.md。

- `state.py`：本维度的私有状态命名空间与维度名常量。
- `static_scan.py`：两个**纯代码**扫描器（行数/Token 卡线、渐进式披露初筛），
  无 LLM、无 IO，可被 CI 当 linter 单独调用。
- `deps.py`：依赖注入容器（Judge、报告器、Skill 仓储、Token 计数器）。
- `nodes.py`：四个节点的实现与判定口径。
- `graph.py`：子图装配，以及给 docs/dev/24 主图用的平铺装配入口。

**与模块一的三点不同**（本维度是第一个"轻量维度"的样板）：

1. 全程走 `MINI` 后端，不碰 Hermes 沙箱——它评审的是 SKILL.md 的静态文本，
   不执行任何任务；
2. 没有测试集、没有执行、没有优化闭环，四个节点一条直线；
3. **阻断策略按检查项区分**：硬性数字超标阻断，Mini Agent 的主观审查只告警。
   这是 docs/dev/12 第 6 节对架构文档"抛出 Warning 或 Error"的工程化收窄。

后续模块的接入点见 docs/dev/interfaces/12_context_scoping_static_pipeline.md。
"""

from skill_evaluate.nodes.context_scoping.deps import (
    PEER_REVIEW_CRITICALITY,
    PEER_REVIEW_TEMPLATE_KEYS,
    ContextScopingDeps,
)
from skill_evaluate.nodes.context_scoping.graph import (
    INTERRUPT_BEFORE_NODES,
    add_context_scoping_nodes,
    build_context_scoping_subgraph,
)
from skill_evaluate.nodes.context_scoping.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    TERMINAL_NODE,
    ContextScopingPipeline,
    PeerReviewOutcome,
)
from skill_evaluate.nodes.context_scoping.state import DIMENSION, ContextScopingState
from skill_evaluate.nodes.context_scoping.static_scan import (
    MissingTriggerCandidate,
    ProgressiveDisclosureScan,
    StaticMetricsResult,
    format_reference_files_for_review,
    scan_progressive_disclosure,
    scan_static_metrics,
)

__all__ = [
    "DIMENSION",
    "ENTRY_NODE",
    "INTERRUPT_BEFORE_NODES",
    "NODE_NAMES",
    "PEER_REVIEW_CRITICALITY",
    "PEER_REVIEW_TEMPLATE_KEYS",
    "TERMINAL_NODE",
    "ContextScopingDeps",
    "ContextScopingPipeline",
    "ContextScopingState",
    "MissingTriggerCandidate",
    "PeerReviewOutcome",
    "ProgressiveDisclosureScan",
    "StaticMetricsResult",
    "add_context_scoping_nodes",
    "build_context_scoping_subgraph",
    "format_reference_files_for_review",
    "scan_progressive_disclosure",
    "scan_static_metrics",
]
