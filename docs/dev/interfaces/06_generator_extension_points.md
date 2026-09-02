# 接入文档：Generator 扩展点（docs/dev/06 留给后续模块的接口）

> 由谁接入：`15`（Attacker）、`16/17`（模块六/七覆盖率补盲）、`19`（模块九）、
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
    combinatorial_pairs=[("cap-3", "cap-9")],   # 模块十的组合矩阵盲区
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

补生成数量默认按 focus 内容规模动态决定（每个能力/组合各一条正向、每个负向
约束一条反向），不固定 8-10。确有把握时可用 `positive_count` / `negative_count`
覆盖。

新增用例**独立**做 60/40 划分，已有用例的 `split` 归属不变——补盲区不应该让
已经跑过优化闭环的训练/验证集边界发生变化。

## 2. `triggered_by` 审计字段（`19`）

纯审计用途，不影响生成逻辑，供排查"测试集为什么突然变了"。约定取值：

| 值 | 谁写入 |
|---|---|
| `auto_bootstrap` | `ensure_test_suite()` 首次生成 |
| `manual_cli` | `skill-evaluate generate --force` |
| `coverage_gap` | 模块六/七检测到盲区 |
| `combinatorial_gap` | 模块十组合矩阵盲区 |
| `cross_model_sampling` | 模块九验证集不足时定向生成 |

## 3. 新增用例类别（`15` ADVERSARIAL、`20` MULTI_SKILL）

`agents/generator/agent.py::_TEMPLATE_BY_CATEGORY` 目前只注册了 POSITIVE 与
NEGATIVE。传入未注册的 category 会抛 `GenerationError`，错误信息里直接点名了
应由哪份文档补齐（不是静默跳过）。

接入方式：

1. 在 `agents/generator/prompts/` 下新增 `adversarial.jinja`（可 `import
   "_shared.jinja"` 复用 `skill_block` / `focus_block` / `output_contract`
   宏，保持输出契约一致）。
2. 在 `_TEMPLATE_BY_CATEGORY` 注册该 category。
3. 若需要额外的模板变量（如模块五的攻击手法清单），在
   `GeneratorAgent._generate_category()` 的 `render(...)` 调用处补参数。
   模板环境使用 `StrictUndefined`，变量拼错会在渲染期立刻报错而不是发出一个
   缺了半截的 Prompt。

`GeneratedCase.diversity_tag` 是自由字符串，新类别可以定义自己的取值集合，
不需要改 Schema。

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
