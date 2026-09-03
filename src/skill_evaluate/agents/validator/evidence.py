"""把 `AssertionResult` 整理成 Judge 的证据输入（docs/dev/10 第 7 节）。

设计文档第 7 节的约定：`AssertionResult` **不**直接构成 `JudgeVerdict`，而是作为
`judgmental_verdict()` 的 `content` 字典的一部分传入，由具体评测维度文档
（`13` 模块三、`15` 模块五）决定"断言证据"与"LLM 语义裁决"如何组合。

本模块因此只做两件事，都不含判定语义：

1. `build_assertion_evidence()`：结果 -> `content` 字典（键名统一，值一律是 str，
   与 `judgmental_verdict(content: dict[str, str])` 的签名对齐）。
2. `all_passed()` / `any_failed()`：两个只读谓词，供维度文档写自己的组合规则
   （常见模式："断言失败直接判负，不必惊动 LLM"）。

刻意**不**提供 `verdict_from_assertion()` 之类的函数：那会变成一条绕过
`JudgeAgent` 的判定路径，违反 docs/dev/interfaces/08 的铁律（凡是"通过/失败"的
结论一律经过 JudgeAgent）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from skill_evaluate.executors.sanitize import truncate_field
from skill_evaluate.state.assertion import AssertionResult, AssertionSpec
from skill_evaluate.state.enums import AssertionStrategy

# `content` 字典里的键名。维度文档的 Prompt 模板按这些键取值，改名会静默地让
# 模板少一段证据（StrictUndefined 会拦下拼错的键，但拦不下"少传一个键"）。
KEY_EXIT_CODE = "assertion_exit_code"
KEY_STDOUT = "assertion_stdout"
KEY_STDERR = "assertion_stderr"
KEY_PASSED = "assertion_passed"
KEY_SUMMARY = "assertion_summary"
KEY_STRATEGY = "assertion_strategy"


def build_assertion_evidence(
    results: Sequence[AssertionResult],
    *,
    spec: AssertionSpec | None = None,
) -> dict[str, str]:
    """把断言结果整理成可直接并入 `judgmental_verdict(content=...)` 的字典。

    没有断言结果时返回一个**显式**的空证据（`assertion_summary` 说明"本用例未做
    确定性断言"），而不是返回 `{}`：模板里少一个键会让裁判以为证据被省略了，
    而"没有断言"这件事本身是裁判需要知道的信息——它意味着这条用例只能靠语义判断。
    """
    if spec is not None and spec.strategy is AssertionStrategy.NONE:
        reason = spec.failure_reason or "该用例未规划确定性断言"
        return {
            KEY_STRATEGY: AssertionStrategy.NONE.value,
            KEY_SUMMARY: f"本用例未执行确定性断言：{reason}",
        }

    if not results:
        return {
            KEY_STRATEGY: spec.strategy.value if spec is not None else "unknown",
            KEY_SUMMARY: "断言已规划但未取到执行结果（沙箱未回传 assertion_executions）",
        }

    first = results[0]
    evidence = {
        KEY_STRATEGY: spec.strategy.value if spec is not None else "unknown",
        KEY_EXIT_CODE: str(first.exit_code),
        KEY_STDOUT: truncate_field(first.stdout) or "",
        KEY_STDERR: truncate_field(first.stderr) or "",
        KEY_PASSED: "true" if first.passed else "false",
        KEY_SUMMARY: _summarize(results),
    }
    return evidence


def _summarize(results: Sequence[AssertionResult]) -> str:
    passed = sum(1 for r in results if r.passed)
    lines = [f"共 {len(results)} 条断言，通过 {passed} 条。"]
    lines.extend(
        f"- {r.assertion_id}: exit_code={r.exit_code} "
        f"{'通过' if r.passed else '失败'}"
        + (f"；stderr: {truncate_field(r.stderr, 500)}" if not r.passed and r.stderr else "")
        for r in results
    )
    return "\n".join(lines)


def all_passed(results: Iterable[AssertionResult]) -> bool:
    """全部断言通过。**空集合返回 True**——"没有断言"不构成失败证据。"""
    return all(r.passed for r in results)


def any_failed(results: Iterable[AssertionResult]) -> bool:
    return any(not r.passed for r in results)


__all__ = [
    "KEY_EXIT_CODE",
    "KEY_PASSED",
    "KEY_STDERR",
    "KEY_STDOUT",
    "KEY_STRATEGY",
    "KEY_SUMMARY",
    "all_passed",
    "any_failed",
    "build_assertion_evidence",
]
