# 接入文档：Generator 扩展点（docs/dev/06 留给后续模块的接口）

> 由谁接入：`15`（Attacker，**见第 3 节的例外**）、`16/17`（模块六/七覆盖率补盲）、`19`（模块九）、
> `20`（模块十组合矩阵）、`21`（种子锚点与反坍塌）。
> 当前状态：接口全部就位，`REUSE` / `FORCE_REGENERATE` / `INCREMENTAL_PATCH`
> 三态已完整实现并有测试覆盖（`tests/skill_evaluate/test_generator.py`）。

## 0. 先读这一段：三态语义不要弄混

| 模式 | 谁能触发 | 是否调用 LLM |
|---|---|---|
| `REUSE` | 流水线默认路径 | **只在"从来没生成过"时**调用一次 |
| `FORCE_REGENERATE` | 只有人（CLI `--force` / CI 显式参数） | 每次都调用 |
| `INCREMENTAL_PATCH` | 模块六/七/十的节点**可在流水线内自动调用** | 每次都调用，但只补盲区 |

`INCREMENTAL_PATCH` 不受"手动强制"约束限制——它是有明确理由（覆盖率盲区）的
定向生成。把它当成"变相的自动重生"来用（例如构造一个覆盖全部能力的 focus）会
破坏整个复用约束，不要这么做。

另有一条容易踩的坑：**SKILL.md 版本漂移不会自动重新生成**，只产生
`EnsureTestSuiteResult.staleness_warning`。这是刻意的——自动重生会让"这次改动
到底影响了什么"永远无法归因（旧题换新题，分数变化说明不了任何问题）。

## 1. `CapabilityFocus` 的生产者（`16`/`17`/`20`）

```python
from skill_evaluate.agents.generator import CapabilityFocus, TestSuiteService

focus = CapabilityFocus(
    capability_ids=["cap-3", "cap-7"],
    negative_constraint_ids=["neg-2"],
    combinatorial_pairs=[("cap-3", "cap-9")],   # 模块七/十的组合矩阵盲区
    # 必填：Prompt 里给模型看的是描述，不是裸 id。缺失的 id 会退化为 id 原文，
    # 那对模型没有任何信息量，生成出来的用例也就补不到真正的盲区。
    descriptions={
        "cap-3": "处理带 BOM 头的 UTF-8 文件",
        "cap-7": "对超过 100MB 的输入切块处理",
        "neg-2": "禁止在未确认目标路径存在时执行写入",
    },
)

version = await TestSuiteService().incremental_patch(
    skill, focus, triggered_by="coverage_gap",
)
```

> ✅ **`16` 已落地，是本方法的首个真实调用方**，实现见
> `nodes/coverage/nodes.py::CoveragePipeline.feedback_driven_generation`。两条经验：
>
> - `descriptions` 的"必填"是字面意义上的。模块六的 `capability_id` 是**描述文本的
>   哈希**（`agents/analyzer/identity.py`），不填描述时 Prompt 里出现的是
>   `csv-cleaner:cap-3f9ac21b0d47` 这样一串东西，补出来的题必然补不到盲区。
> - **调用方要接住 `GenerationError`**。"这个 Skill 还没有任何 active 用例集"会走到
>   这条异常上，而覆盖率维度不阻断合并，为一次补题失败掀掉整条流水线不成比例。
>   模块六的处理是标记补盲耗尽 + 把原因写进报告 findings。
>
> ✅ **`17` 是 `combinatorial_pairs` 的首个真实调用方**，实现见
> `nodes/pruning/nodes.py::PruningPipeline.combinatorial_feedback_generation`。
> `18` 又在 `prompts/_shared.jinja` 里新增了一个 `counterfactual_block` 宏，**只挂在
> `positive.jinja` 上**：它把"怎么构造一个会诱导智能体踩坑的场景"写成硬要求（说出
> 陷阱前提、违反与否可观测、**不要在题面里复述规则本身**——那会让题目退化成一道
> 阅读理解）。`focus` 里没有 `negative_constraint_ids` 时整段不渲染。
>
> 它顺带修好了 `prompts/_shared.jinja` 的一处遗漏：`focus_block` 宏原先只给
> `capability_ids` 与 `negative_constraint_ids` 渲染了人类可读描述，**组合对那一段
> 只渲染裸 id**。`capability_id` 是描述文本的哈希，模型看到两串哈希无法构造出真正
> 同时用到两项能力的场景，于是组合覆盖率永远补不上去。填 `descriptions` 时记得
> 把组合对里的两个 id 都填上。

补生成数量默认按 focus 内容规模动态决定（每个能力/组合各一条正向、每个负向
约束一条反向），不固定 8-10。确有把握时可用 `positive_count` / `negative_count`
覆盖。

> ✅ **`18` 是 `negative_constraint_ids` 的首个真实调用方**，实现见
> `nodes/weighted_coverage/nodes.py::WeightedCoveragePipeline.constraint_feedback_generation`。
> 它暴露了上面那句默认映射的一处**语义错配**，后来的调用方请注意：
>
> 默认把"每个负向约束"映射到 `negative_count`（即 NEGATIVE 类别）。但本项目的
> `NEGATIVE` 指的是"不该触发本 Skill"的**近脱靶**题，而反事实用例恰恰是**该由
> 本 Skill 处理**的真实请求——只是场景里埋了个会让人踩坑的前提。两者是相反的
> 类别。因此 `18` 显式传 `positive_count=约束条数` / `negative_count=0`，走正向
> 模板。
>
> **默认值没有改动**（改它会影响所有既有调用方的条数计算），但凡是用
> `negative_constraint_ids` 补题的调用方都应当显式指定这两个参数。

新增用例**独立**做 60/40 划分，已有用例的 `split` 归属不变——补盲区不应该让
已经跑过优化闭环的训练/验证集边界发生变化。

## 2. `triggered_by` 审计字段（`19`）

纯审计用途，不影响生成逻辑，供排查"测试集为什么突然变了"。约定取值：

| 值 | 谁写入 |
|---|---|
| `auto_bootstrap` | `ensure_test_suite()` 首次生成 |
| `manual_cli` | `skill-evaluate generate --force` |
| `coverage_gap` | 模块六/七检测到盲区 |
| `combinatorial_gap` | **模块七组合矩阵盲区**（docs/dev/17，首个真实生产者）、模块十组合矩阵盲区 |
| `negative_constraint_gap` | **模块八负向约束盲区**（docs/dev/18，首个也是唯一的生产者）——某条 Gotchas 禁令没有任何反事实用例去诱导 |
| `cross_model_sampling` | 模块九验证集不足时定向生成 |

## 3. 新增用例类别（`20` MULTI_SKILL）

> ⚠️ **`13` 落地后此节已改**：原先的硬编码字典 `_TEMPLATE_BY_CATEGORY` 已被
> **注册表**取代，新增类别不再需要改 `agent.py`。

已注册四个类别：`positive` / `negative`（`06`）、`progressive_disclosure_trigger` /
`progressive_disclosure_regular`（`13`）。传入未注册的 category 仍然抛
`GenerationError`，错误信息里直接点名了应由哪份文档补齐（不是静默跳过）。

> ⚠️ **`15` 落地后此节又改了一处：`ADVERSARIAL` 不走这张表**。模块五底下有**七个
> 攻击面**，各有各的构造要求，共用一个 `adversarial.jinja` 会得到一份七种要求混在
> 一起的超长 Prompt，模型只会挑最好写的那两类反复出题。它由
> `agents.attacker.AttackerAgent`（`GeneratorAgent` 的子类，只重写 `ADVERSARIAL`
> 这一支，其余类别照常交给父类）用**第二层**注册表
> `agents/attacker/playbook.py` 承担。用普通 `GeneratorAgent` 传 ADVERSARIAL 会拿到
> `GenerationError`，错误信息里点名了应改用 `AttackerAgent`。
>
> 换句话说：**类别底下还要再分手法时，加第二层注册表，不要把手法塞进一个模板**。
> `20` 的 MULTI_SKILL 若也演化成多种组合手法，照 `attacker/playbook.py` 抄。

接入方式（以 `20` 的 MULTI_SKILL 为例）：

1. 在 `agents/generator/prompts/` 下新增 `adversarial.jinja`（可 `import
   "_shared.jinja"` 复用 `skill_block` / `focus_block` / `output_contract`
   宏，保持输出契约一致）。
2. 调一次 `register_generation_template()`——放在
   `agents/generator/prompts/registry.py` 的内置注册区，或自己模块里做导入副作用
   （两者都可，前者更容易被人找到）：

   ```python
   from skill_evaluate.agents.generator.prompts.registry import register_generation_template

   register_generation_template(
       TestCaseCategory.MULTI_SKILL,
       "multi_skill.jinja",
       description="多技能并发用例（模块十 / docs/dev/20）",
   )
   ```

   重名注册、模板文件不存在都会**立刻**抛 `GenerationError`，不会拖到真实出题时
   才暴露。
3. 若需要额外的模板变量（如模块五的攻击手法清单），在
   `GeneratorAgent._generate_category()` 的 `render(...)` 调用处补参数。
   模板环境使用 `StrictUndefined`，变量拼错会在渲染期立刻报错而不是发出一个
   缺了半截的 Prompt。
4. 条数用 `GenerationRequest.category_counts`（`13` 追加的字段）声明，不要去动
   `positive_count` / `negative_count` 的语义：

   ```python
   GenerationRequest(..., category_counts={TestCaseCategory.MULTI_SKILL: 12})
   ```

`GeneratedCase.diversity_tag` 是自由字符串，新类别可以定义自己的取值集合，
不需要改 Schema。`GeneratedCase.probe_target_reference` 同理是 `13` 追加的可选
字段，只有声明了 `requires_probe_target=True` 的类别会读它。

### 3.1 `ensure_test_suite(extra_categories=...)`：给自己的维度补一批专属用例

`13` 需要两个只有它自己用的类别，为此给 `ensure_test_suite()` 加了两个关键字参数
（**追加式扩展，原有调用方不受影响**）：

```python
result = await TestSuiteService().ensure_test_suite(
    skill,
    extra_categories=[TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER],
    category_counts={TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER: 3},
)
```

语义仍然是 **REUSE**：只有当现有 active 用例集里这些类别**一条都没有**时才补生成，
且**只生成这些类别**（正/反向用例原样继承、`split` 归属不变，走
`INCREMENTAL_PATCH` 模式落一个新版本）。判定口径刻意是"一条都没有"而不是"条数够
不够"——后者没有客观答案，做成自动触发条件等于给流水线开了个每次运行都可能悄悄
再出一批题的口子。

请求条数算到 0 时**不会**发 LLM 请求，直接当"这个 Skill 没有这类用例可出"处理
（`13` 用它表达"这份 Skill 没有 references/，无渐进式披露可探"）。

### 3.2 `extra_triggered_by=`：让"这批题是谁让出的"可追溯（`15` 追加）

补生成走 `INCREMENTAL_PATCH` 落一个新版本，`triggered_by` 默认写
`"dimension_extra_categories"`。所有走 `extra_categories` 的维度共用一个值，等于把
"测试集为什么变了"这条线索抹平了，因此加了一个纯审计的覆盖参数：

```python
await TestSuiteService().ensure_test_suite(
    skill,
    extra_categories=[TestCaseCategory.ADVERSARIAL],
    category_counts={TestCaseCategory.ADVERSARIAL: 14},
    extra_triggered_by="attacker_bootstrap",       # 15 用这个值
)
```

不影响任何生成逻辑，只落到 `triggered_by` 审计字段（取值表见第 2 节）。

### 3.3 `incremental_patch_categories()`：按**类别**定向重出题（`15` 追加）

`incremental_patch()` 是按**能力盲区**补题（要一个非空的 `CapabilityFocus`）。`15`
的"红队手法更新了，同一批攻击面重新出题"不属于那个场景——它不是某个能力没覆盖到。

```python
version = await TestSuiteService().incremental_patch_categories(
    skill,
    categories=[TestCaseCategory.ADVERSARIAL],
    category_counts={TestCaseCategory.ADVERSARIAL: 14},
    triggered_by="manual_cli",
)
```

与 `_ensure_extra_categories()` 的差别：那个的口径是"该类别一条都没有才生成"
（REUSE 语义），本方法是**显式的重出题，每次都生成**。刻意做成两个方法而不是加一个
`force` 参数——那个参数会让 REUSE 路径上多一条随时可能被误传的分支，而"测试集是否
重出"是本项目最要紧的一条约束。已有用例（包括旧的同类别用例）原样继承，`split` 归属
不变，旧版本保留为非 active。

## 4. 反坍塌校验与种子锚点（`21`）

### 4.1 `_check_generation_collapse()`

```python
# src/skill_evaluate/agents/generator/service.py
async def _check_generation_collapse(new_cases: list[TestCase]) -> bool: ...
```

当前占位实现：非空即放行。返回 False 时 `_generate_and_activate()` 会抛
`GenerationError` 并**阻断 activate**（坍塌的用例集比没有用例集更危险——它会给
出一个虚高的通过率）。

`21` 接入时只需替换函数体为"计算 prompt 向量与历史用例库的分布距离"，
`_generate_and_activate()` 的调用结构不需要改动。依赖 `23` 的 pgvector 检索层。

### 4.2 `GenerationRequest.seed_anchor_ids`

当前 `GeneratorAgent._resolve_seed_texts()` 是简化版：直接把 id 原文当作
few-shot 文本注入 Prompt（模板里的 `seed_block` 宏已就位）。`21` 接入 GitHub
托管的种子锚点配置文件后，替换该方法实现即可，调用点不变。

注意 `TestCase.seed_anchor_id` 当前恒为 `None`（因为没有真实种子库可溯源）。
`21` 落地后需要在 `_generate_category()` 里回填这个字段。

## 5. Optimizer 闭环的澄清（`09`/`11`）

"Optimizer 重写 description 后需要在训练集上重新验证"这个场景**不经过
Generator**——它不需要新用例，只需要重跑现有 `TestCase`，由 `11` 直接调用
Executor。不要为此调用 `incremental_patch()`。

## 6. staleness 告警的透传（`24`）

`ensure_test_suite()` 返回 `EnsureTestSuiteResult`：

```python
result = await TestSuiteService().ensure_test_suite(skill)
report = await ReportGenerator().build(
    run_id, test_suite_staleness_warning=result.staleness_warning,
)
```

`BenchmarkReport.test_suite_staleness_warning` 与 HTML 报告的告警条已就位，
`24` 只需要把这个值透传进去。不透传的后果是：读报告的人不知道这份分数是拿旧题
跑出来的。
