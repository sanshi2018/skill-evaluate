# 07 Mini Agent 评审框架

> 状态：**待确认**
> 路线图位置：第 1 层 / 第 2 份
> 依赖：`02`（数据契约）、`03`（`MiniAgentBackend`，注意与本文档的关系见第 2 节）、`06`（`BaseLLMAgent` 基类复用）
> 被依赖：`12`（模块二，静态审查主力）、`14`（模块四，Help 文档质量/建设性报错审查）、`13`（模块三，控制标定审查）、`19`（模块九，语言坏味道 Linter）、`08`（Judge Agent 复用本框架的低温审查能力作为共识投票的基础评审单元）

---

## 1. 本文档目标

封装一个**通用、可配置、输出结构化**的低温文本评审能力（Temperature=0.1），供架构文档中散落在模块二/三/四/九的多处"扮演苛刻审查员"场景复用。目标是：新增一种审查维度时，只需要新增一份 Prompt 模板 + 一个输出 Schema，不需要新写调用逻辑。

## 2. 与 `MiniAgentBackend`（文档 03）的关系澄清

两者容易混淆，必须在此明确边界：

- **`MiniAgentBackend`**（文档 03，`executors/` 包）：`ExecutorBackend` 接口的一个实现，职责是"把一次 LLM 调用包装成合法的 `ExecutionTrace`"，服务于**评测维度节点选择执行后端**这一层。
- **`MiniReviewAgent`**（本文档，`agents/mini/` 包）：一个**业务智能体**，职责是"对给定文本内容做结构化审查并给出 JSON 判定"，服务于**具体审查逻辑**这一层。

关系是：`MiniReviewAgent` 在需要产出 `ExecutionTrace`（例如要挂到统一报告的执行记录里）时，可以借助 `MiniAgentBackend` 包装其调用；但更多时候（如模块二的纯审查），`MiniReviewAgent` 直接产出 `JudgeVerdict`，不需要经过 `ExecutionTrace` 这一层。**简单说：`MiniAgentBackend` 是"用什么后端跑"，`MiniReviewAgent` 是"跑什么审查逻辑"，两者正交。**

## 3. `MiniReviewAgent` 核心接口

```python
# src/skill_evaluate/agents/mini/service.py
from skill_evaluate.agents.base import BaseLLMAgent


class ReviewRequest(BaseModel):
    subject_id: str                  # 落到 JudgeVerdict.subject_id
    template_key: str                 # 见第 4 节模板注册表
    content: dict[str, str]            # 模板变量，如 {"skill_md": "...", "help_output": "..."}


class MiniReviewAgent(BaseLLMAgent):
    def __init__(self, model: str = settings.llm.mini_agent_model, temperature: float = 0.1, **kw):
        super().__init__(model=model, temperature=temperature, **kw)

    async def review(self, request: ReviewRequest) -> JudgeVerdict:
        template = REVIEW_TEMPLATE_REGISTRY[request.template_key]
        prompt = template.render(request.content)
        raw = await self._call_llm(prompt, response_schema=template.output_schema)
        verdict = JudgeVerdict(
            verdict_id=new_id(),
            subject_id=request.subject_id,
            status=template.to_status(raw),          # 每个模板自带"如何把结构化输出映射为 pass/fail"的规则
            reasoning=raw.reasoning,
            temperature=self.temperature,
            model=self.model,
            created_at=utcnow(),
        )
        await judge_repository.save(verdict)   # 统一落库，报告生成器（05）可直接读取
        return verdict
```

**设计取舍**：`review()` 直接返回并落库 `JudgeVerdict`，而不是返回一个"审查专用的临时结构再转换"。理由：模块二这类纯静态审查场景不需要文档 08 的多副本共识机制（那是留给"高危/低覆盖率"这类重大负面判定的），`MiniReviewAgent` 的单次输出就是最终判定，直接落一份 `JudgeVerdict` 记录既满足报告聚合需要，又不引入不必要的中间态。若某个审查场景后续被要求升级为需要共识投票（例如模块九的语言坏味道审查如果被认为足够重要），文档 08 会在其共识流程内部**多次调用同一个 `MiniReviewAgent.review()`**（不同 temperature），而不是让 `MiniReviewAgent` 自己实现投票逻辑——投票是 Judge Agent 的职责，评审模板执行是 Mini Agent 的职责，分层不重叠。

## 4. 模板注册表设计

```python
# src/skill_evaluate/agents/mini/templates/registry.py
class ReviewTemplate(BaseModel, arbitrary_types_allowed=True):
    key: str
    prompt_path: str                  # jinja 模板文件路径
    output_schema: type[BaseModel]      # 该模板要求 LLM 输出的结构化 schema
    to_status: Callable[[BaseModel], JudgeVerdictStatus]  # 结构化输出 -> pass/fail 的映射函数

REVIEW_TEMPLATE_REGISTRY: dict[str, ReviewTemplate] = {}

def register_template(template: ReviewTemplate) -> None:
    REVIEW_TEMPLATE_REGISTRY[template.key] = template
```

每个后续文档新增一种审查场景时，在 `agents/mini/templates/` 下新增一个 `.jinja` 文件 + 一个输出 Schema 类 + 一次 `register_template()` 调用，不改动 `MiniReviewAgent` 本体。

## 5. 首批模板（本文档预置，供文档 12/14/19 直接复用；具体审查阈值/裁定细节由各自文档定稿，此处只固化输出结构）

### 5.1 常识剥离度审计（Omission Audit，模块二）

```python
class OmissionAuditOutput(BaseModel):
    reasoning: str
    common_sense_statements: list[str]   # 被判定为"常识"的原文片段
    verdict: str   # "pass" | "fail"

register_template(ReviewTemplate(
    key="omission_audit",
    prompt_path="templates/omission_audit.jinja",
    output_schema=OmissionAuditOutput,
    to_status=lambda o: JudgeVerdictStatus.PASS if o.verdict == "pass" else JudgeVerdictStatus.FAIL,
))
```

### 5.2 范围连贯性审查（Scoping Check，模块二）

```python
class ScopingCheckOutput(BaseModel):
    reasoning: str
    scope_issue: str | None   # None | "too_broad" | "too_fragmented"
    verdict: str
```

### 5.3 渐进式披露触发条件审查（模块二，静态版；模块三另有动态探查版，属于 `PLUGGABLE` 后端场景，不在本文档范围）

```python
class ProgressiveDisclosureStaticOutput(BaseModel):
    reasoning: str
    reference_files_without_trigger_condition: list[str]
    verdict: str
```

### 5.4 Help 文档质量审查（模块四）

```python
class HelpDocQualityOutput(BaseModel):
    reasoning: str
    lists_all_flags: bool
    documents_env_vars: bool
    has_usage_example: bool
    verdict: str
```

### 5.5 建设性报错审查（模块四）

```python
class ConstructiveErrorOutput(BaseModel):
    reasoning: str
    states_what_went_wrong: bool
    states_expected_input: bool
    states_next_action: bool
    verdict: str
```

### 5.6 语言坏味道审查（模块九 Linguistic Smell Check）

```python
class LinguisticSmellOutput(BaseModel):
    reasoning: str
    overuse_of_caps_lock_emphasis: bool
    model_specific_incantations: list[str]   # 检测到的"讨好特定模型"话术片段
    tone_issue: str | None   # None | "intimidating" | "sycophantic"
    verdict: str
```

### 5.7 刚性/柔性控制标定（模块三 Control Calibration）

```python
class ControlCalibrationOutput(BaseModel):
    reasoning: str
    task_fragility: str   # "fragile" | "tolerant"
    control_style_observed: str   # "rigid" | "flexible" | "mismatched"
    has_default_recommendation: bool   # 是否避免了"甩出等价工具菜单"
    has_checklist_or_plan_verify_loop: bool
    verdict: str
```

**说明**：以上 7 个模板覆盖模块二全部、模块四两项、模块三一项、模块九一项，是本文档预置的"首批"，其余分散在各模块文档中的审查点（如模块五的严重性定级、模块八的能力/权重提取本质上是"提取"而非"审查"）不在本文档预置范围，由各自文档按第 4 节的注册机制自行新增模板，不需要修改本文档。

## 6. Prompt 模板的共同约束（写入所有 `.jinja` 文件的公共 system 前缀）

- 输出必须是严格 JSON，禁止 Markdown 代码块包裹以外的任何自然语言前后缀（配合 `_call_llm()` 的结构化解析）。
- 明确要求"如果证据不足，倾向于给出较宽容的判定并在 reasoning 中说明不确定性"——这是对架构文档"静态审查可能纸上谈兵"这一权衡的直接回应：宁可漏判也不误判，把边界情况的最终裁量权交还给下游的人工审核或多轮共识机制，而不是让 Mini Agent 用一次性输出武断下结论。
- `reasoning` 字段强制要求引用被评审文本的具体原文片段（而非泛泛而谈），便于后续报告展示和人工复核时快速定位。

## 7. 待接入文档（本文档留给后续模块的接口清单）

| 预留位置 | 当前状态 | 由哪份文档接入 | 接入方式 |
|---|---|---|---|
| 7 个首批模板的具体 `.jinja` 内容与判定阈值细节 | 输出 Schema 与注册骨架已定义 | `12`（5.1~5.3）、`14`（5.4~5.5）、`19`（5.6）、`13`（5.7） | 各文档编写具体 Prompt 措辞与 `to_status` 判定细则，不改变 Schema 字段 |
| 模块三"动态渐进式披露探查"模板 | 未预置（该场景需要 `PLUGGABLE` 后端配合 Trace，不是纯文本审查） | `13` | 新增模板并结合 `ExecutionTrace.actions` 一并作为 review content 传入 |
| 模块五严重性定级模板 | 未预置 | `15` | 新增 `security_severity_rating` 模板，`to_status` 映射到 `SeverityLevel` 而非 `JudgeVerdictStatus`（需要在 `ReviewTemplate` 泛化 `to_status` 的返回类型，或新增并行的 `to_severity` 字段，由 15 文档决定具体扩展方式并在此说明变更） |
| Judge Agent 多副本调用 `MiniReviewAgent.review()` 的编排逻辑 | 接口已备好（同一 `review()` 可被多次调用） | `08` | Judge 框架内部对同一 `ReviewRequest` 以不同 temperature 并发调用 3 次，聚合为 `ConsensusResult` |

---

## 下一步

待你确认本文档后，我将输出 **文档 08：Judge Agent 核心框架与裁判可信度机制**——把架构文档模块十一子节点一的"裁判员的裁判"提前做成地基，供后续所有需要重大判定的评测维度复用。
