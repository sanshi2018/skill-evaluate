"""测试集生命周期管理（docs/dev/06 第 4、6、7 节）。

这里落实项目的第一条关键约束：**Generator 只在初始化时生成一次，之后只能手动
强制生成，流水线重复执行默认复用已有测试集**。

三态语义（务必在后续文档中保持一致理解）：

| 模式 | 谁能触发 | 是否调用 LLM |
|---|---|---|
| `REUSE` | 流水线默认路径 | 只在"从来没生成过"时调用一次 |
| `FORCE_REGENERATE` | 只有人（CLI `--force` / CI 显式参数） | 每次都调用 |
| `INCREMENTAL_PATCH` | 模块六/七/十的节点可在流水线内自动调用 | 每次都调用，但只补盲区 |

`INCREMENTAL_PATCH` **不受**"手动强制"约束限制：它是有明确理由（覆盖率盲区）
的定向生成，与"CI 每跑一次就重新出一套题"是两回事。
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from skill_evaluate.agents.generator.agent import GeneratorAgent
from skill_evaluate.agents.generator.collapse_detector import (
    CollapseAssessment,
    CollapseDetector,
    GenerationCollapseDetector,
)
from skill_evaluate.agents.generator.schema import CapabilityFocus, GenerationRequest
from skill_evaluate.config import get_settings
from skill_evaluate.errors import GenerationCollapseError, GenerationError
from skill_evaluate.logging import get_logger
from skill_evaluate.observability.alerts import (
    AlertDispatcher,
    dispatch_alert,
    get_alert_dispatcher,
)
from skill_evaluate.persistence.repository import (
    GenerationCollapseEventRepository,
    TestCaseRepository,
    TestSuiteRepository,
)
from skill_evaluate.state.enums import DatasetSplit, GenerationMode, TestCaseCategory
from skill_evaluate.state.generator_trust import GenerationCollapseEvent
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion

logger = get_logger(component="test_suite_service")

TRAIN_RATIO = 0.6  # 架构文档：60/40 划分训练集/验证集

# 连续坍塌达到上限时发出的告警类型（docs/dev/22 第 8 节按它走 `INJECT_NEW_SEED` 处理路径）。
ALERT_TYPE_GENERATION_COLLAPSE = "generation_collapse_persistent"


@dataclass(slots=True)
class EnsureTestSuiteResult:
    """`ensure_test_suite()` 的返回值。

    比裸返回 `TestSuiteVersion` 多带两个事实，供流水线入口决定怎么记录：
    - `staleness_warning`：SKILL.md 版本已漂移但按约定**没有**自动重新生成；
    - `generated`：本次是否真的调用了 LLM（用于审计"测试集为什么变了"）。
    """

    suite_version: TestSuiteVersion
    staleness_warning: str | None = None
    generated: bool = False


class TestSuiteService:
    """测试集的复用/重生/补盲区入口。

    依赖以构造参数注入，便于 docs/dev/11 等上层节点替换实现、也便于单测不碰库。
    """

    def __init__(
        self,
        *,
        generator: GeneratorAgent | None = None,
        test_suite_repo: TestSuiteRepository | None = None,
        test_case_repo: TestCaseRepository | None = None,
        collapse_detector: CollapseDetector | None = None,
        collapse_event_repo: GenerationCollapseEventRepository | None = None,
        alert_dispatcher: AlertDispatcher | None = None,
    ) -> None:
        self._generator = generator or GeneratorAgent()
        self._suite_repo = test_suite_repo or TestSuiteRepository()
        self._case_repo = test_case_repo or TestCaseRepository()
        # docs/dev/21：反坍塌检测器与坍塌事件表。默认实现复用本服务的用例仓储做存量回填。
        self._collapse_detector: CollapseDetector = collapse_detector or (
            GenerationCollapseDetector(test_case_repository=self._case_repo)
        )
        self._collapse_event_repo = collapse_event_repo or GenerationCollapseEventRepository()
        # None = 每次发送时回落到进程级注册的分发器（docs/dev/22 可以晚于本服务构造再注册）。
        self._alert_dispatcher = alert_dispatcher

    # ------------------------------------------------------------------ #
    # 4.1 REUSE（默认）
    # ------------------------------------------------------------------ #

    async def ensure_test_suite(
        self,
        skill: SkillDefinition,
        *,
        extra_categories: list[TestCaseCategory] | None = None,
        category_counts: dict[TestCaseCategory, int] | None = None,
        extra_triggered_by: str = "dimension_extra_categories",
        background_skills: list[SkillDefinition] | None = None,
    ) -> EnsureTestSuiteResult:
        """流水线默认入口：能复用就复用，从未生成过才首次生成。

        `extra_categories`（docs/dev/13 追加）用于"本维度需要一批**别的维度不需要**
        的用例"这种场景（模块三的渐进式披露动态探查用例就是第一例）。语义仍然是
        **REUSE**：只有当现有 active 用例集里一条这些类别的用例都没有时，才补生成
        这些类别——**且只生成这些类别**，已有的正/反向用例原样继承，`split` 归属不
        被打乱。为什么不让各维度自己去调 `force_regenerate()`：那会把整套题重出一
        遍，"这次改动到底影响了什么"就再也归因不了（docs/dev/06 第 4.1 节）。

        `category_counts` 是逐类别的条数覆盖，透传给 `GenerationRequest`；模块三按
        "每个参考文件各出一条触发探查题"算出条数，只有调用方算得出来。

        `extra_triggered_by`（docs/dev/15 追加）只影响审计字段
        `TestSuiteVersion.generation_mode` 旁边的那条 `triggered_by` 记录，不影响
        任何生成逻辑。加它是因为"这批题是谁让出的"在排查"测试集为什么变了"时是
        主要线索，而所有走 `extra_categories` 的维度共用一个
        `dimension_extra_categories`，等于把线索抹平了（模块五传
        `attacker_bootstrap`）。

        `background_skills`（docs/dev/20 追加）只透传给 `GenerationRequest`，供
        MULTI_SKILL 模板渲染协作对象；不影响复用/补生成的判定口径。
        """
        extras = list(extra_categories or [])
        backgrounds = list(background_skills or [])
        existing = await self._suite_repo.get_active_version(skill.skill_id, skill.version_ref)
        if existing is not None:
            topped_up = await self._ensure_extra_categories(
                skill, existing, extras, category_counts, extra_triggered_by, backgrounds
            )
            if topped_up is not None:
                return EnsureTestSuiteResult(suite_version=topped_up, generated=True)
            logger.info(
                "test_suite_reused",
                skill_id=skill.skill_id,
                suite_version_id=existing.suite_version_id,
            )
            return EnsureTestSuiteResult(suite_version=existing)

        stale = await self._suite_repo.get_active_version(skill.skill_id, None)
        if stale is not None:
            # 关键设计：SKILL.md 变了但没有人手动触发 force_regenerate，不代表要
            # 自动重新生成。自动重生会打破"手动强制"的约束（哪怕理由是版本变
            # 了），而且会让"这次改动到底影响了什么"永远无法对比——旧题换新题，
            # 分数变化归因不了。这里继续复用旧用例集，只发 staleness 告警，交给
            # 人类判断这次改动是否大到需要重新出题。
            warning = (
                f"测试集与当前 SKILL.md 版本不匹配："
                f"用例集绑定 {stale.skill_version_ref!r}，当前 {skill.version_ref!r}。"
                "已按约定继续复用旧用例集（不自动重新生成）。"
                "如需重新出题，请显式执行 `skill-evaluate generate --skill-path <path> --force`。"
            )
            logger.warning(
                "test_suite_stale",
                skill_id=skill.skill_id,
                active_version_ref=stale.skill_version_ref,
                current_ref=skill.version_ref,
            )
            # 版本漂移时仍然要补齐缺失的类别：一份 2024 年生成的用例集里当然不会有
            # docs/dev/13 才引入的探查用例，那不是"漂移"而是"这个维度从没出过题"。
            # 补生成挂在漂移的那一版上，staleness 告警照样带出去。
            topped_up = await self._ensure_extra_categories(
                skill, stale, extras, category_counts, extra_triggered_by, backgrounds
            )
            return EnsureTestSuiteResult(
                suite_version=topped_up or stale,
                staleness_warning=warning,
                generated=topped_up is not None,
            )

        logger.info("test_suite_bootstrap", skill_id=skill.skill_id)
        version = await self._generate_and_activate(
            GenerationRequest(
                skill=skill,
                mode=GenerationMode.REUSE,
                categories=[
                    TestCaseCategory.POSITIVE,
                    TestCaseCategory.NEGATIVE,
                    *extras,
                ],
                category_counts=dict(category_counts or {}),
                triggered_by="auto_bootstrap",
                background_skills=backgrounds,
            )
        )
        return EnsureTestSuiteResult(suite_version=version, generated=True)

    async def _ensure_extra_categories(
        self,
        skill: SkillDefinition,
        current: TestSuiteVersion,
        extra_categories: list[TestCaseCategory],
        category_counts: dict[TestCaseCategory, int] | None,
        triggered_by: str = "dimension_extra_categories",
        background_skills: list[SkillDefinition] | None = None,
    ) -> TestSuiteVersion | None:
        """补齐现有用例集里**一条都没有**的额外类别，返回新版本；无需补齐时返回 None。

        判定口径是"该类别一条都没有"，而不是"条数够不够"：条数够不够是个没有客观
        答案的问题（几条算够？），把它做成自动触发条件，等于给流水线开了一个每次
        运行都可能悄悄再出一批题的口子。要加题请显式走
        `force_regenerate()` / `incremental_patch()`。
        """
        if not extra_categories:
            return None
        present = await self._case_repo.list_by_categories(
            current.suite_version_id, extra_categories
        )
        missing = sorted(
            set(extra_categories) - {case.category for case in present}, key=lambda c: c.value
        )
        if not missing:
            return None

        logger.info(
            "test_suite_extra_categories_generating",
            skill_id=skill.skill_id,
            suite_version_id=current.suite_version_id,
            missing_categories=[c.value for c in missing],
        )
        counts = {c: (category_counts or {}).get(c, 0) for c in missing}
        if all(count <= 0 for count in counts.values()):
            # 调用方算出来"这个 Skill 一条这类题都出不了"（例如它根本没有
            # references/ 目录，就没有渐进式披露可探）。这不是错误，直接返回 None
            # 让调用方按"没有用例"处理，而不是发一次注定出 0 条的 LLM 请求。
            logger.info(
                "test_suite_extra_categories_skipped",
                skill_id=skill.skill_id,
                reason="requested_count_is_zero",
                missing_categories=[c.value for c in missing],
            )
            return None
        return await self._generate_and_activate(
            GenerationRequest(
                skill=skill,
                mode=GenerationMode.INCREMENTAL_PATCH,
                categories=missing,
                # 只生成缺的那些类别：把 positive/negative 归零，避免顺手重出一套
                # 正反向题（那就等于变相的 force_regenerate）。
                positive_count=0,
                negative_count=0,
                category_counts=counts,
                triggered_by=triggered_by,
                background_skills=list(background_skills or []),
            ),
            inherited_case_ids=current.case_ids,
        )

    # ------------------------------------------------------------------ #
    # 4.2 FORCE_REGENERATE
    # ------------------------------------------------------------------ #

    async def force_regenerate(
        self,
        skill: SkillDefinition,
        triggered_by: str = "manual_cli",
        *,
        positive_count: int | None = None,
        negative_count: int | None = None,
    ) -> TestSuiteVersion:
        """全量重新生成。旧版本不删除（`is_active=False`），保留历史供新旧对比。"""
        overrides = {
            key: value
            for key, value in (
                ("positive_count", positive_count),
                ("negative_count", negative_count),
            )
            if value is not None
        }
        return await self._generate_and_activate(
            GenerationRequest(
                skill=skill,
                mode=GenerationMode.FORCE_REGENERATE,
                triggered_by=triggered_by,
                **overrides,
            )
        )

    # ------------------------------------------------------------------ #
    # 4.3 INCREMENTAL_PATCH
    # ------------------------------------------------------------------ #

    async def incremental_patch(
        self,
        skill: SkillDefinition,
        focus: CapabilityFocus,
        triggered_by: str,
        *,
        positive_count: int | None = None,
        negative_count: int | None = None,
    ) -> TestSuiteVersion:
        """针对盲区定向补生成，新老用例合并成新版本。

        新增数量默认按 focus 的内容规模动态决定（不固定 8-10）：每个待覆盖的能力
        /约束/组合各出一条。调用方确有把握时可用 `positive_count`/`negative_count`
        覆盖。

        已有用例的 `split` 归属**不被打乱**——补盲区不应该让已经跑过优化闭环的
        训练/验证集边界发生变化（docs/dev/06 第 6 节）。
        """
        if focus.is_empty:
            raise GenerationError("incremental_patch 需要非空的 CapabilityFocus，否则无盲区可补")

        current = await self._suite_repo.get_active_version(skill.skill_id, None)
        if current is None:
            raise GenerationError(
                f"skill_id={skill.skill_id!r} 还没有任何 active 测试集版本，"
                "无法做增量补齐；请先走 ensure_test_suite() 完成首次生成。"
            )

        positive = (
            positive_count
            if positive_count is not None
            else len(focus.capability_ids) + len(focus.combinatorial_pairs)
        )
        negative = (
            negative_count if negative_count is not None else len(focus.negative_constraint_ids)
        )

        request = GenerationRequest(
            skill=skill,
            mode=GenerationMode.INCREMENTAL_PATCH,
            positive_count=positive,
            negative_count=negative,
            capability_focus=focus,
            triggered_by=triggered_by,
        )
        return await self._generate_and_activate(request, inherited_case_ids=current.case_ids)

    async def incremental_patch_categories(
        self,
        skill: SkillDefinition,
        *,
        categories: list[TestCaseCategory],
        category_counts: dict[TestCaseCategory, int],
        triggered_by: str,
    ) -> TestSuiteVersion:
        """按**类别**定向补生成（docs/dev/15 追加）。

        与 `incremental_patch()` 的分工：那个按**能力盲区**补题（模块六/七/十的
        `CapabilityFocus`），这个按**类别**补题。模块五的"重出一套对抗题"属于后者
        ——它不是某个能力没覆盖到，而是"红队手法更新了，同一批攻击面要重新出题"。

        为什么不复用 `_ensure_extra_categories()`：那个的判定口径是"该类别一条都
        没有才生成"（REUSE 语义），而本方法是显式的重出题，必须每次都生成。两者
        共用一个方法就得加一个 `force` 参数，而那个参数会让 REUSE 路径上多一条
        随时可能被误传的分支——测试集是否重出是本项目最要紧的一条约束。

        已有用例**原样继承**（包括旧的同类别用例），`split` 归属不变：新旧对抗题
        并存，历史结论仍可回查，旧版本只是不再 active。
        """
        current = await self._suite_repo.get_active_version(skill.skill_id, None)
        if current is None:
            raise GenerationError(
                f"skill_id={skill.skill_id!r} 还没有任何 active 测试集版本，"
                "无法按类别补齐；请先走 ensure_test_suite() 完成首次生成。"
            )
        if not categories:
            raise GenerationError("incremental_patch_categories 需要非空的 categories")

        return await self._generate_and_activate(
            GenerationRequest(
                skill=skill,
                mode=GenerationMode.INCREMENTAL_PATCH,
                categories=list(categories),
                # 归零，避免顺手重出一套正/反向题（那就等于变相的 force_regenerate）。
                positive_count=0,
                negative_count=0,
                category_counts=dict(category_counts),
                triggered_by=triggered_by,
            ),
            inherited_case_ids=current.case_ids,
        )

    # ------------------------------------------------------------------ #
    # 生成 + 激活
    # ------------------------------------------------------------------ #

    async def _generate_and_activate(
        self,
        request: GenerationRequest,
        *,
        inherited_case_ids: list[str] | None = None,
    ) -> TestSuiteVersion:
        generator_run_id = str(uuid.uuid4())
        new_cases = await self._generator.generate(request, generator_run_id=generator_run_id)

        # docs/dev/21 第 2 节的反坍塌门禁。未通过时**阻断 activate**，而不是"生成了就用"——
        # 坍塌的用例集比没有用例集更危险，它会给出一个虚高的通过率。
        assessment = await self._collapse_detector.assess(
            new_cases, inherited_case_ids=inherited_case_ids
        )
        if not assessment.passed:
            await self._reject_collapsed_batch(request, generator_run_id, assessment)

        # 新增用例独立做 60/40 划分，不触碰继承来的用例。
        _split_dataset(new_cases, skill_id=request.skill.skill_id)
        await self._case_repo.save_many(new_cases)
        # 向量必须在用例落库**之后**写（外键），且只写通过校验的这批（废题不进历史分布）。
        await self._collapse_detector.persist(assessment)

        version = TestSuiteVersion(
            suite_version_id=str(uuid.uuid4()),
            skill_id=request.skill.skill_id,
            skill_version_ref=request.skill.version_ref,
            generation_mode=request.mode.value,
            case_ids=[*(inherited_case_ids or []), *(c.case_id for c in new_cases)],
            created_at=datetime.now(UTC),
            is_active=True,
        )
        await self._suite_repo.activate_new_version(version)

        logger.info(
            "test_suite_activated",
            skill_id=request.skill.skill_id,
            suite_version_id=version.suite_version_id,
            mode=request.mode.value,
            triggered_by=request.triggered_by,
            new_case_count=len(new_cases),
            total_case_count=len(version.case_ids),
            collapse_check=assessment.reason.value,
        )
        return version

    async def _reject_collapsed_batch(
        self,
        request: GenerationRequest,
        generator_run_id: str,
        assessment: CollapseAssessment,
    ) -> None:
        """记录坍塌事件、必要时呼叫人工，然后抛 `GenerationCollapseError`（永不正常返回）。

        连续坍塌次数 = "自当前 active 版本创建以来的坍塌事件数"：成功激活会刷新 active
        版本的 `created_at`，于是这个数天然就在成功时清零，不需要维护独立计数器。

        告警只在**恰好达到**上限时发一次（`==` 而非 `>=`）：第 4、5 次坍塌时人已经被叫过了，
        重复轰炸只会让告警被静音；此后每次仍然带着 `requires_human_seed=True` 抛出，由
        docs/dev/22 决定是否挂起流水线。
        """
        skill_id = request.skill.skill_id
        await self._collapse_event_repo.record(
            GenerationCollapseEvent(
                event_id=str(uuid.uuid4()),
                skill_id=skill_id,
                generator_run_id=generator_run_id,
                generation_mode=request.mode.value,
                triggered_by=request.triggered_by,
                reason=assessment.reason,
                avg_distance_to_history=assessment.avg_distance_to_history,
                intra_batch_distance=assessment.intra_batch_distance,
                threshold=assessment.threshold,
                historical_count=assessment.historical_count,
                new_case_count=assessment.new_case_count,
                occurred_at=datetime.now(UTC),
            )
        )
        active = await self._suite_repo.get_active_version(skill_id, None)
        consecutive = await self._collapse_event_repo.count_since(
            skill_id=skill_id, since=active.created_at if active is not None else None
        )
        limit = get_settings().generator_trust.max_consecutive_collapses
        requires_human_seed = consecutive >= limit

        if consecutive == limit:
            await dispatch_alert(
                self._alert_dispatcher or get_alert_dispatcher(),
                alert_type=ALERT_TYPE_GENERATION_COLLAPSE,
                # 出题不隶属于某一次流水线运行（CLI 也能触发），用 generator_run_id 作关联键。
                run_id=generator_run_id,
                payload={
                    "skill_id": skill_id,
                    "skill_version_ref": request.skill.version_ref,
                    "consecutive_collapses": consecutive,
                    "max_consecutive_collapses": limit,
                    "last_reason": assessment.reason.value,
                    "avg_distance_to_history": assessment.avg_distance_to_history,
                    "intra_batch_distance": assessment.intra_batch_distance,
                    "threshold": assessment.threshold,
                    "triggered_by": request.triggered_by,
                    "action_required": "请向种子锚点库注入新的真实 Prompt 后再重新生成",
                },
            )

        raise GenerationCollapseError(
            f"本次生成被判定为语义坍塌（skill_id={skill_id}，reason={assessment.reason.value}，"
            f"历史距离={_fmt(assessment.avg_distance_to_history)}，"
            f"批内距离={_fmt(assessment.intra_batch_distance)}，"
            f"阈值={assessment.threshold:.3f}），已阻断激活；连续第 {consecutive} 次坍塌。",
            skill_id=skill_id,
            consecutive_collapses=consecutive,
            requires_human_seed=requires_human_seed,
        )


def _fmt(value: float | None) -> str:
    return "未计算" if value is None else f"{value:.3f}"


# --------------------------------------------------------------------------- #
# 数据集划分
# --------------------------------------------------------------------------- #


def _split_dataset(cases: list[TestCase], *, skill_id: str) -> list[TestCase]:
    """按 60/40 就地打上 TRAIN/VALIDATION 标签。

    shuffle 用 `skill_id` 派生的确定性种子：同一个 Skill 的划分结果可复现，排查
    "为什么这条用例这次进了验证集"时不至于查无实据。

    **验证集不参与优化闭环**是模块一的强约束（防过拟合），但那属于 docs/dev/09
    与 docs/dev/11 的职责——本函数只负责打标签，不做任何过滤假设。

    正/反向分别划分，避免小样本下随机划分出"验证集里一条反向用例都没有"。
    """
    rng = random.Random(f"skill-evaluate:{skill_id}")
    by_category: dict[str, list[TestCase]] = {}
    for case in cases:
        by_category.setdefault(case.category.value, []).append(case)

    for group in by_category.values():
        shuffled = list(group)
        rng.shuffle(shuffled)
        train_count = round(len(shuffled) * TRAIN_RATIO)
        for index, case in enumerate(shuffled):
            case.split = DatasetSplit.TRAIN if index < train_count else DatasetSplit.VALIDATION
    return cases


__all__ = [
    "ALERT_TYPE_GENERATION_COLLAPSE",
    "TRAIN_RATIO",
    "EnsureTestSuiteResult",
    "TestSuiteService",
]
