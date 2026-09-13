# 接入文档：用例集瘦身、组合矩阵与建议队列（docs/dev/17 留给后续模块的接口）

> 由谁接入：~~`18`（模块八——权重分级落地后替换组合矩阵的截断策略）~~ ✅ **已接入**
> （见第 4 节与 `docs/dev/interfaces/18_weighted_coverage.md`）、
> `22`（审查工作台——消费 `test_case_suggestions` 建议队列，并实现真正的淘汰动作）、
> `24`（主图装配、状态 schema 合并、节点顺序、`COLD` 用例的 Nightly 调度）。
> 当前状态：五个节点、`test_case_suggestions` 表与仓储、`DatasetSplit.COLD` 的首个
> 真实生产者、组合矩阵与截断策略全部落地，有测试覆盖
> （`tests/skill_evaluate/test_pruning.py` 33 条，不碰库、不发真实请求）。

---

## 0. 三十秒上手

```python
from skill_evaluate.nodes.coverage import add_coverage_nodes
from skill_evaluate.nodes.coverage import TERMINAL_NODE as COVERAGE_TERMINAL
from skill_evaluate.nodes.pruning import (
    ENTRY_NODE, TERMINAL_NODE, NODE_NAMES, PruningDeps,
    add_pruning_nodes, build_pruning_subgraph,
)

# A. 装进主图（docs/dev/24 的用法）：本维度必须排在模块六之后
cov = add_coverage_nodes(builder)                                  # docs/dev/16
add_pruning_nodes(builder, PruningDeps.from_coverage(cov.deps))    # docs/dev/17
builder.add_edge(COVERAGE_TERMINAL, ENTRY_NODE)                    # 16 → 17
builder.add_edge(TERMINAL_NODE, "<下一个维度>")

# B. 单独跑一遍（本地调试 / 集成测试）
graph = build_pruning_subgraph().compile(checkpointer=...)
await graph.ainvoke({
    "run_id": ..., "skill_id": ..., "skill_version_ref": ...,
    "active_suite_version_id": ...,   # 必填
    "capability_tree_id": ...,        # 必填，由模块六产出
})
```

本模块**不注册**任何量化规则，也**不产出 `JudgeVerdict`**：它没有通过/失败的结论
要判（见第 6 节）。因此没有"导入即注册"的副作用需要留意。

---

## 1. ⚠️ 三个名字：两个沿用模块六，一个必须不同

| 常量 | 取值 | 与模块六的关系 |
|---|---|---|
| `ROUTING_KEY` | `coverage_analysis` | **沿用**（从 `nodes.coverage.state` 直接导入） |
| `NODE_PREFIX` | `coverage` | **沿用**（主图里是同一个 `coverage` 分区） |
| `DIMENSION` | `test_suite_health` | **本文档独有** |

`docs/dev/interfaces/16` 第 1 节当初建议 `17` 取 `test_suite_pruning`；实际落地取的是
docs/dev/17 第 7 节原文里的 **`test_suite_health`**——本维度报告的不只是"瘦身了多少"，
还有组合覆盖缺口与孤儿用例，它衡量的是**测试集自身的健康度**，pruning 只是三件事
之一。`18` 仍按原建议取 `weighted_coverage`。

**节点名同样不能与模块六撞。** docs/dev/17 第 7 节把收尾节点也写成
`finalize_dimension_report`，与模块六重名——同一张图里节点名必须唯一，实际实现改名
为 `coverage.finalize_pruning_report`。五个节点名：

```
coverage.redundant_case_pruning              （ENTRY_NODE）
coverage.combinatorial_matrix_analysis
coverage.combinatorial_feedback_generation
coverage.orphan_case_detection
coverage.finalize_pruning_report             （TERMINAL_NODE）
```

---

## 2. ⚠️ 主图的状态 schema 必须包含本维度私有键

与模块一~六同一条坑，但本维度的症状是全项目**最危险**的一个：

三个分析节点照常跑、照常**改库**（用例真的被降级到 `COLD`、建议真的落表），只是
finalize 拿不到任何计数，于是报告里写"折叠 0 条 / 未覆盖 0 对 / 孤儿 0 条"——一份
看起来"测试集非常健康"的报告，而实际上刚刚有一批用例被移出了活跃集合。

`nodes/pruning/state.py::PruningState` 声明的十三个键必须并进主图状态：

```
_pruning_demoted_case_ids            _pruning_demoted_validation_count
_pruning_redundant_cluster_count     _pruning_uncovered_pairs
_pruning_analyzed_pair_count         _pruning_total_pair_count
_pruning_pair_coverage_ratio         _pruning_matrix_truncated
_pruning_matrix_tier_ranked          _pruning_patched_pair_count
_pruning_patch_failure               _pruning_orphan_case_ids
_pruning_new_suggestion_count
```

`finalize_pruning_report` 对"组合覆盖率取不到"的处理是判 `NEEDS_HUMAN_REVIEW` 并在
findings 里点名 `_pruning_pair_coverage_ratio`，不是判 PASS——漏并了 schema 至少能
从报告里看出来。

---

## 3. 前置依赖与排序：必须排在模块六之后

三个分析节点全部读取模块六的产出（`CapabilityTree` 与
`TestCase.target_capability_ids`）。能力树没建好时，"没有冗余、没有组合缺口、没有
孤儿"这三个结论都是假的，而且是**看起来很健康**的假结论。

- `capability_tree_id` 取不到 → `PersistenceError`，错误信息里点名这条顺序约束；
- `active_suite_version_id` 取不到 → `PersistenceError`（同模块六的理由：
  "这个测试集没有冗余"和"我根本没拿到测试集"是两件完全不同的事）。

反过来，本维度会**修改**两样东西，排在它之后的维度要意识到：

1. **一批用例的 `split` 变成了 `COLD`**。它们仍在
   `test_suite_versions.case_ids` 里，但按 `TRAIN`/`VALIDATION` 过滤的消费方
   （文档 11/13/15 等）从此看不到它们。这正是 docs/dev/02 设计 `COLD` 时的
   "惰性过滤"意图，本模块是它的**第一个真实生产者**。
2. **`active_suite_version_id` 可能被换成新版本**（组合缺口补题时）。与模块六
   一样是数据飞轮的意图而非副作用。

若某个维度对"所有维度必须跑同一批题"有要求，把它排在 `coverage` 分区之前。

---

## 4. `18`：把组合矩阵的截断策略换成正式实现 —— ✅ 已落地

> ✅ `18` 接管了这个插槽，但实际结果与本节当初的预期有**一处重要出入**，因为
> 顺序是 16 → 17 → 18 而权重分级是 18 的第一个节点：
>
> - **同一轮里本维度看到的 tier 仍然是占位值**，`_tier_ranked()` 仍然返回 False，
>   报告里那句"未做优先级筛选的截断分析"**不会**在接入当轮消失。真实分级落库后，
>   **下一轮**评测的本节点才会看到它并翻成 True。
> - 当轮的真实优先级由 `18` 的 `coverage.upgrade_combinatorial_priority` 节点重排
>   一次给出（它读 `combinatorial_pairs_covered`，不重扫用例）。
> - 排序实现已收敛到 `nodes/weighted_coverage/priority.py::prioritized_pairs()`，
>   本维度的 `_prioritized_pairs()` 改为委托调用；`_tier_ranked()` 同样委托给
>   `CapabilityTree.tier_grading_applied()`。同一个问题在两处各判一次会慢慢漂移。
> - 排序口径本身换了：从"档位序号之和"改成 **`TIER_WEIGHTS` 之和**（docs/dev/18
>   第 6 节定的）。差异只体现在 P0×P2（0.7）与 P1×P1（0.6）谁优先上，两种答案都
>   说得通，统一采用 18 定的那种——权重表全项目只该有一张。

当前实现已经**预留好了插槽**，权重分级落地后不需要改结构，只需要确认行为：

`nodes/pruning/nodes.py::_prioritized_pairs()` 按
`(tier 之和, 两者中较低的优先级, id 对)` 排序全部两两组合，超过
`max_capability_pairs_for_matrix`（默认 100）时截断取前 N 对。文档 18 把
`CapabilityNode.tier` 填成真实分级之后：

- 排序**自动**变成"P0×P0 → P0×P1 → P1×P1 → P0×P2 → …"，无需改代码；
- `_tier_ranked()` 自动翻成 `True`，报告里那句"未做优先级筛选的截断分析，建议待
  模块八完成后重新评估"随之消失。

`_tier_ranked()` 的判据是**树上是否出现了不止一档 tier**，而不是"是否等于
`PLACEHOLDER_TIER`"：这样文档 18 若改用别的占位策略，这里也不会悄悄给出错误答案。

⚠️ **不要把截断换成采样**。随机采样会让同一份测试集在两次运行中得到不同的组合
覆盖率，那个数字就再也没法拿来比较了——而"组合覆盖率有没有涨"正是本维度存在的
意义。要提高覆盖范围请调大 `max_capability_pairs_for_matrix`。

### 4.1 `18` 可以直接读的事实

`CapabilityTree.combinatorial_pairs_covered` 由本模块首次真实写入，内容是**全部**
已覆盖的组合对（含本轮分析范围之外的），每对都已排序（`(a, b)`，`a < b`）。
加权覆盖率若要把"组合覆盖"计入，直接读它即可，不必重算。

注意它**只统计非 `COLD` 的正向用例**，并且只保留仍在能力树上的 id——前者避免
"靠已降级的冗余用例刷组合覆盖率"的悖论，后者避免库里留下指向不存在节点的组合对。

---

## 5. `22`：审查工作台怎么消费建议队列

### 5.1 表与仓储

`test_case_suggestions`（Alembic `0008_test_case_suggestions`），仓储是
`persistence/repository.py::TestCaseSuggestionRepository`：

```python
repo = TestCaseSuggestionRepository()
pending = await repo.list_by_status(SuggestionStatus.PENDING)          # 工作台主查询
await repo.update_status(sug.suggestion_id, SuggestionStatus.CONFIRMED)  # 人工决策
```

- `save_if_absent()` 是**评测侧**的唯一写入口，靠 `(case_id, suggestion_type)`
  唯一约束去重：同一条孤儿用例连续三次评测都会被检出，但人只需要处理一次。
- `update_status()` 只接受 `CONFIRMED` / `REJECTED`，且 `WHERE status='pending'`
  ——它天然幂等，并且挡住"把已确认淘汰的建议改回 pending"这类会让审计线索断掉的
  操作。要重开请新建一条建议。
- 已被 `REJECTED` 的建议**不会**被评测重新推回待办。人已经判断过"这条孤儿用例要
  留着"，系统不该每跑一次就再问一遍。

### 5.2 ⚠️ 真正的淘汰动作属于 `22`，不属于本模块

> ✅ **`22` 已实现**：`POST /api/suggestions/{id}/decide`（`confirmed` / `rejected`）。确认后执行
> `TestCaseRepository.retire(case_id)`：`split → COLD`（**归档不删除**，仍留在
> `test_suite_versions.case_ids` 里保证历史可复现）。仓储追加 `get(suggestion_id)`；确认动作按
> `SuggestionType` 显式分派，新类型未登记时 501。评测流水线里仍然没有任何自动淘汰路径。

架构文档模块七"应对方案"原文：*瘦身节点默认只做"降级运行"或"建议剔除"，硬性的
删除操作必须在内部审核工作台上，保留人类开发者的最终 Review 确认权限。*

落到代码上：`nodes/pruning/` 里**没有任何一条删除用例的代码路径**，
`TestCaseSuggestionRepository` 也**刻意不提供** `delete()`。`status=CONFIRMED` 的
语义只是"人已确认可以淘汰"；把用例从 `test_suite_versions.case_ids` 里移除（或
归档）是 `22` 要实现的动作。请不要在评测流水线里加一条自动执行它的路径。

### 5.3 工作台该给人看什么

- `reason` 字段里已经带了**消失的能力 id 列表**与**用例 prompt 的前 200 字**。
  人要判断的是"这条题该淘汰，还是该重新绑定到改名后的能力上"——只给一串哈希等于
  把这个判断重新推回给人自己去查库。
- 想追溯"哪次运行检出的"，看结构化日志事件 `pruning_orphan_case_detected`
  （字段 `run_id` / `case_id` / `stale_capability_ids` / `suggestion_created`）。
  表上**没有** `run_id` 列是刻意的：建议的生命周期比一次运行长得多，挂上 run_id
  会诱使工作台按运行过滤，从而漏掉上周检出、至今没人管的那些。

### 5.4 被降级的用例也值得一个界面

`split=COLD` 的用例没有进建议队列（它们不需要人确认——降级是可逆且低风险的），
但工作台若想展示"本次评测折叠了哪些用例"，可从结构化日志事件
`pruning_case_demoted`（字段 `case_id` / `representative_case_id` /
`capability_path`）取，或直接查 `test_cases.split = 'cold'`。

⚠️ 若工作台提供"把某条 COLD 用例恢复为 TRAIN"的操作，注意它会在下一次评测中
**被重新降级**（同一簇里代表没变）。要让恢复持久，正确做法是先修改该用例使其能力
路径真正不同（那它本来就不是冗余），而不是反复改 `split`。

---

## 6. 本模块为什么不产出 `JudgeVerdict`、也不产出 FAIL

`docs/dev/interfaces/08` 第 0 节的铁律是"凡是**通过/失败**的结论一律经过
JudgeAgent"。本维度没有这样的结论：

- `DimensionResult.status` 恒为 `PASS`；唯一的例外 `NEEDS_HUMAN_REVIEW` 不是门禁
  而是**故障信号**（"这次分析根本没跑出数"，见第 2 节）；
- `blocking` 恒为 `False`。它评估的是测试集自身的健康度而不是 Skill 质量，判 FAIL
  会让一个功能完全正确的 Skill 因为"测试集还不够全"被拦下，最终结果是所有人都学会
  绕过这条门禁。

因此铁律在这里**没有适用对象**。请不要为了"形式统一"补一条"测试集健康度阈值"
量化规则——那道门禁该定在多少，没有人回答得了。

`score` 填的是**组合覆盖率**（本维度唯一有连续取值的量）。折叠条数与孤儿条数是
计数不是比率，塞进 `score` 会让 `BenchmarkReport` 里的分数失去可比性。

---

## 7. `24`：主图装配清单

1. **状态 schema**：并入第 2 节的十三个私有键（与模块六的八个一起）。
2. **`interrupt_before`**：`nodes.pruning.graph.INTERRUPT_BEFORE_NODES` 是
   **空列表**——本维度没有任何挂起点。显式导出而不是干脆不定义，是为了让"这份
   文档忘了写"与"这份文档确实没有挂起点"分得开。
3. **`recursion_limit`**：本子图**无环**，固定 5 个超步（有组合缺口时 5，无则 4）。
4. **排序**：`coverage.finalize_dimension_report` → `coverage.redundant_case_pruning`，
   见第 3 节。
5. **`COLD` 用例的 Nightly 调度**（架构文档"仅在周末的 Nightly Build 中运行"）：
   本模块只负责打标签，调度是 `24` 的事。CI 里新增一个独立的定时 workflow，按
   `split=COLD` 取题跑一遍即可——查询用
   `TestCaseRepository.list_by_categories()` 取全部正向用例后按 `split` 过滤，
   或按需要在仓储层加一个按 split 过滤的方法（当前没有，因为除 Nightly 外没人需要
   主动查 COLD）。

---

## 8. 配置项

沿用 `SKILLEVAL_COVERAGE_*`（`config.py::CoverageSettings`，与模块六同一组，
理由是它们共享同一棵能力树）。本文档新增两项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_CAPABILITY_PAIRS_FOR_MATRIX` | 100 | 组合矩阵单轮分析上限（约 14 个能力节点内可做全组合）。超限按权重排序后截断 |
| `MAX_COMBINATORIAL_PATCH_PER_ROUND` | 5 | 单轮针对未覆盖组合对的补生成上限。**调成 0 的语义是"只分析、不自动补题"**，路由会尊重它并跳过补题节点 |

冗余折叠**没有**任何阈值旋钮：聚类口径是"能力路径完全相等"这一精确的集合关系，
没有相似度阈值可调。这是刻意的——语义相似度阈值定在哪都会同时产生两类相反的错误
（把边界条件用例当冗余折掉、把真冗余留下），且两者都无法从报告里看出来。

---

## 9. 可复用的公共件

```python
from skill_evaluate.state.suggestion import TestCaseSuggestion
from skill_evaluate.state.enums import SuggestionStatus, SuggestionType
from skill_evaluate.persistence.repository import TestCaseSuggestionRepository
```

`TestCaseSuggestion` 是**通用**的"建议人工处置某条用例"契约，不限于孤儿淘汰。
将来若有别的模块想产出"建议合并这两条用例"之类的待办，加一个 `SuggestionType`
枚举值即可复用整套表、仓储与工作台，不要另起一张表。

**加新类型时请一并检查 `22` 的工作台**：不同类型要人确认的东西完全不同（"淘汰
孤儿用例"和"合并重复用例"的操作按钮不一样），做成枚举而不是裸字符串，正是为了让
工作台漏掉新类型时会显式地走不进任何分支，而不是静默走进 else。
