# 实现说明：07 Mini Agent 评审框架

> 对应设计文档：`docs/dev/07_Mini_Agent评审框架.md`
> 状态：已实现（注册机制 + 首批 7 个模板全部落地，含 Prompt 措辞）

本框架复用 `docs/dev/06` 落地的 `BaseLLMAgent` 底座，底座本身的说明见
[`06_Generator_Agent与测试集生命周期管理.md`](./06_Generator_Agent与测试集生命周期管理.md)。

---

## 1. 交付了什么

| 文件 | 内容 | 对应设计文档章节 |
|---|---|---|
| `agents/mini/service.py` | `MiniReviewAgent`、`ReviewRequest`、`DetailedReview` | 第 3 节 |
| `agents/mini/templates/registry.py` | `ReviewTemplate`、`REVIEW_TEMPLATE_REGISTRY`、`register_template()`、`get_template()`、`verdict_field_to_status()` | 第 4 节 |
| `agents/mini/templates/schemas.py` | 7 个输出 Schema + 公共基类 `BaseReviewOutput` | 第 5 节 |
| `agents/mini/templates/builtin.py` | 首批 7 个模板的注册 | 第 5 节 |
| `agents/mini/templates/_prefix.jinja` | 全部模板共用的三条框架级约束 | 第 6 节 |
| `agents/mini/templates/*.jinja`（7 份） | 各审查场景的 Prompt 措辞 | 第 5.1~5.7 节 |
| `agents/mini/llm_client.py` | `RealMiniLLMClient`：把 `AgentLLMClient` 适配成 `MiniLLMClient` 协议 | interfaces/05 第 2 节 |

首批 7 个模板：

| key | 归属模块 | 必需 `content` 变量 |
|---|---|---|
| `omission_audit` | 模块二 / `12` | `skill_md` |
| `scoping_check` | 模块二 / `12` | `skill_md` |
| `progressive_disclosure_static` | 模块二 / `12` | `skill_md`, `reference_files` |
| `help_doc_quality` | 模块四 / `14` | `script_path`, `help_output` |
| `constructive_error` | 模块四 / `14` | `invocation`, `error_output` |
| `linguistic_smell` | 模块九 / `19` | `skill_md` |
| `control_calibration` | 模块三 / `13` | `skill_md` |

---

## 2. 用什么方式实现了需求

### 2.1 核心目标：新增审查维度不该动框架代码

设计文档的目标是"新增一种审查维度时，只需要新增一份 Prompt 模板 + 一个输出
Schema，不需要新写调用逻辑"。实现成一个注册表：

```python
register_template(ReviewTemplate(
    key="my_check",
    prompt_path="my_check.jinja",
    output_schema=MyCheckOutput,
    to_status=verdict_field_to_status,
    required_variables=("skill_md",),
))
```

`MiniReviewAgent` 本体完全不感知有哪些模板——它只做"取模板 → 渲染 → 调 LLM →
按模板自带的规则映射成 `JudgeVerdict` → 落库"。7 个内置模板走的是和后续文档
新增模板**完全相同**的注册路径，没有特殊待遇。

注册期就做了两道校验，都是"宁可早失败"的取舍：

- **key 重复注册直接报错**：静默覆盖会让"到底跑的是哪份 Prompt"无法追溯。
- **prompt 文件不存在直接报错**：不留到真正跑评审时才炸。

### 2.2 Prompt 措辞：把框架级约束做成宏，避免逐份复制

设计文档第 6 节的三条公共约束（只输出 JSON / 证据不足时倾向宽容 / reasoning
必须引用原文片段）做成 `_prefix.jinja` 里的 `common_rules()` 宏，7 份模板各
`import` 一次。测试逐份断言这三条确实出现在渲染结果里——公共约束靠"复制粘贴时
别忘了"来保证，迟早会漏。

其中"**宁可漏判也不误判**"这一条是对架构文档"静态审查可能纸上谈兵"这一权衡的
直接回应，措辞里写明了理由（"你判定为多余的一条说明，实际执行时可能恰恰是校准
方向的关键"），而不是干巴巴一句"请宽容"。

7 份模板的正文都按同一套结构写：**判定信号 → 反面示例 → 误伤边界**。第三段
尤其重要，例如：

- `omission_audit`：明确列出"看起来平常但被限定在本项目语境下的约定""反直觉的
  坑""对模型已知知识的否定"三类**不算常识**，防止把 Skill 最有价值的内容误杀。
- `scoping_check`：写明"行数少不等于范围过窄"，判据是触发条件与调用者心智模型
  是否一致，不是篇幅。
- `linguistic_smell`：区分"明确、强硬但讲道理的约束"（好指令）与"用情绪代替
  信息"（坏味道）。

各文档接入时**只需定阈值**（几条命中算维度级 FAIL）与是否 blocking，措辞已就绪。

### 2.3 输出契约：用 `Literal` 而不是 `str`

`verdict` 等字段全部用 `Literal["pass", "fail"]`。这样模型返回 `"PASS"` 或
`"ok"` 这类没约定过的值时，会在 Pydantic 校验阶段失败并触发 `BaseLLMAgent` 的
重试，而不是被 `verdict == "pass"` 悄悄判成 FAIL——后者会产生一个看起来正常、
实际上完全错误的评审结论。

### 2.4 `review()` 直接落库 `JudgeVerdict`，投票留给 Judge

按设计文档第 3 节的取舍：纯静态审查不需要 08 的多副本共识，单次输出就是最终
判定，直接落一份 `JudgeVerdict` 既满足报告聚合需要，又不引入中间态。

同时为 08 的共识流程备好了两个接口，保证"投票是 Judge 的职责、模板执行是 Mini
Agent 的职责"这条分层不被打破：

- 同一个 `ReviewRequest` 可被不同 temperature / 不同 model 的实例反复调用；
- `persist=False` 关闭单次落库，由 Judge 侧统一决定哪些 verdict 入库。

这条复用路径有测试锁定
（`test_same_request_can_be_replayed_at_different_temperatures`）。

### 2.5 与 `MiniAgentBackend` 的边界（设计文档第 2 节）

两者正交，实现上也确实没有互相调用：

- `MiniAgentBackend`（`executors/`）= **用什么后端跑**，产出 `ExecutionTrace`；
- `MiniReviewAgent`（`agents/mini/`）= **跑什么审查逻辑**，产出 `JudgeVerdict`。

`agents/mini/llm_client.py::RealMiniLLMClient` 是两者唯一的接触点，而它只做
协议适配（`AgentLLMClient` → `MiniLLMClient`），不含任何评审逻辑。

---

## 3. 与设计文档的差异 / 必要补充

| 差异点 | 类型 | 说明 |
|---|---|---|
| `ReviewTemplate.to_severity` 字段 | 提前解决设计文档留的开放问题 | 07 第 7 节把"`15` 的严重性定级如何扩展"列为待定（"泛化 `to_status` 返回类型，或新增并行字段，由 15 决定"）。已按"新增并行字段"落地，`15` 因此**不需要修改** `ReviewTemplate` 或 `MiniReviewAgent`：`review()` 仍只返回 `JudgeVerdict`（返回类型稳定），严重级别从 `review_detailed()` 的 `template.to_severity(output)` 取得 |
| `review_detailed()` 方法 | 必要补充 | 设计文档的 `review()` 只返回 `JudgeVerdict`，但模板专有字段（如 `common_sense_statements`、`reference_files_without_trigger_condition`）是各维度节点真正要用的判定依据，裸 `JudgeVerdict` 带不出来。新增 `review_detailed()` 返回 `(verdict, output, template)`，`review()` 保留为它的薄封装 |
| `BaseReviewOutput` 公共基类 | 必要补充 | 设计文档 7 个 Schema 各自声明 `reasoning` 与 `verdict`。提取公共基类，避免 7 份重复，也让"所有评审都必须给出 reasoning"成为类型层面的约束 |
| `ReviewTemplate.required_variables` | 必要补充 | 设计文档没有这个字段。加上后，`content` 少传变量会在**发请求之前**抛 `ReviewTemplateError`，而不是发出一个缺了半截的 Prompt 再拿回一个看似正常的结论。配合 Jinja 的 `StrictUndefined` 双保险 |
| `persist` 构造参数 | 必要补充 | 设计文档的 `review()` 无条件落库。08 的共识流程会对同一 subject 连打多次，无条件落库会污染 `judge_verdicts` 表。默认仍为 `True`（纯静态审查场景不变） |
| `verdict` 字段用 `Literal` | 收紧 | 见上方 2.3 |
| 模板 key 重复注册报错 | 收紧 | 设计文档的 `register_template()` 是直接赋值（静默覆盖） |

---

## 4. 顺带完成的前序接入（`docs/dev/interfaces/05` 第 2 节）

`executors/factory.py::build_backend()` 原先无参构造 `MiniAgentBackend()`，注入的
是 `StubMiniLLMClient`——返回带 `[stub]` 标记的占位文本，`final_response` 不代表
任何真实评审结论。

本次落地后改为注入 `RealMiniLLMClient` 与 `LLMSettings.mini_agent_model`。
`MiniAgentBackend.execute()` 的 `final_response` 从此是真实模型输出，模块二等
调用方可以正常读取它做业务决策。

`StubMiniLLMClient` 保留为**显式注入用的测试替身**（无参构造时仍是它），其
docstring 已改写说明这一点：真实报告里如果看到 `[stub]` 字样，说明后端被错误地
无参构造了。

同一份接入文档第 1 节（Agent 基类挂载 Langfuse 钩子）由 `docs/dev/06` 的
`BaseLLMAgent._invoke()` 完成，两节均已在 interfaces 文档里标注为已接入。

---

## 5. ⚠️ 一处架构约束在默认模型上不成立

设计文档要求 Mini Agent 用 `Temperature=0.1`。**新一代 Claude 模型已移除采样
参数**，该约束在那类模型上物理不成立。

当前默认 `mini_agent_model` 是 `claude-haiku-4-5`，**仍支持采样**，所以低温审查
在默认配置下是真实生效的。但如果有人把它改成 `claude-sonnet-5`：

- 请求里不会带 `temperature`（带了会 400）；
- 构造时打一条 `mini_review_temperature_ignored` 警告日志；
- `JudgeVerdict.temperature` 仍记录请求值，但实际未下发。

判定的确定性此时只能靠 Prompt 约束保证。完整说明见
`docs/dev/interfaces/06_llm_client_and_sampling.md` 第 2 节——这一条对 `08` 的
共识机制影响更大，08 开工前必读。

---

## 6. 已知留白

| 留白 | 现状 | 接入方 |
|---|---|---|
| 7 个模板的判定**阈值** | Prompt 措辞与单次 pass/fail 已就绪；"几条命中算维度级 FAIL"未定 | `12`/`13`/`14`/`19`：阈值是维度节点的策略，不该写死在模板里 |
| 模块三"动态渐进式披露探查"模板 | 未预置（需要 `PLUGGABLE` 后端配合 Trace，不是纯文本审查） | `13`：新增模板，把 `ExecutionTrace.actions` 序列化后作为 content 变量传入 |
| 模块五严重性定级模板 | 未预置，但 `to_severity` 扩展位已备好 | `15` |
| 各维度的 `record_dimension_result()` 调用 | 未接 | `12`~`20` |

---

## 7. 如何验证

```bash
pytest tests/skill_evaluate/test_mini_review.py -q
```

14 项覆盖：

- **注册机制**：首批恰好 7 个且 key 集合正确；每份模板都能用其声明的变量渲染
  成功且三条框架级约束都在；缺变量在调 LLM 前就报错；未知 key 的报错会列出已
  注册的 key；重复注册被拒；prompt 文件不存在被拒；`to_severity` 扩展位存在。
- **Schema 契约**：`verdict` 只接受约定字面量（`"PASS"` 会校验失败）；可选枚举
  字段接受 null；7 个 Schema 全部能转成合法 json_schema 且 `reasoning`/`verdict`
  进了 required。
- **Agent 行为**：pass/fail 的映射与落库、`review_detailed()` 返回模板专有字段、
  `persist=False` 不落库、同一请求在不同 temperature 下重放（08 的共识复用路径）。

全量基线：`ruff check` / `mypy src`（73 files）/ `pytest`（78 passed）均通过。

---

## 8. 待接入 / 下一步

- `docs/dev/interfaces/07_review_template_registry.md` — 首批 7 个模板的调用
  方式与各文档还需要做什么、新增模板三步法、`13` 的 Trace 模板、`15` 的
  `to_severity` 用法、`08` 的共识复用姿势。
- `docs/dev/08` 开工前请先读
  `docs/dev/interfaces/06_llm_client_and_sampling.md` 第 2 节。
