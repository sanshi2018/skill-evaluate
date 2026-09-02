# 10 Validator Agent 与动态断言 Git 工具箱

> 状态：**待确认**
> 路线图位置：第 1 层 / 第 5 份（第 1 层收官文档）
> 依赖：`02`（`AssertionSpec`/`AssertionResult`）、`03`（`ExecutorBackend`，本文档对其接口做一次扩展，见第 4 节）、`04`（`AssertionRepository`）、`05`（Hook 端点，本文档扩展其 payload）、`06`（`BaseLLMAgent`）
> 被依赖：`13`（模块三，执行效果评测的确定性证据来源）、`15`（模块五，间接数据投毒/SAST 扫描场景）

---

## 1. 本文档目标

落地架构文档模块三第 5 节"动态断言与校验脚本注入机制"：在 Hermes 真正执行任务**之前**规划验证策略，任务执行完毕后把校验脚本注入**同一沙箱**执行，把 `exit_code`/`stdout`/`stderr` 作为比纯 LLM 裁判更可靠的确定性证据。第 1 层至此收官——本文档完成后，五个公共智能体（Generator/Mini Review/Judge/Optimizer/Validator）齐备，可以开始第 2 层各评测维度的具体拼装。

## 2. Validator Agent 职责与执行流

```python
# src/skill_evaluate/agents/validator/service.py
class ValidatorAgent(BaseLLMAgent):
    async def plan_assertion(self, case: TestCase, skill: SkillDefinition) -> AssertionSpec:
        """
        输入：case.prompt / case.expected_output / skill 的意图（description + body_markdown 摘要）
        决策顺序：
          1. template_lookup: 先查 Git 断言工具箱（第3节），命中则直接引用模板，不调用生成 Prompt
          2. template_inherit: 工具箱中存在"形状相似但需要参数化"的模板，走继承式生成
             （把模板作为 few-shot，要求模型只改写必要部分）
          3. generated_from_scratch: 工具箱无匹配，从头生成校验脚本
        本方法只产出 AssertionSpec（策略 + 脚本内容/模板引用），不执行。
        """
```

`expected_output` 为空（`TestCase.expected_output is None`，即架构文档"可以不通过代码检查的断言"场景）时，`plan_assertion()` 直接返回 `strategy="none"` 的空 spec（新增取值，见第 5 节），意味着该用例只依赖 Judge Agent 的语义裁决，不生成校验脚本——不是所有用例都需要确定性断言，本文档不强制。

## 3. Git 断言工具箱

### 3.1 仓库结构约定（外部独立仓库，非本项目仓库）

```
skill-evaluate-assertion-toolbox/           # 独立 GitHub 仓库
├── templates/
│   ├── json_schema_validator.py.jinja
│   ├── file_exists_validator.py.jinja
│   ├── csv_shape_validator.py.jinja
│   ├── sql_no_injection_validator.py       # 模块五复用，直接调 sqlparse/Semgrep 规则
│   └── html_no_xss_validator.py
├── manifest.yaml                             # 每个模板的元数据：适用场景描述、参数 schema、关键词标签
└── CHANGELOG.md
```

`manifest.yaml` 每条记录：

```yaml
- template: json_schema_validator.py.jinja
  description: "校验目标文件是否为合法 JSON 且满足给定 schema"
  keywords: [json, schema, 结构化输出, 格式校验]
  params: [target_file, schema_definition]
```

### 3.2 检索机制（分阶段实现）

- **本文档实现的简化版**：基于 `manifest.yaml` 的关键词匹配（对 `case.expected_output` 与 `skill` 描述做关键词抽取后与 `keywords` 字段做交集打分），取分数最高且过阈值的模板作为 `template_lookup` 命中；未过阈值则判定为需要 `template_inherit` 或 `generated_from_scratch`。
- **占位钩子（对齐文档06处理"反坍塌"的同一模式）**：预留 `_semantic_lookup()` 空实现，文档 23（长时记忆与数据飞轮）接入后升级为向量语义检索 + BM25 混合检索 + Reranker（复用架构文档"长时记忆"一节的方案），本文档不重复设计该检索算法。

```python
async def _semantic_lookup(query: str) -> list[TemplateMatch]:
    """占位：当前仅调用关键词匹配版本；文档23接入后替换为混合检索实现"""
    return _keyword_lookup(query)
```

### 3.3 仓库同步

工具箱仓库通过 CI 定时或按需 `git pull` 同步到本地缓存目录（`~/.cache/skill-evaluate/assertion-toolbox/` 或容器内固定路径），`config.py` 新增 `ValidatorSettings.toolbox_repo_url` / `toolbox_ref`（追加式扩展）。锁定具体 `toolbox_ref`（commit sha）写入每次生成的 `AssertionSpec.template_ref`，保证同一次评测运行中断点恢复后引用的模板版本不漂移。

## 4. 沙箱内执行闭环：对文档 03 接口的扩展

**问题**：文档 03 的 `ExecutorBackend.execute()` 执行完任务即销毁 Ephemeral 容器（"执行结束（无论成败）立即销毁"），但本文档要求校验脚本在**同一沙箱**、**任务完成之后**运行，才能直接访问任务产出的文件/状态。这是本文档对文档 03 的一次**正式接口扩展**，而非另起新协议。

### 4.1 `ExecutionRequest` 追加字段

```python
class ExecutionRequest(BaseModel):
    # ...文档03已定义字段...
    assertion_specs: list[AssertionSpec] = Field(default_factory=list)   # 本文档追加
```

### 4.2 `HermesBackend.execute()` 行为扩展

当 `assertion_specs` 非空时，`HermesBackend` 在任务主流程结束、**容器销毁前**，将每个 `AssertionSpec` 对应的脚本内容下发到同一沙箱执行，按顺序收集 `AssertionResult`，一并通过 Hook 回调上报（而不是任务与断言各发一次 Hook——避免两次网络往返之间沙箱状态发生意外变化）。

### 4.3 Hook Payload 扩展（对文档 03 第 4.3 节映射表、文档 05 端点解析逻辑的追加）

| Hermes Hook Payload 新增字段 | 映射到 |
|---|---|
| `assertion_executions[]`（每项含 `assertion_id`, `exit_code`, `stdout`, `stderr`） | `AssertionResult` 列表（非 `ExecutionTrace` 内部字段，独立落 `assertion_results` 表） |

文档 05 的 `hermes_hook()` 端点在保存 `ExecutionTrace` 之后，追加一步：若 payload 含 `assertion_executions`，逐条构造 `AssertionResult` 并通过 `AssertionRepository.save()` 落库，`AssertionResult.passed = (exit_code == 0)`。这一步是本文档对文档 05 端点实现的正式补充说明，实际代码改动发生在文档 05 对应文件中，此处仅作接口契约声明。

### 4.4 `MiniAgentBackend` 场景

`MiniAgentBackend` 不涉及真实沙箱，若节点误传 `assertion_specs` 给 Mini 后端，`MiniAgentBackend.execute()` 直接忽略该字段并在日志中记一条 warning——断言脚本执行天然要求真实执行环境，这是路由表（文档 03 第 5 节）已经把相关维度固定为 `PLUGGABLE` 后端的原因之一，不属于本文档需要处理的常规路径。

## 5. `AssertionSpec.strategy` 的完整取值（对文档 02 该字段的语义补全）

文档 02 只声明了字段类型，本文档补全枚举取值：

```python
class AssertionStrategy(StrEnum):
    NONE = "none"                    # 不生成脚本，纯 LLM 裁决
    TEMPLATE_LOOKUP = "template_lookup"
    TEMPLATE_INHERIT = "template_inherit"
    GENERATED_FROM_SCRATCH = "generated_from_scratch"
```

## 6. 脚本生成的安全约束

`generated_from_scratch` 与 `template_inherit` 两种路径生成的脚本本身也是"LLM 产出的代码"，架构文档明确指出这存在"验证器自身幻觉"风险。本文档要求：

- 生成的脚本在下发沙箱执行前，先经过本地（非沙箱内）的静态检查：Python 走 `ast.parse()` 语法校验，Bash 走 `bash -n` 语法检查，失败则视为生成失败，触发一次重试（最多 2 次，复用文档 06 的重试模式），仍失败则降级为 `strategy="none"`（放弃确定性断言，退回纯 LLM 裁决，并在报告中标记"断言生成失败"供人工关注，而不是让流水线因为脚本生成失败而整体中断）。
- 生成脚本一律要求：干净输出走 `stdout`（供潜在的管道级联），日志/诊断走 `stderr`，`exit_code` 非 0 且仅非 0 代表失败——这个约定不仅是模块四对被测 Skill 脚本的要求，也是 Validator Agent 生成的校验脚本自身必须遵守的规范，保证 `AssertionResult.passed` 的判定逻辑（`exit_code == 0`）全局一致。

## 7. `AssertionResult` 作为 Judge Agent 的证据输入

`AssertionResult` 不直接构成 `JudgeVerdict`，而是作为 `judgmental_verdict()` 调用时 `content` 字典的一部分传入（例如某个"是否生成了合法 JSON 文件"的判定，`content={"assertion_exit_code": 0, "assertion_stdout": "...", ...}`），由具体评测维度文档决定"断言证据"与"LLM 语义裁决"如何组合成最终判定（常见模式：断言失败直接判负，不需要 LLM 介入；断言通过仍可能需要 LLM 补充判断非结构化质量，如文案是否得体）。本文档不预设这一组合逻辑，留给文档 13/15。

## 8. 待接入文档（本文档留给后续模块的接口清单）

| 预留位置 | 当前状态 | 由哪份文档接入 | 接入方式 |
|---|---|---|---|
| `skill-evaluate-assertion-toolbox` 仓库本身 | 结构约定已定义，仓库未创建 | 运维侧初始化（不属于单一开发文档） | 按第 3.1 节结构创建仓库，初始至少含 `json_schema_validator`/`file_exists_validator` 两个模板 |
| `_semantic_lookup()` | 占位为关键词匹配 | `23` | 替换为向量+BM25+Reranker 混合检索实现 |
| `AssertionResult` 与 `JudgeVerdict` 的组合判定逻辑 | 接口已声明输入方式，无组合规则 | `13`（模块三执行效果）、`15`（模块五 SAST 场景） | 各文档在其判定节点中实现具体组合规则 |
| `sql_no_injection_validator.py` / `html_no_xss_validator.py` 的实际扫描后端（Semgrep 等） | 工具箱清单占位 | `15` | 接入 Semgrep 或等价 SAST 工具，脚本内部调用其 CLI 并翻译 exit_code |

---

## 第 1 层收官说明

至此，五个跨维度复用的公共智能体——**Generator（06）、Mini Review（07）、Judge（08）、Optimizer（09）、Validator（10）**——接口已全部定义并相互对齐：Generator 产出 `TestCase`，Executor（第0层，03）产出 `ExecutionTrace`，Validator 产出 `AssertionResult`，Judge 消费三者产出 `JudgeVerdict`/`ConsensusResult`，Optimizer 消费 Judge 的失败判定产出 `Patch` 并驱动重试闭环。第 2 层的各评测维度文档（11~20）将不再定义新的公共能力，只负责"用正确的方式组装这五个智能体去回答某个具体评测问题"。

## 下一步

待你确认本文档后，我将进入 **第 2 层**，输出 **文档 11：模块一——触发准确度与泛化能力评测流水线**，作为全项目第一个端到端可运行的垂直切片。
