"""0003 judge trust tables

新增 `golden_cases`、`judge_miss_records`、`judge_health_status` 三张表，支撑
docs/dev/08 的黄金基准盲测与失误率冻结机制。按 docs/dev/04 第 7 节约定以**新增
revision** 的方式追加，不修改任何历史 revision。

Revision ID: 0003_judge_trust_tables
Revises: 0002_reporting_tables
Create Date: 2026-09-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_judge_trust_tables"
down_revision: str | None = "0002_reporting_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "golden_cases",
        sa.Column("golden_id", sa.String(), primary_key=True),
        sa.Column("template_key", sa.String(), nullable=False),
        sa.Column("content", postgresql.JSONB(), server_default="{}"),
        sa.Column("human_labeled_status", sa.String(), nullable=False),
        sa.Column("human_labeled_reasoning", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_golden_cases_template_key", "golden_cases", ["template_key"])

    op.create_table(
        "judge_miss_records",
        sa.Column("miss_id", sa.String(), primary_key=True),
        sa.Column("golden_id", sa.String(), nullable=False),
        sa.Column("judge_output_status", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=False),
        sa.Column("temperature_bucket", sa.String(), nullable=False),
        sa.Column("is_miss", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_judge_miss_records_golden_id", "judge_miss_records", ["golden_id"])
    op.create_index("ix_judge_miss_records_model", "judge_miss_records", ["model"])
    op.create_index(
        "ix_judge_miss_records_temperature_bucket", "judge_miss_records", ["temperature_bucket"]
    )

    op.create_table(
        "judge_health_status",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("temperature_bucket", sa.String(), nullable=False),
        sa.Column("frozen", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("miss_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("window_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("model", "temperature_bucket", name="uq_judge_health_config"),
    )


def downgrade() -> None:
    op.drop_table("judge_health_status")
    op.drop_table("judge_miss_records")
    op.drop_table("golden_cases")
