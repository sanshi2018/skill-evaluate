"""0008 test case suggestions

docs/dev/17（模块七：用例集瘦身与动态演进）新增 `test_case_suggestions` 一张表，
承载"孤儿用例建议淘汰"这类**非阻塞**的人工待办。纯新增，不改任何既有表结构，
也不需要数据回填（符合 docs/dev/04 第 7 节"新增表新增 revision"的约定）。

本模块的另外两件产出**不需要迁移**，这里一并说明，免得后来人找不到对应的 DDL：

- 冗余用例折叠只是把 `test_cases.split` 改写成 `cold`，该列与该枚举值在
  0001 就已存在（docs/dev/02 早就为模块七留好了这个"惰性过滤"机制）；
- 组合能力覆盖矩阵写的是 `capability_trees.combinatorial_pairs_covered`，
  同样在 0001 里建好了，此前一直是空列表。

`(case_id, suggestion_type)` 唯一约束是本次迁移的关键：同一条孤儿用例在连续多次
评测中都会被重新检出，去重必须发生在库层面。放在应用层"先查后写"在多个 run 并发
评测同一个 Skill 时必然漏掉，人就会在工作台上看到同一条待办的若干副本。

Revision ID: 0008_test_case_suggestions
Revises: 0007_security_red_team_columns
Create Date: 2026-09-10

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_test_case_suggestions"
down_revision: str | None = "0007_security_red_team_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "test_case_suggestions",
        sa.Column("suggestion_id", sa.String(), primary_key=True),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("suggestion_type", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "case_id", "suggestion_type", name="uq_test_case_suggestions_case_type"
        ),
    )
    op.create_index("ix_test_case_suggestions_case_id", "test_case_suggestions", ["case_id"])
    # 工作台主查询是"列出所有 pending"，pending 只占全表一小部分（已处理的长期留存
    # 作审计），是索引最划算的形状。
    op.create_index("ix_test_case_suggestions_status", "test_case_suggestions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_test_case_suggestions_status", table_name="test_case_suggestions")
    op.drop_index("ix_test_case_suggestions_case_id", table_name="test_case_suggestions")
    op.drop_table("test_case_suggestions")
