"""0011 search documents

docs/dev/23（长时记忆与数据飞轮）新增一张通用记忆库表 `search_documents`，纯新增、不改任何
既有表结构、无需数据回填（`skill-evaluate memory-index` 从本地缓存的工具箱/种子库回填）。

## 与 docs/dev/23 正文 SQL 的偏差

1. 全文检索生成列基于 Python 侧预切分的 `lexical_text`，词典用 `simple` 而不是
   `to_tsvector('english', text)`：中文不含空格，`english` 解析器会把整段中文当作一个词元，
   BM25 路对中文查询完全失效。切分规则见 `memory/hybrid_search.py::lexical_tokens`。
2. 向量索引用 HNSW 而不是 ivfflat（理由同 0009：空表上训练出的 ivfflat 聚类中心召回率极差）。
3. 追加 `embedding_model`（过滤旧模型向量）、`content_hash`（同步时跳过未变文本的重复 embed）、
   `lexical_text`、`updated_at` 四列。

Revision ID: 0011_search_documents
Revises: 0010_approval_workbench
Create Date: 2026-09-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql

revision: str = "0011_search_documents"
down_revision: str | None = "0010_approval_workbench"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 persistence.models.SEARCH_DOCUMENT_EMBEDDING_DIMENSIONS 保持一致（迁移写字面量，理由同 0009）。
_EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "search_documents",
        sa.Column("collection", sa.String(), primary_key=True),
        sa.Column("doc_id", sa.String(), primary_key=True),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("lexical_text", sa.String(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("embedding", VECTOR(_EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # 生成列用原生 SQL 声明：Alembic 的 sa.Computed 在不同版本对 tsvector 类型的渲染不一致。
    op.execute(
        "ALTER TABLE search_documents ADD COLUMN text_search tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', lexical_text)) STORED"
    )
    op.execute(
        "CREATE INDEX ix_search_documents_embedding_hnsw "
        "ON search_documents USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_search_documents_text_search ON search_documents USING gin (text_search)"
    )
    # 元数据过滤（如 Optimizer 经验按 role、冷启动检索按 kind / skill_id）走 JSONB 包含查询。
    op.execute(
        "CREATE INDEX ix_search_documents_metadata "
        "ON search_documents USING gin (metadata jsonb_path_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_search_documents_metadata")
    op.execute("DROP INDEX IF EXISTS ix_search_documents_text_search")
    op.execute("DROP INDEX IF EXISTS ix_search_documents_embedding_hnsw")
    op.drop_table("search_documents")
