"""模块七：覆盖率驱动的用例集瘦身与动态演进。

实现文档：docs/dev/17_模块七_用例集瘦身与动态演进.md。

- `state.py`：私有状态命名空间；`DIMENSION` 独有（`test_suite_health`），
  `ROUTING_KEY`/`NODE_PREFIX` 从模块六原样沿用（三份覆盖率文档共用一个图分区）。
- `deps.py`：依赖注入容器，继承 `CoverageDeps` 并追加建议队列仓储。
- `nodes.py`：五个节点——冗余折叠、组合矩阵、组合缺口补题、孤儿检测、报告收尾。
- `graph.py`：子图装配，以及给 docs/dev/24 主图用的平铺装配入口。

**为什么单独一个包而不是塞进 `nodes/coverage/`**：三份覆盖率文档在**图**上是同一个
分区（节点名前缀都是 `coverage.`），但在**代码**上是三件独立的事——模块六回答"是否
覆盖"、模块七回答"测试集自身是否健康"、模块八回答"覆盖得是否够重要"。各自成包让
"改瘦身逻辑会不会动到覆盖率计算"这个问题有一个一眼可见的答案。

**本模块与其余维度的三点不同**：

1. 它是全项目**唯一会修改既有用例归属**的维度：把冗余用例的 `split` 降级为
   `COLD`。降级不是删除——`COLD` 用例仍在 `test_suite_versions.case_ids` 里，只是
   消费方按 `split` 过滤时天然跳过（docs/dev/02 早就设计好的"惰性过滤"机制，本
   模块是它的第一个真实生产者）。
2. 它引入了本项目的**第二种人机协作模式**：非阻塞建议队列（`test_case_suggestions`
   表）。与已有的阻塞式挂起（`suspend_and_wait()` + `human_approvals`）的分界见
   `state/suggestion.py` 模块头。
3. 它**不产出 FAIL、也不产出任何 `JudgeVerdict`**：本维度衡量的是测试集健康度而非
   Skill 质量，没有通过/失败的结论可判，因此 docs/dev/interfaces/08 那条"结论一律
   经 JudgeAgent"的铁律在这里没有适用对象（见 `deps.py` 模块头）。

它**不注册**任何量化规则（与模块六不同）：没有结论要判，就没有规则要注册。

后续模块的接入点见 docs/dev/interfaces/17_test_suite_pruning.md。
"""

from skill_evaluate.nodes.pruning.deps import TRIGGERED_BY_COMBINATORIAL_GAP, PruningDeps
from skill_evaluate.nodes.pruning.graph import (
    INTERRUPT_BEFORE_NODES,
    add_pruning_nodes,
    build_pruning_subgraph,
)
from skill_evaluate.nodes.pruning.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    TERMINAL_NODE,
    PruningPipeline,
)
from skill_evaluate.nodes.pruning.state import (
    DIMENSION,
    KEY_ANALYZED_PAIR_COUNT,
    KEY_CLUSTER_COUNT,
    KEY_DEMOTED_CASE_IDS,
    KEY_DEMOTED_VALIDATION_COUNT,
    KEY_MATRIX_TIER_RANKED,
    KEY_MATRIX_TRUNCATED,
    KEY_NEW_SUGGESTION_COUNT,
    KEY_ORPHAN_CASE_IDS,
    KEY_PAIR_COVERAGE_RATIO,
    KEY_PATCH_FAILURE,
    KEY_PATCHED_PAIR_COUNT,
    KEY_TOTAL_PAIR_COUNT,
    KEY_UNCOVERED_PAIRS,
    NODE_PREFIX,
    ROUTING_KEY,
    PruningState,
)

__all__ = [
    "DIMENSION",
    "ENTRY_NODE",
    "INTERRUPT_BEFORE_NODES",
    "KEY_ANALYZED_PAIR_COUNT",
    "KEY_CLUSTER_COUNT",
    "KEY_DEMOTED_CASE_IDS",
    "KEY_DEMOTED_VALIDATION_COUNT",
    "KEY_MATRIX_TIER_RANKED",
    "KEY_MATRIX_TRUNCATED",
    "KEY_NEW_SUGGESTION_COUNT",
    "KEY_ORPHAN_CASE_IDS",
    "KEY_PAIR_COVERAGE_RATIO",
    "KEY_PATCHED_PAIR_COUNT",
    "KEY_PATCH_FAILURE",
    "KEY_TOTAL_PAIR_COUNT",
    "KEY_UNCOVERED_PAIRS",
    "NODE_NAMES",
    "NODE_PREFIX",
    "ROUTING_KEY",
    "TERMINAL_NODE",
    "TRIGGERED_BY_COMBINATORIAL_GAP",
    "PruningDeps",
    "PruningPipeline",
    "PruningState",
    "add_pruning_nodes",
    "build_pruning_subgraph",
]
