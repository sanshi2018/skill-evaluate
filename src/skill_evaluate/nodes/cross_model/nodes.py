"""模块九：跨模型泛化与代理绑架防范的节点实现（docs/dev/19）。

```
prepare_cross_model_sample（验证集 20% 确定性抽样）
      ↓
   ┌─────────────────────────┬──────────────────────────────┬──────────────────────────┐
   ↓                         ↓                              ↓
heterogeneous_execution_  parameter_perturbation_       stochastic_ablation_
matrix（主 vs 备用代理）   robustness_probe（T=0 vs 扰动）  testing（原版 vs 剥离咒语）
   └─────────────────────────┴──────────────────────────────┴──────────────────────────┘
                                   ↓
                 linguistic_smell_check（静态，模板 5.6 + 同一份词典）
                                   ↓
                      finalize_dimension_report
```

本维度的核心关切是：`SKILL.md` 的有效性到底建立在"领域专有步骤"上，还是建立在"讨好
当前这一个执行模型的话术"上。三条对照实验分别从三个方向把后者逼出来——换一个模型、
换一组采样参数、删掉情绪化措辞——任何一条让"原本表现正确"变成"表现错误"，都说明
Skill 被绑架在了某种模型特性上。

## 这些节点是证据收集，不是补丁裁决

真正的"接受/拒绝补丁"发生在 `agents/optimizer/consensus_gate.py`（docs/dev/19 第 7 节），
由各优化闭环按需叠加。本维度只评测**当前仓库里这份** Skill，不产生补丁、不进闭环。

## 相对 docs/dev/19 正文的实现修正（照抄正文会踩坑）

1. **"通过"的口径**：正文用裸 `loaded_skill_md`，这只对 POSITIVE 用例成立。验证集里
   还有 NEGATIVE 用例，它们"没加载"才是对的——这里统一比较"触发行为是否符合用例类别
   的预期"（`executors/comparison.py`）。
2. **判定经 Judge**：正文在节点里写 if/else 追加 findings，违反 docs/dev/interfaces/08
   第 0 节铁律；这里注册成三条量化规则（`rules.py`）。
3. **失败态 Trace 不是证据**：沙箱超时/故障返回的 `loaded_skill_md=False` 被正文当成
   "没触发"，会凭空制造代理差异；这里记为"证据不足"并交人工。
4. **抽样可复现**：正文 `random.Random(seed=hash(skill_id))` 在不同进程里种子不同
   （`PYTHONHASHSEED`），且仓储回读顺序不确定；这里先按 case_id 排序再用字符串种子抽样。
5. **号段隔离**：六条执行分支各占一个 run_index 号段（`state/trace.py`），不与模块一/三/五
   以及彼此的 Trace 互相覆盖。
6. **节点只返回增量**：正文 `{**state, ...}` 会让 `executed_trace_ids` 等 add-reducer 字段翻倍。
7. **备用代理不可用时如实报告**：未配置 `llama_control` 时异构矩阵跳过并记
   NEEDS_HUMAN_REVIEW，而不是让整个维度崩掉（本维度非阻断，不值得拖垮整条流水线），
   也不是报 PASS（那等于把维度悄悄关掉）。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Sequence
from typing import Literal, cast

from pydantic import BaseModel, Field

from skill_evaluate.agents.analyzer.ablation_lexicon import (
    LexiconKind,
    ablate,
    ablation_seed,
    format_hits_for_review,
    scan_lexicon,
)
from skill_evaluate.agents.judge.golden_injector import is_golden_subject
from skill_evaluate.errors import PersistenceError, PipelineSuspended
from skill_evaluate.executors.base import ExecutorBackend
from skill_evaluate.executors.comparison import COMPARABLE_CATEGORIES, run_arm, summarize_arm
from skill_evaluate.logging import get_logger
from skill_evaluate.nodes.cross_model import rules
from skill_evaluate.nodes.cross_model.deps import (
    LINGUISTIC_SMELL_CRITICALITY,
    LINGUISTIC_SMELL_TEMPLATE_KEY,
    CrossModelDeps,
)
from skill_evaluate.nodes.cross_model.state import (
    DIMENSION,
    KEY_ABLATION_OUTCOME,
    KEY_HETERO_OUTCOME,
    KEY_LINGUISTIC_OUTCOME,
    KEY_PERTURBATION_OUTCOME,
    KEY_SAMPLE_CASE_IDS,
    KEY_SAMPLE_NOTE,
    NODE_PREFIX,
    CrossModelState,
)
from skill_evaluate.state.enums import DatasetSplit, JudgeVerdictStatus
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase
from skill_evaluate.state.trace import (
    RUN_INDEX_XMODEL_ABLATION_ABLATED,
    RUN_INDEX_XMODEL_ABLATION_ORIGINAL,
    RUN_INDEX_XMODEL_PERTURB_BASELINE,
    RUN_INDEX_XMODEL_PERTURB_VARIANT,
    RUN_INDEX_XMODEL_PRIMARY,
    RUN_INDEX_XMODEL_SECONDARY,
    ExecutionTrace,
)

logger = get_logger(component=DIMENSION)

NODE_NAMES = {
    "prepare_cross_model_sample": f"{NODE_PREFIX}.prepare_cross_model_sample",
    "heterogeneous_execution_matrix": f"{NODE_PREFIX}.heterogeneous_execution_matrix",
    "parameter_perturbation_robustness_probe": (
        f"{NODE_PREFIX}.parameter_perturbation_robustness_probe"
    ),
    "stochastic_ablation_testing": f"{NODE_PREFIX}.stochastic_ablation_testing",
    "linguistic_smell_check": f"{NODE_PREFIX}.linguistic_smell_check",
    "finalize_dimension_report": f"{NODE_PREFIX}.finalize_dimension_report",
}

ENTRY_NODE = NODE_NAMES["prepare_cross_model_sample"]
TERMINAL_NODE = NODE_NAMES["finalize_dimension_report"]
# 三条并行对照实验支路（主图装配时 fan-out / fan-in 用）。
PROBE_NODES: tuple[str, ...] = (
    NODE_NAMES["heterogeneous_execution_matrix"],
    NODE_NAMES["parameter_perturbation_robustness_probe"],
    NODE_NAMES["stochastic_ablation_testing"],
)

# **不阻断合并**（docs/dev/19 第 9 节）：架构文档对本机制的权衡分析是"成本与迭代阻力激增"，
# 建议"权重容忍度"而非绝对红线。采用"高可见度告警 + 不阻断"，把"是否接受一个目前只在
# Hermes 上稳定的 Skill 先上线"交给人决定。团队要改成阻断属于策略调整，改这一个常量即可；
# 刻意不做成配置项——那应当留下代码评审记录，而不是某次 CI 里悄悄翻一个环境变量。
BLOCKING = False

# 抽样种子前缀。与 docs/dev/06 第 6 节 60/40 划分的 `skill-evaluate:{skill_id}` 区分开：
# 共用同一个种子会让"抽样"与"划分"两次随机过程相关，抽到的样本带上划分的偏差。
SAMPLE_SEED_PREFIX = "skill-evaluate:cross-model-sample"

# 报告里每条语言坏味道结论摘录的长度（完整 reasoning 已随 JudgeVerdict 落库）。
REASONING_EXCERPT_CHARS = 200
# 报告里列出的被剥离措辞的条数上限。
ABLATION_PREVIEW_LIMIT = 5

ProbeStatus = Literal["completed", "not_applicable", "skipped"]

# 一条对照臂：(执行后端, 用哪份 Skill, run_index 号段起点, 采样参数覆盖)。
type _Arm = tuple[ExecutorBackend, SkillDefinition, int, dict[str, float] | None]


class ProbeOutcome(BaseModel):
    """一条对照实验支路的结论摘要（进图状态用）。

    `status` 三态的区别决定了维度级状态：

    - `completed`：实验跑完了，结论在各 case 列表里；
    - `not_applicable`：这条实验对这份 Skill 没有意义（例如词典一处都没命中，没有
      咒语可剥离）——这是一个**正常**结论，不需要人看；
    - `skipped`：本该跑但没跑成（抽样为空、备用代理不可用）——必须让人看见，
      维度记 NEEDS_HUMAN_REVIEW。
    """

    probe: str
    status: ProbeStatus
    note: str | None = None
    compared_case_ids: list[str] = Field(default_factory=list)  # 两臂都有有效证据、完成了比较
    diverged_case_ids: list[str] = Field(default_factory=list)  # 规则判 FAIL：暴露了脆弱性
    inconclusive_case_ids: list[str] = Field(default_factory=list)  # 任一臂没有有效执行证据
    # 参照臂自己就没表现对的用例：不计入本维度（那是模块一的问题），列出来供参考。
    reference_failed_case_ids: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    verdict_ids: list[str] = Field(default_factory=list)


class LinguisticOutcome(BaseModel):
    """语言坏味道审查的结论摘要。"""

    verdict_id: str | None = None
    status: JudgeVerdictStatus | None = None
    reasoning_excerpt: str = ""
    lexicon_hit_count: int = 0
    caps_emphasis_count: int = 0
    lexicon_preview: list[str] = Field(default_factory=list)
    skipped_reason: str | None = None  # 非空 = 被黄金基准盲测占用，本次没有针对本 Skill 的结论


def _in_given_order(cases: Sequence[TestCase], case_ids: Sequence[str]) -> list[TestCase]:
    """按给定 id 顺序还原用例顺序（仓储 `IN (...)` 查询不保证顺序，理由同模块一）。"""
    order = {case_id: index for index, case_id in enumerate(case_ids)}
    return sorted(cases, key=lambda case: order.get(case.case_id, len(order)))


def sample_validation_cases(
    cases: Sequence[TestCase], *, skill_id: str, ratio: float
) -> list[TestCase]:
    """从验证集的正/反向用例里确定性抽样（docs/dev/19 第 3 节）。

    纯函数，便于单测与复现：
    - 先按 case_id 排序，消除仓储回读顺序的不确定性；
    - 字符串种子（跨进程稳定），同一 Skill 每次抽到同一批；
    - 至少抽 1 条（验证集非空时）：比例乘出来是 0 就等于把维度悄悄关掉。
    """
    pool = sorted(
        (
            case
            for case in cases
            if case.split is DatasetSplit.VALIDATION and case.category in COMPARABLE_CATEGORIES
        ),
        key=lambda case: case.case_id,
    )
    if not pool:
        return []
    size = min(len(pool), max(1, round(len(pool) * ratio)))
    return random.Random(f"{SAMPLE_SEED_PREFIX}:{skill_id}").sample(pool, size)


class CrossModelPipeline:
    """模块九的六个节点。做成类是为了让依赖注入只发生一次（构造时）。"""

    def __init__(self, deps: CrossModelDeps | None = None) -> None:
        self.deps = deps or CrossModelDeps()
        # 装配期硬校验，理由见 `deps.py`。
        CrossModelDeps.assert_backend_routing()
        self.deps.assert_heterogeneous()

    # ------------------------------------------------------------------ #
    # 1. prepare_cross_model_sample
    # ------------------------------------------------------------------ #

    async def prepare_cross_model_sample(self, state: CrossModelState) -> dict[str, object]:
        """确定性抽取验证集 20% 的正/反向用例。

        用例集取 `active_suite_version_id`（模块一在 Phase A 产出，docs/dev/interfaces/11
        第 4.1 节）；状态里没有时退回按 skill_id 查 active 版本（只读，**不**调
        `ensure_test_suite()`——那可能在"从没生成过"时触发出题）。

        验证集为空时**不**定向补题（docs/dev/06 预留的 `triggered_by="cross_model_sampling"`
        本维度不启用）：一个非阻断维度不该拥有让用例集发生变化的权力，用例集的演进归
        模块六/七/八与人工 `--force`。空抽样如实写进报告。
        """
        run_id = str(state["run_id"])
        skill_id = str(state["skill_id"])
        suite_version_id = state.get("active_suite_version_id")
        if not suite_version_id:
            active = await self.deps.test_suite_repository.get_active_version(skill_id)
            suite_version_id = active.suite_version_id if active else None

        note: str | None = None
        sampled: list[TestCase] = []
        if not suite_version_id:
            note = "没有可用的 active 用例集（模块一尚未产出测试集？），跨模型对照实验未执行。"
        else:
            cases = await self.deps.test_case_repository.list_by_categories(
                suite_version_id, list(COMPARABLE_CATEGORIES)
            )
            sampled = sample_validation_cases(
                cases, skill_id=skill_id, ratio=self.deps.settings().sample_ratio
            )
            if not sampled:
                note = (
                    f"用例集 {suite_version_id} 的验证集中没有正/反向用例，跨模型对照实验未执行。"
                    "请检查测试集生成结果与 60/40 划分。"
                )

        logger.info(
            "cross_model_sample_prepared",
            run_id=run_id,
            node_name=ENTRY_NODE,
            suite_version_id=suite_version_id,
            sampled=len(sampled),
            note=note,
        )
        return {KEY_SAMPLE_CASE_IDS: [c.case_id for c in sampled], KEY_SAMPLE_NOTE: note}

    # ------------------------------------------------------------------ #
    # 2. heterogeneous_execution_matrix
    # ------------------------------------------------------------------ #

    async def heterogeneous_execution_matrix(self, state: CrossModelState) -> dict[str, object]:
        """同一批用例同时交给主代理与异构备用代理执行，标记"主代理对、备用代理错"的用例。

        先做一次 `health_check()`：备用代理是外部可选依赖，不可用时跳过本支路（报告
        记 NEEDS_HUMAN_REVIEW），而不是让 `ExecutorBackendError` 拖垮整条流水线——本维度
        非阻断，为它中断其余九个维度的评测得不偿失。主代理**不**做这个检查：主代理
        不可用时其余维度同样跑不了，那是全局故障，应当照常抛出。
        """
        probe = NODE_NAMES["heterogeneous_execution_matrix"]
        cases = await self._sample_cases(state)
        if not cases:
            return self._skipped(state, KEY_HETERO_OUTCOME, probe)

        secondary = self.deps.secondary()
        if not await secondary.health_check():
            outcome = ProbeOutcome(
                probe=probe,
                status="skipped",
                note=(
                    f"备用代理 {self.deps.secondary_name()} 不可用（health_check=False），异构执行"
                    "矩阵未执行。请配置 SKILLEVAL_EXECUTOR_LLAMA_CONTROL_ENDPOINT 或更换备用后端。"
                ),
            )
            logger.warning("cross_model_secondary_unavailable", run_id=str(state["run_id"]))
            return {KEY_HETERO_OUTCOME: outcome.model_dump()}

        run_id = str(state["run_id"])
        skill = await self._load_skill(state)
        reference, variant = await self._run_pair(
            run_id,
            cases,
            reference=(self.deps.primary(), skill, RUN_INDEX_XMODEL_PRIMARY, None),
            variant=(secondary, skill, RUN_INDEX_XMODEL_SECONDARY, None),
        )
        outcome = await self._compare(
            probe=probe,
            cases=cases,
            reference=reference,
            variant=variant,
            rule_name=rules.RULE_HETERO_CONSISTENCY,
            subject_prefix="xmodel_hetero:",
            finding=lambda case_id: (
                f"[代理差异] {case_id} 在主代理上表现符合预期，但在备用代理 "
                f"{self.deps.secondary_name()} 上不符合，可能存在代理特定的触发依赖"
            ),
        )
        return self._probe_update(KEY_HETERO_OUTCOME, outcome, reference, variant)

    # ------------------------------------------------------------------ #
    # 3. parameter_perturbation_robustness_probe
    # ------------------------------------------------------------------ #

    async def parameter_perturbation_robustness_probe(
        self, state: CrossModelState
    ) -> dict[str, object]:
        """主代理上：贪心解码基线 vs 微小采样扰动。

        `sampling_overrides` 是 docs/dev/03 在 `ExecutionRequest` 上预留的字段，本节点是它
        的首个真实使用方。⚠️ 扰动只在执行模型支持采样参数时才真实发生（新一代 Claude
        模型已移除 temperature/top_p，见 docs/dev/interfaces/06 第 2 节）——报告里会写明本次
        下发的参数，读的人据此判断"没有发现脆弱性"是否可信。
        """
        probe = NODE_NAMES["parameter_perturbation_robustness_probe"]
        cases = await self._sample_cases(state)
        if not cases:
            return self._skipped(state, KEY_PERTURBATION_OUTCOME, probe)

        settings = self.deps.settings()
        run_id = str(state["run_id"])
        skill = await self._load_skill(state)
        primary = self.deps.primary()
        reference, variant = await self._run_pair(
            run_id,
            cases,
            reference=(
                primary,
                skill,
                RUN_INDEX_XMODEL_PERTURB_BASELINE,
                settings.perturbation_baseline_overrides,
            ),
            variant=(
                primary,
                skill,
                RUN_INDEX_XMODEL_PERTURB_VARIANT,
                settings.perturbation_overrides,
            ),
        )
        baseline_desc = _format_overrides(settings.perturbation_baseline_overrides)
        variant_desc = _format_overrides(settings.perturbation_overrides)
        outcome = await self._compare(
            probe=probe,
            cases=cases,
            reference=reference,
            variant=variant,
            rule_name=rules.RULE_PERTURBATION_ROBUSTNESS,
            subject_prefix="xmodel_perturb:",
            finding=lambda case_id: (
                f"[脆弱] {case_id} 在 {baseline_desc} 时表现符合预期，但在 {variant_desc} 时"
                "不符合，指令逻辑可能过拟合了贪心解码路径"
            ),
        )
        outcome.findings.insert(
            0,
            f"参数扰动：基线 {baseline_desc} / 扰动 {variant_desc}"
            "（仅在执行模型支持采样参数时扰动才真实生效）",
        )
        return self._probe_update(KEY_PERTURBATION_OUTCOME, outcome, reference, variant)

    # ------------------------------------------------------------------ #
    # 4. stochastic_ablation_testing
    # ------------------------------------------------------------------ #

    async def stochastic_ablation_testing(self, state: CrossModelState) -> dict[str, object]:
        """原版 SKILL.md vs 按词典随机剥离"咒语"后的版本（docs/dev/19 第 6 节）。

        消融版本一处都没改动时（词典未命中，或概率抽样一处没抽中）直接判
        `not_applicable`、**不起沙箱**：拿两份一模一样的文本做对照只会烧钱，还可能因为
        执行噪声凭空"发现"一次咒语依赖。
        """
        probe = NODE_NAMES["stochastic_ablation_testing"]
        cases = await self._sample_cases(state)
        if not cases:
            return self._skipped(state, KEY_ABLATION_OUTCOME, probe)

        settings = self.deps.settings()
        skill = await self._load_skill(state)
        result = ablate(
            skill.body_markdown,
            ablation_seed(skill.skill_id),
            drop_probability=settings.ablation_drop_probability,
        )
        if not result.changed:
            outcome = ProbeOutcome(
                probe=probe,
                status="not_applicable",
                note=(
                    f"词典在正文中命中 {len(result.hits)} 处、本次抽样剥离 0 处，"
                    "消融版本与原版相同，未执行对照（没有可剥离的咒语式措辞）。"
                ),
            )
            return {KEY_ABLATION_OUTCOME: outcome.model_dump()}

        ablated_skill = skill.model_copy(
            update={
                "body_markdown": result.ablated,
                "line_count": len(result.ablated.splitlines()),
                # 版本号带 `+ablation`（含 git 非法字符，与补丁工作副本同一约定）：
                # 防止有人把它当成仓库里真实存在的版本去 checkout。
                "version_ref": f"{skill.version_ref}+ablation",
            }
        )
        run_id = str(state["run_id"])
        primary = self.deps.primary()
        reference, variant = await self._run_pair(
            run_id,
            cases,
            reference=(primary, skill, RUN_INDEX_XMODEL_ABLATION_ORIGINAL, None),
            variant=(primary, ablated_skill, RUN_INDEX_XMODEL_ABLATION_ABLATED, None),
        )
        outcome = await self._compare(
            probe=probe,
            cases=cases,
            reference=reference,
            variant=variant,
            rule_name=rules.RULE_ABLATION_ROBUSTNESS,
            subject_prefix="xmodel_ablation:",
            finding=lambda case_id: (
                f"[咒语依赖] {case_id} 删除情绪化措辞后即表现不符合预期，Skill 可能过度依赖"
                "模型注意力机制而非真实专有知识"
            ),
        )
        preview = "、".join(repr(hit.text) for hit in result.dropped[:ABLATION_PREVIEW_LIMIT])
        outcome.findings.insert(
            0,
            f"随机消融：词典命中 {len(result.hits)} 处，本次剥离 {len(result.dropped)} 处"
            f"（{preview}{'等' if len(result.dropped) > ABLATION_PREVIEW_LIMIT else ''}）",
        )
        return self._probe_update(KEY_ABLATION_OUTCOME, outcome, reference, variant)

    # ------------------------------------------------------------------ #
    # 5. linguistic_smell_check
    # ------------------------------------------------------------------ #

    async def linguistic_smell_check(self, state: CrossModelState) -> dict[str, object]:
        """语言坏味道审查（docs/dev/07 模板 5.6，经 Judge）。

        词典命中清单作为 `lexicon_hits` 一并交给模板复核——**同一份词典**，静态审查与
        动态消融的判定标准才一致（docs/dev/19 第 8 节）。词典刻意偏向误报，最终定性以
        模型对照原文的复核为准。

        不吞 `JudgeFrozenError`：被冻结的裁判给出的任何结论都不该进报告。
        """
        skill = await self._load_skill(state)
        hits = scan_lexicon(skill.body_markdown)
        result = await self.deps.judge().judgmental_verdict(
            subject_id=f"xmodel_linguistic:{skill.skill_id}",
            template_key=LINGUISTIC_SMELL_TEMPLATE_KEY,
            content={"skill_md": skill.body_markdown, "lexicon_hits": format_hits_for_review(hits)},
            criticality=LINGUISTIC_SMELL_CRITICALITY,
        )
        outcome = self._to_linguistic_outcome(result)
        outcome.lexicon_hit_count = len(hits)
        outcome.caps_emphasis_count = sum(1 for h in hits if h.kind is LexiconKind.CAPS_EMPHASIS)
        outcome.lexicon_preview = [f"第 {h.line_no} 行 {h.text!r}" for h in hits[:10]]
        logger.info(
            "cross_model_linguistic_smell_checked",
            run_id=str(state["run_id"]),
            node_name=NODE_NAMES["linguistic_smell_check"],
            status=outcome.status.value if outcome.status else None,
            lexicon_hits=len(hits),
            skipped=outcome.skipped_reason is not None,
        )
        return {
            "judge_verdict_ids": [outcome.verdict_id] if outcome.verdict_id else [],
            KEY_LINGUISTIC_OUTCOME: outcome.model_dump(),
        }

    @staticmethod
    def _to_linguistic_outcome(result: JudgeVerdict | ConsensusResult) -> LinguisticOutcome:
        """收敛 Judge 的返回值，处理共识未达成与黄金盲测两种特殊情形（同模块二口径）。"""
        if isinstance(result, ConsensusResult) and not result.consensus_reached:
            # 本维度声明 ROUTINE，正常拿不到 ConsensusResult；有人把重要度调成 CRITICAL
            # 时这条路径会活过来。NEEDS_HUMAN_REVIEW 不允许被降级（docs/dev/08 明令禁止）。
            raise PipelineSuspended(
                f"{NODE_NAMES['linguistic_smell_check']}：语言坏味道审查的三副本复核未达成共识"
                f"（subject_id={result.subject_id!r}），需人工仲裁。"
            )
        if is_golden_subject(result.subject_id):
            return LinguisticOutcome(
                skipped_reason="本次请求被黄金基准盲测占用，未产生针对本 Skill 的审查结论"
            )
        if isinstance(result, ConsensusResult):
            verdict = result.verdicts[0] if result.verdicts else None
            return LinguisticOutcome(
                verdict_id=verdict.verdict_id if verdict else None,
                status=result.final_status,
                reasoning_excerpt=(verdict.reasoning if verdict else "")[:REASONING_EXCERPT_CHARS],
            )
        return LinguisticOutcome(
            verdict_id=result.verdict_id,
            status=result.status,
            reasoning_excerpt=result.reasoning[:REASONING_EXCERPT_CHARS],
        )

    # ------------------------------------------------------------------ #
    # 6. finalize_dimension_report
    # ------------------------------------------------------------------ #

    async def finalize_dimension_report(self, state: CrossModelState) -> dict[str, object]:
        """聚合三条对照实验与语言坏味道审查，写进 `dimension_results`（docs/dev/19 第 9 节）。

        | 情形 | status |
        |---|---|
        | 任一对照实验发现脆弱性，或语言坏味道审查 FAIL | FAIL |
        | 抽样为空 / 某条实验被跳过 / 有用例证据不足 / 审查被盲测占用 / 结果缺失 | NEEDS_HUMAN_REVIEW |
        | 其余 | PASS |

        `blocking` 恒为 `BLOCKING`（False）；`score=None`：三条性质不同的实验加一项静态审查，
        硬凑"通过项 / 总项"只会得到一个没含义的数字（与模块二、五同一取舍）。
        """
        run_id = str(state["run_id"])
        findings: list[str] = []
        failed = False
        needs_human = False

        sample_ids = _id_list(state, KEY_SAMPLE_CASE_IDS)
        sample_note = cast("str | None", state.get(KEY_SAMPLE_NOTE))
        if sample_note:
            findings.append(sample_note)
            needs_human = True
        else:
            findings.append(f"从验证集确定性抽样 {len(sample_ids)} 条正/反向用例参与对照实验")

        for key, label in (
            (KEY_HETERO_OUTCOME, "异构执行矩阵"),
            (KEY_PERTURBATION_OUTCOME, "参数扰动"),
            (KEY_ABLATION_OUTCOME, "随机消融"),
        ):
            outcome = _model_from_state(state, key, ProbeOutcome)
            if outcome is None:
                needs_human = True
                findings.append(
                    f"{label}：未取到实验结果，请确认主图状态 schema 包含本维度私有键"
                    f"（{key}），见 docs/dev/interfaces/19 第 2 节。"
                )
                continue
            probe_failed, probe_needs_human, probe_findings = _summarize_probe(label, outcome)
            failed = failed or probe_failed
            needs_human = needs_human or probe_needs_human
            findings.extend(probe_findings)

        linguistic = _model_from_state(state, KEY_LINGUISTIC_OUTCOME, LinguisticOutcome)
        if linguistic is None:
            needs_human = True
            findings.append(f"语言坏味道审查：未取到结果（{KEY_LINGUISTIC_OUTCOME}）。")
        elif linguistic.skipped_reason:
            needs_human = True
            findings.append(f"语言坏味道审查：{linguistic.skipped_reason}")
        else:
            if linguistic.status is JudgeVerdictStatus.FAIL:
                failed = True
                findings.append(f"[语言坏味道] 审查未通过：{linguistic.reasoning_excerpt}")
            elif linguistic.status is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW:
                needs_human = True
                findings.append(f"[语言坏味道] 需人工复核：{linguistic.reasoning_excerpt}")
            if linguistic.caps_emphasis_count:
                # 架构文档："对过度使用全大写强调进行自动降级警告"。只告警，不影响状态：
                # 是否算"过度"由上面的模板判定给出。
                findings.append(
                    f"[降级警告] 正文中有 {linguistic.caps_emphasis_count} 处全大写强调"
                    "（ALWAYS/NEVER 等）；解释原因的指令通常比音量式强调在模型间泛化得更好"
                )

        status = (
            JudgeVerdictStatus.FAIL
            if failed
            else JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
            if needs_human
            else JudgeVerdictStatus.PASS
        )
        await self.deps.reporter().record_dimension_result(
            run_id=run_id,
            dimension=DIMENSION,
            status=status,
            score=None,
            findings=findings,
            blocking=BLOCKING,
        )
        logger.info(
            "cross_model_dimension_recorded",
            run_id=run_id,
            node_name=TERMINAL_NODE,
            status=status.value,
            blocking=BLOCKING,
            findings=len(findings),
        )
        return {}

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    async def _run_pair(
        self,
        run_id: str,
        cases: Sequence[TestCase],
        *,
        reference: _Arm,
        variant: _Arm,
    ) -> tuple[dict[str, list[ExecutionTrace]], dict[str, list[ExecutionTrace]]]:
        """两条臂并发执行（共用一个信号量）并落库 Trace。

        并发跑而不是先后跑：两条臂之间隔得越久，外部环境（模型服务端版本、限流状态）
        漂移的机会越大，对照实验就混进了与被测变量无关的差异。
        """
        settings = self.deps.settings()
        semaphore = self.deps.semaphore()

        async def run(
            arm: _Arm,
        ) -> dict[str, list[ExecutionTrace]]:
            backend, skill, run_index_base, overrides = arm
            return await run_arm(
                backend,
                run_id=run_id,
                skill=skill,
                cases=cases,
                run_index_base=run_index_base,
                runs=settings.runs_per_arm,
                timeout_s=settings.execution_timeout_s,
                semaphore=semaphore,
                sampling_overrides=overrides,
            )

        reference_traces, variant_traces = await asyncio.gather(run(reference), run(variant))
        for traces_by_case in (reference_traces, variant_traces):
            for traces in traces_by_case.values():
                for trace in traces:
                    await self.deps.trace_repository.save(trace)
        return reference_traces, variant_traces

    async def _compare(
        self,
        *,
        probe: str,
        cases: Sequence[TestCase],
        reference: dict[str, list[ExecutionTrace]],
        variant: dict[str, list[ExecutionTrace]],
        rule_name: str,
        subject_prefix: str,
        finding: Callable[[str], str],
    ) -> ProbeOutcome:
        """逐条用例比较两条臂，产出 `ProbeOutcome`。判定一律经 `quantitative_verdict()`。

        只归档 FAIL 判定（与模块一同一口径）：通过判定没人读，失败判定是报告与人工复核
        唯一能回查的证据。`subject_id` 带实验前缀，避免与其他维度按裸 case_id 存的判定混在一起。
        """
        judge = self.deps.judge()
        outcome = ProbeOutcome(probe=probe, status="completed")
        for case in cases:
            ref = summarize_arm(reference.get(case.case_id, []), case.category)
            var = summarize_arm(variant.get(case.case_id, []), case.category)
            if ref.behaved_as_expected is None or var.behaved_as_expected is None:
                outcome.inconclusive_case_ids.append(case.case_id)
                continue
            verdict = judge.quantitative_verdict(
                subject_id=f"{subject_prefix}{case.case_id}",
                rule_name=rule_name,
                inputs=rules.comparison_inputs(ref, var),
            )
            outcome.compared_case_ids.append(case.case_id)
            outcome.verdict_ids.append(verdict.verdict_id)
            if verdict.status is JudgeVerdictStatus.FAIL:
                outcome.diverged_case_ids.append(case.case_id)
                outcome.findings.append(finding(case.case_id))
                await self.deps.judge_repository.save_verdict(verdict)
            elif not ref.behaved_as_expected:
                outcome.reference_failed_case_ids.append(case.case_id)

        logger.info(
            "cross_model_probe_compared",
            probe=probe,
            compared=len(outcome.compared_case_ids),
            diverged=len(outcome.diverged_case_ids),
            inconclusive=len(outcome.inconclusive_case_ids),
            reference_failed=len(outcome.reference_failed_case_ids),
        )
        return outcome

    @staticmethod
    def _probe_update(
        key: str,
        outcome: ProbeOutcome,
        reference: dict[str, list[ExecutionTrace]],
        variant: dict[str, list[ExecutionTrace]],
    ) -> dict[str, object]:
        """支路节点的状态增量：只回本次产生的 trace/verdict id（add reducer）+ 自己的结果键。"""
        trace_ids = [
            trace.trace_id
            for traces_by_case in (reference, variant)
            for traces in traces_by_case.values()
            for trace in traces
        ]
        return {
            "executed_trace_ids": trace_ids,
            "judge_verdict_ids": list(outcome.verdict_ids),
            key: outcome.model_dump(),
        }

    @staticmethod
    def _skipped(state: CrossModelState, key: str, probe: str) -> dict[str, object]:
        note = cast("str | None", state.get(KEY_SAMPLE_NOTE)) or "抽样为空"
        outcome = ProbeOutcome(probe=probe, status="skipped", note=f"未执行：{note}")
        return {key: outcome.model_dump()}

    async def _sample_cases(self, state: CrossModelState) -> list[TestCase]:
        case_ids = _id_list(state, KEY_SAMPLE_CASE_IDS)
        if not case_ids:
            return []
        cases = await self.deps.test_case_repository.list_by_ids(case_ids)
        return _in_given_order(cases, case_ids)

    async def _load_skill(self, state: CrossModelState) -> SkillDefinition:
        """取被测 Skill 的**原版**。

        不用模块一的 `_working_skill`：本维度评测的是仓库里这份 SKILL.md 的泛化性，拿
        一份内存里改过 description 的版本来测，报告就与人能看到的文件对不上了（补丁的
        泛化性由共识门控在闭环内部把关）。
        """
        skill = await self.deps.skill_repository.get(
            str(state["skill_id"]), str(state["skill_version_ref"])
        )
        if skill is None:
            raise PersistenceError(
                f"未找到被测 Skill：skill_id={state['skill_id']!r} "
                f"version_ref={state['skill_version_ref']!r}。"
                "请先经 `ingestion.load_skill()` + `SkillRepository.save()` 入库。"
            )
        return skill


def _summarize_probe(label: str, outcome: ProbeOutcome) -> tuple[bool, bool, list[str]]:
    """把一条支路的结论翻译成 (是否发现脆弱性, 是否需要人看, findings)。"""
    if outcome.status == "skipped":
        return False, True, [f"{label}：{outcome.note}"]
    if outcome.status == "not_applicable":
        return False, False, [f"{label}：{outcome.note}"]

    findings = [
        (
            f"{label}：比较 {len(outcome.compared_case_ids)} 条，发现脆弱性 "
            f"{len(outcome.diverged_case_ids)} 条，证据不足 {len(outcome.inconclusive_case_ids)} 条"
        )
    ]
    findings.extend(outcome.findings)
    needs_human = bool(outcome.inconclusive_case_ids)
    if outcome.inconclusive_case_ids:
        findings.append(
            f"{label}：以下用例至少一条臂没有有效执行证据（沙箱超时/故障），未参与比较，"
            f"请人工确认：{outcome.inconclusive_case_ids}"
        )
    if outcome.reference_failed_case_ids:
        findings.append(
            f"{label}：以下用例在参照臂上就不符合预期（属于触发准确度问题，不计入本维度）："
            f"{outcome.reference_failed_case_ids}"
        )
    return bool(outcome.diverged_case_ids), needs_human, findings


def _format_overrides(overrides: dict[str, float]) -> str:
    return ", ".join(f"{name}={value}" for name, value in sorted(overrides.items())) or "默认参数"


def _id_list(state: CrossModelState, key: str) -> list[str]:
    value = cast("list[str] | None", state.get(key))
    return [str(item) for item in (value or [])]


def _model_from_state[T: BaseModel](state: CrossModelState, key: str, model: type[T]) -> T | None:
    """从图状态取 Pydantic 模型（Checkpoint 反序列化后可能是 dict，统一 model_validate）。"""
    value = state.get(key)
    return None if value is None else model.model_validate(value)


__all__ = [
    "BLOCKING",
    "ENTRY_NODE",
    "NODE_NAMES",
    "PROBE_NODES",
    "TERMINAL_NODE",
    "CrossModelPipeline",
    "LinguisticOutcome",
    "ProbeOutcome",
    "sample_validation_cases",
]
