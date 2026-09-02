"""0004 optimizer patch tables

新增 `patches`、`patch_application_results`、`node_retry_counts` 三张表，支撑
docs/dev/09 的补丁记录与闭环重试计数。同样以新增 revision 追加。

Revision ID: 0004_optimizer_patch_tables
Revises: 0003_judge_trust_tables
Create Date: 2026-09-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_optimizer_patch_tables"
down_revision: str | None = "0003_judge_trust_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "patches",
        sa.Column("patch_id", sa.String(), primary_key=True),
        sa.Column("skill_id", sa.String(), nullable=False),
        sa.Column("base_skill_version_ref", sa.String(), nullable=False),
        sa.Column("patch_type", sa.String(), nullable=False),
        sa.Column("target_path", sa.String(), nullable=False),
        sa.Column("diff", sa.String(), nullable=False),
        sa.Column("rationale", sa.String(), nullable=False),
        sa.Column("triggered_by_finding_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_patches_skill_id", "patches", ["skill_id"])
    op.create_index("ix_patches_finding_id", "patches", ["triggered_by_finding_id"])

    op.create_table(
        "patch_application_results",
        sa.Column("patch_id", sa.String(), primary_key=True),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("regression_passed", sa.Boolean(), nullable=True),
        sa.Column("working_skill_version_ref", sa.String(), nullable=True),
        sa.Column("detail", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "node_retry_counts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("node_name", sa.String(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "node_name", name="uq_node_retry_counts_key"),
    )
    op.create_index("ix_node_retry_counts_run_id", "node_retry_counts", ["run_id"])


def downgrade() -> None:
    op.drop_table("node_retry_counts")
    op.drop_table("patch_application_results")
    op.drop_table("patches")
