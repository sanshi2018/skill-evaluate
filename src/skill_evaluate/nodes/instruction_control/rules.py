"""模块三的量化判定规则（docs/dev/13 第 7 节，注册进 docs/dev/08 的规则表）。

本维度只有**一条**量化规则：渐进式披露动态探查。其余三项子评测（ROI 对比、
效率诊断、控制标定）都是语义裁决，走 `judgmental_verdict()`。

## 为什么探查判定要走量化规则，而不是直接在节点里写 if

判定依据是"这次执行里有没有出现读取某个参考文件的动作"——一个由 `probe.py` 扫出
来的确定性事实，让 LLM 去数只会引入不确定性和成本。但它**仍然要产出
`JudgeVerdict`**：报告聚合、人工复核、`dimension_results` 落库都按同一种判定对象
读取，节点里直接写 `if severe: findings.append(...)` 会让这条结论在裁判记录里
查无实据（docs/dev/interfaces/08 第 0 节的铁律）。

**导入即注册**：本模块必须在图装配前被导入一次，否则 `get_rule()` 会以未知规则名
报错。`nodes/instruction_control/__init__.py` 已经 import 了它，任何从本包取节点
函数的调用方都自动完成注册。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from skill_evaluate.agents.judge.rules import register_rule
from skill_evaluate.nodes.instruction_control.probe import ProbeFinding
from skill_evaluate.state.enums import JudgeVerdictStatus

# 规则名。量化规则名会写进 `JudgeVerdict.model`（形如 `rule:progressive_disclosure_probe`）
# 并出现在报告里，拼错只会在运行期以 JudgeRuleError 暴露，因此收敛成常量。
RULE_PROGRESSIVE_DISCLOSURE_PROBE = "progressive_disclosure_probe"

# `inputs` 的键。与 docs/dev/interfaces/08 第 1 节一致：只传**计数**，不传整串
# Trace 或整串发现——`quantitative_verdict()` 会把 `inputs` 的 repr 原样拼进
# `JudgeVerdict.reasoning`，传大对象会让每条 reasoning 膨胀到几十 KB。
KEY_SEVERE_COUNT = "severe_finding_count"
KEY_MINOR_COUNT = "minor_finding_count"
KEY_CATEGORY = "category"


def probe_inputs(category: str, findings: Sequence[ProbeFinding]) -> dict[str, Any]:
    """把一条探查用例的扫描结果折算成规则输入。

    带上 `category` 只为让 reasoning 里能看出这条判定针对的是"触发探查"还是
    "常规对照"——两者的失败含义完全相反，报告里混在一起看不出所以然。
    """
    return {
        KEY_CATEGORY: category,
        KEY_SEVERE_COUNT: sum(1 for f in findings if f.severe),
        KEY_MINOR_COUNT: sum(1 for f in findings if not f.severe),
    }


@register_rule(RULE_PROGRESSIVE_DISCLOSURE_PROBE)
def _progressive_disclosure_probe(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """有任何发现即 FAIL，一条都没有才 PASS。

    **严重与非严重都判 FAIL**，两者的区别不在这条判定上，而在两个下游动作里：
    - 是否阻断合并（`nodes.py` 的 `blocking`）；
    - 是否进优化闭环（docs/dev/13 第 8 节：只有"漏读"这类严重问题才进）。

    为什么不把"过度抓取"判成 PASS 再靠 findings 提一嘴：那样报告里这条用例的
    裁判记录会显示"通过"，而正文里又说它有问题，两处自相矛盾。判定就该如实说
    "这条没达标"，严重程度由别的字段承担。
    """
    severe = int(inputs.get(KEY_SEVERE_COUNT, 0))
    minor = int(inputs.get(KEY_MINOR_COUNT, 0))
    return JudgeVerdictStatus.PASS if severe + minor == 0 else JudgeVerdictStatus.FAIL


__all__ = [
    "KEY_CATEGORY",
    "KEY_MINOR_COUNT",
    "KEY_SEVERE_COUNT",
    "RULE_PROGRESSIVE_DISCLOSURE_PROBE",
    "probe_inputs",
]
