"""模块五：安全性与注入风险红蓝对抗评测。

实现文档：docs/dev/15_模块五_安全性与注入风险红蓝对抗评测.md。

- `rules.py`：五条量化判定规则（**导入本包即完成注册**，见下）。
- `state.py`：本维度的私有状态命名空间与维度名常量。
- `detectors.py`：确定性证据扫描器（无 LLM、无 IO，纯函数，可单独测）。
- `regression.py`：强制功能回归——复用模块一/三的判定，验证安全补丁没把业务改坏。
- `deps.py`：依赖注入容器 + 两条装配期硬校验（后端路由、无出站网络）。
- `nodes.py`：九个节点的实现与条件路由。
- `graph.py`：子图装配，以及给 docs/dev/24 主图用的平铺装配入口。

**这是架构文档里对第 0/1 层公共能力复用度最高的一个维度**：它一个新的执行机制都
没发明，全部是把已有能力按安全语义组合起来。

| 支路 | 判定方式 | Criticality | 初始严重级别 | 是否进闭环 |
|---|---|---|---|---|
| 直接提示词注入 | 语义裁决 `prompt_injection_defense` | **CRITICAL** | high | 是（训练集） |
| 间接数据投毒 | 量化规则（载荷是否被执行） | 不适用 | critical / medium | 是（训练集） |
| 环境变量窃取 | 量化规则（复用 05 的脱敏正则库） | 不适用 | critical | 是（训练集） |
| 目录穿越 | 量化规则（路径逃逸 + 访问成功） | 不适用 | high | 是（训练集） |
| DoS 与上下文耗尽 | 量化规则（**超时 = 通过**） | 不适用 | medium | 是（训练集） |
| 生成物 SAST | 断言脚本 exit_code（**不经 LLM 裁判**） | 不适用 | high | 是（训练集） |
| 严重性最终裁定 | 语义裁决 `security_severity_rating` | **CRITICAL** | — | — |

**导入副作用（刻意为之）**：本模块导入 `rules`，从而把五条量化规则注册进
docs/dev/08 的规则表。与模板注册表同一种"导入即注册"模式：宁可在装配期因为忘了
导入而立刻失败，也不要在跑了半小时的评测中途才发现规则没挂上。

红队用例的**出题**侧在 `agents/attacker/`（Attacker Agent）。
后续模块的接入点见 docs/dev/interfaces/15_security_red_team.md。
"""

from skill_evaluate.nodes.security import rules as rules  # 导入即注册量化规则
from skill_evaluate.nodes.security.deps import (
    SECURITY_CRITICALITY,
    TEMPLATE_PROMPT_INJECTION_DEFENSE,
    TEMPLATE_SECURITY_SEVERITY_RATING,
    SecurityDeps,
)
from skill_evaluate.nodes.security.detectors import (
    DEFAULT_WORKSPACE_ROOT,
    find_escaped_file_access,
    find_payload_executions,
    has_graceful_error,
    looks_like_payload_execution,
    path_escapes_workspace,
    scan_for_credential_patterns,
    summarize_actions,
    trace_crashed,
    trace_timed_out,
)
from skill_evaluate.nodes.security.graph import (
    INTERRUPT_BEFORE_NODES,
    add_security_nodes,
    build_security_subgraph,
)
from skill_evaluate.nodes.security.nodes import (
    BLOCKING_SEVERITIES,
    ENTRY_NODE,
    NODE_NAMES,
    PARALLEL_PROBE_NODES,
    REPORT_BLOCKING_SEVERITIES,
    TERMINAL_NODE,
    SecurityPipeline,
)
from skill_evaluate.nodes.security.regression import FunctionalRegressionRunner, RegressionOutcome
from skill_evaluate.nodes.security.rules import (
    RULE_ARTIFACT_SAST,
    RULE_DOS_RESILIENCE,
    RULE_ENV_LEAK,
    RULE_PATH_TRAVERSAL,
    RULE_PAYLOAD_EXECUTION,
)
from skill_evaluate.nodes.security.state import DIMENSION, SecurityState

__all__ = [
    "BLOCKING_SEVERITIES",
    "DEFAULT_WORKSPACE_ROOT",
    "DIMENSION",
    "ENTRY_NODE",
    "INTERRUPT_BEFORE_NODES",
    "NODE_NAMES",
    "PARALLEL_PROBE_NODES",
    "REPORT_BLOCKING_SEVERITIES",
    "RULE_ARTIFACT_SAST",
    "RULE_DOS_RESILIENCE",
    "RULE_ENV_LEAK",
    "RULE_PATH_TRAVERSAL",
    "RULE_PAYLOAD_EXECUTION",
    "SECURITY_CRITICALITY",
    "TEMPLATE_PROMPT_INJECTION_DEFENSE",
    "TEMPLATE_SECURITY_SEVERITY_RATING",
    "TERMINAL_NODE",
    "FunctionalRegressionRunner",
    "RegressionOutcome",
    "SecurityDeps",
    "SecurityPipeline",
    "SecurityState",
    "add_security_nodes",
    "build_security_subgraph",
    "find_escaped_file_access",
    "find_payload_executions",
    "has_graceful_error",
    "looks_like_payload_execution",
    "path_escapes_workspace",
    "scan_for_credential_patterns",
    "summarize_actions",
    "trace_crashed",
    "trace_timed_out",
]
