"""docs/dev/23：长时记忆与数据飞轮。

覆盖：词元切分与 Markdown 分块、混合检索（两路合并 / 重排 / RRF 降级 / embedding 故障降级 /
增量索引 / 集合同步）、三处集成点（Validator 模板检索、种子锚点、Generator 冷启动范本、
Optimizer 修复经验）的接入与降级、RAG 归档门槛。

不碰库、不发真实 embedding 请求、不加载 Cross-Encoder：存储、embedding、Reranker 全部用确定性替身。
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from skill_evaluate.agents.embedding import EmbeddingError
from skill_evaluate.agents.generator import GenerationRequest, GeneratorAgent
from skill_evaluate.agents.generator.seed_anchors import SeedAnchorLibrary, SeedAnchorResolver
from skill_evaluate.agents.llm import LLMCompletion
from skill_evaluate.agents.optimizer import (
    LoopResult,
    OptimizationLoop,
    OptimizerAgent,
    build_failure_context,
)
from skill_evaluate.agents.validator import AssertionToolbox
from skill_evaluate.config import MemorySettings
from skill_evaluate.memory import (
    ArchivedCase,
    ArchivedExample,
    HybridSearchService,
    MemoryCollection,
    RerankerUnavailableError,
    SearchDocument,
    chunk_markdown,
    lexical_tokens,
    query_tokens,
)
from skill_evaluate.memory.indexers import (
    build_assertion_template_documents,
    build_seed_anchor_documents,
    sync_seed_anchor_index,
)
from skill_evaluate.memory.patch_history import (
    OUTCOME_APPLY_FAILED,
    OUTCOME_REGRESSION_FAILED,
    OUTCOME_REGRESSION_PASSED,
    PatchAttempt,
    PatchHistoryMemory,
    summarize_failure,
)
from skill_evaluate.memory.rag_archive import RagArchive
from skill_evaluate.memory.reranker import CrossEncoderReranker, _to_probabilities
from skill_evaluate.observability.discord_notifier import LoggingApprovalNotifier
from skill_evaluate.persistence import suspension as suspension_module
from skill_evaluate.persistence.approval_service import ApprovalService
from skill_evaluate.state.enums import (
    DatasetSplit,
    GenerationMode,
    JudgeVerdictStatus,
    PatchType,
    TestCaseCategory,
)
from skill_evaluate.state.judge import JudgeVerdict
from skill_evaluate.state.memory import StoredSearchDocument
from skill_evaluate.state.patch import Patch
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion

# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #

# 概念词 → 向量维度。文本向量 = 命中概念维度之和；一个都没命中落在最后一维。
# 这样"清洗 CSV"与"表格去重"在稠密路上相近（同属 data 概念），而 BM25 路只认词面。
_CONCEPTS: dict[str, int] = {
    "csv": 0,
    "表格": 0,
    "清洗": 0,
    "去重": 0,
    "json": 1,
    "schema": 1,
    "格式": 1,
    "pdf": 2,
    "pdfplumber": 2,
    "安全": 3,
    "路径": 3,
    "description": 4,
    "触发": 4,
}
DIM = 8


class ConceptEmbedding:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self._fail = fail

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._fail:
            raise EmbeddingError("embedding 通道故障（替身）")
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            vec = [0.0] * DIM
            lowered = text.lower()
            for word, axis in _CONCEPTS.items():
                if word in lowered:
                    vec[axis] += 1.0
            if not any(vec):
                vec[DIM - 1] = 1.0
            vectors.append(vec)
        return vectors


class MemoryStore:
    """`SearchDocumentStore` 的内存实现：稠密 = 余弦，词面 = 词元重叠比例。"""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], StoredSearchDocument] = {}
        self.upserts: list[list[str]] = []
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("database unavailable（替身）")

    async def get_fingerprints(
        self, collection: str, doc_ids: list[str]
    ) -> dict[str, tuple[str, str]]:
        self._check()
        return {
            doc_id: (row.content_hash, row.embedding_model)
            for (coll, doc_id), row in self.rows.items()
            if coll == collection and doc_id in doc_ids
        }

    async def upsert_many(self, docs: list[StoredSearchDocument]) -> None:
        self._check()
        self.upserts.append([doc.document.doc_id for doc in docs])
        for doc in docs:
            self.rows[(doc.document.collection, doc.document.doc_id)] = doc

    async def update_metadata(
        self, collection: str, doc_id: str, metadata: dict[str, object]
    ) -> None:
        row = self.rows[(collection, doc_id)]
        row.document = row.document.model_copy(update={"metadata": dict(metadata)})

    async def delete_missing(self, collection: str, keep_doc_ids: list[str]) -> int:
        doomed = [k for k in self.rows if k[0] == collection and k[1] not in keep_doc_ids]
        for key in doomed:
            del self.rows[key]
        return len(doomed)

    def _filtered(
        self, collection: str, metadata_filter: dict[str, object] | None
    ) -> list[StoredSearchDocument]:
        self._check()
        out = []
        for (coll, _), row in self.rows.items():
            if coll != collection:
                continue
            if metadata_filter and any(
                row.document.metadata.get(k) != v for k, v in metadata_filter.items()
            ):
                continue
            out.append(row)
        return out

    async def dense_search(
        self,
        collection: str,
        query_embedding: list[float],
        *,
        embedding_model: str,
        limit: int,
        metadata_filter: dict[str, object] | None = None,
    ) -> list[tuple[SearchDocument, float]]:
        scored = [
            (row.document, _cosine(query_embedding, row.embedding))
            for row in self._filtered(collection, metadata_filter)
            if row.embedding_model == embedding_model
        ]
        scored.sort(key=lambda pair: (-pair[1], pair[0].doc_id))
        return scored[:limit]

    async def lexical_search(
        self,
        collection: str,
        tokens: list[str],
        *,
        limit: int,
        metadata_filter: dict[str, object] | None = None,
    ) -> list[tuple[SearchDocument, float]]:
        wanted = set(tokens)
        scored = []
        for row in self._filtered(collection, metadata_filter):
            overlap = wanted & set(row.lexical_text.split())
            if overlap:
                scored.append((row.document, len(overlap) / (len(overlap) + 1)))
        scored.sort(key=lambda pair: (-pair[1], pair[0].doc_id))
        return scored[:limit]

    async def list_documents(
        self,
        collection: str,
        *,
        metadata_filter: dict[str, object] | None = None,
        limit: int = 100,
    ) -> list[SearchDocument]:
        return [row.document for row in self._filtered(collection, metadata_filter)][:limit]

    async def count(
        self, collection: str, *, metadata_filter: dict[str, object] | None = None
    ) -> int:
        return len(self._filtered(collection, metadata_filter))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return 0.0 if not na or not nb else sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)


class NoReranker:
    available = False

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        raise RerankerUnavailableError("未安装（替身）")


class KeywordReranker:
    """把含 `favorite` 子串的文档打到最高分，用来证明"重排结果覆盖了召回顺序"。"""

    available = True

    def __init__(self, favorite: str) -> None:
        self.favorite = favorite
        self.calls = 0

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        self.calls += 1
        return [0.95 if self.favorite in text else 0.1 for text in texts]


def _settings(**overrides: Any) -> MemorySettings:
    return MemorySettings(enabled=True, **overrides)


def _service(
    store: MemoryStore | None = None,
    *,
    reranker: Any | None = None,
    embedding: ConceptEmbedding | None = None,
    **settings: Any,
) -> HybridSearchService:
    return HybridSearchService(
        store=store or MemoryStore(),
        embedding_client=embedding or ConceptEmbedding(),
        reranker=reranker or NoReranker(),
        settings=_settings(**settings),
        embedding_model="test-embedding",
    )


def _doc(
    doc_id: str, text: str, collection: str = "assertion_templates", **meta: Any
) -> SearchDocument:
    return SearchDocument(doc_id=doc_id, collection=collection, text=text, metadata=meta)


def _skill(
    skill_id: str = "csv-cleaner",
    description: str = "清洗 CSV 导出文件，去重并统一日期格式",
    body: str = "# CSV Cleaner\n\n## 清洗规则\n\n去掉重复行。\n\n## 错误处理\n\n遇到坏行跳过。\n",
) -> SkillDefinition:
    return SkillDefinition(
        skill_id=skill_id,
        version_ref="v1",
        root_path=".",
        description=description,
        body_markdown=body,
        line_count=len(body.splitlines()),
        token_count=40,
    )


def _case(
    case_id: str,
    *,
    category: TestCaseCategory = TestCaseCategory.POSITIVE,
    split: DatasetSplit = DatasetSplit.TRAIN,
    prompt: str | None = None,
) -> TestCase:
    return TestCase(
        case_id=case_id,
        skill_id="csv-cleaner",
        category=category,
        split=split,
        prompt=prompt or f"帮我把表格去重（{case_id}）",
        generator_run_id="gen-1",
        created_at=datetime.now(UTC),
    )


# --------------------------------------------------------------------------- #
# 词元切分与分块
# --------------------------------------------------------------------------- #


class LexicalTokenTests:
    def test_chinese_is_split_into_bigrams_and_english_by_word(self) -> None:
        tokens = lexical_tokens("用 pdfplumber 提取表格 and the JSON")
        assert "pdfplumber" in tokens and "json" in tokens
        # 中文按 2-gram：`提取表格` → 提取 / 取表 / 表格
        assert {"提取", "取表", "表格"} <= set(tokens)
        # 极少数英文虚词剔除，避免 OR 查询被淹没
        assert "the" not in tokens and "and" not in tokens

    def test_query_tokens_are_unique_and_capped(self) -> None:
        text = " ".join(f"word{i}" for i in range(200)) + " word1"
        tokens = query_tokens(text)
        assert len(tokens) == len(set(tokens)) == 64


class ChunkingTests:
    def test_splits_by_h2_h3_and_keeps_heading_path(self) -> None:
        body = (
            "# 文档标题\n\n导言一句。\n\n## 数据清洗\n\n清洗步骤。\n\n"
            "### 错误处理\n\n坏行跳过。\n\n## 导出\n\n导出为 CSV。\n"
        )
        chunks = chunk_markdown(body)
        assert [c.heading_path for c in chunks] == [
            (),
            ("数据清洗",),
            ("数据清洗", "错误处理"),
            ("导出",),
        ]
        assert chunks[2].text.startswith("### 错误处理")
        assert "坏行跳过" in chunks[2].text

    def test_hash_lines_inside_code_fences_are_not_headings(self) -> None:
        body = "## 用法\n\n```bash\n## 这是注释不是标题\necho hi\n```\n\n## 参数\n\nx\n"
        chunks = chunk_markdown(body)
        assert [c.heading_path for c in chunks] == [("用法",), ("参数",)]
        assert "## 这是注释不是标题" in chunks[0].text

    def test_oversized_section_falls_back_to_paragraphs_and_repeats_heading(self) -> None:
        paragraphs = "\n\n".join("这一段是参数说明，" * 30 for _ in range(6))
        chunks = chunk_markdown(f"## 参数说明\n\n{paragraphs}\n", max_chunk_tokens=150)
        assert len(chunks) > 1
        assert all(c.text.startswith("## 参数说明") for c in chunks)
        assert [c.part for c in chunks] == list(range(len(chunks)))
        # 段落不被拦腰截断：每个分片里的段落都是完整的原文段落。
        original = "这一段是参数说明，" * 30
        assert all(original in c.text for c in chunks)


# --------------------------------------------------------------------------- #
# 混合检索服务
# --------------------------------------------------------------------------- #


class HybridSearchTests:
    async def test_index_skips_unchanged_text_but_refreshes_metadata(self) -> None:
        store, embedding = MemoryStore(), ConceptEmbedding()
        service = _service(store, embedding=embedding)
        await service.index_many([_doc("a", "校验 JSON schema", commit_sha="c1")])
        stats = await service.index_many([_doc("a", "校验 JSON schema", commit_sha="c2")])

        assert (stats.indexed, stats.skipped) == (0, 1)
        assert len(embedding.calls) == 1  # 第二次没有重新 embed
        assert store.rows[("assertion_templates", "a")].document.metadata["commit_sha"] == "c2"

        changed = await service.index_many([_doc("a", "校验 JSON schema 与格式")])
        assert changed.indexed == 1 and len(embedding.calls) == 2

    async def test_sync_collection_deletes_documents_missing_from_source(self) -> None:
        store = MemoryStore()
        service = _service(store)
        await service.sync_collection(
            MemoryCollection.ASSERTION_TEMPLATES, [_doc("a", "json"), _doc("b", "pdf")]
        )
        stats = await service.sync_collection(
            MemoryCollection.ASSERTION_TEMPLATES, [_doc("a", "json")]
        )
        assert stats.deleted == 1
        assert list(store.rows) == [("assertion_templates", "a")]

    async def test_sync_collection_refuses_append_only_archives(self) -> None:
        service = _service()
        with pytest.raises(ValueError, match="只增不删"):
            await service.sync_collection(MemoryCollection.OPTIMIZER_PATCH_HISTORY, [])

    async def test_merges_dense_and_lexical_candidates(self) -> None:
        # "表格去重" 与 csv 文档词面不重合（BM25 路召不回），但同属 data 概念（稠密路召回）；
        # "pdfplumber" 文档靠 BM25 精确词命中。
        service = _service()
        await service.index_many(
            [
                _doc("csv", "清洗 CSV 文件"),
                _doc("pdf", "用 pdfplumber 抽取文本"),
                _doc("other", "发送邮件通知"),
            ]
        )
        results = await service.search("表格去重 pdfplumber", "assertion_templates", top_k=3)
        ids = [doc.doc_id for doc in results]
        assert "csv" in ids and "pdf" in ids
        pdf = next(doc for doc in results if doc.doc_id == "pdf")
        assert "lexical_rank" in pdf.score_breakdown
        assert "rrf_score" in pdf.score_breakdown
        # 无 Reranker：不伪装成重排过
        assert all("rerank_score" not in doc.score_breakdown for doc in results)
        assert all(doc.embedding is None for doc in results)

    async def test_reranker_order_wins_and_scores_are_probabilities(self) -> None:
        reranker = KeywordReranker(favorite="邮件")
        service = _service(reranker=reranker)
        await service.index_many([_doc("csv", "清洗 CSV 文件"), _doc("mail", "CSV 发送邮件")])
        results = await service.search("清洗 CSV", "assertion_templates", top_k=2)
        assert [doc.doc_id for doc in results] == ["mail", "csv"]
        assert results[0].score == pytest.approx(0.95)
        assert results[0].score_breakdown["rerank_score"] == pytest.approx(0.95)
        assert reranker.calls == 1

    async def test_embedding_failure_degrades_to_lexical_only(self) -> None:
        store = MemoryStore()
        await _service(store).index_many([_doc("pdf", "用 pdfplumber 抽取文本")])
        degraded = _service(store, embedding=ConceptEmbedding(fail=True))
        results = await degraded.search("pdfplumber", "assertion_templates")
        assert [doc.doc_id for doc in results] == ["pdf"]
        assert "dense_similarity" not in results[0].score_breakdown

    async def test_metadata_filter_is_applied_to_both_routes(self) -> None:
        service = _service()
        await service.index_many(
            [
                _doc("a", "清洗 CSV", role="prompt_engineer"),
                _doc("b", "清洗 CSV", role="appsec_expert"),
            ]
        )
        results = await service.search(
            "清洗 CSV", "assertion_templates", metadata_filter={"role": "appsec_expert"}
        )
        assert [doc.doc_id for doc in results] == ["b"]

    async def test_blank_query_returns_nothing_without_calling_embedding(self) -> None:
        embedding = ConceptEmbedding()
        assert await _service(embedding=embedding).search("   ", "assertion_templates") == []
        assert embedding.calls == []


class RerankerTests:
    def test_logits_are_squashed_but_probabilities_are_kept(self) -> None:
        assert _to_probabilities([0.2, 0.9]) == [0.2, 0.9]
        squashed = _to_probabilities([-3.0, 4.0])
        assert 0 < squashed[0] < 0.1 and 0.9 < squashed[1] < 1

    def test_disabled_reranker_is_unavailable_without_importing_the_model(self) -> None:
        reranker = CrossEncoderReranker(settings=MemorySettings(reranker_enabled=False))
        assert reranker.available is False
        with pytest.raises(RerankerUnavailableError):
            reranker.score("q", ["t"])

    def test_missing_dependency_is_remembered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builtins

        real_import = builtins.__import__
        attempts: list[str] = []

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "sentence_transformers":
                attempts.append(name)
                raise ImportError("no module")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        reranker = CrossEncoderReranker(settings=MemorySettings())
        assert reranker.available is False
        assert reranker.available is False
        assert attempts == ["sentence_transformers"]  # 加载失败被记住，不反复尝试


# --------------------------------------------------------------------------- #
# 集成 1：Validator 断言模板检索
# --------------------------------------------------------------------------- #

MANIFEST = """\
- template: json_schema_validator.py.jinja
  description: "校验目标文件是否为合法 JSON 且满足给定 schema"
  keywords: [json, schema]
  params: [target_file, schema_definition]
- template: pdf_text_validator.py
  description: "用 pdfplumber 检查 PDF 产物里是否包含指定文本"
  keywords: [pdfplumber]
  params: []
"""


def _toolbox(tmp_path: Path, search: HybridSearchService | None) -> AssertionToolbox:
    (tmp_path / "templates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "manifest.yaml").write_text(MANIFEST, encoding="utf-8")
    return AssertionToolbox(tmp_path, search_service=search)


class ValidatorSemanticLookupTests:
    async def test_semantic_hits_raise_scores_without_dropping_keyword_hits(
        self, tmp_path: Path
    ) -> None:
        search = _service(reranker=KeywordReranker(favorite="PDF"))
        toolbox = _toolbox(tmp_path, search)
        await search.sync_collection(
            MemoryCollection.ASSERTION_TEMPLATES, build_assertion_template_documents(toolbox)
        )
        # 查询里没有任何 manifest keyword → 关键词版一个都命不中；语义版靠 Reranker 命中 PDF 模板。
        matches = await toolbox.lookup("确认导出的 PDF 报告里写了合同编号")
        assert [m.template.template for m in matches] == ["pdf_text_validator.py"]
        assert matches[0].score == pytest.approx(0.95)

        # 关键词精确命中的模板不会因为语义分低而被挤掉（取 max）。
        keyword = await toolbox.lookup("输出 json 并满足 schema")
        assert "json_schema_validator.py.jinja" in [m.template.template for m in keyword]
        assert next(
            m.score for m in keyword if m.template.template == "json_schema_validator.py.jinja"
        ) == pytest.approx(1.0)

    async def test_stale_index_entries_are_ignored(self, tmp_path: Path) -> None:
        search = _service(reranker=KeywordReranker(favorite="已删除"))
        await search.index_many([_doc("removed.py", "已删除的 PDF 模板", template="removed.py")])
        toolbox = _toolbox(tmp_path, search)
        matches = await toolbox.lookup("PDF 已删除")
        assert "removed.py" not in [m.template.template for m in matches]

    async def test_memory_failure_falls_back_to_keyword_lookup(self, tmp_path: Path) -> None:
        store = MemoryStore()
        store.fail = True
        toolbox = _toolbox(tmp_path, _service(store))
        matches = await toolbox.lookup("输出 json 并满足 schema")
        assert [m.template.template for m in matches] == ["json_schema_validator.py.jinja"]

    async def test_memory_disabled_uses_keywords_only(self, tmp_path: Path) -> None:
        # conftest 关闭了总开关且未注入服务 → 与 docs/dev/10 的关键词版行为完全一致。
        toolbox = _toolbox(tmp_path, None)
        matches = await toolbox.lookup("确认导出的 PDF 报告里写了合同编号")
        assert matches == []


# --------------------------------------------------------------------------- #
# 集成 2：种子锚点混合检索
# --------------------------------------------------------------------------- #


def _seed_library(tmp_path: Path) -> SeedAnchorLibrary:
    (tmp_path / "anchors").mkdir(parents=True)
    (tmp_path / "manifest.yaml").write_text(
        "- domain_tag: data\n  description: 数据\n  source: test\n"
        "- domain_tag: docs\n  description: 文档\n  source: test\n",
        encoding="utf-8",
    )
    (tmp_path / "anchors" / "data.yaml").write_text(
        "- id: d1\n  prompt: 这个导出的表格好多重复行 帮我去一下\n", encoding="utf-8"
    )
    (tmp_path / "anchors" / "docs.yaml").write_text(
        "- id: p1\n  prompt: 用 pdfplumber 把合同里的表抽出来\n", encoding="utf-8"
    )
    return SeedAnchorLibrary(tmp_path, repo_url=None)


class SeedAnchorHybridTests:
    async def test_indexed_anchors_are_resolved_via_hybrid_search(self, tmp_path: Path) -> None:
        library = _seed_library(tmp_path)
        search = _service(reranker=KeywordReranker(favorite="重复行"))
        stats = await sync_seed_anchor_index(library, search)
        assert stats is not None and stats.indexed == 2

        fallback_embedding = ConceptEmbedding()
        resolver = SeedAnchorResolver(
            library=library, embedding_client=fallback_embedding, search_service=search
        )
        anchors = await resolver.resolve_for_skill(_skill(), 1)
        assert [a.anchor_id for a in anchors] == ["data/d1"]
        assert anchors[0].commit_sha == library.commit_sha()
        assert fallback_embedding.calls == []  # 没走 docs/dev/21 的进程内向量路径

    async def test_unindexed_library_falls_back_to_in_process_embedding(
        self, tmp_path: Path
    ) -> None:
        library = _seed_library(tmp_path)
        fallback_embedding = ConceptEmbedding()
        resolver = SeedAnchorResolver(
            library=library, embedding_client=fallback_embedding, search_service=_service()
        )
        anchors = await resolver.resolve_for_skill(_skill(), 1)
        assert [a.anchor_id for a in anchors] == ["data/d1"]
        assert fallback_embedding.calls  # 回落路径确实被调用

    async def test_index_from_another_commit_is_not_used(self, tmp_path: Path) -> None:
        library = _seed_library(tmp_path)
        search = _service()
        stale = [
            doc.model_copy(update={"metadata": {**doc.metadata, "commit_sha": "old-commit"}})
            for doc in build_seed_anchor_documents(library)
        ]
        await search.index_many(stale)
        fallback_embedding = ConceptEmbedding()
        resolver = SeedAnchorResolver(
            library=library, embedding_client=fallback_embedding, search_service=search
        )
        await resolver.resolve_for_skill(_skill(), 1)
        assert fallback_embedding.calls


# --------------------------------------------------------------------------- #
# 集成 3：RAG 归档与 Generator 冷启动
# --------------------------------------------------------------------------- #


class FakeRuns:
    def __init__(self, suite_version_id: str | None = "suite-1") -> None:
        self.suite_version_id = suite_version_id

    async def get(self, run_id: str) -> Any:
        if run_id == "missing":
            return None
        return {
            "run_id": run_id,
            "skill_id": "csv-cleaner",
            "skill_version_ref": "v1",
            "suite_version_id": self.suite_version_id,
            "generation_mode": "reuse",
            "created_at": datetime.now(UTC),
        }


class FakeDimensions:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    async def list_by_run(self, run_id: str) -> list[dict[str, object]]:
        return self.rows


class FakeSkills:
    def __init__(self, skill: SkillDefinition | None) -> None:
        self.skill = skill

    async def get(self, skill_id: str, version_ref: str) -> SkillDefinition | None:
        return self.skill


class FakeSuites:
    async def get_active_version(
        self, skill_id: str, skill_version_ref: str | None = None
    ) -> TestSuiteVersion | None:
        return TestSuiteVersion(
            suite_version_id="suite-active",
            skill_id=skill_id,
            skill_version_ref="v1",
            generation_mode="reuse",
            case_ids=[],
            created_at=datetime.now(UTC),
        )


class FakeCases:
    def __init__(self, cases: list[TestCase]) -> None:
        self.cases = cases
        self.queried: list[str] = []

    async def list_by_categories(
        self, suite_version_id: str, categories: list[TestCaseCategory]
    ) -> list[TestCase]:
        self.queried.append(suite_version_id)
        return self.cases


def _pass(dimension: str, blocking: bool = True, status: str = "pass") -> dict[str, object]:
    return {
        "dimension": dimension,
        "status": status,
        "blocking": blocking,
        "score": 1.0,
        "findings": [],
    }


def _archive(
    search: HybridSearchService,
    *,
    dimensions: list[dict[str, object]] | None = None,
    skill: SkillDefinition | None = None,
    cases: list[TestCase] | None = None,
    runs: FakeRuns | None = None,
    **settings: Any,
) -> tuple[RagArchive, FakeCases]:
    case_repo = FakeCases(cases if cases is not None else [])
    archive = RagArchive(
        search=search,
        run_repository=runs or FakeRuns(),
        dimension_repository=FakeDimensions(
            dimensions if dimensions is not None else [_pass("trigger_accuracy")]
        ),
        skill_repository=FakeSkills(skill if skill is not None else _skill()),
        suite_repository=FakeSuites(),
        case_repository=case_repo,
        settings=_settings(**settings),
    )
    return archive, case_repo


class RagArchiveTests:
    @pytest.mark.parametrize(
        ("dimensions", "reason"),
        [
            ([], "no_blocking_dimensions"),
            ([_pass("coverage", blocking=False)], "no_blocking_dimensions"),
            (
                [_pass("trigger_accuracy"), _pass("security", status="fail")],
                "blocking_dimensions_not_passed",
            ),
            # 待人工确认的阻断维度同样不算通过：还没人确认过的结论不配当范本。
            ([_pass("security", status="needs_human_review")], "blocking_dimensions_not_passed"),
        ],
    )
    async def test_only_fully_passed_runs_are_archived(
        self, dimensions: list[dict[str, object]], reason: str
    ) -> None:
        store = MemoryStore()
        archive, _ = _archive(_service(store), dimensions=dimensions)
        outcome = await archive.archive_successful_run("run-1")
        assert outcome.archived is False and outcome.reason == reason
        assert store.rows == {}

    async def test_non_blocking_failures_do_not_prevent_archiving(self) -> None:
        store = MemoryStore()
        archive, _ = _archive(
            _service(store),
            dimensions=[
                _pass("trigger_accuracy"),
                _pass("coverage", blocking=False, status="fail"),
            ],
        )
        assert (await archive.archive_successful_run("run-1")).archived is True

    async def test_missing_run_is_reported(self) -> None:
        archive, _ = _archive(_service())
        outcome = await archive.archive_successful_run("missing")
        assert outcome.reason == "run_not_found"

    async def test_archives_profile_chunks_and_train_validation_cases(self) -> None:
        store = MemoryStore()
        cases = [
            _case("c-train"),
            _case("c-val", split=DatasetSplit.VALIDATION, category=TestCaseCategory.NEGATIVE),
            _case("c-cold", split=DatasetSplit.COLD),
        ]
        archive, case_repo = _archive(_service(store), cases=cases)
        outcome = await archive.archive_successful_run("run-1")

        assert outcome.archived and outcome.archive_key == "csv-cleaner@v1"
        docs = {doc_id: row.document for (_, doc_id), row in store.rows.items()}
        assert "csv-cleaner@v1:description" in docs
        assert "csv-cleaner@v1:case:c-train" in docs
        assert "csv-cleaner@v1:case:c-val" in docs
        assert "csv-cleaner@v1:case:c-cold" not in docs  # cold 区降级用例不当范本
        chunk_titles = [d.text.splitlines()[0] for d in docs.values() if ":chunk:" in d.doc_id]
        assert "[清洗规则]" in chunk_titles and "[错误处理]" in chunk_titles
        assert case_repo.queried == ["suite-1"]
        assert outcome.document_count == len(docs)

    async def test_falls_back_to_active_suite_when_run_has_none(self) -> None:
        archive, case_repo = _archive(_service(), runs=FakeRuns(suite_version_id=None))
        await archive.archive_successful_run("run-1")
        assert case_repo.queried == ["suite-active"]

    async def test_rearchiving_the_same_version_is_idempotent(self) -> None:
        store = MemoryStore()
        archive, _ = _archive(_service(store), cases=[_case("c-1")])
        await archive.archive_successful_run("run-1")
        size = len(store.rows)
        await archive.archive_successful_run("run-2")
        assert len(store.rows) == size

    async def test_cold_start_retrieves_distinct_examples_with_category_limits(self) -> None:
        store = MemoryStore()
        search = _service(store)
        cases = [_case(f"p{i}") for i in range(6)] + [
            _case(f"n{i}", category=TestCaseCategory.NEGATIVE) for i in range(2)
        ]
        archive, _ = _archive(search, cases=cases, cold_start_cases_per_example=3)
        await archive.archive_successful_run("run-1")

        new_skill = _skill(skill_id="tsv-cleaner", description="清洗 TSV 表格并去重")
        examples = await archive.retrieve_few_shot_examples_for_cold_start(new_skill, count=2)

        assert [e.archive_key for e in examples] == ["csv-cleaner@v1"]  # 同一范本多段命中只算一份
        example = examples[0]
        assert example.description == _skill().description
        assert len(example.cases_for("positive")) == 3
        assert len(example.cases_for("negative")) == 2

    async def test_skill_with_its_own_archive_is_not_cold_start(self) -> None:
        search = _service()
        archive, _ = _archive(search, cases=[_case("c-1")])
        await archive.archive_successful_run("run-1")
        assert await archive.retrieve_few_shot_examples_for_cold_start(_skill()) == []


def _example() -> ArchivedExample:
    return ArchivedExample(
        archive_key="json-tool@v3",
        skill_id="json-tool",
        skill_version_ref="v3",
        description="把 JSON 转成表格",
        matched_section="把 JSON 转成表格",
        cases=[
            ArchivedCase(category="positive", split="train", prompt="这坨 json 帮我摊平成表"),
            ArchivedCase(category="negative", split="validation", prompt="json 和 yaml 哪个好"),
        ],
    )


class RecordingRetriever:
    def __init__(self, examples: list[ArchivedExample] | None = None, fail: bool = False) -> None:
        self.examples = examples or []
        self.fail = fail
        self.calls = 0

    async def retrieve_few_shot_examples_for_cold_start(
        self, new_skill: SkillDefinition, count: int | None = None
    ) -> list[ArchivedExample]:
        self.calls += 1
        if self.fail:
            raise ConnectionError("db down")
        return self.examples


class CasesLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(
        self, *, prompt: str, model: str, temperature: float, **_: Any
    ) -> LLMCompletion:
        self.prompts.append(prompt)
        payload = {
            "cases": [{"prompt": "随便一条", "rationale": "r", "diversity_tag": "colloquial"}]
        }
        return LLMCompletion(
            text=json.dumps(payload, ensure_ascii=False),
            prompt_tokens=1,
            completion_tokens=1,
            model=model,
            temperature_applied=None,
        )


class NoSeeds:
    def resolve_ids(self, anchor_ids: list[str]) -> list[Any]:
        return []

    async def resolve_for_skill(self, skill: SkillDefinition, count: int) -> list[Any]:
        return []


def _generator(retriever: RecordingRetriever) -> tuple[GeneratorAgent, CasesLLM]:
    llm = CasesLLM()
    agent = GeneratorAgent(
        model="claude-haiku-4-5",
        llm_client=llm,
        seed_anchor_resolver=NoSeeds(),
        cold_start_retriever=retriever,
    )
    return agent, llm


class GeneratorColdStartTests:
    async def test_examples_are_rendered_per_category(self) -> None:
        agent, llm = _generator(RecordingRetriever([_example()]))
        request = GenerationRequest(
            skill=_skill(),
            mode=GenerationMode.REUSE,
            triggered_by="auto_bootstrap",
            positive_count=1,
            negative_count=1,
        )
        await agent.generate(request, generator_run_id="g-1")

        positive_prompt, negative_prompt = llm.prompts
        assert "历史优质用例范本" in positive_prompt
        assert "这坨 json 帮我摊平成表" in positive_prompt
        assert "json 和 yaml 哪个好" not in positive_prompt  # 正向模板只看正向范例
        assert "json 和 yaml 哪个好" in negative_prompt

    async def test_incremental_patch_does_not_retrieve(self) -> None:
        retriever = RecordingRetriever([_example()])
        agent, llm = _generator(retriever)
        request = GenerationRequest(
            skill=_skill(),
            mode=GenerationMode.INCREMENTAL_PATCH,
            triggered_by="coverage_gap",
            positive_count=1,
            negative_count=0,
        )
        await agent.generate(request, generator_run_id="g-1")
        assert retriever.calls == 0
        assert "历史优质用例范本" not in llm.prompts[0]

    async def test_retrieval_failure_does_not_block_generation(self) -> None:
        agent, llm = _generator(RecordingRetriever(fail=True))
        request = GenerationRequest(
            skill=_skill(),
            mode=GenerationMode.REUSE,
            triggered_by="auto_bootstrap",
            positive_count=1,
            negative_count=0,
        )
        cases = await agent.generate(request, generator_run_id="g-1")
        assert len(cases) == 1 and "历史优质用例范本" not in llm.prompts[0]

    async def test_explicit_empty_list_means_no_examples(self) -> None:
        retriever = RecordingRetriever([_example()])
        agent, _ = _generator(retriever)
        request = GenerationRequest(
            skill=_skill(),
            mode=GenerationMode.REUSE,
            triggered_by="auto_bootstrap",
            positive_count=1,
            negative_count=0,
            archived_examples=[],
        )
        await agent.generate(request, generator_run_id="g-1")
        assert retriever.calls == 0


# --------------------------------------------------------------------------- #
# 集成 4：Optimizer 修复经验
# --------------------------------------------------------------------------- #


def _verdict(reasoning: str = "没有命中 description") -> JudgeVerdict:
    return JudgeVerdict(
        verdict_id="v-1",
        subject_id="case-1",
        status=JudgeVerdictStatus.FAIL,
        reasoning=reasoning,
        temperature=0.1,
        model="m",
        created_at=datetime.now(UTC),
    )


def _patch(patch_id: str, new_text: str = "清洗并校验表格导出文件") -> Patch:
    return Patch(
        patch_id=patch_id,
        skill_id="csv-cleaner",
        base_skill_version_ref="v1",
        patch_type=PatchType.DESCRIPTION_PATCH,
        target_path="SKILL.md",
        diff=f"@@ -1,1 +1,1 @@\n-清洗 CSV 导出文件，去重并统一日期格式\n+{new_text}",
        rationale=f"理由 {patch_id}",
        created_at=datetime.now(UTC),
    )


def _ctx(role: str = "prompt_engineer") -> Any:
    return build_failure_context(
        _skill(),
        [_case("c-1", prompt="这个表格帮我去个重")],
        [_verdict()],
        role=role,
    )


class PatchHistoryTests:
    def test_failure_summary_excludes_body_but_keeps_evidence(self) -> None:
        summary = summarize_failure(_ctx())
        assert "这个表格帮我去个重" in summary
        assert "没有命中 description" in summary
        assert "遇到坏行跳过" not in summary  # 正文不进摘要

    async def test_archives_all_attempts_and_retrieves_same_role_only(self) -> None:
        search = _service()
        memory = PatchHistoryMemory(
            search=search, settings=_settings(optimizer_patch_diff_max_chars=100)
        )
        await memory.archive_attempts(
            "run-1",
            _ctx(),
            [
                PatchAttempt(_patch("p-bad"), OUTCOME_APPLY_FAILED, "diff 对不上", 0),
                PatchAttempt(_patch("p-meh"), OUTCOME_REGRESSION_FAILED, "还是不触发", 1),
                PatchAttempt(_patch("p-ok"), OUTCOME_REGRESSION_PASSED, "全通过", 2),
            ],
        )
        await memory.archive_attempts(
            "run-2",
            _ctx("appsec_expert"),
            [PatchAttempt(_patch("p-sec"), OUTCOME_REGRESSION_PASSED)],
        )

        experiences = await memory.retrieve_similar(_ctx(), top_k=10)
        assert {e.patch_id for e in experiences} == {"p-bad", "p-meh", "p-ok"}
        by_id = {e.patch_id: e for e in experiences}
        assert by_id["p-ok"].passed is True and by_id["p-meh"].passed is False
        assert by_id["p-bad"].outcome == OUTCOME_APPLY_FAILED

    async def test_optimizer_prompt_separates_successes_from_failures(self) -> None:
        search = _service()
        memory = PatchHistoryMemory(search=search, settings=_settings())
        await memory.archive_attempts(
            "old-run",
            _ctx(),
            [
                PatchAttempt(
                    _patch("p-old-fail", "只加了个同义词"), OUTCOME_REGRESSION_FAILED, "仍不触发"
                ),
                PatchAttempt(_patch("p-old-ok", "写明触发场景"), OUTCOME_REGRESSION_PASSED, "通过"),
            ],
        )

        prompts: list[str] = []

        class LLM:
            async def complete(
                self, *, prompt: str, model: str, temperature: float, **_: Any
            ) -> LLMCompletion:
                prompts.append(prompt)
                payload = {
                    "patch_type": "description_patch",
                    "target_path": "SKILL.md",
                    "diff": "@@ -1,1 +1,1 @@\n-a\n+b",
                    "rationale": "r",
                }
                return LLMCompletion(
                    text=json.dumps(payload),
                    prompt_tokens=1,
                    completion_tokens=1,
                    model=model,
                    temperature_applied=None,
                )

        class Repo:
            async def save(self, patch: Patch) -> None:
                return None

        agent = OptimizerAgent(llm_client=LLM(), patch_repository=Repo(), patch_memory=memory)  # type: ignore[arg-type]
        await agent.propose_patch(_ctx())
        prompt = prompts[0]
        assert "历史修复经验" in prompt
        success_at = prompt.index("过往成功修复")
        failure_at = prompt.index("过往失败尝试")
        assert prompt.index("写明触发场景") > success_at
        assert failure_at < prompt.index("只加了个同义词")
        assert "仍不触发" in prompt

    async def test_optimizer_without_memory_renders_no_experience_section(self) -> None:
        prompts: list[str] = []

        class LLM:
            async def complete(
                self, *, prompt: str, model: str, temperature: float, **_: Any
            ) -> LLMCompletion:
                prompts.append(prompt)
                payload = {
                    "patch_type": "description_patch",
                    "target_path": "SKILL.md",
                    "diff": "@@ -1 +1 @@\n-a\n+b",
                    "rationale": "r",
                }
                return LLMCompletion(
                    text=json.dumps(payload),
                    prompt_tokens=1,
                    completion_tokens=1,
                    model=model,
                    temperature_applied=None,
                )

        class Repo:
            async def save(self, patch: Patch) -> None:
                return None

        agent = OptimizerAgent(llm_client=LLM(), patch_repository=Repo())  # type: ignore[arg-type]
        await agent.propose_patch(_ctx())
        assert "历史修复经验" not in prompts[0]


class _PatchRepo:
    async def save(self, patch: Patch) -> None:
        return None

    async def save_application_result(self, result: Any) -> None:
        return None


class _StateRepo:
    async def increment_retry(self, run_id: str, node_name: str) -> int:
        return 1


class _Ledger:
    async def create(self, **_: Any) -> None:
        return None


class _Pending:
    async def save(self, approval: Any) -> bool:
        return True


class _NoRun:
    async def get(self, run_id: str) -> Any:
        return None


class ScriptedOptimizer:
    def __init__(self, patches: list[Patch]) -> None:
        self._patches = patches
        self.count = 0

    async def propose_patch(self, ctx: Any) -> Patch:
        self.count += 1
        return self._patches[min(self.count - 1, len(self._patches) - 1)]


class RecordingPatchMemory:
    def __init__(self, fail: bool = False) -> None:
        self.archived: list[tuple[str, list[PatchAttempt]]] = []
        self.fail = fail

    async def archive_attempts(self, run_id: str, ctx: Any, attempts: list[PatchAttempt]) -> int:
        if self.fail:
            raise ConnectionError("db down")
        self.archived.append((run_id, list(attempts)))
        return len(attempts)


def _loop(memory: RecordingPatchMemory) -> OptimizationLoop:
    return OptimizationLoop(
        max_retries=2,
        patch_repository=_PatchRepo(),  # type: ignore[arg-type]
        pipeline_state_repository=_StateRepo(),  # type: ignore[arg-type]
        approval_service=ApprovalService(
            ledger_repository=_Ledger(),  # type: ignore[arg-type]
            pending_repository=_Pending(),  # type: ignore[arg-type]
            run_repository=_NoRun(),  # type: ignore[arg-type]
            notifier=LoggingApprovalNotifier(),
        ),
        patch_memory=memory,  # type: ignore[arg-type]
    )


class OptimizationLoopArchiveTests:
    async def test_success_archives_every_attempt_including_failures(self) -> None:
        memory = RecordingPatchMemory()
        optimizer = ScriptedOptimizer(
            [
                Patch(**{**_patch("p-bad").model_dump(), "diff": "@@ -1 +1 @@\n-不存在的原文\n+x"}),
                _patch("p-good"),
            ]
        )

        async def retest(skill: SkillDefinition) -> LoopResult:
            return LoopResult(passed=True, detail="ok")

        patch = await _loop(memory).run("run-1", _ctx(), retest, optimizer)  # type: ignore[arg-type]
        assert patch is not None and patch.patch_id == "p-good"
        [(run_id, attempts)] = memory.archived
        assert run_id == "run-1"
        assert [(a.patch.patch_id, a.outcome) for a in attempts] == [
            ("p-bad", OUTCOME_APPLY_FAILED),
            ("p-good", OUTCOME_REGRESSION_PASSED),
        ]

    async def test_exhausted_loop_archives_before_suspending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        memory = RecordingPatchMemory()
        order: list[str] = []
        original = memory.archive_attempts

        async def tracking_archive(run_id: str, ctx: Any, attempts: list[PatchAttempt]) -> int:
            order.append("archive")
            return await original(run_id, ctx, attempts)

        memory.archive_attempts = tracking_archive  # type: ignore[method-assign]

        async def fake_suspend(*, reason: str, wait_key: str) -> Any:
            order.append("suspend")
            return None

        async def retest(skill: SkillDefinition) -> LoopResult:
            return LoopResult(passed=False, detail="还是不过")

        monkeypatch.setattr(suspension_module, "suspend_and_wait", fake_suspend)
        optimizer = ScriptedOptimizer([_patch("p-1", "改法一")])
        assert await _loop(memory).run("run-1", _ctx(), retest, optimizer) is None  # type: ignore[arg-type]
        assert order == ["archive", "suspend"]
        assert all(not a.passed for a in memory.archived[0][1])

    async def test_archive_failure_does_not_change_the_loop_result(self) -> None:
        async def retest(skill: SkillDefinition) -> LoopResult:
            return LoopResult(passed=True)

        patch = await _loop(RecordingPatchMemory(fail=True)).run(
            "run-1",
            _ctx(),
            retest,
            ScriptedOptimizer([_patch("p-1")]),  # type: ignore[arg-type]
        )
        assert patch is not None and patch.patch_id == "p-1"


class MemorySettingsTests:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SKILLEVAL_MEMORY_ENABLED", raising=False)  # conftest 为单测关闭了它
        settings = MemorySettings()
        assert settings.enabled is True
        assert settings.chunk_max_tokens == 800
        assert (settings.dense_k, settings.bm25_k) == (20, 20)
