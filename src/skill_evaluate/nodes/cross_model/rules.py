"""模块九三条对照实验的量化判定规则（docs/dev/19 第 4~6 节）。

docs/dev/19 正文在节点里直接写 `if primary.loaded_skill_md and not secondary.loaded_skill_md:
findings.append(...)`——这正是 docs/dev/interfaces/08 第 0 节铁律禁止的"维度节点里自己
写 if/else 判定"。这里把它注册成规则，经 `JudgeAgent.quantitative_verdict()` 产出
`JudgeVerdict`，报告与人工复核按同一种判定对象读取。

## 三条规则，一份实现

三条实验的判定语义完全相同——**参照臂表现正确、变体臂却表现错误，即为一次"脆弱性
暴露"**——区别只在变体是什么（换代理 / 换采样参数 / 剥离措辞）。仍然注册成三个名字：
规则名会写进 `JudgeVerdict.model`（`rule:<name>`），报告里要能一眼区分"这是跨代理差异"
还是"这是咒语依赖"。

## 参照臂自己就没表现对的用例：PASS，不是本维度的问题

"主代理上就没触发的正向用例"是模块一（触发准确度）要报的问题。在这里记 FAIL 会让同一个
缺陷在两个维度各扣一次分，而且报告措辞会错（它不是"代理差异"，而是"两边都不行"）。
节点侧会把这类用例单独列出来供参考，但不计入本维度的脆弱性发现。

**导入即注册**：`nodes/cross_model/__init__.py` 已导入本模块。
"""

from __future__ import annotations

from typing import Any

from skill_evaluate.agents.judge.rules import register_rule
from skill_evaluate.executors.comparison import EXPECTATION_RATE_THRESHOLD, ArmEvidence
from skill_evaluate.state.enums import JudgeVerdictStatus

RULE_HETERO_CONSISTENCY = "cross_model_heterogeneous_consistency"
RULE_PERTURBATION_ROBUSTNESS = "cross_model_perturbation_robustness"
RULE_ABLATION_ROBUSTNESS = "cross_model_ablation_robustness"

KEY_REFERENCE_EXPECTED = "reference_expected"
KEY_REFERENCE_CONCLUSIVE = "reference_conclusive"
KEY_VARIANT_EXPECTED = "variant_expected"
KEY_VARIANT_CONCLUSIVE = "variant_conclusive"


def comparison_inputs(reference: ArmEvidence, variant: ArmEvidence) -> dict[str, Any]:
    """两条臂的证据折算成规则输入：只传计数（inputs 的 repr 会写进 reasoning）。"""
    return {
        KEY_REFERENCE_EXPECTED: reference.expected_count,
        KEY_REFERENCE_CONCLUSIVE: reference.conclusive_count,
        KEY_VARIANT_EXPECTED: variant.expected_count,
        KEY_VARIANT_CONCLUSIVE: variant.conclusive_count,
    }


def _rate(expected: Any, conclusive: Any) -> float | None:
    conclusive_count = int(conclusive)
    if conclusive_count <= 0:
        return None
    return int(expected) / conclusive_count


def _divergence(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """参照臂达标而变体臂不达标 → FAIL。

    任一臂没有有效证据时判 FAIL（没有证据不等于通过）。节点侧会在调用前把这类用例
    分流为"证据不足"，这里的兜底只防调用方漏判——宁可多一条让人看的 FAIL，也不要
    一条凭空的 PASS。
    """
    reference_rate = _rate(inputs[KEY_REFERENCE_EXPECTED], inputs[KEY_REFERENCE_CONCLUSIVE])
    variant_rate = _rate(inputs[KEY_VARIANT_EXPECTED], inputs[KEY_VARIANT_CONCLUSIVE])
    if reference_rate is None or variant_rate is None:
        return JudgeVerdictStatus.FAIL
    if reference_rate < EXPECTATION_RATE_THRESHOLD:
        return JudgeVerdictStatus.PASS
    return (
        JudgeVerdictStatus.FAIL
        if variant_rate < EXPECTATION_RATE_THRESHOLD
        else JudgeVerdictStatus.PASS
    )


@register_rule(RULE_HETERO_CONSISTENCY)
def _hetero_consistency(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """主代理表现正确、异构备用代理表现错误 → 疑似存在代理特定的触发依赖。"""
    return _divergence(inputs)


@register_rule(RULE_PERTURBATION_ROBUSTNESS)
def _perturbation_robustness(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """贪心解码下表现正确、微小采样扰动下表现错误 → 指令逻辑过拟合了贪心解码路径。"""
    return _divergence(inputs)


@register_rule(RULE_ABLATION_ROBUSTNESS)
def _ablation_robustness(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """原版表现正确、剥离情绪化措辞后表现错误 → Skill 依赖"咒语"兜底而非专有知识。"""
    return _divergence(inputs)


__all__ = [
    "KEY_REFERENCE_CONCLUSIVE",
    "KEY_REFERENCE_EXPECTED",
    "KEY_VARIANT_CONCLUSIVE",
    "KEY_VARIANT_EXPECTED",
    "RULE_ABLATION_ROBUSTNESS",
    "RULE_HETERO_CONSISTENCY",
    "RULE_PERTURBATION_ROBUSTNESS",
    "comparison_inputs",
]
