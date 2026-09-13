"""RAG 增强闭环：成功案例归档与 Generator 冷启动检索（docs/dev/23 第 3.3、4 节）。

## 质量门槛：只归档"完全通过"的 Skill

`archive_successful_run()` 只在**存在阻断维度且全部阻断维度 PASS** 时写入。把评测未通过的
Skill 当作范本存进检索库，后续 Generator 检索到的 few-shot 会一轮比一轮差——数据飞轮
"越转越差"。门槛比 `BenchmarkReport.blocking`（只看 FAIL）更严：`NEEDS_HUMAN_REVIEW` 的阻断
维度同样不算通过，一份"还没人确认过"的结论不配当范本。

## 归档什么（`collection="successful_skill_archive"`）

| kind | doc_id | text | 用途 |
|---|---|---|---|
| `profile` / section=`description` | `<key>:description` | description 原文 | 冷启动检索的主召回面 |
| `profile` / section=`body` | `<key>:chunk:<i>` | 按标题分块的正文（`chunking.py`） | 描述写得短时靠正文章节补召回 |
| `case` | `<key>:case:<case_id>` | 用例 prompt | 被检索命中的范本附带的优质用例 |

`<key>` = `<skill_id>@<skill_version_ref>`。doc_id 全部可由内容推导，同一版本重复归档即 upsert，
主图断点恢复后重跑收尾节点不会产生重复记忆。

## 冷启动检索与种子锚点的分工

两者是不同性质的先验，在 Generator Prompt 里作为**两段独立**的 few-shot 注入、不合并：

- 种子锚点（第 3.2 节）回答"用户会怎么问"——真实用户口吻；
- 历史范本（本模块）回答"什么样的用例设计模式被验证过有效"——结构与覆盖手法。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol

from skill_evaluate.config import MemorySettings, get_settings
from skill_evaluate.logging import get_logger
from skill_evaluate.memory.chunking import chunk_markdown
from skill_evaluate.memory.hybrid_search import HybridSearchService, get_default_hybrid_search
from skill_evaluate.persistence.repository import (
    DimensionResultRepository,
    RunInfo,
    RunRepository,
    SkillRepository,
    TestCaseRepository,
    TestSuiteRepository,
)
from skill_evaluate.state.enums import DatasetSplit, JudgeVerdictStatus, TestCaseCategory
from skill_evaluate.state.memory import (
    ArchivedCase,
    ArchivedExample,
    ArchiveOutcome,
    MemoryCollection,
    SearchDocument,
)
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion

logger = get_logger(component="rag_archive")

KIND_PROFILE = "profile"
KIND_CASE = "case"
SECTION_DESCRIPTION = "description"
SECTION_BODY = "body"

# 归档用例的 split 白名单：cold 区是模块七瘦身降级的用例，已被判定为冗余或低价值，不配当范本。
_ARCHIVABLE_SPLITS = frozenset({DatasetSplit.TRAIN, DatasetSplit.VALIDATION})

# 单份范本最多列出多少条用例文档（冷启动检索时一次取回）。一份用例集通常 18~60 条，
# 这个上限只是防止某份异常巨大的用例集把一次检索拖成全表扫描。
_MAX_CASES_PER_ARCHIVE = 500


def archive_key_for(skill_id: str, skill_version_ref: str) -> str:
    return f"{skill_id}@{skill_version_ref}"


# --------------------------------------------------------------------------- #
# 依赖协议（Postgres 仓储天然满足；单测注入内存替身）
# --------------------------------------------------------------------------- #


class _RunSource(Protocol):
    async def get(self, run_id: str) -> RunInfo | None: ...


class _DimensionSource(Protocol):
    async def list_by_run(self, run_id: str) -> list[dict[str, object]]: ...


class _SkillSource(Protocol):
    async def get(self, skill_id: str, version_ref: str) -> SkillDefinition | None: ...


class _SuiteSource(Protocol):
    async def get_active_version(
        self, skill_id: str, skill_version_ref: str | None = None
    ) -> TestSuiteVersion | None: ...


class _CaseSource(Protocol):
    async def list_by_categories(
        self, suite_version_id: str, categories: list[TestCaseCategory]
    ) -> list[TestCase]: ...


class RagArchive:
    """成功范本的归档与冷启动检索。"""

    def __init__(
        self,
        *,
        search: HybridSearchService | None = None,
        run_repository: _RunSource | None = None,
        dimension_repository: _DimensionSource | None = None,
        skill_repository: _SkillSource | None = None,
        suite_repository: _SuiteSource | None = None,
        case_repository: _CaseSource | None = None,
        settings: MemorySettings | None = None,
    ) -> None:
        self._settings = settings or get_settings().memory
        self._search = search or get_default_hybrid_search()
        self._runs = run_repository or RunRepository()
        self._dimensions = dimension_repository or DimensionResultRepository()
        self._skills = skill_repository or SkillRepository()
        self._suites = suite_repository or TestSuiteRepository()
        self._cases = case_repository or TestCaseRepository()

    # ------------------------------------------------------------------ #
    # 归档（文档 24 主图收尾节点调用）
    # ------------------------------------------------------------------ #

    async def archive_successful_run(self, run_id: str) -> ArchiveOutcome:
        """全部阻断维度 PASS 时归档该次评测的 Skill 画像与 TRAIN+VALIDATION 用例。

        不满足门槛时返回 `archived=False` 与原因，**不抛异常**：没归档是正常分支，不该让主图
        收尾节点失败。存储/embedding 故障则原样上抛——那是基础设施问题，调用方（主图）决定是
        否吞掉，本层不替它假装"没什么可归档的"。
        """
        run = await self._runs.get(run_id)
        if run is None:
            return self._skip(run_id, "run_not_found")

        dimensions = await self._dimensions.list_by_run(run_id)
        blocking = [d for d in dimensions if d.get("blocking")]
        if not blocking:
            # 没有任何阻断维度的结论，就无从证明"完全通过"——宁可不归档。
            return self._skip(run_id, "no_blocking_dimensions")
        failed = sorted(
            str(d.get("dimension"))
            for d in blocking
            if str(d.get("status")) != JudgeVerdictStatus.PASS.value
        )
        if failed:
            return self._skip(run_id, "blocking_dimensions_not_passed", failed_dimensions=failed)

        skill = await self._skills.get(run["skill_id"], run["skill_version_ref"])
        if skill is None:
            return self._skip(run_id, "skill_not_found")

        suite_version_id = run["suite_version_id"]
        if suite_version_id is None:
            active = await self._suites.get_active_version(skill.skill_id, skill.version_ref)
            suite_version_id = active.suite_version_id if active else None
        if suite_version_id is None:
            return self._skip(run_id, "no_suite_version")

        cases = await self._cases.list_by_categories(suite_version_id, list(TestCaseCategory))
        documents = self.build_archive_documents(
            skill, cases, run_id=run_id, suite_version_id=suite_version_id
        )
        await self._search.index_many(documents)

        key = archive_key_for(skill.skill_id, skill.version_ref)
        archived_cases = sum(1 for doc in documents if doc.metadata.get("kind") == KIND_CASE)
        logger.info(
            "rag_run_archived",
            run_id=run_id,
            archive_key=key,
            documents=len(documents),
            cases=archived_cases,
        )
        return ArchiveOutcome(
            run_id=run_id, archived=True, archive_key=key, document_count=len(documents)
        )

    def build_archive_documents(
        self,
        skill: SkillDefinition,
        cases: list[TestCase],
        *,
        run_id: str,
        suite_version_id: str,
    ) -> list[SearchDocument]:
        """把一份通过评测的 Skill 转成归档文档（纯函数，便于单测覆盖分块与过滤口径）。"""
        key = archive_key_for(skill.skill_id, skill.version_ref)
        base = {
            "archive_key": key,
            "skill_id": skill.skill_id,
            "skill_version_ref": skill.version_ref,
            "run_id": run_id,
            "suite_version_id": suite_version_id,
        }
        collection = MemoryCollection.SUCCESSFUL_SKILL_ARCHIVE.value
        documents = [
            SearchDocument(
                doc_id=f"{key}:description",
                collection=collection,
                text=skill.description,
                metadata={**base, "kind": KIND_PROFILE, "section": SECTION_DESCRIPTION},
            )
        ]
        for chunk in chunk_markdown(
            skill.body_markdown, max_chunk_tokens=self._settings.chunk_max_tokens
        ):
            documents.append(
                SearchDocument(
                    doc_id=f"{key}:chunk:{chunk.index}",
                    collection=collection,
                    # 标题路径前置：三级标题"错误处理"脱离所属二级标题后，Reranker 与读者都不知道
                    # 这是哪个功能的错误处理。
                    text=f"[{chunk.title}]\n{chunk.text}",
                    metadata={
                        **base,
                        "kind": KIND_PROFILE,
                        "section": SECTION_BODY,
                        "heading_path": list(chunk.heading_path),
                        "chunk_index": chunk.index,
                        "chunk_part": chunk.part,
                    },
                )
            )
        for case in cases:
            if case.split not in _ARCHIVABLE_SPLITS or not case.prompt.strip():
                continue
            documents.append(
                SearchDocument(
                    doc_id=f"{key}:case:{case.case_id}",
                    collection=collection,
                    text=case.prompt,
                    metadata={
                        **base,
                        "kind": KIND_CASE,
                        "case_id": case.case_id,
                        "category": case.category.value,
                        "split": case.split.value,
                        "expected_output": case.expected_output,
                    },
                )
            )
        return documents

    # ------------------------------------------------------------------ #
    # 冷启动检索（Generator 调用）
    # ------------------------------------------------------------------ #

    async def retrieve_few_shot_examples_for_cold_start(
        self, new_skill: SkillDefinition, count: int | None = None
    ) -> list[ArchivedExample]:
        """为一个**历史上从未成功评测过**的 Skill 检索最相近的历史成功范本。

        返回类型从文档 23 正文的 `list[dict]` 收紧为 `list[ArchivedExample]`（字段即 dict 的键，
        `model_dump()` 可得原形态）：模板渲染与单测都依赖稳定的字段名。

        该 Skill 自己已经有归档（成功评测过）时返回空列表：它已经不是冷启动，拿自己的旧版本当
        范本只会把上一版的用例原样复读一遍。
        """
        wanted = self._settings.cold_start_example_count if count is None else count
        if wanted <= 0:
            return []
        collection = MemoryCollection.SUCCESSFUL_SKILL_ARCHIVE
        own = await self._search.count(
            collection, metadata_filter={"kind": KIND_PROFILE, "skill_id": new_skill.skill_id}
        )
        if own > 0:
            logger.info("rag_cold_start_skipped", skill_id=new_skill.skill_id, reason="has_archive")
            return []

        # 多取几倍：同一份范本的 description 与多个正文章节可能同时命中，按 archive_key 去重后
        # 才是 `wanted` 份不同的范本。
        hits = await self._search.search(
            new_skill.description,
            collection,
            top_k=wanted * 4,
            metadata_filter={"kind": KIND_PROFILE},
        )
        best: dict[str, SearchDocument] = {}
        for hit in hits:
            key = str(hit.metadata.get("archive_key", ""))
            if not key or hit.metadata.get("skill_id") == new_skill.skill_id or key in best:
                continue
            best[key] = hit
            if len(best) >= wanted:
                break

        examples = [await self._load_example(key, hit) for key, hit in best.items()]
        examples = [example for example in examples if example.cases]
        logger.info(
            "rag_cold_start_retrieved",
            skill_id=new_skill.skill_id,
            archive_keys=[example.archive_key for example in examples],
        )
        return examples

    async def _load_example(self, key: str, hit: SearchDocument) -> ArchivedExample:
        collection = MemoryCollection.SUCCESSFUL_SKILL_ARCHIVE
        description = hit.text
        if hit.metadata.get("section") != SECTION_DESCRIPTION:
            profile = await self._search.list_documents(
                collection,
                metadata_filter={"archive_key": key, "section": SECTION_DESCRIPTION},
                limit=1,
            )
            if profile:
                description = profile[0].text
        case_docs = await self._search.list_documents(
            collection,
            metadata_filter={"archive_key": key, "kind": KIND_CASE},
            limit=_MAX_CASES_PER_ARCHIVE,
        )
        return ArchivedExample(
            archive_key=key,
            skill_id=str(hit.metadata.get("skill_id", "")),
            skill_version_ref=str(hit.metadata.get("skill_version_ref", "")),
            description=description,
            matched_section=hit.text,
            score=hit.score,
            cases=self._select_cases(case_docs),
        )

    def _select_cases(self, case_docs: list[SearchDocument]) -> list[ArchivedCase]:
        """每个类别最多取 `cold_start_cases_per_example` 条（按入库顺序）。

        按类别限额而不是总数限额：否则一份正向用例很多的范本会把反向示例整体挤掉，而反向模板
        恰恰最需要"近脱靶题该怎么出"的示范。
        """
        limit = self._settings.cold_start_cases_per_example
        by_category: dict[str, list[ArchivedCase]] = {}
        for doc in case_docs:
            category = str(doc.metadata.get("category", ""))
            bucket = by_category.setdefault(category, [])
            if len(bucket) >= limit:
                continue
            expected = doc.metadata.get("expected_output")
            bucket.append(
                ArchivedCase(
                    category=category,
                    split=str(doc.metadata.get("split", "")),
                    prompt=doc.text,
                    expected_output=str(expected) if expected is not None else None,
                )
            )
        return [case for bucket in by_category.values() for case in bucket]

    @staticmethod
    def _skip(
        run_id: str, reason: str, *, failed_dimensions: list[str] | None = None
    ) -> ArchiveOutcome:
        logger.info(
            "rag_run_not_archived",
            run_id=run_id,
            reason=reason,
            failed_dimensions=failed_dimensions or [],
        )
        return ArchiveOutcome(
            run_id=run_id,
            archived=False,
            reason=reason,
            failed_dimensions=list(failed_dimensions or []),
        )


@lru_cache
def get_default_rag_archive() -> RagArchive:
    return RagArchive()


async def archive_successful_run(run_id: str) -> ArchiveOutcome:
    """文档 23 第 3.3 节的模块级入口（文档 24 主图收尾节点调用）。

    记忆库总开关关闭时直接返回 `memory_disabled`，不访问数据库。
    """
    if not get_settings().memory.enabled:
        return ArchiveOutcome(run_id=run_id, archived=False, reason="memory_disabled")
    return await get_default_rag_archive().archive_successful_run(run_id)


async def retrieve_few_shot_examples_for_cold_start(
    new_skill: SkillDefinition, count: int | None = None
) -> list[ArchivedExample]:
    """文档 23 第 3.3 节的模块级入口。总开关关闭时返回空列表。"""
    if not get_settings().memory.enabled:
        return []
    return await get_default_rag_archive().retrieve_few_shot_examples_for_cold_start(
        new_skill, count
    )


__all__ = [
    "KIND_CASE",
    "KIND_PROFILE",
    "RagArchive",
    "archive_key_for",
    "archive_successful_run",
    "get_default_rag_archive",
    "retrieve_few_shot_examples_for_cold_start",
]
