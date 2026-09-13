"""Pareto 最优补丁裁决：`retest_fn` 的可组合门控（docs/dev/19 第 7 节）。

架构文档模块九："当 Optimizer Agent 提出 SKILL.md 的修改补丁后，Judge Agent 必须审核
该补丁在异构矩阵中的表现"——

- **接受**：补丁在主代理上提升了通过率，**且**在备用代理上没有显著退化；
- **打回（劫持判定）**：主代理通过率飙升、备用代理却崩了 → "代理过拟合"。

这条约束理论上适用于**任何**产出补丁的闭环（模块一 description 优化、模块五 AppSec
修复）。docs/dev/19 的约定是**不回头修改**那两份已确认文档，而是提供装饰器，由各闭环
按需叠加：

```python
retest_fn = with_quirk_stripping_gate(retest_fn, baseline_skill=skill)   # 静态，最便宜，最先跑
retest_fn = with_consensus_gate(retest_fn, train_sample, run_id=run_id,
                                baseline_skill=skill)                    # 动态，最贵，最后跑
patch = await OptimizationLoop().run(run_id, ctx, retest_fn, optimizer)
```

两个装饰器都**只在原判定通过之后**才加码（静态拦截器除外，它在前面短路），因此
叠加它们永远不会让一个原本失败的补丁变成通过——门控只收紧、不放宽。

## 与 docs/dev/19 正文的三处差异

1. **"退化"按相对基线算，不按绝对值算**。正文代码写的是
   `secondary_rate < 1 - tolerance`，但架构文档说的是"备用代理的**降幅**控制在 5% 以内"。
   一份原版就只在备用代理上跑通 60% 的 Skill，按绝对值口径任何补丁都过不了门——
   Optimizer 会永远碰壁，恰好是架构文档警告的"重试耗时大幅上升"。因此传了
   `baseline_skill` 时先在备用代理上测一次原版（每个门控实例只测一次，缓存复用），
   按 `基线通过率 - 候选通过率 > tolerance` 判退化；不传时才退回正文的绝对口径。
2. **抽样用例必须来自训练集**。门控的判定会决定"哪一版补丁被采纳"，这本身就是优化
   信号——拿验证集用例来门控，等于让验证集间接参与了优化（docs/dev/09 第 4 节的
   训练集约束）。传入非训练集用例直接 `ValueError`。
3. **判定经 `JudgeAgent.quantitative_verdict()`**，而不是装饰器里手写 if/else
   （docs/dev/interfaces/08 第 0 节铁律）；"通过"的口径是触发行为是否符合用例类别的
   预期（`executors/comparison.py` 模块头），不是裸 `loaded_skill_md`。

`LoopResult` 只有 `detail` 没有正文里的 `reasoning` 字段：打回原因以固定前缀
`AGENT_OVERFITTING_MARKER` / `MODEL_QUIRK_MARKER` 写进 `detail`，调用方用
`is_agent_overfitting()` / `is_model_quirk_rejection()` 识别，不要自己去匹配字符串。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from skill_evaluate.agents.analyzer.ablation_lexicon import LexiconKind, introduced_hits
from skill_evaluate.agents.judge.rules import register_rule
from skill_evaluate.agents.optimizer.loop import LoopResult, RetestFn
from skill_evaluate.config import get_settings
from skill_evaluate.executors.base import ExecutorBackend
from skill_evaluate.executors.comparison import COMPARABLE_CATEGORIES, run_arm, summarize_arm
from skill_evaluate.executors.registry import get_backend
from skill_evaluate.logging import get_logger
from skill_evaluate.state.enums import DatasetSplit, JudgeVerdictStatus
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase
from skill_evaluate.state.trace import (
    RUN_INDEX_XMODEL_ARM_WIDTH,
    RUN_INDEX_XMODEL_GATE_BASELINE,
    RUN_INDEX_XMODEL_GATE_CANDIDATE,
    ExecutionTrace,
)

logger = get_logger(component="consensus_gate")

# `LoopResult.detail` 的固定前缀。调用方（如未来接入的 appsec_optimizer_loop）按它区分
# "补丁讨好了主代理"与"补丁本身没修好"，前者该让 Optimizer 换一种更通用的表达重写。
AGENT_OVERFITTING_MARKER = "代理过拟合(Agent Overfitting)"
MODEL_QUIRK_MARKER = "模型怪癖剥离(Model-Quirk Stripping)"

RULE_CONSENSUS_GATE = "cross_model_consensus_gate"

# 规则 inputs 的键：只传计数（`quantitative_verdict()` 会把 inputs 的 repr 写进 reasoning）。
KEY_CANDIDATE_PASSED = "candidate_passed"
KEY_CANDIDATE_JUDGED = "candidate_judged"
KEY_BASELINE_PASSED = "baseline_passed"
KEY_BASELINE_JUDGED = "baseline_judged"
KEY_TOLERANCE = "tolerance"

# 静态拦截器**打回**的类别。全大写强调与连串感叹号不在此列：架构文档对它们的要求是
# "自动降级**警告**"，而 AppSec 补丁写一句 "NEVER pass user input to a shell" 是正当的
# 安全约束，因为一个 NEVER 就把安全修复打回，门控会变成所有人都想绕过的东西。
BLOCKING_QUIRK_KINDS = frozenset(
    {
        LexiconKind.INCANTATION,
        LexiconKind.PERSONA_FLATTERY,
        LexiconKind.MODEL_ADDRESS,
        LexiconKind.EMOTIONAL_PRESSURE,
    }
)

# 浮点比较的容差：`0.9 - 0.85` 在 IEEE 754 下是 0.05000000000000004，不加它的话一次
# 刚好卡在容忍度上的降幅会被判成超限。
_EPSILON = 1e-9


@register_rule(RULE_CONSENSUS_GATE)
def _consensus_gate_rule(inputs: dict[str, Any]) -> JudgeVerdictStatus:
    """备用代理上的表现是否"未被严重绑架"。

    - 候选版本一条有效证据都没有 → FAIL：备用代理全挂了不等于补丁泛化良好，门控
      宁可打回让人看（闭环耗尽重试后会挂起到人工审批，人可以明确采纳）；
    - 有基线 → 降幅（基线通过率 - 候选通过率）超过容忍度即 FAIL；
    - 无基线 → 退回 docs/dev/19 正文口径：候选通过率 < 1 - 容忍度即 FAIL。
    """
    judged = int(inputs[KEY_CANDIDATE_JUDGED])
    if judged <= 0:
        return JudgeVerdictStatus.FAIL
    candidate_rate = int(inputs[KEY_CANDIDATE_PASSED]) / judged
    tolerance = float(inputs[KEY_TOLERANCE])

    baseline_judged = inputs.get(KEY_BASELINE_JUDGED)
    if baseline_judged is not None and int(baseline_judged) > 0:
        baseline_rate = int(inputs[KEY_BASELINE_PASSED]) / int(baseline_judged)
        regressed = baseline_rate - candidate_rate > tolerance + _EPSILON
    else:
        regressed = candidate_rate < 1 - tolerance - _EPSILON
    return JudgeVerdictStatus.FAIL if regressed else JudgeVerdictStatus.PASS


def is_agent_overfitting(result: LoopResult) -> bool:
    return not result.passed and result.detail.startswith(AGENT_OVERFITTING_MARKER)


def is_model_quirk_rejection(result: LoopResult) -> bool:
    return not result.passed and result.detail.startswith(MODEL_QUIRK_MARKER)


# --------------------------------------------------------------------------- #
# 静态拦截器：模型怪癖剥离
# --------------------------------------------------------------------------- #


def with_quirk_stripping_gate(
    base_retest_fn: RetestFn, *, baseline_skill: SkillDefinition
) -> RetestFn:
    """补丁若往 description / 正文里**新写入**了咒语式措辞，不跑回归直接打回。

    放在最前面短路：它是纯文本扫描、零成本，而后面的回归要起沙箱。一份带着
    "Think step-by-step like a Hermes model"的补丁没有必要先花一轮沙箱去证明它管用。

    只看**新增**的命中（`introduced_hits()`）：原版里本来就有的措辞不是这次补丁的锅，
    那由模块九的语言坏味道审查报给人。
    """
    baseline_text = f"{baseline_skill.description}\n{baseline_skill.body_markdown}"

    async def quirk_gated_retest_fn(working_skill: SkillDefinition) -> LoopResult:
        working_text = f"{working_skill.description}\n{working_skill.body_markdown}"
        introduced = introduced_hits(baseline_text, working_text)
        blocking = [hit for hit in introduced if hit.kind in BLOCKING_QUIRK_KINDS]
        if blocking:
            quoted = "、".join(repr(hit.text) for hit in blocking[:5])
            logger.warning(
                "consensus_gate_model_quirk_rejected",
                working_skill_version_ref=working_skill.version_ref,
                introduced=[hit.text for hit in blocking],
            )
            return LoopResult(
                passed=False,
                detail=(
                    f"{MODEL_QUIRK_MARKER}：补丁新增了针对特定模型的话术 {quoted}"
                    f"{'等' if len(blocking) > 5 else ''}。指令必须回归任务逻辑本身"
                    "（具体的 API 规范、JSON 模板或边界条件），请换一种不依赖模型措辞偏好的写法。"
                ),
            )
        warned = [hit for hit in introduced if hit.kind not in BLOCKING_QUIRK_KINDS]
        if warned:
            # 音量式强调只告警不打回（见 BLOCKING_QUIRK_KINDS 的说明）。
            logger.info(
                "consensus_gate_emphasis_introduced",
                working_skill_version_ref=working_skill.version_ref,
                introduced=[hit.text for hit in warned],
            )
        return await base_retest_fn(working_skill)

    return quirk_gated_retest_fn


# --------------------------------------------------------------------------- #
# 动态门控：异构共识
# --------------------------------------------------------------------------- #


def with_consensus_gate(
    base_retest_fn: RetestFn,
    sample_cases: Sequence[TestCase],
    *,
    run_id: str,
    baseline_skill: SkillDefinition | None = None,
    tolerance: float | None = None,
    secondary_backend: ExecutorBackend | None = None,
    judge: Any | None = None,
    runs_per_case: int | None = None,
    timeout_s: int | None = None,
    trace_repository: Any | None = None,
    max_concurrent: int | None = None,
) -> RetestFn:
    """包装任意已有的 `retest_fn`：原判定通过后，再要求备用代理上没有显著退化。

    参数：
    - `sample_cases`：门控用的用例，**必须全部来自训练集**，且只取 POSITIVE / NEGATIVE
      （其余类别没有"触发行为是否符合预期"这个口径，会被过滤并记日志）；
    - `baseline_skill`：补丁前的原版 Skill。传了按"相对基线降幅"判，不传按绝对口径判
      （见模块头第 1 条差异）；
    - `secondary_backend` / `judge` / `trace_repository`：依赖注入点，默认分别取
      `CrossModelSettings.secondary_backend` 注册的后端、`JudgeAgent()`、不落库；
    - `tolerance` / `runs_per_case` / `timeout_s`：默认取 `CrossModelSettings`。

    构造期就做参数校验（训练集约束、号段宽度），而不是等闭环跑到第一轮才炸。
    """
    settings = get_settings()
    non_train = [case.case_id for case in sample_cases if case.split is not DatasetSplit.TRAIN]
    if non_train:
        raise ValueError(
            f"共识门控不得使用非训练集用例: {non_train}（门控结果决定补丁取舍，本身就是优化"
            "信号；验证集不参与优化以防止过拟合，见 docs/dev/09 第 4 节）"
        )
    cases = [case for case in sample_cases if case.category in COMPARABLE_CATEGORIES]
    if len(cases) != len(sample_cases):
        logger.warning(
            "consensus_gate_incomparable_cases_dropped",
            dropped=[c.case_id for c in sample_cases if c.category not in COMPARABLE_CATEGORIES],
        )

    cross_model = settings.cross_model
    effective_tolerance = cross_model.consensus_tolerance if tolerance is None else tolerance
    runs = cross_model.runs_per_arm if runs_per_case is None else runs_per_case
    if not 1 <= runs <= RUN_INDEX_XMODEL_ARM_WIDTH:
        raise ValueError(f"runs_per_case 必须在 1..{RUN_INDEX_XMODEL_ARM_WIDTH} 之间，收到 {runs}")
    effective_timeout = cross_model.execution_timeout_s if timeout_s is None else timeout_s
    concurrency = max(
        1, settings.executor.max_concurrent_sandboxes if max_concurrent is None else max_concurrent
    )

    # 懒构造 + 缓存：备用后端、Judge 只在第一次真正需要时创建；基线只测一次。
    resolved: dict[str, Any] = {}
    baseline_cache: dict[str, tuple[int, int]] = {}

    def backend() -> ExecutorBackend:
        if "backend" not in resolved:
            resolved["backend"] = secondary_backend or get_backend(cross_model.secondary_backend)
        return resolved["backend"]  # type: ignore[no-any-return]

    def judge_agent() -> Any:
        if "judge" not in resolved:
            if judge is not None:
                resolved["judge"] = judge
            else:
                from skill_evaluate.agents.judge.service import JudgeAgent  # 延迟导入，避免循环依赖

                resolved["judge"] = JudgeAgent()
        return resolved["judge"]

    async def measure(skill: SkillDefinition, run_index_base: int) -> tuple[int, int]:
        """在备用代理上跑一遍，返回 (符合预期的用例数, 有有效证据的用例数)。"""
        traces_by_case = await run_arm(
            backend(),
            run_id=run_id,
            skill=skill,
            cases=cases,
            run_index_base=run_index_base,
            runs=runs,
            timeout_s=effective_timeout,
            semaphore=asyncio.Semaphore(concurrency),
        )
        await _persist(trace_repository, traces_by_case)
        passed = judged = 0
        for case in cases:
            verdict = summarize_arm(traces_by_case.get(case.case_id, []), case.category)
            if verdict.behaved_as_expected is None:
                continue
            judged += 1
            passed += int(verdict.behaved_as_expected)
        return passed, judged

    async def gated_retest_fn(working_skill: SkillDefinition) -> LoopResult:
        base_result = await base_retest_fn(working_skill)
        if not base_result.passed:
            return base_result
        if not cases:
            # 没有可门控的用例：不拦，但把"门控没生效"写进 detail，让审批卡片上看得见。
            return LoopResult(
                passed=True, detail=f"{base_result.detail}；[共识门控未生效：无可用训练集抽样用例]"
            )

        inputs: dict[str, Any] = {KEY_TOLERANCE: effective_tolerance}
        if baseline_skill is not None:
            if "baseline" not in baseline_cache:
                baseline_cache["baseline"] = await measure(
                    baseline_skill, RUN_INDEX_XMODEL_GATE_BASELINE
                )
            inputs[KEY_BASELINE_PASSED], inputs[KEY_BASELINE_JUDGED] = baseline_cache["baseline"]
        passed, judged = await measure(working_skill, RUN_INDEX_XMODEL_GATE_CANDIDATE)
        inputs[KEY_CANDIDATE_PASSED], inputs[KEY_CANDIDATE_JUDGED] = passed, judged

        verdict = judge_agent().quantitative_verdict(
            subject_id=f"xmodel_gate:{run_id}:{working_skill.version_ref}",
            rule_name=RULE_CONSENSUS_GATE,
            inputs=inputs,
        )
        summary = _summary(inputs, total=len(cases))
        logger.info(
            "consensus_gate_judged",
            run_id=run_id,
            working_skill_version_ref=working_skill.version_ref,
            status=verdict.status.value,
            **{k: v for k, v in inputs.items() if k != KEY_TOLERANCE},
        )
        if verdict.status is JudgeVerdictStatus.FAIL:
            return LoopResult(
                passed=False,
                detail=(
                    f"{AGENT_OVERFITTING_MARKER}：{summary}。补丁在主代理上通过了回归，"
                    "但在异构备用代理上显著退化，疑似讨好了当前执行模型而非改进通用逻辑。"
                    f"（主代理回归结论：{base_result.detail}）"
                ),
            )
        return LoopResult(passed=True, detail=f"{base_result.detail}；[共识门控通过：{summary}]")

    return gated_retest_fn


def _summary(inputs: dict[str, Any], *, total: int) -> str:
    candidate = f"备用代理 {inputs[KEY_CANDIDATE_PASSED]}/{inputs[KEY_CANDIDATE_JUDGED]} 条符合预期"
    invalid = total - int(inputs[KEY_CANDIDATE_JUDGED])
    if invalid:
        candidate += f"（另有 {invalid} 条无有效执行证据）"
    if inputs.get(KEY_BASELINE_JUDGED):
        candidate += f"，补丁前基线 {inputs[KEY_BASELINE_PASSED]}/{inputs[KEY_BASELINE_JUDGED]}"
    return f"{candidate}，容忍度 {float(inputs[KEY_TOLERANCE]):.0%}"


async def _persist(
    trace_repository: Any | None, traces_by_case: dict[str, list[ExecutionTrace]]
) -> None:
    """可选落库。不传仓储时不落：门控可能在单测或没有数据库的调试环境里被直接调用。"""
    if trace_repository is None:
        return
    for traces in traces_by_case.values():
        for trace in traces:
            await trace_repository.save(trace)


__all__ = [
    "AGENT_OVERFITTING_MARKER",
    "BLOCKING_QUIRK_KINDS",
    "MODEL_QUIRK_MARKER",
    "RULE_CONSENSUS_GATE",
    "is_agent_overfitting",
    "is_model_quirk_rejection",
    "with_consensus_gate",
    "with_quirk_stripping_gate",
]
