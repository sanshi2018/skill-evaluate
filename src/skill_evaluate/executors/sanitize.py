"""防刷屏 / 输出截断约定（docs/dev/03 第 8 节）。

`ActionStep.stdout` / `stderr` 及 `final_response` 在写入 `ExecutionTrace` 前
统一经过此处的截断逻辑，避免"逻辑炸弹/Zip Bomb"场景下超长输出把 Postgres
记录或后续 LLM 裁判的上下文撑爆。两个 Backend 实现共用，不允许各自实现一遍。
"""

from __future__ import annotations

DEFAULT_MAX_FIELD_BYTES = 32 * 1024  # 32KB
_TRUNCATION_MARKER = "\n...[truncated by skill_evaluate.executors.sanitize]...\n"


def truncate_field(value: str | None, max_bytes: int = DEFAULT_MAX_FIELD_BYTES) -> str | None:
    """超出上限的字段保留头尾各一半 + 中间省略标记。"""
    if value is None:
        return None
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value

    marker_bytes = _TRUNCATION_MARKER.encode("utf-8")
    remaining = max_bytes - len(marker_bytes)
    if remaining <= 0:
        # 上限过小时退化为纯截断
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    half = remaining // 2
    head = encoded[:half].decode("utf-8", errors="ignore")
    tail = encoded[-half:].decode("utf-8", errors="ignore")
    return f"{head}{_TRUNCATION_MARKER}{tail}"
