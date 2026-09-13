"""语义信息熵与生成坍塌监控（docs/dev/21 第 2 节，替换 docs/dev/06 的占位实现）。

## 判定口径

一批新生成的用例在向量空间里"高度聚集"即判定为坍塌，拒绝激活。聚集有两种形态，本模块
**两种都查**：

1. **贴着历史题**（docs/dev/21 第 2.1 节原文口径）：新题与该 Skill 最近 N 条历史题的平均
   余弦距离低于弹性阈值——模型在反复出同一批题，只是换了几个词。
2. **批内彼此雷同**（实现阶段追加）：新题两两之间的平均余弦距离低于冷启动阈值。原文只做
   新旧对比，而首次生成（`auto_bootstrap`）时历史为空、必然走冷启动放行——18 条几乎一样的
   题会原封不动地成为第一版测试集，并且此后**永远是**后续所有对比的"历史分布"。批内检查不
   依赖历史，正好补上这个口子。

批内检查**固定使用宽容的 initial 阈值**，不随成熟度收紧：同一 Skill、同一批次的题天然围绕
同一个主题，严格阈值在这里没有意义；它只负责抓"几乎一模一样"这种最明显的坍塌。

## 弹性阈值

`current_collapse_threshold()` 按历史样本量在 `[initial, mature]` 之间**线性插值**，而不是两档
if/else——阈值突变会让同一份 Skill 在第 199 次与第 200 次生成之间得出截然不同的结论。

## 写入时序（与 docs/dev/21 正文的差异）

正文在判定**之前**就把新题向量写库。这里改为"先判定、激活成功后再落向量"，原因有二：

- `case_embeddings.case_id` 外键指向 `test_cases`，判定时新题还没落库，先写必然违反外键；
- 更重要的是，被拒绝的废题**不该**进入历史分布——否则下一次生成拿一批废题当参照系，越比越像。

`assess()` 只算不写，`persist()` 由 `TestSuiteService` 在用例落库后调用。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Protocol

from skill_evaluate.agents.embedding import (
    EmbeddingClient,
    OpenRouterEmbeddingClient,
    dot,
    normalize,
)
from skill_evaluate.config import GeneratorTrustSettings, get_settings
from skill_evaluate.logging import get_logger
from skill_evaluate.persistence.repository import CaseEmbeddingRepository, TestCaseRepository
from skill_evaluate.state.enums import CollapseReason
from skill_evaluate.state.test_case import TestCase

logger = get_logger(component="generation_collapse_detector")


@dataclass(slots=True)
class CollapseAssessment:
    """一次反坍塌校验的完整结论。

    比裸 bool 多带的每个字段都有用途：`reason` 区分"冷启动放行"与"真的多样"；两个距离与
    阈值写进坍塌事件表，人工排查"阈值是不是设太严了"时要看；`vectors` 留给 `persist()`，
    避免激活成功后再为同一批文本调一次 embedding。
    """

    passed: bool
    reason: CollapseReason
    threshold: float
    historical_count: int
    new_case_count: int
    skill_id: str = ""
    avg_distance_to_history: float | None = None
    intra_batch_distance: float | None = None
    embedding_model: str = ""
    vectors: dict[str, list[float]] = field(default_factory=dict)  # case_id -> 原始向量


class CollapseDetector(Protocol):
    """`TestSuiteService` 依赖的最小协议；测试注入替身。"""

    async def assess(
        self, new_cases: list[TestCase], *, inherited_case_ids: list[str] | None = None
    ) -> CollapseAssessment: ...

    async def persist(self, assessment: CollapseAssessment) -> None: ...


def current_collapse_threshold(
    historical_count: int, settings: GeneratorTrustSettings | None = None
) -> float:
    """样本量线性插值：从 initial 逐步收紧到 mature（docs/dev/21 第 2.2 节）。

    落实架构文档"冷启动时设定较宽容的阈值，随着数据飞轮的转动逐步收紧标准"：连续插值而不是
    两档切换，避免阈值突变造成的评测结果跳变。签名去掉了正文里的 `skill_id`——阈值只取决于
    样本量，传一个不用的参数只会让人误以为存在按 Skill 定制的阈值。
    """
    cfg = settings or get_settings().generator_trust
    initial = cfg.collapse_distance_threshold_initial
    mature = cfg.collapse_distance_threshold_mature
    progress = min(max(historical_count, 0) / cfg.maturity_sample_count, 1.0)
    return initial + (mature - initial) * progress


def mean_cross_distance(new: list[list[float]], historical: list[list[float]]) -> float:
    """新题 × 历史题的平均余弦距离（输入须已 L2 归一化）。"""
    return statistics.fmean(1.0 - dot(a, b) for a in new for b in historical)


def mean_pairwise_distance(vectors: list[list[float]]) -> float:
    """批内两两（i<j）平均余弦距离（输入须已 L2 归一化）。至少两条。"""
    return statistics.fmean(
        1.0 - dot(vectors[i], vectors[j])
        for i in range(len(vectors))
        for j in range(i + 1, len(vectors))
    )


class GenerationCollapseDetector:
    """基于 `case_embeddings` 历史分布的坍塌检测器。"""

    def __init__(
        self,
        *,
        embedding_client: EmbeddingClient | None = None,
        embedding_repository: CaseEmbeddingRepository | None = None,
        test_case_repository: TestCaseRepository | None = None,
        settings: GeneratorTrustSettings | None = None,
    ) -> None:
        self._settings = settings or get_settings().generator_trust
        # 惰性构造的真实客户端不读 Key，没配 Key 的环境也能构造服务（真正 embed 时才报错）。
        self._embedding_client = embedding_client or OpenRouterEmbeddingClient()
        self._embedding_repo = embedding_repository or CaseEmbeddingRepository()
        self._case_repo = test_case_repository or TestCaseRepository()

    @property
    def embedding_model(self) -> str:
        return self._settings.embedding_model

    async def assess(
        self, new_cases: list[TestCase], *, inherited_case_ids: list[str] | None = None
    ) -> CollapseAssessment:
        cfg = self._settings
        if not new_cases:
            return CollapseAssessment(
                passed=False,
                reason=CollapseReason.EMPTY_BATCH,
                threshold=cfg.collapse_distance_threshold_initial,
                historical_count=0,
                new_case_count=0,
            )
        skill_id = new_cases[0].skill_id

        if not cfg.collapse_check_enabled:
            # 显式关闭时放行，但必须留痕：这批题没有经过反坍塌校验，事后要能查到。
            logger.warning(
                "generation_collapse_check_disabled",
                skill_id=skill_id,
                new_case_count=len(new_cases),
            )
            return CollapseAssessment(
                passed=True,
                reason=CollapseReason.DISABLED,
                threshold=cfg.collapse_distance_threshold_initial,
                historical_count=0,
                new_case_count=len(new_cases),
                skill_id=skill_id,
            )

        # 存量回填：docs/dev/21 之前生成的用例没有向量，不回填的话已有项目会永远停在冷启动。
        # 只回填本次继承的用例（即当前 active 版本里的题），它们正是"历史分布"该代表的东西。
        await self._backfill_history(skill_id, inherited_case_ids or [])

        raw_vectors = await self._embedding_client.embed([case.prompt for case in new_cases])
        vectors = {case.case_id: vec for case, vec in zip(new_cases, raw_vectors, strict=True)}
        new_normalized = [normalize(vec) for vec in raw_vectors]

        new_ids = [case.case_id for case in new_cases]
        historical_count = await self._embedding_repo.count_by_skill(
            skill_id=skill_id, embedding_model=self.embedding_model
        )
        threshold = current_collapse_threshold(historical_count, cfg)
        base = CollapseAssessment(
            passed=True,
            reason=CollapseReason.DIVERSE,
            threshold=threshold,
            historical_count=historical_count,
            new_case_count=len(new_cases),
            skill_id=skill_id,
            embedding_model=self.embedding_model,
            vectors=vectors,
        )

        # ---- 批内雷同检查（不依赖历史，冷启动同样生效） ----
        if len(new_normalized) >= cfg.min_batch_size_for_intra_check:
            base.intra_batch_distance = mean_pairwise_distance(new_normalized)
            if base.intra_batch_distance < cfg.collapse_distance_threshold_initial:
                base.passed = False
                base.reason = CollapseReason.COLLAPSED_INTRA_BATCH
                base.threshold = cfg.collapse_distance_threshold_initial
                self._log_collapse(base)
                return base

        # ---- 新旧分布对比（docs/dev/21 第 2.1 节原文口径） ----
        historical = await self._embedding_repo.get_recent(
            skill_id=skill_id,
            embedding_model=self.embedding_model,
            exclude_case_ids=new_ids,
            limit=cfg.historical_window,
        )
        if len(historical) < cfg.min_historical_samples:
            base.reason = CollapseReason.COLD_START
            logger.info(
                "generation_collapse_cold_start",
                skill_id=skill_id,
                historical_samples=len(historical),
                intra_batch_distance=base.intra_batch_distance,
            )
            return base

        base.avg_distance_to_history = mean_cross_distance(
            new_normalized, [normalize(vec) for vec in historical]
        )
        if base.avg_distance_to_history < threshold:
            base.passed = False
            base.reason = CollapseReason.COLLAPSED_VS_HISTORY
            self._log_collapse(base)
            return base

        logger.info(
            "generation_collapse_check_passed",
            skill_id=skill_id,
            avg_distance_to_history=base.avg_distance_to_history,
            intra_batch_distance=base.intra_batch_distance,
            threshold=threshold,
            historical_count=historical_count,
        )
        return base

    async def persist(self, assessment: CollapseAssessment) -> None:
        """激活成功、用例已落 `test_cases` 之后，把这批向量写进历史分布。"""
        if not assessment.passed or not assessment.vectors:
            return
        await self._embedding_repo.save_many(
            skill_id=assessment.skill_id,
            embedding_model=assessment.embedding_model,
            vectors=assessment.vectors,
        )

    async def _backfill_history(self, skill_id: str, inherited_case_ids: list[str]) -> None:
        if not inherited_case_ids:
            return
        missing = await self._embedding_repo.missing_case_ids(
            inherited_case_ids, embedding_model=self.embedding_model
        )
        if not missing:
            return
        cases = await self._case_repo.list_by_ids(missing)
        # 只回填最近的一个窗口：更早的题不会进入 `get_recent()` 的窗口，算了也白算。
        cases.sort(key=lambda case: case.created_at, reverse=True)
        cases = cases[: self._settings.historical_window]
        if not cases:
            return
        raw = await self._embedding_client.embed([case.prompt for case in cases])
        await self._embedding_repo.save_many(
            skill_id=skill_id,
            embedding_model=self.embedding_model,
            vectors={case.case_id: vec for case, vec in zip(cases, raw, strict=True)},
        )
        logger.info("generation_collapse_history_backfilled", skill_id=skill_id, count=len(cases))

    @staticmethod
    def _log_collapse(assessment: CollapseAssessment) -> None:
        logger.warning(
            "generation_collapse_detected",
            skill_id=assessment.skill_id,
            reason=assessment.reason.value,
            avg_distance_to_history=assessment.avg_distance_to_history,
            intra_batch_distance=assessment.intra_batch_distance,
            threshold=assessment.threshold,
            historical_count=assessment.historical_count,
        )


__all__ = [
    "CollapseAssessment",
    "CollapseDetector",
    "GenerationCollapseDetector",
    "current_collapse_threshold",
    "mean_cross_distance",
    "mean_pairwise_distance",
]
