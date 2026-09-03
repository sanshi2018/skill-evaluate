"""触发率量化判定规则（docs/dev/11 第 5 节，落地 docs/dev/08 留下的空规则表）。

这里是模块一唯一下"通过/失败"结论的地方，且**没有 LLM**：触发判定的依据是
`ExecutionTrace.loaded_skill_md`——一个由执行后端如实观测到的布尔量，3 次冗余执行
数出来的比例是确定性算术，让模型去数数只会引入不确定性和成本（docs/dev/08 第 2 节）。

规则本身仍然经 `JudgeAgent.quantitative_verdict()` 产出 `JudgeVerdict`，而不是节点里
写 if/else——报告聚合、人工复核、`dimension_results` 落库都按同一种判定对象读取
（docs/dev/interfaces/08 第 0 节的铁律）。

**导入即注册**：本模块必须在图装配前被导入一次，否则 `get_rule()` 会以未知规则名
报错。`nodes/trigger_accuracy/__init__.py` 已经 import 了它，任何从本包取节点函数的
调用方都自动完成注册。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from skill_evaluate.agents.judge.rules import register_rule
from skill_evaluate.state.enums import JudgeVerdictStatus, TestCaseCategory
from skill_evaluate.state.trace import ExecutionTrace

# 规则名。节点侧统一引用这两个常量而不是裸字符串——量化规则名会写进
# `JudgeVerdict.model`（形如 `rule:trigger_rate_positive`）并出现在报告里，
# 拼错在运行期才会以 JudgeRuleError 暴露。
RULE_TRIGGER_RATE_POSITIVE = "trigger_rate_positive"
RULE_TRIGGER_RATE_NEGATIVE = "trigger_rate_negative"

# 架构文档模块一：正向用例 3 次运行中至少触发 2 次（触发率 >= 0.5）记为通过；
# 反向用例触发率必须 < 0.5 才算通过。两条规则共用同一个阈值，方向相反。
TRIGGER_RATE_THRESHOLD = 0.5

# `inputs` 的键。与 docs/dev/interfaces/08_judge_rules_and_criticality.md 第 1 节
# 给出的形状一致：只传两个计数，不传整串 Trace。
# 为什么不按 docs/dev/11 正文那样直接传 `traces`：`quantitative_verdict()` 会把
# `inputs` 的 repr 原样拼进 `JudgeVerdict.reasoning`，传整串 Trace 会让每条判定的
# reasoning 膨胀成几十 KB 的 JSON——既没人读，落库/报告也被撑坏。
KEY_LOADED_COUNT = "loaded_count"
KEY_RUN_COUNT = "run_count"


def trigger_rate_inputs(traces: Sequence[ExecutionTrace]) -> dict[str, Any]:
    """把一条用例的若干次冗余执行折算成规则输入。

    `loaded_skill_md` 由执行后端填写：Hermes 后端从 Hook 回调上报的工具调用链里
    检测是否读取了目标 SKILL.md（docs/dev/03），这正是架构文档要求的"调用链路监控"。
    """
    return {
        KEY_LOADED_COUNT: sum(1 for trace in traces if trace.loaded_skill_md),
        KEY_RUN_COUNT: len(traces),
    }


def _rate(inputs: dict[str, Any]) -> float | None:
    """算触发率；没有任何一次执行记录时返回 None（**不是** 0.0）。

    区分"跑了 3 次都没触发"和"一次都没跑成"很重要：前者是被测 Skill 的问题，
    后者是评测系统自身的问题（沙箱全挂了）。把后者算成 0.0 会让反向用例凭空
    "通过"——一次执行都没有的用例当然没有误触发，但那不构成任何证据。
    """
    run_count = int(inputs[KEY_RUN_COUNT])
    if run_count <= 0:
        return None
    return int(inputs[KEY_LOADED_COUNT]) / run_count


@register_rule(RULE_TRIGGER_RATE_POSITIVE)
def _trigger_rate_positive(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """正向触发用例：该触发却没触发即失败。"""
    rate = _rate(inputs)
    if rate is None:
        return JudgeVerdictStatus.FAIL
    return JudgeVerdictStatus.PASS if rate >= TRIGGER_RATE_THRESHOLD else JudgeVerdictStatus.FAIL


@register_rule(RULE_TRIGGER_RATE_NEGATIVE)
def _trigger_rate_negative(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """反向近脱靶用例：不该触发却触发即失败。

    无执行记录时同样判 FAIL（见 `_rate()`）：没有证据不等于通过。这条判定会把
    该用例送进 Optimizer 闭环，比静默放行更容易被人发现"沙箱其实没跑起来"。
    """
    rate = _rate(inputs)
    if rate is None:
        return JudgeVerdictStatus.FAIL
    return JudgeVerdictStatus.PASS if rate < TRIGGER_RATE_THRESHOLD else JudgeVerdictStatus.FAIL


def rule_for_category(category: TestCaseCategory) -> str:
    """按用例类别选规则。

    模块一只处理 POSITIVE / NEGATIVE 两类；ADVERSARIAL（模块五）、MULTI_SKILL
    （模块十）有各自的判定语义，落到这里说明上游过滤写漏了，显式报错而不是
    按正向用例凑合判一下。
    """
    if category is TestCaseCategory.POSITIVE:
        return RULE_TRIGGER_RATE_POSITIVE
    if category is TestCaseCategory.NEGATIVE:
        return RULE_TRIGGER_RATE_NEGATIVE
    raise ValueError(
        f"触发准确度维度只判定 POSITIVE / NEGATIVE 用例，收到 category={category!r}："
        "对抗用例见 docs/dev/15，多技能用例见 docs/dev/20。"
    )


__all__ = [
    "KEY_LOADED_COUNT",
    "KEY_RUN_COUNT",
    "RULE_TRIGGER_RATE_NEGATIVE",
    "RULE_TRIGGER_RATE_POSITIVE",
    "TRIGGER_RATE_THRESHOLD",
    "rule_for_category",
    "trigger_rate_inputs",
]
