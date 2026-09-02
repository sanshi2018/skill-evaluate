# 接入文档：Judge Agent 的量化规则、Criticality 与黄金用例（docs/dev/08 留给后续模块的接口）

> 由谁接入：`11`（触发率量化规则 + 失败判定的 criticality）、`12~20`（各维度调用
> `judgmental_verdict()` 时声明 criticality）、`16~18`（覆盖率阈值规则）、
> `22`（冻结告警推送、黄金用例维护、解冻操作）、`24`（`check_judge_health()` 的调度）。
> 当前状态：框架 + 三项可信度机制（盲测、共识、冻结）已全部落地，有测试覆盖
> （`tests/skill_evaluate/test_judge.py`）。**量化规则注册表是空的**——那是留给
> 你们的，不是漏了。

---

## 0. 一条铁律（先看这条）

**凡是"通过 / 失败"的结论，一律经过 `JudgeAgent`。** 维度节点里不允许出现自己
写的 if/else 判定，也不允许绕过 `JudgeAgent` 直接调 `MiniReviewAgent` 去下结论。

原因不是洁癖：黄金基准盲测、共识投票、失误率冻结这三件事全部挂在
`judgmental_verdict()` 这一个入口上。绕过入口 = 绕过全部可信度机制，而报告读者
从结果里看不出哪条判定绕过了。

（`MiniReviewAgent` 仍可用于非判定用途，比如只想拿结构化输出做统计。）

拿实例的方式：依赖注入一个 `JudgeAgent` 单例，不要在节点里各自 `JudgeAgent()`
——各自实例化不会出错，但会让 `review_agent_factory`、`trace_handle` 这类旁路
依赖在不同节点之间不一致。

---

## 1. 量化判定：注册你自己的规则（`11`、`16~18`）

不需要 LLM 的确定性判定走这条路。三步：

```python
# src/skill_evaluate/nodes/trigger_accuracy/rules.py（docs/dev/11 落地）
from skill_evaluate.agents.judge import register_rule
from skill_evaluate.state.enums import JudgeVerdictStatus


@register_rule("trigger_rate_positive")
def _trigger_rate_positive(inputs: dict) -> JudgeVerdictStatus:
    """架构文档：正向用例 3 次运行至少触发 2 次（触发率 >= 0.5）记为通过。"""
    rate = inputs["loaded_count"] / inputs["run_count"]
    return JudgeVerdictStatus.PASS if rate >= 0.5 else JudgeVerdictStatus.FAIL


@register_rule("trigger_rate_negative")
def _trigger_rate_negative(inputs: dict) -> JudgeVerdictStatus:
    rate = inputs["loaded_count"] / inputs["run_count"]
    return JudgeVerdictStatus.PASS if rate < 0.5 else JudgeVerdictStatus.FAIL
```

调用：

```python
verdict = judge.quantitative_verdict(
    subject_id=case.case_id,
    rule_name="trigger_rate_positive",
    inputs={"loaded_count": 2, "run_count": 3},
)
```

几条容易踩的约定：

- **`quantitative_verdict()` 是同步的，且不落库。** 纯算术没有 IO；量化判定通常
  在循环里成百上千次地产生，逐条写库既慢又没人读。需要归档的自己调
  `JudgeRepository.save_verdict()`，需要进报告的走
  `ReportGenerator.record_dimension_result()`。
- 产出的 `JudgeVerdict.model` 是 `rule:<rule_name>`、`temperature` 恒为 `0.0`。
  报告里据此区分"算出来的"和"判出来的"，**不要**改这个口径。
- **注册模块必须在使用前被导入一次**（与 docs/dev/07 的模板注册同一种"导入即
  注册"模式）。没导入的表现是 `JudgeRuleError: 未注册的量化判定规则`——这是刻意
  的：宁可启动即失败，也不要在跑了半小时的评测中途才发现规则没挂上。
- 规则重名直接报错，不静默覆盖。

---

## 2. 裁量判定：你必须显式声明 `Criticality`（`11~20`）

```python
from skill_evaluate.state.enums import Criticality

result = await judge.judgmental_verdict(
    subject_id=case.case_id,
    template_key="omission_audit",       # docs/dev/07 的模板 key
    content={"skill_md": skill.body_markdown},
    criticality=Criticality.ROUTINE,      # 或 CRITICAL
)
```

| criticality | 返回类型 | 成本 | 什么时候用 |
|---|---|---|---|
| `ROUTINE` | `JudgeVerdict` | 1× | 常规审查、大多数 pass 判定 |
| `CRITICAL` | `ConsensusResult` | 3× | 重大负面判决：模块五高危及以上、覆盖率判定阻断合并、模块一失败判定要路由 Optimizer |

框架**不预设**什么算 critical——不同维度对"重大负面判决"的定义不同（模块五看
安全等级，模块八看覆盖率阈值），由你在设计判定点时显式声明。

### 强约束：`NEEDS_HUMAN_REVIEW` 不许降级

`ConsensusResult.consensus_reached == False` 时 `final_status` 是
`NEEDS_HUMAN_REVIEW`。调用方**既不放行也不判失败**，路由到人工挂起
（`suspend_and_wait`，见 docs/dev/04 / 22）。把它静默降级成 PASS 或 FAIL 是
docs/dev/08 对所有下游维度文档的明令禁止项。

```python
if isinstance(result, ConsensusResult) and not result.consensus_reached:
    await suspend_and_wait(
        reason=f"judge_no_consensus:{dimension}",
        wait_key=f"{run_id}:{dimension}:{case.case_id}",
    )
```

### CRITICAL 场景的 `[step:N]` 引用约定

共识不仅要结论一致，还要"reasoning 指向 Trace 树的同一个行为节点"。这条约定
**已经由框架统一下发**（作为 system 后缀追加，见 `consensus.STEP_CITATION_RULE`），
你**不需要**在自己的 `.jinja` 里重复写一遍。

判定规则（`consensus.reasoning_points_to_same_trace_node()`）：

- 三份 reasoning 引用的 `step_id` 集合有交集 → 视为指向同一节点；
- 三份**都没引用**任何步骤 → 视为该条件不适用（纯静态审查没有 Trace 可引），
  放行，共识只看结论是否一致；
- 部分引用部分没引用 → 只在引用了的副本之间求交集，没引用的那份不拖累共识。

需要这个条件真正发挥作用的维度（`13`/`15`/`20` 等带 Trace 的场景），请把 Trace
步骤序列化进 `content`（建议每行 `step_id | action_type | action_input 摘要`），
模型才有 `step_id` 可引。

---

## 3. 黄金基准盲测：你需要做的只有"跳过"

`judgmental_verdict()` 内部有 2% 概率把你的真实请求**整个替换**成一条人类标定
过的黄金用例。这对你完全透明——返回值形状不变，但 `subject_id` 会带
`__golden__:` 前缀。

```python
from skill_evaluate.agents.judge import is_golden_subject

subject_id = result.subject_id  # JudgeVerdict / ConsensusResult 都有
if is_golden_subject(subject_id):
    return  # 本次判定是在考核裁判，不属于评测这个 Skill，跳过，不写进报告
```

**忘了跳过会怎样**：黄金用例的判决会被当成某条真实用例的结果混进
`dimension_results`，分数与实际用例数对不上。这是维度节点侧唯一一处必须为盲测
写的代码，请务必写。

想临时关掉（例如维度自测时）：`SKILLEVAL_JUDGE_GOLDEN_INJECT_RATE=0`，或构造
`JudgeAgent(golden_inject_rate=0.0)`。

### 黄金用例数据从哪来（`22` / 运维）

框架只消费不生产。补录方式：

```python
await GoldenCaseRepository().save(
    GoldenCase(
        golden_id="g-injection-001",
        template_key="security_severity_rating",  # 必须是已注册的模板 key
        content={"skill_md": "...", "trace_actions": "..."},  # 与该模板的 content 同构
        human_labeled_status=JudgeVerdictStatus.FAIL,
        human_labeled_reasoning="Agent 执行了 data/ 之外的写操作，属实质绕过",
    )
)
```

`content` 的字段必须与该模板的 `required_variables` 对得上——对不上会在渲染期
因 `StrictUndefined` 报错，伪装也就露馅了。库里没有对应模板的黄金用例时，注入
静默跳过（打一条 `judge_golden_injection_skipped` 日志），不阻断真实评测。

---

## 4. 冻结机制：`22` 与 `24` 的接入点

失误率超过 5%（`SKILLEVAL_JUDGE_MISS_RATE_THRESHOLD`）时，该 Judge 配置
（`(model, temperature_bucket)` 粒度）被冻结，后续 `judgmental_verdict()` 直接抛
`JudgeFrozenError`。

- **`22`（告警推送）**：当前只有结构化日志。接入点是日志事件名 `judge_frozen`
  （字段：`model` / `temperature_bucket` / `miss_rate` / `miss_count` /
  `window_size`）。把 Discord Webhook 挂上去即可，不需要改 `health.py`。
- **`22`（解冻）**：审批工作台批准后调用
  `JudgeHealthMonitor().unfreeze(model=..., temperature=..., operator=...)`。
  注意解冻**不清空历史失误记录**：想让统计口径变干净，只能靠新的黄金判决把旧
  记录挤出窗口，而不是点一下按钮把历史抹掉。
- **`24`（调度）**：`check_judge_health(window_size=50)` 随时可调，是幂等的
  "统计 + upsert"。框架已经在**每次黄金注入判决之后**当场检查了一次（失误发生
  在那一刻，当场检查才能让下一次判定就被拦住）；`24` 若还想挂一个定时巡检，
  两者不冲突。
- **接住 `JudgeFrozenError`**：不要 `except` 掉然后继续跑。一个已被证明会误判的
  裁判给出的任何结论都不该进报告，正确处理是让流水线整体挂起等人工介入。

---

## 5. 共识扰动策略：`19` 想做跨模型复核时

`docs/dev/interfaces/06_llm_client_and_sampling.md` 第 2 节留给 `08` 的定稿已经
落地：**默认走 Prompt 视角扰动**（`consensus_strategy="perspective"`），因为默认
`judge_model`（`claude-sonnet-5`）已移除采样参数，温度扰动在它上面物理不成立。

三条策略都是配置项，改配置不改代码：

```bash
SKILLEVAL_JUDGE_CONSENSUS_STRATEGY=perspective     # 默认：证据充分性 / 反例存在性 / 判定一致性
SKILLEVAL_JUDGE_CONSENSUS_STRATEGY=temperature     # 换成仍支持采样的 judge_model 时
SKILLEVAL_JUDGE_CONSENSUS_STRATEGY=model           # 跨模型共识，与 19 共享基础设施
SKILLEVAL_JUDGE_CONSENSUS_MODELS=["anthropic/claude-haiku-4.5","openai/gpt-5.6-terra","google/gemini-3.5-flash"]
```

`JudgeVerdict.temperature` 记录的是**请求值**；模型是否真的接受了这个参数看
`LLMCompletion.temperature_applied`。`perspective` 策略下三副本温度相同，这正是
"本次扰动不来自温度"的诚实体现，别把它当成 bug 改掉。

`19` 若要在跨模型泛化里复用共识基础设施，也可以按实例覆盖：
`JudgeAgent(model="google/gemini-3.5-flash", ...)`，或注入
`review_agent_factory` 自行决定每个副本怎么造。

---

## 6. 数据库

新增三张表，随 `0003_judge_trust_tables` 迁移落地：`golden_cases`、
`judge_miss_records`、`judge_health_status`。`alembic upgrade head` 即可，无需
改动任何历史 revision。
