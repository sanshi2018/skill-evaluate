# pruning 子图（模块七：用例集瘦身与动态演进）

> 代码：`src/skill_evaluate/nodes/pruning/graph.py`（装配）、`nodes.py`（五个节点）、
> `state.py`（13 个私有键）、`deps.py`（依赖容器）
> 对应文档：`docs/_5_关键架构.md` 模块七、`docs/dev/17_模块七_用例集瘦身与动态演进.md`

---

## 一句话概括

**这张图不评测 Skill，它评测"题库"本身。**

整个项目的其他维度（模块一~六、八~十一）都在回答"这个 Skill 行不行"，
只有模块七回答的是"我们拿来考它的这套题行不行"。

用考试来类比：

| 角色 | 对应模块 |
|---|---|
| 出题老师 | Generator Agent（模块六） |
| 阅卷老师 | Judge Agent（模块八文档） |
| **教务处整理题库的人** | **模块七（本子图）** |

教务处干三件事：
1. 把重复的题合并掉（同一个知识点五道换了名字的题，留最有代表性的一道）；
2. 检查有没有"综合题"缺失（单个知识点都考到了，但"知识点 1+2 一起用"一道题都没有）；
3. 把已经删掉的考纲章节对应的题捞出来，报给人工（不是自己删）。

---

## 一、有哪些 node

图上共 **5 个节点 + 1 个条件路由函数**。节点名全部带 `coverage.` 前缀
（模块六/七/八在主图里共用同一个 `coverage` 图分区）。

```
（承接模块六 coverage.finalize_dimension_report 之后）

coverage.redundant_case_pruning                冗余折叠
        ↓
coverage.combinatorial_matrix_analysis         N×N 组合矩阵
        ↓
   route_after_matrix（条件路由）
        ├─ 有缺口 且 预算>0 ──→ coverage.combinatorial_feedback_generation  定向补题
        │                                         ↓
        └─ 否 ────────────→ coverage.orphan_case_detection  ←──────┘   孤儿检测
                                        ↓
                            coverage.finalize_pruning_report   写 dimension_results
```

`ENTRY_NODE = coverage.redundant_case_pruning`
`TERMINAL_NODE = coverage.finalize_pruning_report`
`INTERRUPT_BEFORE_NODES = []`（空列表，见第四节）

### 1. `redundant_case_pruning` — 冗余用例折叠

**它干什么**：把 `target_capability_ids` **完全相同**的正向用例聚成一簇，
每簇留一条"最具代表性"的，其余 `split` 改成 `COLD`。

**聚类口径是集合相等，不是语义相似**。
这是个有意识的选择：语义聚类需要一个相似度阈值，而那个阈值定在哪都会同时
产生两类相反的错误——把真正测边界条件的用例当冗余折掉、把真冗余留下，
而且两类错误都看不出来。集合相等是精确关系，不需要 LLM。

**代表性打分是五元组**（`_representativeness_score`），按优先级从高到低：

```python
(
    0 if case.split is COLD else 1,   # 1. 仍是活跃用例
    _length_bucket(case.prompt),      # 2. prompt 长度分桶（1.2 倍对数分桶 ≈ 20%）
    1 if case.split is TRAIN else 0,  # 3. 优先留 TRAIN
    len(case.prompt),                 # 4. 同桶内更长者
    case.case_id,                     # 5. 字典序兜底
)
```

这五档里有三档纯粹是为了**幂等和确定性**，值得单独记一下（dev/17 第 4.1 节的
原始伪码只有两档，落地时发现都是坑）：

- 第 1 档"是否活跃"：没有它本节点就**不幂等**。第二轮跑时，若上一轮被降级的那条
  COLD 用例 prompt 更长，它会被选成新代表，于是上一轮留下的活跃用例也被降级
  → **整簇冷掉**，这条能力路径从此没有任何活跃用例覆盖，而且报告上看不出来。
- 第 2 档用"分桶"而不是直接比长度：直接比长度的话，第 3 档的 TRAIN 偏好
  **永远不会生效**——长度只要差一个字就把它压过去了。
- 第 5 档 `case_id`：仓储返回顺序由数据库决定，没有它 `max()` 返回"第一个达到
  最大值的元素"，同一批数据两次运行可能选出不同代表，于是**每次评测都降级一批
  不同的用例**。

另外两个细节：
- 整簇已经全是 COLD 时，把代表恢复成 `TRAIN` 并打 `pruning_representative_restored`
  警告。这不算"自动撤销人工决策"——`COLD` 在全项目里只有本节点一个生产者，
  人工决策走的是建议队列。
- 已经是 COLD 的不重复计数，否则报告里"折叠冗余用例 12 条"会在连续每轮评测里
  重复出现同样的 12 条，看起来像测试集在持续膨胀。
- 被降级的用例里原属 `VALIDATION` 的条数单独统计 —— 验证集是优化闭环（dev/09）
  判断"补丁有没有真的改好"的依据，被瘦身缩小时应当有人知情。

### 2. `combinatorial_matrix_analysis` — 组合能力覆盖矩阵

**它干什么**：对能力树上的节点做两两组合（`itertools.combinations`，N² 级别），
统计哪些组合对**没有被同一条活跃用例同时触发**。

架构文档的原始意图：*即使单个能力的覆盖率都是 100%，"能力 1 + 能力 2 并发调用"
的场景仍可能一条题都没有。* 很多 Skill 缺陷就藏在这个交叉点上。

**三处口径值得记住**：

| 口径 | 为什么 |
|---|---|
| 已降级（COLD）用例**不计入**覆盖 | 否则出现悖论：刚被折叠的冗余用例仍在给组合覆盖率贡献分子，瘦身看不出任何代价，而那条题以后只在 Nightly 跑 |
| 超限时**截断**而非采样 | 随机采样会让同一份测试集两次运行得到不同的覆盖率，那个数字就没法比较了。上限 `max_capability_pairs_for_matrix=100`（约 14 个能力节点的全组合） |
| 只统计**仍在树上**的 id | 用例可能带着指向已消失能力的旧绑定（那正是下一个节点要处理的孤儿），否则库里会留一批指向不存在节点的组合对 |

**覆盖率的分母是"分析范围内"的组合对**，不是全量。截断之后拿全量做分母，
得到的数字既不是分析范围的覆盖情况也不是全量的覆盖情况，谁也解释不了。
但落库的 `tree.combinatorial_pairs_covered` 仍是**全部**已覆盖组合对——
那是能力树上的事实记录，不该因为本轮的分析预算而缺一块。

**截断排序的现状（一个时序上的小尴尬）**：
排序委托给 `nodes/weighted_coverage/priority.py::prioritized_pairs()`，
按 `TIER_WEIGHTS` 之和降序（P0×P0=1.2 最前，P2×P2=0.2 最后）。
但模块八的**分级节点排在本维度之后**（16 → 17 → 18），所以同一轮里本节点看到的
`tier` 还是占位值，排序退化为按 id 排，报告会如实写"未做优先级筛选的截断分析"。
真实分级落库后，**下一轮**才生效；当轮的真实优先级由模块八的
`upgrade_combinatorial_priority` 节点重排一次给出。

### 3. `combinatorial_feedback_generation` — 定向补题（条件节点）

**它干什么**：把未覆盖的组合对填进 `CapabilityFocus.combinatorial_pairs`，
调 `TestSuiteService.incremental_patch()` 让 Generator 专门生成"要求同时运用这
两种能力"的复杂 Prompt。`triggered_by="combinatorial_gap"`。

**为什么是新节点，而不是复用模块六的 `feedback_driven_generation`**
（dev/17 第 3 节原本写的是复用）——两个各自独立的原因：

1. 同一张图里节点名唯一。模块六/七在主图是同一个 `coverage` 分区，让两条不同的边
   都指向那一个节点，等于把**模块六的补盲回环接进模块七的直线流程**——那个节点的
   两个出口都会把控制权交回 `coverage.map_case_coverage`。
2. 那个节点的输入是 `_coverage_blind_spots`（模块六私有键），模块七读它就违反了
   "各维度不得读写其他维度私有键"的约定。

**该复用的是服务而不是节点**：两者调的都是 `incremental_patch()`，只是 focus 填法不同。

两个细节：
- `descriptions` 必填。`capability_id` 是描述文本的**哈希**，Prompt 里出现两串哈希
  模型没法据此构造出真正同时用到两项能力的场景。只带本轮用到的那几项，不把整棵树塞进去。
- 单轮上限 `max_combinatorial_patch_per_round=5`。首次接入时几乎所有组合对都未覆盖，
  不设上限就会一次性生成上百条题，测试集和账单一起撑爆。
- 补题失败**不抛异常**，只记 `KEY_PATCH_FAILURE` 并写进报告。为一次补题失败掀掉
  整条流水线不成比例；但"缺口还在、系统却安静地不补了"必须让人看见。
- 成功时写回公共字段 `active_suite_version_id` —— 排在本维度之后的维度会看到补过
  组合缺口的用例集，这是数据飞轮的**意图**而非副作用。

### 4. `orphan_case_detection` — 能力漂移与弃用侦测

**它干什么**：找出"绑定的能力**全部**已从能力树消失"的用例，往
`test_case_suggestions` 表写一条 `pending` 建议。

场景：开发者在 `SKILL.md` 里删掉了"支持 Excel"只留"支持 CSV"，
那些测 Excel 的题从此注定失败——提前拦下来，避免 CI 出现"预料之中的红灯假警报"。

**判定是"全部消失"而不是"任意一项消失"**。`capability_id` 是描述文本的哈希，
改一个字就是一个新 id；按"任意一项"判，会把"三项能力里改写了一项描述"的用例
也判成孤儿，而它仍然测得到另外两项，淘汰它是纯粹的损失。

**这不是一次挂起**（不调 `suspend_and_wait()`）。孤儿用例不影响本次运行任何结论
的正确性：模块一/三/五等消费方按 `split` 取题后各自独立判定，没有谁会去检查
"这条题绑的能力还在不在"。本项目由此明确区分两种人机协作模式：

| 模式 | 用于 | 例子 |
|---|---|---|
| **阻塞式挂起** | 不确认就无法继续算下去 | 模块六的能力树粒度确认 |
| **非阻塞建议队列** | 可以先继续跑，但需要人找时间清理 | 本节点 |

`save_if_absent()` + `(case_id, suggestion_type)` 的**库层唯一约束**去重：
同一条孤儿用例连续三次评测都会被检出，但人只需要处理一次。
放在节点里"先查后写"在多 run 并发评测同一个 Skill 时必然漏掉。
副作用是已被人 `rejected` 的不会被推回待办——这是期望行为，人已经判断过
"这条孤儿用例要留着"。

`reason` 里除了消失的 id 还带 prompt 前 200 字：人要判断的是"该淘汰还是该重新
绑定到改名后的能力上"，只给一串哈希等于把判断推回给人自己去查库。

### 5. `finalize_pruning_report` — 报告聚合

写 `dimension_results`，`dimension="test_suite_health"`。

| 情形 | status | blocking |
|---|---|---|
| 组合覆盖率取不到（私有键被裁/节点被跳过） | `NEEDS_HUMAN_REVIEW` | False |
| 其余（含有孤儿、有组合缺口） | `PASS` | False |

**本维度永远不产出 FAIL**。它衡量的是测试集自身的健康度，不是 Skill 的质量。
判 FAIL 会让一个功能完全正确的 Skill 因为"测试集还不够全"被拦下，
最终结果是所有人都学会绕过这条门禁。

`NEEDS_HUMAN_REVIEW` 那一档**不是门禁，是故障信号**。这是本维度最值得记住的一条
防御性设计，见第五节。

`score` 填组合覆盖率——本维度唯一有连续取值的量。折叠条数与孤儿条数是计数不是比率，
塞进 `score` 会让 `BenchmarkReport` 里的分数失去可比性。

### 路由函数 `route_after_matrix`

```python
if not uncovered_pairs:                          → orphan_case_detection
if max_combinatorial_patch_per_round <= 0:       → orphan_case_detection
else:                                            → combinatorial_feedback_generation
```

第二条分支专门尊重"旋钮调成 0"的运维语义 = **只分析、不自动补题**
（例如测试集正在人工整理期间）。否则会走进一个立刻空转返回的节点。

---

## 二、在整个评测流程里的位置和作用

### 位置：第 2 层，紧跟模块六，必须晚于它

```
模块六（coverage）         建能力树 + 把用例映射到能力 + 补盲回环（唯一带环的子图）
        ↓ 必须
模块七（pruning）          ← 本子图：用模块六的产出来体检题库
        ↓ 必须
模块八（weighted_coverage） 能力权重分级 + 加权覆盖率
```

**顺序约束是硬的，而且违反它的后果特别阴险**。本维度三个分析节点全部读模块六的
产出（`CapabilityTree` + `TestCase.target_capability_ids`）。能力树没建好时，
"没有冗余、没有组合缺口、没有孤儿"这三个结论全是假的——**而且是看起来很健康的
假结论**。

代码用 `_load_tree()` 取不到 `capability_tree_id` 就抛 `PersistenceError`，
错误信息里直接点名这条顺序约束。接错了立刻失败，而不是给出一份 0 冗余 0 缺口的报告。

主图（docs/dev/24）的装配写法：

```python
cov   = add_coverage_nodes(builder, deps)                       # 16
prune = add_pruning_nodes(builder, PruningDeps.from_coverage(cov.deps))
builder.add_edge(coverage.TERMINAL_NODE, pruning.ENTRY_NODE)    # 16 → 17
builder.add_edge(pruning.TERMINAL_NODE, "<下一个维度>")
```

> 当前状态：`src/skill_evaluate/graph/` 下只有 `__init__.py`，主图还没落地（dev/24 待实现）。
> 现在跑本子图走 `build_pruning_subgraph()`，用于本地调试与集成测试。

### 作用：它是"数据飞轮"上的刹车片

项目里有两个节点会反向驱动 Generator 补题：

| | 触发原因 | `triggered_by` |
|---|---|---|
| 模块六 `feedback_driven_generation` | 单项能力零覆盖 | `coverage_gap` |
| 模块七 `combinatorial_feedback_generation` | 两项能力没被同一条题同时用到 | `combinatorial_gap` |

两个审计值**刻意分开**：排查"测试集为什么突然多了 5 条题"时，这是两个完全不同的原因。

但只有加法的飞轮会慢性臃肿——Generator 补得越多，同质化用例越多，
CI 的 token 和沙箱成本线性上涨，而信息量不涨。
模块七是这个飞轮上**唯一往回收的力**：一边补交叉盲区（加），一边折叠冗余（减），
目标是让留在流水线里的每条用例都**不可替代**。

架构文档的说法是维持测试集的"高信息熵"。

---

## 三、设计目的

### 目的一：让题库保持高信息密度，而不是单纯追求覆盖率数字

单纯追高覆盖率 + 过度依赖 Agent 生成，必然产生大量高度同质化的用例——
五道题都只测"读取 CSV + 过滤空行"，只是换了文件名和自然语言表述。
它们让覆盖率看起来很好，却一分钱的额外信息都不提供，每次 CI 都要真金白银地跑一遍。

### 目的二：把"单项覆盖 100%"这个假象捅破

组合矩阵存在的全部理由就是这一句：**单个能力的覆盖率都是 100%，也不代表交叉点被测过。**
复杂 Skill 的缺陷恰恰最爱藏在交叉点上（上下文组合连贯性）。

### 目的三：防止 CI 出现"预料之中的红灯"

能力被弃用后，对应的题注定失败。提前捞出来报人工，比让它在某次 CI 里红一次、
有人花半小时查出"哦这个能力上个月删了"要便宜得多。

### 目的四（最重要的一条）：把风险最高的动作锁死在人工手里

架构文档自己写明了这个模块的缺点：

> "删减测试用例"是一个极具风险的决策。LLM 可能会因为理解偏差，错误地将一个其实是在
> 测试罕见边界条件（Corner Case）的用例当作冗余用例给标记掉。

应对方案落到代码上是两条，都很硬：

**1. 降级不删除 —— 本文件没有任何一条删除用例的代码路径。**
最重的动作是把 `split` 改成 `COLD`（用例仍在 `case_ids` 里，消费方按 `split`
过滤时天然跳过它），以及往建议队列写一条 `pending`。
`COLD` 这个枚举值早在 dev/02 就定义好了，模块七只是它的**第一个真实生产者**——
也就是说本模块**不需要修改任何已确认文档的查询逻辑**，"惰性过滤"机制本来就在那。
真正的删除动作属于 dev/22 的审查工作台。

**2. 全程零 LLM。**
三件事都是精确的集合运算：
- 冗余 = 能力集合完全相等
- 孤儿 = 绑定 id 不在树上（差集）
- 组合缺口 = 两两组合的差集

dev/17 第 4.1 节写明了理由：这类低风险决策交给 LLM，换来的是一个**每次跑都可能
不一样的瘦身结果**，而瘦身恰恰是本项目里最需要可复现的动作——它会改变以后每一次
评测跑哪些题。

> 这里和架构文档有一处**有意的偏离**：架构文档写"Judge Agent 会对现存用例进行聚类
> 分析"、"Analyzer Agent 构建 N×N 矩阵"，实现里这两个 Agent 一次都没调。
> `deps.py` 虽然从 `CoverageDeps` 继承了 `analyzer()`，但注释明确写了不用它。
> `JudgeAgent` 同样不用——本维度不产出通过/失败结论，dev/interfaces/08 第 0 节
> "凡是通过/失败的结论一律经过 JudgeAgent"这条铁律在这里没有适用对象。
> 硬造一条"测试集健康度阈值"规则只会凭空多出一道谁也说不清该定在多少的门禁。

---

## 四、两个"与模块六刻意不同"的地方

### 1. 本子图无环

模块六是全项目**唯一**带回边的子图：补盲 → 重新映射 → 直到覆盖率达标。

模块七刻意不学。组合缺口首次接入时几乎是全量未覆盖，回环会把
`max_combinatorial_patch_per_round` 想要的"逐步收敛"变成"一次跑到底"。
所以 `combinatorial_feedback_generation` 的唯一去处是 `orphan_case_detection`，
两条分支在那里汇合，图上不存在任何一条指回上游的边。
剩余缺口交给**后续评测轮次**收敛。

### 2. 本子图没有挂起点

`INTERRUPT_BEFORE_NODES: list[str] = []` —— 显式导出一个空列表，而不是干脆不定义。
理由很实际：dev/24 汇总 `interrupt_before` 时是逐个维度取这个常量的，
缺一个会让"这份文档忘了写"和"这份文档确实没有挂起点"分不开。

孤儿用例走的是非阻塞建议队列，不是 `suspend_and_wait()`。

### 顺带：三个命名常量的共享关系

| 常量 | 取值 | 与模块六 |
|---|---|---|
| `ROUTING_KEY` | `coverage_analysis` | **沿用**（import，不重新定义） |
| `NODE_PREFIX` | `coverage` | **沿用**（同一图分区） |
| `DIMENSION` | `test_suite_health` | **必须独有** |

`DIMENSION` 必须不同的原因很硬：`dimension_results` 表的唯一约束是
`(run_id, dimension)`，两个模块写同一个值，**后跑完的会把先跑完的整行覆盖掉**，
报告里只剩一个维度，且不会有任何报错。

取值定为 `test_suite_health` 而不是接口文档当初建议的 `test_suite_pruning`：
本维度报告的不只是"瘦身了多少"，还有组合缺口和孤儿用例——它衡量的是**测试集
自身的健康度**，pruning 只是三件事之一。

依赖容器那边 `PruningDeps` 继承 `CoverageDeps` 并提供 `from_coverage()`：
各自 `CoverageDeps()` 会让两个 Agent 实例持有不同的 `trace_handle`，
Langfuse 上就会出现两条彼此无关的调用线，而它们本该是同一次评测里的同一个分析基座。
`from_coverage()` 逐字段搬运而不是 `copy.replace()`，因为几个 Agent 字段是**惰性
构造**的——逐字段搬运会把"已经构造好的那个实例"和"还是 None"两种状态都如实带过来。

---

## 五、最值得单独记一笔的防御：私有键被裁掉的症状

LangGraph 按节点函数第一个参数的类型注解推导输入 schema。主图状态 schema 若漏并
本维度私有键，它们会在进入节点前被**静默裁掉**。

本维度被裁掉的症状是全项目最危险的一个：

> 三个分析节点照常跑、**照常改库**（用例真的被降级、建议真的落表），
> 只是 finalize 拿不到任何计数，报告里显示
> "折叠 0 条 / 未覆盖 0 对 / 孤儿 0 条"
> —— 一份看起来"测试集非常健康"的报告，而实际上刚刚有一批用例被移进了冷数据区。

所以 `finalize_pruning_report` 在 `_pruning_pair_coverage_ratio` 取不到时判
`NEEDS_HUMAN_REVIEW` 并点名该键，而不是判 PASS。判 PASS 等于把整个维度悄悄关掉。

同一条思路还出现在 `_positive_cases()`：`active_suite_version_id` 缺失时抛
`PersistenceError` 而不是返回空列表 —— "这个测试集没有冗余"和"我根本没拿到测试集"
是两件完全不同的事，后者被当成前者，报告里会写"折叠 0 条"，看起来像测试集很健康。

**这个模块里反复出现的同一个判断：凡是"异常情况"会伪装成"一切健康"的地方，
都必须显式抛错或降级成故障信号，而不是让默认值悄悄通过。**

---

## 六、留给后续模块的插槽

| 插槽 | 当前状态 | 谁来接 |
|---|---|---|
| 组合矩阵按 P0 优先级截断 | 排序键已就位，委托给 `weighted_coverage/priority.py`；但分级节点在本维度之后，同轮仍是占位 tier | dev/18（已部分落地） |
| `test_case_suggestions` 审查界面 | 表已建（Alembic `0008`），无消费界面 | dev/22 工作台 |
| `COLD` 用例的 Nightly Build 调度 | 打标签完成，无调度 | dev/24 CI 配置 |
| 主图装配（16→17→18 连线） | `src/skill_evaluate/graph/` 仍为空 | dev/24 |

---

## 附：两个配置旋钮

```python
# config.py::CoverageSettings
max_capability_pairs_for_matrix: int = 100   # 约 14 个能力节点的全组合；超限截断不采样
max_combinatorial_patch_per_round: int = 5   # 调成 0 = 只分析不补题，路由会尊重
```
