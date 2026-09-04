"""模块四：脚本接口的智能体易用性黑盒探测。

实现文档：docs/dev/14_模块四_脚本接口的智能体易用性黑盒探测.md。

- `state.py`：本维度的私有状态命名空间与维度名常量。
- `probes.py`：**纯代码**的探测工具（运行时推断、脏数据构造、流隔离/崩溃特征
  检查），无 LLM、无容器调用，可被单独测试或被 docs/dev/15 复用。
- `rules.py`：五条量化判定规则，注册进 docs/dev/08 的规则表（导入即注册）。
- `deps.py`：依赖注入容器（脚本沙箱、Judge、报告器、Skill 仓储）。
- `nodes.py`：六个节点的实现与判定口径。
- `graph.py`：子图装配，以及给 docs/dev/24 主图用的平铺装配入口。

**与前三个维度的三点不同**：

1. 它不用 `ExecutorBackend`，而用 `executors/script_sandbox.py` 的
   `ScriptSandboxRunner`——本维度裸调脚本子进程，没有 Agent 推理循环，硬塞进
   `ExecutorBackend` 只会产出污染统计口径的假 Trace（详见该模块的模块头）；
2. 它**不落 `ExecutionTrace`**，因此不需要在 `state/trace.py` 的 `run_index`
   分配表里申领号段（docs/dev/interfaces/13 第 4 节的全局约定对本维度无适用性）；
3. 它没有测试集：探测对象是 `scripts/` 目录下的真实文件，用例由 `probes.py`
   按预置模式确定性地构造，不经 Generator。

**导入即注册**：`import skill_evaluate.nodes.script_usability` 会把五条量化规则
注册进 docs/dev/08 的规则表。评审模板（5.4 `help_doc_quality` / 5.5
`constructive_error`）在 docs/dev/07 落地时就已注册，本维度直接复用。

后续模块的接入点见 docs/dev/interfaces/14_script_usability_probing.md。
"""

# 导入即注册：本行让五条量化判定规则进入 docs/dev/08 的规则表（见 rules.py 模块头）。
from skill_evaluate.nodes.script_usability import rules as rules
from skill_evaluate.nodes.script_usability.deps import (
    ERROR_REVIEW_CRITICALITY,
    HELP_REVIEW_CRITICALITY,
    TEMPLATE_CONSTRUCTIVE_ERROR,
    TEMPLATE_HELP_DOC_QUALITY,
    ScriptUsabilityDeps,
)
from skill_evaluate.nodes.script_usability.graph import (
    INTERRUPT_BEFORE_NODES,
    PARALLEL_PROBE_NODES,
    add_script_usability_nodes,
    build_script_usability_subgraph,
)
from skill_evaluate.nodes.script_usability.nodes import (
    ENTRY_NODE,
    NODE_NAMES,
    TERMINAL_NODE,
    ScriptCheckOutcome,
    ScriptUsabilityPipeline,
)
from skill_evaluate.nodes.script_usability.probes import (
    DIRTY_PAYLOAD_MODES,
    DirtyPayload,
    ScriptProbeTarget,
    build_probe_targets,
    check_io_separation,
    generate_dirty_payloads,
    is_mutating_script,
    looks_like_unhandled_crash,
)
from skill_evaluate.nodes.script_usability.state import DIMENSION, ScriptUsabilityState

__all__ = [
    "DIMENSION",
    "DIRTY_PAYLOAD_MODES",
    "ENTRY_NODE",
    "ERROR_REVIEW_CRITICALITY",
    "HELP_REVIEW_CRITICALITY",
    "INTERRUPT_BEFORE_NODES",
    "NODE_NAMES",
    "PARALLEL_PROBE_NODES",
    "TEMPLATE_CONSTRUCTIVE_ERROR",
    "TEMPLATE_HELP_DOC_QUALITY",
    "TERMINAL_NODE",
    "DirtyPayload",
    "ScriptCheckOutcome",
    "ScriptProbeTarget",
    "ScriptUsabilityDeps",
    "ScriptUsabilityPipeline",
    "ScriptUsabilityState",
    "add_script_usability_nodes",
    "build_probe_targets",
    "build_script_usability_subgraph",
    "check_io_separation",
    "generate_dirty_payloads",
    "is_mutating_script",
    "looks_like_unhandled_crash",
]
