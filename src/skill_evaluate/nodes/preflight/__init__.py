"""流水线前置质量门禁：沙箱环境指纹校验 + 金丝雀技能探针。

实现文档：docs/dev/21_模块十一续_Generator可信度与沙箱环境一致性证明.md（Part C/D、第 6、7 节）。

- `fingerprint.py`：指纹模型、探测脚本下发与解析、黄金指纹读写、严格比对；
- `env_fingerprint_probe.sh`：随基础镜像维护的 POSIX sh 探测脚本；
- `deps.py` / `nodes.py` / `graph.py`：依赖容器、两道门禁节点、子图装配；
- 金丝雀探针本体在 `executors/canary.py`（只依赖执行后端协议，CLI 与主图共用）。

后续模块（docs/dev/22 / 24）的接入点见 docs/dev/interfaces/21_generator_trust_and_preflight.md。
"""

from skill_evaluate.nodes.preflight.deps import PreflightDeps, assert_backend_routing
from skill_evaluate.nodes.preflight.fingerprint import (
    EnvironmentProbeRunner,
    SandboxFingerprint,
    diff_fingerprint,
    dump_fingerprint,
    load_golden_fingerprint,
    probe_current_fingerprint,
)
from skill_evaluate.nodes.preflight.graph import (
    INTERRUPT_BEFORE_NODES,
    add_preflight_nodes,
    build_preflight_subgraph,
)
from skill_evaluate.nodes.preflight.nodes import (
    ENTRY_NODE,
    GATE_CANARY,
    GATE_FINGERPRINT,
    NODE_NAMES,
    TERMINAL_NODE,
    PreflightPipeline,
)
from skill_evaluate.nodes.preflight.state import (
    KEY_CANARY_OUTCOME,
    KEY_FINGERPRINT_OUTCOME,
    ROUTING_KEY,
    PreflightState,
)

__all__ = [
    "ENTRY_NODE",
    "GATE_CANARY",
    "GATE_FINGERPRINT",
    "INTERRUPT_BEFORE_NODES",
    "KEY_CANARY_OUTCOME",
    "KEY_FINGERPRINT_OUTCOME",
    "NODE_NAMES",
    "ROUTING_KEY",
    "TERMINAL_NODE",
    "EnvironmentProbeRunner",
    "PreflightDeps",
    "PreflightPipeline",
    "PreflightState",
    "SandboxFingerprint",
    "add_preflight_nodes",
    "assert_backend_routing",
    "build_preflight_subgraph",
    "diff_fingerprint",
    "dump_fingerprint",
    "load_golden_fingerprint",
    "probe_current_fingerprint",
]
