"""0002 reporting tables

新增 `runs`、`dimension_results` 两张表，支撑 docs/dev/05 的 `ReportGenerator`
按 run_id 聚合各维度判定结果。docs/dev/04 原表清单未列出，随 05 文档新增
（符合 docs/dev/04 第 7 节"新增表新增 revision，不修改历史 revision"约定）。

Revision ID: 0002_reporting_tables
Revises: 0001_initial_schema
Create Date: 2026-09-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_reporting_tables"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("skill_id", sa.String(), nullable=False),
        sa.Column("skill_version_ref", sa.String(), nullable=False),
        sa.Column("suite_version_id", sa.String(), nullable=True),
        sa.Column("generation_mode", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_runs_skill_id", "runs", ["skill_id"])

    op.create_table(
        "dimension_results",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("dimension", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("findings", postgresql.JSONB(), server_default="[]"),
        sa.Column("blocking", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "dimension", name="uq_dimension_results_run_dimension"),
    )
    op.create_index("ix_dimension_results_run_id", "dimension_results", ["run_id"])


def downgrade() -> None:
    op.drop_table("dimension_results")
    op.drop_table("runs")
