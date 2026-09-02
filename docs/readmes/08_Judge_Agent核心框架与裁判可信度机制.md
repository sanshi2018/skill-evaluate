# 实现说明：08 Judge Agent 核心框架与裁判可信度机制

> 对应设计文档：`docs/dev/08_Judge_Agent核心框架与裁判可信度机制.md`
> 状态：已实现（判定框架 + 黄金基准盲测 + 3 副本共识 + 失误率冻结全部落地）

复用 `docs/dev/06` 的 `BaseLLMAgent` 底座与 `docs/dev/07` 的评审模板体系，两者的
说明见 [`06`](./06_Generator_Agent与测试集生命周期管理.md)、
[`07`](./07_Mini_Agent评审框架.md)。

---

## 1. 交付了什么

| 文件 | 内容 | 对应设计文档章节 |
|---|---|---|
| `agents/judge/service.py` | `JudgeAgent`：`quantitative_verdict()` / `judgmental_verdict()` / `check_judge_health()` | 第 2、6 节 |
| `agents/judge/rules.py` | 量化规则注册表 `register_rule()` / `get_rule()`（**空表，留给 11/16~18**） | 第 2 节 |
| `agents/judge/golden_injector.py` | `maybe_inject_golden_case()`、`__golden__:` 前缀约定、`is_golden_subject()` | 第 3.2 节 |
| `agents/judge/consensus.py` | 3 副本扰动策略、`[step:N]` 解析与一致性判定、共识归并 | 第 4 节 |
| `agents/judge/health.py` | 失误率滑动窗口、冻结/解冻、`check_judge_health()` | 第 3.3 节 |
| `state/golden.py` | `GoldenCase` / `JudgeMissRecord` | 第 3.1 节 |
| `state/enums.py::Criticality` | `ROUTINE` / `CRITICAL` | 第 4.1 节 |
| `errors.py` | `JudgeFrozenError`、`JudgeRuleError` | 第 3.3 节 |
| `config.py::JudgeSettings` | 注入概率、失误率阈值、窗口大小、共识策略 | 第 3.2、4 节 |
| `persistence/` | `golden_cases` / `judge_miss_records` / `judge_health_status` 三表 + revision `0003` | 第 3.1、3.3 节 |

---

## 2. 用什么方式实现了需求

### 2.1 一个入口，三重机制

设计文档最核心的一句是"凡涉及判定通过/失败，一律经过 Judge Agent，不允许某个
维度节点自己写 if/else 下结论"。实现上把这句话变成了**结构性事实**而不是纪律
要求：黄金盲测、共识投票、冻结检查三件事全部挂在 `judgmental_verdict()` 内部，
绕过入口就等于绕过全部可信度机制。

### 2.2 量化判定与裁量判定：两个方法，两种性质

`quantitative_verdict()` 是**同步的、不调 LLM、不落库**的：

- 同步——纯算术没有 IO，硬套 async 只会逼调用方在非 async 上下文里绕路；
- 不落库——量化判定在循环里成百上千次地产生，逐条写库既慢又没人读；
- `model` 字段填 `rule:<rule_name>`、`temperature` 恒为 `0.0`，报告里一眼能看出
  这条判定不是模型给的。

规则注册表**故意是空的**。设计文档第 8 节说得很清楚：规则由 11（触发率）、
16~18（覆盖率阈值）各自注册。框架层预置一条 `trigger_rate_positive` 看起来贴心，
实际是替还没写的文档决定了"多少算通过"，而且会让 11 接入时撞上"重复注册"。

### 2.3 黄金基准盲测：对内可见，对外透明

注入点在 `judgmental_verdict()` 内部，被替换后的请求与真实请求**同构**——同一个
模板、同一条模型通道、Prompt 里没有任何"这是考题"的痕迹。

一处与设计文档字面不同的取舍：`maybe_inject_golden_case()` 返回的是
`GoldenInjection(request, golden)` 而不是裸 `ReviewRequest`。文档说"调用方无法
区分"，指的是**外部调用方**（维度节点）；`judgmental_verdict()` 内部必须知道这次
是不是黄金注入，否则无从比对人类标定、无从记账。把这个事实放进返回值类型，比
让内部靠 `subject_id` 前缀反解更直白，也让"谁能看见这个事实"一目了然。

对外的透明性靠 `__golden__:` 前缀维持：返回值形状不变，维度节点用
`is_golden_subject()` 识别并跳过。这是维度节点侧唯一需要为盲测写的代码。

### 2.4 共识扰动：把"温度扰动"换成"视角扰动"

`docs/dev/interfaces/06_llm_client_and_sampling.md` 第 2 节把这个决策留给了本
文档：新一代 Claude 模型已移除采样参数，默认 `judge_model` 上"3 副本温度扰动"
物理不成立。

**定稿：默认走 Prompt 视角扰动，另外两条（温度、跨模型）保留为配置项。**

选它不只是因为温度用不了。温度扰动检验的是"同一个裁判掷三次骰子会不会掷出不同
结果"；视角扰动是让三个裁判分别从**证据充分性 / 反例存在性 / 判定一致性**切入
同一份材料——三个角度看完都得出同一结论，更接近架构文档说的"高置信度"。

副本差异通过 `MiniReviewAgent(system_suffix=...)` 注入（对 `docs/dev/07` 的一次
追加式改动，默认 `None`，不影响任何既有调用）。**没有**塞进 `content`：`content`
是各模板自己的契约，往里塞一个只有部分模板会渲染的键，会让扰动在另一部分模板上
悄悄失效。

`JudgeVerdict.temperature` 记录**请求值**；是否真的下发看
`LLMCompletion.temperature_applied`。视角扰动下三副本温度相同——这正是"本次扰动
不来自温度"的诚实体现，不是 bug。

三副本用 `asyncio.gather` 并发，这也是"背靠背独立"的实现方式：串行执行时很容易
被后来者写成"参考上一份判决"，那就不是独立复核而是自我确认了。

### 2.5 `[step:N]` 一致性：两处边界写死在框架里

架构文档要求共识不仅结论一致，还要 reasoning "指向 Trace 树的同一个行为节点"。
实现为"引用的 step_id 集合有交集"，两处边界统一处理（免得各维度各猜一套）：

- **三份都没引用步骤** → 视为该条件不适用，放行。纯静态文本审查根本没有 Trace
  可引，把"无法引用"判成"没有共识"会让模块二这类维度永远拿不到 CRITICAL 判定。
- **部分引用部分没引用** → 只在引用了的副本之间求交集，没引用的那份不拖累共识。

`[step:N]` 的书写要求由框架统一作为 system 后缀下发（`STEP_CITATION_RULE`），
各模板**不需要**在 `.jinja` 里各写一遍——设计文档第 7 节声明了这条约定，落实点
只有一个才不会漂移。

### 2.6 冻结：粒度、时机与"为什么不降级"

- **粒度是 `(model, temperature_bucket)`**：换模型或换温度档就是另一个裁判，不该
  被别的配置连坐；反过来也不该靠"换个 subject 再试"绕过。
- **不支持采样的模型分桶为 `n/a`**：此时所有请求温度物理同档，硬按请求值分桶会把
  同一个裁判拆成三个统计口径，每个口径样本数都不够触发阈值，冻结机制形同虚设。
- **检查时机**：每次黄金注入判决后当场检查一次。失误发生在那一刻，当场检查才能
  让**下一次**判定就被拦住；`24` 想再挂定时巡检也不冲突（`check()` 是幂等的）。
- **命中与失误都记账**，否则失误率没有分母。
- **冻结而不是降级**：一个已被统计证明会误判的裁判，它的任何结论都不该进报告。
  正确行为是整体挂起等人工介入，而不是"那就当它 PASS 吧"跑完——后者产出的是一份
  看起来正常、实际毫无公信力的报告。

---

## 3. 与设计文档的差异 / 必要补充

| 位置 | 差异 | 原因 |
|---|---|---|
| `maybe_inject_golden_case()` 返回值 | 返回 `GoldenInjection` 而非裸 `ReviewRequest` | 见 2.3：内部必须能区分，外部透明性由 `__golden__:` 前缀维持 |
| `JudgeAgent` 不继承 `BaseLLMAgent` | interfaces/06 的样例是继承 | 裁量判定的 Prompt 就是 `07` 的评审模板，模板执行是 Mini Agent 的职责。再继承一次 LLM 底座会凭空多出一条"Judge 自己的 Prompt"，与模板注册表分叉。本类是纯编排，自己一个 Prompt 都不写 |
| `JudgeMissRecord` 增加 `model`/`temperature`/`is_miss` | 设计文档只列了 4 个字段 | 失误率是按 `(model, temperature_bucket)` 分别统计并分别冻结的，不记配置就无法定位该冻结谁；`is_miss` 用来保留分母 |
| `Criticality` / `PatchType` 定义在 `state/enums.py` | 文档写在各自子域文件里 | 项目约定全局枚举只有 `enums.py` 一个落点；`state/golden.py`、`state/patch.py` 原样再导出，文档里的 import 路径照常可用 |
| 共识策略可配置 | 文档只描述温度扰动 | 见 2.4；三条方案都做成配置项，换 `judge_model` 时不必改代码 |
| 新增 `JudgeRuleError` | 文档未提 | 与 `ReviewTemplateError` 对称。规则找不到就默认放行，等于把一个评测维度悄悄关掉 |

---

## 4. 已知留白（刻意，非偏差）

- **量化规则注册表为空**：由 `11`、`16~18` 注册（设计文档第 8 节）。
- **`golden_cases` 表无数据**：框架只消费不生产，由运维/资深工程师通过审查工作台
  或直接写库补充（设计文档第 8 节）。没有黄金用例时注入静默跳过，不阻断评测。
- **冻结告警只有结构化日志**：Discord Webhook 推送由 `22` 接入，接入点是
  `judge_frozen` 这条日志事件。
- **`check_judge_health()` 的定时调度**：由 `24` 决定（设计文档明确不预设）。

---

## 5. 如何验证

```bash
pytest tests/skill_evaluate/test_judge.py -q
```

25 项覆盖：

- **量化规则**：注册表初始不含维度规则、注册后可用且 `model=rule:*`、重名被拒、
  未知规则不静默放行。
- **共识**：`[step:N]` 解析、交集/无交集/全空/部分引用四种情形、结论分歧与
  "结论一致但依据不同"都判为未达成共识、三种扰动策略的构造与错误配置的拒绝。
- **调度**：ROUTINE 起 1 个副本、CRITICAL 起 3 个副本且三份 verdict + 共识结果
  都落库、冻结配置下一个请求都不发出去。
- **盲测**：注入后送给模型的是黄金内容、`subject_id` 带前缀、命中也记账、判决与
  人类标定不符记为失误并当场触发健康检查、`rate=0` 时不查库不注入。
- **健康**：分桶口径（含 `n/a`）、空窗口视为健康、超阈值冻结并给出原因、
  低于阈值保持健康、解冻不清空历史。

全量基线：`ruff check src tests` / `mypy src`（86 files）/ `pytest`（145 passed）
均通过。

> 本地跑测试需要 `pytest-asyncio`（在 `pyproject.toml` 的 dev 组里）。只装了运行期
> 依赖的环境会把所有 async 用例报成 "async def functions are not natively
> supported"，这与本次改动无关。

---

## 6. 待接入 / 下一步

- `docs/dev/interfaces/08_judge_rules_and_criticality.md` —— 给 `11~20` 的接入
  清单：怎么注册量化规则、怎么声明 `Criticality`、**必须写的那行 `is_golden_subject()`
  跳过**、`NEEDS_HUMAN_REVIEW` 不许降级、`22`/`24` 的冻结与调度接入点。
- 顺带更新了 `docs/dev/interfaces/06_llm_client_and_sampling.md` 第 2 节（记录本
  文档对温度扰动的定稿）与 `07_review_template_registry.md` 第 5 节（`system_suffix`
  的实际用法）。
- 迁移：`alembic upgrade head` 应用 `0003_judge_trust_tables`。
