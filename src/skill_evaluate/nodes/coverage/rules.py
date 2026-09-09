"""模块六的量化判定规则（docs/dev/16 第 6 节）。

**导入本模块即完成注册**（`nodes/coverage/__init__.py` 会导入），与
docs/dev/11/13/14/15 同一种"导入即注册"模式：宁可在装配期因为忘了导入而立刻失败，
也不要在跑了半小时的评测中途才发现规则没挂上。

## 为什么覆盖率判定走量化规则而不是 LLM

`docs/dev/interfaces/08` 第 0 节的铁律要求"通过/失败的结论一律经过 JudgeAgent"，
但没要求它一定烧一次 LLM。"覆盖率 0.83 是否 >= 阈值 0.9"是纯算术，让模型去比大小
只会引入不确定性和成本，同时**丧失**量化路径的一个关键性质：`JudgeVerdict.model`
会写成 `rule:capability_coverage_threshold`，报告读者据此一眼看出这个结论是算出来
的、不是判出来的。

## ⚠️ 文档 18 接入方式：改函数体，不要注册同名规则

docs/dev/16 第 9 节约定，加权覆盖率落地后本规则会被**替换**为加权版本，**规则名
保持不变**，调用方（`blind_spot_detection` 节点）不需要修改。

落到代码上，正确做法是**直接改写下面这个函数的函数体**（以及
`coverage_inputs()` 里 `tier_weighted` 的取值）。`register_rule()` 遇到重名会抛
`JudgeRuleError`，所以"在 nodes/coverage 之外再注册一条同名规则"这条路走不通——
这是有意的：静默覆盖会让"这次判定到底用的哪条规则"无法追溯，而量化规则恰恰是
报告里最像客观事实的那部分数字。
"""

from __future__ import annotations

from typing import Any

from skill_evaluate.agents.judge.rules import register_rule
from skill_evaluate.state.enums import JudgeVerdictStatus

RULE_CAPABILITY_COVERAGE = "capability_coverage_threshold"


@register_rule(RULE_CAPABILITY_COVERAGE)
def _capability_coverage_threshold(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """覆盖率达标即通过（架构文档模块六第 3 节的 ">= 90%"）。

    用 `>=` 而不是 `>`：阈值 0.9 的语义是"达到九成"，恰好 0.9 应当通过。

    本文档阶段比较的是**未加权**的简单比例（每项能力等权）。这是模块六自己的判定
    口径，不是对模块八的抢跑：模块六只回答"是否覆盖"这一二元问题，能力有多重要
    是文档 18 的维度（见 `docs/dev/16` 第 1 节的职责划分）。
    """
    return (
        JudgeVerdictStatus.PASS
        if float(inputs["coverage_ratio"]) >= float(inputs["threshold"])
        else JudgeVerdictStatus.FAIL
    )


def coverage_inputs(*, coverage_ratio: float, threshold: float) -> dict[str, Any]:
    """折算成 `quantitative_verdict(inputs=...)` 的入参。

    集中在这里拼而不是让节点自己拼字典，理由与 docs/dev/15 的 `rules.py` 相同：
    `inputs` 的 repr 会被拼进 `JudgeVerdict.reasoning`（docs/dev/interfaces/11
    第 6 节踩过的坑），传进去的东西越随意，判定记录就越难读。这里只传三个标量。

    `tier_weighted` 是给报告读者的**口径标记**，不参与判定：本文档阶段恒为 False
    （所有 `CapabilityNode.tier` 都还是占位值，加权算出来的数与未加权完全一样，
    但那个"一样"是巧合而不是结论）。文档 18 接入加权覆盖率时把它翻成 True，
    历史判定记录据此仍能区分是拿哪种口径算的。
    """
    return {
        "coverage_ratio": round(float(coverage_ratio), 4),
        "threshold": float(threshold),
        "tier_weighted": False,
    }


__all__ = ["RULE_CAPABILITY_COVERAGE", "coverage_inputs"]
