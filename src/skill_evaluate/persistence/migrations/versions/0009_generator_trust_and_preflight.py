"""0009 generator trust and preflight

docs/dev/21（模块十一续：Generator 可信度与沙箱环境一致性证明）新增三张表，纯新增、
不改任何既有表结构、无需数据回填：

- `case_embeddings`：用例 prompt 向量，反坍塌检测的"历史分布"。docs/dev/23 的
  `search_documents` 是独立姊妹表，不在此扩列。
- `generation_collapse_events`：被阻断的坍塌事件，连续坍塌计数与人工注入种子告警的依据。
- `canary_probe_history`：金丝雀探针记录，`nightly_or_image_change` 调度模式的跳过依据。

## 与 docs/dev/21 正文的一处偏差：向量索引用 HNSW 而不是 ivfflat

正文写的是 `USING ivfflat`。ivfflat 的聚类中心在**建索引那一刻**按表内已有数据训练，
而迁移时这张表必然是空的——空表上建出来的 ivfflat 索引召回率极差，且不会随数据增长
自动改善（必须等数据攒够后手动 REINDEX）。HNSW 不需要训练，空表建索引后随写入增量构建，
正是这种"从零开始积累"的表该用的索引。pgvector >= 0.5 支持（docker-compose 用的
`pgvector/pgvector:pg16` 满足）。

注意：坍塌检测本身按 `skill_id` 取最近 N 条后在进程内算距离，**不走**这个索引；建它是为
docs/dev/23 在同一张表上做近邻检索时不必再迁移一次。

Revision ID: 0009_generator_trust_and_preflight
Revises: 0008_test_case_suggestions
Create Date: 2026-09-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql

revision: str = "0009_generator_trust_and_preflight"
down_revision: str | None = "0008_test_case_suggestions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 persistence.models.CASE_EMBEDDING_DIMENSIONS 保持一致。迁移里写字面量而不 import 模型：
# 迁移脚本必须描述"当时"的表结构，模型常量将来改了也不该改写历史迁移。
_EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    # 0001 已经建过扩展；这里再声明一次是防御性的——有人手工 downgrade 到 0000 再升上来时
    # vector 类型必须先存在。IF NOT EXISTS 使其幂等。
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "case_embeddings",
        sa.Column(
            "case_id",
            sa.String(),
            sa.ForeignKey("test_cases.case_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("skill_id", sa.String(), nullable=False),
        sa.Column("embedding", VECTOR(_EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_case_embeddings_skill_id", "case_embeddings", ["skill_id"])
    op.execute(
        "CREATE INDEX ix_case_embeddings_embedding_hnsw "
        "ON case_embeddings USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "generation_collapse_events",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("skill_id", sa.String(), nullable=False),
        sa.Column("generator_run_id", sa.String(), nullable=False),
        sa.Column("generation_mode", sa.String(), nullable=False),
        sa.Column("triggered_by", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("avg_distance_to_history", sa.Float(), nullable=True),
        sa.Column("intra_batch_distance", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("historical_count", sa.Integer(), nullable=False),
        sa.Column("new_case_count", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_generation_collapse_events_skill_id", "generation_collapse_events", ["skill_id"]
    )

    op.create_table(
        "canary_probe_history",
        sa.Column("probe_id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("image_ref", sa.String(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), server_default="[]"),
        sa.Column("probed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_canary_probe_history_run_id", "canary_probe_history", ["run_id"])
    op.create_index("ix_canary_probe_history_image_ref", "canary_probe_history", ["image_ref"])


def downgrade() -> None:
    op.drop_index("ix_canary_probe_history_image_ref", table_name="canary_probe_history")
    op.drop_index("ix_canary_probe_history_run_id", table_name="canary_probe_history")
    op.drop_table("canary_probe_history")
    op.drop_index("ix_generation_collapse_events_skill_id", table_name="generation_collapse_events")
    op.drop_table("generation_collapse_events")
    op.execute("DROP INDEX IF EXISTS ix_case_embeddings_embedding_hnsw")
    op.drop_index("ix_case_embeddings_skill_id", table_name="case_embeddings")
    op.drop_table("case_embeddings")
