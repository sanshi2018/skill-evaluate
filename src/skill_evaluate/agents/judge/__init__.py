"""Judge Agent：裁判核心框架与裁判可信度机制。

实现文档：docs/dev/08_Judge_Agent核心框架与裁判可信度机制.md。

- `service.py`：`JudgeAgent`——全项目唯一的判定入口（量化判定 + 裁量判定）。
- `rules.py`：量化判定规则注册表（**空表**，由 docs/dev/11、16~18 注册具体规则）。
- `golden_injector.py`：黄金基准盲测的注入与 `__golden__:` 前缀约定。
- `consensus.py`：3 副本背靠背复核、`[step:N]` 引用一致性、共识归并。
- `health.py`：失误率滑动窗口、按 `(model, temperature_bucket)` 冻结/解冻。

后续模块的接入点见 docs/dev/interfaces/08_judge_rules_and_criticality.md。
"""

from skill_evaluate.agents.judge.consensus import (
    PERSPECTIVE_SUFFIXES,
    STEP_CITATION_RULE,
    ReplicaSpec,
    build_replica_specs,
    evaluate_consensus,
    extract_cited_step_ids,
    reasoning_points_to_same_trace_node,
)
from skill_evaluate.agents.judge.golden_injector import (
    GOLDEN_SUBJECT_PREFIX,
    GoldenInjection,
    golden_subject_id,
    is_golden_subject,
    maybe_inject_golden_case,
)
from skill_evaluate.agents.judge.health import (
    JudgeHealth,
    JudgeHealthMonitor,
    check_judge_health,
    temperature_bucket,
)
from skill_evaluate.agents.judge.rules import (
    QUANTITATIVE_RULE_REGISTRY,
    QuantitativeRule,
    get_rule,
    register_rule,
    registered_rule_names,
)
from skill_evaluate.agents.judge.service import JudgeAgent

__all__ = [
    "GOLDEN_SUBJECT_PREFIX",
    "PERSPECTIVE_SUFFIXES",
    "QUANTITATIVE_RULE_REGISTRY",
    "STEP_CITATION_RULE",
    "GoldenInjection",
    "JudgeAgent",
    "JudgeHealth",
    "JudgeHealthMonitor",
    "QuantitativeRule",
    "ReplicaSpec",
    "build_replica_specs",
    "check_judge_health",
    "evaluate_consensus",
    "extract_cited_step_ids",
    "get_rule",
    "golden_subject_id",
    "is_golden_subject",
    "maybe_inject_golden_case",
    "reasoning_points_to_same_trace_node",
    "register_rule",
    "registered_rule_names",
    "temperature_bucket",
]
