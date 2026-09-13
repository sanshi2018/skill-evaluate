"""模块九：跨模型泛化与代理绑架防范机制。

实现文档：docs/dev/19_模块九_跨模型泛化与代理绑架防范机制.md。

- `state.py`：私有状态命名空间、维度名 / 路由键 / 节点前缀三个常量。
- `rules.py`：三条对照实验的量化判定规则（**导入本包即完成注册**）。
- `deps.py`：依赖注入容器（主/备两个执行后端、Judge、报告器、仓储）。
- `nodes.py`：六个节点——抽样、异构矩阵、参数扰动、随机消融、语言坏味道审查、收尾。
- `graph.py`：子图装配（三条实验并行扇出再汇合），以及给 docs/dev/24 的平铺装配入口。

本包之外的三块同属模块九：

- `executors/llama_backend.py`：备用异构代理 `llama_control`（docs/dev/03 的待接入项）；
- `executors/comparison.py`：对照实验的执行骨架与"什么算有效证据"的口径；
- `agents/analyzer/ablation_lexicon.py`：AI 话术黑名单词典（消融 / 静态审查 / 补丁拦截共用）；
- `agents/optimizer/consensus_gate.py`：Pareto 最优补丁裁决（可选门控，由各闭环按需叠加）。

后续模块的接入点见 docs/dev/interfaces/19_cross_model_generalization.md。
"""

from skill_evaluate.nodes.cross_model import rules as rules  # noqa: F401  导入即注册
from skill_evaluate.nodes.cross_model.deps import (
    LINGUISTIC_SMELL_CRITICALITY,
    LINGUISTIC_SMELL_TEMPLATE_KEY,
    CrossModelDeps,
)
from skill_evaluate.nodes.cross_model.graph import (
    INTERRUPT_BEFORE_NODES,
    add_cross_model_nodes,
    build_cross_model_subgraph,
)
from skill_evaluate.nodes.cross_model.nodes import (
    BLOCKING,
    ENTRY_NODE,
    NODE_NAMES,
    PROBE_NODES,
    TERMINAL_NODE,
    CrossModelPipeline,
    LinguisticOutcome,
    ProbeOutcome,
    sample_validation_cases,
)
from skill_evaluate.nodes.cross_model.rules import (
    RULE_ABLATION_ROBUSTNESS,
    RULE_HETERO_CONSISTENCY,
    RULE_PERTURBATION_ROBUSTNESS,
    comparison_inputs,
)
from skill_evaluate.nodes.cross_model.state import (
    DIMENSION,
    NODE_PREFIX,
    ROUTING_KEY,
    CrossModelState,
)

__all__ = [
    "BLOCKING",
    "DIMENSION",
    "ENTRY_NODE",
    "INTERRUPT_BEFORE_NODES",
    "LINGUISTIC_SMELL_CRITICALITY",
    "LINGUISTIC_SMELL_TEMPLATE_KEY",
    "NODE_NAMES",
    "NODE_PREFIX",
    "PROBE_NODES",
    "ROUTING_KEY",
    "RULE_ABLATION_ROBUSTNESS",
    "RULE_HETERO_CONSISTENCY",
    "RULE_PERTURBATION_ROBUSTNESS",
    "TERMINAL_NODE",
    "CrossModelDeps",
    "CrossModelPipeline",
    "CrossModelState",
    "LinguisticOutcome",
    "ProbeOutcome",
    "add_cross_model_nodes",
    "build_cross_model_subgraph",
    "comparison_inputs",
    "sample_validation_cases",
]
