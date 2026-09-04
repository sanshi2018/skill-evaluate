"""0006 instruction control columns

docs/dev/13（模块三：指令控制度与执行效果评测）给 `test_cases` 追加一列：

- `probe_target_reference`：渐进式披露**动态**探查用例指向的参考文件路径
  （如 `references/errors.md`）。探查判定"该读的读了没有"必须与出题时的意图
  一一对应，因此这个目标在用例落库时就固定下来，而不是事后再猜。

纯追加列且 nullable：既有几千条 POSITIVE/NEGATIVE 用例的语义不变（恒为 NULL =
"这条用例没有探查目标"），不需要数据回填。

Revision ID: 0006_instruction_control_columns
Revises: 0005_validator_assertion_columns
Create Date: 2026-09-04

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_instruction_control_columns"
down_revision: str | None = "0005_validator_assertion_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("test_cases", sa.Column("probe_target_reference", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("test_cases", "probe_target_reference")
