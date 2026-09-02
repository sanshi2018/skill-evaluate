"""docs/dev/01 配置系统与异常体系基础校验。"""

import pytest

from skill_evaluate.config import Settings, get_settings
from skill_evaluate.errors import (
    ConfigurationError,
    ExecutorBackendError,
    PipelineSuspended,
    SkillEvaluateError,
)


def test_get_settings_returns_cached_singleton() -> None:
    assert get_settings() is get_settings()


def test_default_executor_backend_is_mini() -> None:
    settings = Settings()
    assert settings.executor.backend == "mini"


def test_db_dsn_contains_configured_fields() -> None:
    settings = Settings()
    assert settings.db.user in settings.db.dsn
    assert settings.db.database in settings.db.dsn


@pytest.mark.parametrize("exc_cls", [ConfigurationError, ExecutorBackendError, PipelineSuspended])
def test_all_custom_exceptions_inherit_base(exc_cls: type[Exception]) -> None:
    assert issubclass(exc_cls, SkillEvaluateError)
