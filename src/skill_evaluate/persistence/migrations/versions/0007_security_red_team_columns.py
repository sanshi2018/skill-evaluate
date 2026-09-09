"""0007 security red team columns

docs/dev/15（模块五：安全性与注入风险红蓝对抗评测）追加两列，都是**纯追加、
nullable、无需数据回填**：

- `test_cases.attack_subtype`：一条 ADVERSARIAL 用例具体在打哪个攻击面
  （`AttackSubtype` 枚举值）。五条探测支路按它切分自己该跑的用例子集，因此建
  索引；其余类别恒为 NULL。
- `judge_verdicts.severity`：落地 docs/dev/07 预留的 `ReviewTemplate.to_severity`
  （docs/dev/15 第 10.1 节）。只有 `security_severity_rating` 这类"结论不是通过/
  失败而是多严重"的模板会填它，其余判定为 NULL。

为什么 `severity` 不给默认值：给一个 `'low'` 默认值会让"这条判定没有严重级别"
与"这条判定被评为低危"在 SQL 层面分不开，而报告正是按严重级别做聚合的。

Revision ID: 0007_security_red_team_columns
Revises: 0006_instruction_control_columns
Create Date: 2026-09-09

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_security_red_team_columns"
down_revision: str | None = "0006_instruction_control_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("test_cases", sa.Column("attack_subtype", sa.String(), nullable=True))
    op.create_index("ix_test_cases_attack_subtype", "test_cases", ["attack_subtype"])
    op.add_column("judge_verdicts", sa.Column("severity", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("judge_verdicts", "severity")
    op.drop_index("ix_test_cases_attack_subtype", table_name="test_cases")
    op.drop_column("test_cases", "attack_subtype")
