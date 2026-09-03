"""0005 validator assertion columns

docs/dev/10（Validator Agent）给 `assertion_specs` 追加三列：

- `script_content`：要下发到沙箱执行的脚本正文。必须落库——断点恢复后重新下发
  时要用**同一份**脚本，重新生成一份就不是同一条断言了。
- `failure_reason`：`strategy='none'` 的原因（本来就不需要断言 / 生成失败降级），
  报告侧据此标记"断言生成失败"。
- `created_at`：生成时间，可空以兼容 0001 时期已存在的行。

纯追加列（全部 nullable），不改动既有列的类型与语义。

Revision ID: 0005_validator_assertion_columns
Revises: 0004_optimizer_patch_tables
Create Date: 2026-09-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_validator_assertion_columns"
down_revision: str | None = "0004_optimizer_patch_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("assertion_specs", sa.Column("script_content", sa.String(), nullable=True))
    op.add_column("assertion_specs", sa.Column("failure_reason", sa.String(), nullable=True))
    op.add_column(
        "assertion_specs",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assertion_specs", "created_at")
    op.drop_column("assertion_specs", "failure_reason")
    op.drop_column("assertion_specs", "script_content")
