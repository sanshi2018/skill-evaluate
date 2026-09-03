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
    # Optimizer Agent（docs/dev/09）写补丁的模型。补丁会进人工审查、可能被合进
    # 真实仓库，质量优先，因此默认与 judge_model 同档而不是走廉价的 mini 档。
    optimizer_model: str = "anthropic/claude-sonnet-5"
    # Validator Agent（docs/dev/10）生成校验脚本的模型。脚本的 exit_code 会被当作
    # 比 LLM 裁决更可靠的"确定性证据"，写错了比没有更糟，因此同样走高档模型。
    validator_model: str = "anthropic/claude-sonnet-5"
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
    # docs/dev/11 第 4 节新增（追加式扩展，不改动已有字段语义）：各维度节点同时
    # 发起的沙箱执行数量上限，由节点侧的 `asyncio.Semaphore` 落实。
    # 为什么需要它：模块一对每条用例做 3 次冗余执行，`asyncio.gather` 会把整个
    # 训练集的执行请求一次性打出去（20 条用例 = 60 个沙箱）。这是评测系统自身的
    # 资源节流（不是被测 Skill 的 DoS 场景），但采用与模块五同一套防御思路：
    # 并发有上限，而不是"看调度器扛不扛得住"。
    max_concurrent_sandboxes: int = 10


class ContextScopingSettings(BaseSettings):
    """模块二（docs/dev/12）：上下文利用率与范围界定静态评测的阈值。

    默认值取自架构文档明确给出的数字（500 行 / 5,000 Token），做成配置项是为了让
    团队按自身规范收紧或放宽，**不是**为了在 CI 里临时调大好让某次合并通过。

    为什么阈值在这里、而冗余执行次数在 `TriggerAccuracyDeps` 里是常量：500/5000
    是可以独立调整的口径（调了只是卡得松紧不同，判定语义不变），而模块一的"跑 3
    次 + 阈值 0.5"两个数字互相绑定，单独调一个会让判定语义悄悄变化。
    """

    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_CONTEXT_SCOPING_")

    line_limit: int = 500
    token_limit: int = 5000
    # Token 计数不精确时（没装 tiktoken，退化为"字符数 × 3/4"）的不确定带宽度。
    # 估算值落在 `token_limit × (1 ± ratio)` 区间内时，本维度**不阻断**而是判
    # NEEDS_HUMAN_REVIEW——拿一个 ±15% 的估算值去阻断别人的合并请求，是
    # docs/dev/interfaces/06 明确警告过的误判来源。精确计数时本项不生效。
    estimate_uncertainty_ratio: float = 0.15
    # 允许几个 references/ 文件缺少按需加载触发条件。默认 0：渐进式披露的意义
    # 就在于"条件明确"，缺一个就该有人看一眼。该判定是非阻断的 Warning
    # （docs/dev/12 第 6 节），所以零容忍不会误伤合并流程。
    max_reference_files_without_trigger: int = 0
    # 正文规模达到限额的这个比例、却一个参考文件都没有时，判定为"该做渐进式
    # 披露而没做"（架构文档模块二第 1 节的目录结构审查）。0.8 = 400 行 / 4000
    # Token 就该开始往 references/ 拆了，而不是等超标之后才发现。
    bulk_inline_ratio: float = 0.8


class JudgeSettings(BaseSettings):
    """Judge Agent 的可信度机制参数（docs/dev/08）。

    `consensus_strategy` 是 docs/dev/interfaces/06_llm_client_and_sampling.md 第 2 节
    留给本文档的定稿项：新一代 Claude 模型已移除采样参数，"3 副本温度扰动"在默认
    `judge_model` 上物理不成立。三个候选方案都做成了配置项而不是二选一写死——
    换 `judge_model` 时不该被迫改代码：

    - `perspective`（默认）：3 副本同模型同温度，但各自被指派一个不同的**审查
      视角**（证据充分性 / 反例存在性 / 判定一致性）。语义上更接近"三个不同的
      裁判"，而不是"同一个裁判掷三次骰子"，且不依赖任何采样能力。
    - `temperature`：字面意义的温度扰动，只在仍支持采样的模型上有意义。
    - `model`：跨模型共识，与 docs/dev/19 共享基础设施。
    """

    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_JUDGE_")

    golden_inject_rate: float = 0.02  # 架构文档"每 50 次真实评测混入 1 次"
    miss_rate_threshold: float = 0.05  # 失误率超过 5% 触发告警并冻结
    health_window_size: int = 50  # 失误率滑动窗口大小
    consensus_strategy: str = "perspective"  # perspective | temperature | model
    consensus_temperatures: list[float] = Field(default_factory=lambda: [0.1, 0.3, 0.5])
    consensus_models: list[str] = Field(default_factory=list)  # 空表示回落到 judge_model
    # 共识副本走 Mini Agent 通道（成本）还是 judge_model（能力）。默认 True：
    # 复用 docs/dev/07 的模板体系本来就是 Mini Agent 的职责，Judge 只负责投票。
    consensus_uses_mini_model: bool = True


class OptimizerSettings(BaseSettings):
    """Optimizer 闭环重试参数（docs/dev/09 第 5 节）。"""

    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_OPTIMIZER_")

    max_retries: int = 3  # 达到后 suspend_and_wait，而不是判负
    temperature: float = 0.2  # 补丁生成偏保守；模型不支持采样时该值不会被下发


class ValidatorSettings(BaseSettings):
    """Validator Agent 与 Git 断言工具箱参数（docs/dev/10 第 3.3、6 节）。

    `toolbox_repo_url` 指向**外部独立仓库** `skill-evaluate-assertion-toolbox`
    （不是本项目仓库）。未配置时工具箱视为不可用：`plan_assertion()` 会跳过
    `template_lookup` / `template_inherit`，直接走 `generated_from_scratch`，
    而不是报错——工具箱是加速与规范化手段，不是运行前置条件。
    """

    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_VALIDATOR_")

    toolbox_repo_url: str | None = None
    # 锁定到具体 ref（分支名或 commit sha）。同一次评测运行中断点恢复后引用的
    # 模板版本不允许漂移，因此实际命中的 commit sha 会写进 AssertionSpec.template_ref。
    toolbox_ref: str = "main"
    toolbox_cache_dir: str = "~/.cache/skill-evaluate/assertion-toolbox"
    # 关键词匹配命中阈值（0~1，模板 keywords 的命中比例）。低于该值判定为未命中，
    # 转 generated_from_scratch。
    template_match_threshold: float = 0.34
    max_script_repair_retries: int = 2  # 静态语法检查失败后的重试次数（docs/dev/10 第 6 节）
    # 校验脚本在沙箱内的落盘目录。沙箱侧按 AssertionSpec.script_path 写文件并执行。
    sandbox_script_dir: str = "/tmp/skill-evaluate/assertions"
    assertion_timeout_s: int = 30  # 单条断言脚本在沙箱内的执行超时，供沙箱客户端下发


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
    context_scoping: ContextScopingSettings = Field(default_factory=ContextScopingSettings)
    judge: JudgeSettings = Field(default_factory=JudgeSettings)
    optimizer: OptimizerSettings = Field(default_factory=OptimizerSettings)
    validator: ValidatorSettings = Field(default_factory=ValidatorSettings)
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
