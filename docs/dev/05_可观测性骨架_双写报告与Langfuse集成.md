# 05 可观测性骨架：双写报告与 Langfuse 集成

> 状态：**待确认**
> 路线图位置：第 0 层 / 第 5 份（第 0 层收官文档）
> 依赖：`02`（数据契约）、`03`（Hook 协议、待接入项：HTTP 端点宿主）、`04`（待接入项：Hook 端点调用 `resolve_suspension`）
> 被依赖：`06~24` 全部——任何节点产出的判定/报告都通过本文档的双写适配器输出；本文档也是文档 03/04 两处"待接入"缺口的收口点，第 0 层至此闭环。

---

## 1. 本文档目标

1. 落地文档 03 定义的 Hermes Hook HTTP 端点，串联文档 04 的 `resolve_suspension`，让"接收外部执行结果 → 落库 → 唤醒挂起节点"链路完整可跑。
2. 定义 `benchmark.json` / HTML 报告生成器接口——不可变制品，挂载到 CI 归档与阻断判定。
3. 定义 Langfuse 双写适配器——把 LangGraph 节点/Agent 调用/`ExecutionTrace` 映射为 Langfuse 的嵌套 Trace/Span，用于可视化与成本分析。
4. 统一"打点"约定：所有后续文档新增的 Agent/Node，必须遵守本文档定义的打点接口，不再各自决定日志格式。

## 2. API 层落位

新增模块 `src/skill_evaluate/api/`，承载 Hook 端点与（后续文档 22 会复用的）人工审批回调端点。选用 FastAPI（补入文档 01 依赖列表：`fastapi>=0.115`, `uvicorn>=0.32`，同样遵循"新增字段不改语义"的追加原则）。

```
src/skill_evaluate/api/
├── __init__.py
├── app.py                 # FastAPI app 工厂
├── hooks_hermes.py          # 本文档实现
├── hooks_approval.py         # 桩，22 文档实现
└── security.py                 # HMAC 签名校验（复用于两类 Hook）
```

### 2.1 HMAC 签名校验（文档 03 第 4.4 节要求）

```python
# src/skill_evaluate/api/security.py
def verify_hermes_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

签名校验失败一律返回 `401`，并打点 `structlog` 告警（可能是伪造 Trace 注入尝试，与模块五安全评测的"可信度"要求呼应）。

### 2.2 Hermes Hook 端点

```python
# src/skill_evaluate/api/hooks_hermes.py
@router.post("/hooks/hermes/{run_id}/{case_id}/{run_index}")
async def hermes_hook(run_id: str, case_id: str, run_index: int, request: Request):
    raw = await request.body()
    if not verify_hermes_signature(raw, request.headers["X-Hermes-Signature"], settings.executor.hermes_hook_secret.get_secret_value()):
        raise HTTPException(401)

    payload = HermesHookPayload.model_validate_json(raw)   # 按文档03 4.3 表格定义的 schema
    trace = map_hermes_payload_to_trace(payload, case_id=case_id, run_index=run_index)  # 03 4.3 映射规则实现于此

    await trace_repository.save(trace)
    await resolve_suspension(
        wait_key=f"{run_id}:{case_id}:{run_index}",
        resume_payload=trace.trace_id,
        thread_id=f"{skill_id_of(run_id)}:{run_id}",
    )
    return {"status": "accepted"}
```

- `map_hermes_payload_to_trace()` 是文档 03 第 4.3 节映射表的**唯一实现位置**（放在 `executors/hermes_backend.py` 内以保持内聚，本端点只调用不重复实现）。
- 重复回调忽略逻辑（文档 04 第 6 节）在 `resolve_suspension` 内部实现（检查 `pending_hooks.status`），本端点不重复判断，保持端点函数职责单一。
- 端点本身**不做业务判定**（不算触发率、不做裁判），只做"接收-映射-落库-唤醒"，业务判定留给被唤醒后继续跑的 LangGraph 节点——保持"薄 I/O 层，厚业务层"的分层。

## 3. 报告生成器：`benchmark.json` / HTML

### 3.1 `benchmark.json` Schema

```python
# src/skill_evaluate/observability/report_schema.py
class DimensionResult(BaseModel):
    dimension: str                    # "trigger_accuracy" | "context_scoping" | ... (与 03 NODE_BACKEND_ROUTING 键一致)
    status: JudgeVerdictStatus
    score: float | None = None         # 部分维度有量化分数（如加权覆盖率），部分只有 pass/fail
    findings: list[str] = Field(default_factory=list)   # 人类可读摘要，非结构化细节走各自专表
    blocking: bool                       # 是否构成流水线阻断（对应各维度的 Hard/Soft Fail 判定）

class BenchmarkReport(BaseModel):
    run_id: str
    skill_id: str
    skill_version_ref: str
    generated_at: datetime
    overall_status: JudgeVerdictStatus
    dimensions: list[DimensionResult]
    suite_version_id: str
    security_findings_summary: dict[SeverityLevel, int] = Field(default_factory=dict)
    coverage_summary: dict[str, float] = Field(default_factory=dict)  # 模块六/七/八产出
```

`BenchmarkReport` 是文档 02"跨模块交互速查表"之外**唯一允许聚合读取多个维度产出**的模型——它在流水线收尾节点（文档 24 主图的终节点）组装，从各维度已落库的 `JudgeVerdict`/`SecurityFinding`/`CapabilityTree` 读取摘要字段拼装而成，本身不重复存储明细，保持"单一数据源"原则。

### 3.2 生成器接口

```python
# src/skill_evaluate/observability/report_generator.py
class ReportGenerator:
    async def build(self, run_id: str) -> BenchmarkReport: ...
    def to_json(self, report: BenchmarkReport, out_path: str) -> None: ...
    def to_html(self, report: BenchmarkReport, out_path: str) -> None:
        """基于 Jinja2 模板渲染，模板放在 observability/templates/report.html.jinja；
        HTML 报告是给人看的归档制品，不承担阻断判断——阻断判断只看 JSON 里的 blocking 字段。"""
```

新增依赖 `jinja2>=3.1`（补入 01 文档依赖列表）。

### 3.3 CI 阻断判定规则

- `overall_status`：任一 `dimensions[].blocking == True` 且 `status == FAIL` → `overall_status = FAIL`，CLI 进程以非零退出码结束（供 GitHub Actions 直接依据退出码阻断合并，文档 24 落地具体 workflow）。
- 制品归档：`benchmark.json` + `report.html` 作为 CI Job 的 `actions/upload-artifact`（或对应 CI 平台的等价机制）产出，保留期由文档 24 CI 配置决定，本文档不做假设。

## 4. Langfuse 双写适配器

```python
# src/skill_evaluate/observability/langfuse_adapter.py
class LangfuseAdapter:
    """可选组件：未配置 SKILLEVAL_LANGFUSE_* 时整个适配器降级为 no-op，
    不影响 benchmark.json/HTML 主链路——可观测性增强能力不应成为流水线单点故障。"""

    def start_run_trace(self, run_id: str, skill_id: str) -> "LangfuseTraceHandle": ...

    def log_agent_call(self, trace_handle, agent_name: str, prompt: str, response: str,
                        model: str, usage: TimingCostMetrics) -> None:
        """对应 Generator/Judge/Optimizer/Validator/Analyzer/Attacker/Mini Agent 的每次 LLM 调用"""

    def log_execution_trace(self, trace_handle, trace: ExecutionTrace) -> None:
        """把 ExecutionTrace.actions 映射为 Langfuse 的嵌套 Span 序列，
        每个 ActionStep -> 一个 Span，thought 作为 span 的 metadata，
        stdout/stderr 经文档03第8节同样的截断规则后作为 span output"""
```

**双写触发点**：不要求每个 Agent/Node 都手写 `langfuse_adapter.log_xxx(...)` 调用——采用文档 07（Mini Agent 框架）与后续 Agent 基类中统一的"调用后钩子"模式，本文档只定义适配器接口和字段映射规则，具体挂载点在文档 06~10 的 Agent 基类中声明（见第 6 节待接入）。

**配置**：`LangfuseSettings`（新增到 `config.py`，追加式扩展）：

```python
class LangfuseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_LANGFUSE_")
    enabled: bool = False
    public_key: str | None = None
    secret_key: SecretStr = SecretStr("")
    host: str = "https://cloud.langfuse.com"
```

`enabled=False`（默认）时 `LangfuseAdapter` 全部方法为 no-op，本地开发/无 Langfuse 账号的 CI 环境不受影响。

## 5. 统一打点约定（结构化日志，衔接文档 01 第 6 节）

所有 Agent 调用与 LangGraph 节点必须打以下最小字段集（`structlog` bind 上下文）：

```python
logger.bind(run_id=..., skill_id=..., node_name=..., agent_name=..., case_id=None)
```

- 节点进入/退出各打一条 `info` 日志，包含 `duration_ms`。
- Agent 调用失败（LLM API 报错、超过重试次数）打 `error` 日志并附带原始异常类型（`SkillEvaluateError` 子类名）。
- **禁止**在日志中打印完整 `SKILL.md` 正文、完整 Prompt、API Key——本文档定义脱敏工具 `observability/log_sanitize.py`，对超过 500 字符的字符串字段自动截断，对匹配常见密钥格式的字符串自动打码，供 08 之后各 Agent 记录 reasoning 时复用（防止日志本身成为模块五所警惕的"敏感信息泄露"面）。

## 6. 待接入文档（本文档留给后续模块的接口清单）

| 预留位置 | 当前状态 | 由哪份文档接入 | 接入方式 |
|---|---|---|---|
| `hooks_approval.py` | 空文件占位 | `22` | 实现人工审批回调端点，复用 `security.py` 的签名校验模式（换一套 secret） |
| `DimensionResult` 各维度的 `score`/`findings`/`blocking` 具体写入 | 模型已定义，无生产者 | `11~20` | 各维度节点在完成判定后，调用 `ReportGenerator` 暴露的 `record_dimension_result()` 写入方法（本文档新增该方法，各维度文档负责传入正确的枚举键与内容） |
| `security_findings_summary` / `coverage_summary` 聚合逻辑 | 字段占位 | `15`（安全）、`16~18`（覆盖率） | `ReportGenerator.build()` 内部按 `SecurityFindingRepository`/`CapabilityRepository` 聚合统计，两份文档需保证落库时 `severity`/`tier` 字段可被正确 group by |
| Agent 基类的 Langfuse 钩子挂载点 | 适配器接口已定义，未挂载调用方 | `06~10` | 各 Agent 基类在 `_call_llm()` 内部统一调用 `langfuse_adapter.log_agent_call(...)`，具体基类设计见对应文档 |
| CI 制品保留期/上传方式 | 未定义 | `24` | 按所选 CI 平台（默认 GitHub Actions）配置 `upload-artifact` 参数 |

---

## 第 0 层收官说明

至此，「01 脚手架 → 02 State Schema → 03 执行引擎适配层 → 04 持久化 → 05 可观测性骨架」五份文档构成完整的工程地基：数据契约唯一、执行后端可插拔、状态可持久化恢复、结果可观测可归档。此前每份文档遗留的"待接入"项目，均已在本文档或前序文档中逐一收口（唯二真正留到后面的是：`llama_control` 异构后端本身的实现、以及人工审批端点本身的实现，这两者依赖尚未设计的业务模块，属于合理的延后）。

## 下一步

待你确认本文档后，我将进入 **第 1 层**，输出 **文档 06：Generator Agent 与测试集生命周期管理**——落实你要求的"初始化生成一次、默认复用、手动强制重生"机制。
