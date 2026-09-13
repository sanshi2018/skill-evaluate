# 06 Generator Agent 与测试集生命周期管理

> 状态：**待确认**
> 路线图位置：第 1 层 / 第 1 份（跨维度复用的公共智能体）
> 依赖：`02`（`TestCase`/`TestSuiteVersion` 模型）、`03`（`MiniAgentBackend` 用于生成过程本身的 LLM 调用）、`04`（`TestSuiteRepository`）、`05`（Langfuse 打点挂载点）
> 被依赖：`11`（模块一，消费正向/反向用例）、`16/17`（模块六/七，反向驱动定向补生成）、`19`（模块九，验证集抽样）、`20`（模块十，多技能用例）、`21`（真实种子锚点扩散，会扩展本文档的种子来源）

---

## 1. 本文档目标

落实你的第一条关键约束：**Generator 节点只在初始化时生成一次，之后只能"手动强制生成"，流水线重复执行默认复用已有测试集**。同时把 Generator 设计为**可被多个后续模块复用的通用能力**，而不是模块一专属——模块六/七的"覆盖率反向驱动补盲区"、模块十的"多技能组合用例"，最终都调用同一个 Generator，只是传入不同的生成指令。

## 2. Generator Agent 的职责边界

Generator Agent **只负责产出 `TestCase` 列表**，不负责执行、不负责判定。它的输入是"生成指令"（Skill 定义 + 生成模式 + 可选的定向约束），输出是符合文档 02 `TestCase` schema 的结构化用例集合。这个边界很重要：模块六/七/九/十都是"生成指令的不同来源"，不是"另起一个生成器"。

## 3. `GenerationRequest`：统一生成指令

```python
# src/skill_evaluate/agents/generator/schema.py
from skill_evaluate.state.enums import GenerationMode, TestCaseCategory


class CapabilityFocus(BaseModel):
    """模块六/七反向驱动补盲区时传入，指定必须命中的能力/负向约束"""
    capability_ids: list[str] = Field(default_factory=list)
    negative_constraint_ids: list[str] = Field(default_factory=list)
    combinatorial_pairs: list[tuple[str, str]] = Field(default_factory=list)  # 模块七组合矩阵


class GenerationRequest(BaseModel):
    skill: SkillDefinition
    mode: GenerationMode
    categories: list[TestCaseCategory] = Field(
        default_factory=lambda: [TestCaseCategory.POSITIVE, TestCaseCategory.NEGATIVE]
    )
    positive_count: int = 9         # 架构文档建议 8-10
    negative_count: int = 9
    capability_focus: CapabilityFocus | None = None   # None = 常规发散生成；非 None = 定向补盲区
    seed_anchor_ids: list[str] | None = None            # 21 文档扩展：真实种子锚点，本文档先留接口
    triggered_by: str                                     # "manual_cli" | "coverage_gap" | "cross_model_sampling" | ...
```

`triggered_by` 字段是纯审计用途（记录这一批用例是谁、为什么触发生成的），不影响生成逻辑本身，供后续排查"为什么测试集突然变了"时追溯。

## 4. 三态生成模式的具体行为

### 4.1 `REUSE`（默认）

```python
# src/skill_evaluate/agents/generator/service.py
async def ensure_test_suite(skill: SkillDefinition) -> TestSuiteVersion:
    existing = await test_suite_repo.get_active_version(skill.skill_id, skill.version_ref)
    if existing is not None:
        return existing   # 命中：直接复用，不调用任何 LLM

    stale = await test_suite_repo.get_active_version(skill.skill_id, skill_version_ref=None)  # 忽略版本号查询是否存在旧版本
    if stale is not None:
        logger.warning("test_suite_stale", skill_id=skill.skill_id,
                        active_version_ref=stale.skill_version_ref, current_ref=skill.version_ref)
        # 关键设计：SKILL.md 变了但没有人手动触发 force_regenerate，不代表要自动重新生成。
        # 自动重新生成会打破"手动强制"的约束（哪怕理由是版本变了）。
        # 这里的策略是：继续复用旧版本用例集，但在报告中标记 staleness 告警，
        # 交给人类判断"这次 SKILL.md 的改动是否大到需要重新出题"。
        return stale

    # 真正的首次初始化：走 4.2 的生成流程
    return await _generate_and_activate(GenerationRequest(skill=skill, mode=GenerationMode.REUSE,
                                                             triggered_by="auto_bootstrap"))
```

**关键设计取舍**：`REUSE` 模式下，即使检测到 `skill_version_ref` 不匹配（SKILL.md 已经改过），也**不自动重新生成**，只告警。这是对你原话"后续再想生成，需要手动强制生成"的严格执行——版本漂移本身不构成自动触发生成的理由，只有显式的 `force_regenerate` 或 `incremental_patch` 请求才会调用 LLM。这个决策会在报告中体现为 `staleness_warning` 字段，供人类判断是否要手动触发。

### 4.2 `FORCE_REGENERATE`

```python
async def force_regenerate(skill: SkillDefinition, triggered_by: str = "manual_cli") -> TestSuiteVersion:
    request = GenerationRequest(skill=skill, mode=GenerationMode.FORCE_REGENERATE, triggered_by=triggered_by)
    return await _generate_and_activate(request)
```

全量重新生成，旧版本不删除（保留历史，`is_active` 置为 `False`，供审计/回溯对比新旧用例集差异），新版本 `activate_new_version()`（文档 04 Repository 方法，事务内切换 active 标志）。

CLI 暴露：`skill-evaluate generate --skill-path <path> --force`（补齐文档 01 的 CLI 骨架，本文档正式实现该子命令）。CI 默认调用路径**不带** `--force`，需要人为在 CI 触发参数或本地命令中显式加上，从物理层面防止"CI 每次跑都重新生成"这种误用。

### 4.3 `INCREMENTAL_PATCH`

```python
async def incremental_patch(skill: SkillDefinition, focus: CapabilityFocus, triggered_by: str) -> TestSuiteVersion:
    """
    不是全量重来，而是：
    1. 取当前 active 版本的全部 case_ids
    2. 针对 focus 中的盲区，定向生成新增用例（数量按 focus 内容动态决定，不固定 8-10）
    3. 新老 case_ids 合并成新版本，activate_new_version()
    这是模块六/七"反向驱动"、模块十"组合矩阵补盲区"的统一入口，
    调用方只需要构造正确的 CapabilityFocus，不需要理解 Generator 内部生成细节。
    """
```

`INCREMENTAL_PATCH` 不受"手动强制"约束限制——它本身就是显式触发的、有明确理由（覆盖率盲区）的生成请求，与"CI 重复跑自动重新生成"是两回事，因此允许被模块六/七/十的节点在流水线内部自动调用，不需要人工在 CLI 敲 `--force`。这个语义差异是本文档对三态设计的核心区分点，务必在后续模块文档中保持一致理解。

## 5. 用例生成 Prompt 设计

> **实现期修订（由 docs/dev/13 第 3.1 节正式提出并落地）**：本节原先只描述了
> 正向/反向两套模板，实现上对应 `agent.py` 里一个硬编码的
> `_TEMPLATE_BY_CATEGORY` 字典。该字典已改为**类别 → 模板注册表**
> （`agents/generator/prompts/registry.py`），与 docs/dev/07 的 `ReviewTemplate`
> 注册机制同构：
>
> ```python
> from skill_evaluate.agents.generator.prompts.registry import register_generation_template
>
> register_generation_template(TestCaseCategory.ADVERSARIAL, "adversarial.jinja")
> ```
>
> 新增一个用例类别 = 新增一个 `.jinja` + 一次注册，**不再需要改动 `agent.py`**。
> 内置注册四条：`positive` / `negative`（本文档）与 `progressive_disclosure_trigger` /
> `progressive_disclosure_regular`（docs/dev/13 的渐进式披露动态探查用例，见 5.4）。
> 同时 `GenerationRequest` 追加了 `category_counts`，让调用方能在不碰
> `positive_count` / `negative_count` 语义的前提下给新类别定量。

### 5.1 正向触发用例（Should-trigger）

Prompt 模板（`agents/generator/prompts/positive.jinja`）核心要求，直接映射架构文档第 1 节：

- 8-10 个（或 `positive_count` 指定数量）多样化用户提示词。
- 强制注入多样性表述：口语化、错别字、隐式表达（不提技能名，只提业务需求）、冗长文件路径或多步骤复杂上下文——这四类多样性以 few-shot 示例的形式固化在 Prompt 模板里，而不是靠自然语言描述让模型"随便发挥"，保证生成稳定性。
- 若 `capability_focus` 非空，额外注入约束："以下能力必须被至少一条用例覆盖：{capability_focus.capability_ids 对应的描述文本}"。

### 5.2 反向触发用例（Should-not-trigger / Near-misses）

Prompt 模板（`prompts/negative.jinja`）：

- 8-10 个"近脱靶"用例，要求包含与当前 Skill 强相关的共享关键词，但指向完全不同的任务逻辑。
- 生成时会把 Skill 的 `description` 抽取关键词后，要求模型"围绕这些关键词但故意跑题"，保证反向用例的"近似度"是可控的，而不是随机生成一堆完全无关的句子（那样测不出防误触发能力）。

### 5.4 渐进式披露动态探查用例（由 docs/dev/13 追加）

两个新类别，成对存在，判定方向相反：

- `progressive_disclosure_trigger`（`prompts/pd_trigger.jinja`）：场景**精确命中**某个
  `references/` 文件声明的加载条件，期望执行时观测到 Agent 读了它。**每个带触发条件
  的参考文件各出一条**，并由模型回填 `probe_target_reference`（落到 `TestCase` 的同名
  字段，见 docs/dev/02 的模型追加与 Alembic 迁移 `0006`）。
- `progressive_disclosure_regular`（`prompts/pd_regular.jinja`）：完全落在正文范围内、
  **不该**触发任何额外文件读取的常规任务，作为前者的对照组。

只出一半会让评测失效：只有触发探查题时，一份"把参考文件全读一遍"的 Skill 会满分
通过；只有常规题时，一份"从不读参考文件"的 Skill 会满分通过。

### 5.3 输出解析与校验

Generator LLM 调用走文档 03 的 `MiniAgentBackend`（生成任务本身是纯文本任务，不需要沙箱），要求模型输出严格 JSON（`response_format` 强约束 + Pydantic 校验，校验失败自动重试最多 2 次，仍失败则该批次生成判定为 `GenerationFailure`，整体流程失败，不产出半成品用例集）。

## 6. 数据集划分

生成完毕后，`_generate_and_activate()` 内部按 60/40 自动划分 `DatasetSplit.TRAIN` / `DatasetSplit.VALIDATION`：

```python
def _split_dataset(cases: list[TestCase]) -> list[TestCase]:
    shuffled = list(cases)
    random.Random(seed=hash(skill.skill_id) & 0xFFFFFFFF).shuffle(shuffled)  # 确定性 shuffle，同一 skill 可复现
    train_count = round(len(shuffled) * 0.6)
    for i, case in enumerate(shuffled):
        case.split = DatasetSplit.TRAIN if i < train_count else DatasetSplit.VALIDATION
    return shuffled
```

**验证集不参与优化闭环**是模块一的强约束（防止过拟合），本文档只负责打好 `split` 标签，"训练集失败才路由给 Optimizer"的逻辑属于文档 09（Optimizer）与文档 11（模块一流水线）的职责，本文档不做假设、不做过滤。

`INCREMENTAL_PATCH` 新增的用例同样参与 60/40 划分（对新增的这一小批独立计算，不打乱已有用例的 split 归属，避免覆盖率补盲区导致已经跑过优化闭环的训练/验证集边界发生变化）。

## 7. 反坍塌与真实分布对齐的接口预留（对应模块十一子节点二，实现推迟到文档 21）

架构文档"考卷的含金量"一节要求：语义信息熵监控（防止生成坍塌）+ 真实种子锚点扩散。这两项能力依赖 pgvector 向量检索基础设施，而向量检索的完整方案在文档 23（长时记忆与数据飞轮）才落地，因此本文档：

- 在 `GenerationRequest` 中预留 `seed_anchor_ids` 字段（第 3 节已定义），当前实现中若该字段非空，Generator 只是把对应种子文本作为 few-shot 示例注入 Prompt（简化版），**不做**向量空间坍塌检测。
- 在 `_generate_and_activate()` 内部预留一个空实现的校验钩子：

```python
async def _check_generation_collapse(new_cases: list[TestCase]) -> bool:
    """占位实现：恒定返回 True（放行）。
    文档 21 接入后：计算 new_cases 的 prompt 向量与历史用例库的分布距离，
    低于阈值判定坍塌，返回 False 并阻断本次生成结果的 activate。"""
    return True
```

这个占位钩子保证文档 21 接入时只需替换函数体，不需要改动 `_generate_and_activate()` 的调用结构。

> ⚠️ **文档 21 已实现，以代码为准**：占位函数已删除，改为 `TestSuiteService(collapse_detector=...)` 注入的
> `assess()` / `persist()` 两步检测器（向量须在用例落库后写、被拒批次不写，一个 bool 函数表达不了）；
> 不依赖文档 23，最小向量基础设施由 21 自建。`seed_anchor_ids` 的"id 当文本"简化语义已废弃。
> 详见 `docs/dev/interfaces/21_generator_trust_and_preflight.md` 第 1、2 节。

## 8. 与 Judge/Optimizer 闭环的接口预留

模块一的完整闭环（生成 → 执行 → 判定 → 失败重写 description → 再执行）依赖文档 08（Judge）、文档 09（Optimizer）、文档 11（模块一流水线）。本文档只保证 Generator 侧提供的接口能被该闭环正确调用：

- `incremental_patch` 不仅服务覆盖率盲区，也服务"Optimizer 重写 description 后需要在训练集上重新验证"的场景——但那种场景不需要生成新用例，只需要重跑现有 `TestCase`，因此不经过 Generator，直接由文档 11 调用 Executor。本文档特别澄清这一点，避免后续文档误以为"重新验证"也要走 Generator。

## 9. 待接入文档（本文档留给后续模块的接口清单）

| 预留位置 | 当前状态 | 由哪份文档接入 | 接入方式 |
|---|---|---|---|
| `_check_generation_collapse()` | ✅ `21` 已实现（改为注入式 `CollapseDetector`，占位函数已删除） | `21` | 见 `interfaces/21` 第 1 节 |
| `GenerationRequest.seed_anchor_ids` | ✅ `21` 已实现（Git 托管种子库 + embedding 检索 + `TestCase.seed_anchor_id` 溯源） | `21` | 见 `interfaces/21` 第 2 节 |
| `CapabilityFocus` 的实际构造方 | 模型已定义，无生产者 | `16/17`（模块六/七）、`20`（模块十组合矩阵） | 各文档在检测到盲区后，构造 `CapabilityFocus` 并调用 `incremental_patch()` |
| `triggered_by="cross_model_sampling"` 的调用方 | 字符串占位，未实际使用；**`19` 实现时决定不启用** | — | 模块九是非阻断维度，不应拥有改变用例集的权力；验证集为空时如实报告 NEEDS_HUMAN_REVIEW（见 `docs/dev/19` 第 3.2 节）。该取值保留供将来需要时使用 |
| Langfuse 打点挂载 | 未挂载 | 本文档遗留给自身内部的 `_call_llm()` 封装 | 见第 10 节说明 |

## 10. Agent 基类与 Langfuse 钩子的落位说明

为避免文档 06~10（五个 Agent）各自重复实现"调用 LLM + 打点 + 重试"的样板代码，本文档引入一个共享基类，后续 Agent 文档直接继承：

```python
# src/skill_evaluate/agents/base.py
class BaseLLMAgent(ABC):
    def __init__(self, model: str, temperature: float, langfuse_adapter: LangfuseAdapter): ...

    async def _call_llm(self, prompt: str, response_schema: type[BaseModel] | None = None) -> Any:
        """统一封装：调用 LLM API -> 记录 TimingCostMetrics -> log_agent_call() 打点 Langfuse
        -> 若 response_schema 给定，做结构化解析 + 校验失败重试（tenacity，最多 2 次）"""
```

Generator Agent 本身继承 `BaseLLMAgent`，这是**本文档对文档 05 待接入项"Agent 基类挂载 Langfuse 钩子"的正式实现**；文档 07~10 的四个 Agent 直接复用此基类，不再各自声明。

---

## 下一步

待你确认本文档后，我将输出 **文档 07：Mini Agent 评审框架**——封装低温文本审查能力，供模块二、模块四、模块九等多处静态审查场景复用。
