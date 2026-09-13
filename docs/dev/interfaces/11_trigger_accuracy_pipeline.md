# 接入文档：模块一子图的装配、私有状态与可复用件（docs/dev/11 留给后续模块的接口）

> 由谁接入：`24`（主图装配、`interrupt_before` 汇总、staleness 透传）、
> `13`/`19`/`20`（复用本维度的测试集与"冗余执行 + 量化判定"骨架）、
> `22`（接住优化闭环的人工挂起）。
> 当前状态：七个节点、条件路由、优化闭环接入、触发率量化规则、报告写入全部落地，
> 有测试覆盖（`tests/skill_evaluate/test_trigger_accuracy.py`，30 条，不碰库、不发
> 真实请求）。

---

## 0. 三十秒上手

```python
from skill_evaluate.nodes.trigger_accuracy import (
    ENTRY_NODE, TERMINAL_NODE, NODE_NAMES, TriggerAccuracyDeps,
    add_trigger_accuracy_nodes, build_trigger_accuracy_subgraph,
)

# A. 装进主图（docs/dev/24 的用法）：只加维度内部的边，外部连线由主图决定
pipeline = add_trigger_accuracy_nodes(builder)
builder.add_edge("preflight.canary", ENTRY_NODE)
builder.add_edge(TERMINAL_NODE, "finalize.report")

# B. 单独跑一遍（本地调试 / 集成测试）
graph = build_trigger_accuracy_subgraph().compile(checkpointer=...)
await graph.ainvoke({"run_id": ..., "skill_id": ..., "skill_version_ref": ...})
```

**导入即注册**：`import skill_evaluate.nodes.trigger_accuracy` 会把
`trigger_rate_positive` / `trigger_rate_negative` 注册进 docs/dev/08 的量化规则表。
主图只要引用了本包，规则就一定挂上了；反之，谁都没导入的话
`JudgeRuleError: 未注册的量化判定规则` 会在装配期就炸掉——刻意的。

---

## 1. 节点名与图结构

```
trigger_accuracy.prepare_test_suite
        ↓
trigger_accuracy.execute_train_cases
        ↓
trigger_accuracy.judge_train_cases
        ├─ 无失败 ─────────────────────────────┐
        └─ 有失败 → trigger_accuracy.optimizer_loop
                                                ↓
trigger_accuracy.execute_validation_cases ←─────┘
        ↓
trigger_accuracy.judge_validation_cases
        ↓
trigger_accuracy.finalize_dimension_report
```

节点名一律从 `NODE_NAMES` 取，不要写字面量。`ENTRY_NODE` / `TERMINAL_NODE` 是主图
连线用的两端。

**图里没有"验证集判定失败 → optimizer_loop"这条边**，这是"验证集不参与优化以防止
过拟合"在结构层面的落实（`build_failure_context()` 的训练集校验是第二道闸）。
`24` 装配时不要为了"让失败的验证集也能被修"补一条边回去。

---

## 2. ⚠️ 主图的状态 schema 必须包含各维度私有键

这是本文档最容易踩的一条，**`24` 必读**。

LangGraph 会按**节点函数第一个参数的类型注解**推导该节点的输入 schema，并据此把
图状态裁剪一遍再传进节点；同时，图状态本身只保留 schema 里声明过的通道。所以：

- 本维度的节点签名写的是 `TriggerAccuracyState`（= `PipelineState` + 私有键），
  **不是** `PipelineState`；
- 主图若用裸 `PipelineState` 做状态 schema，本维度写进状态的
  `_trigger_train_case_ids` 等键会被**静默丢弃**——节点拿到的用例列表永远是空的，
  一条错也不报，评测会"成功"地跑完并给出满分。

`24` 的做法：把各维度的状态 TypedDict 合并成主图 schema。

```python
# src/skill_evaluate/graph/main.py
from skill_evaluate.nodes.trigger_accuracy import TriggerAccuracyState
# from skill_evaluate.nodes.context_scoping import ContextScopingState  # 12
# ...

class MainGraphState(TriggerAccuracyState, ContextScopingState, ..., total=False):
    """主图状态 = PipelineState 公共字段 + 各维度私有键的并集。"""

builder = StateGraph(MainGraphState)
```

各维度私有键都带维度前缀（本维度用 `_trigger_*` / `_train_failed_case_ids` /
`_validation_failed_case_ids`），合并时不会互相覆盖。12~20 各维度请照此模式导出
自己的 `XxxState`，并在自己的接入文档里点名这一条。

本维度导出的私有键（键名常量在 `nodes/trigger_accuracy/state.py`）：

| 键 | 含义 | 谁写 | 谁读 |
|---|---|---|---|
| `_trigger_case_ids` | 本维度关心的全部用例（POSITIVE+NEGATIVE） | prepare | 报告/排查 |
| `_trigger_train_case_ids` / `_trigger_validation_case_ids` | 按 split 分组 | prepare | 执行/判定节点 |
| `_train_failed_case_ids` | 训练集失败用例 | judge_train | 路由 + optimizer_loop |
| `_validation_failed_case_ids` | 验证集失败用例（**只**进报告） | judge_validation | finalize |
| `_working_skill` | 打了 description 补丁的内存版本 | optimizer_loop | 验证集执行 |
| `_applied_patch_id` | 采纳的补丁 id | optimizer_loop | finalize / `24` 转 PR |
| `_trigger_suite_staleness_warning` | 用例集版本漂移告警 | prepare | `24` 透传给报告 |

其余维度**不得**读写以上键（docs/dev/11 第 3 节的命名空间约定）。

---

## 3. `24` 的三个具体接入点

### 3.1 `interrupt_before` 汇总

```python
from skill_evaluate.nodes.trigger_accuracy import INTERRUPT_BEFORE_NODES

INTERRUPT_BEFORE_NODES_ALL = [*INTERRUPT_BEFORE_NODES, "security.appsec_optimizer_loop", ...]
```

本维度贡献 `trigger_accuracy.optimizer_loop` 一项。注意闭环走的是**动态**
`interrupt()`（超出重试次数才挂起），不加进静态列表也能正常挂起；列出来是为了让
"这个节点可能停在人工审批上"在编译期就是显式的。

> **docs/dev/22 落地后**：人工放弃补丁后本维度抛 `HumanRejectedSuspension`（`PipelineSuspended` 子类，
> 既有 `except` 不受影响）；主图用 `ApprovalGuardedBuilder` 装配时，guard 据此不再追问。

### 3.2 staleness 告警透传给报告

```python
report = await ReportGenerator().build(
    run_id,
    test_suite_staleness_warning=state.get("_trigger_suite_staleness_warning"),
)
```

不透传的后果：读报告的人不知道这份分数是拿旧题跑出来的。本维度已经把该告警同时
写进了 `dimension_results.findings`，两处不冲突（一处给机器读、一处给人读）。

### 3.3 `runs.suite_version_id` 已由本维度回填

`prepare_test_suite` 调用了 `RunRepository.set_suite_version()`，`ReportGenerator.build()`
因此能反查到本次用的是哪一版用例集。`24` 不需要再填一次；但**主图必须保证
`RunRepository.create()` 在进入本维度之前完成**（入口节点的职责）。

### 3.4 补丁转 PR

`_applied_patch_id` 指向 `patches` 表里已通过回归的那条记录，`target_path` + `diff`
可直接 `git apply`。本层只在评测沙箱内做临时应用与验证，不碰代码仓库
（docs/dev/interfaces/09 第 7 节）。

---

## 4. `13`/`19`/`20` 想复用什么

### 4.1 测试集

本维度是主图 Phase A 里第一个产出 `active_suite_version_id` 的节点，Phase B 的维度
按 docs/dev/24 的边声明依赖它。**不要**在自己的维度里再调一次
`ensure_test_suite()`——REUSE 模式下它是幂等的，但多一次调用意味着多一处可能在
"从来没生成过"时触发出题的入口。

### 4.2 冗余执行骨架

`TriggerAccuracyPipeline.run_cases(run_id, skill, cases)` 是"每条用例并发跑 N 次、
受信号量上限约束"的通用实现，返回 `{case_id: [ExecutionTrace, ...]}`。`19`（跨模型
矩阵）与 `20`（多技能并发）可以直接复用同一个实例：

```python
pipeline = add_trigger_accuracy_nodes(builder, deps)   # 返回值就是 pipeline
traces_by_case = await pipeline.run_cases(run_id, skill, cases)
```

> ✅ `19` 已照此办理：它的对照实验骨架是 `executors/comparison.py::run_arm()`，自己构造带
> `sampling_overrides` 的请求，未改动本维度的 `run_cases()`。
> ✅ `20` 同样照此办理：`nodes/multi_skill/nodes.py::MultiSkillPipeline._execute_all()` 自己构造带
> `background_skills` 的请求；也没有调 `ensure_test_suite()` 之外的出题入口（它用
> `extra_categories=[MULTI_SKILL]` 的 REUSE 语义补齐自己的专属类别，同模块三/五）。

需要在请求里加 `sampling_overrides` / `background_skills` 时，请**不要**改本维度的
`run_cases`，而是在自己的维度里按同样的形状写一份——`ExecutionRequest` 的构造是
各维度语义的一部分（本维度刻意不带这两个字段：触发准确度测的是默认配置下的行为）。

**`15` 落地时给它追加了一个关键字参数**（默认值 = 原行为，既有调用方不受影响）：

```python
traces = await pipeline.run_cases(
    run_id, working_skill, cases,
    run_index_base=RUN_INDEX_SEC_REGRESSION_TRIGGER,   # 默认 RUN_INDEX_REDUNDANT_BASE = 0
)
```

理由：模块五的强制功能回归要拿一个打了安全补丁的 `working_skill` 重跑**同一批**
用例。`execution_traces` 的唯一键是 `(case_id, run_index)`，不换号段的话，一次
"为了验证补丁"的重跑会把本维度本次运行的真实结果覆盖掉——而那正是被验证的对象。
号段分配表见 `state/trace.py`（`docs/dev/interfaces/13` 第 4 节）。

### 4.3 判定逻辑本来就可以脱离图状态单独调用（`15` 的一条实现约束）

`docs/dev/15` 第 11.2 节对本维度提了一条约束："判定核心逻辑应可脱离图节点上下文
单独调用"。本维度**天然满足**：`run_cases()` 本来就是公开的，判定那一半是
`rules.py` 里两个纯函数：

```python
from skill_evaluate.nodes.trigger_accuracy import rules as trigger_rules

inputs  = trigger_rules.trigger_rate_inputs(traces[case.case_id])   # -> {loaded_count, run_count}
verdict = judge.quantitative_verdict(subject_id, trigger_rules.rule_for_category(cat), inputs)
```

`_judge_split()` 只是"读 state → 调上面这两行 → 写回 state"的薄包装。因此
`15` 的功能回归直接组合这两个入口，**不复制一份触发率判定逻辑**。

### 4.4 量化规则

`rule_for_category()` 只接受 POSITIVE / NEGATIVE，其余类别直接 `ValueError`。
`15`（ADVERSARIAL）、`20`（MULTI_SKILL）要按自己的语义注册新规则，不要复用触发率
规则去判"攻击是否被挡住"。

**`15` 已照此办理**：它注册了五条自己的规则（`security_payload_execution` /
`security_env_leak` / `security_path_traversal` / `security_dos_resilience` /
`security_artifact_sast`，见 `nodes/security/rules.py`）。它**唯一**复用本维度规则
的地方是强制功能回归——那里跑的本来就是 POSITIVE/NEGATIVE 用例，语义完全一致。

---

## 5. 判定与报告口径（其余维度请照抄这套口径）

| 事项 | 本维度的做法 | 为什么 |
|---|---|---|
| 判定入口 | 全部走 `judge.quantitative_verdict()` | docs/dev/interfaces/08 第 0 节铁律 |
| `Criticality` | **不涉及** | 触发率是纯数值判定，不走 `judgmental_verdict()`，因此也不适用黄金基准盲测 |
| 落库 | **只归档 FAIL 判定** | 通过判定成百上千条，逐条写库既慢又没人读；失败判定是 Optimizer 的输入与人工审查的唯一证据 |
| 分数 | `1 - 验证集失败数 / 验证集总数` | 训练集失败已被优化闭环修过，计入成绩等于给"改过之后当然会通过"打分 |
| `blocking` | `True` | description 唤不醒 Skill，后面所有维度测的都是一份永远用不到的技能 |
| 验证集为空 | `NEEDS_HUMAN_REVIEW`（不是 PASS） | 零用例通过 = 把这个维度悄悄关掉；报告里必须能看见这件事 |

`record_dimension_result()` 的**实际签名是关键字参数**（`run_id` / `dimension` /
`status` / `score` / `findings` / `blocking`），不是 docs/dev/11 正文里写的
`result=DimensionResult(...)`。12~20 照关键字参数写。

---

## 6. 实现期相对 docs/dev/11 正文的四处修正（照抄正文会踩坑）

1. **节点只返回增量，不要 `{**state, ...}`**。`executed_trace_ids` /
   `judge_verdict_ids` 的 reducer 是 `operator.add`，把整个旧状态回抛会让已有 id
   再追加一遍，跑完两个执行节点 trace id 就翻倍。
2. **量化规则的 `inputs` 传计数，不传整串 Trace**。`quantitative_verdict()` 会把
   `inputs` 的 repr 拼进 `JudgeVerdict.reasoning`；传 Trace 会让每条 reasoning 膨胀
   到几十 KB。用 `trigger_rate_inputs(traces)` 折算成
   `{"loaded_count": n, "run_count": m}`（与 docs/dev/interfaces/08 第 1 节一致）。
3. **闭环收敛后不要 `apply_patch(原始 skill, patch)`**。`OptimizationLoop` 逐轮把
   补丁叠加在上一轮产物上，最终补丁的 `base_skill_version_ref` 指向的是上一轮的
   工作副本，对着原始版本再打一次必定抛 `PatchApplyError`。正确做法是从
   `retest_fn` 收到的 `working_skill` 里捞（本实现用
   `working_version_ref(base, patch_id)` 反查命中的那一份）。
4. **私有状态键必须出现在图的状态 schema 里**，见第 2 节。

---

## 7. 配置

新增一项（追加式扩展 `ExecutorSettings`，无迁移、无破坏性变更）：

```bash
SKILLEVAL_EXECUTOR_MAX_CONCURRENT_SANDBOXES=10   # 默认 10
```

同时在飞的沙箱执行数上限，由节点侧 `asyncio.Semaphore` 落实。冗余执行次数
（`REDUNDANT_RUNS=3`）与超时（`EXECUTION_TIMEOUT_S=90`）是**常量不是配置项**：
3 次与判定阈值 0.5（3 次中至少 2 次）是绑死的，单独调一个会让判定语义悄悄变化。
需要在某次运行里改，用 `TriggerAccuracyDeps(redundant_runs=..., execution_timeout_s=...)`
显式注入。

## 8. description 优化 Prompt

docs/dev/11 第 11 节留的"措辞待补"这一项，实际由 docs/dev/09 的
`agents/optimizer/prompts/description_patch.jinja` 承担，已写完可直接用（写触发场景
而非功能罗列、吃进失败用例的真实说法、误触发同样是失败、禁止模型咒语）。本维度
只通过 `FailureContext.extra_instructions` 补了一段判定口径说明（3 次冗余、双向
阈值），**不复制一份模板**——两份措辞早晚会漂移。要调措辞就改那个 `.jinja`。
