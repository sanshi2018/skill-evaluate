"""渐进式披露动态探查的**纯代码**扫描器（docs/dev/13 第 7 节）。

无 LLM、无 IO：输入一条用例 + 它的执行 Trace，输出若干条 `ProbeFinding`。
判定逻辑本身是对 `ExecutionTrace.actions` 的确定性检查（"有没有出现读取某个
`references/` 文件的动作"），属于 docs/dev/08 定义的"量化判定"范畴而不是语义
裁决——让模型去数"它读没读那个文件"，既贵又不如直接扫一遍准。

扫描结果最终仍然经 `JudgeAgent.quantitative_verdict()` 产出 `JudgeVerdict`
（规则见 `rules.py`），而不是节点里直接写 if/else：报告聚合、人工复核、
`dimension_results` 落库都按同一种判定对象读取（docs/dev/interfaces/08 第 0 节）。

## 严重级别的划分（docs/dev/13 第 7 节的关键决策）

| 发现 | 严重 | 为什么 |
|---|---|---|
| 漏读（触发条件满足却没读参考文件） | **是** | Agent 在没有依据的情况下凭记忆/幻觉作答，是**正确性**问题 |
| 过度抓取（常规任务读了参考文件） | 否 | 只多烧了上下文预算，答案该对还是对，是**成本**问题 |
| Token 水位超标 | 否 | 同上，且它是个统计推断，不是直接证据 |
| 触发探查用例没有探查目标 | 否 | 这条题这次什么也没测出来，是**评测系统**的问题，不是被测 Skill 的问题 |

严重级别决定的是"要不要阻断合并"与"要不要进优化闭环"，与判定本身是否成立无关。
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence

from pydantic import BaseModel

from skill_evaluate.state.enums import TestCaseCategory
from skill_evaluate.state.test_case import TestCase
from skill_evaluate.state.trace import ActionStep, ExecutionTrace

# 参考资料在 skill 目录里的固定前缀（架构文档模块二/三的目录约定）。
REFERENCE_DIR_PREFIX = "references/"

# 被视为"读文件"的 `ActionStep.action_type`。取值来自 docs/dev/02 对 `action_type`
# 的举例（"bash" / "python" / "api_call" / "read_file"）与各执行后端的实际用词。
# 比较时大小写不敏感。
READ_ACTION_TYPES = frozenset({"read_file", "read", "open_file", "view_file", "cat"})

# `action_input` 里可能承载文件路径的键。不同后端叫法不一，全试一遍比强求各后端
# 统一字段名现实——`ActionStep.action_input` 本来就声明为"结构随 action_type 变化，
# 不强约束子 schema"（docs/dev/02）。
PATH_KEYS = ("path", "file_path", "filename", "file", "target", "target_path")

# ---- ProbeFinding.kind 的取值 ----
KIND_MISSING_READ = "missing_read"  # 漏读（严重）
KIND_OVER_FETCH = "over_fetch"  # 过度抓取
KIND_TOKEN_WATERMARK = "token_watermark"  # Token 水位超标
KIND_NO_PROBE_TARGET = "no_probe_target"  # 这条触发探查用例没有探查目标

# 报告 findings 的行前缀。docs/dev/13 第 9 节按前缀区分严重/非严重，这里把前缀
# 常量化，避免报告侧再用中文字符串做前缀匹配（改一个字就全盘失效）。
PREFIX_BY_KIND = {
    KIND_MISSING_READ: "[漏读]",
    KIND_OVER_FETCH: "[过度抓取]",
    KIND_TOKEN_WATERMARK: "[Token水位]",
    KIND_NO_PROBE_TARGET: "[探查目标缺失]",
}

_DEFAULT_PREFIX = "[探查]"


class ProbeFinding(BaseModel):
    """一条探查发现。

    带 `case_id` / `trace_id` 是为了让报告里的每一条结论都能被追回到具体的那次
    执行——"某个常规任务过度抓取了"这种说法，读报告的人无从复核。
    """

    case_id: str
    trace_id: str
    # PREFIX_BY_KIND = {
    #     KIND_MISSING_READ: "[漏读]",
    #     KIND_OVER_FETCH: "[过度抓取]",
    #     KIND_TOKEN_WATERMARK: "[Token水位]",
    #     KIND_NO_PROBE_TARGET: "[探查目标缺失]",
    # }
    kind: str
    severe: bool
    message: str

    @property
    def report_line(self) -> str:
        """报告 findings 里的一行（带类别前缀）。"""
        return f"{PREFIX_BY_KIND.get(self.kind, _DEFAULT_PREFIX)} {self.message}"


def _iter_input_strings(action: ActionStep) -> Iterable[str]:
    """把一个动作的输入里所有字符串值摊平。

    只看一层：`action_input` 的约定是扁平的参数字典，为了捞一个路径去递归遍历
    任意深度的嵌套，命中的多半是噪音（例如某个响应体里恰好出现了文件名）。
    """
    for value in action.action_input.values():
        if isinstance(value, str):
            yield value


def read_reference_paths(trace: ExecutionTrace, known_reference_paths: Sequence[str]) -> set[str]:
    """扫出这次执行里实际被读取的参考文件路径。

    两级识别，都**只承认已知路径**（`known_reference_paths` 来自
    `SkillDefinition.reference_files`），不做任何模糊推断：

    1. 显式读文件动作（`action_type` 在 `READ_ACTION_TYPES` 里）：从若干个可能的
       路径键里取值，按后缀匹配已知路径——Agent 给的可能是绝对路径
       （`/work/skill/references/errors.md`），按已知相对路径做后缀匹配即可命中。
    2. 其余动作（典型是 `bash: cat references/errors.md`）：在输入的字符串值里
       找**已知路径的原文**。用"已知路径做子串"而不是"正则捞出一个像路径的东西
       再去比对"，是为了让误报率为零——命中即意味着这次执行确实提到了那个文件。
       代价是漏报（把路径拆成变量再拼出来的写法捞不到），这是刻意的取舍：漏读
       判定是**严重**问题，宁可漏报也不能凭一个正则的猜测去指控一份 Skill。

    路径一律按 POSIX 分隔符比较：沙箱是 Linux 容器（docs/dev/03），这里不为
    Windows 风格路径做兼容——多一条转换规则就多一条会误命中的规则。
    """
    known = [p for p in known_reference_paths if p]
    found: set[str] = set()
    for action in trace.actions:
        is_read_action = action.action_type.lower() in READ_ACTION_TYPES
        candidates = (
            [str(action.action_input[key]) for key in PATH_KEYS if key in action.action_input]
            if is_read_action
            else []
        )
        for raw in candidates:
            for path in known:
                if raw == path or raw.endswith("/" + path):
                    found.add(path)
        for text in _iter_input_strings(action):
            for path in known:
                if path in text:
                    found.add(path)
    return found


def resolve_token_watermark(
    traces: Sequence[ExecutionTrace],
    *,
    ratio: float,
    min_samples: int,
    override: int | None = None,
) -> int | None:
    """算出"常规任务该烧多少 Token"的水位线；样本不足时返回 None（= 不做这项检查）。

    `override` 优先：docs/dev/24 或将来某个掌握真实历史水位的维度可以从图状态
    （`_ic_baseline_token_watermark`）塞一个更靠谱的值进来。

    没有 override 时，水位 = **同一批常规探查用例的 Token 中位数 × (1 + ratio)**。

    为什么这么定：

    - 绝对阈值没有意义。`total_tokens` 里绝大部分是任务提示词与工具输出，换一份
      Skill、换一批题就完全不是一个量级，写死的数字每次都得重调。
    - 用**同批**样本做基线，比较的才是同一类任务；中位数比均值抗离群点——被我们
      盯上的那个异常值本身就在样本里，用均值会被它自己把水位抬高。
    - 样本少于 `min_samples` 时返回 None：三个以下的样本算出来的中位数本身就是
      噪音，拿它去指控某条用例"超出基线"是没有根据的。
    """
    if override is not None:
        return int(override)
    samples = [t.timing.total_tokens for t in traces if t.timing.total_tokens > 0]
    if len(samples) < min_samples:
        return None
    return int(statistics.median(samples) * (1 + ratio))


def scan_probe_trace(
    case: TestCase,
    trace: ExecutionTrace,
    *,
    known_reference_paths: Sequence[str],
    token_watermark: int | None = None,
) -> list[ProbeFinding]:
    """对一条探查用例的执行结果做确定性扫描，返回发现列表（无问题则为空）。

    两类用例的判定方向是相反的，这正是它们必须成对存在的原因：只出触发探查题，
    一份"把所有参考文件都无脑读一遍"的 Skill 会满分通过；只出常规题，一份
    "从来不读参考文件"的 Skill 会满分通过。
    """
    findings: list[ProbeFinding] = []
    read_paths = read_reference_paths(trace, known_reference_paths)

    if case.category is TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER:
        target = case.probe_target_reference
        if not target:
            # 出题阶段没能把探查目标对上号（见 `GeneratorAgent._resolve_probe_target`）。
            # 这条题这次什么也没测出来——如实记一条非严重发现，而不是当它通过了。
            findings.append(
                ProbeFinding(
                    case_id=case.case_id,
                    trace_id=trace.trace_id,
                    kind=KIND_NO_PROBE_TARGET,
                    severe=False,
                    message=(
                        f"用例 {case.case_id} 是触发探查用例但没有探查目标参考文件，"
                        "本次未能对它做渐进式披露判定（出题阶段未能把目标对上号）。"
                    ),
                )
            )
        elif target not in read_paths:
            findings.append(
                ProbeFinding(
                    case_id=case.case_id,
                    trace_id=trace.trace_id,
                    kind=KIND_MISSING_READ,
                    severe=True,
                    message=(
                        f"触发条件满足但未读取 {target}（用例 {case.case_id}，"
                        f"Trace {trace.trace_id}；本次实际读取的参考文件："
                        f"{sorted(read_paths)}）。"
                        "Agent 在没有依据的情况下作答，属于正确性问题。"
                    ),
                )
            )
        return findings

    # PROGRESSIVE_DISCLOSURE_REGULAR
    if read_paths:
        findings.append(
            ProbeFinding(
                case_id=case.case_id,
                trace_id=trace.trace_id,
                kind=KIND_OVER_FETCH,
                severe=False,
                message=(
                    f"常规任务擅自读取了 {sorted(read_paths)}（用例 {case.case_id}，"
                    f"Trace {trace.trace_id}）：不影响答案正确性，但白白吃掉上下文"
                    "预算，属于上下文利用率不达标。"
                ),
            )
        )
    if token_watermark is not None and trace.timing.total_tokens > token_watermark:
        findings.append(
            ProbeFinding(
                case_id=case.case_id,
                trace_id=trace.trace_id,
                kind=KIND_TOKEN_WATERMARK,
                severe=False,
                message=(
                    f"常规任务 Token 消耗 {trace.timing.total_tokens} 超出基线水位 "
                    f"{token_watermark}（用例 {case.case_id}）：渐进式披露设计可能未生效。"
                ),
            )
        )
    return findings


__all__ = [
    "KIND_MISSING_READ",
    "KIND_NO_PROBE_TARGET",
    "KIND_OVER_FETCH",
    "KIND_TOKEN_WATERMARK",
    "PATH_KEYS",
    "PREFIX_BY_KIND",
    "READ_ACTION_TYPES",
    "REFERENCE_DIR_PREFIX",
    "ProbeFinding",
    "read_reference_paths",
    "resolve_token_watermark",
    "scan_probe_trace",
]
