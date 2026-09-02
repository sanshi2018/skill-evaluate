"""日志/Trace 字段脱敏工具（docs/dev/05 第 5 节）。

禁止在日志中打印完整 SKILL.md 正文、完整 Prompt、API Key——对超过 500
字符的字符串字段自动截断，对匹配常见密钥格式的字符串自动打码。供 Agent
基类（docs/dev/06 起）记录 reasoning 时复用，防止日志本身成为模块五所警惕
的"敏感信息泄露"面。
"""

from __future__ import annotations

import re

_MAX_LOG_FIELD_LENGTH = 500
_TRUNCATION_SUFFIX = "...[truncated]"

# 常见密钥格式：sk-xxx（OpenAI/Anthropic 风格）、AWS Access Key、通用 Bearer token、
# GitHub token 前缀等。宁可误伤（过度打码）也不可漏判（见架构文档模块五"信息泄露"要求）。
_SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{20,}=*"),
    # 长 base64/hex 类字符串兜底：要求同时含数字与大小写字母，避免把普通重复字符
    # 文本（如日志占位符 "xxxx...xxxx"）误判为密钥。
    re.compile(
        r"(?=[A-Za-z0-9+/]{40,}={0,2})(?=[^\d]*\d)(?=[^a-z]*[a-z])(?=[^A-Z]*[A-Z])[A-Za-z0-9+/]{40,}={0,2}"
    ),
]

_REDACTED = "***REDACTED***"


def redact_secrets(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def sanitize_for_log(value: str | None, max_length: int = _MAX_LOG_FIELD_LENGTH) -> str | None:
    if value is None:
        return None
    redacted = redact_secrets(value)
    if len(redacted) <= max_length:
        return redacted
    return redacted[:max_length] + _TRUNCATION_SUFFIX


def sanitize_mapping(
    data: dict[str, object], max_length: int = _MAX_LOG_FIELD_LENGTH
) -> dict[str, object]:
    """递归对 dict 中的字符串字段做脱敏，非字符串字段原样保留。"""
    result: dict[str, object] = {}
    for key, val in data.items():
        if isinstance(val, str):
            result[key] = sanitize_for_log(val, max_length)
        elif isinstance(val, dict):
            result[key] = sanitize_mapping(val, max_length)
        else:
            result[key] = val
    return result
