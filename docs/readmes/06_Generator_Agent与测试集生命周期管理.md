# 实现说明：06 Generator Agent 与测试集生命周期管理

> 对应设计文档：`docs/dev/06_Generator_Agent与测试集生命周期管理.md`
> 状态：已实现（反坍塌校验与真实种子锚点库按设计留桩，待 `docs/dev/21` 接入）

本文档同时记录了 06 与 07 共用的 **Agent 底座**（`BaseLLMAgent` / `agents/llm.py`），
因为设计文档 06 第 10 节明确把这层的落位归属于本文档。

---

## 1. 交付了什么

### 1.1 Agent 公共底座（设计文档第 10 节）

| 文件 | 内容 |
|---|---|
| `src/skill_evaluate/agents/llm.py` | `AgentLLMClient` 协议、`OpenRouterLLMClient` 真实实现（`langchain_openai.ChatOpenAI` -> OpenRouter）、模型能力门禁 `model_supports_sampling()` / `normalize_model_id()`、Pydantic→json_schema 转换 `build_response_schema()`、`parse_structured_response()` |
| `src/skill_evaluate/agents/base.py` | `BaseLLMAgent`：统一封装"调用 LLM → 计量 `TimingCostMetrics` → Langfuse 打点 → 结构化解析 + 校验失败重试" |
| `src/skill_evaluate/agents/templating.py` | Agent Prompt 的 Jinja 环境构造（06/07 共用） |

### 1.2 Generator 本体

| 文件 | 内容 | 对应设计文档章节 |
|---|---|---|
| `agents/generator/schema.py` | `GenerationRequest`、`CapabilityFocus`、`GeneratedCase`/`GeneratedCaseBatch` | 第 3 节 |
| `agents/generator/prompts/_shared.jinja` | 被测 Skill 原文块、定向盲区约束块、种子锚点块、输出契约块（宏形式，供正/反向模板与后续新增模板复用） | 第 5 节 |
| `agents/generator/prompts/positive.jinja` | 正向触发用例（四类多样性表述 + few-shot + 反面示例） | 第 5.1 节 |
| `agents/generator/prompts/negative.jinja` | 反向近脱靶用例（关键词锚点 + 四种"擦边"手法） | 第 5.2 节 |
| `agents/generator/agent.py` | `GeneratorAgent`（继承 `BaseLLMAgent`）、`extract_keywords()` | 第 2、5 节 |
| `agents/generator/service.py` | `TestSuiteService`：三态生命周期、`_split_dataset()`、`_check_generation_collapse()` | 第 4、6、7 节 |

### 1.3 配套设施

| 文件 | 内容 |
|---|---|
| `src/skill_evaluate/ingestion/skill_loader.py` | `load_skill()`：磁盘路径 → `SkillDefinition`（CLI 的输入解析，见第 4 节） |
| `cli.py::generate` | `skill-evaluate generate --skill-path <path> [--force]` |
| `errors.py` | 新增 `AgentError` / `AgentResponseFormatError` / `GenerationError` / `ReviewTemplateError` |

---

## 2. 用什么方式实现了需求

### 2.1 「只生成一次，之后只能手动强制生成」——用三态区分"谁能触发"

设计文档最核心的约束不是"要不要缓存"，而是**谁有权触发一次出题**。实现上把
这件事显式建模成三态，而不是靠一个 `force: bool` 参数：

| 模式 | 谁能触发 | 是否调用 LLM |
|---|---|---|
| `REUSE` | 流水线默认路径 | 只在"从来没生成过"时调用一次 |
| `FORCE_REGENERATE` | 只有人（CLI `--force` / CI 显式参数） | 每次都调用 |
| `INCREMENTAL_PATCH` | 模块六/七/十的节点可在流水线内自动调用 | 每次都调用，但只补盲区 |

`INCREMENTAL_PATCH` 是设计文档特意区分出来的第三态：它有明确理由（覆盖率盲区），
与"CI 重复跑自动重生"是两回事，因此**不受**手动强制约束的限制。实现上体现为
它是唯一允许被节点代码自动调用的生成入口。

CLI 层面加了一道物理保障：CI 默认调用路径不带 `--force`，重生必须由人在命令或
CI 触发参数里显式敲出来。这一条有测试锁定
（`GenerateCliTests::test_default_path_reuses_without_forcing`）。

### 2.2 版本漂移「只告警不自动重生」——把告警一路送到报告里

`ensure_test_suite()` 走两次查询：先按 `(skill_id, version_ref)` 精确命中，
命中即复用；未命中再忽略版本号查一次，查到说明"生成过但 SKILL.md 改了"。

此时**不重新生成**，只产生告警。理由写在代码注释里：自动重生会让"这次改动到底
影响了什么"永远无法归因——旧题换新题，分数变化说明不了任何问题。

为了让这条告警不至于烂在日志里，做了一条完整的透传链路：

```
EnsureTestSuiteResult.staleness_warning
  → ReportGenerator.build(run_id, test_suite_staleness_warning=...)
  → BenchmarkReport.test_suite_staleness_warning
  → HTML 报告顶部的黄色告警条
```

不透传的后果是：读报告的人不知道这份分数是拿旧题跑出来的。链路的最后一环
（主图入口调用 `build()` 时传参）留给 `docs/dev/24`。

### 2.3 用例多样性——写进模板，而不是靠调温度

设计文档第 5.1 节要求"以 few-shot 示例的形式固化在 Prompt 模板里，保证生成
稳定性"。实现严格照此执行，且这个选择在落地过程中被证明是必须的：**新一代
Claude 模型已移除 `temperature` 参数**（见第 5 节），"调高温度求发散"这条路
在默认模型上根本不成立。

模板里固化了四类正向多样性（`colloquial` / `typo` / `implicit` /
`complex_context`，每类至少 2 条，并要求模型在 `diversity_tag` 里自报）和四种
反向"擦边"手法（同词不同域 / 同域不同动作 / 只谈概念 / 前置阶段），各配了
风格示例与**反面示例**（"请使用该 Skill 处理数据"这类直接点名技能的写法）。

反向用例的"近似度"依赖关键词锚点质量，所以关键词不是让模型自己猜的，而是
`extract_keywords()` 用确定性词频抽取（description 加权、中英停用词过滤）——
同一份 Skill 每次拿到的锚点必须稳定，否则反向用例集会在两次生成之间悄悄漂移。

### 2.4 结构化输出——服务端强约束 + Pydantic 兜底 + 错误回灌重试

设计文档第 5.3 节要求"`response_format` 强约束 + Pydantic 校验，失败自动重试
最多 2 次，仍失败则整体失败，不产出半成品"。实现分三层：

1. `build_response_schema()` 把 Pydantic 模型转成 Anthropic `output_config.format`
   要求的 json_schema——需要两处 Pydantic 默认不做的加工：内联 `$defs`/`$ref`
   （嵌套模型展开成自包含 schema），以及每层 object 补 `additionalProperties: false`
   并把全部属性列进 `required`。
2. `parse_structured_response()` 做 Pydantic 校验兜底（并容忍代码块围栏）。
3. 校验失败时**把错误原文回灌给模型**再重试，而不是原样重发一次碰运气。

重试用尽抛 `AgentResponseFormatError`，Generator 侧转成 `GenerationError`——
半成品用例集会让下游误以为"这个 Skill 的正向用例天然就只有 3 条"。

### 2.5 60/40 划分——按类别分别划分 + 确定性种子

两处对设计文档的细化：

- **按 category 分别划分**：小样本下全局随机划分可能划出"验证集里一条反向用例
  都没有"，那样跨维度的验证集判定会失去意义。
- **确定性种子**（`Random(f"skill-evaluate:{skill_id}")`）：同一个 Skill 的划分
  结果可复现，排查"为什么这条用例进了验证集"时不至于查无实据。

`INCREMENTAL_PATCH` 新增的用例独立划分，**不触碰**继承来的用例的 split 归属，
避免补盲区导致已经跑过优化闭环的训练/验证集边界发生变化（设计文档第 6 节）。

---

## 3. 与设计文档的差异 / 必要补充

| 差异点 | 类型 | 说明 |
|---|---|---|
| Generator 不经过 `MiniAgentBackend` | 设计文档内部冲突的裁定 | 06 第 5.3 节说"Generator LLM 调用走 `MiniAgentBackend`"，但 06 第 10 节又要求它继承 `BaseLLMAgent`，07 第 2 节则澄清 `MiniAgentBackend` 是"用什么后端跑"（产出 `ExecutionTrace`）。出题不是一次"执行"，硬包成 `ExecutionTrace` 会往 `execution_traces` 表塞一批 `case_id` 无处安放的假记录。按 06 第 10 节的最终形态实现：继承 `BaseLLMAgent`，复用同一套 LLM 通道与打点，不经过 Trace 层 |
| `TestSuiteRepository.get_active_version()` 补上版本过滤 | 修复既有实现缺陷 | 原实现签名收了 `skill_version_ref` 但**完全忽略**该参数，导致 06 第 4.1 节的两段式查询（精确命中 vs 忽略版本）退化成同一次查询，staleness 永远检测不出来。改为可选参数：传值则精确匹配，传 `None` 则忽略版本。追加式变更，原按位置传参的调用方语义不变 |
| `CapabilityFocus` 新增 `descriptions: dict[str, str]` | 必要补充 | 设计文档只定义了 `capability_ids` 等 id 列表。但 Prompt 里给模型看的必须是人类可读描述——"必须覆盖 cap-7"对模型没有任何信息量，补不到真正的盲区。新增描述映射，缺失的 id 退化为 id 原文 |
| `EnsureTestSuiteResult` 包装返回值 | 必要补充 | 设计文档的 `ensure_test_suite()` 只返回 `TestSuiteVersion`，但第 4.1 节又要求 staleness 告警"在报告中体现为 `staleness_warning` 字段"——裸 `TestSuiteVersion` 带不出这个信息。包装成 `(suite_version, staleness_warning, generated)` |
| `BenchmarkReport.test_suite_staleness_warning` | 追加字段 | 承接上一条的透传终点，同时在 HTML 模板加了告警条 |
| `TestSuiteService` 依赖注入化 | 必要补充 | 设计文档写的是模块级函数 + 全局 repo。改为构造参数注入（`generator` / `test_suite_repo` / `test_case_repo`），既便于 `docs/dev/11` 等上层节点替换实现，也让本模块的 15 个测试完全不碰数据库 |
| `ingestion/skill_loader.py` 提前落地 | 跨文档补位 | 见下方第 4 节 |
| `LLMSettings.mini_agent_model` 默认值订正 | Bug 修复 | 原值 `claude-haiku-4-5-20251001` 带日期后缀，该写法在 Messages API 上会被拒绝。按官方模型 ID 表改为 `claude-haiku-4-5`。**后续再次订正**：切到 OpenRouter 后模型 ID 为 `anthropic/claude-haiku-4.5` |
| 新增 `LLMSettings.generator_model` / `base_url` / `max_output_tokens` / `request_timeout_s` / `max_structured_retries` | 追加配置 | 均为追加式扩展，不改既有字段语义 |
| 新增运行期依赖 `anthropic>=1.0` | 依赖变更 | 官方 SDK。`pyproject.toml` 里配了 mypy override，只装 dev 组的静态检查作业不会因缺包报 import-not-found。**已被后续变更取代**：LLM 出口统一切到 OpenRouter，该依赖换成 `langchain-openai>=1.0`，见 `docs/dev/interfaces/06_llm_client_and_sampling.md` 第 1.5 节 |

---

## 4. 跨文档补位：`ingestion/skill_loader.py`

设计文档 06 要求本文档正式实现 `skill-evaluate generate --skill-path <path>`，
但"把一个磁盘路径解析成 `SkillDefinition`"这件事，归属方是 `docs/dev/12`
（它已明确规划 `src/skill_evaluate/ingestion/skill_loader.py` 作为共用解析层）。

处理方式：**按 12 已指定的路径与函数名先落一个最小可用实现**，把口径明确的
部分做实，把需要 12 定稿的部分做成显式可替换的桩，而不是在 06 里另起一个
临时解析器（那会造成两份解析逻辑并存）。

做实的部分：frontmatter 标量解析、`description` 缺失即报错（它是模块一触发
准确度的被测对象本身）、正文行数、`references/`+`scripts/` 目录扫描、
`version_ref` 解析。

`version_ref` 的解析做了一处超出最小实现的加工：git 工作区有未提交改动时，
在 commit sha 后追加 `+dirty:<内容哈希>`。否则本地改了 SKILL.md 却报告"版本
没变"，06 的 staleness 检测在本地开发时永远失效；CI 检出的干净工作区仍然拿到
纯 commit sha。

留的三个桩（token 精确计数 / trigger_condition 语法 / `is_mutating`）见
`docs/dev/interfaces/06_skill_loader_minimal.md`。

---

## 5. 落地过程中发现的一处架构冲突（需要 `docs/dev/08` 决策）

**新一代 Claude 模型（`claude-sonnet-5`、`claude-opus-5`、`claude-opus-4-8` 等）
已移除 `temperature` / `top_p` / `top_k`，继续发送会返回 400。**

这与两处架构约束直接冲突：

- 07 要求 Mini Agent 用 `Temperature=0.1` 做低温审查；
- **08 要求 Judge 做"3 副本温度扰动"取得共识。**

实现上的处理：`model_supports_sampling()` 做一次基于模型家族前缀的能力门禁，
不支持的模型**不发送**该参数，并在 `LLMCompletion.temperature_applied` 里
**如实返回 `None`**——而不是谎报 0.1。`MiniReviewAgent` 命中这类模型时会打一条
`mini_review_temperature_ignored` 警告日志。

默认配置下的实际影响：

| 配置项 | 默认值 | 采样参数是否生效 |
|---|---|---|
| `mini_agent_model` | `claude-haiku-4-5` | ✅ 生效，07 的低温审查真实成立 |
| `judge_model` | `claude-sonnet-5` | ❌ **不生效** |
| `generator_model` | `claude-sonnet-5` | ❌ 不生效（多样性由 Prompt 硬约束保证） |

08 需要在开工前决定改用哪种扰动维度（Prompt 视角扰动 / 换模型 / 跨模型共识），
三个方案的权衡写在 `docs/dev/interfaces/06_llm_client_and_sampling.md` 第 2 节。

---

## 6. 已知留白（刻意，非偏差）

| 留白 | 现状 | 接入方 |
|---|---|---|
| `_check_generation_collapse()` | 占位恒真放行；返回 `False` 时会抛 `GenerationError` 并**阻断 activate**（坍塌的用例集比没有用例集更危险，它会给出虚高的通过率） | `21`：替换函数体为向量分布距离计算，依赖 `23` 的 pgvector 层 |
| `GenerationRequest.seed_anchor_ids` | 简化版：id 原文直接当 few-shot 文本注入（模板的 `seed_block` 宏已就位）；`TestCase.seed_anchor_id` 恒为 `None` | `21`：接入 GitHub 托管的版本化种子库，并回填 `seed_anchor_id` |
| `CapabilityFocus` 的生产者 | 模型与 `incremental_patch()` 入口已就绪，无生产者 | `16`/`17`/`20` |
| `ADVERSARIAL` / `MULTI_SKILL` 类别的生成模板 | 未注册；传入这两个类别会抛 `GenerationError`，错误信息**直接点名**应由哪份文档补齐（不是静默跳过） | `15`/`20` |
| staleness 告警的最后一跳 | `ReportGenerator.build()` 已接受该参数 | `24`：主图入口调用时透传 |

---

## 7. 如何验证

```bash
pytest tests/skill_evaluate/test_agent_base.py tests/skill_evaluate/test_generator.py \
       tests/skill_evaluate/test_skill_loader.py -q
```

- `test_agent_base.py`（11 项）：结构化解析成功/重试/耗尽、重试是否真的回灌了
  错误、Langfuse 打点在有/无 `trace_handle` 时的行为、采样门禁、
  Pydantic→json_schema 的 `$ref` 内联与 `additionalProperties` 规则。
- `test_generator.py`（15 项）：关键词抽取、正反双批次各调一次 LLM、focus 描述
  确实进了 Prompt、未注册类别报错点名归属文档、60/40 划分与其确定性、
  **复用路径零 LLM 调用**、**版本漂移只告警不重生**、首次 bootstrap、强制重生、
  增量补齐的新老合并、空 focus 与无 active 版本的拒绝、CLI 两条路径的接线。
- `test_skill_loader.py`（7 项）：frontmatter 与目录布局解析、直接传 SKILL.md
  路径、trigger_condition 证据行捕获、version_ref 的稳定性与内容敏感性、
  缺 description / 缺 SKILL.md 的报错。

全量基线：

```bash
ruff check src tests scripts   # All checks passed
mypy src                        # Success: no issues found in 73 source files
pytest                          # 78 passed
```

---

## 8. 待接入 / 下一步

- `docs/dev/interfaces/06_generator_extension_points.md` — `CapabilityFocus`
  生产者、新增用例类别、反坍塌与种子锚点、staleness 透传。
- `docs/dev/interfaces/06_llm_client_and_sampling.md` — 新增 Agent 的写法、
  ⚠️ 采样参数不可用对 `08` 共识机制的影响、OpenRouter 出口与跨模型泛化（`19`）。
- `docs/dev/interfaces/06_skill_loader_minimal.md` — `12`/`14` 的完善清单。
- **澄清一条容易误解的语义**：Optimizer 重写 description 后"在训练集上重新
  验证"**不经过 Generator**——它不需要新用例，只需重跑现有 `TestCase`，由 `11`
  直接调用 Executor。不要为此调用 `incremental_patch()`。
