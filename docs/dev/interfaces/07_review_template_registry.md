# 接入文档：Mini Agent 评审模板注册表（docs/dev/07 留给后续模块的接口）

> 由谁接入：`12`（模块二）、`13`（模块三）、`14`（模块四）、`15`（模块五）、
> `19`（模块九）、`08`（Judge 复用本框架做共识投票）。
> 当前状态：注册机制 + 首批 7 个模板（Prompt 措辞、输出 Schema、判定映射）已
> 全部落地并有测试覆盖（`tests/skill_evaluate/test_mini_review.py`）。

## 1. 首批 7 个模板已就位

| key | 归属模块 | 必需的 `content` 变量 | 输出 Schema |
|---|---|---|---|
| `omission_audit` | 模块二 / `12` | `skill_md` | `OmissionAuditOutput` |
| `scoping_check` | 模块二 / `12` | `skill_md` | `ScopingCheckOutput` |
| `progressive_disclosure_static` | 模块二 / `12` | `skill_md`, `reference_files` | `ProgressiveDisclosureStaticOutput` |
| `help_doc_quality` | 模块四 / `14` | `script_path`, `help_output` | `HelpDocQualityOutput` |
| `constructive_error` | 模块四 / `14` | `invocation`, `error_output` | `ConstructiveErrorOutput` |
| `linguistic_smell` | 模块九 / `19` | `skill_md` | `LinguisticSmellOutput` |
| `control_calibration` | 模块三 / `13` | `skill_md` | `ControlCalibrationOutput` |

调用方式：

```python
from skill_evaluate.agents.mini import MiniReviewAgent, ReviewRequest

verdict = await MiniReviewAgent().review(
    ReviewRequest(
        subject_id=skill.skill_id,
        template_key="omission_audit",
        content={"skill_md": skill.body_markdown},
    )
)
```

需要读模板专有字段（如 `common_sense_statements`）时用 `review_detailed()`，
它返回 `DetailedReview(verdict, output, template)`。

`content` 变量缺失会在**发请求之前**抛 `ReviewTemplateError`（模板环境用
`StrictUndefined`），不会发出一个缺了半截的 Prompt。

### `12`/`13`/`14`/`19` 还需要做什么

Prompt 措辞与判定细则已在 `.jinja` 里写完（含判定信号、反面示例、"什么不算问题"
的误伤边界）。各文档接入时**只需**：

1. 决定**阈值**——例如 `omission_audit` 命中几条常识才算维度级 FAIL、
   `progressive_disclosure_static` 允许几个文件无触发条件。模板层只产出单次
   pass/fail 与命中清单，"多少条算不合格"是维度节点的策略，不在模板里写死。
2. 决定该维度是否 `blocking`，然后调
   `ReportGenerator.record_dimension_result()` 落库。
3. 若实测发现措辞需要调整，直接改 `.jinja` —— **但不要改 `schemas.py` 里的
   字段**，报告聚合与后续共识投票都按字段读取。

## 2. 新增一个模板（三步，不改 `MiniReviewAgent` 本体）

```python
# 1) agents/mini/templates/my_check.jinja
#    建议 {% import "_prefix.jinja" as prefix %} 并调用 prefix.common_rules()，
#    以继承框架级的三条约束（只输出 JSON / 宁可漏判不误判 / reasoning 引用原文）

# 2) agents/mini/templates/schemas.py
class MyCheckOutput(BaseReviewOutput):
    suspicious_lines: list[str] = Field(default_factory=list)

# 3) 注册（放在自己模块的导入路径上，或追加到 templates/builtin.py）
register_template(ReviewTemplate(
    key="my_check",
    prompt_path="my_check.jinja",
    output_schema=MyCheckOutput,
    to_status=verdict_field_to_status,
    required_variables=("skill_md",),
    description="……（模块 X / docs/dev/NN）",
))
```

约束：key 重复注册直接报错（静默覆盖会让"到底跑的是哪份 Prompt"无法追溯）；
`prompt_path` 指向的文件不存在也在注册期就报错。

## 3. `13`：动态渐进式披露探查模板

`07` 说明该场景需要 `PLUGGABLE` 后端配合 Trace，不是纯文本审查，因此未预置。

接入方式：新增模板，把 `ExecutionTrace.actions` 序列化后作为一个 content 变量
传入（例如 `trace_actions`），与 `skill_md` 一并渲染。`ReviewRequest.content` 是
`dict[str, str]`，序列化格式由 `13` 自定（建议每行一条 `step_id | action_type |
action_input` 摘要，比塞整个 JSON 更省 token 也更好读）。

## 4. `15`：严重性定级（`to_severity` 扩展位已备好）

`07` 原文留了一个开放问题："`to_status` 映射到 `SeverityLevel` 而非
`JudgeVerdictStatus`，需要泛化返回类型或新增并行字段"。

**已按"新增并行字段"落地**，`15` 不需要修改 `ReviewTemplate` 或
`MiniReviewAgent`：

```python
register_template(ReviewTemplate(
    key="security_severity_rating",
    prompt_path="security_severity_rating.jinja",
    output_schema=SecuritySeverityOutput,
    to_status=lambda o: (
        JudgeVerdictStatus.FAIL if o.severity in {"critical", "high"} else JudgeVerdictStatus.PASS
    ),
    to_severity=lambda o: SeverityLevel(o.severity),
))

detailed = await agent.review_detailed(request)
severity = detailed.template.to_severity(detailed.output)   # -> SeverityLevel
```

`review()` 仍然只返回 `JudgeVerdict`（返回类型保持稳定），严重级别从
`review_detailed()` 的 `template` + `output` 组合取得。

## 5. `08`：Judge 多副本共识如何复用本框架

投票是 Judge 的职责，评审模板执行是 Mini Agent 的职责，两层不重叠。Judge 侧对
同一个 `ReviewRequest` 反复调用 `review()` / `review_detailed()` 即可：

```python
agents = [MiniReviewAgent(temperature=t, persist=False) for t in (0.0, 0.3, 0.7)]
verdicts = await asyncio.gather(*(a.review(request) for a in agents))
```

- `persist=False` 关闭单次落库，由 Judge 统一决定哪些 verdict 入库
  （默认 `persist=True`，纯静态审查场景单次输出即最终判定，直接落库）。
- ⚠️ **温度扰动在默认 `judge_model`（`claude-sonnet-5`）上不生效** ——
  新一代 Claude 模型已移除采样参数。这会直接影响 `08` 的共识机制设计，务必先读
  `docs/dev/interfaces/06_llm_client_and_sampling.md` 第 2 节，里面列了三个可选
  方案。

### `08` 已落地（实际做法）

`08` 定稿为**视角扰动**：三副本同模型同温度，差异来自各自的 system 后缀。为此
`MiniReviewAgent` 追加了一个可选构造参数 `system_suffix`（默认 `None`，不影响
任何既有调用）：

```python
MiniReviewAgent(system_suffix=f"{STEP_CITATION_RULE}\n\n复核视角：**反例存在性**……")
```

追加到 system 而不是塞进 `content`：`content` 变量是各模板自己的契约，往里塞一个
只有部分模板会渲染的键，等于让扰动在另一部分模板上悄悄失效。

因此 CRITICAL 场景要求的 `[step:N]` 引用约定**由 Judge 统一下发**，各模板
（含 `15` 的严重性定级模板）**不需要**在自己的 `.jinja` 里重复写这条要求。
详见 `docs/dev/interfaces/08_judge_rules_and_criticality.md` 第 2 节。

## 6. `MiniAgentBackend` 的 Stub 已被替换

`docs/dev/interfaces/05_langfuse_hook_and_agent_base.md` 第 2 节的待接入项已完成：
`executors/factory.py::build_backend()` 现在注入
`agents/mini/llm_client.py::RealMiniLLMClient`，`StubMiniLLMClient` 不再是默认。
`MiniAgentBackend.execute()` 的 `final_response` 从此是真实模型输出，依赖它做
业务决策的调用方可以正常读取了。
