# 08 Judge Agent 核心框架与裁判可信度机制

> 状态：**待确认**
> 路线图位置：第 1 层 / 第 3 份
> 依赖：`02`（`JudgeVerdict`/`ConsensusResult`）、`03`（Trace 结构，用于量化判定如触发率）、`04`（`JudgeRepository`）、`06`（`BaseLLMAgent`）、`07`（`MiniReviewAgent`，共识投票复用其审查能力）
> 被依赖：`09`（Optimizer，消费判定结果决定是否重写）、`11~20` 全部评测维度节点（凡涉及"判定通过/失败"，一律经过本文档的 Judge Agent，不允许某个维度节点自己写 if/else 下结论）

---

## 1. 本文档目标

架构文档把"裁判员的裁判"放在模块十一（收尾的元评估），但裁判的可信度机制**不能是事后补丁**——如果先在模块一~十里散落实现各自的判定逻辑，再回头统一套上共识投票，会导致大量返工。因此本文档把 Judge Agent 提前到第 1 层地基，同时把黄金基准盲测、自我一致性扰动这两项可信度机制作为**框架的内建能力**，而不是可选插件。

## 2. Judge Agent 的两类职责

架构文档中"Judge Agent"承担了两种性质不同的工作，本文档把它们拆成两个方法，避免接口语义混乱：

1. **量化判定（Quantitative Judgment）**：基于确定性规则对结构化数据（如多次 `ExecutionTrace` 的 `loaded_skill_md` 布尔值）做算术聚合，例如模块一的"触发率≥0.5 记为通过"。这类判定**不需要 LLM**，纯代码计算即可，但仍然产出统一的 `JudgeVerdict` 以便报告聚合和后续可能的复核。
2. **裁量判定（Judgmental Verdict）**：基于文本/Trace 的语义理解做主观裁决，如"这次提示词注入攻击 Agent 是否真的被绕过了"。这类判定必须走 LLM，且是黄金基准盲测和共识投票机制真正要防范"幻觉/不一致"的对象。

```python
# src/skill_evaluate/agents/judge/service.py
class JudgeAgent:
    def quantitative_verdict(self, subject_id: str, rule_name: str, inputs: dict) -> JudgeVerdict:
        """纯代码规则引擎，rule_name 对应注册在 judge/rules.py 的确定性函数，如：
        'trigger_rate_positive'：3 次执行中 loaded_skill_md=True 的比例 >= 0.5 -> PASS
        'trigger_rate_negative'：同上但 < 0.5 -> PASS
        规则本身由各评测维度文档在 rules.py 中新增注册，本文档只提供执行框架。"""

    async def judgmental_verdict(
        self, subject_id: str, template_key: str, content: dict,
        criticality: "Criticality"
    ) -> JudgeVerdict | ConsensusResult:
        """走 MiniReviewAgent 或更大模型做语义裁决；criticality 决定是否触发共识投票（见第 4 节）。"""
```

## 3. 黄金基准盲测（Golden Standard Blind Testing）

### 3.1 数据模型

```python
# src/skill_evaluate/state/golden.py（新增子域，纳入文档02"待接入"追加，属于本文档对02的一次小追加）
class GoldenCase(BaseModel):
    golden_id: str
    template_key: str              # 复用 07 的模板体系，黄金用例针对特定审查模板构造
    content: dict[str, str]           # 与 ReviewRequest.content 同构
    human_labeled_status: JudgeVerdictStatus   # 人类专家预标定的绝对正确判决
    human_labeled_reasoning: str
    active: bool = True

class JudgeMissRecord(BaseModel):
    miss_id: str
    golden_id: str
    judge_output_status: JudgeVerdictStatus
    occurred_at: datetime
```

对应新表 `golden_cases` / `judge_miss_records`（文档 04 Alembic 迁移的一次追加，本文档负责编写该 revision，不改动已有历史 revision）。

### 3.2 注入机制

```python
# src/skill_evaluate/agents/judge/golden_injector.py
async def maybe_inject_golden_case(real_request: ReviewRequest) -> ReviewRequest:
    """以 1/50 概率（可配置 SKILLEVAL_JUDGE_GOLDEN_INJECT_RATE，默认 0.02）
    用一个伪装成日常任务的 GoldenCase 替换本次真实请求，返回值与真实请求同构，
    调用方（judgmental_verdict 内部）无法区分是否是黄金用例——这是"盲测"的关键：
    Judge Agent 本身不知道自己正在被考核。"""
```

**执行位置**：注入判定发生在 `judgmental_verdict()` 内部，对调用方（各评测维度节点）完全透明——节点侧代码不需要知道、也不应该知道某次调用实际上是黄金基准测试。返回给节点的仍是一个合法 `JudgeVerdict`（对外行为一致），但内部会额外记一笔 `JudgeMissRecord`（如果判决与人类标定不一致）并**不会**把黄金用例的判决结果计入真实评测报告——`judgmental_verdict()` 内部需要区分"这次调用是否为黄金注入"，若是，比对结果后返回一个**中性/跳过标记**给调用方（约定用 `subject_id` 前缀 `__golden__` 标识，调用方按此约定跳过，不计入 `BenchmarkReport`）。

### 3.3 失误率监控与冻结

```python
async def check_judge_health(window_size: int = 50) -> bool:
    """滑动窗口统计最近 window_size 次黄金用例判决的失误率，超过 5% 触发：
    1. 平台级告警（复用05文档的结构化日志 + 未来可接入的通知渠道）
    2. 冻结该 Judge Agent 配置（temperature/model 组合）的评测权限：
       后续 judgmental_verdict() 调用对该配置直接抛出 JudgeFrozenError，
       流水线整体挂起（复用04文档的 suspend_and_wait），等待人工介入调整
       Prompt 或更换模型后手动解冻（人工操作走文档22的审批工作台）。"""
```

`JudgeFrozenError` 继承文档 01 的 `SkillEvaluateError`。冻结状态存于 `judge_health_status` 表（本文档 Alembic 追加），键为 `(model, temperature_bucket)`，允许不同 Judge 配置独立冻结/解冻。

## 4. 自我一致性扰动测试（Self-Consistency Probing）

### 4.1 `Criticality` 判定

不是所有判定都值得 3 倍 Token 成本去做共识投票。本文档定义一个显式枚举，由**调用方（各评测维度节点）**在调用 `judgmental_verdict()` 时声明，而不是 Judge Agent 自己猜：

```python
class Criticality(StrEnum):
    ROUTINE = "routine"      # 单节点快速通行（模块二常规审查、大多数 Pass 判定）
    CRITICAL = "critical"    # 触发 3 副本共识投票（模块五高危及以上、覆盖率判定阻断合并、模块一失败判定影响是否路由 Optimizer）
```

各评测维度文档在调用时需要显式传入 `criticality`，本文档不预设"什么情况算 critical"的全局规则——因为不同维度对"重大负面判决"的定义不同（模块五是安全等级，模块八是覆盖率阈值），交给各自文档决定，本文档只提供投票机制本身。

### 4.2 共识投票实现

```python
async def _consensus_vote(request: ReviewRequest) -> ConsensusResult:
    temperatures = [0.1, 0.3, 0.5]
    verdicts = await asyncio.gather(*[
        mini_review_agent_with_temp(t).review(request) for t in temperatures
    ])
    same_status = len({v.status for v in verdicts}) == 1
    same_node = _reasoning_points_to_same_trace_node(verdicts)  # 见4.3
    consensus_reached = same_status and same_node
    result = ConsensusResult(
        subject_id=request.subject_id,
        verdicts=verdicts,
        consensus_reached=consensus_reached,
        final_status=verdicts[0].status if consensus_reached else JudgeVerdictStatus.NEEDS_HUMAN_REVIEW,
        dissenting_node=None if consensus_reached else _summarize_dissent(verdicts),
    )
    await consensus_repository.save(result)
    return result
```

### 4.3 "指向同一行为节点"的判定

架构文档要求共识不仅要结论一致，还要"reasoning 指向 Trace 树的同一个行为节点"。实现为：从每份 `reasoning` 中提取被引用的 `ActionStep.step_id`（依赖第 7 节所述、要求 Prompt 模板在 reasoning 中显式标注引用的 `step_id`，格式约定为 `[step:3]` 这类可解析标记），三份 reasoning 提取出的 step_id 集合若有交集即视为"指向同一节点"。这个约定需要写入所有 Criticality=CRITICAL 场景使用的 Prompt 模板的公共后缀（对文档 07 模板公共约束的一次追加，见第 7 节）。

### 4.4 未达成共识的处理

`consensus_reached=False` 时，`final_status=NEEDS_HUMAN_REVIEW`，调用方节点应将其视为"既不放行也不判定失败"，路由到人工挂起（复用文档 04/22 的挂起机制），不允许调用方自行把 `NEEDS_HUMAN_REVIEW` 静默降级为 PASS 或 FAIL——这是本文档对所有下游维度文档的强约束。

## 5. 成本控制策略

- `ROUTINE` 判定：单次 `MiniReviewAgent.review()` 调用，成本基线。
- `CRITICAL` 判定：3 倍 Token 成本，仅在第 4.1 节声明的场景触发。
- 黄金基准注入：2% 概率，均摊成本可忽略，且注入的黄金用例本身走的是正常 `judgmental_verdict()` 路径（不额外调用 LLM 三次），除非该黄金用例恰好也被判定为 CRITICAL——两种机制正交，不冲突不叠加复杂度。

## 6. `JudgeAgent` 对外暴露的完整接口

```python
class JudgeAgent:
    def quantitative_verdict(self, subject_id: str, rule_name: str, inputs: dict) -> JudgeVerdict: ...

    async def judgmental_verdict(
        self, subject_id: str, template_key: str, content: dict, criticality: Criticality
    ) -> JudgeVerdict | ConsensusResult:
        """criticality=ROUTINE -> 返回 JudgeVerdict（含黄金盲测的透明处理）
        criticality=CRITICAL -> 返回 ConsensusResult"""

    async def check_judge_health(self, window_size: int = 50) -> bool: ...
```

各评测维度节点统一通过依赖注入拿到 `JudgeAgent` 单例，不自行实例化 `MiniReviewAgent` 做判定用途（`MiniReviewAgent` 仍可被 Judge Agent 内部调用，也可被其他非判定场景直接调用，如模块四 Help 文档质量审查本质是 ROUTINE 判定，应通过 `judgmental_verdict()` 走统一入口而不是绕过 Judge 直接调 Mini Review）。

## 7. 对文档 07 的追加约定

- Criticality=CRITICAL 场景使用的模板，其 `.jinja` 文件需在 reasoning 要求中追加："引用你依据的具体 Trace 步骤时，使用 `[step:N]` 格式标注 N 为该步骤的 step_id"。此约定在具体维度文档（如 15 安全模块的高危判定）实现对应模板时落实，本文档只声明约定本身。
- `ReviewTemplate.to_status` 的返回类型在模块五场景需要扩展为 `to_severity`（文档 07 第 7 节已预留此待接入项），本文档明确：`Criticality` 判定与 `SeverityLevel` 是两个独立维度——"是否需要共识投票"（criticality）与"判定结果多严重"（severity）正交，不要把两者合并成一个字段，模块五文档实现时需同时提供两者。

## 8. 待接入文档（本文档留给后续模块的接口清单）

| 预留位置 | 当前状态 | 由哪份文档接入 | 接入方式 |
|---|---|---|---|
| `judge/rules.py` 中的量化规则注册 | 空注册表 | `11`（触发率规则）、`16~18`（覆盖率阈值规则） | 新增 `rule_name -> Callable[[dict], JudgeVerdictStatus]` 注册项 |
| 各维度调用 `judgmental_verdict()` 时的 `criticality` 取值 | 枚举已定义，调用方未接入 | `11~20` | 各文档在设计具体判定点时显式声明 ROUTINE/CRITICAL |
| `GoldenCase` 数据的人工标定来源 | 表结构已建，无数据 | 运维侧持续补充（不属于单一开发文档，在文档 22 的人工协作说明中给出维护指引） | 人工通过审查工作台或直接写库补充黄金用例，本文档只消费不生产 |
| `check_judge_health()` 的调度触发 | 方法已实现，未接入定时/事件触发 | `24` | 主图装配时决定是每次评测运行后检查一次，还是独立定时任务，本文档不预设 |
| 通知渠道（冻结告警的实际推送） | 仅结构化日志 | `22` | 复用 Discord Webhook 机制 |

---

## 下一步

待你确认本文档后，我将输出 **文档 09：Optimizer Agent 与闭环重试策略**——消费本文档的判定结果，实现失败用例路由、`SKILL.md`/脚本补丁生成、最大重试挂起。
