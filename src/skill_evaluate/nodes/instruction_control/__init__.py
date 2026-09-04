"""模块三：指令控制度与执行效果评测。

实现文档：docs/dev/13_模块三_指令控制度与执行效果评测.md。

- `rules.py`：渐进式披露探查的量化规则（**导入本包即完成注册**，见下）。
- `state.py`：本维度的私有状态命名空间与维度名常量。
- `probe.py`：渐进式披露探查的**纯代码**扫描器（无 LLM、无 IO，可单独测）。
- `trace_digest.py`：把 `ExecutionTrace` 压成能塞进评审 Prompt 的文本。
- `deps.py`：依赖注入容器（Executor 后端、Judge、Generator、Optimizer、报告器）
  与四项 `Criticality` 声明。
- `nodes.py`：八个节点的实现与条件路由。
- `graph.py`：子图装配，以及给 docs/dev/24 主图用的平铺装配入口。

**这是四项子评测拼成的一个维度**（架构文档里最复杂的一个）：

| 子评测 | 判定方式 | Criticality | 是否阻断 | 是否进优化闭环 |
|---|---|---|---|---|
| A/B 增值对比（ROI） | 语义裁决 `roi_comparison` | CRITICAL | 是 | 是 |
| Trace 效率诊断 | 语义裁决 `trace_efficiency` | ROUTINE | 否 | 否 |
| 控制标定静态扫描 | 语义裁决 `control_calibration`（复用 07 的 5.7） | ROUTINE | 否 | 否 |
| 渐进式披露动态探查 | **量化规则**（确定性扫描 Trace） | 不适用 | 漏读=是 | 漏读=是 |

**导入副作用（刻意为之）**：本模块导入 `rules`，从而把
`progressive_disclosure_probe` 注册进 docs/dev/08 的量化规则表。与模板注册表同一种
"导入即注册"模式：宁可在装配期因为忘了导入而立刻失败，也不要在跑了半小时的评测
中途才发现规则没挂上。

后续模块的接入点见 docs/dev/interfaces/13_instruction_control_pipeline.md。
"""

from skill_evaluate.nodes.instruction_control import rules as rules  # 导入即注册量化规则
from skill_evaluate.nodes.instruction_control.deps import (
    CALIBRATION_CRITICALITY,
    EFFICIENCY_CRITICALITY,
    EXECUTION_TIMEOUT_S,
    ROI_CRITICALITY,
    TEMPLATE_CONTROL_CALIBRATION,
    TEMPLATE_ROI_COMPARISON,
    TEMPLATE_TRACE_EFFICIENCY,
    InstructionControlDeps,
)
from skill_evaluate.nodes.instruction_control.graph import (
    INTERRUPT_BEFORE_NODES,
    add_instruction_control_nodes,
    build_instruction_control_subgraph,
)
from skill_evaluate.nodes.instruction_control.nodes import (
    ENTRY_NODE,
    MAX_RUN_COUNT_PER_ARM,
    NODE_NAMES,
    TERMINAL_NODE,
    AbPair,
    InstructionControlPipeline,
    JudgmentOutcome,
)
from skill_evaluate.nodes.instruction_control.probe import (
    KIND_MISSING_READ,
    KIND_NO_PROBE_TARGET,
    KIND_OVER_FETCH,
    KIND_TOKEN_WATERMARK,
    ProbeFinding,
    read_reference_paths,
    resolve_token_watermark,
    scan_probe_trace,
)
from skill_evaluate.nodes.instruction_control.rules import (
    RULE_PROGRESSIVE_DISCLOSURE_PROBE,
    probe_inputs,
)
from skill_evaluate.nodes.instruction_control.state import DIMENSION, InstructionControlState
from skill_evaluate.nodes.instruction_control.trace_digest import (
    format_actions_for_review,
    format_final_response,
)

__all__ = [
    "CALIBRATION_CRITICALITY",
    "DIMENSION",
    "EFFICIENCY_CRITICALITY",
    "ENTRY_NODE",
    "EXECUTION_TIMEOUT_S",
    "INTERRUPT_BEFORE_NODES",
    "KIND_MISSING_READ",
    "KIND_NO_PROBE_TARGET",
    "KIND_OVER_FETCH",
    "KIND_TOKEN_WATERMARK",
    "MAX_RUN_COUNT_PER_ARM",
    "NODE_NAMES",
    "ROI_CRITICALITY",
    "RULE_PROGRESSIVE_DISCLOSURE_PROBE",
    "TEMPLATE_CONTROL_CALIBRATION",
    "TEMPLATE_ROI_COMPARISON",
    "TEMPLATE_TRACE_EFFICIENCY",
    "TERMINAL_NODE",
    "AbPair",
    "InstructionControlDeps",
    "InstructionControlPipeline",
    "InstructionControlState",
    "JudgmentOutcome",
    "ProbeFinding",
    "add_instruction_control_nodes",
    "build_instruction_control_subgraph",
    "format_actions_for_review",
    "format_final_response",
    "probe_inputs",
    "read_reference_paths",
    "resolve_token_watermark",
    "scan_probe_trace",
]
