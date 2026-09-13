# 接入文档：模块三子图、探查扫描器与 run_index 号段分配（docs/dev/13 留给后续模块的接口）

> 由谁接入：`24`（主图装配、状态 schema 合并、`interrupt_before` 汇总）、
> `14`~`20`（复用探查扫描器、Trace 摘要、A/B 骨架；**必须遵守 run_index 号段分配**）、
> `15`（Validator 断言在本维度的可选接入点）、`22`（接住优化闭环的人工挂起）。
> 当前状态：八个节点、并行分叉 + 汇合、条件路由、优化闭环接入、探查量化规则、两个
> 新评审模板、Generator 类别注册表全部落地，有测试覆盖
> （`tests/skill_evaluate/test_instruction_control.py` 56 条 +
> `test_generator.py` 新增 12 条，不碰库、不发真实请求）。

---

## 0. 三十秒上手

```python
from skill_evaluate.nodes.instruction_control import (
    ENTRY_NODE, TERMINAL_NODE, NODE_NAMES, InstructionControlDeps,
    add_instruction_control_nodes, build_instruction_control_subgraph,
)

# A. 装进主图（docs/dev/24 的用法）：只加维度内部的边，外部连线由主图决定
pipeline = add_instruction_control_nodes(builder)
builder.add_edge("trigger_accuracy.prepare_test_suite", ENTRY_NODE)   # 见第 3.1 节
builder.add_edge(TERMINAL_NODE, "finalize.report")

# B. 单独跑一遍（本地调试 / 集成测试）
graph = build_instruction_control_subgraph().compile(checkpointer=...)
await graph.ainvoke({"run_id": ..., "skill_id": ..., "skill_version_ref": ...})
```

**导入即注册**：`import skill_evaluate.nodes.instruction_control` 会把
`progressive_disclosure_probe` 注册进 docs/dev/08 的量化规则表。两个评审模板
（`roi_comparison` / `trace_efficiency`）则由 `import skill_evaluate.agents.mini`
注册（`agents/mini/templates/instruction_control.py` 的导入副作用），Judge 侧本来
就会导入，不需要额外操心。

---

## 1. 节点名与图结构

```
instruction_control.prepare_instruction_control_cases
   ├──────────────────────────┬──────────────────────────────────┐
   ↓                          ↓                                  ↓
.ab_comparative_execution   .control_calibration_static_scan   .progressive_disclosure_dynamic_probe
   ↓                          │                                  │
.trace_efficiency_diagnosis   │                                  │
   └──────────────────────────┴──────────────────────────────────┘
                              ↓
                      .collect_findings
                              ├─（训练集有可优化失败）→ .optimizer_loop ─┐
                              └──────────────────────────────────────────┤
                                                                         ↓
                                              .finalize_dimension_report
```

节点名一律从 `NODE_NAMES` 取，不要写字面量。`ENTRY_NODE` / `TERMINAL_NODE` 是主图
连线用的两端。

### 相对 docs/dev/13 正文的两处结构差异

1. **多了一个 `collect_findings` 节点**。正文的流程图里没有它。条件路由必须挂在
   某个节点上，而"要不要进优化闭环"需要同时看到 A/B 与探查两条支路的结论；挂在
   其中一条支路上会漏掉另一条的失败信号。它是纯汇总，不发请求、不写库。
2. **用静态并行边，不用 `Send`**。正文提到用 `Send` API 并发触发。`Send` 解决的是
   "运行时才知道要派生多少个同构分支"（map-reduce），而这里是三段**结构不同的固定
   逻辑**，节点数编译期就确定。静态边的好处是图结构在 `get_graph().draw()` 里看得
   见，`interrupt_before` 也能按节点名精确挂载。

**`optimizer_loop` 之后没有回到子节点的回边**：重测在 `optimizer_loop` 内部的
`retest_fn` 里完成（只重跑失败的那个子集）。`24` 不要为了"让闭环重跑整条支路"补一
条回边——闭环最多跑 3 轮，每轮全量重跑 A/B 的成本没人会接受，而且回边会让这张图
出现环。

---

## 2. ⚠️ 主图的状态 schema 必须包含本维度私有键

与 `docs/dev/interfaces/11` 第 2 节、`12` 第 2 节同一条坑，**本维度的私有键最多**，
漏一个的表现是"某条支路的结论在收尾节点凭空消失"。

LangGraph 按节点函数第一个参数的类型注解裁剪图状态。本维度节点签名写的是
`InstructionControlState`（= `PipelineState` + 私有键）。主图若用裸 `PipelineState`
做 schema，本维度写进状态的结果会被**静默丢弃**。

`24` 的做法：

```python
# src/skill_evaluate/graph/main.py
from skill_evaluate.nodes.trigger_accuracy import TriggerAccuracyState
from skill_evaluate.nodes.context_scoping import ContextScopingState
from skill_evaluate.nodes.instruction_control import InstructionControlState
# ...

class MainGraphState(
    TriggerAccuracyState, ContextScopingState, InstructionControlState, ..., total=False
):
    """主图状态 = PipelineState 公共字段 + 各维度私有键的并集。"""

builder = StateGraph(MainGraphState)
```

本维度导出的私有键（键名常量在 `nodes/instruction_control/state.py`，全部以 `_ic_`
开头）：

| 键 | 含义 | 谁写 | 谁读 |
|---|---|---|---|
| `_ic_ab_case_ids` | 参与 A/B 的用例（POSITIVE ∩ 训练集） | prepare | ab / collect / finalize |
| `_ic_pd_case_ids` | 渐进式披露探查用例（两个 PD 类别） | prepare | probe / collect / finalize |
| `_ic_ab_pairs` | `[{case_id, loaded_trace_id, baseline_trace_id}]` | ab | trace_efficiency |
| `_ic_roi_outcomes` | ROI 判定摘要（`JudgmentOutcome` 的 dump） | ab | collect / finalize |
| `_ic_efficiency_outcomes` | 效率诊断摘要 | trace_efficiency | finalize |
| `_ic_calibration_outcome` | 控制标定摘要（整份 Skill 一条） | calibration | finalize |
| `_ic_pd_findings` | 探查发现（`ProbeFinding` 的 dump） | probe | collect / finalize |
| `_ic_failed_train_case_ids` | 可优化的失败信号（只含训练集） | collect | 路由 + optimizer_loop |
| `_ic_working_skill` | 本维度闭环打完补丁的内存工作副本 | optimizer_loop | 重测 |
| `_ic_applied_patch_id` | 采纳的补丁 id | optimizer_loop | finalize / `24` 转 PR |
| `_ic_suite_staleness_warning` | 用例集版本漂移告警 | prepare | `24` 透传给报告 |
| `_ic_baseline_token_watermark` | **输入型**：外部提供的 Token 水位（可选） | `24` 或外部 | probe |

其余维度**不得**读写以上键。反过来本维度也**不读**别人的：特别是模块一的
`_working_skill`——那是 description 补丁（触发相关），与本维度评的"正文写得好不好
用"不是一回事，而且跨维度读私有键是命名空间约定明令禁止的。

---

## 3. `24` 的接入点

### 3.1 依赖模块一的测试集（Phase 顺序）

本维度的 A/B 对比复用**模块一已经生成并落库的 POSITIVE 训练集用例**，因此
`ENTRY_NODE` 必须排在 `trigger_accuracy.prepare_test_suite` 之后。它**不依赖**模块
一的判定结果，所以不必等模块一整条支路跑完。

另外有一条更硬的顺序约束，见第 4 节：本维度的 A/B Trace 与模块一的冗余执行 Trace
共用 `execution_traces` 表，虽然号段已经隔开、模块一的判定也已经加了过滤，
把本维度排在 `trigger_accuracy.judge_train_cases` 之后仍然是更稳妥的做法。

### 3.2 `interrupt_before` 汇总

> **docs/dev/22 落地后**：人工放弃补丁后抛 `HumanRejectedSuspension`；ROI 共识未达成抛的普通
> `PipelineSuspended` 由节点级 guard 接成 `ABANDON_RUN` 阻塞审批（retry / abandon），见 `interfaces/22` 第 1 节。

```python
from skill_evaluate.nodes.instruction_control import INTERRUPT_BEFORE_NODES
```

本维度贡献 `instruction_control.optimizer_loop` 一项。与模块一同样的说明：闭环走
的是**动态** `interrupt()`，不加进静态列表也能正常挂起；列出来是为了让"这个节点
可能停在人工审批上"在编译期就是显式的。

### 3.3 staleness 告警透传

`_ic_suite_staleness_warning` 与模块一的 `_trigger_suite_staleness_warning` 语义相同
（同一份用例集）。两处都取到时任选其一透传给 `ReportGenerator.build()` 即可，本
维度已经把它同时写进了 `dimension_results.findings`。

### 3.4 补丁转 PR

`_ic_applied_patch_id` 指向 `patches` 表里已通过回归的那条记录。注意本维度的补丁
多为 `rigid_constraint`（改正文），与模块一的 `description_patch` 改的是同一个
`SKILL.md` 的不同部位——`24` 若把两者一起转成 PR，需要先确认两份 diff 能叠加。

### 3.5 可选：预置 Token 水位

若 `24`（或将来某个掌握真实历史水位的维度）能提供一个比"同批中位数"更靠谱的水位
基线，在进入本维度前往状态里写 `_ic_baseline_token_watermark`（int）即可，探查节点
会优先用它。不写就用本维度自算的那个（见第 5.4 节）。

---

## 4. ⚠️ `run_index` 号段分配表（**这是会影响别人判定的全局约定**）

`execution_traces` 的唯一键是 `(case_id, run_index)`，而**同一条用例会被多个维度反复
执行**。本文档实现期发现并修复了一处真实的相互污染：

- 模块一对每条 POSITIVE 用例跑 3 次冗余（`run_index` 0/1/2）；
- 模块三拿**同一批用例**跑 A/B。若也从 0 开始编号，后跑的会静默覆盖先跑的；
- 即便不覆盖，模块一按 `list_by_case()` 统计触发率时会把模块三那条"故意不加载
  Skill"的基线分支算成一次"没触发"——一份完全正常的 Skill 会在下一次运行里莫名
  其妙触发率不达标。

因此 `state/trace.py` 里新增了一张**全局分配表**，新维度要落 Trace 时在此申领号段，
不要就地写字面量：

```python
from skill_evaluate.state.trace import (
    RUN_INDEX_REDUNDANT_BASE,   # 0    模块一：0 .. redundant_runs-1（0~99 留给同维度内的冗余执行）
    RUN_INDEX_DIMENSION_BASE,   # 100  各维度专用号段的起点
    RUN_INDEX_AB_LOADED,        # 100  模块三：A/B 加载分支
    RUN_INDEX_AB_BASELINE,      # 101  模块三：A/B 基线分支
    RUN_INDEX_PD_PROBE,         # 110  模块三：渐进式披露探查
)
```

配套的两处修改：

1. **模块一的判定加了过滤**：`_judge_split()` 只统计 `run_index < redundant_runs`
   的 Trace（回归测试：`test_trigger_accuracy.py::test_其他维度的Trace不参与触发率统计`）。
   `14`~`20` 若也要按 `list_by_case()` 聚合，**照此过滤自己的号段**。
2. **模块三的 `run_count_per_arm` 有上限**：A/B 按
   `RUN_INDEX_AB_LOADED + 2*i + arm` 编号，到 110 为止，因此最多 5 次；超过会在
   `InstructionControlPipeline` 构造时抛 `ConfigurationError`，而不是静默覆盖探查
   用例的 Trace。要跑更多次请先在分配表里扩容。

`14`（脚本易用性）、`15`（红队）、`19`（跨模型矩阵）、`20`（多技能并发）落地时请各
自申领号段并在此表登记。

**已登记的结论：`14` 不占号段**——模块四裸调脚本子进程（`ScriptSandboxRunner`），
一条 `ExecutionTrace` 都不落，`execution_traces` 表与它无关，因此按
`list_by_case()` 聚合的维度不需要为它做任何过滤。理由见
`docs/dev/interfaces/14_script_usability_probing.md` 第 3.2、4 节。

**`15` 已登记两组号段**（`docs/dev/interfaces/15_security_red_team.md` 第 4 节）：

```python
RUN_INDEX_SEC_PROMPT_INJECTION = 120        # 五条探测支路各占一个号，
RUN_INDEX_SEC_DATA_POISONING = 121          # 闭环重测**复用同一个号**（覆盖旧记录，
RUN_INDEX_SEC_ENV_AND_TRAVERSAL = 122       # 让判定只看当前这版的表现，与本维度
RUN_INDEX_SEC_DOS = 123                     # 的闭环重测同一处理）
RUN_INDEX_SEC_ARTIFACT_SAST = 124
RUN_INDEX_SEC_REGRESSION_TRIGGER = 130      # 强制功能回归：重跑模块一（130~132）
RUN_INDEX_SEC_REGRESSION_AB_LOADED = 140    # 强制功能回归：重跑本维度的 A/B
RUN_INDEX_SEC_REGRESSION_AB_BASELINE = 141
```

**回归那一组尤其重要**：模块五的安全补丁必须证明自己没把正常业务改坏，为此要拿
`working_skill` 重跑模块一的触发率与本维度的 A/B。不换号段的话，一次"为了验证补丁"
的重跑会把模块一/三本次运行的真实结果覆盖掉——而那正是被验证的对象。为此本维度的
`_run_ab()` 追加了一个 `run_index_base` 参数（默认值 = 本维度自己的号段，行为不变）。

---

## 5. `14`~`20` 可以直接复用的四样东西

### 5.1 探查扫描器（无 LLM、无 IO，纯函数）

```python
from skill_evaluate.nodes.instruction_control import (
    read_reference_paths, scan_probe_trace, resolve_token_watermark,
)

paths = read_reference_paths(trace, [ref.path for ref in skill.reference_files])
```

`read_reference_paths()` 回答"这次执行读了哪些参考文件"，两级识别（显式读文件动作
的路径键 + 其余动作里的已知路径子串），**只承认已知路径**，因此误报率为零、代价是
漏报。`20`（多技能并发）判"有没有串读到别的 Skill 的文件"可以直接复用。

### 5.2 Trace 摘要（把执行轨迹压进 Prompt）

```python
from skill_evaluate.nodes.instruction_control import format_actions_for_review, format_final_response
```

任何要把 `ExecutionTrace` 交给 LLM 审查的维度都该走它，而不是
`[a.model_dump() for a in trace.actions]`（docs/dev/13 正文的伪代码写法）：后者会
把上百步 × 32KB 输出整串塞进 Prompt，且不脱敏。本函数留头尾掐中间、逐字段截断、
统一过一遍 `redact_secrets()`。

### 5.3 A/B 骨架与 ROI 判定的**公开**入口（`15` 追加）

`docs/dev/15` 第 11.2 节对本维度提了一条实现约束："判定核心逻辑应可脱离图节点上下文
单独调用，LangGraph 节点函数只是对它的一层薄包装"。落实方式是加了两个公开包装
（追加式扩展，本维度既有节点行为一个字没变）：

```python
# 执行骨架：并发跑"加载/基线"两条分支，返回 [(case, loaded_traces, baseline_traces)]
results = await pipeline.run_ab_pairs(
    run_id, working_skill, positive_cases,
    run_index_base=RUN_INDEX_SEC_REGRESSION_AB_LOADED,   # 默认 RUN_INDEX_AB_LOADED
)

# ROI 判定：CRITICAL 共识，返回 JudgmentOutcome（含 skipped_reason，黄金盲测时非空）
outcome = await pipeline.judge_roi(case, loaded, baseline)
```

`ab_comparative_execution` 节点现在就是这两个入口的薄包装。复用方注意两条：

1. **必须换号段**（理由同第 4 节）；
2. **`outcome.skipped_reason` 非空时不要算成失败**——那次请求被黄金基准盲测占用了，
   根本没有评到这条用例，算成失败等于让一次抽检把补丁枪毙了。

`15` 的 `FunctionalRegressionRunner` 就是这两个入口 + 模块一那两个入口的组合，
`19`/`20` 若也需要"拿某个变体 Skill 重跑 A/B"，直接用它们，不要再造一份。
（仍然遵守第 9 节那条：**不要改 `_run_ab()` 里 `ExecutionRequest` 的构造**——
`sampling_overrides` / `background_skills` 属于那两个维度自己的语义。）

### 5.4 Token 水位的算法（`resolve_token_watermark()`）

docs/dev/13 正文第 7 节写的是 `state.get("_baseline_token_watermark", 999999)`，
但正文没有说这个水位从哪来。实现按下面的口径补齐：

- 有 override（状态键 `_ic_baseline_token_watermark`）→ 用它；
- 否则 = **同批常规探查用例 Token 中位数 × (1 + `pd_token_watermark_ratio`)**；
- 干净样本少于 `pd_watermark_min_samples`（默认 3）→ 返回 `None`，**不做**这项检查。

为什么不是绝对阈值：`total_tokens` 里绝大部分是任务提示词与工具输出，换一份 Skill
就完全不是一个量级，写死的数字每次都得重调。为什么是中位数：被我们盯上的异常值
本身就在样本里，用均值会被它自己把水位抬高。样本太少时宁可不查——三个以下样本算
出来的中位数本身就是噪音。

---

## 6. 判定与报告口径（与模块一/二的差异）

| 事项 | 本维度的做法 | 为什么 |
|---|---|---|
| 判定入口 | ROI / 效率 / 标定走 `judgmental_verdict()`；探查走 `quantitative_verdict()` | docs/dev/interfaces/08 第 0 节铁律；探查是对 Trace 的确定性扫描，让模型去数既贵又不准 |
| `Criticality` | ROI = **CRITICAL**（3 副本共识）；效率/标定 = ROUTINE | "这份 Skill 没有附加价值、必须打回重构"直接决定要不要合并，值得多花两倍成本换可信度；后两者只作参考 |
| A/B 冗余次数 | 每分支 **1** 次（可配） | 成本：3 次会让本维度达到基础评测的 6 倍。ROI 关心的是"存在不存在显著差异"，且这条前提**写进了裁判 Prompt**，让它在差距不明显时倾向 pass |
| 分数 | `score=None` | 四项性质完全不同的检查，硬凑"通过项/总项数"会把它们平均成一个没含义的数字 |
| `blocking` | **运行期计算**：`roi_failed or 漏读` | 漏读是正确性问题（凭幻觉作答）；过度抓取只是成本问题；效率/标定是写作风格建议 |
| 维度状态 | FAIL（阻断项）> NEEDS_HUMAN_REVIEW（只有非阻断问题）> PASS | 见下 |
| 用例集为空 | `NEEDS_HUMAN_REVIEW` | 零用例通过 = 把维度悄悄关掉；报告里必须能看见 |
| 黄金盲测 | `is_golden_subject()` 跳过，并在 findings 里写明"这一项本次没跑成" | 与模块二同一口径 |
| 共识未达成 | 抛 `PipelineSuspended` 等人工仲裁 | `NEEDS_HUMAN_REVIEW` 不允许被降级成 PASS/FAIL（docs/dev/08 明令禁止） |

**维度状态那一行是相对 docs/dev/13 正文第 9 节的一处收窄**：正文写的是"非阻断问题
只进 findings、status 仍为 PASS"，那会让报告出现"结论通过、正文里却列着一串问题"
的自相矛盾。改判 `NEEDS_HUMAN_REVIEW` 既保留了不阻断合并（`blocking=False` 才是
阻断与否的唯一依据，见 `ReportGenerator.build()`），又让"这里有事情要人看一眼"在
总状态里可见——而这恰恰是正文第 8 节对这两类问题的处置方式："交给人工在报告中
查看后自行决定是否采纳"。

`record_dimension_result()` 是**关键字参数**（`run_id` / `dimension` / `status` /
`score` / `findings` / `blocking`），不是正文写的 `result=DimensionResult(...)`。

### 6.1 `subject_id` 前缀约定

同一条用例在模块一那边已经有一条按裸 `case_id` 存的触发率判定，因此本维度的判定
一律带前缀，避免 `JudgeRepository.list_verdicts(subject_id)` 把两者混成一堆：

| 前缀 | 用途 |
|---|---|
| `roi:<case_id>` | ROI 判定 |
| `efficiency:<trace_id>` | 效率诊断（按 Trace 而不是按用例：一条用例可能有多条轨迹） |
| `pd_probe:<case_id>` | 渐进式披露探查 |
| `<skill_id>`（无前缀） | 控制标定（整份 Skill 一条，与模块二的同行评审同键） |

`14`~`20` 新增判定时请照此加自己的前缀。

---

## 7. 数据契约与迁移（其他模块可能受影响）

1. **`TestCaseCategory` 新增两项**：`progressive_disclosure_trigger` /
   `progressive_disclosure_regular`。任何按类别做 `if/elif` 的地方要确认它们不会掉进
   某个"其余一律按正向处理"的分支——模块一的
   `rule_for_category()` 已经是显式白名单（收到其他类别直接 `ValueError`），是正确
   的写法，请照抄。
2. **`TestCase.probe_target_reference: str | None`**：新增字段 + Alembic 迁移
   `0006_instruction_control_columns`（纯追加列、nullable，无需数据回填）。其余类别
   恒为 `None`。
3. **`TestCaseRepository` 新增两个查询**：`list_by_category(suite_version_id, category)`
   与 `list_by_categories(suite_version_id, categories)`。**口径是
   `suite_version_id` 而不是 `skill_id`**：按 skill_id 查会把已经不在 active 版本里
   的历史用例一起捞回来，评测就跑了一批没人再维护的旧题。
4. **`GenerationRequest.category_counts`** 与 **`GeneratedCase.probe_target_reference`**：
   见 `docs/dev/interfaces/06_generator_extension_points.md` 第 3 节（已同步更新）。

---

## 8. 配置

新增一组（追加式扩展，无迁移、无破坏性变更）：

```bash
SKILLEVAL_INSTRUCTION_CONTROL_RUN_COUNT_PER_ARM=1          # A/B 每条分支跑几次，上限 5（见第 4 节）
SKILLEVAL_INSTRUCTION_CONTROL_PD_TOKEN_WATERMARK_RATIO=0.5 # 常规探查用例的 Token 容忍比例
SKILLEVAL_INSTRUCTION_CONTROL_PD_WATERMARK_MIN_SAMPLES=3   # 少于这个样本数就不做水位检查
SKILLEVAL_INSTRUCTION_CONTROL_TRACE_DIGEST_MAX_STEPS=40    # 交给效率诊断的动作步数上限
SKILLEVAL_INSTRUCTION_CONTROL_TRACE_DIGEST_MAX_OUTPUT_CHARS=400
```

并发上限与模块一共用 `SKILLEVAL_EXECUTOR_MAX_CONCURRENT_SANDBOXES`：本维度的
`用例数 × 2 × run_count` 比模块一更需要它。

`run_count_per_arm` 就是 docs/dev/13 第 10 节留的那项"可配置化"待办，已落地。
若报告数据显示 ROI 判定本身抖动过大，调大它即可，不需要改任何代码结构。

---

## 9. 留给后续文档的接入点

| 预留位置 | 当前状态 | 由哪份文档接入 | 接入方式 |
|---|---|---|---|
| `ExecutionRequest.assertion_specs` 在本维度的使用 | **未使用**（本维度的 A/B 与探查都不需要确定性断言） | ✅ `15` 已在**自己的**维度里用（生成物 SAST 那条支路） | 若某天要在 A/B 里核对产物，按 docs/dev/interfaces/10 的协议往 `_run_ab()` 的 `ExecutionRequest` 里加 `assertion_specs` 即可，节点结构不变 |
| `run_index` 号段 100~99xx | 已用 100/101/110（本维度）+ 120~124/130~132/140~141（`15`） | `19`/`20` | 在 `state/trace.py` 的分配表里申领并登记，见第 4 节 |
| A/B 骨架与 ROI 判定的公开入口 | ✅ 已加（`run_ab_pairs()` / `judge_roi()`） | `15` 已用于强制功能回归 | 见第 5.3 节；`19`/`20` 可直接复用 |
| 效率诊断/控制标定升级为阻断项 | 当前非阻断 | 运维调优，非新文档职责 | 改 `finalize_dimension_report()` 里 `blocking` 的计算式一处即可；同时应把对应 `Criticality` 升到 CRITICAL（`_to_outcome()` 已备好共识路径） |
| ROI 判定的黄金基准用例 | 依赖 docs/dev/08 的黄金库 | `21`（Generator 可信度与黄金基准） | 往 `golden_cases` 表里加 `template_key='roi_comparison'` 的条目即可，本维度不需要改动 |
| 跨模型跑 A/B | 未涉及（`19` 已落地，未跨模型跑 A/B） | `19` | `19` 自己构造带 `sampling_overrides` 的 `ExecutionRequest`，**不要**改本维度的 `_run_ab()`——请求的构造是各维度语义的一部分（与 docs/dev/interfaces/11 第 4.2 节同一条约定） |
