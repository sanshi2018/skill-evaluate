# 接入文档：Validator 的工具箱、断言下发与证据组合（docs/dev/10 留给后续模块的接口）

> 由谁接入：`13`（模块三，执行效果的确定性证据）、`15`（模块五，SAST / 数据投毒
> 场景 + 两个安全模板的扫描后端）、`23`（`_semantic_lookup()` 换成混合检索）、
> `24`（`sync-toolbox` 的 CI 调度）、以及**运维侧**（创建工具箱仓库本身）。
> 当前状态：三条策略路径、静态检查与降级、沙箱下发过滤、Hook 回传落库、Judge
> 证据字典全部落地，有测试覆盖（`tests/skill_evaluate/test_validator.py`）。
> **工具箱仓库本身还不存在**——那不是漏了，见第 4 节。

---

## 0. 一条铁律（先看这条）

**Validator 只规划，不判定。** `AssertionResult` 是证据，不是结论：

- 断言脚本的执行发生在沙箱里（`HermesBackend` 下发 + Hook 回传），不在本包内；
- "断言过了算不算通过"由 `JudgeAgent` 决定（docs/dev/interfaces/08 的铁律同样
  适用：凡是"通过/失败"的结论一律经过 `judgmental_verdict()`）。

因此本包**故意不提供** `verdict_from_assertion()` 之类的函数。你需要的是把证据
喂进 `content`，然后让 Judge 出结论——写法见第 3 节。

---

## 1. 三步用起来

```python
from skill_evaluate.agents.validator import ValidatorAgent

agent = ValidatorAgent()          # 依赖注入一个单例，别在每个节点里各自 new

# 1) 执行之前：规划断言（可批量）
batch = await agent.plan_assertions(cases, skill)

# 2) 执行时：把 spec 挂到 ExecutionRequest 上，随任务一起进沙箱
trace = await backend.execute(
    ExecutionRequest(
        skill=skill, case=case, run_index=0, run_id=run_id,
        assertion_specs=[spec_of(case)],       # 只放这条用例自己的 spec
    )
)

# 3) 判定时：把断言证据并进 Judge 的 content
from skill_evaluate.agents.validator import build_assertion_evidence
from skill_evaluate.persistence.repository import AssertionRepository

results = await AssertionRepository().list_results(spec.assertion_id)
verdict = await judge.judgmental_verdict(
    subject_id=case.case_id,
    template_key="execution_effect",          # 你自己的模板（docs/dev/07 注册）
    content={**your_content, **build_assertion_evidence(results, spec=spec)},
    criticality=Criticality.ROUTINE,
)
```

`plan_assertion()` 的可选参数 `known_params`：你的维度如果**自己约定了产物路径**
（比如"产物一律写到 `out.json`"），把它作为模板参数传进来，就能走
`template_lookup` 这条**零 LLM 成本**的路径；不传的话同一个模板会退化成
`template_inherit`（要调一次模型做参数化）。

### 什么时候会拿到 `strategy=NONE`

两种，语义不同，**报告里要区分**：

| 情况 | `failure_reason` | 含义 |
|---|---|---|
| `case.expected_output` 为空 | "用例未声明 expected_output……" | 本来就不需要断言，正常 |
| 连续 3 次生成的脚本语法不过 | "断言生成失败（连续 3 次未通过静态检查）……" | **要标记出来给人看** |

`plan_assertions()` 返回的 `AssertionPlanBatch.degraded_case_ids` 只收第二种。
流水线不会因为断言生成失败而中断（docs/dev/10 第 6 节），但报告不该假装无事发生。

---

## 2. 执行侧契约（`13`/`15` 用得到，但基本不用改代码）

- `ExecutionRequest.assertion_specs`：非空时由 `HermesBackend` 下发。
  `executable_assertion_specs()` 会先滤掉 `strategy=NONE` 与没有脚本正文的 spec，
  沙箱客户端拿到的一定是能跑的。
- Hook payload 追加了 `assertion_executions[]`（`assertion_id` / `exit_code` /
  `stdout` / `stderr`），`api/hooks_hermes.py` 收到后落 `assertion_results` 表，
  `passed = (exit_code == 0)`。
- `MiniAgentBackend` 收到 `assertion_specs` 会**忽略并 warning**。断言脚本要求真实
  沙箱，需要断言的维度必须路由到 `PLUGGABLE` 后端（`executors/routing.py`）。
- 落库前会先查 spec 是否存在（外键）。回传了一个系统没规划过的 `assertion_id` 时
  记 warning 跳过，不让一条来路不明的断言把整个 Hook 变成 500 —— Trace 是主线数据。

**真实 Hermes 客户端的实现方**（`docs/dev/interfaces/03_hermes_sandbox_client.md`）
必须遵守：在任务主流程结束、**容器销毁之前**把脚本写到 `spec.script_path` 并执行，
结果随**同一次** Hook 回调上报，不要单独再发一次。

---

## 3. `13`/`15` 要自己实现的：证据怎么和 LLM 裁决组合

docs/dev/10 第 7 节明确把组合规则留给你们，这里只给两条已经被验证过的模式：

```python
# 模式 A（模块三常用）：断言失败直接判负，不惊动 LLM
if any_failed(results):
    # quantitative_verdict 是同步的、不调 LLM（docs/dev/08 第 1 节）
    verdict = judge.quantitative_verdict(
        case.case_id, "execution_assertion_failed", {"exit_code": results[0].exit_code}
    )
else:
    verdict = await judge.judgmental_verdict(...)   # 断言通过仍要判非结构化质量

# 模式 B（模块五常用）：断言只是证据之一，安全结论仍走语义裁决
content = {**base_content, **build_assertion_evidence(results, spec=spec)}
verdict = await judge.judgmental_verdict(..., criticality=Criticality.CRITICAL)
```

模式 A 的量化规则要按 `docs/dev/interfaces/08_judge_rules_and_criticality.md`
第 1 节注册（`@register_rule`），**不要**在节点里写 `if ... else` 直接下结论。

`build_assertion_evidence()` 产出的键（全部是 `str`，与 `content: dict[str, str]`
对齐）：`assertion_strategy` / `assertion_exit_code` / `assertion_stdout` /
`assertion_stderr` / `assertion_passed` / `assertion_summary`。你的 Prompt 模板按
这些键取值即可。没有断言时只有 `assertion_strategy` + `assertion_summary`，且
`summary` 会显式说明"本用例未执行确定性断言"——**不要**把这种情况当作断言通过。

---

## 4. 工具箱仓库：要有人去建（运维侧，不属于单一开发文档）

`skill-evaluate-assertion-toolbox` 是**外部独立仓库**，本项目只消费它。按
docs/dev/10 第 3.1 节创建：

```
skill-evaluate-assertion-toolbox/
├── templates/
│   ├── json_schema_validator.py.jinja      # 初始至少这两个
│   ├── file_exists_validator.py.jinja
│   ├── sql_no_injection_validator.py       # 由 15 补实际扫描后端
│   └── html_no_xss_validator.py            # 同上
├── manifest.yaml
└── CHANGELOG.md
```

`manifest.yaml` 每条记录（`keywords` 与 `params` 行内/块列表都支持；装了 PyYAML
就走 PyYAML，没装走内置的极简解析器）：

```yaml
- template: json_schema_validator.py.jinja
  description: "校验目标文件是否为合法 JSON 且满足给定 schema"
  keywords: [json, schema, 结构化输出, 格式校验]
  params: [target_file, schema_definition]
  language: python          # 可选，缺省按文件名后缀推断
```

写模板时必须遵守和生成脚本同一套规范（`prompts/_shared.jinja` 里的
`script_contract`）：`exit 0` 通过 / 非 0 失败，干净输出走 stdout、诊断走 stderr，
只读验证、只用标准库。

配置与同步：

```bash
export SKILLEVAL_VALIDATOR_TOOLBOX_REPO_URL=git@github.com:<org>/skill-evaluate-assertion-toolbox.git
export SKILLEVAL_VALIDATOR_TOOLBOX_REF=main
skill-evaluate sync-toolbox        # 跑评测之前执行，不要在评测中途拉仓库
```

`24` 负责把这条命令挂进 CI（评测 job 之前的一个 step，或一个定时 job）。

**没有工具箱也能跑**：`available=False` 时全部走 `generated_from_scratch`，只是
每条断言都要花一次 LLM 调用，且缺少"被复用过很多次的模板"这层质量兜底。

### `15` 的两件事已完成 ✅

产物在**本仓库根目录** `assertion_toolbox/`（含 `README.md`，写明了 `manifest.yaml`
要追加的两条记录）：

```
assertion_toolbox/templates/sql_no_injection_validator.py
assertion_toolbox/templates/html_no_xss_validator.py
```

**运维侧还需要做一步**：把这两个文件复制进外部的
`skill-evaluate-assertion-toolbox` 仓库的 `templates/`，并按 README 里的片段追加
`manifest.yaml` 记录（`params: []`，纯 `.py` 非 `.jinja`，`render()` 原样返回，
因此能走零 LLM 的 `template_lookup`）。

两个脚本的关键设计：

- **两级扫描**：Semgrep（装了才跑，`p/sql-injection` / `p/xss`，可用
  `SKILLEVAL_SEMGREP_SQL_CONFIG` / `SKILLEVAL_SEMGREP_XSS_CONFIG` 指向本地规则）
  + 内置正则（永远跑）。Semgrep 不在 PATH 或规则包拉不下来（模块五强制无出站网络
  的沙箱里这是常态）时**不判扫描失败**，只记一条 info 继续用内置规则。
- **退出码**：`0` 通过 / `1` 发现高危 / `2` **扫描器本身出错**（没有可扫的产物、
  读不了文件）。`AssertionResult.passed` 只认 `exit_code == 0`，1 与 2 都算失败——
  分开只是为了让人一眼看出"发现了问题"和"没扫成"不是一回事。
- **没扫成绝不算通过**：一个因为"扫描器崩了"而被判成安全的产物，比一个明确被判失败
  的产物危险得多——后者会被人看到，前者不会。`15` 的判定侧同样贯彻这条：断言没跑成
  时判 `NEEDS_HUMAN_REVIEW` 而不是 PASS。
- **XSS 那份只认未转义的载荷**：一份把 `&lt;script&gt;` 原样打印出来的报告是安全的
  ——那正是转义生效的样子。

---

## 5. `23` 要替换的：`_semantic_lookup()`

> ✅ **`23` 已落地**：混合检索（`collection="assertion_templates"`）与关键词分数取 max，记忆库故障回落纯关键词；
> 索引由 `sync-toolbox`（同步后自动）/ `memory-index` 写入。`AssertionToolbox` 新增可选参数 `search_service`。
> 详见 `docs/dev/interfaces/23_memory_and_data_flywheel.md` 第 2 节。下面是原始约定，保留备查。

```python
# src/skill_evaluate/agents/validator/toolbox.py
async def _semantic_lookup(self, query: str) -> list[TemplateMatch]:
    return self._keyword_lookup(query)      # <- 换成向量 + BM25 + Reranker
```

**只换函数体，保持签名与返回类型**：`score` 继续归一化到 0~1（`lookup()` 用
`ValidatorSettings.template_match_threshold` 过滤），`ValidatorAgent` 和阈值配置
都不需要改。当前的关键词版本（`extract_keywords()` + `score_template()`）保留为
降级路径与单测基线，不要删。

---

## 6. 相关配置（`config.py::ValidatorSettings`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `toolbox_repo_url` | `None` | 未配置 = 工具箱不可用（合法状态） |
| `toolbox_ref` | `main` | 分支或 commit sha |
| `toolbox_cache_dir` | `~/.cache/skill-evaluate/assertion-toolbox` | 本地缓存目录 |
| `template_match_threshold` | `0.34` | 关键词命中比例阈值 |
| `max_script_repair_retries` | `2` | 语法检查失败的重试次数（第 6 节要求 2 次） |
| `sandbox_script_dir` | `/tmp/skill-evaluate/assertions` | 脚本在沙箱内的落盘目录 |
| `assertion_timeout_s` | `30` | 单条断言的沙箱执行超时，供沙箱客户端下发 |
| `LLMSettings.validator_model` | `anthropic/claude-sonnet-5` | 生成脚本的模型 |
