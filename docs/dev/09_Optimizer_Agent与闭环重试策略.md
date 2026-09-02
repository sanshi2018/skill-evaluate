# 09 Optimizer Agent 与闭环重试策略

> 状态：**待确认**
> 路线图位置：第 1 层 / 第 4 份
> 依赖：`02`（数据契约，本文档新增 `Patch` 模型）、`04`（`suspend_and_wait`、`interrupt_before` 挂载点、`PipelineState.retry_counts`）、`06`（`BaseLLMAgent`）、`08`（消费 `JudgeVerdict`/`ConsensusResult` 作为失败依据）
> 被依赖：`11`（模块一 description 重写闭环）、`15`（模块五补丁生成 + 强制功能回归）、`22`（人工审批闭环，接住本文档的挂起点）

---

## 1. 本文档目标

架构文档里"失败 → 重写 → 再测试"的闭环出现在多个模块（模块一的 description 优化、模块五的 Prompt/代码补丁），如果每个维度各写一套重试循环，会导致挂起条件、重试计数、补丁记录格式互不一致。本文档提供一个**通用闭环编排器**，各评测维度只需要提供"如何生成补丁"和"如何判定补丁是否生效"两个回调。

## 2. `Patch` 数据模型（对文档 02 的一次追加）

```python
# src/skill_evaluate/state/patch.py
class PatchType(StrEnum):
    DESCRIPTION_PATCH = "description_patch"     # 重写 SKILL.md 的 description 字段（模块一）
    RIGID_CONSTRAINT = "rigid_constraint"        # 在 SKILL.md 正文追加刚性安全约束（模块五 Prompt 加固）
    CODE_PATCH = "code_patch"                     # 修改 scripts/ 下代码（模块五代码防御）

class Patch(BaseModel):
    patch_id: str
    skill_id: str
    base_skill_version_ref: str            # 补丁基于哪个版本生成，防止对已过期版本打补丁
    patch_type: PatchType
    target_path: str                        # "SKILL.md" 或具体脚本相对路径
    diff: str                                # 统一 diff 格式（unified diff），供人工审查与自动应用
    rationale: str                            # Optimizer 生成补丁的理由说明
    triggered_by_finding_id: str | None = None  # 关联 SecurityFinding.finding_id（模块五场景）
    created_at: datetime

class PatchApplicationResult(BaseModel):
    patch_id: str
    applied: bool
    regression_passed: bool | None = None     # None = 尚未跑回归
    working_skill_version_ref: str | None = None  # 应用补丁后的临时工作版本引用
```

新表 `patches` / `patch_application_results`（文档 04 Alembic 追加）。

## 3. Optimizer Agent 接口

```python
# src/skill_evaluate/agents/optimizer/service.py
class FailureContext(BaseModel):
    skill: SkillDefinition
    failed_case_ids: list[str]              # 必须全部来自 TRAIN split，见第 4 节强约束
    verdicts: list[JudgeVerdict]              # 或 ConsensusResult 展开后的 verdicts
    role: str = "prompt_engineer"              # "prompt_engineer" | "appsec_expert"（模块五切换角色）


class OptimizerAgent(BaseLLMAgent):
    async def propose_patch(self, ctx: FailureContext) -> Patch:
        """role=prompt_engineer 时走 description/正文优化 Prompt 模板；
        role=appsec_expert 时走安全加固 Prompt 模板（第6节）。
        两套模板都要求输出 unified diff 格式而非整篇重写，
        便于人工审查阶段（22文档）做最小化 diff 比对，也便于回归失败时精确定位是哪处改动导致的。"""
```

## 4. 训练集约束的强制执行

架构文档明确"失败日志（仅限训练集，验证集不参与优化以防止过拟合）"。本文档在类型层面强制这一约束，而不是靠调用方自觉：

```python
def build_failure_context(skill: SkillDefinition, failed_cases: list[TestCase], verdicts: list[JudgeVerdict]) -> FailureContext:
    non_train = [c for c in failed_cases if c.split != DatasetSplit.TRAIN]
    if non_train:
        raise ValueError(f"Optimizer 不得接收非训练集用例: {[c.case_id for c in non_train]}")
    return FailureContext(skill=skill, failed_case_ids=[c.case_id for c in failed_cases], verdicts=verdicts)
```

`build_failure_context()` 是所有调用方构造 `FailureContext` 的**唯一入口**，直接实例化 `FailureContext(...)` 绕过校验的写法在 code review 阶段应视为违反本文档约定。

## 5. 通用闭环编排器 `OptimizationLoop`

```python
# src/skill_evaluate/agents/optimizer/loop.py
class LoopResult(BaseModel):
    passed: bool
    detail: str


RetestFn = Callable[[SkillDefinition], Awaitable[LoopResult]]


class OptimizationLoop:
    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries

    async def run(
        self, run_id: str, ctx: FailureContext, retest_fn: RetestFn, optimizer: OptimizerAgent,
    ) -> Patch | None:
        working_skill = ctx.skill
        for attempt in range(self.max_retries):
            patch = await optimizer.propose_patch(FailureContext(
                skill=working_skill, failed_case_ids=ctx.failed_case_ids,
                verdicts=ctx.verdicts, role=ctx.role,
            ))
            working_skill = apply_patch(working_skill, patch)   # 第7节
            result = await retest_fn(working_skill)
            await patch_result_repository.save(PatchApplicationResult(
                patch_id=patch.patch_id, applied=True, regression_passed=result.passed,
            ))
            if result.passed:
                return patch
            await pipeline_state_repo.increment_retry(run_id, node_name=f"optimizer:{ctx.role}")

        # 达到最大重试次数：安全挂起，不静默失败也不无限重试
        await suspend_and_wait(
            reason=f"optimizer_max_retries_exceeded:{ctx.role}",
            wait_key=f"{run_id}:optimizer:{ctx.role}",
        )
        return None
```

**关键设计**：
- `retest_fn` 由**调用方**（模块一或模块五节点）提供，`OptimizationLoop` 本身不知道"重新测试"具体意味着什么——模块一的 `retest_fn` 是"用 training set 用例重跑 Executor+Judge"，模块五的 `retest_fn` 是"重跑该安全用例 **且** 触发一次全量功能回归测试"（见第 6 节）。这个回调抽象是本文档最重要的复用点。
- 达到 `max_retries` 后调用 `suspend_and_wait()`（文档 04），**不是**返回失败——架构文档要求"安全挂起状态机"而非直接判负，给人工介入留出空间。`PipelineState.retry_counts`（文档 02 已定义字段）由 `pipeline_state_repo.increment_retry()`（文档 04 Repository 的一次追加方法）维护。

## 6. 模块五专用：AppSec 角色与强制功能回归

模块五的补丁分两类（Prompt 加固 / 代码补丁），且架构文档明确要求"补丁生成后必须强制触发一次全量功能测试回归"。本文档提供角色切换和回归编排的接口骨架，具体回归测试内容（复用模块三的执行效果评测）由文档 15 实现：

```python
# 文档15 中会这样组装（本文档只声明接口形状，不实现具体内容）：
async def security_retest_fn(working_skill: SkillDefinition) -> LoopResult:
    security_ok = await retest_security_case(working_skill, original_finding)   # 15文档实现
    if not security_ok:
        return LoopResult(passed=False, detail="安全漏洞未修复")
    functional_ok = await run_full_functional_regression(working_skill)          # 15文档调用13文档能力
    if not functional_ok:
        return LoopResult(passed=False, detail="安全补丁破坏了正常业务功能")
    return LoopResult(passed=True, detail="安全修复且功能回归通过")
```

`FailureContext.role="appsec_expert"` 时，`OptimizerAgent.propose_patch()` 使用不同的 Prompt 模板（`agents/optimizer/prompts/appsec_patch.jinja`），要求模型：
- Prompt 加固场景：注入"绝对不允许"式刚性约束，但同时要求模型说明"该约束是否可能误伤正常功能路径"（作为 rationale 的一部分，供人工审查参考，缓解架构文档提到的"过度杀伤力"风险）。
- 代码补丁场景：针对 `subprocess` 调用补 `shlex.quote()`、修复未加引号变量等具体模式，Prompt 模板内置这些安全编码规范的 few-shot 示例。

## 7. 补丁应用机制

```python
# src/skill_evaluate/agents/optimizer/patch_applier.py
def apply_patch(skill: SkillDefinition, patch: Patch) -> SkillDefinition:
    """在内存中对 skill.body_markdown 或对应脚本内容应用 unified diff，
    产出一个新的 SkillDefinition（working_skill_version_ref 用 f"{base_ref}+patch:{patch.patch_id}" 标识，
    不是真实的 git commit——补丁在通过回归测试、经人工审批合并前，只存在于评测流水线的临时工作副本中）。
    应用失败（diff 与当前内容不匹配，如并发场景下 base_version 已过期）抛出 PatchApplyError，
    调用方（OptimizationLoop）视为本轮尝试失败，不重试同一 patch，进入下一次 propose_patch。"""
```

`PatchApplyError` 继承文档 01 `SkillEvaluateError`。真正把补丁转换为 git commit / Pull Request 的动作，属于文档 24（CI/CD 落地）的职责——本文档只负责"评测沙箱内的临时应用与验证"，不直接操作代码仓库。

## 8. 待接入文档（本文档留给后续模块的接口清单）

| 预留位置 | 当前状态 | 由哪份文档接入 | 接入方式 |
|---|---|---|---|
| `retest_fn` 的具体实现 | 抽象为 `RetestFn` 类型，无实现 | `11`（training set 重跑）、`15`（安全+功能双重回归） | 各文档在其节点内实现符合签名的函数并传给 `OptimizationLoop.run()` |
| `agents/optimizer/prompts/description_patch.jinja` 等模板内容 | 目录/角色骨架已定 | `11` | 补充具体 Prompt 措辞 |
| `agents/optimizer/prompts/appsec_patch.jinja` | 同上 | `15` | 补充具体 Prompt 措辞 |
| 挂起点接入 `graph.compile(interrupt_before=[...])` | `suspend_and_wait` 已可用，编译期列表未追加 | `24` | 把使用 `OptimizationLoop` 的节点名加入编译期中断列表（如需要静态中断而非仅动态 `interrupt()`） |
| 补丁转正式 PR 的流程 | `apply_patch` 只产出内存工作副本 | `24` | 通过成功回归的 `Patch` 记录，调用 `gh` CLI 或 Git API 生成真实提交与 PR |
| 人工挂起后的解冻/继续 | `suspend_and_wait` 等待外部 resume | `22` | 审查工作台批准后调用 `resolve_suspension` 恢复图执行，可选择"采纳当前候选补丁"或"放弃并标记该 Skill 评测为最终失败" |

---

## 下一步

待你确认本文档后，我将输出 **文档 10：Validator Agent 与动态断言 Git 工具箱**——第 1 层最后一份公共智能体文档，完成后即可进入第 2 层各评测维度的具体实现。
