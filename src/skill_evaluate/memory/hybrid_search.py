"""统一混合检索服务：Dense + BM25 + Reranker（docs/dev/23 第 2 节）。

```
query ──┬─ embed ─→ pgvector 余弦近邻 (dense_k) ─┐
        │                                        ├─ 按 doc_id 去重合并 ─→ Cross-Encoder 重排 ─→ top_k
        └─ lexical_tokens ─→ ts_rank_cd (bm25_k) ─┘            （Reranker 不可用时：RRF 融合排序）
```

架构文档对两路召回的分工写得很清楚：稠密检索理解"数据清理 ≈ 数据清洗"，BM25 保证
`pdfplumber` 这类低频专有词被精准命中，Reranker 做最终的精排。三处 Few-shot 闭环
（Validator 模板、Generator 种子锚点 / 冷启动范本、Optimizer 修复经验）全部经由本服务检索，
不各自实现一套"差不多的相似度"。

## 降级语义（逐级退化，而不是一处故障全链路失败）

| 故障 | 行为 |
|---|---|
| Reranker 未安装 / 加载失败 | RRF 融合排序，`score` = 稠密相似度（或归一化 ts_rank） |
| 检索时 embedding 通道故障 | 只走 BM25 路，打 warning |
| 数据库不可用 | 异常原样上抛，由三处调用方各自回落到本文档之前的实现 |
| 索引时 embedding 通道故障 | 异常原样上抛（`embedding` 列 NOT NULL，没有向量就不能入库） |

"数据库不可用"不在本层吞掉：调用方各有更合适的回退（Validator 回关键词匹配、种子锚点回进程内
embedding），本层吞掉只会让它们拿到一个"空结果"而误以为库里真的没有相关记忆。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Sequence
from functools import lru_cache
from typing import Protocol

from skill_evaluate.agents.embedding import (
    EmbeddingClient,
    EmbeddingError,
    OpenRouterEmbeddingClient,
)
from skill_evaluate.config import MemorySettings, get_settings
from skill_evaluate.errors import ConfigurationError
from skill_evaluate.logging import get_logger
from skill_evaluate.memory.reranker import CrossEncoderReranker, Reranker, RerankerUnavailableError
from skill_evaluate.state.memory import (
    IndexStats,
    MemoryCollection,
    SearchDocument,
    StoredSearchDocument,
)

logger = get_logger(component="hybrid_search")

# 查询侧最多取多少个词元参与 OR 检索。一段几千字的失败摘要切出几百个 2-gram，全部 OR 起来
# 等于"命中任何一个常用字组合就算相关"，BM25 路会退化成噪声。
_MAX_QUERY_TOKENS = 64

# 词元切分：英文/数字按词（≥2 字符），中文按 2-gram。与 `agents/validator/toolbox.py::extract_keywords`
# 同一规则，但**不复用**它：toolbox 反过来要依赖本模块做语义检索，复用会形成循环导入。
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}|[一-鿿]+")

# 只剔除极少数英文虚词：`simple` 词典不做停用词处理，这几个词出现在几乎每条英文文档里，
# 保留会让 OR 查询的召回被它们淹没。中文 2-gram 不做停用词表——词表一膨胀，"删除"这类真正
# 有区分度的动词也会被误伤。
_STOPWORDS = frozenset({"the", "and", "or", "of", "to", "in", "is", "are", "for", "with", "an"})


def lexical_tokens(text: str) -> list[str]:
    """把文本切成全文检索词元（保序、保留重复，重复次数即词频，影响 ts_rank_cd）。

    入库（`lexical_text`）与查询共用这一个函数，是 BM25 路能工作的前提：两侧切分规则只要有
    一点不一致，中文就会一个都对不上。
    """
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        piece = match.group(0)
        if _ASCII_TOKEN_RE.fullmatch(piece):
            if piece not in _STOPWORDS:
                tokens.append(piece)
            continue
        if len(piece) <= 2:
            tokens.append(piece)
            continue
        tokens.extend(piece[i : i + 2] for i in range(len(piece) - 1))
    return tokens


def query_tokens(text: str) -> list[str]:
    """查询侧词元：去重保序并截断到 `_MAX_QUERY_TOKENS`。"""
    return list(dict.fromkeys(lexical_tokens(text)))[:_MAX_QUERY_TOKENS]


def content_hash(text: str) -> str:
    """文本内容哈希。embedding 只取决于文本，因此只哈希文本（元数据变化不触发重新 embed）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SearchDocumentStore(Protocol):
    """存储层协议。Postgres 实现是 `persistence.repository.SearchDocumentRepository`，单测注入内存替身。"""

    async def get_fingerprints(
        self, collection: str, doc_ids: list[str]
    ) -> dict[str, tuple[str, str]]: ...

    async def upsert_many(self, docs: list[StoredSearchDocument]) -> None: ...

    async def update_metadata(
        self, collection: str, doc_id: str, metadata: dict[str, object]
    ) -> None: ...

    async def delete_missing(self, collection: str, keep_doc_ids: list[str]) -> int: ...

    async def dense_search(
        self,
        collection: str,
        query_embedding: list[float],
        *,
        embedding_model: str,
        limit: int,
        metadata_filter: dict[str, object] | None = None,
    ) -> list[tuple[SearchDocument, float]]: ...

    async def lexical_search(
        self,
        collection: str,
        tokens: list[str],
        *,
        limit: int,
        metadata_filter: dict[str, object] | None = None,
    ) -> list[tuple[SearchDocument, float]]: ...

    async def list_documents(
        self,
        collection: str,
        *,
        metadata_filter: dict[str, object] | None = None,
        limit: int = 100,
    ) -> list[SearchDocument]: ...

    async def count(
        self, collection: str, *, metadata_filter: dict[str, object] | None = None
    ) -> int: ...


class HybridSearchService:
    """记忆库的唯一读写入口：`index` / `index_many` / `sync_collection` / `search`。"""

    def __init__(
        self,
        *,
        store: SearchDocumentStore | None = None,
        embedding_client: EmbeddingClient | None = None,
        reranker: Reranker | None = None,
        settings: MemorySettings | None = None,
        embedding_model: str | None = None,
    ) -> None:
        self._settings = settings or get_settings().memory
        if store is None:
            # 延迟导入：只用内存替身的单测不必加载 SQLAlchemy 引擎配置。
            from skill_evaluate.persistence.repository import SearchDocumentRepository

            store = SearchDocumentRepository()
        self._store = store
        self._embedding_client = embedding_client or OpenRouterEmbeddingClient()
        self._reranker: Reranker = reranker or CrossEncoderReranker(settings=self._settings)
        # 与 case_embeddings 同一个模型口径（见 MemorySettings 类注释）。
        self._embedding_model = embedding_model or get_settings().generator_trust.embedding_model

    @property
    def store(self) -> SearchDocumentStore:
        return self._store

    @property
    def embedding_model(self) -> str:
        return self._embedding_model

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    async def index(self, doc: SearchDocument) -> None:
        """写入一条文档（向量 + 全文检索两套索引在同一行内共存，见迁移 0011）。"""
        await self.index_many([doc])

    async def index_many(self, docs: Sequence[SearchDocument]) -> IndexStats:
        """批量写入。文本与 embedding 模型都没变的文档跳过重新 embed，只刷新元数据。

        按集合分组处理，但统计合并返回（`collection` 取第一条的集合名）：调用方通常一次只写
        一个集合，混写时统计仅供参考。
        """
        stats = IndexStats(collection=docs[0].collection if docs else "")
        by_collection: dict[str, list[SearchDocument]] = {}
        for doc in docs:
            by_collection.setdefault(doc.collection, []).append(doc)

        for collection, group in by_collection.items():
            # 同一批里 doc_id 重复时后者覆盖前者：一次 upsert 里同主键出现两次会让 Postgres 报
            # "ON CONFLICT DO UPDATE command cannot affect row a second time"。
            unique = list({doc.doc_id: doc for doc in group}.values())
            fingerprints = await self._store.get_fingerprints(
                collection, [doc.doc_id for doc in unique]
            )
            to_embed: list[SearchDocument] = []
            for doc in unique:
                known = fingerprints.get(doc.doc_id)
                if doc.embedding is None and known == (
                    content_hash(doc.text),
                    self._embedding_model,
                ):
                    await self._store.update_metadata(collection, doc.doc_id, dict(doc.metadata))
                    stats.skipped += 1
                else:
                    to_embed.append(doc)

            missing = [doc for doc in to_embed if doc.embedding is None]
            vectors = (
                await self._embedding_client.embed([doc.text for doc in missing]) if missing else []
            )
            computed = dict(zip((doc.doc_id for doc in missing), vectors, strict=True))
            stored = [
                StoredSearchDocument(
                    document=doc.model_copy(update={"embedding": None, "score": None}),
                    embedding=doc.embedding if doc.embedding is not None else computed[doc.doc_id],
                    embedding_model=self._embedding_model,
                    lexical_text=" ".join(lexical_tokens(doc.text)),
                    content_hash=content_hash(doc.text),
                )
                for doc in to_embed
            ]
            await self._store.upsert_many(stored)
            stats.indexed += len(stored)

        logger.info(
            "memory_indexed",
            collections=sorted(by_collection),
            indexed=stats.indexed,
            skipped=stats.skipped,
        )
        return stats

    async def sync_collection(
        self, collection: MemoryCollection | str, docs: Sequence[SearchDocument]
    ) -> IndexStats:
        """让集合内容与"源"完全一致：upsert 全部 `docs`，删除源里已不存在的文档。

        只用于**以外部仓库为唯一真相**的集合（断言模板、种子锚点）：模板从工具箱里删掉了，
        检索还能召回它，Validator 就会去读一个已经不存在的模板文件。归档类集合
        （成功范本、修复经验）是只增不删的历史，禁止走这里。
        """
        name = str(collection)
        if name in (
            MemoryCollection.SUCCESSFUL_SKILL_ARCHIVE,
            MemoryCollection.OPTIMIZER_PATCH_HISTORY,
        ):
            raise ValueError(f"集合 {name!r} 是只增不删的历史归档，不能做全量同步")
        foreign = [doc.doc_id for doc in docs if doc.collection != name]
        if foreign:
            raise ValueError(f"sync_collection({name!r}) 收到了其他集合的文档：{foreign[:5]}")
        stats = await self.index_many(docs) if docs else IndexStats(collection=name)
        stats.collection = name
        stats.deleted = await self._store.delete_missing(name, [doc.doc_id for doc in docs])
        logger.info(
            "memory_collection_synced",
            collection=name,
            indexed=stats.indexed,
            skipped=stats.skipped,
            deleted=stats.deleted,
        )
        return stats

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #

    async def search(
        self,
        query: str,
        collection: MemoryCollection | str,
        top_k: int = 5,
        dense_k: int | None = None,
        bm25_k: int | None = None,
        *,
        metadata_filter: dict[str, object] | None = None,
    ) -> list[SearchDocument]:
        """混合检索（文档 23 第 2 节）：

        1. dense_results = pgvector 余弦相似度取 dense_k 条
        2. bm25_results = Postgres ts_rank_cd 全文检索取 bm25_k 条
        3. candidates = dense_results ∪ bm25_results（按 doc_id 去重）
        4. Cross-Encoder 重排取 top_k；Reranker 不可用时按 RRF 融合分排序

        `metadata_filter` 是实现期追加的关键字参数（JSONB 包含匹配），例如 Optimizer 只检索
        同角色的修复经验、冷启动检索只看"范本画像"而不是单条用例。
        """
        name = str(collection)
        if top_k <= 0 or not query.strip():
            return []
        dense_limit = dense_k or self._settings.dense_k
        bm25_limit = bm25_k or self._settings.bm25_k

        dense_hits: list[tuple[SearchDocument, float]] = []
        try:
            [query_vector] = await self._embedding_client.embed([query])
        except (EmbeddingError, ConfigurationError) as exc:
            # 检索时 embedding 故障退化为纯 BM25：专有词精确命中这一路仍然有价值。
            logger.warning("memory_search_dense_degraded", collection=name, error=str(exc)[:300])
        else:
            dense_hits = await self._store.dense_search(
                name,
                query_vector,
                embedding_model=self._embedding_model,
                limit=dense_limit,
                metadata_filter=metadata_filter,
            )
        lexical_hits = await self._store.lexical_search(
            name, query_tokens(query), limit=bm25_limit, metadata_filter=metadata_filter
        )

        candidates = _merge_candidates(dense_hits, lexical_hits, rrf_k=self._settings.rrf_k)
        if not candidates:
            return []
        ranked = await self._rank(query, candidates)
        results = ranked[:top_k]
        logger.info(
            "memory_searched",
            collection=name,
            dense_hits=len(dense_hits),
            lexical_hits=len(lexical_hits),
            candidates=len(candidates),
            reranked="rerank_score" in results[0].score_breakdown if results else False,
            doc_ids=[doc.doc_id for doc in results],
        )
        return results

    async def count(
        self,
        collection: MemoryCollection | str,
        *,
        metadata_filter: dict[str, object] | None = None,
    ) -> int:
        return await self._store.count(str(collection), metadata_filter=metadata_filter)

    async def list_documents(
        self,
        collection: MemoryCollection | str,
        *,
        metadata_filter: dict[str, object] | None = None,
        limit: int = 100,
    ) -> list[SearchDocument]:
        return await self._store.list_documents(
            str(collection), metadata_filter=metadata_filter, limit=limit
        )

    async def _rank(self, query: str, candidates: list[SearchDocument]) -> list[SearchDocument]:
        """有 Reranker 走 Cross-Encoder 精排，否则按 RRF 融合分排序。"""
        try:
            scores = await asyncio.to_thread(
                self._reranker.score, query, [doc.text for doc in candidates]
            )
        except RerankerUnavailableError:
            return sorted(
                candidates,
                key=lambda doc: (-doc.score_breakdown.get("rrf_score", 0.0), doc.doc_id),
            )
        reranked = [
            doc.model_copy(
                update={
                    "score": value,
                    "score_breakdown": {**doc.score_breakdown, "rerank_score": value},
                }
            )
            for doc, value in zip(candidates, scores, strict=True)
        ]
        reranked.sort(key=lambda doc: (-(doc.score or 0.0), doc.doc_id))
        return reranked


def _merge_candidates(
    dense_hits: list[tuple[SearchDocument, float]],
    lexical_hits: list[tuple[SearchDocument, float]],
    *,
    rrf_k: int,
) -> list[SearchDocument]:
    """两路结果按 doc_id 去重合并，并预先算好无重排时要用的分数。

    - `rrf_score` = Σ 1/(rrf_k + 名次)：只看名次、不看原始分数。两路分数量纲完全不同（余弦
      相似度 vs ts_rank），直接相加没有意义；RRF 是业界融合异构召回的标准做法。
    - `score`（无重排时的相关度）优先取稠密相似度：它是有语义含义的 0~1 值，Validator 的阈值
      可以直接比较；只被 BM25 召回的文档退而取归一化 ts_rank。
    """
    merged: dict[str, SearchDocument] = {}
    for hits, signal in ((dense_hits, "dense_similarity"), (lexical_hits, "lexical_rank")):
        for rank_index, (doc, value) in enumerate(hits, start=1):
            current = merged.get(doc.doc_id) or doc.model_copy(update={"score_breakdown": {}})
            breakdown = dict(current.score_breakdown)
            breakdown[signal] = max(0.0, min(1.0, value))
            breakdown["rrf_score"] = breakdown.get("rrf_score", 0.0) + 1.0 / (rrf_k + rank_index)
            merged[doc.doc_id] = current.model_copy(update={"score_breakdown": breakdown})

    results: list[SearchDocument] = []
    for doc in merged.values():
        breakdown = doc.score_breakdown
        relevance = breakdown.get("dense_similarity", breakdown.get("lexical_rank", 0.0))
        results.append(doc.model_copy(update={"score": relevance}))
    return results


def memory_enabled() -> bool:
    """记忆库总开关（`SKILLEVAL_MEMORY_ENABLED`）。三处集成点构造默认依赖前先问它。"""
    return get_settings().memory.enabled


@lru_cache
def get_default_hybrid_search() -> HybridSearchService:
    """进程内共享的默认检索服务（Reranker 模型只加载一次）。"""
    return HybridSearchService()


__all__ = [
    "HybridSearchService",
    "SearchDocument",
    "SearchDocumentStore",
    "content_hash",
    "get_default_hybrid_search",
    "lexical_tokens",
    "memory_enabled",
    "query_tokens",
]
