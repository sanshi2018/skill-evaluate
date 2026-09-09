"""模块六：能力覆盖率与测试完备性评测。

实现文档：docs/dev/16_模块六_能力覆盖率与测试完备性评测.md。
（本包同时是模块七/八的落脚点——三份文档共享同一棵能力树、同一个 `coverage`
子图分区，各自负责不同的分析维度：本文档只回答"是否覆盖"这一二元问题，权重分级
是 docs/dev/18、冗余折叠与组合矩阵是 docs/dev/17。）

- `state.py`：私有状态命名空间，以及"路由键 / 节点前缀 / 报告维度名"三个**不相同**
  的常量（本维度是全项目唯一一个三者不同的维度，理由见该文件）。
- `rules.py`：`capability_coverage_threshold` 量化规则，注册进 docs/dev/08 的规则表
  （导入即注册）。文档 18 接入加权覆盖率时**改函数体**，不要注册同名规则。
- `deps.py`：依赖注入容器（Analyzer、Judge、Generator、四个仓储、报告器）。
- `nodes.py`：五个节点的实现与判定口径。
- `graph.py`：子图装配，以及给 docs/dev/24 主图用的平铺装配入口。

**与前五个维度的三点不同**：

1. 它是第一个**既读又写测试集**的维度：读正向用例做映射、回填
   `TestCase.target_capability_ids`、检出盲区后反向调用
   `TestSuiteService.incremental_patch()` 补题。这就是架构文档说的"反向驱动与数据
   飞轮闭环"。
2. 它的子图**带环**（补盲 → 重新映射），且有两道出口把环封死，理由见 `graph.py`。
3. 它**不落 `ExecutionTrace`**、也不调 `ExecutorBackend`：全部结论来自 SKILL.md
   文本与用例 prompt 的分析，因此不需要在 `state/trace.py` 的 `run_index` 分配表里
   申领号段（docs/dev/interfaces/13 第 4 节的全局约定对本维度无适用性）。

**导入即注册**：`import skill_evaluate.nodes.coverage` 会把
`capability_coverage_threshold` 注册进 docs/dev/08 的规则表（见 rules.py 模块头）。

后续模块的接入点见 docs/dev/interfaces/16_capability_coverage.md。
"""

# 导入即注册：本行让覆盖率阈值规则进入 docs/dev/08 的规则表（见 rules.py 模块头）。
from skill_evaluate.nodes.coverage import rules as rules
from skill_evaluate.nodes.coverage.deps import TRIGGERED_BY_COVERAGE_GAP, CoverageDeps
from skill_evaluate.nodes.coverage.graph import (
    INTERRUPT_BEFORE_NODES,
    add_coverage_nodes,
    build_coverage_subgraph,
)
from skill_evaluate.nodes.coverage.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    RESUME_CONFIRM,
    SUBJECT_PREFIX_COVERAGE,
    TERMINAL_NODE,
    CoveragePipeline,
)
from skill_evaluate.nodes.coverage.rules import RULE_CAPABILITY_COVERAGE, coverage_inputs
from skill_evaluate.nodes.coverage.state import (
    DIMENSION,
    KEY_BLIND_SPOTS,
    KEY_COVERAGE_RATIO,
    KEY_MAPPED_CASE_COUNT,
    KEY_PATCH_EXHAUSTED,
    KEY_PATCH_FAILURE,
    KEY_PATCH_ITERATIONS,
    KEY_TREE_NODE_COUNT,
    KEY_TREE_REVIEW_CONFIRMED,
    NODE_PREFIX,
    ROUTING_KEY,
    BlindSpot,
    CoverageState,
)

__all__ = [
    "DIMENSION",
    "ENTRY_NODE",
    "INTERRUPT_BEFORE_NODES",
    "KEY_BLIND_SPOTS",
    "KEY_COVERAGE_RATIO",
    "KEY_MAPPED_CASE_COUNT",
    "KEY_PATCH_EXHAUSTED",
    "KEY_PATCH_FAILURE",
    "KEY_PATCH_ITERATIONS",
    "KEY_TREE_NODE_COUNT",
    "KEY_TREE_REVIEW_CONFIRMED",
    "NODE_NAMES",
    "NODE_PREFIX",
    "RESUME_CONFIRM",
    "ROUTING_KEY",
    "RULE_CAPABILITY_COVERAGE",
    "SUBJECT_PREFIX_COVERAGE",
    "TERMINAL_NODE",
    "TRIGGERED_BY_COVERAGE_GAP",
    "BlindSpot",
    "CoverageDeps",
    "CoveragePipeline",
    "CoverageState",
    "add_coverage_nodes",
    "build_coverage_subgraph",
    "coverage_inputs",
]
