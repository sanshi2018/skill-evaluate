# 接入文档：加权覆盖率、负向约束与可追溯性制品（docs/dev/18 留给后续模块的接口）

> 由谁接入：`22`（审查工作台——展示未覆盖负向约束、消费可追溯性制品）、
> `23`（长时记忆——能力树 + 权重是天然的检索索引）、`24`（主图装配、状态 schema
> 合并、制品归档到 CI）。团队运维侧可选对接：Base44 等热力图控制台消费
> `traceability_matrix.json`（**不在本仓库内实现**，见第 7 节）。
> 当前状态：七个节点、`AnalyzerAgent` 的两个新方法、`negative_constraint_probe`
> 评审模板、组合对优先级纯函数、JSON/CSV 双制品全部落地，有测试覆盖
> （`tests/skill_evaluate/test_weighted_coverage.py` 38 条，不碰库、不发真实请求、
> 不写项目目录）。

---

## 0. 三十秒上手

```python
from skill_evaluate.nodes.coverage import add_coverage_nodes
from skill_evaluate.nodes.pruning import PruningDeps, add_pruning_nodes
from skill_evaluate.nodes.pruning import TERMINAL_NODE as PRUNING_TERMINAL
from skill_evaluate.nodes.weighted_coverage import (
    ENTRY_NODE, TERMINAL_NODE, NODE_NAMES, WeightedCoverageDeps,
    add_weighted_coverage_nodes, build_weighted_coverage_subgraph,
)

# A. 装进主图（docs/dev/24 的用法）：本维度必须排在模块六、模块七之后
cov = add_coverage_nodes(builder)                                        # docs/dev/16
prune = add_pruning_nodes(builder, PruningDeps.from_coverage(cov.deps))  # docs/dev/17
add_weighted_coverage_nodes(                                             # docs/dev/18
    builder, WeightedCoverageDeps.from_coverage(prune.deps)
)
builder.add_edge(PRUNING_TERMINAL, ENTRY_NODE)                           # 17 → 18
builder.add_edge(TERMINAL_NODE, "<下一个维度>")

# B. 单独跑一遍（本地调试 / 集成测试）
graph = build_weighted_coverage_subgraph().compile(checkpointer=...)
await graph.ainvoke({
    "run_id": ..., "skill_id": ..., "skill_version_ref": ...,
    "active_suite_version_id": ...,   # 必填
    "capability_tree_id": ...,        # 必填，由模块六产出
})
```

本模块**不注册**任何新的量化规则（加权判定复用模块六那条，见第 4 节），但
**新增一个评审模板** `negative_constraint_probe`——`import skill_evaluate.agents.mini`
即完成注册（`agents/mini/__init__.py` 已导入）。

---

## 1. 三个名字：两个沿用模块六，一个必须不同

| 常量 | 取值 | 与模块六的关系 |
|---|---|---|
| `ROUTING_KEY` | `coverage_analysis` | **沿用**（从 `nodes.coverage.state` 直接导入） |
| `NODE_PREFIX` | `coverage` | **沿用**（主图里是同一个 `coverage` 分区） |
| `DIMENSION` | `weighted_coverage` | **本文档独有**（沿用 interfaces/16 第 1 节的建议） |

七个节点名（都与模块六的五个、模块七的五个不重复）：

```
coverage.extract_tier_and_negative_constraints   （ENTRY_NODE）
coverage.map_negative_constraint_coverage
coverage.constraint_feedback_generation
coverage.recompute_weighted_coverage
coverage.upgrade_combinatorial_priority
coverage.generate_traceability_artifact
coverage.finalize_weighted_coverage_report       （TERMINAL_NODE）
```

收尾节点叫 `finalize_weighted_coverage_report` 而不是 docs/dev/18 原文的
`finalize_dimension_report`：那个名字模块六已经占了（模块七同样改过名，叫
`finalize_pruning_report`）。同一张图里节点名必须唯一。

---

## 2. ⚠️ 主图的状态 schema 必须包含本维度私有键

`nodes/weighted_coverage/state.py::WeightedCoverageState` 声明的十八个键必须并进
主图状态：

```
_weighted_coverage_node_count               _weighted_coverage_tier_distribution
_weighted_coverage_tier_graded              _weighted_coverage_constraint_count
_weighted_coverage_uncovered_constraint_ids _weighted_coverage_undetermined_constraint_ids
_weighted_coverage_probe_call_count         _weighted_coverage_probe_budget_exhausted
_weighted_coverage_constraint_patched_count _weighted_coverage_patch_failure
_weighted_coverage_ratio                    _weighted_coverage_constraint_ratio
_weighted_coverage_verdict_status           _weighted_coverage_ranked_uncovered_pairs
_weighted_coverage_ranked_pair_count        _weighted_coverage_total_pair_count
_weighted_coverage_artifact_path            _weighted_coverage_artifact_failure
```

被裁掉的症状：分级与约束抽取照常跑、照常改库（tier 真的被改写、约束真的落表、
补题真的落了新版本），只是 finalize 拿不到任何数。因此 finalize 对"加权覆盖率取
不到"判 `NEEDS_HUMAN_REVIEW` 并点名该键，而不是判 PASS。

> docs/dev/18 正文的伪码用的是 `_uncovered_constraint_ids` /
> `_traceability_artifact_path`（无维度前缀）。实现统一加了 `_weighted_coverage_`
> 前缀——主图把十个维度的状态并成一张 schema，LangGraph 的默认 reducer 是"后写
> 胜"，无前缀的键撞名时不报错，只会让某个维度悄悄拿到别人的数据。

最省事的做法是让主图状态直接继承各维度的 `*State`（它们都是 `PipelineState` 的
`total=False` 扩展，可以安全地多重继承）。

---

## 3. 前置依赖与排序：必须排在模块六**和**模块七之后

- **模块六**：本维度全部节点读它建好的 `CapabilityTree`（分级是在已有节点上打
  标签，不是重新抽树）。`capability_tree_id` 取不到 → `PersistenceError`，错误
  信息里点名这条顺序约束。
- **模块七**：`upgrade_combinatorial_priority` 读
  `CapabilityTree.combinatorial_pairs_covered`——那是模块七的产出。顺序反了
  **不会报错**：它会读到空列表然后报告"全部组合都未覆盖"，一个刺眼但完全错误
  的结论。
- `active_suite_version_id` 取不到 → `PersistenceError`（同模块六/七的理由：
  "这些约束一条都没被测到"和"我根本没拿到测试集"是两件完全不同的事）。

反过来，本维度会**修改**两样东西：

1. **`CapabilityNode.tier` 被改写成真实分级**，`negative_constraints` 被整批重抽
   （落库是整树 upsert）。排在本维度之后再读能力树的模块会看到真实权重。
2. **`active_suite_version_id` 可能被换成新版本**（未覆盖约束补反事实用例时）。
   与模块六/七一样是数据飞轮的意图而非副作用。

---

## 4. 加权覆盖率：规则名只有一个，不要再注册一条

docs/dev/16 第 9 节的约定已兑现：**规则名 `capability_coverage_threshold` 不变，
调用方不需要修改**。落到代码上是三处：

1. `CapabilityTree.weighted_coverage()` 是**唯一**的覆盖率算法。模块六的
   `blind_spot_detection` 也改成调它（`docs/dev/interfaces/16` 第 4.2 节点名的坑：
   规则换了而节点里的算法没换，判定与报告会给出两个不同的数）。
2. `coverage_inputs()` 的 `tier_weighted` 从写死的 `False` 改成**必填参数**，取值
   是 `CapabilityTree.tier_grading_applied()`。模块六跑在分级之前传 False，
   本维度跑在分级之后传 True。
3. 规则函数体本身没变——它一直就只是一次阈值比较。

> docs/dev/18 第 5 节原本设想给 `judge/rules.py` 加一个 `override_rule()`、并把
> 整棵 `CapabilityTree` 塞进 `inputs` 让规则自己算。**没有这么做**，两个原因：
> `inputs` 的 repr 会被拼进 `JudgeVerdict.reasoning` 落库，塞一棵树进去会让每条
> 判定记录都带一份能力树快照；`override_rule()` 会引入"同一个规则名在不同时刻
> 指向不同实现"的可能，而量化规则恰恰是报告里最像客观事实的那部分数字。
> 正文已同步修订。

**两条判定记录并存**（模块六一条、本维度一条），靠 `subject_id` 前缀区分：
`coverage:<skill_id>`（等权口径）与 `wcoverage:<skill_id>`（加权口径）。它们回答
的是两个不同的问题，不是冗余。

---

## 5. 负向约束：数据结构、id 与覆盖判定

### 5.1 `constraint_id` 的稳定性

`agents/analyzer/identity.py::build_constraint_id()`，形如
`<skill_id>:neg-<hash12>`：与 `build_capability_id()` 共用同一套归一化与哈希，
只换中缀。理由与能力 id 完全相同——`TestCase.negative_constraint_ids` 是长期存活
的绑定，id 若是抽取顺序的函数，历史绑定会在下一次抽取后集体失效且没有任何报错。

中缀分开（`cap` / `neg`）是为了让"某处把两个 id 列表填反了"一眼看得出来。

### 5.2 覆盖判定的口径（`22` 展示时要讲清楚的）

一条约束算"已覆盖"，要求存在一条用例**故意诱导智能体去踩这个坑**，而不是"用到了
相关功能"。判据两条（写在 `negative_constraint_probe.jinja` 里）：场景里存在踩坑
的机会、且违反与否可观测。

判定来源有两级：

1. **用例自带的 `negative_constraint_ids` 绑定**（Generator 出题时回填）——直接
   认，不花判定调用；
2. 其余组合走 `JudgeAgent.judgmental_verdict()`，`Criticality.ROUTINE`，
   `subject_id` 形如 `neg_probe:<constraint_id>:<case_id>`（可在 `judge_verdicts`
   里精确回查每一条判定）。

候选用例只取 `POSITIVE` + `ADVERSARIAL`，且**不含 `COLD`**。

### 5.3 ⚠️ "未覆盖"与"未判定"是两回事

`_weighted_coverage_uncovered_constraint_ids` 是**结论**（判过了，没覆盖）；
`_weighted_coverage_undetermined_constraint_ids` 是"这次没算出结论"——判定预算
（`SKILLEVAL_COVERAGE_MAX_CONSTRAINT_PROBE_CALLS`，默认 200）打满，或候选判定恰好
被黄金基准盲测占用。

**只有前者触发补题**。工作台展示时也请分开呈现：把未判定的当成盲区推给人，会让
人去补一条可能本来就覆盖着的规则。

---

## 6. `22`：工作台可以直接用的三样东西

1. **未覆盖负向约束**：`CapabilityTree.negative_constraints` 里 `covered=False`
   的条目（描述与 `covering_case_ids` 都在树上）。它们**不进** `test_case_suggestions`
   建议队列——那张表的语义是"建议人工处置某条**已有用例**"（docs/dev/17），而这里
   要做的是"补一条新用例"，两者不是一件事，系统也已经自动补了。
2. **可追溯性矩阵**：见第 7 节，直接读 JSON。
3. **逐条判定的回查**：结构化日志事件
   `weighted_coverage_constraints_mapped`（汇总）、
   `analyzer_capability_tier_assigned`（字段 `capability_id` / `tier` / `reason`，
   人要判断"这项凭什么算 P0"时看它）、
   `analyzer_negative_constraint_extracted`（字段 `constraint_id` / `description` /
   `evidence_quote`，人要判断"这条规则是不是模型脑补的"时看它——`evidence_quote`
   不进 `NegativeConstraint` 字段表，只在日志里）。

---

## 7. 可追溯性制品：schema 是对外承诺

`<SKILLEVAL_COVERAGE_ARTIFACTS_DIR>/<run_id>/traceability_matrix.json`（默认
`artifacts/<run_id>/`），同目录下同名 `.csv`。

```jsonc
{
  "skill_id": "...", "skill_version_ref": "...", "generated_at": "ISO-8601",
  "nodes": [{"id", "description", "tier", "weight", "covered", "covering_cases"}],
  "negative_constraints": [{"id", "description", "covered", "covering_cases"}],
  "combinatorial_coverage": {
    "covered_pairs": [["cap-a", "cap-b"]],
    "weighted_coverage_ratio": 0.87,
    "negative_constraint_coverage_ratio": 0.5,
    "tier_grading_applied": true
  }
}
```

- **加字段安全，改名/删字段不安全**：下游可视化工具按字段读。
- **两个百分比一并写进制品**，让这份文件自洽——下游不必为了显示一个数而重新
  实现一遍加权算法（重新实现就会漂移）。
- **CSV 是同一份数据的扁平版**（列：`kind,id,description,tier,weight,covered,
  covering_cases`，`covering_cases` 用分号分隔，组合对各占一行）。它写的是
  `utf-8-sig`：不带 BOM 的 UTF-8 CSV 在 Excel 里中文会变乱码，而"拖进表格工具"
  正是 CSV 那一半的全部理由。
- **写盘失败不阻断评测**（结论都在库里），但失败原因会进报告 findings。

### 7.1 `24`：制品归档

与 docs/dev/05 的 `benchmark.json`/HTML 走**同一套 CI 归档机制**，本文档不新建
归档管道。`upload-artifact` 配置时把 `artifacts/**/traceability_matrix.*` 一并
纳入即可。

### 7.2 Base44 热力图控制台：明确排除在本仓库之外

docs/dev/18 第 7 节的原文约定：本项目只保证 `traceability_matrix.json` 的 schema
稳定、语义自洽、可被任意下游可视化工具消费。Base44 是第三方低代码平台，接入细节
属于独立的运维/前端配置任务，**不产生新的开发文档编号**，也不在本仓库内实现。

---

## 8. `24`：主图装配清单

1. **状态 schema**：并入第 2 节的十八个私有键（与模块六的八个、模块七的十三个
   一起）。
2. **`interrupt_before`**：`nodes.weighted_coverage.graph.INTERRUPT_BEFORE_NODES`
   是**空列表**——本维度没有任何挂起点。显式导出而不是不定义，是为了让"这份文档
   忘了写"与"这份文档确实没有挂起点"分得开。
3. **`recursion_limit`**：本子图**无环**，固定 7 个超步（有约束缺口时 7，无则 6）。
4. **排序**：`coverage.finalize_pruning_report` →
   `coverage.extract_tier_and_negative_constraints`，见第 3 节。
   **16 → 17 → 18 的顺序是硬约束**（docs/dev/18 收官说明正式确认）。
5. **`coverage_summary` 透传**：`BenchmarkReport.coverage_summary` 是
   `dict[str, float]`，本维度的加权覆盖率从 `DimensionResult.score`（维度名
   `weighted_coverage`）取，与模块六的 `capability_coverage`、模块七的
   `test_suite_health` 并列，无需另开通道。
6. **成本预期**：本维度是覆盖率三份文档里最贵的一个——它有一次分级调用、一次约束
   抽取调用，外加最多 `max_constraint_probe_calls`（默认 200）次裁判调用。默认
   `judge` 走 Mini 通道，但把 `max_constraint_probe_calls` 调大之前请先算一次账。

---

## 9. 可复用的公共件

```python
from skill_evaluate.nodes.weighted_coverage import (
    prioritized_pairs,            # 树上全部两两组合，按 TIER_WEIGHTS 之和降序
    prioritized_uncovered_pairs,  # 对给定的缺口列表排序并截断
    pair_priority,                # 单对组合的优先级分值
    as_sorted_pair,               # (b, a) -> (a, b)，全项目统一的组合对归一
    build_traceability_matrix,    # CapabilityTree -> 制品 dict（纯函数，可单测）
    flatten_for_csv, write_matrix,
)
from skill_evaluate.agents.analyzer import build_constraint_id
```

`priority.py` 是**纯函数模块**（只依赖 `state/capability.py`），模块七已经改为
调用它——同一个"组合优先级怎么定"的问题必须只有一个答案。这条依赖方向是
17 → 18，不构成环。

> ⚠️ **不要把截断换成采样**（与 docs/dev/interfaces/17 第 4 节同一条）：随机采样
> 会让同一份测试集在两次运行中得到不同的组合覆盖率，那个数字就再也没法比较了。

---

## 10. 配置项

沿用 `SKILLEVAL_COVERAGE_*`（`config.py::CoverageSettings`，与模块六/七同一组，
理由是它们共享同一棵能力树）。本文档新增两项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_CONSTRAINT_PROBE_CALLS` | 200 | 反事实覆盖判定的单轮 LLM 调用上限。超限的组合记为**未判定**（不是未覆盖），如实进报告 |
| `ARTIFACTS_DIR` | `artifacts` | 可追溯性制品的输出根目录，最终路径是 `<dir>/<run_id>/traceability_matrix.{json,csv}` |

复用的还有 `MIN_COVERAGE_RATIO`（达标线）、`MAX_CONCURRENT_MAPPINGS`（判定并发
上限，与模块六的用例映射同一个旋钮——两者都是"评测系统自己发出的 LLM 请求"）、
`MAX_CAPABILITY_PAIRS_FOR_MATRIX`（组合缺口重排的范围，与模块七保持一致才能对照
着看）。

这里同样**没有**"自动放宽阈值直到通过"这类旋钮，理由与模块六一字不差。
