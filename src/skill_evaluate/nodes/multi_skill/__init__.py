"""模块十：多技能并发加载与上下文冲突防范评测。

实现文档：docs/dev/20_模块十_多技能并发加载与上下文冲突防范评测.md。

- `state.py`：私有状态命名空间、维度名 / 路由键 / 节点前缀三个常量。
- `rules.py`：七条量化判定规则（**导入本包即完成注册**）。
- `probes.py`：纯函数（命名冲突扫描、交替报错计数、步骤顺序打乱、角色预设抽取）。
- `deps.py`：依赖注入容器（执行后端、Judge、出题服务、报告器、告警分发器、仓储）。
- `nodes.py`：八个节点——准备、命名空间扫描、三条并行动态探测、角色/时序、基石熔断、收尾。
- `graph.py`：子图装配，以及给 docs/dev/24 的平铺装配入口。

本包之外同属模块十的改动：

- `executors/skill_attribution.py`：多技能并发 Trace 的"加载了哪个 Skill"归因；
- `observability/alerts.py`：通用告警分发器接口（docs/dev/22 注册真实通道）；
- `agents/generator/prompts/multi_skill.jinja`：MULTI_SKILL 复合用例生成模板；
- `agents/mini/templates/multi_skill.py`：三个评审模板。

后续模块的接入点见 docs/dev/interfaces/20_multi_skill_conflict.md。
"""

from skill_evaluate.nodes.multi_skill import rules as rules  # 导入即注册
from skill_evaluate.nodes.multi_skill.deps import (
    ALERT_TYPE_DEEP_CONFLICT,
    MULTI_SKILL_CRITICALITY,
    MultiSkillDeps,
)
from skill_evaluate.nodes.multi_skill.graph import (
    INTERRUPT_BEFORE_NODES,
    add_multi_skill_nodes,
    build_multi_skill_subgraph,
)
from skill_evaluate.nodes.multi_skill.nodes import (
    BLOCKING_OUTCOME_KEYS,
    ENTRY_NODE,
    NODE_NAMES,
    PROBE_NODES,
    TERMINAL_NODE,
    MultiSkillPipeline,
    ProbeOutcome,
)
from skill_evaluate.nodes.multi_skill.probes import (
    count_error_ping_pong,
    extract_persona_lines,
    find_tool_name_collisions,
    shuffle_step_order,
)
from skill_evaluate.nodes.multi_skill.rules import (
    RULE_ATTENTION_DECAY,
    RULE_BACKGROUND_OVERTRIGGER,
    RULE_CORE_REGRESSION,
    RULE_INSTRUCTION_DEADLOCK,
    RULE_NAMESPACE_COLLISION,
    RULE_TEMPORAL_FRAGILITY,
    RULE_TRIGGER_HIJACK,
)
from skill_evaluate.nodes.multi_skill.state import (
    DIMENSION,
    NODE_PREFIX,
    ROUTING_KEY,
    MultiSkillState,
)

__all__ = [
    "ALERT_TYPE_DEEP_CONFLICT",
    "BLOCKING_OUTCOME_KEYS",
    "DIMENSION",
    "ENTRY_NODE",
    "INTERRUPT_BEFORE_NODES",
    "MULTI_SKILL_CRITICALITY",
    "NODE_NAMES",
    "NODE_PREFIX",
    "PROBE_NODES",
    "ROUTING_KEY",
    "RULE_ATTENTION_DECAY",
    "RULE_BACKGROUND_OVERTRIGGER",
    "RULE_CORE_REGRESSION",
    "RULE_INSTRUCTION_DEADLOCK",
    "RULE_NAMESPACE_COLLISION",
    "RULE_TEMPORAL_FRAGILITY",
    "RULE_TRIGGER_HIJACK",
    "TERMINAL_NODE",
    "MultiSkillDeps",
    "MultiSkillPipeline",
    "MultiSkillState",
    "ProbeOutcome",
    "add_multi_skill_nodes",
    "build_multi_skill_subgraph",
    "count_error_ping_pong",
    "extract_persona_lines",
    "find_tool_name_collisions",
    "shuffle_step_order",
]
