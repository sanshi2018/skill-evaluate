"""模块一：触发准确度与泛化能力评测流水线。

实现文档：docs/dev/11_模块一_触发准确度与泛化能力评测流水线.md。

- `rules.py`：触发率量化规则（**导入本包即完成注册**，见下）。
- `state.py`：本维度的私有状态命名空间与维度名常量。
- `deps.py`：依赖注入容器（Executor 后端、Judge、Generator、Optimizer、报告器）。
- `nodes.py`：七个节点的实现与条件路由。
- `graph.py`：子图装配，以及给 docs/dev/24 主图用的平铺装配入口。

**导入副作用（刻意为之）**：本模块导入 `rules`，从而把 `trigger_rate_positive` /
`trigger_rate_negative` 注册进 docs/dev/08 的量化规则表。与模板注册表同一种"导入
即注册"模式：宁可在装配期因为忘了导入而立刻失败，也不要在跑了半小时的评测中途
才发现规则没挂上。

后续模块的接入点见 docs/dev/interfaces/11_trigger_accuracy_pipeline.md。
"""

from skill_evaluate.nodes.trigger_accuracy import rules as rules  # noqa: F401  导入即注册
from skill_evaluate.nodes.trigger_accuracy.deps import (
    EXECUTION_TIMEOUT_S,
    REDUNDANT_RUNS,
    TriggerAccuracyDeps,
)
from skill_evaluate.nodes.trigger_accuracy.graph import (
    INTERRUPT_BEFORE_NODES,
    add_trigger_accuracy_nodes,
    build_trigger_accuracy_subgraph,
)
from skill_evaluate.nodes.trigger_accuracy.nodes import (
    BLOCKING,
    ENTRY_NODE,
    NODE_NAMES,
    TERMINAL_NODE,
    TriggerAccuracyPipeline,
)
from skill_evaluate.nodes.trigger_accuracy.rules import (
    RULE_TRIGGER_RATE_NEGATIVE,
    RULE_TRIGGER_RATE_POSITIVE,
    TRIGGER_RATE_THRESHOLD,
    rule_for_category,
    trigger_rate_inputs,
)
from skill_evaluate.nodes.trigger_accuracy.state import DIMENSION, TriggerAccuracyState

__all__ = [
    "BLOCKING",
    "DIMENSION",
    "ENTRY_NODE",
    "EXECUTION_TIMEOUT_S",
    "INTERRUPT_BEFORE_NODES",
    "NODE_NAMES",
    "REDUNDANT_RUNS",
    "RULE_TRIGGER_RATE_NEGATIVE",
    "RULE_TRIGGER_RATE_POSITIVE",
    "TERMINAL_NODE",
    "TRIGGER_RATE_THRESHOLD",
    "TriggerAccuracyDeps",
    "TriggerAccuracyPipeline",
    "TriggerAccuracyState",
    "add_trigger_accuracy_nodes",
    "build_trigger_accuracy_subgraph",
    "rule_for_category",
    "trigger_rate_inputs",
]
