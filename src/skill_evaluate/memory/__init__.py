"""长时记忆与数据飞轮（docs/dev/23）。

- `hybrid_search.py`：统一混合检索服务（pgvector 稠密 + Postgres 全文 + Cross-Encoder 重排）。
- `reranker.py`：本地 Cross-Encoder（可选依赖 `sentence-transformers`，缺失时退化为 RRF）。
- `chunking.py`：SKILL.md 按标题层级分块。
- `rag_archive.py`：全维度通过的 Skill 归档 + Generator 冷启动范本检索。
- `patch_history.py`：Optimizer 修复经验（成败都存）的归档与检索。
- `indexers.py`：断言工具箱 / 种子锚点库 → 记忆库的全量同步。

本包 `__init__` **只导出无业务依赖的检索基础设施**。`rag_archive` / `patch_history` /
`indexers` 请按模块路径导入：`agents/optimizer`、`agents/validator`、`agents/generator` 反过来
依赖这些模块，在这里一并导入会形成循环导入。

接入说明见 docs/dev/interfaces/23_memory_and_data_flywheel.md。
"""

from skill_evaluate.memory.chunking import MarkdownChunk, chunk_markdown
from skill_evaluate.memory.hybrid_search import (
    HybridSearchService,
    SearchDocumentStore,
    content_hash,
    get_default_hybrid_search,
    lexical_tokens,
    memory_enabled,
    query_tokens,
)
from skill_evaluate.memory.reranker import (
    CrossEncoderReranker,
    Reranker,
    RerankerUnavailableError,
)
from skill_evaluate.state.memory import (
    ArchivedCase,
    ArchivedExample,
    ArchiveOutcome,
    IndexStats,
    MemoryCollection,
    PatchExperience,
    SearchDocument,
)

__all__ = [
    "ArchiveOutcome",
    "ArchivedCase",
    "ArchivedExample",
    "CrossEncoderReranker",
    "HybridSearchService",
    "IndexStats",
    "MarkdownChunk",
    "MemoryCollection",
    "PatchExperience",
    "Reranker",
    "RerankerUnavailableError",
    "SearchDocument",
    "SearchDocumentStore",
    "chunk_markdown",
    "content_hash",
    "get_default_hybrid_search",
    "lexical_tokens",
    "memory_enabled",
    "query_tokens",
]
