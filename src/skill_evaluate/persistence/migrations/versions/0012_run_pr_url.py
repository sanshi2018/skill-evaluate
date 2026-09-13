"""0012 run pr url

docs/dev/24（主图编排与 CI/CD 落地）第 5 节：补丁转 PR 之后要把 PR 链接记回本次运行
（`RunRepository.record_pr_url()`）。纯追加一个 nullable 列，不改既有列、无需数据回填，
满足 docs/dev/24 第 7 节发布清单"迁移不含破坏性变更、紧邻一次回滚无需降级脚本"——旧版本
代码不读这一列，照常跑在新 schema 上。

Revision ID: 0012_run_pr_url
Revises: 0011_search_documents
Create Date: 2026-09-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_run_pr_url"
down_revision: str | None = "0011_search_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("pr_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "pr_url")
