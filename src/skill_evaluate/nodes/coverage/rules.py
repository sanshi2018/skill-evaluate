"""模块六/八共用的覆盖率量化判定规则（docs/dev/16 第 6 节 + docs/dev/18 第 5 节）。

**导入本模块即完成注册**（`nodes/coverage/__init__.py` 会导入），与
docs/dev/11/13/14/15 同一种"导入即注册"模式：宁可在装配期因为忘了导入而立刻失败，
也不要在跑了半小时的评测中途才发现规则没挂上。

## 为什么覆盖率判定走量化规则而不是 LLM

`docs/dev/interfaces/08` 第 0 节的铁律要求"通过/失败的结论一律经过 JudgeAgent"，
但没要求它一定烧一次 LLM。"覆盖率 0.83 是否 >= 阈值 0.9"是纯算术，让模型去比大小
只会引入不确定性和成本，同时**丧失**量化路径的一个关键性质：`JudgeVerdict.model`
会写成 `rule:capability_coverage_threshold`，报告读者据此一眼看出这个结论是算出来
的、不是判出来的。

## ⚠️ 本规则由模块六与模块八**共用**，规则名只有一个

docs/dev/16 第 9 节约定：加权覆盖率落地后本规则被替换为加权版本，**规则名保持
不变**，调用方不需要修改。docs/dev/18 落地时兑现了这条约定，落到代码上是三处改动：

1. `CapabilityTree.weighted_coverage()` 成为**唯一**的覆盖率算法（模块六的
   `blind_spot_detection` 也改成调它，不再自己算未加权比例）；
2. `coverage_inputs()` 的 `tier_weighted` 从写死的 False 改成**由调用方按树的
   实际状态传入**（`CapabilityTree.tier_grading_applied()`）；
3. 规则函数体本身**不变**——它一直就只是一次阈值比较，加权与否体现在传进来的
   `coverage_ratio` 是怎么算出来的。

docs/dev/18 第 5 节原本设想的是给 `judge/rules.py` 加一个 `override_rule()`、并把
整棵 `CapabilityTree` 塞进 `inputs` 让规则自己算。实现时没有这么做，两个原因：

- `inputs` 的 repr 会被拼进 `JudgeVerdict.reasoning` 落库（docs/dev/interfaces/11
  第 6 节踩过的坑）。把一棵几十个节点的树塞进去，每条判定记录都会带上一份完整的
  能力树快照，判定记录从此没法读。
- `override_rule()` 会引入"同一个规则名在不同时刻指向不同实现"的可能。量化规则
  恰恰是报告里最像客观事实的那部分数字，"这次判定到底用的哪条规则"必须是确定的
  ——这也正是 `register_rule()` 遇到重名直接报错的理由。

因此仍然**只有一条实现**，两个模块用同一条：模块六在分级之前调它（此时
`tier_weighted=False`，加权口径退化为未加权，见 `weighted_coverage()` 的说明），
模块八在分级之后再调一次（`tier_weighted=True`）。两条判定记录并存不是冗余——
它们回答的是两个不同的问题："按等权口径测全了吗"与"按重要性加权测全了吗"。
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

    `coverage_ratio` 由调用方按 `CapabilityTree.weighted_coverage()` 算好传进来。
    规则本身**不关心**它是加权还是等权口径——那是 `tier_weighted` 这个标记要
    回答的问题，判定逻辑在两种口径下完全相同（都是"达没达到那条线"）。
    """
    return (
        JudgeVerdictStatus.PASS
        if float(inputs["coverage_ratio"]) >= float(inputs["threshold"])
        else JudgeVerdictStatus.FAIL
    )


def coverage_inputs(
    *, coverage_ratio: float, threshold: float, tier_weighted: bool
) -> dict[str, Any]:
    """折算成 `quantitative_verdict(inputs=...)` 的入参。

    集中在这里拼而不是让节点自己拼字典，理由与 docs/dev/15 的 `rules.py` 相同：
    `inputs` 的 repr 会被拼进 `JudgeVerdict.reasoning`（docs/dev/interfaces/11
    第 6 节踩过的坑），传进去的东西越随意，判定记录就越难读。这里只传三个标量。

    `tier_weighted` 是给报告读者的**口径标记**，不参与判定，取值就是
    `CapabilityTree.tier_grading_applied()`：

    - 模块六的 `blind_spot_detection` 跑在权重分级之前，树上全是占位 tier，
      传 False——此时加权算出来的数与等权完全一样，但那个"一样"是巧合而不是
      结论，标成 True 会让读报告的人以为这个百分比已经体现了能力的重要性差异。
    - 模块八的 `recompute_weighted_coverage` 跑在分级之后，传 True。

    **不设默认值**是刻意的：这个标记决定了历史判定记录能不能被正确解读，让它
    有个默认值，等于允许下一个调用方在不想清楚的情况下顺手用掉一个口径。
    """
    return {
        "coverage_ratio": round(float(coverage_ratio), 4),
        "threshold": float(threshold),
        "tier_weighted": bool(tier_weighted),
    }


__all__ = ["RULE_CAPABILITY_COVERAGE", "coverage_inputs"]
