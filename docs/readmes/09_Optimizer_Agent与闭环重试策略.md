# 实现说明：09 Optimizer Agent 与闭环重试策略

> 对应设计文档：`docs/dev/09_Optimizer_Agent与闭环重试策略.md`
> 状态：已实现（通用闭环编排 + 两套角色 Prompt + 补丁应用 + 超限挂起全部落地）

消费 [`08`](./08_Judge_Agent核心框架与裁判可信度机制.md) 的判定结果，复用
[`06`](./06_Generator_Agent与测试集生命周期管理.md) 的 `BaseLLMAgent` 底座。

---

## 1. 交付了什么

| 文件 | 内容 | 对应设计文档章节 |
|---|---|---|
| `agents/optimizer/service.py` | `OptimizerAgent.propose_patch()`、`build_failure_context()`、角色注册表 | 第 3、4、6 节 |
| `agents/optimizer/loop.py` | `OptimizationLoop`、`LoopResult`、`RetestFn`、超限挂起与人工裁决 | 第 5 节 |
| `agents/optimizer/patch_applier.py` | `apply_unified_diff()`、`apply_patch()`、临时工作副本 | 第 7 节 |
| `agents/optimizer/schema.py` | `FailureContext`、`PatchProposal` | 第 3 节 |
| `agents/optimizer/prompts/*.jinja` | 公共 diff 约束宏 + `description_patch` + `appsec_patch` | 第 3、6 节 |
| `state/patch.py` | `Patch` / `PatchApplicationResult` / `PatchType` | 第 2 节 |
| `errors.py::PatchApplyError` | diff 与当前内容不匹配 | 第 7 节 |
| `config.py::OptimizerSettings` + `LLMSettings.optimizer_model` | 最大重试次数、温度、补丁模型 | 第 5 节 |
| `persistence/` | `patches` / `patch_application_results` / `node_retry_counts` 三表 + revision `0004` | 第 2、5 节 |

---

## 2. 用什么方式实现了需求

### 2.1 `retest_fn` 是这套编排能同时服务两种闭环的唯一原因

`OptimizationLoop` **不知道**"重新测试"具体意味着什么。模块一的 `retest_fn` 是
"用训练集用例重跑 Executor+Judge"；模块五的是"重跑该安全用例**并且**触发一次全量
功能回归"。两者语义完全不同，共用同一套挂起条件、重试计数、补丁记录格式。

配套的一个实现细节：`working_skill` **逐轮迭代**（第 2 轮补丁打在第 1 轮结果上），
因为第 1 轮改动往往部分有效；每轮从原始版本重开会让模型反复走同一条死路。

### 2.2 训练集约束落在类型层面

`build_failure_context()` 是构造 `FailureContext` 的唯一合法入口，非 TRAIN 用例
直接 `ValueError`。靠调用方自觉是不够的——真正想拿验证集去优化的那一次，恰恰是
最想走捷径的那一次。

顺手做了两件事：接受 `ConsensusResult` 并展开成三份 verdict（调用方不必为
ROUTINE / CRITICAL 写两套构造代码）；把 `failed_case_prompts` 一起带上（它手上
本来就有 `TestCase` 对象，只给 id 的 Prompt 对模型毫无信息量）。

### 2.3 角色做成注册表，而不是 `if role == ...`

`prompt_engineer` / `appsec_expert` 各自绑定模板与**允许的补丁类型**。模型越界
（`prompt_engineer` 交回一个 `code_patch`）会抛 `AgentError`——这不是"模型有
创意"，是这次产出下游没法正确处理：模块五的安全回归根本不会被触发。

`20` 之后若出现第三种优化场景（例如"精简 Token 占用"），新增一个 `.jinja` +
一次 `register_role()` 即可，不改 `service.py`。

### 2.4 补丁应用：不信任模型写的行号，但严格校验内容

模型写 diff 时行号常常差一两行。`apply_unified_diff()` 因此**只把 `@@` 的行号当
定位提示**，真正的定位是拿 hunk 的"旧内容块"（上下文行 + 被删除行）去原文里找：
找不到报 `PatchApplyError`，找到多处取离声明行号最近的那处。

这是容错而不是放宽——旧内容块必须逐字符匹配，只是允许整体位移。另外，diff 里
没有任何 `@@ hunk` 会直接被拒（模型输出整篇重写时的典型表现）。

三类补丁的落点不同，其中只有代码补丁需要磁盘：

| patch_type | 作用对象 | 工作副本 |
|---|---|---|
| `DESCRIPTION_PATCH` | `skill.description`（diff 的原文就是这段文本） | 否 |
| `RIGID_CONSTRAINT` | `skill.body_markdown` | 否 |
| `CODE_PATCH` | `scripts/` 下的脚本 | **是** |

代码补丁把整个目录复制到临时目录再改，**原仓库只读**；工作副本的 `SKILL.md` 会
同步内存中最新的 description/body，否则回归跑的是一份过期的 SKILL.md。副本用
`.skilleval-working-copy` 标记识别，同一条闭环复用同一份、`cleanup_working_copy()`
只删带标记的目录（传进真实仓库路径时什么都不做）。

`target_path` 是模型输出的字符串，按不可信输入处理：`../../etc/passwd` 这类路径
穿越会被拒——不能因为"是我们自己的模型写的"就放行。

### 2.5 达到重试上限：挂起，不是判负

架构文档要求"安全挂起状态机"。自动优化解决不了的问题，往往正是最需要人看一眼的
问题；直接判负会把它变成一条 CI 红灯，而人看不到候选补丁长什么样。

实现上做了三件事：写 `human_approvals` 的 waiting 记录（`22` 的工作台据此列出待
办）、打 `optimizer_max_retries_exceeded` 错误日志（含候选 patch_id，告警卡片接
这条）、`suspend_and_wait()` 挂起。

并且给 `22` 定死了 resume payload 的形状：`"adopt"` / `{"decision": "adopt"}` /
`{"adopt_patch": true}` 表示采纳当前候选补丁，其余一律放弃。**默认放弃**是刻意的
——一个没被明确批准的补丁，不该因为 payload 形状没对上就被当成批准。

### 2.6 "过度杀伤力"风险的两道关卡

架构文档点名：自动生成的安全约束容易误伤正常功能（为了防路径穿越把路径写死）。

1. **Prompt 层**：`appsec_patch.jinja` 要求模型必填 `functional_risk`，明确写出这条
   改动是否可能误伤正常功能路径、什么情况下会误伤。该内容会被拼进
   `Patch.rationale` 的「功能误伤评估」段落，跟着补丁一路走到人工审查卡片上——
   不能只留在模型的中间输出里。
2. **回归层**：`15` 的 `retest_fn` 第二段强制跑全量功能回归。

模板还内置了安全编码规范的 few-shot（`subprocess` 不过 shell / `shlex.quote()`、
Bash 变量加引号、路径 `resolve()` 后校验 `is_relative_to`），并明确要求"能用代码
修的优先用代码修"：刚性约束依赖模型每次都遵守，代码校验不依赖任何人自觉。

---

## 3. 与设计文档的差异 / 必要补充

| 位置 | 差异 | 原因 |
|---|---|---|
| `FailureContext` 增加 4 个字段 | 文档只列了 4 个 | `failed_case_prompts`（只给 id 的 Prompt 没信息量）、`triggered_by_finding_id`、`target_path`、`extra_instructions`。全部有默认值，文档里的原始构造方式仍然合法 |
| `PatchApplicationResult` 增加 `detail` | 文档未列 | 回归失败原因要直接出现在人工审批卡片上，不能只写 `False` |
| `pipeline_state_repo.increment_retry()` 落在新表 `node_retry_counts` | 文档只说"Repository 的一次追加方法" | 闭环重试发生在**节点内部**，此时节点的状态更新还没被 LangGraph 合并回图状态。写独立小表既能让循环中途崩溃后的重启看到真实次数，也能让审批工作台查到"它试了几次" |
| `OptimizationLoop.run()` 增加 `thread_id` 参数 | 文档未提 | `human_approvals` 需要 thread_id 才能唤醒；默认取 `run_id`，分离部署时可覆盖 |
| 挂起后返回值 | 文档骨架恒返回 `None` | 文档第 8 节要求 `22` 能"采纳当前候选补丁"，于是解析 resume payload：采纳时返回该 `Patch`。放弃仍返回 `None` |
| 角色注册表 | 文档是 role 字符串 + 两套模板 | 见 2.3，扩展新角色不必改本体 |
| 新增 `LLMSettings.optimizer_model` | 文档未指定模型 | 补丁会进人工审查、可能合进真实仓库，质量优先，默认与 `judge_model` 同档而不是走廉价的 mini 档 |
| `PatchType` 定义在 `state/enums.py` | 文档写在 `state/patch.py` | 项目约定全局枚举只有一个落点；`state/patch.py` 原样再导出，文档里的 import 路径照常可用 |

---

## 4. 已知留白（刻意，非偏差）

- **`retest_fn` 没有任何内置实现**：这是抽象点本身，由 `11`/`15` 各自提供。
- **补丁转真实 commit / PR**：本层只做"评测沙箱内的临时应用与验证"，不碰代码
  仓库；转 PR 是 `24` 的职责（`patches` 表的 `target_path` + `diff` 可直接
  `git apply`）。
- **模型咒语的静态拦截器**（架构文档模块九"模型怪癖剥离"）：`description_patch.jinja`
  已在 Prompt 层禁止，自动化拦截由 `19` 挂在 `propose_patch()` 返回值上，不需要
  改本层。
- **编译期 `interrupt_before` 列表**：当前走动态 `suspend_and_wait()`，`24` 若需要
  静态中断再把节点名加进 `compile()`。

---

## 5. 如何验证

```bash
pytest tests/skill_evaluate/test_optimizer.py -q
```

27 项覆盖：

- **训练集约束**：验证集用例被拒、`ConsensusResult` 被展开、失败用例原文进上下文、
  未知角色 fail fast。
- **unified diff**：增删改、行号错但上下文对仍能应用、上下文对不上被拒、整篇重写
  （无 hunk）被拒、文件头被忽略。
- **补丁应用**：description 只改 description 且不就地修改原对象、正文补丁重算
  行数、description 被删空被拒、基线版本过期被拒、代码补丁在临时副本上生效且
  原仓库不被改动 + SKILL.md 同步 + 清理、清理不误删真实仓库、路径穿越被拒。
- **OptimizerAgent**：两套角色各自出对应类型的补丁、功能误伤评估进 rationale、
  Prompt 里带失败证据与 diff 约束、角色越界被拒、appsec 模板含安全编码 few-shot。
- **闭环**：一次过 / 第二轮基于第一轮产物 / 补丁应用失败算一次失败尝试且留痕 /
  超限挂起并写审批记录 / 人工采纳返回候选补丁 / 无法识别的 payload 默认放弃 /
  模块五"安全 + 功能回归"双段 `retest_fn` 的形状。

全量基线：`ruff check src tests` / `mypy src`（86 files）/ `pytest`（145 passed）
均通过。

---

## 6. 待接入 / 下一步

- `docs/dev/interfaces/09_optimizer_retest_and_patch.md` —— 给 `11`/`15`/`22`/`24`
  的接入清单：三步接入一个闭环、训练集约束、两套角色各自要写什么 `retest_fn`、
  工作副本的生命周期、resume payload 的形状约定、补丁转 PR 需要的字段。
- 迁移：`alembic upgrade head` 应用 `0004_optimizer_patch_tables`。
- 下一份：`docs/dev/10 Validator Agent 与动态断言 Git 工具箱`（第 1 层最后一份
  公共智能体文档）。
