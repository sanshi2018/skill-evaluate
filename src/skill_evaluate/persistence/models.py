"""SQLAlchemy ORM 模型（docs/dev/04 第 3 节）。

与 `state/` 下的 Pydantic 模型一一对应，职责分离：Pydantic 负责跨进程/跨节点
的数据契约，SQLAlchemy 负责落盘。JSONB 使用原则：凡是"随该记录一起读写、
不需要独立跨记录检索"的嵌套结构（如 `ExecutionTrace.actions`、
`CapabilityTree.nodes`）用 JSONB 内嵌，不拆多表。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_str() -> str:
    return str(uuid.uuid4())


class SkillORM(Base):
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("skill_id", "version_ref", name="uq_skills_skill_version"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid_str)
    skill_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    version_ref: Mapped[str] = mapped_column(String, nullable=False)
    root_path: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    body_markdown: Mapped[str] = mapped_column(String, nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_files: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    scripts: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TestCaseORM(Base):
    __tablename__ = "test_cases"

    case_id: Mapped[str] = mapped_column(String, primary_key=True)
    skill_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    split: Mapped[str] = mapped_column(String, nullable=False)
    prompt: Mapped[str] = mapped_column(String, nullable=False)
    expected_output: Mapped[str | None] = mapped_column(String, nullable=True)
    target_capability_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    negative_constraint_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    seed_anchor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    generator_run_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TestSuiteVersionORM(Base):
    __tablename__ = "test_suite_versions"
    __table_args__ = (
        Index(
            "uq_test_suite_versions_active",
            "skill_id",
            unique=True,
            postgresql_where="is_active",
        ),
    )

    suite_version_id: Mapped[str] = mapped_column(String, primary_key=True)
    skill_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    skill_version_ref: Mapped[str] = mapped_column(String, nullable=False)
    generation_mode: Mapped[str] = mapped_column(String, nullable=False)
    case_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ExecutionTraceORM(Base):
    __tablename__ = "execution_traces"
    __table_args__ = (
        UniqueConstraint("case_id", "run_index", name="uq_execution_traces_case_run"),
    )

    trace_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    run_index: Mapped[int] = mapped_column(Integer, nullable=False)
    backend_type: Mapped[str] = mapped_column(String, nullable=False)
    loaded_skill_md: Mapped[bool] = mapped_column(Boolean, nullable=False)
    timing: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    actions: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    final_response: Mapped[str] = mapped_column(String, nullable=False)
    modified_files_manifest: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CapabilityTreeORM(Base):
    __tablename__ = "capability_trees"
    __table_args__ = (
        UniqueConstraint("skill_id", "skill_version_ref", name="uq_capability_trees_skill_version"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid_str)
    skill_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    skill_version_ref: Mapped[str] = mapped_column(String, nullable=False)
    nodes: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    negative_constraints: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    combinatorial_pairs_covered: Mapped[list[Any]] = mapped_column(JSONB, default=list)


class JudgeVerdictORM(Base):
    __tablename__ = "judge_verdicts"

    verdict_id: Mapped[str] = mapped_column(String, primary_key=True)
    subject_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reasoning: Mapped[str] = mapped_column(String, nullable=False)
    temperature: Mapped[float] = mapped_column(Float, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConsensusResultORM(Base):
    __tablename__ = "consensus_results"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid_str)
    subject_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    verdict_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    consensus_reached: Mapped[bool] = mapped_column(Boolean, nullable=False)
    final_status: Mapped[str] = mapped_column(String, nullable=False)
    dissenting_node: Mapped[str | None] = mapped_column(String, nullable=True)


class SecurityFindingORM(Base):
    __tablename__ = "security_findings"

    finding_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, index=True, nullable=False)
    evidence: Mapped[str] = mapped_column(String, nullable=False)
    remediation_patch_id: Mapped[str | None] = mapped_column(String, nullable=True)


class AssertionSpecORM(Base):
    __tablename__ = "assertion_specs"

    assertion_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    strategy: Mapped[str] = mapped_column(String, nullable=False)
    template_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    script_path: Mapped[str | None] = mapped_column(String, nullable=True)
    language: Mapped[str] = mapped_column(String, default="python", nullable=False)


class AssertionResultORM(Base):
    __tablename__ = "assertion_results"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid_str)
    assertion_id: Mapped[str] = mapped_column(
        String, ForeignKey("assertion_specs.assertion_id"), index=True, nullable=False
    )
    exit_code: Mapped[int] = mapped_column(Integer, nullable=False)
    stdout: Mapped[str] = mapped_column(String, nullable=False)
    stderr: Mapped[str] = mapped_column(String, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)


class PendingHookORM(Base):
    """外部事件唤醒机制的等待记录（docs/dev/04 第 5 节）。无独立 Pydantic 模型。"""

    __tablename__ = "pending_hooks"
    __table_args__ = (
        UniqueConstraint("run_id", "case_id", "run_index", name="uq_pending_hooks_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid_str)
    run_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    case_id: Mapped[str] = mapped_column(String, nullable=False)
    run_index: Mapped[int] = mapped_column(Integer, nullable=False)
    thread_id: Mapped[str] = mapped_column(String, nullable=False)
    wait_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String, default="waiting", nullable=False)
    resume_payload: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunORM(Base):
    """一次流水线运行的身份记录（docs/dev/02 `PipelineState.run_id` 的落盘锚点）。

    docs/dev/04 原表清单未列出本表；`PipelineState`/`pending_hooks` 均以 run_id
    为核心标识，报告生成器（docs/dev/05）需要按 run_id 反查 skill_id 等元信息，
    这里以新增表的方式补齐（符合"新增字段不改语义"的追加约定，见 docs/dev/01
    第 9 节、docs/dev/04 第 7 节）。
    """

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    skill_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    skill_version_ref: Mapped[str] = mapped_column(String, nullable=False)
    suite_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    generation_mode: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DimensionResultORM(Base):
    """各评测维度节点的判定结果（docs/dev/05 `DimensionResult`，供 `ReportGenerator`
    聚合为 `BenchmarkReport`）。docs/dev/04 原表清单未列出，随 05 文档新增。
    """

    __tablename__ = "dimension_results"
    __table_args__ = (
        UniqueConstraint("run_id", "dimension", name="uq_dimension_results_run_dimension"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid_str)
    run_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    dimension: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    findings: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HumanApprovalORM(Base):
    """人工审批闭环占位表（docs/dev/22 详述），本文档先建表占位。"""

    __tablename__ = "human_approvals"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid_str)
    run_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    node_name: Mapped[str] = mapped_column(String, nullable=False)
    thread_id: Mapped[str] = mapped_column(String, nullable=False)
    wait_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String, default="waiting", nullable=False)
    resume_payload: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
