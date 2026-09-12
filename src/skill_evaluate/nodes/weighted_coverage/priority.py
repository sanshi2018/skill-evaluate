"""能力组合对的优先级排序（docs/dev/18 第 6 节）。

这是**纯函数模块**：不碰库、不发请求、不读图状态，只依赖 `CapabilityTree` 与
`TIER_WEIGHTS`。单独成文件是因为它有两个调用方，分属两份文档：

- 模块七（`nodes/pruning`）的组合矩阵在截断前用它排序（docs/dev/17 第 5 节留下的
  "过渡截断策略"插槽，docs/dev/18 正式接管）；
- 模块八（本包）的 `upgrade_combinatorial_priority` 在**真实分级落地之后**用它
  重排一次未覆盖组合对。

## 为什么模块七排完了模块八还要再排一次

顺序摆在那里：16 → 17 → 18，而权重分级是 18 的第一个节点。模块七跑组合矩阵时
树上还全是占位 tier，此时"按权重排序"与"按 id 排序"没有区别——它的报告会如实写
"未做优先级筛选的截断分析"。模块八分级之后重排一次，才第一次得到"P0×P0 排在最前"
的真实缺口清单。

两者共用本模块而不是各写一份，是为了让"优先级到底怎么定的"只有一个答案。

## 为什么按权重之和而不是按档位序号之和

`TIER_WEIGHTS` 是 docs/dev/02 定下的唯一一张权重表，加权覆盖率用的就是它。用另一
套"档位序号"排序，等于让同一个概念在系统里有两把尺子：一对 P0×P2（0.6+0.1=0.7）
与一对 P1×P1（0.3+0.3=0.6），按权重是前者优先，按序号之和则是后者优先。两种答案
都说得通，但必须只留一种，否则"为什么这对组合被截断掉了"永远解释不清。
"""

from __future__ import annotations

import itertools

from skill_evaluate.state.capability import TIER_WEIGHTS, CapabilityTree

# 树上找不到的 capability_id 的权重。
#
# 取 0.0（排在最后）而不是抛异常：图状态里的组合对可能是上一次运行留下的
# （Checkpoint 恢复），而能力树在这期间被重抽过，某个 id 已经不存在了。为这种
# 情形掀掉整条流水线不成比例——它本来就是模块七"孤儿检测"要处理的事，本模块
# 只需保证这类组合对不会因为排序而插队到真实的 P0 组合前面。
_UNKNOWN_WEIGHT = 0.0


def _tier_weights(tree: CapabilityTree) -> dict[str, float]:
    """`capability_id -> 权重` 索引。

    先建索引再排序，而不是在比较函数里每次 `next(n for n in tree.nodes if ...)`
    线性查找（docs/dev/18 第 6 节伪码的写法）：组合对是 N² 级别的，逐对再做两次
    线性查找就是 N³，20 个能力节点时已经是七万次比较。
    """
    return {node.capability_id: TIER_WEIGHTS[node.tier] for node in tree.nodes}


def pair_priority(tree: CapabilityTree, pair: tuple[str, str]) -> float:
    """一对组合的优先级分值：两端能力权重之和。

    P0×P0 = 1.2 最高，P2×P2 = 0.2 最低。值本身没有单位，只用于比较。
    """
    weights = _tier_weights(tree)
    return weights.get(pair[0], _UNKNOWN_WEIGHT) + weights.get(pair[1], _UNKNOWN_WEIGHT)


def _sort_key(weights: dict[str, float], pair: tuple[str, str]) -> tuple[float, tuple[str, str]]:
    """排序键：优先级降序，同优先级按 id 对升序。

    第二项保证**确定性**：同一棵树每次给出同一个次序，截断范围因此稳定，组合
    覆盖率才可以在两次运行之间比较。少了它，`sorted()` 只保证稳定排序，而输入
    顺序本身来自数据库返回的行序——那个次序没有任何保证。
    """
    priority = weights.get(pair[0], _UNKNOWN_WEIGHT) + weights.get(pair[1], _UNKNOWN_WEIGHT)
    return (-priority, pair)


def as_sorted_pair(first: str, second: str) -> tuple[str, str]:
    """把两个 id 收敛成有序二元组。

    `(a, b)` 与 `(b, a)` 是同一对组合。全项目统一按字典序排一次，落库、比较、
    去重才都对得上（模块七落 `combinatorial_pairs_covered` 时也按这个口径）。
    """
    left, right = sorted((first, second))
    return (left, right)


def prioritized_pairs(tree: CapabilityTree) -> list[tuple[str, str]]:
    """树上全部两两组合，按"应当优先分析"的次序排列。

    调用方按需截断（模块七取前 `max_capability_pairs_for_matrix` 对）。**不要**
    把截断换成采样：随机采样会让同一份测试集在两次运行中得到不同的组合覆盖率，
    那个数字就再也没法拿来比较了，而"组合覆盖率有没有涨"正是那个维度存在的意义。
    """
    weights = _tier_weights(tree)
    pairs = (as_sorted_pair(a, b) for a, b in itertools.combinations(sorted(weights), 2))
    return sorted(pairs, key=lambda pair: _sort_key(weights, pair))


def prioritized_uncovered_pairs(
    tree: CapabilityTree, uncovered_pairs: list[tuple[str, str]], limit: int
) -> list[tuple[str, str]]:
    """把给定的未覆盖组合对按优先级排序并截断到 `limit` 对（docs/dev/18 第 6 节）。

    与 `prioritized_pairs()` 的分工：那个从树**重新枚举**全部组合，这个只对调用方
    手里已有的一份列表排序。模块八的重排走这条——它要回答的是"当前这批缺口里，
    哪几对最值得先补"，而不是"全部组合里哪几对该进入分析范围"。

    `limit <= 0` 返回空列表：语义是"一对都不要"，而不是"不限量"。切片在负数上会
    悄悄给出别的结果（`[:-1]` 是"去掉最后一个"），因此显式拦一次。
    """
    if limit <= 0:
        return []
    weights = _tier_weights(tree)
    ordered = sorted(
        (as_sorted_pair(a, b) for a, b in uncovered_pairs),
        key=lambda pair: _sort_key(weights, pair),
    )
    return ordered[:limit]


__all__ = [
    "as_sorted_pair",
    "pair_priority",
    "prioritized_pairs",
    "prioritized_uncovered_pairs",
]
