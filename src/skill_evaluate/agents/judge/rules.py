"""量化判定规则注册表（docs/dev/08 第 2 节第 1 类职责）。

**这里没有 LLM**。量化判定是对结构化数据做确定性算术聚合——例如"3 次执行里
`loaded_skill_md=True` 的比例是否 >= 0.5"——纯代码就能算准，让模型去数数只会
引入不确定性和成本。但它依然产出统一的 `JudgeVerdict`：报告聚合、后续人工复核、
`dimension_results` 落库都按同一种判定对象读取，不需要为"这条是算出来的还是判
出来的"分叉。

**本文件启动时是一张空注册表**，这是设计而不是遗漏（docs/dev/08 第 8 节）：
规则的语义属于具体评测维度（触发率是模块一的事、覆盖率阈值是模块六/八的事），
把它们预置在框架层等于替还没写的文档做决定。各维度文档按下面的方式自行注册：

```python
# nodes/trigger_accuracy/rules.py（docs/dev/11 落地）
from skill_evaluate.agents.judge.rules import register_rule
from skill_evaluate.state.enums import JudgeVerdictStatus


@register_rule("trigger_rate_positive")
def _trigger_rate_positive(inputs: dict) -> JudgeVerdictStatus:
    rate = inputs["loaded_count"] / inputs["run_count"]
    return JudgeVerdictStatus.PASS if rate >= 0.5 else JudgeVerdictStatus.FAIL
```

注册模块必须在图装配前被导入一次（与 docs/dev/07 的模板注册同一种"导入即注册"
模式），否则 `get_rule()` 会以未知规则名报错——这是刻意的：宁可启动即失败，也
不要在一次跑了半小时的评测中途才发现某个维度的判定规则根本没挂上。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from skill_evaluate.errors import JudgeRuleError
from skill_evaluate.state.enums import JudgeVerdictStatus

QuantitativeRule = Callable[[dict[str, Any]], JudgeVerdictStatus]

QUANTITATIVE_RULE_REGISTRY: dict[str, QuantitativeRule] = {}

TRule = TypeVar("TRule", bound=QuantitativeRule)


def register_rule(name: str) -> Callable[[TRule], TRule]:
    """把一个确定性函数注册为量化判定规则。可作装饰器使用。

    重名直接报错（与模板注册表同样的取舍）：静默覆盖会让"这次判定到底用的哪条
    规则"无法追溯，而量化规则恰恰是报告里最像"客观事实"的那部分数字。
    """

    def _decorator(rule: TRule) -> TRule:
        if name in QUANTITATIVE_RULE_REGISTRY:
            raise JudgeRuleError(f"量化判定规则重复注册：{name!r}")
        QUANTITATIVE_RULE_REGISTRY[name] = rule
        return rule

    return _decorator


def get_rule(name: str) -> QuantitativeRule:
    rule = QUANTITATIVE_RULE_REGISTRY.get(name)
    if rule is None:
        raise JudgeRuleError(
            f"未注册的量化判定规则 rule_name={name!r}；已注册："
            f"{sorted(QUANTITATIVE_RULE_REGISTRY)}。"
            "规则由各评测维度文档（11 触发率、16~18 覆盖率阈值）自行注册，"
            "并需保证注册模块在使用前被导入一次。"
        )
    return rule


def registered_rule_names() -> list[str]:
    return sorted(QUANTITATIVE_RULE_REGISTRY)


__all__ = [
    "QUANTITATIVE_RULE_REGISTRY",
    "QuantitativeRule",
    "get_rule",
    "register_rule",
    "registered_rule_names",
]
