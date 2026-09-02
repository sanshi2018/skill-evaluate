"""docs/dev/03 第 8 节 / docs/dev/05 第 5 节 截断与脱敏工具测试。"""

from skill_evaluate.executors.sanitize import DEFAULT_MAX_FIELD_BYTES, truncate_field
from skill_evaluate.observability.log_sanitize import redact_secrets, sanitize_for_log


def test_truncate_field_under_limit_unchanged() -> None:
    assert truncate_field("short") == "short"


def test_truncate_field_none_passthrough() -> None:
    assert truncate_field(None) is None


def test_truncate_field_over_limit_truncated_with_marker() -> None:
    huge = "a" * (DEFAULT_MAX_FIELD_BYTES * 2)
    result = truncate_field(huge)
    assert result is not None
    assert len(result.encode("utf-8")) <= DEFAULT_MAX_FIELD_BYTES
    assert "truncated" in result


def test_redact_secrets_masks_api_key() -> None:
    text = "here is my key: sk-ABCDEFGHIJ0123456789abcdefghij"
    assert "sk-ABCDEFGHIJ" not in redact_secrets(text)
    assert "REDACTED" in redact_secrets(text)


def test_redact_secrets_leaves_normal_text_alone() -> None:
    text = "please read the SKILL.md and summarize it"
    assert redact_secrets(text) == text


def test_sanitize_for_log_truncates_long_field() -> None:
    result = sanitize_for_log("x" * 1000, max_length=500)
    assert result is not None
    assert len(result) == 500 + len("...[truncated]")
