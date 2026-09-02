"""全局配置系统。

统一用 `pydantic-settings`，来源优先级：环境变量 > `.env` 文件 > 代码默认值。
所有后续模块的可配置项都在此文件中新增字段，禁止在业务代码里散落
`os.environ.get(...)`（见 docs/dev/01_项目脚手架与技术栈基线.md 第 4 节）。

向后兼容约定：新增字段以 `class XxxSettings(BaseSettings)` 追加并挂到 `Settings`
上，禁止修改已存在字段的类型/默认值语义。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_DB_")

    host: str = "localhost"
    port: int = 5432
    user: str = "skill_evaluate"
    password: SecretStr = SecretStr("")
    database: str = "skill_evaluate"

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @property
    def async_dsn(self) -> str:
        """SQLAlchemy async engine 使用的 DSN（psycopg 异步驱动，见 docs/dev/04 第 4 节）。"""
        return (
            f"postgresql+psycopg://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.database}"
        )


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_LLM_")

    provider: str = "anthropic"
    judge_model: str = "claude-sonnet-5"  # Judge Agent 默认模型（08 会细化多副本策略）
    mini_agent_model: str = "claude-haiku-4-5-20251001"  # Mini Agent 走更便宜的模型
    api_key: SecretStr = SecretStr("")


class ExecutorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_EXECUTOR_")

    backend: str = "mini"  # "mini" | "hermes" | <其他可插拔后端名>，见 docs/dev/03
    hermes_endpoint: str | None = None
    hermes_hook_secret: SecretStr = SecretStr("")
    sandbox_wall_clock_timeout_s: int = 60  # 模块五：单沙箱存活硬上限
    outbound_network_allowlist: list[str] = Field(default_factory=list)


class LangfuseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_LANGFUSE_")

    enabled: bool = False
    public_key: str | None = None
    secret_key: SecretStr = SecretStr("")
    host: str = "https://cloud.langfuse.com"


class ApiSettings(BaseSettings):
    """Hook/审批回调 API 层配置（见 docs/dev/05 第 2 节）。"""

    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_API_")

    host: str = "0.0.0.0"
    port: int = 8000
    internal_base_url: str = "http://localhost:8000"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    env: str = "local"  # local | ci | prod
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    executor: ExecutorSettings = Field(default_factory=ExecutorSettings)
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
