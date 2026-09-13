"""0010 approval workbench

docs/dev/22（容错机制与人工审批闭环）新增两张表，纯新增、不改任何既有表结构、无需数据回填：

- `pending_approvals`：统一的人工审批卡片（六处人工介入场景收口到同一张表）；
- `approval_decisions`：人对卡片的决定（审计，一张卡片一条）。

## 与 docs/dev/22 正文的偏差：不删除 `human_approvals`

正文写"替换 docs/dev/04 占位的 `human_approvals` 表定义"。该表在实现中已经是
`resolve_suspension()` 的挂起账本（waiting → resolved 的幂等状态迁移），与 Hermes/Llama
回调的 `pending_hooks` 同构。替换它需要改写已验证的唤醒路径，收益只是少一张表，
因此保留，分层说明见 `state/approval.py` 模块头。

Revision ID: 0010_approval_workbench
Revises: 0009_generator_trust_and_preflight
Create Date: 2026-09-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_approval_workbench"
down_revision: str | None = "0009_generator_trust_and_preflight"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_approvals",
        sa.Column("approval_id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("wait_key", sa.String(), nullable=False, unique=True),
        sa.Column("decision_type", sa.String(), nullable=False),
        sa.Column("node_name", sa.String(), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("context_summary", sa.String(), nullable=False),
        sa.Column("context_ref", postgresql.JSONB(), server_default="{}"),
        sa.Column("blocking", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pending_approvals_run_id", "pending_approvals", ["run_id"])
    # 工作台主查询是"列出所有 pending"，pending 只占全表一小部分。
    op.create_index("ix_pending_approvals_status", "pending_approvals", ["status"])

    op.create_table(
        "approval_decisions",
        sa.Column("decision_id", sa.String(), primary_key=True),
        sa.Column(
            "approval_id",
            sa.String(),
            sa.ForeignKey("pending_approvals.approval_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,  # 一张卡片只能被决定一次，并发决策由库层裁决先到者
        ),
        sa.Column("decided_by", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("approval_decisions")
    op.drop_index("ix_pending_approvals_status", table_name="pending_approvals")
    op.drop_index("ix_pending_approvals_run_id", table_name="pending_approvals")
    op.drop_table("pending_approvals")
