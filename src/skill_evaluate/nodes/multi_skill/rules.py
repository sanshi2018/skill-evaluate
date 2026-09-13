"""模块十的量化判定规则（docs/dev/20 第 5~10 节）。

docs/dev/20 正文在每个节点里直接 `if ...: findings.append(...)`——那正是
docs/dev/interfaces/08 第 0 节铁律禁止的"维度节点里自己写 if/else 判定"。这里把全部
确定性判定注册成规则，经 `JudgeAgent.quantitative_verdict()` 产出 `JudgeVerdict`，
报告与审查工作台按同一种判定对象回查。

| 规则 | 判什么 | FAIL 条件 |
|---|---|---|
| `multi_skill_namespace_collision` | 工具命名冲突（静态） | 被测 Skill 暴露的工具与干扰包同名 ≥ 1 个 |
| `multi_skill_trigger_hijack` | 触发劫持 | 单测加载了目标、并发时没加载 |
| `multi_skill_background_overtrigger` | 背景过触发 | 目标自己的正向任务里干扰技能被加载 ≥ 1 个 |
| `multi_skill_instruction_deadlock` | 指令拮抗死锁 | 错误动作的"A→B→A"往返次数 ≥ 阈值 |
| `multi_skill_attention_decay` | 注意力衰减 | 单测遵守负向约束、并发时违反 |
| `multi_skill_temporal_fragility` | 拓扑脆弱 | 原顺序执行健康、打乱顺序后不健康 |
| `multi_skill_core_regression` | 基石回归熔断 | 并发触发率 < 安全下限 **且** 低于该核心 Skill 独立执行的触发率 |

## 参照臂自己就不对的：PASS

与模块九同一条口径：单测就没触发的正向用例是模块一的问题，单测就违反约束的是模块三/
八的问题——在本维度记 FAIL 会让同一个缺陷在两个维度各扣一次分，且措辞会错（它不是
"并发导致的"）。节点侧把这类情形写进报告的说明行，不计入冲突发现。

## 基石回归为什么要比基线

docs/dev/20 正文只看"并发触发率 < 0.8"。一个核心 Skill 独立执行时触发率就只有 0.6，
那么引入任何新 Skill 都会"导致它跌破 0.8"——而这是全模块唯一**阻断合并**的判定，
把一个与本次 PR 无关的既有弱点算到提交者头上，会让这道闸门很快被人关掉。因此要求
"跌破下限"与"比独立执行更差"同时成立，才能说是**引入本 Skill 导致的**。

**导入即注册**：`nodes/multi_skill/__init__.py` 已导入本模块。
"""

from __future__ import annotations

from typing import Any

from skill_evaluate.agents.judge.rules import register_rule
from skill_evaluate.state.enums import JudgeVerdictStatus

RULE_NAMESPACE_COLLISION = "multi_skill_namespace_collision"
RULE_TRIGGER_HIJACK = "multi_skill_trigger_hijack"
RULE_BACKGROUND_OVERTRIGGER = "multi_skill_background_overtrigger"
RULE_INSTRUCTION_DEADLOCK = "multi_skill_instruction_deadlock"
RULE_ATTENTION_DECAY = "multi_skill_attention_decay"
RULE_TEMPORAL_FRAGILITY = "multi_skill_temporal_fragility"
RULE_CORE_REGRESSION = "multi_skill_core_regression"


def _fail_if(condition: bool) -> JudgeVerdictStatus:
    return JudgeVerdictStatus.FAIL if condition else JudgeVerdictStatus.PASS


def _reference_ok_variant_broken(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """参照臂（单测 / 原顺序）表现正确而变体臂（并发 / 打乱顺序）表现错误 → FAIL。"""
    return _fail_if(bool(inputs["reference_ok"]) and not bool(inputs["variant_ok"]))


@register_rule(RULE_NAMESPACE_COLLISION)
def _namespace_collision(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """同名工具被多个 Skill 暴露：Agent 调用 `parse_data` 时到底调的是谁，取决于加载顺序。"""
    return _fail_if(int(inputs["colliding_tool_count"]) > 0)


@register_rule(RULE_TRIGGER_HIJACK)
def _trigger_hijack(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """单测能触发目标 Skill，挂上干扰包后就不触发了 → 被背景技能劫持。"""
    return _reference_ok_variant_broken(inputs)


@register_rule(RULE_BACKGROUND_OVERTRIGGER)
def _background_overtrigger(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """目标 Skill 的正向任务里，干扰技能因语义重叠被额外激活（架构文档"过度触发"）。"""
    return _fail_if(int(inputs["background_loaded_count"]) > 0)


@register_rule(RULE_INSTRUCTION_DEADLOCK)
def _instruction_deadlock(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """高频交替报错：为满足 A 的指令打破 B 的约束、再为满足 B 打破 A，陷入验证反馈循环。"""
    return _fail_if(int(inputs["ping_pong_count"]) >= int(inputs["threshold"]))


@register_rule(RULE_ATTENTION_DECAY)
def _attention_decay(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """单测遵守了 Gotchas 约束，多技能并发时却忽略了 → 注意力衰减（中间遗忘效应）。"""
    return _reference_ok_variant_broken(inputs)


@register_rule(RULE_TEMPORAL_FRAGILITY)
def _temporal_fragility(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """原顺序下执行健康，打乱业务步骤顺序后崩溃/未触发/死锁 → 拓扑脆弱。"""
    return _reference_ok_variant_broken(inputs)


def _rate(loaded: Any, conclusive: Any) -> float | None:
    conclusive_count = int(conclusive)
    return None if conclusive_count <= 0 else int(loaded) / conclusive_count


@register_rule(RULE_CORE_REGRESSION)
def _core_regression(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """并发触发率跌破安全下限，且比该核心 Skill 独立执行时更差（见模块头）。

    任一臂没有有效证据时判 FAIL：这是唯一阻断合并的规则，"没有证据"绝不能被读成
    "没有回归"。节点侧会在调用前把这类情形分流为"证据不足"，这里的兜底只防调用方漏判。
    """
    baseline = _rate(inputs["baseline_loaded"], inputs["baseline_conclusive"])
    crowded = _rate(inputs["crowded_loaded"], inputs["crowded_conclusive"])
    if baseline is None or crowded is None:
        return JudgeVerdictStatus.FAIL
    return _fail_if(crowded < float(inputs["min_rate"]) and crowded < baseline)


__all__ = [
    "RULE_ATTENTION_DECAY",
    "RULE_BACKGROUND_OVERTRIGGER",
    "RULE_CORE_REGRESSION",
    "RULE_INSTRUCTION_DEADLOCK",
    "RULE_NAMESPACE_COLLISION",
    "RULE_TEMPORAL_FRAGILITY",
    "RULE_TRIGGER_HIJACK",
]
