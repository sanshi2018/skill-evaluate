"""模块八：多维加权覆盖率算法与隐式边界追踪。

实现文档：docs/dev/18_模块八_多维加权覆盖率算法与隐式边界追踪.md。
它是第 2 层"覆盖率三部曲"（16 → 17 → 18）的收官：三份文档围绕**同一棵能力树**
展开，16 负责提取与二元覆盖判定，17 负责测试集自身的健康度，18 负责加权与负向
约束的最终形态，并把 17 遗留的组合矩阵优先级策略正式升级。

- `state.py`：私有状态命名空间；`DIMENSION` 独有（`weighted_coverage`），
  `ROUTING_KEY`/`NODE_PREFIX` 从模块六原样沿用（三份文档共用一个图分区）。
- `deps.py`：依赖注入容器，继承 `CoverageDeps`（复用同一批 Agent/仓储实例）。
- `priority.py`：组合对优先级排序的**纯函数**，模块七与本模块共用。
- `artifact.py`：可追溯性矩阵 JSON/CSV 制品，schema 是对外承诺。
- `nodes.py`：七个节点的实现与判定口径。
- `graph.py`：子图装配，以及给 docs/dev/24 主图用的平铺装配入口。

**本模块补齐的三处占位**（docs/dev/16/17 明确标注的）：

1. **能力权重分级**：`CapabilityNode.tier` 此前恒为占位值 `P1_CONDITIONAL`，
   由 `AnalyzerAgent.classify_tiers()` 填成真实分级，加权覆盖率从此有意义。
2. **负向约束覆盖率**：`CapabilityTree.negative_constraints` 此前恒为空列表，
   由 `AnalyzerAgent.extract_negative_constraints()` 抽出 Gotchas 类禁令，再由
   反事实覆盖判定逐条追踪"有没有用例故意诱导智能体踩这个坑"。本模块是
   `CapabilityFocus.negative_constraint_ids`（docs/dev/06 定义）的首个真实调用方。
3. **组合矩阵的 P0 优先级筛选**：模块七的截断跑在分级之前只能按 id 排序，本模块
   在分级之后重排一次，第一次得到真实的"P0×P0 优先"缺口清单。

**它不注册任何新的量化规则**：加权覆盖率判定复用模块六已注册的
`capability_coverage_threshold`（`register_rule()` 遇重名直接抛错是有意的，理由见
`nodes/coverage/rules.py` 模块头）。它**新增**一个评审模板
`negative_constraint_probe`（`agents/mini/templates/weighted_coverage.py`），
因此是三份覆盖率文档里唯一会走 `judgmental_verdict()` 裁量路径的一个。

后续模块的接入点见 docs/dev/interfaces/18_weighted_coverage.md。
"""

from skill_evaluate.nodes.weighted_coverage.artifact import (
    ARTIFACT_BASENAME,
    CSV_COLUMNS,
    build_traceability_matrix,
    flatten_for_csv,
    write_matrix,
)
from skill_evaluate.nodes.weighted_coverage.deps import (
    TRIGGERED_BY_NEGATIVE_CONSTRAINT_GAP,
    WeightedCoverageDeps,
)
from skill_evaluate.nodes.weighted_coverage.graph import (
    INTERRUPT_BEFORE_NODES,
    add_weighted_coverage_nodes,
    build_weighted_coverage_subgraph,
)
from skill_evaluate.nodes.weighted_coverage.nodes import (
    CONSTRAINT_PROBE_CRITICALITY,
    ENTRY_NODE,
    NODE_NAMES,
    SUBJECT_PREFIX_CONSTRAINT_PROBE,
    SUBJECT_PREFIX_WEIGHTED,
    TEMPLATE_NEGATIVE_CONSTRAINT_PROBE,
    TERMINAL_NODE,
    WeightedCoveragePipeline,
)
from skill_evaluate.nodes.weighted_coverage.priority import (
    as_sorted_pair,
    pair_priority,
    prioritized_pairs,
    prioritized_uncovered_pairs,
)
from skill_evaluate.nodes.weighted_coverage.state import (
    DIMENSION,
    KEY_ARTIFACT_FAILURE,
    KEY_ARTIFACT_PATH,
    KEY_CONSTRAINT_COUNT,
    KEY_CONSTRAINT_PATCHED_COUNT,
    KEY_CONSTRAINT_RATIO,
    KEY_NODE_COUNT,
    KEY_PATCH_FAILURE,
    KEY_PROBE_BUDGET_EXHAUSTED,
    KEY_PROBE_CALL_COUNT,
    KEY_RANKED_PAIR_COUNT,
    KEY_RANKED_UNCOVERED_PAIRS,
    KEY_RATIO,
    KEY_TIER_DISTRIBUTION,
    KEY_TIER_GRADED,
    KEY_TOTAL_PAIR_COUNT,
    KEY_UNCOVERED_CONSTRAINT_IDS,
    KEY_UNDETERMINED_CONSTRAINT_IDS,
    KEY_VERDICT_STATUS,
    NODE_PREFIX,
    ROUTING_KEY,
    WeightedCoverageState,
)

__all__ = [
    "ARTIFACT_BASENAME",
    "CONSTRAINT_PROBE_CRITICALITY",
    "CSV_COLUMNS",
    "DIMENSION",
    "ENTRY_NODE",
    "INTERRUPT_BEFORE_NODES",
    "KEY_ARTIFACT_FAILURE",
    "KEY_ARTIFACT_PATH",
    "KEY_CONSTRAINT_COUNT",
    "KEY_CONSTRAINT_PATCHED_COUNT",
    "KEY_CONSTRAINT_RATIO",
    "KEY_NODE_COUNT",
    "KEY_PATCH_FAILURE",
    "KEY_PROBE_BUDGET_EXHAUSTED",
    "KEY_PROBE_CALL_COUNT",
    "KEY_RANKED_PAIR_COUNT",
    "KEY_RANKED_UNCOVERED_PAIRS",
    "KEY_RATIO",
    "KEY_TIER_DISTRIBUTION",
    "KEY_TIER_GRADED",
    "KEY_TOTAL_PAIR_COUNT",
    "KEY_UNCOVERED_CONSTRAINT_IDS",
    "KEY_UNDETERMINED_CONSTRAINT_IDS",
    "KEY_VERDICT_STATUS",
    "NODE_NAMES",
    "NODE_PREFIX",
    "ROUTING_KEY",
    "SUBJECT_PREFIX_CONSTRAINT_PROBE",
    "SUBJECT_PREFIX_WEIGHTED",
    "TEMPLATE_NEGATIVE_CONSTRAINT_PROBE",
    "TERMINAL_NODE",
    "TRIGGERED_BY_NEGATIVE_CONSTRAINT_GAP",
    "WeightedCoverageDeps",
    "WeightedCoveragePipeline",
    "WeightedCoverageState",
    "add_weighted_coverage_nodes",
    "as_sorted_pair",
    "build_traceability_matrix",
    "build_weighted_coverage_subgraph",
    "flatten_for_csv",
    "pair_priority",
    "prioritized_pairs",
    "prioritized_uncovered_pairs",
    "write_matrix",
]
