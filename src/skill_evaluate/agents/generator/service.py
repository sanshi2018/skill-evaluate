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
from skill_evaluate.agents.generator.schema import CapabilityFocus, GenerationRequest
from skill_evaluate.errors import GenerationError
from skill_evaluate.logging import get_logger
from skill_evaluate.persistence.repository import TestCaseRepository, TestSuiteRepository
from skill_evaluate.state.enums import DatasetSplit, GenerationMode
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion

logger = get_logger(component="test_suite_service")

TRAIN_RATIO = 0.6  # 架构文档：60/40 划分训练集/验证集


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
    ) -> None:
        self._generator = generator or GeneratorAgent()
        self._suite_repo = test_suite_repo or TestSuiteRepository()
        self._case_repo = test_case_repo or TestCaseRepository()

    # ------------------------------------------------------------------ #
    # 4.1 REUSE（默认）
    # ------------------------------------------------------------------ #

    async def ensure_test_suite(self, skill: SkillDefinition) -> EnsureTestSuiteResult:
        """流水线默认入口：能复用就复用，从未生成过才首次生成。"""
        existing = await self._suite_repo.get_active_version(skill.skill_id, skill.version_ref)
        if existing is not None:
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
            return EnsureTestSuiteResult(suite_version=stale, staleness_warning=warning)

        logger.info("test_suite_bootstrap", skill_id=skill.skill_id)
        version = await self._generate_and_activate(
            GenerationRequest(skill=skill, mode=GenerationMode.REUSE, triggered_by="auto_bootstrap")
        )
        return EnsureTestSuiteResult(suite_version=version, generated=True)

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

        # docs/dev/06 第 7 节的反坍塌校验挂载点。返回 False 时**阻断 activate**，
        # 而不是"生成了就用"——坍塌的用例集比没有用例集更危险，它会给出一个虚高
        # 的通过率。
        if not await _check_generation_collapse(new_cases):
            raise GenerationError(
                f"本次生成被判定为语义坍塌（skill_id={request.skill.skill_id}），已阻断激活。"
            )

        # 新增用例独立做 60/40 划分，不触碰继承来的用例。
        _split_dataset(new_cases, skill_id=request.skill.skill_id)
        await self._case_repo.save_many(new_cases)

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
        )
        return version


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


async def _check_generation_collapse(new_cases: list[TestCase]) -> bool:
    """反坍塌校验（**占位实现，恒定放行**）。

    docs/dev/21 接入后：计算 `new_cases` 的 prompt 向量与历史用例库的分布距离，
    低于阈值判定为生成坍塌，返回 False 并阻断本次生成结果的 activate。依赖
    docs/dev/23 的 pgvector 检索层。

    保持本函数签名不变，docs/dev/21 只需替换函数体，`_generate_and_activate()`
    的调用结构不需要改动。
    """
    return bool(new_cases)


__all__ = [
    "TRAIN_RATIO",
    "EnsureTestSuiteResult",
    "TestSuiteService",
]
