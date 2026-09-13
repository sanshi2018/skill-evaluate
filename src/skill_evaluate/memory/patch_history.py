"""Optimizer 修复经验库（docs/dev/23 第 3.4、4 节）。

每次 `OptimizationLoop.run()` 结束时——**无论成败**——把本轮每一次尝试的
`(失败摘要, 补丁, 是否通过回归)` 归档进 `collection="optimizer_patch_history"`；
`OptimizerAgent.propose_patch()` 出补丁前按失败摘要检索相似经验作为 few-shot。

## 为什么失败尝试也要存

与 `rag_archive.py` "只存完全通过的范本" 恰好相反：那里检索的目的是"提供范本"，劣质样本
会污染输出；这里检索的目的是"提供经验"，"某种改法对某类失败无效"本身就是经验。Prompt 里
按 `passed` 分成"过往成功修复"与"过往失败尝试（避免重蹈覆辙）"两段展示。

## 与文档 23 正文的偏差：归档每一次尝试，而不只是"最终 Patch"

正文写"归档 (FailureContext摘要, 最终Patch, 是否通过回归)"。实现把一轮闭环里的**每一次**
尝试都归档（包括 diff 对不上原文的 `apply_failed`）：闭环第 1、2 轮失败、第 3 轮成功时，只存
第 3 轮会丢掉"前两种改法没用"这条最有价值的负面经验。doc_id 为 `patch_id`，重复归档即 upsert。

## 检索文本 = 失败摘要

入库文档的 `text` 只放失败摘要，diff / rationale 放元数据：检索的问题是"以前有没有遇到过类似
的失败"，把 diff 拼进检索文本会让相似度被"补丁长得像不像"主导。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from skill_evaluate.config import MemorySettings, get_settings
from skill_evaluate.logging import get_logger
from skill_evaluate.memory.hybrid_search import HybridSearchService, get_default_hybrid_search
from skill_evaluate.state.memory import MemoryCollection, PatchExperience, SearchDocument
from skill_evaluate.state.patch import Patch

if TYPE_CHECKING:
    # 仅类型引用：`agents/optimizer/service.py` 反过来依赖本模块，运行期导入会形成循环。
    from skill_evaluate.agents.optimizer.schema import FailureContext

logger = get_logger(component="patch_history")

OUTCOME_REGRESSION_PASSED = "regression_passed"
OUTCOME_REGRESSION_FAILED = "regression_failed"
OUTCOME_APPLY_FAILED = "apply_failed"

# 失败摘要里每类证据最多列几条、每条截断多长。摘要既是检索 query 也是入库文本，
# 太长会让 embedding 被少数超长证据（如上下文洪泛类对抗题）主导。
_MAX_EVIDENCE_ITEMS = 5
_MAX_EVIDENCE_CHARS = 300
# 入库时 diff 的截断上限（渲染进 Prompt 时还会按 `optimizer_patch_diff_max_chars` 再截一次）。
_MAX_STORED_DIFF_CHARS = 8000


@dataclass(frozen=True, slots=True)
class PatchAttempt:
    """闭环中一次尝试的结局（由 `OptimizationLoop` 在每轮结束时记录）。"""

    patch: Patch
    outcome: str  # OUTCOME_* 常量
    detail: str = ""
    attempt: int = 0

    @property
    def passed(self) -> bool:
        return self.outcome == OUTCOME_REGRESSION_PASSED


def summarize_failure(ctx: FailureContext) -> str:
    """把 `FailureContext` 压成一段检索用的失败摘要（文档 23 `_summarize_failure`）。

    只保留"是什么类型的失败"相关的信号：角色、补丁目标、description、失败用例原文、裁判理由、
    红队证据。**不放 SKILL.md 正文全文**：正文在同一个 Skill 的所有失败里都一样，拼进去会让
    同一 Skill 的任意两次失败看起来都高度相似，而不同 Skill 的同类失败反而检索不到。
    """
    lines = [
        f"角色：{ctx.role}",
        f"补丁目标：{ctx.target_path}",
        f"Skill description：{_clip(ctx.skill.description)}",
    ]
    if ctx.failed_case_prompts:
        lines.append("失败用例：")
        lines.extend(
            f"- {_clip(prompt)}" for prompt in ctx.failed_case_prompts[:_MAX_EVIDENCE_ITEMS]
        )
    reasons = [verdict.reasoning for verdict in ctx.verdicts if verdict.reasoning]
    if reasons:
        lines.append("裁判判定：")
        lines.extend(
            f"- [{verdict.status}] {_clip(verdict.reasoning)}"
            for verdict in ctx.verdicts[:_MAX_EVIDENCE_ITEMS]
            if verdict.reasoning
        )
    if ctx.security_findings:
        lines.append("红队发现：")
        lines.extend(
            f"- {finding.category}（{finding.severity}）：{_clip(finding.evidence)}"
            for finding in ctx.security_findings[:_MAX_EVIDENCE_ITEMS]
        )
    if ctx.extra_instructions:
        lines.append(f"额外要求：{_clip(ctx.extra_instructions)}")
    return "\n".join(lines)


def _clip(text: str, limit: int = _MAX_EVIDENCE_CHARS) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else f"{flat[:limit]}…"


class PatchHistoryMemory:
    """修复经验的归档与检索。"""

    def __init__(
        self,
        *,
        search: HybridSearchService | None = None,
        settings: MemorySettings | None = None,
    ) -> None:
        self._settings = settings or get_settings().memory
        self._search = search or get_default_hybrid_search()

    async def archive_attempts(
        self, run_id: str, ctx: FailureContext, attempts: list[PatchAttempt]
    ) -> int:
        """归档一轮闭环的全部尝试，返回写入条数。"""
        if not attempts:
            return 0
        summary = summarize_failure(ctx)
        documents = [
            SearchDocument(
                doc_id=attempt.patch.patch_id,
                collection=MemoryCollection.OPTIMIZER_PATCH_HISTORY.value,
                text=summary,
                metadata={
                    "role": ctx.role,
                    "skill_id": ctx.skill.skill_id,
                    "skill_version_ref": attempt.patch.base_skill_version_ref,
                    "run_id": run_id,
                    "attempt": attempt.attempt,
                    "patch_type": attempt.patch.patch_type.value,
                    "target_path": attempt.patch.target_path,
                    "diff": attempt.patch.diff[:_MAX_STORED_DIFF_CHARS],
                    "rationale": attempt.patch.rationale,
                    "passed": attempt.passed,
                    "outcome": attempt.outcome,
                    "detail": attempt.detail[: _MAX_EVIDENCE_CHARS * 2],
                },
            )
            for attempt in attempts
        ]
        await self._search.index_many(documents)
        logger.info(
            "optimizer_patch_history_archived",
            run_id=run_id,
            role=ctx.role,
            skill_id=ctx.skill.skill_id,
            attempts=len(attempts),
            passed=[attempt.passed for attempt in attempts],
        )
        return len(documents)

    async def retrieve_similar(
        self, ctx: FailureContext, top_k: int | None = None
    ) -> list[PatchExperience]:
        """按失败摘要检索**同角色**的相似经验。

        按角色过滤：AppSec 的代码补丁经验对"重写 description"毫无参考价值，反之亦然；混在一起
        检索时，Reranker 会被"都是失败"这一表面相似性误导。
        """
        limit = self._settings.optimizer_patch_top_k if top_k is None else top_k
        if limit <= 0:
            return []
        hits = await self._search.search(
            summarize_failure(ctx),
            MemoryCollection.OPTIMIZER_PATCH_HISTORY,
            top_k=limit,
            metadata_filter={"role": ctx.role},
        )
        max_diff = self._settings.optimizer_patch_diff_max_chars
        experiences = []
        for hit in hits:
            meta = hit.metadata
            diff = str(meta.get("diff", ""))
            experiences.append(
                PatchExperience(
                    patch_id=hit.doc_id,
                    role=str(meta.get("role", ctx.role)),
                    skill_id=str(meta.get("skill_id", "")),
                    patch_type=str(meta.get("patch_type", "")),
                    target_path=str(meta.get("target_path", "")),
                    failure_summary=hit.text,
                    diff=diff if len(diff) <= max_diff else f"{diff[:max_diff]}\n…（已截断）",
                    rationale=str(meta.get("rationale", "")),
                    passed=bool(meta.get("passed", False)),
                    outcome=str(meta.get("outcome", "")),
                    detail=str(meta.get("detail", "")),
                    score=hit.score,
                )
            )
        return experiences


@lru_cache
def get_default_patch_history() -> PatchHistoryMemory:
    return PatchHistoryMemory()


__all__ = [
    "OUTCOME_APPLY_FAILED",
    "OUTCOME_REGRESSION_FAILED",
    "OUTCOME_REGRESSION_PASSED",
    "PatchAttempt",
    "PatchHistoryMemory",
    "get_default_patch_history",
    "summarize_failure",
]
