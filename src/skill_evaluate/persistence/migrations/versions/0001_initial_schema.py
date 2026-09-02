"""0001 initial schema

对应 docs/dev/04 第 3 节业务表清单，字段与 skill_evaluate.persistence.models 保持
一一对应。启用 pgvector 扩展的第一条迁移语句沿用 docs/dev/01 第 5 节约定。

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "skills",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("skill_id", sa.String(), nullable=False),
        sa.Column("version_ref", sa.String(), nullable=False),
        sa.Column("root_path", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("body_markdown", sa.String(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("reference_files", postgresql.JSONB(), server_default="[]"),
        sa.Column("scripts", postgresql.JSONB(), server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("skill_id", "version_ref", name="uq_skills_skill_version"),
    )
    op.create_index("ix_skills_skill_id", "skills", ["skill_id"])

    op.create_table(
        "test_cases",
        sa.Column("case_id", sa.String(), primary_key=True),
        sa.Column("skill_id", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("split", sa.String(), nullable=False),
        sa.Column("prompt", sa.String(), nullable=False),
        sa.Column("expected_output", sa.String(), nullable=True),
        sa.Column("target_capability_ids", postgresql.JSONB(), server_default="[]"),
        sa.Column("negative_constraint_ids", postgresql.JSONB(), server_default="[]"),
        sa.Column("seed_anchor_id", sa.String(), nullable=True),
        sa.Column("generator_run_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_test_cases_skill_id", "test_cases", ["skill_id"])

    op.create_table(
        "test_suite_versions",
        sa.Column("suite_version_id", sa.String(), primary_key=True),
        sa.Column("skill_id", sa.String(), nullable=False),
        sa.Column("skill_version_ref", sa.String(), nullable=False),
        sa.Column("generation_mode", sa.String(), nullable=False),
        sa.Column("case_ids", postgresql.JSONB(), server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_test_suite_versions_skill_id", "test_suite_versions", ["skill_id"])
    op.create_index(
        "uq_test_suite_versions_active",
        "test_suite_versions",
        ["skill_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "execution_traces",
        sa.Column("trace_id", sa.String(), primary_key=True),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("run_index", sa.Integer(), nullable=False),
        sa.Column("backend_type", sa.String(), nullable=False),
        sa.Column("loaded_skill_md", sa.Boolean(), nullable=False),
        sa.Column("timing", postgresql.JSONB(), nullable=False),
        sa.Column("actions", postgresql.JSONB(), server_default="[]"),
        sa.Column("final_response", sa.String(), nullable=False),
        sa.Column("modified_files_manifest", postgresql.JSONB(), server_default="[]"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("case_id", "run_index", name="uq_execution_traces_case_run"),
    )
    op.create_index("ix_execution_traces_case_id", "execution_traces", ["case_id"])

    op.create_table(
        "capability_trees",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("skill_id", sa.String(), nullable=False),
        sa.Column("skill_version_ref", sa.String(), nullable=False),
        sa.Column("nodes", postgresql.JSONB(), server_default="[]"),
        sa.Column("negative_constraints", postgresql.JSONB(), server_default="[]"),
        sa.Column("combinatorial_pairs_covered", postgresql.JSONB(), server_default="[]"),
        sa.UniqueConstraint(
            "skill_id", "skill_version_ref", name="uq_capability_trees_skill_version"
        ),
    )
    op.create_index("ix_capability_trees_skill_id", "capability_trees", ["skill_id"])

    op.create_table(
        "judge_verdicts",
        sa.Column("verdict_id", sa.String(), primary_key=True),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reasoning", sa.String(), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_judge_verdicts_subject_id", "judge_verdicts", ["subject_id"])

    op.create_table(
        "consensus_results",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("verdict_ids", postgresql.JSONB(), server_default="[]"),
        sa.Column("consensus_reached", sa.Boolean(), nullable=False),
        sa.Column("final_status", sa.String(), nullable=False),
        sa.Column("dissenting_node", sa.String(), nullable=True),
    )
    op.create_index("ix_consensus_results_subject_id", "consensus_results", ["subject_id"])

    op.create_table(
        "security_findings",
        sa.Column("finding_id", sa.String(), primary_key=True),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("evidence", sa.String(), nullable=False),
        sa.Column("remediation_patch_id", sa.String(), nullable=True),
    )
    op.create_index("ix_security_findings_case_id", "security_findings", ["case_id"])
    op.create_index("ix_security_findings_severity", "security_findings", ["severity"])

    op.create_table(
        "assertion_specs",
        sa.Column("assertion_id", sa.String(), primary_key=True),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("strategy", sa.String(), nullable=False),
        sa.Column("template_ref", sa.String(), nullable=True),
        sa.Column("script_path", sa.String(), nullable=True),
        sa.Column("language", sa.String(), nullable=False, server_default="python"),
    )
    op.create_index("ix_assertion_specs_case_id", "assertion_specs", ["case_id"])

    op.create_table(
        "assertion_results",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "assertion_id",
            sa.String(),
            sa.ForeignKey("assertion_specs.assertion_id"),
            nullable=False,
        ),
        sa.Column("exit_code", sa.Integer(), nullable=False),
        sa.Column("stdout", sa.String(), nullable=False),
        sa.Column("stderr", sa.String(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_assertion_results_assertion_id", "assertion_results", ["assertion_id"])

    op.create_table(
        "pending_hooks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("run_index", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("wait_key", sa.String(), nullable=False, unique=True),
        sa.Column("status", sa.String(), nullable=False, server_default="waiting"),
        sa.Column("resume_payload", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "case_id", "run_index", name="uq_pending_hooks_key"),
    )
    op.create_index("ix_pending_hooks_run_id", "pending_hooks", ["run_id"])

    op.create_table(
        "human_approvals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("node_name", sa.String(), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("wait_key", sa.String(), nullable=False, unique=True),
        sa.Column("status", sa.String(), nullable=False, server_default="waiting"),
        sa.Column("resume_payload", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_human_approvals_run_id", "human_approvals", ["run_id"])


def downgrade() -> None:
    op.drop_table("human_approvals")
    op.drop_table("pending_hooks")
    op.drop_table("assertion_results")
    op.drop_table("assertion_specs")
    op.drop_table("security_findings")
    op.drop_table("consensus_results")
    op.drop_table("judge_verdicts")
    op.drop_table("capability_trees")
    op.drop_table("execution_traces")
    op.drop_table("test_suite_versions")
    op.drop_table("test_cases")
    op.drop_table("skills")
