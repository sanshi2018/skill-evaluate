# weighted_coverage 子图：多维加权覆盖率与隐式边界追踪（模块八）

> 代码：`src/skill_evaluate/nodes/weighted_coverage/`
> 开发文档：`docs/dev/18_模块八_多维加权覆盖率算法与隐式边界追踪.md`
> 接入文档：`docs/dev/interfaces/18_weighted_coverage.md`
> 测试：`tests/skill_evaluate/test_weighted_coverage.py`

---

## 一、一句话说清楚

模块六告诉你"声明的能力**有没有**被测到"，模块七告诉你"测试集本身**健不健康**"，
而 weighted_coverage 回答的是最后一个问题：

> **测到的东西够不够重要？文档里写明"不许做"的那些坑，有没有一条用例专门去诱导智能体踩？**

它是覆盖率三部曲（16 → 17 → 18）的收官，不跑任何沙箱，只在同一棵 `CapabilityTree`
上做"升级"：给能力打权重、抽出负向约束并追踪覆盖、重排组合缺口，最后产出一份
可被任何工具消费的可追溯性矩阵。

---

## 二、为什么需要它（设计目的）

### 1. 等权覆盖率会撒谎

模块六的覆盖率是"覆盖了几项 / 一共几项"，每项能力一样重。设想一个 Skill：

| 能力 | 重要性 | 是否被测 |
|---|---|---|
| 读取 CSV（没它 Skill 就废了） | 核心 | ❌ |
| 输出带 BOM 的文件 | 格式修饰 | ✅ |

等权口径：50%。可真正致命的那项根本没测。按 `P0=0.6 / P1=0.3 / P2=0.1` 加权后，
覆盖率只有约 14%——**这才是真实的风险画像**。

### 2. "普通用例"证明不了"禁令被遵守"

SKILL.md 里常有 Gotchas："查询 users 表必须过滤软删除记录"。一条"帮我查一下
users 表有多少行"的用例用到了相关功能，但场景里**根本没有软删除数据**，智能体守没
守规矩完全看不出来。要测禁令，必须构造**反事实场景**——故意把陷阱摆出来。模块六/七
都不处理这件事，这是"隐式边界"。

### 3. 组合矩阵需要真实优先级

模块七分析能力两两组合时，如果组合太多就要截断。截断应该优先保留 P0×P0 组合，
但模块七跑的时候权重还没分级，只能按 id 排。模块八分级之后要把这件事补上。

### 4. 结论需要一份可交付的"账本"

覆盖率数字只在报告里是不够的，团队需要一份**逐项可追溯**的矩阵：哪项能力、
几档权重、被哪几条用例覆盖；哪条禁令、被谁诱导过——供 diff、审阅、可视化。

---

## 三、在整个评测流程中的位置

```
主图（docs/dev/24 装配）

  bootstrap.ensure_test_suite        ← 准备测试集
          │
          ▼
 ┌──────────────── coverage 分区（三份文档共用一棵能力树）───────────────┐
 │                                                                       │
 │  模块六  capability_coverage   抽能力树 → 用例映射 → 补盲回环          │
 │          （tier 全是占位值 P1，negative_constraints 为空）              │
 │               │                                                       │
 │               ▼                                                       │
 │  模块七  test_suite_health     冗余折叠 → 组合矩阵 → 孤儿检测          │
 │          （写入 combinatorial_pairs_covered）                          │
 │               │                                                       │
 │               ▼                                                       │
 │  模块八  weighted_coverage     ◀── 本文档                              │
 │          分级 → 约束追踪 → 加权判定 → 组合重排 → 可追溯性制品           │
 │                                                                       │
 └───────────────────────────────────────────────────────────────────────┘
          │
          ▼
     其余维度 … → finalize.report（benchmark.json / HTML）
```

**顺序是硬约束**：

- 必须在模块六之后——没有能力树，分级无从谈起（拿不到 `capability_tree_id` 直接抛
  `PersistenceError`）；
- 必须在模块七之后——组合重排读的是模块七写入的 `combinatorial_pairs_covered`。
  顺序反了**不会报错**，只会得出"所有组合都未覆盖"这个错误结论。

三个维度在报告里各占一行，互不覆盖：

| 维度名 | 模块 | score 含义 |
|---|---|---|
| `capability_coverage` | 六 | 等权能力覆盖率 |
| `test_suite_health` | 七 | 组合覆盖率 |
| `weighted_coverage` | 八 | **加权**能力覆盖率 |

---

## 四、Graph 结构

```
                 ┌──────────────────────────────────────────┐
   ENTRY ──────▶ │ 1. extract_tier_and_negative_constraints │
                 └────────────────────┬─────────────────────┘
                                      ▼
                 ┌──────────────────────────────────────────┐
                 │ 2. map_negative_constraint_coverage      │
                 └────────────────────┬─────────────────────┘
                                      │
                   有"明确未覆盖"的约束？
                     │是                         │否
                     ▼                           │
   ┌──────────────────────────────────┐          │
   │ 3. constraint_feedback_generation│          │
   └──────────────────┬───────────────┘          │
                      └───────────┬──────────────┘
                                  ▼        （无回边，不回到 2）
                 ┌──────────────────────────────────────────┐
                 │ 4. recompute_weighted_coverage           │
                 └────────────────────┬─────────────────────┘
                                      ▼
                 ┌──────────────────────────────────────────┐
                 │ 5. upgrade_combinatorial_priority        │
                 └────────────────────┬─────────────────────┘
                                      ▼
                 ┌──────────────────────────────────────────┐
                 │ 6. generate_traceability_artifact        │
                 └────────────────────┬─────────────────────┘
                                      ▼
                 ┌──────────────────────────────────────────┐
   TERMINAL ◀─── │ 7. finalize_weighted_coverage_report     │
                 └──────────────────────────────────────────┘
```

- 节点名前缀统一为 `coverage.`（与模块六/七同一个分区），名字互不重复；
- **无环**，固定 6~7 个超步；
- **没有挂起点**（`INTERRUPT_BEFORE_NODES = []`）；
- 唯一的分支在节点 2 之后。

---

## 五、逐个节点说明

### 节点 1：`extract_tier_and_negative_constraints` —— 补齐两处占位

| | |
|---|---|
| 输入 | 能力树（模块六落库）、SKILL.md |
| LLM 调用 | 2 次：`AnalyzerAgent.classify_tiers()`、`extract_negative_constraints()` |
| 写库 | 整树 upsert：tier 被改写，negative_constraints 被整批替换 |
| 输出状态 | 节点数、tier 分布、是否真的分了级、约束条数 |

**权重分级**：模型按三句判据给每项能力定档，Prompt 里带一张 few-shot 范本表压住漂移：

- **P0 核心**：失效 = Skill 基本不可用
- **P1 条件**：特定条件下才触发的分支
- **P2 防御**：格式、降级、边界修饰

几条落地口径：

- `capability_id` **不变**，只改 tier（改 id 会让历史用例绑定集体失效）；
- 模型回填了清单外的 id → 丢弃；漏判的节点 → 保留占位值，不默认填 P0；
- 分完树上仍只有一个档位 → 报告中标注"加权口径退化为等权"。

**负向约束抽取**：从正文里找"必须避免 / 不能 / 禁止"型规则，**必须逐字引用原文**
（防止把常识脑补成约束）。每条约束的 id 是描述文本的确定性哈希
`<skill_id>:neg-<hash12>`，跨版本稳定。

---

### 节点 2：`map_negative_constraint_coverage` —— 谁在诱导智能体踩坑

这是整个子图最有"业务味"的一步。对每条约束 × 每条候选用例，判断：
**这条用例是不是故意构造了会让人踩这个坑的场景？**

```
候选用例 = active 用例集中的 POSITIVE + ADVERSARIAL，排除 COLD
                     │
      ┌──────────────┴───────────────┐
      ▼                              ▼
用例自带 negative_constraint_ids   其余 (约束, 用例) 组合
   → 直接算覆盖，不花调用            → Judge.judgmental_verdict()
                                      模板 negative_constraint_probe
                                      Criticality.ROUTINE
                                      并发受 max_concurrent_mappings 限制
                                      总量受 max_constraint_probe_calls 限制（默认 200）
```

裁判的判据（两条同时满足才算 pass）：
1. 场景里**存在踩坑的机会**；
2. 违反与否**可观测**。

并且模板**反转了通用规则**：拿不准时判 fail（误判 pass 会让真盲区被永久标记为已覆盖）。

**结果分三类，这是本节点最关键的设计：**

| 类别 | 含义 | 是否触发补题 |
|---|---|---|
| 已覆盖 | 有绑定或裁判判 pass | — |
| **未覆盖** | 判过了，确实没有 | ✅ 触发 |
| **未判定** | 预算用完 / 被黄金基准盲测占用 / 共识未达成 | ❌ 不触发，报告单列 |

把"没算出结论"当成"没覆盖"，会凭空生成一批多余的补题。

另外两点：
- 候选组合按"**用例优先**"排队，预算打满时每条约束拿到的判定机会均等；
- 每轮从零重算覆盖标记，节点幂等。

---

### 节点 3：`constraint_feedback_generation` —— 补反事实用例

仅当存在"明确未覆盖"的约束时进入。

- 构造 `CapabilityFocus(negative_constraint_ids=..., descriptions=...)`，调用
  `TestSuiteService.incremental_patch()`，落一个新的 active 用例集版本；
- **显式 `positive_count=约束数, negative_count=0`**：反事实用例是"该由本 Skill
  处理、但场景埋了坑"的真实请求，属于正向用例。服务的默认映射会把它出成"不该触发"的
  近脱靶题，语义正好相反；
- 正向出题模板里新增了"反事实场景构造"指令：说出陷阱前提、结果可观测、**不许在题面
  里复述规则**；
- 补题失败不抛异常，原因写进报告；
- `triggered_by = "negative_constraint_gap"`，便于审计测试集为什么变了。

**不回环**：新题带着 `negative_constraint_ids` 绑定，**下一轮**评测的节点 2 直接认，
零判定成本——数据飞轮在这里闭合。

---

### 节点 4：`recompute_weighted_coverage` —— 加权判定

```
ratio = CapabilityTree.weighted_coverage()
      = Σ(已覆盖节点权重) / Σ(全部节点权重)

verdict = Judge.quantitative_verdict(
    subject_id = "wcoverage:<skill_id>",
    rule_name  = "capability_coverage_threshold",   # 与模块六是同一条规则
    inputs     = {coverage_ratio, threshold, tier_weighted=True}
)
```

- **只有一条规则、一个算法**。模块六在分级前调它（`tier_weighted=False`，等权），
  模块八在分级后再调（`tier_weighted=True`，加权），两条判定记录靠 `subject_id`
  前缀 `coverage:` / `wcoverage:` 分开归档；
- 判定结论写入状态，finalize 直接使用，不再自己比一次阈值（避免两处口径漂移）；
- 不检测盲区、不补盲——那是模块六的事。

---

### 节点 5：`upgrade_combinatorial_priority` —— 组合缺口重排

- 读模块七落库的 `combinatorial_pairs_covered`（不重扫用例）；
- 用 `priority.prioritized_pairs()` 按**两端权重之和**排序：P0×P0=1.2 最前，
  P2×P2=0.2 最后；同分按 id 排，保证确定性；
- 与模块七使用同一个分析上限（`max_capability_pairs_for_matrix`），便于对照；
- **只重排、不补题**：模块七本轮已经补过一次。

`priority.py` 是纯函数模块，模块七现在也委托它排序——下一轮评测时模块七会直接看到
真实分级。

---

### 节点 6：`generate_traceability_artifact` —— 可追溯性矩阵

输出到 `<ARTIFACTS_DIR>/<run_id>/`：

**traceability_matrix.json**（schema 是对外承诺，只加不改）

```jsonc
{
  "skill_id": "csv-cleaner",
  "skill_version_ref": "v1",
  "generated_at": "2026-09-12T15:02:29+00:00",
  "nodes": [
    {"id": "csv-cleaner:cap-…", "description": "支持读取 CSV 文件",
     "tier": "p0_core", "weight": 0.6, "covered": true, "covering_cases": ["c1"]}
  ],
  "negative_constraints": [
    {"id": "csv-cleaner:neg-…", "description": "查询 users 表必须过滤软删除",
     "covered": false, "covering_cases": []}
  ],
  "combinatorial_coverage": {
    "covered_pairs": [["cap-a", "cap-b"]],
    "weighted_coverage_ratio": 0.7,
    "negative_constraint_coverage_ratio": 0.0,
    "tier_grading_applied": true
  }
}
```

**traceability_matrix.csv**：同一份数据的扁平版，`kind` 列区分
`capability / negative_constraint / combinatorial_pair`，一对多用分号分隔，
`utf-8-sig` 编码保证 Excel 打开中文不乱码。

两种格式各有用途：JSON 给程序 diff 和可视化工具（如 Base44 热力图，仓库外对接），
CSV 给人拖进表格审阅。写盘失败不阻断评测，但会写进报告。

---

### 节点 7：`finalize_weighted_coverage_report` —— 写报告

写入 `dimension_results`，维度名 `weighted_coverage`：

| 情形 | status |
|---|---|
| 加权覆盖率取不到（状态键被裁掉） | NEEDS_HUMAN_REVIEW |
| 能力树为空 | NEEDS_HUMAN_REVIEW |
| 其他 | 直接取节点 4 的 Judge 判定（PASS / FAIL） |

- `score` = 加权能力覆盖率（约束覆盖率不混入，分母不同）；
- **`blocking` 恒为 False**——覆盖率反映"测试测得全不全"，不是 Skill 质量问题；
- findings 包括：加权覆盖率与权重分布、分级退化警告、约束覆盖率、未覆盖约束 id、
  **单列**的未判定约束、预算打满提示、补题结果、Top 10 组合缺口、制品路径。

---

## 六、主要业务流程（一次完整运行）

以一个"CSV 清洗"Skill 为例：

```
① 模块六已抽出 3 项能力（全是 P1 占位），模块七已写入已覆盖组合对
        │
② 节点1：分级 → 读CSV=P0，读Excel=P1，输出BOM=P2
         抽约束 → "查询 users 表必须过滤软删除记录"
        │
③ 节点2：候选用例 20 条（正向+对抗，去掉 COLD）
         没有用例带这条约束的绑定 → 20 次 Judge 判定
         全部 fail → 该约束"未覆盖"
        │
④ 节点3：补 1 条正向反事实用例，例如
         "统计下 users 表的活跃用户数，上个月我们批量注销了一批账号"
         新用例集版本激活，用例自带 negative_constraint_ids
        │
⑤ 节点4：读CSV未覆盖、读Excel和BOM已覆盖
         加权覆盖率 = (0.3+0.1)/1.0 = 40%（等权口径是 67%）
         Judge 判 FAIL（阈值 90%）
        │
⑥ 节点5：重排组合缺口，(读CSV, 读Excel) 这对 P0×P1 排在最前
        │
⑦ 节点6：写出 artifacts/<run_id>/traceability_matrix.json / .csv
        │
⑧ 节点7：报告记 weighted_coverage = FAIL（非阻断），列出上述发现
        │
⑨ 下一轮评测：节点2 靠绑定直接认出补出的反事实用例 → 约束覆盖率 100%，零调用
```

---

## 七、关键设计取舍一览

| 取舍 | 做法 | 为什么 |
|---|---|---|
| 规则复用还是新建 | 复用 `capability_coverage_threshold`，不加 `override_rule()` | 一个规则名只能对应一个实现，判定才可追溯 |
| 约束判定走不走 Judge | 走，`ROUTINE` | 是二元结论，需要黄金盲测；但不阻断合并，不值三倍成本的共识 |
| 判定预算 | 总量上限 200，超出记"未判定" | 调用量 = 约束数 × 用例数，容易失控；未判定≠未覆盖 |
| 补题后要不要回环 | 不回环 | 每圈又是一批裁判调用；靠绑定在下一轮零成本收敛 |
| 反事实用例的类别 | 正向 | 它是该由 Skill 处理的请求，只是埋了坑 |
| 组合缺口要不要再补 | 只重排不补 | 模块七当轮已补过，避免重复出题 |
| 是否阻断合并 | 不阻断 | 测试集不全不等于 Skill 有问题 |
| 制品写失败 | 不阻断，进报告 | 结论都在库里，制品只是导出 |

---

## 八、代码组织

```
nodes/weighted_coverage/
├── __init__.py    导出与模块说明
├── state.py       DIMENSION + 18 个 _weighted_coverage_* 私有状态键
├── deps.py        WeightedCoverageDeps（继承 CoverageDeps，可 from_coverage 复用实例）
├── priority.py    组合对排序纯函数（模块七也在用）
├── artifact.py    矩阵构造 + JSON/CSV 写盘
├── nodes.py       7 个节点 + 路由
└── graph.py       add_weighted_coverage_nodes / build_weighted_coverage_subgraph

配套：
agents/analyzer/service.py              classify_tiers / extract_negative_constraints
agents/analyzer/identity.py             build_constraint_id
agents/analyzer/prompts/*.jinja         分级与约束抽取 Prompt
agents/mini/templates/weighted_coverage.py + negative_constraint_probe.jinja
agents/generator/prompts/_shared.jinja  counterfactual_block
state/capability.py                     tier_grading_applied / negative_constraint_coverage
```

## 九、配置项（`SKILLEVAL_COVERAGE_*`）

| 变量 | 默认 | 作用 |
|---|---|---|
| `MIN_COVERAGE_RATIO` | 0.9 | 加权覆盖率达标线（与模块六共用） |
| `MAX_CONSTRAINT_PROBE_CALLS` | 200 | 约束判定单轮调用上限 |
| `MAX_CONCURRENT_MAPPINGS` | 10 | 判定并发上限 |
| `MAX_CAPABILITY_PAIRS_FOR_MATRIX` | 100 | 组合重排范围（与模块七共用） |
| `ARTIFACTS_DIR` | `artifacts` | 制品输出根目录 |

## 十、装进主图时要注意

1. 主图状态 schema 必须并入 `WeightedCoverageState` 的 18 个私有键，否则会被
   LangGraph 静默裁掉，报告降级为 NEEDS_HUMAN_REVIEW；
2. 连线顺序：`coverage.finalize_pruning_report → coverage.extract_tier_and_negative_constraints`；
3. 依赖复用：`WeightedCoverageDeps.from_coverage(prune.deps)`，共享同一个 Analyzer / Judge 实例；
4. CI 归档把 `artifacts/**/traceability_matrix.*` 与 `benchmark.json` 一起上传。
