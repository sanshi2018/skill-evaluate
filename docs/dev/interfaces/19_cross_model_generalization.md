# 接入文档：模块九子图、`llama_control` 后端、共识门控与话术词典（docs/dev/19 留给后续模块的接口）

> 由谁接入：`24`（主图装配、状态 schema 合并、健康检查清单）、`11`/`15`（**可选**：给优化闭环
> 叠加共识门控与模型怪癖剥离拦截器）、`20`（复用对照实验骨架）、`21`（金丝雀探针消费
> `LlamaControlBackend.health_check()`）、`22`（接住门控打回后的人工挂起），以及**运维侧**
> （部署一个满足第 3 节 REST 契约的 Llama Agent 运行时）。
> 当前状态：六个节点（抽样 + 三条并行对照实验 + 语言坏味道审查 + 收尾）、三条量化规则、
> `llama_control` 执行后端（poll / callback 两种等待机制 + HTTP 客户端 + 回调端点）、
> 对照实验执行骨架、AI 话术词典与确定性消融、共识门控 + 模型怪癖剥离拦截器全部落地，
> 有测试覆盖（`tests/skill_evaluate/test_cross_model.py` 60 条，不碰库、不发真实请求、不起沙箱）。
> **唯一没有落地的是一个真实存在的 Llama 运行时**——那不是本仓库能提供的东西，见第 3 节。

---

## 0. 三十秒上手

```python
from skill_evaluate.nodes.cross_model import (
    ENTRY_NODE, TERMINAL_NODE, NODE_NAMES, CrossModelDeps,
    add_cross_model_nodes, build_cross_model_subgraph,
)

# A. 装进主图（docs/dev/24）：只加维度内部的边（含三条支路的扇出与汇合）
pipeline = add_cross_model_nodes(builder)
builder.add_edge("trigger_accuracy.finalize_dimension_report", ENTRY_NODE)   # 需要模块一先产出用例集
builder.add_edge(TERMINAL_NODE, "finalize.report")   # 模块十入口见 interfaces/20（排在模块八之后）

# B. 单独跑一遍
graph = build_cross_model_subgraph().compile(checkpointer=...)
await graph.ainvoke({"run_id": ..., "skill_id": ..., "skill_version_ref": ...,
                     "active_suite_version_id": ...})   # 可省略，省略时按 skill_id 查 active 版本
```

**导入即注册**：`import skill_evaluate.nodes.cross_model` 注册三条对照规则；
`import skill_evaluate.agents.optimizer`（或 `...optimizer.consensus_gate`）注册门控规则
`cross_model_consensus_gate`；`import skill_evaluate.executors`（或 `executors.factory`）注册
`llama_control` 后端。

**装配期硬校验**（`CrossModelPipeline.__init__`，配错直接 `ConfigurationError`）：

1. `NODE_BACKEND_ROUTING["cross_model_generalization"]` 必须是 `PLUGGABLE`——Mini 后端的
   `loaded_skill_md` 恒为 True，任何对照都会"完全一致"；
2. 备用后端名必须已注册，且与主后端**不是同一个名字**——主备相同会拿同一个模型和自己比。

---

## 1. 三个名字

| 常量 | 取值 | 用途 |
|---|---|---|
| `DIMENSION` / `ROUTING_KEY` | `cross_model_generalization` | `dimension_results.dimension`、路由表键（docs/dev/03 早已登记） |
| `NODE_PREFIX` | `cross_model` | 节点名前缀（docs/dev/24 按 `cross_model.*` 引用） |

```
cross_model.prepare_cross_model_sample                 （ENTRY_NODE）
cross_model.heterogeneous_execution_matrix             ┐
cross_model.parameter_perturbation_robustness_probe    ├ PROBE_NODES，并行
cross_model.stochastic_ablation_testing                ┘
cross_model.linguistic_smell_check                     （等三条支路全部完成）
cross_model.finalize_dimension_report                  （TERMINAL_NODE）
```

`INTERRUPT_BEFORE_NODES = []`：本维度不产生补丁、不进闭环。

---

## 2. ⚠️ 主图的状态 schema 必须包含本维度私有键

与 `interfaces/11` 第 2 节同一条坑。`CrossModelState` 声明的六个键必须并进主图 schema：

| 键 | 含义 | 谁写 | 谁读 |
|---|---|---|---|
| `_xmodel_sample_case_ids` | 验证集确定性抽样出的用例 | prepare | 三条支路 + finalize |
| `_xmodel_sample_note` | 抽样为空的原因 | prepare | 三条支路（跳过说明）+ finalize |
| `_xmodel_hetero_outcome` | `ProbeOutcome`：异构矩阵 | hetero | finalize |
| `_xmodel_perturbation_outcome` | `ProbeOutcome`：参数扰动 | perturbation | finalize |
| `_xmodel_ablation_outcome` | `ProbeOutcome`：随机消融 | ablation | finalize |
| `_xmodel_linguistic_outcome` | `LinguisticOutcome`：语言坏味道 | linguistic | finalize |

漏掉的后果比别的维度温和但同样白跑：收尾节点拿不到结果，会如实报 `NEEDS_HUMAN_REVIEW`
并在 findings 里点名缺了哪个键（不会假装通过）。三条支路各写自己的键，**不存在**并行
写同一个"后写胜"键的问题；`executed_trace_ids` / `judge_verdict_ids` 是 add reducer，并行安全。

---

## 3. 运维侧：部署 `llama_control` 运行时

### 3.1 为什么需要一层运行时

Hugging Face Inference Endpoint / vLLM 提供的是**补全接口**，不是 Agent：它不会读文件、调工具，
也就产不出 Trace 树。备用代理必须是"Llama 模型 + Agent 循环 + 沙箱"的完整执行环境——可以是
换成 Llama 模型的 Hermes 部署，也可以是自研适配服务。本系统只规定它的对外形状。

### 3.2 REST 契约（`HttpLlamaControlClient` 已实现客户端侧）

| 方法 | 路径 | 请求 / 响应 |
|---|---|---|
| `POST` | `/v1/tasks` | 请求体见下；响应 `{"task_id": "..."}` |
| `GET` | `/v1/tasks/{task_id}` | `{"status": "queued\|running\|succeeded\|failed", "result": <payload>\|null, "error": str\|null}` |
| `GET` | `/healthz` | 2xx 即可达（`health_check()` / 金丝雀探针） |

鉴权：`Authorization: Bearer <SKILLEVAL_EXECUTOR_LLAMA_CONTROL_API_KEY>`（未配置则不带）。
5xx 被视为"评测系统自身故障"抛 `ExecutorBackendError`；4xx 走 `raise_for_status()`，在
`execute()` 里被收敛成失败态 Trace。

`POST /v1/tasks` 请求体：

```json
{
  "model": "meta-llama/Llama-3.3-70B-Instruct",
  "case_id": "...", "run_index": 160, "prompt": "<case.prompt>",
  "skill": {"skill_id": "...", "version_ref": "...", "root_path": "...",
            "description": "...", "body_markdown": "<可能是消融后的正文>",
            "reference_files": ["references/x.md"], "scripts": ["scripts/y.py"]},
  "background_skills": [],
  "sampling_overrides": {"temperature": 0.2, "top_p": 0.9},
  "wall_clock_timeout_s": 90,
  "callback_url": null, "callback_secret": null
}
```

运行时侧的三条硬要求：

1. **以请求里的 `body_markdown` 为准写出 SKILL.md**，不要从 `root_path` 重新读——消融实验下发的
   是改过的正文；`scripts/`、`references/` 按 `root_path` + 清单自行挂载（共享卷 / 制品仓库）。
2. **`sampling_overrides` 原样下发给模型**（参数扰动实验靠它）。模型不接受时请在运行时日志里
   记明，别静默吞掉。
3. **结果体（`result` / 回调 body）与 `HermesHookPayload` 同构**（`LlamaRunPayload` 是它的别名），
   包括 `skill_md_loaded`：能显式上报就上报；给 `null` 时本系统扫描 `trajectory[]` 里
   `tool_name == "read_file"` 且路径含 `SKILL.md` 的动作兜底（docs/dev/03 第 3 节的 fallback）。

### 3.3 两种等待机制

```bash
SKILLEVAL_EXECUTOR_LLAMA_CONTROL_ENDPOINT=https://llama-agent.internal
SKILLEVAL_EXECUTOR_LLAMA_CONTROL_API_KEY=...
SKILLEVAL_EXECUTOR_LLAMA_CONTROL_MODEL=meta-llama/Llama-3.3-70B-Instruct
SKILLEVAL_EXECUTOR_LLAMA_CONTROL_WAIT_MODE=poll          # poll（默认）| callback
SKILLEVAL_EXECUTOR_LLAMA_CONTROL_POLL_INTERVAL_S=5
SKILLEVAL_EXECUTOR_LLAMA_CONTROL_HOOK_SECRET=...         # 仅 callback 模式
```

- **poll**：进程内轮询 `GET /v1/tasks/{id}`，超过 `wall_clock_timeout_s + 15s` 记 `sandbox_timeout`
  失败态 Trace。不依赖图上下文，也能在共识门控里直接用。
- **callback**：提交时带 `callback_url = {API_INTERNAL_BASE_URL}/hooks/llama_control/{run_id}/{case_id}/{run_index}`
  与 `callback_secret`；运行时结束后 `POST` 该地址，body 为 payload，`X-Llama-Signature` 为
  body 的 HMAC-SHA256（与 Hermes 同算法、**不同密钥**）。后端经 `pending_hooks` +
  `suspend_and_wait()` 挂起，端点 `api/hooks_llama.py` 验签→映射→落库→`resolve_suspension()`
  唤醒。要求 `GraphResumer` 已注册（`interfaces/04_graph_resumer.md`）。
  ⚠️ `scripts/pending_hooks_reaper.py` 对超时记录构造失败态 Trace 时不区分后端，callback 模式
  同样被兜底；若要先对 `llama_control` 做一次 `GET /v1/tasks/{id}` 拉取兜底，需要与
  `interfaces/03_hermes_sandbox_client.md` 第 3 点同样给 `pending_hooks` 补记 `task_id` 列。

未配置 endpoint 时默认客户端是 `UnconfiguredLlamaControlClient`：提交抛
`ExecutorBackendError`、`health_check()` 为 False。模块九据此**跳过**异构矩阵并在报告里写明
（维度记 `NEEDS_HUMAN_REVIEW`），参数扰动与消融照常在主代理上跑。**不要**给它加一个"假成功"。

---

## 4. `11`/`15`：给优化闭环叠加门控（可选）

docs/dev/19 第 7 节约定**不修改**已确认的 11/15，由各闭环按需叠加。推荐顺序——静态拦截器最先
（零成本短路），共识门控最后（最贵，且只在原判定通过后才跑）：

```python
from skill_evaluate.agents.optimizer import (
    with_consensus_gate, with_quirk_stripping_gate, is_agent_overfitting,
)

retest_fn = with_quirk_stripping_gate(retest_fn, baseline_skill=skill)
retest_fn = with_consensus_gate(
    retest_fn,
    train_sample_cases,          # ⚠️ 必须全部是 TRAIN split，否则构造期 ValueError
    run_id=run_id,
    baseline_skill=skill,        # 传了按"相对基线降幅"判，不传按绝对口径（见下）
    trace_repository=TraceRepository(),   # 可选，不传不落库
)
patch = await OptimizationLoop().run(run_id, ctx, retest_fn, optimizer)
```

判定口径（规则 `cross_model_consensus_gate`，经 `JudgeAgent.quantitative_verdict()`）：

| 情形 | 结论 |
|---|---|
| 原 `retest_fn` 未通过 | 原样返回，不跑备用代理 |
| 门控可用用例为空（全被过滤） | 通过，`detail` 追加"共识门控未生效" |
| 候选版本在备用代理上一条有效证据都没有 | **打回**（fail closed：备用代理全挂不等于泛化良好；闭环耗尽后挂起，人可以明确采纳） |
| 传了 `baseline_skill` | `基线通过率 − 候选通过率 > tolerance` 即打回（基线每个门控实例只测一次） |
| 未传 `baseline_skill` | `候选通过率 < 1 − tolerance` 即打回（docs/dev/19 正文口径） |

"通过"= 触发行为符合用例类别预期（POSITIVE 加载、NEGATIVE 不加载），失败态 Trace 不计入。

打回时 `LoopResult.detail` 以固定前缀开头，**用函数识别，不要自己匹配字符串**：

- `is_agent_overfitting(result)` —— `代理过拟合(Agent Overfitting)：...`
- `is_model_quirk_rejection(result)` —— `模型怪癖剥离(Model-Quirk Stripping)：...`

两个已知限制（留给后续迭代；`22` 落地后**仍未处理**——它们与人工审批无关，门控打回后的闭环耗尽照常走
`ACCEPT_PATCH` 审批卡片）：

1. `OptimizationLoop` 目前**不把**上一轮的失败 `detail` 回灌给 Optimizer（`FailureContext` 每轮
   相同，见 `agents/optimizer/loop.py`）。门控打回确实会让闭环换一版补丁，但模型并不知道
   "上一版因为讨好主代理被拒"。要让它"换一种更通用的表达"，需要在 `loop.py` 里把失败 detail
   追加进 `attempt_ctx.extra_instructions`——那是对 docs/dev/09 的行为修改，本文档未做。
2. 门控候选号段在每轮复用（`RUN_INDEX_XMODEL_GATE_CANDIDATE`）；同一次运行里模块一和模块五
   **都**启用门控且抽到同一条用例时，后跑的会覆盖先跑的 Trace。门控只读自己刚跑出来的
   内存结果，判定不受影响，仅影响事后回查。

静态拦截器只打回**新增**的思维咒语 / 身份抬举 / 点名模型 / 情绪施压四类措辞；新增的
全大写强调（`NEVER`）与连串感叹号只记 `consensus_gate_emphasis_introduced` 日志——AppSec
补丁写 "NEVER pass user input to a shell" 是正当的。

---

## 5. `20` 及其他维度可以直接复用的东西

### 5.1 对照实验骨架（`executors/comparison.py`）

```python
from skill_evaluate.executors.comparison import (
    run_arm, summarize_arm, is_conclusive_trace, trigger_matches_expectation,
)
traces = await run_arm(backend, run_id=..., skill=..., cases=..., run_index_base=...,
                       runs=1, timeout_s=90, semaphore=shared_semaphore,
                       sampling_overrides=None)
evidence = summarize_arm(traces[case_id], case.category)   # expected / conclusive / total
evidence.behaved_as_expected                                 # True / False / None(无有效证据)
```

同一节点里并发跑两条臂时**共用一个信号量**。`20` 的 `background_skills` 请求属于它自己的语义，
按 `interfaces/11` 第 4.2 节约定自己构造，别往 `run_arm` 上加参数。

> ✅ `20` 已照此办理（`MultiSkillPipeline._execute_all()`），复用了 `is_conclusive_trace()` 的证据口径；
> 没有复用 `summarize_arm()`——多技能并发下"加载了没有"要先经 `executors/skill_attribution.py` 归因，
> 不能直接读 `loaded_skill_md`。

### 5.2 AI 话术词典（`agents/analyzer/ablation_lexicon.py`）

`scan_lexicon(text)` / `ablate(text, seed, drop_probability=...)` / `introduced_hits(before, after)` /
`format_hits_for_review(hits)`。代码块与行内代码受保护。**词典允许持续补充，不需要新文档**；
补充规则见模块头"维护约定"——只收"用情绪/身份/音量代替信息"的措辞，别把"必须"这类通用
情态词收进来。

### 5.3 `linguistic_smell` 模板新增的可选变量

`content` 里可以额外传 `lexicon_hits`（渲染好的命中清单文本）；不传（例如黄金基准用例只带
`skill_md`）时模板照常渲染。`required_variables` 仍然只有 `skill_md`，`LinguisticSmellOutput`
字段未改。

---

## 6. `run_index` 号段登记（`state/trace.py`）

本维度每条臂占 **10 个号**（`RUN_INDEX_XMODEL_ARM_WIDTH`，`runs_per_arm` 上限由它决定）：

| 常量 | 起点 | 用途 |
|---|---|---|
| `RUN_INDEX_XMODEL_PRIMARY` | 150 | 异构矩阵：主代理 |
| `RUN_INDEX_XMODEL_SECONDARY` | 160 | 异构矩阵：备用代理 |
| `RUN_INDEX_XMODEL_PERTURB_BASELINE` | 170 | 参数扰动：贪心基线 |
| `RUN_INDEX_XMODEL_PERTURB_VARIANT` | 180 | 参数扰动：扰动分支 |
| `RUN_INDEX_XMODEL_ABLATION_ORIGINAL` | 190 | 消融：原版 |
| `RUN_INDEX_XMODEL_ABLATION_ABLATED` | 200 | 消融：剥离后 |
| `RUN_INDEX_XMODEL_GATE_BASELINE` | 210 | 共识门控：补丁前基线（备用代理） |
| `RUN_INDEX_XMODEL_GATE_CANDIDATE` | 220 | 共识门控：候选补丁（备用代理，每轮复用） |

后续维度从 230 起申领（`20` 已申领 230~237，`24` 的 Nightly COLD 回归已申领 240~249，再往后从 250 起）。

---

## 7. 判定与报告口径

| 事项 | 本维度的做法 | 为什么 |
|---|---|---|
| 判定入口 | 三条对照走 `quantitative_verdict()`（`cross_model_heterogeneous_consistency` / `_perturbation_robustness` / `_ablation_robustness`）；语言坏味道走 `judgmental_verdict()` | `interfaces/08` 第 0 节铁律 |
| 规则语义 | 参照臂符合预期而变体臂不符合 → FAIL；参照臂自己就不符合 → PASS（列入 `reference_failed_case_ids` 供参考） | 后者是模块一的问题，不在两个维度重复扣分 |
| `Criticality` | 语言坏味道 `ROUTINE` | 维度非阻断，不值得 3 倍 Token |
| 落库 | 对照判定只归档 FAIL；`subject_id` 前缀 `xmodel_hetero:` / `xmodel_perturb:` / `xmodel_ablation:` / `xmodel_linguistic:` / 门控 `xmodel_gate:` | 与模块一/五同口径 |
| 失败态 Trace | 不计入，任一臂无有效证据 → "证据不足" → `NEEDS_HUMAN_REVIEW` | 超时的 `loaded=False` 会凭空制造代理差异 |
| 分数 | `score=None` | 三条实验 + 一项审查硬凑分数没有含义 |
| `blocking` | 恒 `False`（`nodes.py::BLOCKING`，刻意不做配置项） | docs/dev/19 第 9 节 |
| 维度状态 | FAIL（有脆弱性 / 坏味道 FAIL）> NEEDS_HUMAN_REVIEW（抽样为空、支路跳过、证据不足、盲测占用、结果缺失）> PASS | 同其余维度 |
| 消融无可剥离措辞 | `not_applicable`，不起沙箱，不影响状态 | 两份相同文本做对照只会烧钱并引入噪声 |
| 全大写强调 | findings 追加一条"降级警告"，不改状态 | 架构文档"自动降级警告" |
| 共识未达成 | 抛 `PipelineSuspended` | `NEEDS_HUMAN_REVIEW` 不允许降级 |

---

## 8. 配置

```bash
SKILLEVAL_CROSS_MODEL_SAMPLE_RATIO=0.2
SKILLEVAL_CROSS_MODEL_SECONDARY_BACKEND=llama_control
SKILLEVAL_CROSS_MODEL_RUNS_PER_ARM=1                     # 1..10
SKILLEVAL_CROSS_MODEL_EXECUTION_TIMEOUT_S=90
SKILLEVAL_CROSS_MODEL_PERTURBATION_BASELINE_OVERRIDES='{"temperature": 0.0}'
SKILLEVAL_CROSS_MODEL_PERTURBATION_OVERRIDES='{"temperature": 0.2, "top_p": 0.9}'
SKILLEVAL_CROSS_MODEL_ABLATION_DROP_PROBABILITY=0.8
SKILLEVAL_CROSS_MODEL_CONSENSUS_TOLERANCE=0.05
```

⚠️ 参数扰动只在**执行模型支持采样参数**时才真实发生（`interfaces/06_llm_client_and_sampling.md`
第 2 节）。Hermes 若跑在新一代 Claude 上，扰动分支与基线分支实际是同一种解码，本实验会
"没有发现脆弱性"——报告 findings 首行写明了下发的参数与这条前提，读报告的人据此判断可信度。
这要求 `HermesSandboxClient` 的实现把 `sampling_overrides` 下发给模型（已追加到
`interfaces/03_hermes_sandbox_client.md`）。

并发上限与其余维度共用 `SKILLEVAL_EXECUTOR_MAX_CONCURRENT_SANDBOXES`，三条并行支路**共用**
同一个信号量（`CrossModelDeps.semaphore()`）。

无数据库迁移。

---

## 9. 留给后续文档的接入点

| 预留位置 | 当前状态 | 由哪份文档接入 | 接入方式 |
|---|---|---|---|
| 真实的 Llama Agent 运行时 | 客户端 + 契约已就位，默认未配置 | 运维 | 按第 3 节部署并配置 endpoint |
| `with_consensus_gate` 在 11/15 中启用 | 可选钩子已提供 | 视团队策略 | 见第 4 节 |
| 门控打回原因回灌 Optimizer | 未做 | `22` 或 docs/dev/09 修订 | 见第 4 节已知限制 1 |
| `llama_control` callback 模式的拉取兜底 | 未做 | 与 Hermes 同步接入 | `pending_hooks` 加 `task_id` 列，reaper 先 `get_task()` 再退化失败态 |
| `LlamaControlBackend.health_check()` 金丝雀消费 | 接口就位 | `21` / `24` | 预检节点调用；不可用时本维度会自行跳过异构矩阵，不必阻断整条流水线 |
| 主图装配 | ✅ `24` 已接入：入口排在 `trigger_accuracy.finalize_dimension_report` 之后 | `24` | 第 0、2 节 |
| 词典维护 | 初版词典 | 运维持续补充（不产生新文档） | `agents/analyzer/ablation_lexicon.py::LEXICON` |
| 验证集不足时定向补题（`triggered_by="cross_model_sampling"`） | **决定不启用** | — | 非阻断维度不应拥有改变用例集的权力；空抽样如实报告 |
