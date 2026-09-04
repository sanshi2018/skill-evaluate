"""模块四的量化判定规则（docs/dev/14，注册进 docs/dev/08 的规则表）。

## 为什么这些确定性事实也要走 Judge

docs/dev/14 正文对"挂起""连续两次崩溃""没响应 --help"给的是"直接构造结论"的写法。
实现改走 `JudgeAgent.quantitative_verdict()`，与模块三的探查判定同一条理由
（docs/dev/interfaces/08 第 0 节的铁律）：判定依据确实是纯代码算出来的确定性事实
（让 LLM 去数 exit_code 只会更贵更不准），但**结论仍然要以 `JudgeVerdict` 的形式
存在**——报告聚合、人工复核、`dimension_results` 落库都按同一种判定对象读取；
节点里直接 `findings.append("[致命] ...")` 会让这条阻断合并的结论在裁判记录里
查无实据。

与模块二的差别（模块二的行数/Token 卡线**没有**走 Judge）在于"判定对象"：模块二
那条是整份 Skill 的单一指标，聚合成维度级状态即可；本维度是**逐个脚本**的一串
pass/fail，每一条都需要能被单独回查。

## 五条规则

| 规则 | 判什么 | 阻断？ |
|---|---|---|
| `script_non_interactive` | 缺参数时脚本有没有挂起 | **是**（致命） |
| `script_idempotent` | 连续两次执行第二次有没有崩 | **是** |
| `script_help_responsive` | 脚本认不认 `--help`（LLM 审查前的确定性闸门） | 否 |
| `script_rejects_dirty_input` | 脏数据有没有被识别出来 | 否 |
| `script_output_bounded` | 输出体量有没有超过防刷屏上限 | 否 |

阻断与否不在这里决定（规则只回答"达标没达标"），而在 `nodes.py` 的
`finalize_dimension_report()` 里按 docs/dev/14 第 8 节的策略计算。

**导入即注册**：本模块必须在图装配前被导入一次，否则 `get_rule()` 会以未知规则名
报错。`nodes/script_usability/__init__.py` 已经 import 了它，任何从本包取节点函数
的调用方都自动完成注册。
"""

from __future__ import annotations

from typing import Any

from skill_evaluate.agents.judge.rules import register_rule
from skill_evaluate.state.enums import JudgeVerdictStatus

# 规则名。量化规则名会写进 `JudgeVerdict.model`（形如 `rule:script_non_interactive`）
# 并出现在报告里，拼错只会在运行期以 JudgeRuleError 暴露，因此收敛成常量。
RULE_NON_INTERACTIVE = "script_non_interactive"
RULE_HELP_RESPONSIVE = "script_help_responsive"
RULE_REJECTS_DIRTY_INPUT = "script_rejects_dirty_input"
RULE_IDEMPOTENT = "script_idempotent"
RULE_OUTPUT_BOUNDED = "script_output_bounded"

# `inputs` 的键。与 docs/dev/interfaces/08 第 1 节一致：只传**标量**，不传整串
# stdout/stderr——`quantitative_verdict()` 会把 `inputs` 的 repr 原样拼进
# `JudgeVerdict.reasoning`，传大对象会让每条 reasoning 膨胀到几十 KB。
KEY_TIMED_OUT = "timed_out"
KEY_TIMEOUT_S = "timeout_s"
KEY_EXIT_CODE = "exit_code"
KEY_HELP_OUTPUT_CHARS = "help_output_chars"
KEY_MODE_COUNT = "dirty_mode_count"
KEY_REJECTED_COUNT = "rejected_mode_count"
KEY_CRASHED = "second_run_crashed"
KEY_OUTPUT_BYTES = "output_bytes"
KEY_WARN_BYTES = "warn_bytes"
KEY_ALREADY_TRUNCATED = "already_truncated"


@register_rule(RULE_NON_INTERACTIVE)
def _non_interactive(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """挂起 = FAIL，其余一律 PASS（docs/dev/14 第 4 节）。

    注意**退出码不参与判定**：缺参数时以非 0 退出正是我们期望的行为（"脚本应该
    立刻拒绝或报错"），把它算成失败会让所有正确实现的脚本都不达标。这条规则只
    有一个自变量——它有没有在无 TTY、空 stdin 的环境下等下去。
    """
    return JudgeVerdictStatus.FAIL if bool(inputs.get(KEY_TIMED_OUT)) else JudgeVerdictStatus.PASS


@register_rule(RULE_HELP_RESPONSIVE)
def _help_responsive(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """脚本是否对 `--help` 给出了可读输出（docs/dev/14 第 5 节的末段约定）。

    判定口径：**只要拿到了非空输出就算响应**，哪怕退出码非 0——不少 CLI 框架
    （argparse 的 `--help` 之外，还有一大批手写脚本）会把用法打到 stderr 然后以 2
    退出，那仍然是一份能读的文档。两者皆空才算没实现。

    这条规则是 LLM 审查前的**确定性闸门**：FAIL 时节点不会再去调 `help_doc_quality`
    模板——把空字符串交给裁判没有任何审查价值，只是白烧一次 Token。
    """
    responded = int(inputs.get(KEY_HELP_OUTPUT_CHARS, 0)) > 0
    return JudgeVerdictStatus.PASS if responded else JudgeVerdictStatus.FAIL


@register_rule(RULE_REJECTS_DIRTY_INPUT)
def _rejects_dirty_input(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """脏数据至少要被识别出一次（docs/dev/14 第 6 节）。

    全部模式都以 `exit_code == 0` 收场 = 脚本把非 UTF-8 字节、残缺 JSON 一并
    "笑纳"了。对调用它的智能体来说这是最坏的一种失败：没有任何信号说明输入有
    问题，它会拿着一份静默产生的错误结果继续往下走。

    反过来只要有**一种**模式被拒绝，就说明脚本有输入校验，这条判定即通过——具体
    那条报错写得够不够建设性，由 `constructive_error` 模板的语义审查回答。
    """
    modes = int(inputs.get(KEY_MODE_COUNT, 0))
    rejected = int(inputs.get(KEY_REJECTED_COUNT, 0))
    if modes == 0:
        # 一份负载都没构造出来（模式配置被清空）。判 PASS 等于用"没测"换"通过"。
        return JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
    return JudgeVerdictStatus.PASS if rejected > 0 else JudgeVerdictStatus.FAIL


@register_rule(RULE_IDEMPOTENT)
def _idempotent(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """连续两次执行，第二次不得以未处理崩溃收场（docs/dev/14 第 7 节）。

    判定依据是 `looks_like_unhandled_crash()` 的扫描结果而不是裸 exit_code：第二次
    以非 0 退出**并不必然是问题**——一条 `错误：输出文件已存在，请加 --force` 的
    报错恰恰是"安全处理了状态已存在"的正确表现。只有裸堆栈、`FileExistsError`、
    唯一键冲突这类"没被接住"的信号才判失败。
    """
    return JudgeVerdictStatus.FAIL if bool(inputs.get(KEY_CRASHED)) else JudgeVerdictStatus.PASS


@register_rule(RULE_OUTPUT_BOUNDED)
def _output_bounded(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """单次执行的输出体量不得超过防刷屏建议上限（docs/dev/14 第 7 节）。

    体量取的是**截断前**的原始字节数：拿我们自己截断后的长度去判，等于用评测系统
    的防护掩盖了脚本没做防护这件事。
    """
    output = int(inputs.get(KEY_OUTPUT_BYTES, 0))
    warn = int(inputs.get(KEY_WARN_BYTES, 0))
    return JudgeVerdictStatus.FAIL if warn and output > warn else JudgeVerdictStatus.PASS


__all__ = [
    "KEY_ALREADY_TRUNCATED",
    "KEY_CRASHED",
    "KEY_EXIT_CODE",
    "KEY_HELP_OUTPUT_CHARS",
    "KEY_MODE_COUNT",
    "KEY_OUTPUT_BYTES",
    "KEY_REJECTED_COUNT",
    "KEY_TIMED_OUT",
    "KEY_TIMEOUT_S",
    "KEY_WARN_BYTES",
    "RULE_HELP_RESPONSIVE",
    "RULE_IDEMPOTENT",
    "RULE_NON_INTERACTIVE",
    "RULE_OUTPUT_BOUNDED",
    "RULE_REJECTS_DIRTY_INPUT",
]
