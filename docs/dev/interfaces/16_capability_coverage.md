# 接入文档：Analyzer Agent、能力树与模块六子图（docs/dev/16 留给后续模块的接口）

> 由谁接入：`17`（模块七——冗余折叠 / 组合矩阵 / 漂移检测）、`18`（模块八——权重
> 分级 / 负向约束 / 加权覆盖率）、`24`（主图装配、状态 schema 合并、`interrupt_before`
> 汇总、`coverage_summary` 透传）、`22`（接住能力树规模超阈值的人工审核卡片）、
> `23`（能力树是长时记忆的天然索引键）。
> 当前状态：`AnalyzerAgent`（抽取 + 映射）、`capability_id` 稳定性方案、五个节点、
> 一条量化规则、带环子图与两道出口全部落地，有测试覆盖
> （`tests/skill_evaluate/test_coverage.py` 45 条，不碰库、不发真实请求）。
> **`17` 已按本文档接入完毕**（见第 4.4 节与
> `docs/dev/interfaces/17_test_suite_pruning.md`）；`18`/`22`/`24` 的接入点仍然空着。

---

## 0. 三十秒上手

```python
from skill_evaluate.nodes.coverage import (
    ENTRY_NODE, TERMINAL_NODE, NODE_NAMES, CoverageDeps,
    add_coverage_nodes, build_coverage_subgraph,
)

# A. 装进主图（docs/dev/24 的用法）：只加维度内部的边，外部连线由主图决定
pipeline = add_coverage_nodes(builder)
builder.add_edge("bootstrap.ensure_test_suite", ENTRY_NODE)   # 见第 3 节：有前置依赖
builder.add_edge(TERMINAL_NODE, "finalize.report")

# B. 单独跑一遍（本地调试 / 集成测试）
graph = build_coverage_subgraph().compile(checkpointer=...)
await graph.ainvoke({
    "run_id": ..., "skill_id": ..., "skill_version_ref": ...,
    "active_suite_version_id": ...,      # 必填，见第 3 节
})
```

**导入即注册**：`import skill_evaluate.nodes.coverage` 会把
`capability_coverage_threshold` 注册进 docs/dev/08 的规则表。

---

## 1. ⚠️ 三个名字各司其职，不要合并

本维度是全项目**唯一**一个"后端路由键 / 图节点前缀 / 报告维度名"三者不同的维度。
三个常量都在 `nodes/coverage/state.py`：

| 常量 | 取值 | 谁在读 | 谁共用 |
|---|---|---|---|
| `ROUTING_KEY` | `coverage_analysis` | `NODE_BACKEND_ROUTING` | 模块六/七/八共用 |
| `NODE_PREFIX` | `coverage` | 图节点名前缀 | 模块六/七/八共用 |
| `DIMENSION` | `capability_coverage` | `DimensionResult.dimension` | **本文档独有** |

**`17`/`18` 必须各自取一个新的 `DIMENSION` 值**。`dimension_results` 的唯一约束是
`(run_id, dimension)`，三份文档若都写 `coverage_analysis`，后跑完的那个会把先跑完的
**整行覆盖掉**，报告里只剩一个维度，而且不会有任何报错。

> ✅ **`17` 已落地**，取的是 `test_suite_health`（不是这里当初建议的
> `test_suite_pruning`）：它报告的不只是瘦身，还有组合覆盖缺口与孤儿用例，衡量的是
> **测试集自身的健康度**。`18` 仍按建议取 `weighted_coverage`。

`ROUTING_KEY` 与 `NODE_PREFIX` 则**要**沿用：三份文档共享同一棵能力树，在主图里被
装配为同一个 `coverage` 分区，前缀不一致会让这件事在 `get_graph().draw()` 里看不
出来。新增节点请写 `f"{NODE_PREFIX}.<node>"`，并确认名字不与
`nodes/coverage/nodes.py::NODE_NAMES` 里已有的五个重复。

---

## 2. ⚠️ 主图的状态 schema 必须包含本维度私有键

与模块一~五同一条坑，症状不同：本维度的私有键被 LangGraph 静默裁掉时，表现是
**能力树抽出来了、盲区一个都没检出、覆盖率显示 100%**，然后给出一份看起来完美的
报告。

`nodes/coverage/state.py::CoverageState` 声明的八个键必须并进主图状态：

```
_coverage_blind_spots          _coverage_ratio
_coverage_patch_iterations     _coverage_patch_exhausted
_coverage_patch_failure        _coverage_tree_node_count
_coverage_mapped_case_count    _coverage_tree_review_confirmed
```

最省事的做法是让主图状态直接继承各维度的 `*State`（它们都是
`PipelineState` 的 `total=False` 扩展，可以安全地多重继承）。

`finalize_dimension_report` 对"覆盖率键取不到"的处理是判 `NEEDS_HUMAN_REVIEW` 并在
findings 里点名这个键，不是判 PASS——漏并了 schema 至少能从报告里看出来。

---

## 3. 前置依赖：本维度**不能**第一个跑

`map_case_coverage` 读 `state["active_suite_version_id"]`，取不到直接抛
`PersistenceError`（不静默跳过：没有用例集时"覆盖率 0%"和"这次没测"是两件完全不同
的事）。因此主图必须先走过 `TestSuiteService.ensure_test_suite()` 并把版本号写进
状态，再进 `ENTRY_NODE`。

反过来，本维度会**修改** `active_suite_version_id`：检出盲区后
`feedback_driven_generation` 落一个新的 active 版本并写回状态。这是架构文档"反向
驱动与数据飞轮闭环"的意图，不是副作用——但装配主图时要意识到：**排在覆盖率维度
之后的维度会看到补过盲的用例集**，排在它之前的看到的是补盲前的。若某个维度对
"所有维度必须跑同一批题"有要求，把它排在覆盖率之前。

---

## 4. `17`/`18` 怎么挂自己的节点

两份文档都在同一个 `coverage` 分区里加节点，但**不要改 `graph.py` 里的边**。各自
提供自己的 `add_*_nodes(builder, deps)`，由主图（docs/dev/24）串起来：

```python
cov_pipeline = add_coverage_nodes(builder, deps)                             # 16
add_pruning_nodes(builder, PruningDeps.from_coverage(cov_pipeline.deps))     # 17
builder.add_edge(TERMINAL_NODE, pruning.ENTRY_NODE)
```

> ✅ **`17` 已落地**（`nodes/pruning/`）。两条经验：
>
> - 它另建了一个包而不是塞进 `nodes/coverage/`：图上是同一个分区（节点名前缀仍是
>   `coverage.`），代码上是三件独立的事。`18` 照此办理。
> - **节点名也要查重，不只是 `DIMENSION`**。docs/dev/17 原文的收尾节点也叫
>   `finalize_dimension_report`，与本文档撞名，实现改为 `finalize_pruning_report`。
>   写新节点前先比一次 `nodes/coverage/nodes.py::NODE_NAMES` 与
>   `nodes/pruning/nodes.py::NODE_NAMES`。
> - 复用依赖的入口是 `PruningDeps.from_coverage(deps)`（逐字段搬运，共享已惰性
>   构造好的 Agent 实例）。`18` 可以照抄这个写法。

**顺序是有约束的**：`17`/`18` 的节点必须排在
`coverage.finalize_dimension_report` **之后**。能力树要先建好、覆盖标记要先算完
（含补盲回环收敛），冗余折叠与加权分级才有输入。

复用同一个 `CoverageDeps` 实例（`add_coverage_nodes()` 的返回值上有 `.deps`）比各自
`CoverageDeps()` 更好：`AnalyzerAgent` 只实例化一次，避免两个实例各自持有不同的
`trace_handle` 而让 Langfuse 上出现两条独立的 Agent 调用线。`17` 的做法是让
`PruningDeps` 继承 `CoverageDeps` 并提供 `from_coverage()` 逐字段搬运，`18` 可照抄。

### 4.1 `18`：权重分级怎么原地更新 `tier`

当前所有 `CapabilityNode.tier` 都是占位值
`agents.analyzer.service.PLACEHOLDER_TIER`（`P1_CONDITIONAL`），**不代表真实分级**。

接入方式：在 `AnalyzerAgent` 上新增一个分级子任务方法（新增模板文件 +
新增 `_call_llm()` 调用），读回能力树 → 逐节点定 tier → `CapabilityRepository.save()`。

**不要重新生成 `capability_id`**。它是描述文本的确定性哈希（
`agents/analyzer/identity.py`），只要描述没变，重抽得到同一个 id，
`TestCase.target_capability_ids` 里的历史绑定就仍然有效。若你改用别的 id 方案，
后果是覆盖率在某次运行后毫无征兆地从 92% 掉到 40%，且没有任何报错。

选 P1 作占位值也是有理由的：占位值会被 `CapabilityTree.weighted_coverage()` 当真。
全填 P0 会让接入前的任何一次加权计算都得出"全部是核心能力"这一最激进的口径，全填
P2 则相反；取中间档错得最不离谱。

### 4.2 `18`：怎么把覆盖率换成加权版本

`capability_coverage_threshold` 这条规则的**实现体**要被替换，**规则名不变**，
调用方（`blind_spot_detection` 节点）不需要修改——这是 docs/dev/16 第 9 节的约定。

落到代码上：**直接改写 `nodes/coverage/rules.py::_capability_coverage_threshold`
的函数体**，以及 `coverage_inputs()` 里 `tier_weighted` 的取值（翻成 True，让历史
判定记录仍能区分是拿哪种口径算的）。

**不要**在 `nodes/weighted_coverage/rules.py` 里再 `@register_rule` 一条同名规则——
`register_rule()` 遇到重名会抛 `JudgeRuleError`。这是有意的：静默覆盖会让"这次判定
到底用的哪条规则"无法追溯。

同时要改 `blind_spot_detection` 里 `coverage_ratio` 的算法（换成
`tree.weighted_coverage()`），以及 `finalize_dimension_report` 的比较——那两处目前
都用未加权的简单比例，规则改了而算法没改的话，判定与报告会给出两个不同的数。

### 4.3 `18`：负向约束抽取

`CapabilityTree.negative_constraints` 当前恒为空列表。`AnalyzerAgent` 里加一个
反事实约束抽取子任务（`SKILL.md` 的 Gotchas / 避坑指南 → `NegativeConstraint`），
写回同一棵树。

`NegativeConstraint.constraint_id` 请**照 `build_capability_id()` 的做法**做描述文本
的确定性哈希（可以直接复用它，或按同样思路加一个 `build_constraint_id()`），理由
与 4.1 完全相同：`TestCase.negative_constraint_ids` 也是长期存活的绑定。

补盲侧不需要改：`CapabilityFocus` 已经有 `negative_constraint_ids` 与
`descriptions` 字段，`feedback_driven_generation` 里多填一项即可。

### 4.4 `17`：组合矩阵与冗余折叠 —— ✅ 已落地

接入文档见 `docs/dev/interfaces/17_test_suite_pruning.md`。下面三条保留原文，
并标注实际结果：

- `CapabilityTree.combinatorial_pairs_covered` 当前未使用，由 `17` 的组合矩阵分析
  节点填充。→ **已填充**：内容是全部已覆盖组合对（每对已排序、只含仍在树上的 id、
  不含 `COLD` 用例贡献的）。`18` 想把组合覆盖计入加权口径时直接读它即可。
- 冗余用例折叠与孤儿用例检测的输入已经就位：`TestCase.target_capability_ids`
  由本文档首次真实写入，`CapabilityNode.covering_case_ids` 记录了反向索引。
- `map_case_coverage` 已经把"指向已消失能力的旧绑定"识别出来并打日志
  （事件名 `coverage_orphan_case_binding`，字段 `case_id` / `capability_id`），
  但**不做任何淘汰动作**——那是 `17`"平滑淘汰"的职责（架构文档要求先向开发者发出
  确认提示，而不是直接剔除）。→ **已实现**为 `coverage.orphan_case_detection`：
  直接比对 `TestCase.target_capability_ids` 与树上现存 id 集合，判据是**全部**绑定
  能力都已消失（部分消失的用例仍测得到其余能力，淘汰它是纯粹的损失），检出后写
  `test_case_suggestions` 的**非阻塞**建议队列，仍然不做任何淘汰动作。

> ⚠️ **`17` 落地后新增的一条全局事实：`TestCase.split` 现在会被评测流水线改写。**
> 冗余折叠会把同簇的非代表用例降级为 `DatasetSplit.COLD`，本项目从此有了 `COLD`
> 用例。按 `TRAIN`/`VALIDATION` 过滤的消费方（文档 11/13/15 等）**不需要改**——
> 那正是文档 02 设计 `COLD` 时的"惰性过滤"意图；但排在 `coverage` 分区之后的维度
> 会看到一个比之前小的活跃用例集，装配主图时要意识到这一点。

---

## 5. `22`：接住能力树规模超阈值的人工审核卡片

`extract_capability_tree` 在 `len(tree.nodes) > capability_count_review_threshold`
（默认 20）时挂起，这是架构文档给模块六"应对方案"的落地点。

- **待办记录**：挂起前先 `HumanApprovalRepository.create()`，
  `wait_key = f"{run_id}:coverage:tree_review"`，`thread_id = run_id`。
- **挂起原因**：`capability_tree_size_exceeds_threshold:<节点数>`。
- **唤醒 payload**：被认作"确认继续"的形状有
  `"confirm"` / `{"decision": "confirm"}` / `{"confirmed": true}`。
  **其余一切（含 `None` 和形状对不上的）都算未确认**，节点会抛 `PipelineSuspended`。
  默认不通过是刻意的——一棵没被明确确认过的能力树若被当成确认过的继续算下去，
  得到的是一份建立在错误粒度之上、却看起来正常的覆盖率报告。
- **审批工作台该给人看什么**：能力条目的描述与它们的 `evidence_quote`。后者不进
  `CapabilityNode`（字段表由 docs/dev/02 定，本文档不擅自扩展），但逐条落在结构化
  日志事件 `analyzer_capability_extracted` 里（字段 `capability_id` / `description`
  / `evidence_quote`）。人要判断的是"这 20 多项是不是把执行步骤拆成能力了"，看引文
  比看抽象描述快得多。

⚠️ **挂起恢复后节点整体重跑**（LangGraph 动态 `interrupt()` 的语义，全项目一致），
即会重新调一次抽取。能力树不会因此错乱（`capability_id` 是确定性哈希，落库是按
`(skill_id, skill_version_ref)` 的 upsert），代价只是一次 LLM 调用。

---

## 6. `24`：主图装配清单

1. **状态 schema**：并入第 2 节的八个私有键。
2. **`interrupt_before`**：`nodes.coverage.graph.INTERRUPT_BEFORE_NODES`
   （`[coverage.extract_capability_tree]`）。它走的是动态 `interrupt()`，不加也能
   挂起；列出来是为了让"这个节点可能停在人工审核卡片上"在编译期显式可见。
3. **`recursion_limit`**：⚠️ **本维度是全项目唯一一张带环的子图**。最坏情况
   `1 + (max_patch_iterations + 1) × 3 + 1` 个超步（默认配置约 14）。主图把十个维度
   串起来后总步数会累加，LangGraph 默认的 25 很可能不够，请显式设置。
4. **`coverage_summary` 透传**：`BenchmarkReport.coverage_summary` 是
   `dict[str, float]`，本维度的覆盖率从 `DimensionResult.score`（维度名
   `capability_coverage`）取，无需另开通道。
5. **排序**：见第 3 节——覆盖率维度必须排在 `ensure_test_suite()` 之后；排在它之后
   的维度会看到补过盲的用例集。

---

## 7. 可复用的公共件（给任何后续文档）

```python
from skill_evaluate.agents.analyzer import (
    AnalyzerAgent,               # 抽取 + 映射，两个方法都不落库
    build_capability_id,         # 描述文本 → 稳定 id
    build_capability_tree_id,    # (skill_id, version_ref) → PipelineState 里的扁平 id
    parse_capability_tree_id,    # 反解（用 rpartition，skill_id 允许含冒号）
    normalize_capability_text,   # 只归一书写差异，**不做**语义等价判断
)
```

`normalize_capability_text()` 的边界值得单独说明：它只吸收空白、大小写、全角半角
与常见中英标点变体这类**抽取噪声**，不做同义词替换或词干还原。语义等价一旦交给
启发式，就会同时产生"改了个词但系统认为还是同一项能力"与"没改语义但系统认为换了
一项能力"这两类相反的错误，而两者都无法从报告里看出来。想做语义聚类（`17` 的冗余
折叠会需要）请另起一层，不要改这个函数。

---

## 8. 配置项

`SKILLEVAL_COVERAGE_*`（`config.py::CoverageSettings`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `CAPABILITY_COUNT_REVIEW_THRESHOLD` | 20 | 超过就挂人工审核卡片 |
| `MIN_COVERAGE_RATIO` | 0.9 | 达标线，架构文档的 ">= 90%" |
| `MAX_PATCH_ITERATIONS` | 3 | 补盲回环硬上限，防"无限重试死锁" |
| `MAX_CONCURRENT_MAPPINGS` | 10 | 用例映射的并发上限（LLM 请求，不是沙箱） |

另有 `SKILLEVAL_LLM_ANALYZER_MODEL`（默认 `anthropic/claude-sonnet-5`）。走高档
模型而不是廉价的 mini 档，是因为能力树是模块六/七/八共同的分析基座：拆错一项会一路
传染到覆盖率、瘦身与加权算法，而重跑的代价是整条补盲回环。

这里**没有**"自动放宽阈值直到通过"这类旋钮，这是刻意的：覆盖率不达标时正确的动作
是把事实写进报告交给人判断（是能力树切太细还是测试集真的不足），而不是让机器自己
把及格线降到刚好能过。
