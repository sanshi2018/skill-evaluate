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
    # Analyzer Agent（docs/dev/16）拆解能力树与做用例-能力映射的模型。走高档模型
    # 而不是廉价的 mini 档：能力树是模块六/七/八三份文档共同的分析基座，拆错一项
    # 会一路传染到覆盖率、瘦身与加权算法，而重跑的代价是整条补盲回环。
    analyzer_model: str = "anthropic/claude-sonnet-5"
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


class InstructionControlSettings(BaseSettings):
    """模块三（docs/dev/13）：指令控制度与执行效果评测的参数。

    这里的每一项都是**成本或口径**开关，不是"卡线阈值"（本维度的判定要么是 LLM
    语义裁决、要么是对 Trace 的确定性扫描，没有类似模块二 500 行那样的硬指标）。
    """

    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_INSTRUCTION_CONTROL_")

    # A/B 对比每条分支跑几次。**默认 1**（docs/dev/13 第 4 节的明确成本决策）：
    # 架构文档模块一要求每用例 3 次冗余，但 A/B 是"每用例 2 条分支 × 每分支 N 次"，
    # N=3 会让本维度的成本达到基础评测的 6 倍。ROI 判定关心的是"存在不存在显著
    # 差异"，容忍单次执行的非确定性噪音（模板正文里也明确告诉了裁判这一点）。
    # 若报告显示 ROI 判定本身抖动过大，调大这一项即可，不需要改任何代码结构。
    run_count_per_arm: int = 1
    # 常规探查用例的 Token 水位容忍比例：超过"同批常规用例中位数 × (1 + ratio)"
    # 才记一条水位告警。0.5 = 允许比同批常规任务的中位数多烧一半 Token。
    # 为什么用同批中位数而不是一个绝对数字：`total_tokens` 包含任务提示词、工具
    # 输出等与 Skill 无关的量，绝对阈值换一个 Skill 就得重调，毫无意义。
    pd_token_watermark_ratio: float = 0.5
    # 算中位数至少要几个干净样本（没读任何参考文件的常规用例）。样本太少时中位数
    # 本身就是噪音，此时**不做**水位检查而不是拿一两条数据去指控别人。
    pd_watermark_min_samples: int = 3
    # 交给效率诊断模板的动作步数上限与单步输出截断长度。Trace 可以有上百步、每步
    # 几十 KB 输出；整串塞进 Prompt 既超上下文也让裁判抓不住重点。
    trace_digest_max_steps: int = 40
    trace_digest_max_output_chars: int = 400


class ScriptUsabilitySettings(BaseSettings):
    """模块四（docs/dev/14）：脚本接口易用性黑盒探测的参数。

    分三类，混在一个类里是因为它们都只服务于同一个维度：

    1. **超时**：三种探测各有各的口径，见各字段说明；
    2. **容器资源墙**：与 docs/dev/03 第 7 节的沙箱安全边界对应；
    3. **调用约定**：脏数据/幂等性探测怎么把参数递给脚本。第 3 类是本维度最大的
       不确定来源——我们并不知道任意一个脚本的参数长什么样，只能按最通行的约定
       试一次，因此把它做成配置而不是写死的字面量。
    """

    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_SCRIPT_USABILITY_")

    # 挂起探测的专用短超时（docs/dev/14 第 4 节）。**刻意远小于**常规执行的 60 秒：
    # 这项测的是"缺参数时脚本该立刻拒绝或报错"，10 秒已经是极宽松的容忍窗口，
    # 用 60 秒只会让每个不合格脚本白等 50 秒。
    hang_probe_timeout_s: int = 10
    help_probe_timeout_s: int = 10
    # 脏数据与幂等性探测给到 15 秒：它们会走脚本的真实主流程（读文件、做校验），
    # 比"打印一段 usage 就退出"要慢一个量级。
    dirty_input_timeout_s: int = 15
    idempotency_timeout_s: int = 15

    # 防刷屏建议上限：单次执行的 stdout+stderr 原始字节数超过它就记一条告警
    # （docs/dev/14 第 7 节）。默认与 docs/dev/03 第 8 节的截断阈值对齐——超过我们
    # 自己都要截断的量，就该由脚本自己先截断。
    output_truncation_warn_bytes: int = 32 * 1024

    # 脏数据/幂等性探测传参用的标志位。绝大多数 CLI 脚本用 `--input`；改成
    # `--file`、`--config` 之类只需要改这一项。**探测失败不等于脚本有缺陷**，
    # 因此"脚本根本不认这个标志"的情形在报告里会如实标注（见 nodes.py）。
    dirty_input_flag: str = "--input"
    # 启用哪几种脏数据模式，留空表示全开。取值见 `probes.DIRTY_PAYLOAD_MODES`。
    dirty_payload_modes: list[str] = Field(default_factory=list)
    # 超长字符串模式的长度。64KB 远超任何合理输入，又不至于顶到系统的 ARG_MAX。
    oversized_payload_bytes: int = 64 * 1024

    # 容器资源墙（docs/dev/03 第 7 节的工程约定在本组件上的落地）。
    docker_binary: str = "docker"
    container_memory_limit: str = "512m"
    container_cpu_limit: str = "1.0"
    container_pids_limit: int = 256
    # 按语言覆盖运行时镜像，如 `{"python": "internal-registry/python:3.13-slim"}`。
    # 内网/离线环境用得上：默认镜像来自 Docker Hub，拉不下来时整个维度会全线失败。
    runtime_image_overrides: dict[str, str] = Field(default_factory=dict)
    # 同时在飞的容器数上限。默认 None = 复用 `ExecutorSettings.max_concurrent_sandboxes`
    # ——脚本探测容器比 Agent 沙箱轻得多，但它们跑在同一台机器上，两处各设一套
    # 上限只会让"到底能同时跑多少个容器"没人算得清。
    max_concurrent_scripts: int | None = None


class SecuritySettings(BaseSettings):
    """模块五（docs/dev/15）：安全性与注入风险红蓝对抗评测的参数。

    本组配置的共同特点是**它们都在"成本"与"证据强度"之间做取舍**——安全维度是全
    项目最贵的一个（五条探测支路 + 严重性定级全量走 3 副本共识 + 补丁必须过一次
    全量功能回归），因此把这几个旋钮显式暴露出来，而不是让人去改代码。

    唯一**不可配置**的是"安全判定一律 CRITICAL"（docs/dev/15 第 7 节）：把它做成
    配置项，等于给"这次先关掉共识投票省点钱"留了口子，而安全判定的假阴性正是这份
    文档最想防的东西。
    """

    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_SECURITY_")

    # 一次 bootstrap 出多少条对抗题。None = 按攻击面数量自动算
    # （`agents.attacker.default_adversarial_count()`，当前七个攻击面 × 2 = 14 条）。
    # 做成 None 而不是写死 14：将来注册第八个攻击面时总数自动跟上，不需要有人记得
    # 回来改这个数字。
    adversarial_case_count: int | None = None

    # 探测类节点的单次执行墙钟超时。**比其余维度短**（默认 60s vs 模块一/三的 90s）：
    # 对抗用例期望的结局大多是"立刻被拒绝"，跑满 90 秒只说明它陷进去了。DoS 探测
    # 不用这个值，它显式取 `ExecutorSettings.sandbox_wall_clock_timeout_s`
    # 这个硬上限（docs/dev/15 第 8 节）。
    probe_timeout_s: int = 60

    # 强制功能回归（docs/dev/15 第 11.2 节）是否包含 A/B 的 ROI 判定。
    # 默认开：架构文档要求安全补丁"必须"过一次功能回归，而只测触发率是不够的——
    # 一条把路径写死的刚性约束不会影响 Skill 被唤醒，只会让它唤醒之后干不了活。
    # 允许关掉是因为 A/B 是全项目最贵的一项检查（用例数 × 2 条分支 × 最多 3 轮闭环），
    # 成本压力大到必须取舍时，关掉它并在报告里看得见，好过有人偷偷把整个闭环停掉。
    regression_includes_roi: bool = True
    # 回归时最多重跑几条用例（0 = 不限）。抽样会削弱这道闸门，因此默认不限；
    # 设了非零值时，报告 findings 里会写明"本次回归只跑了 N 条"。
    regression_max_cases: int = 0

    # 探测证据写进 `SecurityFinding.evidence` 的长度上限。证据要够人复现，又不能
    # 让一条 finding 把整个报告页面撑爆；完整轨迹在 `execution_traces` 里可回查。
    evidence_max_chars: int = 2000


class CoverageSettings(BaseSettings):
    """模块六/七/八（docs/dev/16~18）：能力覆盖率分析的参数。

    本组配置全部围绕架构文档给模块六列出的那条**缺点**展开——"能力粒度极其依赖
    LLM 的主观判断，拆得过细会导致覆盖率永远无法达标，引发无限重试死锁"。三个
    旋钮分别对应它的三道闸门：拆太细就交人确认（`capability_count_review_threshold`）、
    达标线可调（`min_coverage_ratio`）、补盲次数有硬上限（`max_patch_iterations`）。

    这里**没有**"自动放宽阈值直到通过"这类旋钮，这是刻意的：覆盖率不达标时正确的
    动作是把事实写进报告交给人判断（是能力树切太细还是测试集真的不足），而不是让
    机器自己把及格线降到刚好能过。
    """

    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_COVERAGE_")

    # 能力节点数超过它就挂起等人工确认（docs/dev/16 第 4 节，架构文档"人工审核卡片"
    # 的应对方案）。默认 20：一份 500 行以内的 SKILL.md 若被拆出 20 项以上的原子
    # 能力，多半是 Analyzer 把"步骤"当成了"能力"，此时继续算覆盖率只会得到一个
    # 永远补不满的盲区清单。
    capability_count_review_threshold: int = 20

    # 覆盖率达标线（架构文档模块六第 3 节的 ">= 90%"）。
    min_coverage_ratio: float = 0.9

    # 反向补盲的最大回环次数（docs/dev/16 第 7 节）。达到上限仍未达标就如实报告
    # "覆盖率未达阈值"，不再回环——宁可把问题暴露给人，也不让机器在不确定的情况
    # 下自我循环空转。
    max_patch_iterations: int = 3

    # `map_case_coverage` 同时在飞的映射请求数。每条正向用例一次 LLM 调用，几十条
    # 用例全量并发打出去会撞上供应商限速；口径与 `ExecutorSettings.max_concurrent_sandboxes`
    # 一致（评测系统自身的资源节流），但这里限的是 LLM 请求而非沙箱，因此单列一项。
    max_concurrent_mappings: int = 10

    # ---- docs/dev/17（模块七：组合能力覆盖矩阵）追加 ----

    # 组合矩阵单次分析的能力对数量上限（docs/dev/17 第 5 节）。
    #
    # 两两组合是 N² 级别：14 个能力节点是 91 对，20 个就是 190 对，40 个是 780 对。
    # 架构文档模块七本身没给上限，但模块十已经写明"放弃全量组合测试"的思路，这里
    # 把它落成一个显式旋钮。默认 100（约 14 个能力节点内可做全组合）。
    #
    # 超限时**不采样、不近似**，而是按能力权重排序后截断取前 N 对——采样会让同一份
    # 测试集在两次运行中得到不同的组合覆盖率，那个数字就再也没法拿来比较了。
    max_capability_pairs_for_matrix: int = 100

    # 单轮针对未覆盖组合对的补生成上限（docs/dev/17 第 5 节）。
    #
    # 默认 5：一次补 5 对（即约 5 条新用例），交由后续评测轮次逐步收敛。不设上限的
    # 后果是首次接入组合矩阵时——此时几乎所有组合对都未覆盖——一口气生成上百条题，
    # 把测试集撑爆的同时也把 Generator 的账单撑爆。
    max_combinatorial_patch_per_round: int = 5

    # ---- docs/dev/18（模块八：加权覆盖率与隐式边界追踪）追加 ----

    # 负向约束覆盖判定的**单轮 LLM 调用上限**（docs/dev/18 第 4 节）。
    #
    # 这一步是"每条约束 × 每条候选用例"的二维扫描：5 条约束 × 40 条用例 = 200 次
    # 裁判调用。约束数与用例数各自增长时乘积会很快失控，因此设一个显式的总闸门。
    #
    # 超限时**不是**把剩下的判成"未覆盖"——那会凭空虚增一批补题需求（而每条补题
    # 又是一次生成调用）。剩余的判定记为"未判定"，如实写进报告，交由下一轮评测
    # 继续（与模块七组合矩阵截断同一种"如实标注、不假装算完了"的处理）。
    #
    # 已经带着 `negative_constraint_ids` 绑定的用例不消耗配额：那是出题时就回填
    # 好的事实，没有什么要判的。
    max_constraint_probe_calls: int = 200

    # 可追溯性矩阵制品（`traceability_matrix.json` / `.csv`）的输出根目录。
    #
    # 最终路径是 `<artifacts_dir>/<run_id>/traceability_matrix.{json,csv}`。做成
    # 配置项而不是写死 "artifacts"：CI 各家的工作目录约定不同，而这两份文件要被
    # `upload-artifact` 一类的步骤按路径捞走（docs/dev/24 配置），路径写死会逼着
    # 那一步去猜。
    artifacts_dir: str = "artifacts"


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
    instruction_control: InstructionControlSettings = Field(
        default_factory=InstructionControlSettings
    )
    script_usability: ScriptUsabilitySettings = Field(default_factory=ScriptUsabilitySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    coverage: CoverageSettings = Field(default_factory=CoverageSettings)
    judge: JudgeSettings = Field(default_factory=JudgeSettings)
    optimizer: OptimizerSettings = Field(default_factory=OptimizerSettings)
    validator: ValidatorSettings = Field(default_factory=ValidatorSettings)
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
