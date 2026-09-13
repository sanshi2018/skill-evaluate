# 接入文档：Optimizer 的 retest_fn、角色与补丁流转（docs/dev/09 留给后续模块的接口）

> 由谁接入：`11`（训练集重跑闭环）、`15`（安全 + 功能双重回归、AppSec 角色）、
> `22`（人工裁决挂起点）、`24`（补丁转真实 PR、编译期中断列表）。
> 当前状态：闭环编排、角色注册表、unified diff 应用、临时工作副本、超限挂起
> 全部落地，有测试覆盖（`tests/skill_evaluate/test_optimizer.py`）。两套 Prompt
> 模板（`description_patch.jinja` / `appsec_patch.jinja`）已写完措辞，可直接用，
> 实测需要调整时**只改 `.jinja`，不要改 `PatchProposal` 的字段**。

---

## 1. 三步接入一个优化闭环

```python
from skill_evaluate.agents.optimizer import (
    OptimizationLoop, OptimizerAgent, LoopResult, build_failure_context,
)

# 1) 构造失败上下文（唯一合法入口，会强制校验训练集约束）
ctx = build_failure_context(skill, failed_cases, verdicts)

# 2) 定义"怎么算补好了"
async def retest_fn(working_skill: SkillDefinition) -> LoopResult:
    passed = await rerun_training_cases(working_skill)   # 你的实现
    return LoopResult(passed=passed, detail="训练集通过率 8/9")

# 3) 跑闭环
patch = await OptimizationLoop().run(run_id, ctx, retest_fn, OptimizerAgent())
```

返回值：

- `Patch` —— 某一轮补丁通过了 `retest_fn`，可以进入人工审批 / 合并流程；
- `None` —— 耗尽重试次数后挂起，且人工选择了放弃。

`retest_fn` 收到的是**已经打好补丁的临时工作副本**（`working_skill`），不是原始
skill。每一轮都建立在上一轮的产物之上（第 2 轮的补丁打在第 1 轮的结果上），
因为第 1 轮改动往往部分有效；每轮从头重开会让模型反复走同一条死路。

---

## 2. 训练集约束（所有调用方必读）

`build_failure_context()` 会拒绝任何 `split != TRAIN` 的用例：

```
ValueError: Optimizer 不得接收非训练集用例: ['c-2']（架构文档：验证集不参与优化以防止过拟合）
```

**不要**为了绕过它而直接 `FailureContext(...)`——那是 docs/dev/09 第 4 节明确
点名的违规写法。`15` 的对抗用例同样受此约束：喂给 Optimizer 的安全用例必须来自
训练集。

`verdicts` 参数同时接受 `JudgeVerdict` 和 `ConsensusResult`（后者会被展开成三份
verdict），所以你不需要为 ROUTINE / CRITICAL 写两套构造代码。

---

## 3. `11`：description 闭环

- 角色：`ROLE_PROMPT_ENGINEER`（默认），模板 `description_patch.jinja`。
- 允许产出：`description_patch` / `rigid_constraint`。模型越界（例如交回一个
  `code_patch`）会抛 `AgentError`——不是"模型有创意"，是这次产出下游没法正确处理。
- `retest_fn` 的内容：用**训练集**用例重跑 Executor + Judge，按触发率量化规则
  判定（见 `docs/dev/interfaces/08_judge_rules_and_criticality.md` 第 1 节）。
- 补丁通过后什么时候动验证集：验证集只用于**最终评估**，不进闭环。

模板措辞已经写进了这些要求：写触发场景而非功能罗列、把失败用例的真实说法吃
进去、误触发同样是失败、**禁止写模型咒语**（呼应架构文档模块九的"模型怪癖剥离"）。
`19` 若要加静态拦截器（检测"Think step-by-step like a Hermes model"这类咒语），
挂在 `OptimizerAgent.propose_patch()` 的返回值上做一次校验即可，不需要改本层。

> ✅ **`19` 已落地，但挂载点不同**：拦截器实现为 `retest_fn` 装饰器
> `with_quirk_stripping_gate()`（`agents/optimizer/consensus_gate.py`），而不是包装
> `propose_patch()`。原因：在 `propose_patch()` 返回值上校验失败只能抛异常，会让
> `OptimizationLoop` 整体崩掉；作为 `retest_fn` 返回 `LoopResult(passed=False)` 则自然计入一轮
> 失败尝试、进入下一轮，与"补丁没修好"走同一条路径。同文件还提供异构共识门控
> `with_consensus_gate()`，用法见 `docs/dev/interfaces/19_cross_model_generalization.md` 第 4 节。

---

## 4. `15`：安全闭环与强制功能回归

```python
ctx = build_failure_context(
    skill, failed_cases,
    [],                                           # ← 15 传空列表，见下
    role=ROLE_APPSEC_EXPERT,
    triggered_by_finding_id=finding.finding_id,   # 落到 Patch.triggered_by_finding_id
    target_path="scripts/convert.py",             # 代码补丁场景指明目标脚本
    security_findings=train_findings,             # ← 15 落地时追加的字段
)

async def security_retest_fn(working_skill: SkillDefinition) -> LoopResult:
    if not await retest_security_case(working_skill, finding):      # 15 实现
        return LoopResult(passed=False, detail="安全漏洞未修复")
    if not await run_full_functional_regression(working_skill):     # 15 调 13 的能力
        return LoopResult(passed=False, detail="安全补丁破坏了正常业务功能")
    return LoopResult(passed=True, detail="安全修复且功能回归通过")
```

### `15` 落地后追加的字段：`FailureContext.security_findings`

模块五的四条探测支路走的是**确定性规则**而不是 LLM 裁决，此时 `verdicts` 里的
`reasoning` 只是一句"quantitative rule 'security_path_traversal' over inputs=…"，
对修补丁的模型没有任何信息量。真正有用的是 `SecurityFinding.evidence` 里那段具体
证据（"[step:3] read_file input=['/etc/passwd'] exit_code=0"）。

因此 `FailureContext` 追加了 `security_findings: list[SecurityFinding]`（默认空列表，
既有调用方不受影响），`appsec_patch.jinja` 在它非空时渲染一个「红队发现」段落，
按严重级别排优先级。`15` 的实际用法就是上面那段：**`verdicts=[]` + 非空的
`security_findings`**。

`description_patch.jinja` 不渲染这个变量（模板环境是 `StrictUndefined`，只在**用到**
未定义变量时报错，多传一个无害）。

**功能回归是强制的**（架构文档原文）。自动生成的安全约束容易"过度杀伤"——为了
防路径穿越把路径写死，正常的跨目录读取就废了。两道关卡：

1. Prompt 层：`appsec_patch.jinja` 要求模型必填 `functional_risk`（这条约束是否
   可能误伤正常功能路径、什么情况下会误伤）。该内容会被拼进 `Patch.rationale`
   的「功能误伤评估」段落，跟着补丁一路走到人工审查卡片上。
2. 回归层：就是你这个 `retest_fn` 的第二段。

**`15` 已落地，实现可直接复用**：`nodes/security/regression.py::FunctionalRegressionRunner`
把模块一的触发率判定与模块三的 ROI 判定拼成一次回归（两者都通过才算 `passed`）。
它不依赖模块五的任何状态，`19`/`20` 若也需要"拿某个变体 Skill 重跑模块一/三的判定"，
直接用它，不要再造一份。细节见
`docs/dev/interfaces/15_security_red_team.md` 第 6 节。

`appsec_patch.jinja` 内置了安全编码规范的 few-shot：`subprocess` 不过 shell /
`shlex.quote()`、Bash 变量加引号、路径 `resolve()` 后校验 `is_relative_to`，并明确
要求"能用代码修的优先用代码修"（刚性约束依赖模型每次都遵守，代码校验不依赖
任何人自觉）。

---

## 5. 补丁怎么被应用（`apply_patch` 的三条路径）

| patch_type | 作用对象 | 是否产生磁盘工作副本 |
|---|---|---|
| `DESCRIPTION_PATCH` | `skill.description`（diff 的原文就是 description 这一段） | 否 |
| `RIGID_CONSTRAINT` | `skill.body_markdown` | 否 |
| `CODE_PATCH` | `scripts/` 下的脚本 | **是** |

代码补丁会把整个 skill 目录复制到临时目录再改，**原仓库只读**，返回的
`SkillDefinition.root_path` 指向副本、`version_ref` 形如 `<base>+patch:<patch_id>`
（刻意包含 git 非法字符，防止有人把它当 commit sha 去 checkout）。同一条闭环里
后续补丁复用同一份副本。

副本的 `SKILL.md` 会被同步成内存中的最新 description/body——否则代码补丁的工作
副本会带着一份过期的 SKILL.md 去跑回归。

回归跑完、补丁被采纳或放弃后，调用方负责清理：

```python
from skill_evaluate.agents.optimizer import cleanup_working_copy
cleanup_working_copy(working_skill)   # 只删带 .skilleval-working-copy 标记的目录
```

关于 diff 的容错：模型写的 `@@` 行号常常是错的，所以定位靠"旧内容块逐字符匹配"，
行号只作提示。内容对不上会抛 `PatchApplyError`，`OptimizationLoop` 把它记成
`applied=False` 的一次失败尝试（**不重试同一个 patch**）并进入下一轮。

---

## 6. `22`：接住挂起点

耗尽 `max_retries`（默认 3，`SKILLEVAL_OPTIMIZER_MAX_RETRIES`）后：

1. 往 `human_approvals` 写一条 waiting 记录，`wait_key = f"{run_id}:optimizer:{role}"`，
   `thread_id` 默认取 `run_id`（可用 `OptimizationLoop.run(..., thread_id=...)` 覆盖）；
2. 打一条 `optimizer_max_retries_exceeded` 错误日志（含候选 `patch_id`，Discord
   告警卡片接这条）；
3. 调 `suspend_and_wait()` 挂起，等待 `resolve_suspension(wait_key, payload, thread_id)`。

**resume payload 的约定**（本层已实现解析，`22` 按这个形状回传即可）：

| payload | 含义 |
|---|---|
| `"adopt"` / `{"decision": "adopt"}` / `{"adopt_patch": true}` | 采纳当前候选补丁，`run()` 返回该 `Patch` |
| 其余任何值（含 `None`） | 放弃，`run()` 返回 `None`，该 Skill 评测按最终失败处理 |

默认放弃是刻意的：一个没被明确批准的补丁，不该因为 payload 形状没对上就被当成
批准。

审批工作台要展示的信息都在库里：`patches`（diff + rationale + 功能误伤评估）、
`patch_application_results`（每一轮 applied / regression_passed / detail）、
`node_retry_counts`（这个节点试了几次）。

---

## 7. `24`：补丁转正式提交，以及编译期中断

- 本层只做"评测沙箱内的临时应用与验证"，**不碰代码仓库**。把
  `regression_passed=True` 的 `Patch` 变成 git commit / PR（`gh` CLI 或 Git API）
  是 `24` 的活。需要的信息都在 `patches` 表：`target_path` + `diff` 可以直接
  `git apply`。
- 使用 `OptimizationLoop` 的节点若需要**静态**中断而非动态 `interrupt()`，把节点名
  加进 `graph.compile(interrupt_before=[...])`。当前实现走的是动态
  `suspend_and_wait()`，不加也能正常挂起。
- `GraphResumer` 必须已注册（见 `docs/dev/interfaces/04_graph_resumer.md`），否则
  人工批准后的 `resolve_suspension()` 会抛 `ConfigurationError`。

---

## 8. 想加第三种优化角色？

不改 `service.py`，注册一个就行：

```python
from skill_evaluate.agents.optimizer import RoleSpec, register_role
from skill_evaluate.state.enums import PatchType

register_role(RoleSpec(
    role="token_slimmer",
    prompt_path="token_slim.jinja",          # 放在 agents/optimizer/prompts/ 下
    allowed_patch_types=frozenset({PatchType.RIGID_CONSTRAINT}),
    description="模块十：注意力衰减时精简 Token 占用（docs/dev/20）",
))
```

角色重名、模板文件不存在，都在注册期直接报错。

---

## 9. 数据库

新增三张表，随 `0004_optimizer_patch_tables` 迁移落地：`patches`、
`patch_application_results`、`node_retry_counts`。`alembic upgrade head` 即可。

`node_retry_counts` 是 `PipelineState.retry_counts` 的落盘形态，由
`PipelineStateRepository.increment_retry()` 维护——闭环重试发生在**节点内部**，
此时节点的状态更新还没被 LangGraph 合并回图状态，所以计数落在独立小表里。要把
它回写进图状态由节点自己决定（`get_retry_counts(run_id)` 一次取全）。
