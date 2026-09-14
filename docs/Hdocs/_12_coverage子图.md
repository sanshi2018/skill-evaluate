# coverage 子图：能力覆盖率与测试完备性评测（模块六）

> 代码：`src/skill_evaluate/nodes/coverage/`、`src/skill_evaluate/agents/analyzer/`
> 开发文档：`docs/dev/16_模块六_能力覆盖率与测试完备性评测.md`
> 接入文档：`docs/dev/interfaces/16_capability_coverage.md`
> 测试：`tests/skill_evaluate/test_coverage.py`

---

## 一、一句话说清楚

其他维度回答的是"**Skill 表现得好不好**"，coverage 子图回答的是一个更靠前的问题：

> **我们拿来评测它的那套测试题，到底测到了 SKILL.md 承诺的多少件事？没测到的，能不能自动补上？**

它不跑沙箱、不执行被测代码，只做三件事：把 SKILL.md 拆成一张"能力清单"，把现有正向用例逐条对到清单上，
找出没人测过的能力并让 Generator 定向补题——然后重新对一遍，直到达标或次数用完。

---

## 二、为什么需要它（设计目的）

### 1. 高通过率可能是假的

设想一个"数据处理 Skill"，SKILL.md 写着支持 CSV、Excel、缺失值处理、透视表输出四件事。
Generator 首次出了 9 条正向用例，碰巧全在问 CSV。模块一跑下来触发率 100%，模块三执行效果全部通过——
报告一片绿。

但 Excel、缺失值、透视表**一次都没被测过**。它们坏了，这份报告也照样是绿的。

这就是架构文档说的"高通过率的虚假繁荣"。其他维度只能对**题目里出现过的东西**下结论，
没有任何一个维度会主动发现"题目本身漏了什么"。coverage 子图就是专门补这个洞的。

### 2. 发现问题不够，还要把问题修掉

只报一句"覆盖率 25%"对开发者帮助有限——他还得自己去想缺哪些题、自己去写。
coverage 子图把盲区打包成**硬性约束**直接喂回 Generator，让系统自己把缺的题补上。
这是整个项目"数据飞轮"的第一个真正转起来的轮子：**评测结果反过来改进评测输入**。

### 3. 给模块七、八打地基

它产出的 `CapabilityTree`（能力树）和 `TestCase.target_capability_ids`（用例→能力绑定）
是后面两个维度的全部输入：

- 模块七（`pruning`）靠绑定关系判断哪些用例高度重叠、哪些用例绑的能力已经从文档里消失；
- 模块八（`weighted_coverage`）在同一棵树上给能力分 P0/P1/P2 档、抽负向约束、算加权覆盖率。

没有模块六，这两个维度无从谈起。

### 4. 同时不能让它失控

"自动补题直到达标"听起来很好，但架构文档点名了它的风险：

> 如果 Analyzer 把能力拆解得过于细碎，覆盖率可能永远无法达标，从而引发流水线的**无限重试死锁**。

所以这张图的设计里，一半的心思花在"怎么让它**停下来**"上——见第五节。

---

## 三、在整个评测流程中的位置

```
pipeline.bootstrap_run
        ↓
preflight.*（沙箱指纹、金丝雀探针）
        ↓
   ┌────┴────────────────────────────────────────────┐
   │ context_scoping.*   script_usability.*          │   与用例集无关的维度，并行
   │                                                 │
   │ trigger_accuracy.prepare_test_suite  ← 首次出题（REUSE 语义）
   │        ↓ （串行：用例集准备节点必须一个一个来）
   │ security.prepare_adversarial_suite
   │        ↓
   │ instruction_control.prepare_cases
   │        ↓
   │ ┌──────────── coverage 分区（一棵能力树，三个维度）─────────────┐
   │ │  coverage.*          模块六 ◀── 本文档（带环）                │
   │ │        ↓                                                     │
   │ │  pruning.*           模块七  冗余折叠 / 组合矩阵 / 孤儿检测     │
   │ │        ↓                                                     │
   │ │  weighted_coverage.* 模块八  分级 / 负向约束 / 加权覆盖率       │
   │ └──────────────────────────────────────────────────────────────┘
   │        ↓
   │ multi_skill.*（模块十）
   └────┬────────────────────────────────────────────┘
        ↓  （所有维度终节点汇合，多起点边 = 同步屏障）
finalize.report → finalize.patch_pr → finalize.rag_archive
```

（拓扑以 `src/skill_evaluate/graph/main.py` 为准。）

### 三条位置约束

**1. 必须排在"用例集准备"之后。**
`map_case_coverage` 要读 `state["active_suite_version_id"]`，没有就直接抛 `PersistenceError`。
不静默跳过，是因为"没有用例集"和"覆盖率 0%"是两件完全不同的事。

**2. 用例集准备节点必须串行，coverage 排在最后一个。**
模块一、五、三、六都会调 `TestSuiteService` 改动 active 用例集版本。并行跑时两边会各自激活一个
继承旧版本的新版本，后激活的把先激活那批用例丢掉（数据库层的 lost update，不报错）。
主图因此把它们串成 `trigger_accuracy → security → instruction_control → coverage`。

**3. 必须排在模块七、八之前，且三者顺序固定。**
能力树要先建好、覆盖标记要先算完（包括补盲回环收敛），冗余折叠与加权分级才有输入。

### 一个"会改变别人输入"的维度

coverage 是全项目第一个**既读又写测试集**的维度。补盲成功后它会把
`active_suite_version_id` 更新成新版本写回主图状态——所以：

- **排在它之后的维度**（模块七、八、十）看到的是补过盲的用例集；
- **排在它之前的维度**（模块一、三、五）看到的是补盲前的。

这是有意的：飞轮的意义就是让后面的环节受益。但如果将来某个维度要求"所有维度必须跑同一批题"，
需要把它排到 coverage 之前。

### 报告里的三行

模块六/七/八共用一个节点前缀 `coverage.*` 和一条后端路由 `coverage_analysis`，
但在报告里**各占一行**：

| `DimensionResult.dimension` | 模块 | score 含义 |
|---|---|---|
| `capability_coverage` | 六 | 等权能力覆盖率 |
| `test_suite_health` | 七 | 组合覆盖率 |
| `weighted_coverage` | 八 | 按重要性加权的能力覆盖率 |

维度名必须不同：`dimension_results` 表的唯一约束是 `(run_id, dimension)`，共用一个名字时
后跑完的维度会把前一个整行覆盖掉，报告里只剩一行，而且不会报错。

---

## 四、Graph 结构

```
                  ┌────────────────────────────────┐
   ENTRY ───────▶ │ 1. extract_capability_tree     │ ── 能力数 > 20 ──▶ 人工审核卡片（挂起）
                  └───────────────┬────────────────┘                      │ confirm
                                  ▼  ◀───────────────────────────────────┘
                  ┌────────────────────────────────┐
             ┌──▶ │ 2. map_case_coverage           │
             │    └───────────────┬────────────────┘
             │                    ▼
             │    ┌────────────────────────────────┐
             │    │ 3. blind_spot_detection        │
             │    └───────────────┬────────────────┘
             │                    │ route_after_blind_spots
             │     有盲区 且 未耗尽 且 迭代 < 3？
             │         │是                          │否
             │         ▼                            │
             │    ┌────────────────────────────────┐│
             │    │ 4. feedback_driven_generation  ││
             │    └───────────────┬────────────────┘│
             │                    │ route_after_feedback
             │   补盲成功         │   补盲耗尽/失败    │
             └────────────────────┤                   │
                                  └─────────┬─────────┘
                                            ▼
                  ┌────────────────────────────────┐
   TERMINAL ◀──── │ 5. finalize_dimension_report   │
                  └────────────────────────────────┘
```

- 节点名：`coverage.extract_capability_tree` 等，前缀 `coverage.` 与模块七/八共用；
- **全项目唯一一张带环的子图**（4 → 2 是回边）；
- 一个挂起点（节点 1，动态 `interrupt()`）；
- 两个条件路由，**两道环出口**。

| 节点 | 做什么 | 调 LLM？ | 写库？ |
|---|---|---|---|
| `extract_capability_tree` | SKILL.md → 能力树 | ✅ 1 次 | 能力树 |
| `map_case_coverage` | 正向用例 → 能力 id | ✅ 每条**未映射**用例 1 次 | 用例绑定、能力树覆盖标记 |
| `blind_spot_detection` | 算盲区、覆盖率、出判定 | ❌ 纯算术 | 一条 `JudgeVerdict` |
| `feedback_driven_generation` | 盲区 → Generator 定向补题 | ✅（Generator 侧） | 新测试集版本 |
| `finalize_dimension_report` | 汇总写报告 | ❌ | `dimension_results` 一行 |

---

## 五、主要业务流程

下面用一个具体例子把整个流程走一遍。被测 Skill 是 `csv-cleaner`，SKILL.md 大意：

> 本工具接受 .csv 与 .xlsx 两种输入，自动填补缺失值，并可按指定字段输出数据透视表。

测试集里有 9 条正向用例，其中 8 条问的是 CSV，1 条问的是透视表。

### 步骤 1：`extract_capability_tree` —— 这个 Skill 承诺了什么？

Analyzer Agent 通读 description 和正文，产出原子能力清单：

| capability_id | description | evidence_quote（原文引用） |
|---|---|---|
| `csv-cleaner:cap-0abcfda5d9d7` | 支持读取 CSV 文件 | 接受 .csv 与 .xlsx 两种输入 |
| `csv-cleaner:cap-7e21…` | 支持读取 Excel 文件 | 接受 .csv 与 .xlsx 两种输入 |
| `csv-cleaner:cap-91c4…` | 支持处理缺失值 | 自动填补缺失值 |
| `csv-cleaner:cap-3b0f…` | 支持输出数据透视表 | 按指定字段输出数据透视表 |

几条关键规则：

- **"原子能力"= 用户能单独提出、能单独验证成败的一件事**。"先读文件→再校验表头→最后写出"
  是**一项**能力的内部流程，不是三项。Prompt 里专门强调了这条，因为拆步骤是造成
  "覆盖率永远补不满"的头号原因。
- **必须能在原文里找到出处**（`evidence_quote` 逐字引用）。凭"它大概也能做 X"补出来的能力
  会变成盲区，逼系统为一件 Skill 从没承诺过的事出题。
- **id 不是模型编的**，而是代码算的：`<skill_id>:cap-<sha256(归一化描述)前12位>`。
  理由见第六节第 1 条。
- `tier` 暂时统一填占位值 `P1_CONDITIONAL`，`negative_constraints` 为空——那是模块八的活。

**人工审核卡片**：如果拆出来的能力数超过 `capability_count_review_threshold`（默认 20），
节点经 `ApprovalService` 发一张 `CONFIRM_TREE_REVIEW` 卡片（附前 10 项描述），然后挂起：

- 人回复 `confirm` → 继续往下算；
- 回复其他任何东西（包括 payload 形状对不上）→ 抛 `HumanRejectedSuspension`，本维度就此停下。

"默认不通过"是刻意的：一棵没被明确确认过的树继续算下去，得到的是一份建立在错误粒度上、
看起来却一切正常的覆盖率报告。而本维度不阻断合并，停下来不会卡住任何人的 CI。

⚠️ 恢复时节点整体重跑（LangGraph 动态 interrupt 的语义），会多一次抽取调用。
能力树不会因此错乱：id 是确定性哈希，落库是 upsert。

### 步骤 2：`map_case_coverage` —— 每条题测到了哪些能力？

只取**正向用例**（反向用例的含义是"这类请求不该由本 Skill 处理"，拿它证明能力被覆盖是自相矛盾的）。

对每条用例，Analyzer 判断"一个合格的智能体处理这条提问时，**必须**动用哪几项能力"：

| 用例 prompt | 映射结果 |
|---|---|
| "这堆导出的 csv 你帮我理一下" | 读取 CSV |
| "把表按部门做个透视，源文件是 csv" | 读取 CSV、输出透视表 |
| ……（其余 7 条都是 CSV） | 读取 CSV |

结果回填到 `TestCase.target_capability_ids` 并**逐条**落库；同时在能力树上标记
`covered=True` 和 `covering_case_ids`。

这一步有三个要点，都是实现时专门处理过的坑：

1. **拿不准的不要填**。多填一项的代价是一个真实盲区被误判为已覆盖、从此没有用例去补它；
   少填一项的代价只是多出一条补盲用例。两边不对称，所以宁可少填。模型编造的 id 会被按能力树过滤掉。
2. **每轮先清空覆盖标记再重算**。这个节点在回环里会跑第二、第三次，不清空的话同一条用例会被
   重复塞进 `covering_case_ids`，模块七拿它做聚类时会被扭曲。
3. **"要不要调 LLM"和"要不要标记覆盖"分开判断**。已经有绑定的用例跳过 LLM 调用，但**照常参与覆盖标记**。
   补盲生成的新题在出题时就带着 `target_capability_ids`——如果把它们整条跳过，
   补盲回环跑多少轮覆盖率都不会涨。

另外，如果一条旧用例绑的 id 在新能力树里找不到了（SKILL.md 改写过那项能力），这里只跳过并打日志
`coverage_orphan_case_binding`，不做淘汰——淘汰是模块七"孤儿用例检测"的职责。

### 步骤 3：`blind_spot_detection` —— 算盲区、出判定

```
盲区 = [读取 Excel, 处理缺失值]
覆盖率 = tree.weighted_coverage()  → 2/4 = 50%    （此时 tier 全是占位值，加权 = 等权）
```

判定**不在节点里自己写 if**，而是走 `JudgeAgent.quantitative_verdict()`：

```python
judge.quantitative_verdict(
    subject_id="coverage:csv-cleaner",
    rule_name="capability_coverage_threshold",   # ratio >= threshold 即 PASS
    inputs={"coverage_ratio": 0.5, "threshold": 0.9, "tier_weighted": False},
)
# → FAIL，model="rule:capability_coverage_threshold"
```

项目铁律是"所有通过/失败结论都经过 JudgeAgent"，这样判定格式统一、可归档，报告读者看到
`rule:` 前缀就知道这个结论是算出来的而不是模型判出来的。`tier_weighted=False` 是口径标记：
告诉读记录的人这个百分比还没体现能力重要性差异（加权口径由模块八再算一份）。

盲区以"id + 描述"的形式写进状态 `_coverage_blind_spots`——只存 id 的话，下一步喂给 Generator 的
是一串哈希，报告里写的也是一串哈希。

### 路由一：`route_after_blind_spots`

```
没有盲区                     → finalize
上一轮补盲已判定耗尽/失败      → finalize
已补盲次数 >= max_patch_iterations（默认 3）→ finalize
其余                         → feedback_driven_generation
```

例子里有 2 个盲区、已补 0 次 → 去补盲。

### 步骤 4：`feedback_driven_generation` —— 让 Generator 定向补题

```python
focus = CapabilityFocus(
    capability_ids=["csv-cleaner:cap-7e21…", "csv-cleaner:cap-91c4…"],
    descriptions={
        "csv-cleaner:cap-7e21…": "支持读取 Excel 文件",
        "csv-cleaner:cap-91c4…": "支持处理缺失值",
    },
)
new_suite = await TestSuiteService().incremental_patch(skill, focus, triggered_by="coverage_gap")
```

Generator 收到的 Prompt 里会有一段"定向补盲区约束（硬性要求）"，逐项列出能力描述，
要求每项至少一条用例并回填对应 id。生成结果：

- 按盲区数量出题（这里 2 条），**不是**再出一整套 8~10 条；
- 新用例独立做 60/40 训练/验证划分，**老用例的 split 归属不变**（它们可能已经跑过优化闭环）；
- 新老用例合并成一个新的 active 版本，旧版本保留为非 active；
- `triggered_by="coverage_gap"` 进审计字段，事后能查"测试集为什么变了"。

节点返回新的 `active_suite_version_id`，迭代计数 +1。

**这是整个项目里唯一允许流水线自动触发出题的地方**。默认复用、强制重生只有人能触发——
这条约束保护的是"分数变化能被归因"。补盲之所以例外，是因为它有明确理由且只补盲区，
不会替换已有题目。

**补盲失败不掀桌子**：如果 `incremental_patch()` 抛 `GenerationError`（比如这个 Skill 根本还没有
active 用例集），节点标记 `_coverage_patch_exhausted` 并把原因写进 `_coverage_patch_failure`，
迭代计数**不加**，然后正常收尾。覆盖率本来就不阻断合并，为一次补题失败让整条流水线挂掉不成比例。

### 路由二：`route_after_feedback`

```
补盲耗尽/失败 → finalize          ← 必须有这道出口
否则          → map_case_coverage（回边）
```

### 第二轮：回到步骤 2、3

- `map_case_coverage` 读到新版本用例集：9 条老题已有绑定，跳过 LLM；2 条新题出题时就带了绑定，
  也跳过 LLM。**这一轮零次映射调用**，直接重算覆盖。
- `blind_spot_detection`：盲区清空，覆盖率 100%，判定 PASS。
- `route_after_blind_spots`：没有盲区 → finalize。

如果某项能力补了 3 轮还是覆盖不上（比如它本身就被拆得太虚，模型怎么出题都映射不到），
到达上限后也会进 finalize，报告里如实写"已达最大补盲迭代次数仍存在零覆盖能力"。

### 步骤 5：`finalize_dimension_report` —— 写报告

| 情形 | status | blocking |
|---|---|---|
| 覆盖率键取不到（主图状态 schema 漏并了私有键） | NEEDS_HUMAN_REVIEW | False |
| 能力树为空 | NEEDS_HUMAN_REVIEW | False |
| 覆盖率 < 阈值 | FAIL | False |
| 其余 | PASS | False |

findings 示例（覆盖率不达标时）：

```
能力覆盖率 50.0%（阈值 90%）：2/4 项声明能力被正向用例覆盖，参与映射的正向用例 9 条。
（等权口径：每项能力权重相同；按重要性加权的口径见 weighted_coverage 维度。）
零覆盖能力 csv-cleaner:cap-7e21…：支持读取 Excel 文件
零覆盖能力 csv-cleaner:cap-91c4…：支持处理缺失值
已达最大补盲迭代次数（3 次）仍存在零覆盖能力：请人工判断是能力树切分过细，还是测试集确实不足。
```

两个值得注意的口径：

- **`blocking` 永远是 False**。覆盖率反映"测试测得全不全"，不是"Skill 本身有没有质量问题"。
  一个功能完全正常的 Skill 不该因为测试基础设施还不完善而被卡住合并——否则大家很快就会学会绕过这条门禁。
  这个默认值可以在 CI 门禁层按项目成熟度收紧。
- **空能力树判 NEEDS_HUMAN_REVIEW 而不是满分**。空树时覆盖率公式给出 1.0，但那意味着"一项能力都没抽出来"，
  不是"测得很全"。状态优先级因此是 NEEDS_HUMAN_REVIEW > FAIL > PASS：在一个算不出来的数字上判 FAIL，
  等于用一个假结论替掉"我不知道"。

---

## 六、几个关键设计决策

### 1. `capability_id` 为什么必须是描述文本的哈希

用例与能力的绑定（`TestCase.target_capability_ids`）会长期存在库里，而能力树每次运行都会重新抽取。

如果 id 是抽取顺序（`cap-1`、`cap-2`……），模型这次把"读取 Excel"排第 3、上次排第 5，
所有历史绑定就整体错位。报告不会报错，只会显示"覆盖率从 92% 突然掉到 40%"，没人解释得清。

所以 id 必须是**能力语义本身**的函数：

- 书写差异（空白、大小写、全角半角、中英文标点）归一后再哈希——两次抽取只差一个逗号，id 不变；
- **不做**同义词归并——描述被实质改写就是新 id。语义等价交给启发式会同时出现两类相反的错误，
  而且都无法从报告里看出来；
- 带 `skill_id` 前缀——两个 Skill 碰巧写了同一句能力描述时不会共用 id。

同一个函数同时去重：模型写了两条只差标点的能力，会撞到同一个 id，保留第一条。不去重的话，
覆盖率分母被虚增，而且其中一个节点永远标不上 covered。

### 2. 为什么映射不走 Judge，而阈值判定走 Judge

- "这条用例激活了哪些能力"是**结构化抽取**，产出一组 id，没有 PASS/FAIL 语义，
  也没法用黄金基准盲测来考核。所以由 Analyzer 自己的 LLM 调用完成。
- "覆盖率是否达标"是**通过/失败结论**，必须经过 JudgeAgent（量化规则，不花 LLM）。

Analyzer 的温度设为 0：抽取任务要求同样的输入给出同样的结果，这是覆盖率数字能被信任的前提。

### 3. 为什么要"两道环出口"

只有路由一不够。节点 4 自己会在两种情况下把状态标成耗尽：迭代上限的防御性检查，以及补盲失败。
补盲失败时迭代计数**没有**增加——如果此时无条件回边，就会走 回映射 → 盲区依旧 → 路由一放行 → 再补一次失败 → ……
正是架构文档警告的无限重试死锁。路由二把这条路堵死。

迭代上限在路由和节点里各写一次，也是同样的考虑：这是本模块点名的首要风险，值得多一道保险。

### 4. 为什么这个维度必须是 MINI 后端

`CoverageDeps.assert_backend_routing()` 在装配期断言 `coverage_analysis` 路由是 `MINI`，
方向与模块一/三/四/五（必须 PLUGGABLE）相反。

"覆盖率"听起来像是"把用例跑一遍看覆盖到哪"，但模块六测的是**测试集与声明能力的映射关系**，
全程只读文本，一次沙箱都不起。改成 PLUGGABLE 不会带来更强的证据，只会让人误以为这里有真实执行。

---

## 七、状态与数据

### 私有状态键（`nodes/coverage/state.py`）

| 键 | 含义 |
|---|---|
| `_coverage_blind_spots` | 零覆盖能力，`[{capability_id, description}]` |
| `_coverage_ratio` | 本轮覆盖率 |
| `_coverage_patch_iterations` | 已补盲次数 |
| `_coverage_patch_exhausted` | 不再补盲 |
| `_coverage_patch_failure` | 补盲失败原因 |
| `_coverage_tree_node_count` | 能力数（区分"没盲区"与"没能力"） |
| `_coverage_mapped_case_count` | 参与映射的正向用例数 |
| `_coverage_tree_review_confirmed` | 能力树经过人工确认 |

⚠️ 主图状态 schema（`graph/state.py`）必须并入 `CoverageState`。漏掉的症状很隐蔽：
LangGraph 会在进节点前把私有键裁掉，表现为"能力树抽出来了、盲区一个都没有、覆盖率 100%"。
finalize 对这种情况会判 NEEDS_HUMAN_REVIEW 并点名缺失的键。

### 公共字段

| 字段 | 读/写 |
|---|---|
| `active_suite_version_id` | 读（映射用哪版题）；**写**（补盲后更新） |
| `capability_tree_id` | 写，值为 `<skill_id>:<version_ref>`（解析时从右边切，skill_id 允许含冒号） |
| `judge_verdict_ids` | 追加每轮的覆盖率判定 id |

### 落库产物

| 表 | 内容 | 谁还会读 |
|---|---|---|
| `capability_trees` | 能力树 + 覆盖标记 | 模块七、八 |
| `test_cases.target_capability_ids` | 用例→能力绑定（本维度首次真实写入） | 模块七 |
| `test_suite_versions` | 补盲产生的新版本 | 后续所有维度 |
| `judge_verdicts` | 覆盖率判定 | 报告、审计 |
| `dimension_results` | `capability_coverage` 一行 | `finalize.report` |

---

## 八、配置

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `SKILLEVAL_COVERAGE_CAPABILITY_COUNT_REVIEW_THRESHOLD` | 20 | 超过就发人工审核卡片 |
| `SKILLEVAL_COVERAGE_MIN_COVERAGE_RATIO` | 0.9 | 达标线 |
| `SKILLEVAL_COVERAGE_MAX_PATCH_ITERATIONS` | 3 | 补盲回环硬上限 |
| `SKILLEVAL_COVERAGE_MAX_CONCURRENT_MAPPINGS` | 10 | 映射 LLM 请求并发上限 |
| `SKILLEVAL_LLM_ANALYZER_MODEL` | `anthropic/claude-sonnet-5` | Analyzer 用高档模型：能力树拆错一项会传染到模块七、八 |

**刻意没有**"达不到就自动放宽阈值"这类旋钮。覆盖率不达标时，正确的做法是把事实写进报告，
让人判断是能力树切得太细还是测试集真的不够——而不是让机器把及格线降到刚好能过。

带环的图对步数有影响：本子图最坏约 `1 + (3+1)×3 + 1 = 14` 个超步。主图叠加所有维度后，
由 `pipeline.recursion_limit` 配置统一设置上限（`graph/resumer.py::graph_config`）。

---

## 九、一张图记住

```
SKILL.md ──Analyzer──▶ 能力树（稳定 id）
                          │
现有正向用例 ──Analyzer──▶ 用例↔能力绑定
                          │
                     算盲区 + Judge 判定
                          │
           ┌── 有盲区且还有次数 ──▶ Generator 只补盲区 ──┐
           │                                           │
           │◀────────────── 重新映射（回边）─────────────┘
           │
    没盲区 / 次数用完 / 补盲失败
           │
           ▼
   报告：覆盖率 + 零覆盖能力清单（不阻断合并）
           │
           ▼
   交给模块七（瘦身）→ 模块八（加权）
```
