"""全局配置系统。

统一用 `pydantic-settings`，来源优先级：环境变量 > `.env` 文件 > 代码默认值。
所有后续模块的可配置项都在此文件中新增字段，禁止在业务代码里散落
`os.environ.get(...)`（见 docs/dev/01_项目脚手架与技术栈基线.md 第 4 节）。

向后兼容约定：新增字段以 `class XxxSettings(BaseSettings)` 追加并挂到 `Settings`
上，禁止修改已存在字段的类型/默认值语义。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field, SecretStr
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
    """LLM 访问配置。

    全项目统一走 **OpenRouter**（`https://openrouter.ai/api/v1`，OpenAI 兼容协议），
    由 `langchain_openai.ChatOpenAI` 承载调用。换模型 = 改这里的 `*_model` 字段，
    不需要改代码、也不需要再引入第二个厂商 SDK（见
    docs/dev/interfaces/06_llm_client_and_sampling.md）。

    模型 ID 用 OpenRouter 的 `<厂商>/<模型>` 命名（例如 `anthropic/claude-sonnet-5`、
    `openai/gpt-5.6-terra`、`google/gemini-3.5-flash`），**不是**各家原生 SDK 的 ID。
    """

    # populate_by_name：`api_key` 用了 validation_alias（见下），不开这个开关就只
    # 能用别名做关键字参数构造，代码/测试里 `LLMSettings(api_key=...)` 会报错。
    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_LLM_", populate_by_name=True)

    provider: str = "openrouter"
    judge_model: str = "anthropic/claude-sonnet-5"  # Judge Agent 默认模型（08 会细化多副本策略）
    # Mini Agent 走更便宜的模型。注意 OpenRouter 的版本号用点号（`claude-haiku-4.5`），
    # 与 Anthropic 原生 API 的短横线写法（`claude-haiku-4-5`）不同，写错会 404。
    mini_agent_model: str = "anthropic/claude-haiku-4.5"
    generator_model: str = "anthropic/claude-sonnet-5"  # Generator Agent（docs/dev/06）出题模型
    # OpenRouter 的 Key（`sk-or-v1-...`）。同时接受裸 `OPENROUTER_API_KEY`，方便与
    # 其他工具共用同一个环境变量。
    api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("SKILLEVAL_LLM_API_KEY", "OPENROUTER_API_KEY"),
    )
    base_url: str = "https://openrouter.ai/api/v1"  # 自建网关/代理时覆盖
    # OpenRouter 的可选归因头：填了会出现在 openrouter.ai 的用量排行里，不影响功能。
    http_referer: str | None = None
    app_title: str | None = None
    max_output_tokens: int = 16000  # 非流式请求的默认上限，避免截断（见 docs/dev/06 第 10 节）
    request_timeout_s: float = 600.0
    max_structured_retries: int = 2  # 结构化解析失败的重试次数（docs/dev/06 第 5.3 节要求 2 次）


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
