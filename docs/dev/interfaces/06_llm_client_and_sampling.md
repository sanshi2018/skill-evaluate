# 接入文档：共享 LLM 客户端与「采样参数在新模型上不可用」

> 由谁接入：`08`（Judge 多副本共识）、`13`/`19`（涉及温度扰动的场景）、
> `19`（跨模型泛化需要新增 provider）。
> 当前状态：`agents/llm.py` + `agents/base.py` 已实现并有测试覆盖。

## 1. 已就绪的基础设施（06~10 直接复用，不要重新造）

| 组件 | 位置 | 作用 |
|---|---|---|
| `AgentLLMClient` | `agents/llm.py` | 全部 Agent 共用的最小 LLM 调用协议（Protocol） |
| `OpenRouterLLMClient` | 同上 | 唯一的真实实现：`langchain_openai.ChatOpenAI` -> OpenRouter |
| `BaseLLMAgent` | `agents/base.py` | 调用 + 计量 + Langfuse 打点 + 结构化解析重试 |
| `build_response_schema()` | `agents/llm.py` | Pydantic 模型 -> `response_format` 的 json_schema |
| `normalize_model_id()` | `agents/llm.py` | OpenRouter 的 `<厂商>/<模型>[:变体]` -> 裸模型名 |

写一个新 Agent（`08`/`09`/`10`）的完整样子：

```python
class JudgeAgent(BaseLLMAgent):
    name = "judge_agent"

    def __init__(self, **kw):
        super().__init__(model=get_settings().llm.judge_model, temperature=0.0, **kw)

    async def judge(self, ...) -> Verdict:
        return await self._call_llm(prompt, VerdictOutput, system=SYSTEM)
```

`_call_llm()` 已经包了：结构化输出强约束、Pydantic 校验、失败重试（默认 2 次，
重试时把上一轮错误回灌给模型）、耗尽后抛 `AgentResponseFormatError`（**不返回
半成品**）、`TimingCostMetrics` 统计、Langfuse `log_agent_call()`。子类不要重复
实现这些。

Langfuse 打点需要 `trace_handle`：由流水线入口 `LangfuseAdapter().start_run_trace()`
产出一次，以构造参数传给各 Agent（旁路依赖注入，**不进 `PipelineState`**）。
不传时打点自动 no-op，Agent 照常工作。

## 1.5 全项目只有 OpenRouter 一条 LLM 出口

`OpenRouterLLMClient` 用 `langchain_openai.ChatOpenAI` 把 `base_url` 指到
`https://openrouter.ai/api/v1`（OpenAI 兼容协议），项目里**不再直连任何厂商 SDK**
（原 `AnthropicLLMClient` + `anthropic` 依赖已移除）。

换模型 / 换厂商 = 只改配置，不改代码：

```bash
SKILLEVAL_LLM_API_KEY=sk-or-v1-...          # 或裸 OPENROUTER_API_KEY
SKILLEVAL_LLM_JUDGE_MODEL=anthropic/claude-sonnet-5
SKILLEVAL_LLM_GENERATOR_MODEL=openai/gpt-5.6-terra
SKILLEVAL_LLM_MINI_AGENT_MODEL=anthropic/claude-haiku-4.5
```

几条容易踩的约定：

- **模型 ID 用 OpenRouter 的写法**：`<厂商>/<模型>`，版本号是点号
  （`anthropic/claude-haiku-4.5`），不是 Anthropic 原生 API 的短横线写法
  （`claude-haiku-4-5`）。写错是 404 而不是降级。
- **结构化输出**走 `response_format: {"type": "json_schema", strict: true}`，并额外
  下发 `provider.require_parameters=true`：OpenRouter 默认会把上游不支持的参数
  静默丢弃，对结构化输出而言"静默丢弃"= 拿回一段自由文本，只能靠 Pydantic 兜底
  重试。加这条让路由只挑支持结构化输出的上游。
- **`ChatOpenAI` 的 model/temperature/max_tokens 是构造期参数**，而
  `AgentLLMClient.complete()` 是按次传的，所以 client 内部按
  `(model, temperature, max_tokens)` 缓存实例。测试注入替身用构造参数
  `OpenRouterLLMClient(chat_factory=...)`，不要去 patch `ChatOpenAI`。
- **`LLMCompletion.model` 回填的是 OpenRouter 实际路由到的模型**
  （`response_metadata.model_name`），跨模型对比报告靠它区分副本，而不是回声
  请求参数。

## 2. ⚠️ 重要：`temperature` 在新一代 Claude 模型上会被 400 拒绝

Claude 4.6 及以后的模型（`claude-opus-5`、`claude-sonnet-5`、`claude-opus-4-8`、
`claude-fable-*` 等）**已移除 `temperature` / `top_p` / `top_k`**，继续发送会直接
返回 400。

这与两处架构约束直接冲突：

- docs/dev/07 要求 Mini Agent 用 `Temperature=0.1` 做低温审查；
- **docs/dev/08 要求 Judge 对同一 subject 做"3 副本温度扰动"取得共识。**

### 当前处理方式

`agents/llm.py::model_supports_sampling(model)` 做一次能力门禁（先经
`normalize_model_id()` 归一 OpenRouter 的写法）：不支持的模型
**不发送**该参数，并在 `LLMCompletion.temperature_applied` 里如实返回 `None`
（而不是谎报 0.1）。`MiniReviewAgent` 构造时若命中这种模型，会打一条
`mini_review_temperature_ignored` 警告日志。

默认配置下的实际情况：

| 配置项 | 默认值 | 采样参数是否生效 |
|---|---|---|
| `LLMSettings.mini_agent_model` | `anthropic/claude-haiku-4.5` | ✅ 生效，`temperature=0.1` 真实下发 |
| `LLMSettings.judge_model` | `anthropic/claude-sonnet-5` | ❌ **不生效** |
| `LLMSettings.generator_model` | `anthropic/claude-sonnet-5` | ❌ 不生效（多样性由 Prompt 硬约束保证） |

> 经 OpenRouter 时，不被上游支持的参数多半是被**静默丢弃**而不是报 400。门禁照旧
> 保留：它的作用是让 `temperature_applied` 如实反映"这次扰动到底有没有发生"。

### `08` 必须做的决策

"温度扰动"这条路在 `claude-sonnet-5` 上物理不成立。三个可选方案，由 `08` 定稿：

1. **换扰动维度**：保持 `judge_model` 不变，把 3 副本的差异来源从温度改为
   *Prompt 视角扰动*（例如分别以"证据充分性""反例存在性""判定一致性"三个切入
   角度提问）。语义上更接近"三个不同的裁判"，而不是"同一个裁判掷三次骰子"。
2. **换模型**：把 `judge_model` 指向仍支持采样的模型（如 `claude-haiku-4-5`），
   保留字面意义上的温度扰动，代价是裁判能力下降。
3. **跨模型共识**：3 副本用 3 个不同模型，正好与 `19`（跨模型泛化）共享基础
   设施。

无论选哪条，`JudgeVerdict.temperature` 字段都请**如实记录实际下发的值**
（不支持采样时记 0.0 或请求值均可，但要在 `08` 文档里写明口径），不要让报告
读者以为做了一次实际没发生的扰动。

`MiniReviewAgent` 侧已经为方案 1/2/3 备好了接口：同一个 `ReviewRequest` 可以被
不同 temperature / 不同 model 的实例反复调用，`persist=False` 可关闭单次落库，
由 Judge 侧统一决定哪些 verdict 入库（见
`tests/skill_evaluate/test_mini_review.py::test_same_request_can_be_replayed_at_different_temperatures`）。

## 3. 跨模型泛化（`19`）：换模型而不是换 provider

OpenRouter 一个 Key 覆盖 400+ 模型，所以 `19` 的"跨模型泛化"不再需要新增
provider/client，只需要在调用点传不同的 OpenRouter 模型 ID——`BaseLLMAgent`
子类本来就允许按实例指定 `model`（见第 2 节末尾的三副本方案）。

`build_default_llm_client()` 只认 `LLMSettings.provider == "openrouter"`，其余值
直接抛 `ConfigurationError`（而不是静默按 OpenRouter 发出去）。自建 OpenAI 兼容
网关请改 `SKILLEVAL_LLM_BASE_URL`，不要新增 provider 分支。

仍然成立的两条既有约定：

- 不支持结构化输出的模型，`response_schema` 会退化为"在 Prompt 里要求 JSON"，
  并保证仍然返回可被 `parse_structured_response()` 解析的文本；本实现里由
  `provider.require_parameters=true` + `parse_structured_response()` 的围栏容错
  + `BaseLLMAgent` 的重试共同兜底。
- 不要伪造成功响应。没配 Key / 缺 `langchain-openai` 时构造即抛
  `ConfigurationError`——假响应会污染下游全部评测结论（与 `03` 对
  `UnconfiguredHermesSandboxClient` 的处理原则一致）。
