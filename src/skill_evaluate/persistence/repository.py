"""统一 Repository 层（docs/dev/04 第 4 节）。

`nodes/`、`agents/` 下的代码一律通过 Repository 存取，不直接写 SQL/ORM
查询语句。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TypedDict

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from skill_evaluate.errors import PersistenceError
from skill_evaluate.persistence.db import new_session
from skill_evaluate.persistence.models import (
    AssertionResultORM,
    AssertionSpecORM,
    CapabilityTreeORM,
    ConsensusResultORM,
    DimensionResultORM,
    ExecutionTraceORM,
    HumanApprovalORM,
    JudgeVerdictORM,
    PendingHookORM,
    RunORM,
    SecurityFindingORM,
    SkillORM,
    TestCaseORM,
    TestSuiteVersionORM,
)
from skill_evaluate.state.assertion import AssertionResult, AssertionSpec
from skill_evaluate.state.capability import CapabilityTree
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.security import SecurityFinding
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion
from skill_evaluate.state.trace import ExecutionTrace


class SkillRepository:
    async def save(self, skill: SkillDefinition) -> None:
        async with new_session() as session:
            stmt = pg_insert(SkillORM).values(
                skill_id=skill.skill_id,
                version_ref=skill.version_ref,
                root_path=skill.root_path,
                description=skill.description,
                body_markdown=skill.body_markdown,
                line_count=skill.line_count,
                token_count=skill.token_count,
                reference_files=[f.model_dump(mode="json") for f in skill.reference_files],
                scripts=[s.model_dump(mode="json") for s in skill.scripts],
                created_at=datetime.now(UTC),
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_skills_skill_version",
                set_={
                    "root_path": stmt.excluded.root_path,
                    "description": stmt.excluded.description,
                    "body_markdown": stmt.excluded.body_markdown,
                    "line_count": stmt.excluded.line_count,
                    "token_count": stmt.excluded.token_count,
                    "reference_files": stmt.excluded.reference_files,
                    "scripts": stmt.excluded.scripts,
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def get(self, skill_id: str, version_ref: str) -> SkillDefinition | None:
        async with new_session() as session:
            row = (
                await session.execute(
                    select(SkillORM).where(
                        SkillORM.skill_id == skill_id, SkillORM.version_ref == version_ref
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return SkillDefinition(
                skill_id=row.skill_id,
                version_ref=row.version_ref,
                root_path=row.root_path,
                description=row.description,
                body_markdown=row.body_markdown,
                line_count=row.line_count,
                token_count=row.token_count,
                reference_files=row.reference_files or [],
                scripts=row.scripts or [],
            )


class TestCaseRepository:
    async def save(self, case: TestCase) -> None:
        async with new_session() as session:
            stmt = pg_insert(TestCaseORM).values(**_test_case_values(case))
            stmt = stmt.on_conflict_do_update(
                index_elements=["case_id"], set_=_test_case_values(case)
            )
            await session.execute(stmt)
            await session.commit()

    async def save_many(self, cases: list[TestCase]) -> None:
        for case in cases:
            await self.save(case)

    async def list_by_skill(self, skill_id: str) -> list[TestCase]:
        async with new_session() as session:
            rows = (
                await session.execute(select(TestCaseORM).where(TestCaseORM.skill_id == skill_id))
            ).scalars()
            return [_orm_to_test_case(r) for r in rows]

    async def list_by_ids(self, case_ids: list[str]) -> list[TestCase]:
        if not case_ids:
            return []
        async with new_session() as session:
            rows = (
                await session.execute(select(TestCaseORM).where(TestCaseORM.case_id.in_(case_ids)))
            ).scalars()
            return [_orm_to_test_case(r) for r in rows]


def _test_case_values(case: TestCase) -> dict[str, object]:
    return {
        "case_id": case.case_id,
        "skill_id": case.skill_id,
        "category": case.category.value,
        "split": case.split.value,
        "prompt": case.prompt,
        "expected_output": case.expected_output,
        "target_capability_ids": case.target_capability_ids,
        "negative_constraint_ids": case.negative_constraint_ids,
        "seed_anchor_id": case.seed_anchor_id,
        "generator_run_id": case.generator_run_id,
        "created_at": case.created_at,
    }


def _orm_to_test_case(row: TestCaseORM) -> TestCase:
    return TestCase(
        case_id=row.case_id,
        skill_id=row.skill_id,
        category=row.category,
        split=row.split,
        prompt=row.prompt,
        expected_output=row.expected_output,
        target_capability_ids=row.target_capability_ids or [],
        negative_constraint_ids=row.negative_constraint_ids or [],
        seed_anchor_id=row.seed_anchor_id,
        generator_run_id=row.generator_run_id,
        created_at=row.created_at,
    )


class TestSuiteRepository:
    async def get_active_version(
        self, skill_id: str, skill_version_ref: str | None = None
    ) -> TestSuiteVersion | None:
        """查询当前 active 的测试集版本。

        - 传入 `skill_version_ref`：只有绑定版本**完全匹配**时才返回，不匹配按
          "没有可直接复用的版本"处理（返回 None）。
        - 传入 None：忽略版本号，返回任意 active 版本——docs/dev/06 用这一路查询
          区分"从来没生成过"（None）与"生成过但版本漂移了"（有值 -> staleness
          告警，但按约定**不**自动重新生成）。

        `skill_version_ref` 由 docs/dev/06 落地时从必填收窄为可选（追加式变更，
        原有按位置传参的调用方语义不变）。
        """
        async with new_session() as session:
            row = (
                await session.execute(
                    select(TestSuiteVersionORM).where(
                        TestSuiteVersionORM.skill_id == skill_id,
                        TestSuiteVersionORM.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if skill_version_ref is not None and row.skill_version_ref != skill_version_ref:
                return None
            return TestSuiteVersion(
                suite_version_id=row.suite_version_id,
                skill_id=row.skill_id,
                skill_version_ref=row.skill_version_ref,
                generation_mode=row.generation_mode,
                case_ids=row.case_ids or [],
                created_at=row.created_at,
                is_active=row.is_active,
            )

    async def activate_new_version(self, version: TestSuiteVersion) -> None:
        """事务内：新版本写入 + 旧 active 版本置 false，保证唯一索引不冲突。"""
        async with new_session() as session, session.begin():
            await session.execute(
                update(TestSuiteVersionORM)
                .where(
                    TestSuiteVersionORM.skill_id == version.skill_id,
                    TestSuiteVersionORM.is_active.is_(True),
                )
                .values(is_active=False)
            )
            session.add(
                TestSuiteVersionORM(
                    suite_version_id=version.suite_version_id,
                    skill_id=version.skill_id,
                    skill_version_ref=version.skill_version_ref,
                    generation_mode=version.generation_mode,
                    case_ids=version.case_ids,
                    created_at=version.created_at,
                    is_active=version.is_active,
                )
            )


class TraceRepository:
    async def save(self, trace: ExecutionTrace) -> None:
        async with new_session() as session:
            stmt = pg_insert(ExecutionTraceORM).values(**_trace_values(trace))
            stmt = stmt.on_conflict_do_update(
                constraint="uq_execution_traces_case_run", set_=_trace_values(trace)
            )
            await session.execute(stmt)
            await session.commit()

    async def get(self, trace_id: str) -> ExecutionTrace | None:
        async with new_session() as session:
            row = (
                await session.execute(
                    select(ExecutionTraceORM).where(ExecutionTraceORM.trace_id == trace_id)
                )
            ).scalar_one_or_none()
            return _orm_to_trace(row) if row else None

    async def list_by_case(self, case_id: str) -> list[ExecutionTrace]:
        async with new_session() as session:
            rows = (
                await session.execute(
                    select(ExecutionTraceORM).where(ExecutionTraceORM.case_id == case_id)
                )
            ).scalars()
            return [_orm_to_trace(r) for r in rows]


def _trace_values(trace: ExecutionTrace) -> dict[str, object]:
    return {
        "trace_id": trace.trace_id,
        "case_id": trace.case_id,
        "run_index": trace.run_index,
        "backend_type": trace.backend_type,
        "loaded_skill_md": trace.loaded_skill_md,
        "timing": trace.timing.model_dump(mode="json"),
        "actions": [a.model_dump(mode="json") for a in trace.actions],
        "final_response": trace.final_response,
        "modified_files_manifest": [
            m.model_dump(mode="json") for m in trace.modified_files_manifest
        ],
        "started_at": trace.started_at,
        "finished_at": trace.finished_at,
    }


def _orm_to_trace(row: ExecutionTraceORM) -> ExecutionTrace:
    return ExecutionTrace(
        trace_id=row.trace_id,
        case_id=row.case_id,
        run_index=row.run_index,
        backend_type=row.backend_type,
        loaded_skill_md=row.loaded_skill_md,
        timing=row.timing,
        actions=row.actions or [],
        final_response=row.final_response,
        modified_files_manifest=row.modified_files_manifest or [],
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


class CapabilityRepository:
    async def save(self, tree: CapabilityTree) -> str:
        async with new_session() as session:
            stmt = pg_insert(CapabilityTreeORM).values(
                skill_id=tree.skill_id,
                skill_version_ref=tree.skill_version_ref,
                nodes=[n.model_dump(mode="json") for n in tree.nodes],
                negative_constraints=[c.model_dump(mode="json") for c in tree.negative_constraints],
                combinatorial_pairs_covered=[list(p) for p in tree.combinatorial_pairs_covered],
            )
            upsert_stmt = stmt.on_conflict_do_update(
                constraint="uq_capability_trees_skill_version",
                set_={
                    "nodes": stmt.excluded.nodes,
                    "negative_constraints": stmt.excluded.negative_constraints,
                    "combinatorial_pairs_covered": stmt.excluded.combinatorial_pairs_covered,
                },
            ).returning(CapabilityTreeORM.id)
            result = await session.execute(upsert_stmt)
            await session.commit()
            return str(result.scalar_one())

    async def get(self, skill_id: str, skill_version_ref: str) -> CapabilityTree | None:
        async with new_session() as session:
            row = (
                await session.execute(
                    select(CapabilityTreeORM).where(
                        CapabilityTreeORM.skill_id == skill_id,
                        CapabilityTreeORM.skill_version_ref == skill_version_ref,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return CapabilityTree(
                skill_id=row.skill_id,
                skill_version_ref=row.skill_version_ref,
                nodes=row.nodes or [],
                negative_constraints=row.negative_constraints or [],
                combinatorial_pairs_covered=[
                    tuple(p) for p in (row.combinatorial_pairs_covered or [])
                ],
            )


class JudgeRepository:
    async def save_verdict(self, verdict: JudgeVerdict) -> None:
        async with new_session() as session:
            stmt = pg_insert(JudgeVerdictORM).values(
                verdict_id=verdict.verdict_id,
                subject_id=verdict.subject_id,
                status=verdict.status.value,
                reasoning=verdict.reasoning,
                temperature=verdict.temperature,
                model=verdict.model,
                created_at=verdict.created_at,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["verdict_id"])
            await session.execute(stmt)
            await session.commit()

    async def list_verdicts(self, subject_id: str) -> list[JudgeVerdict]:
        async with new_session() as session:
            rows = (
                await session.execute(
                    select(JudgeVerdictORM).where(JudgeVerdictORM.subject_id == subject_id)
                )
            ).scalars()
            return [
                JudgeVerdict(
                    verdict_id=r.verdict_id,
                    subject_id=r.subject_id,
                    status=r.status,
                    reasoning=r.reasoning,
                    temperature=r.temperature,
                    model=r.model,
                    created_at=r.created_at,
                )
                for r in rows
            ]

    async def save_consensus(self, consensus: ConsensusResult) -> None:
        async with new_session() as session:
            session.add(
                ConsensusResultORM(
                    subject_id=consensus.subject_id,
                    verdict_ids=[v.verdict_id for v in consensus.verdicts],
                    consensus_reached=consensus.consensus_reached,
                    final_status=consensus.final_status.value,
                    dissenting_node=consensus.dissenting_node,
                )
            )
            await session.commit()


class SecurityFindingRepository:
    async def save(self, finding: SecurityFinding) -> None:
        async with new_session() as session:
            stmt = pg_insert(SecurityFindingORM).values(
                finding_id=finding.finding_id,
                case_id=finding.case_id,
                category=finding.category.value,
                severity=finding.severity.value,
                evidence=finding.evidence,
                remediation_patch_id=finding.remediation_patch_id,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["finding_id"],
                set_={"remediation_patch_id": stmt.excluded.remediation_patch_id},
            )
            await session.execute(stmt)
            await session.commit()

    async def summarize_by_severity(self, skill_id: str | None = None) -> dict[str, int]:
        """供 docs/dev/05 `BenchmarkReport.security_findings_summary` 消费（docs/dev/15 落库时保证
        `severity` 字段可被正确 group by）。
        """
        async with new_session() as session:
            rows = (await session.execute(select(SecurityFindingORM.severity))).scalars()
            summary: dict[str, int] = {}
            for severity in rows:
                summary[severity] = summary.get(severity, 0) + 1
            return summary


class AssertionRepository:
    async def save_spec(self, spec: AssertionSpec) -> None:
        async with new_session() as session:
            stmt = pg_insert(AssertionSpecORM).values(
                assertion_id=spec.assertion_id,
                case_id=spec.case_id,
                strategy=spec.strategy.value,
                template_ref=spec.template_ref,
                script_path=spec.script_path,
                language=spec.language,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["assertion_id"])
            await session.execute(stmt)
            await session.commit()

    async def save_result(self, result: AssertionResult) -> None:
        async with new_session() as session:
            session.add(
                AssertionResultORM(
                    assertion_id=result.assertion_id,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    passed=result.passed,
                )
            )
            await session.commit()


class PendingHookRepository:
    """`pending_hooks` 表存取（docs/dev/04 第 5 节外部事件唤醒机制）。"""

    async def create(
        self, *, run_id: str, case_id: str, run_index: int, thread_id: str, wait_key: str
    ) -> None:
        async with new_session() as session:
            now = datetime.now(UTC)
            stmt = pg_insert(PendingHookORM).values(
                run_id=run_id,
                case_id=case_id,
                run_index=run_index,
                thread_id=thread_id,
                wait_key=wait_key,
                status="waiting",
                created_at=now,
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["wait_key"])
            await session.execute(stmt)
            await session.commit()

    async def mark_resolved(self, wait_key: str, resume_payload: str) -> bool:
        """返回 True 表示本次调用成功把状态从 waiting 迁移到 resolved；
        返回 False 表示记录不存在或已是非 waiting 状态（重复回调，见 docs/dev/04 第 6 节）。
        """
        async with new_session() as session:
            result = await session.execute(
                update(PendingHookORM)
                .where(PendingHookORM.wait_key == wait_key, PendingHookORM.status == "waiting")
                .values(
                    status="resolved", resume_payload=resume_payload, updated_at=datetime.now(UTC)
                )
            )
            await session.commit()
            return result.rowcount > 0  # type: ignore[attr-defined,no-any-return]

    async def list_stale_waiting(self, older_than_seconds: int) -> list[dict[str, object]]:
        """供 `scripts/pending_hooks_reaper.py` 巡检使用。"""
        async with new_session() as session:
            rows = (
                await session.execute(
                    select(PendingHookORM).where(PendingHookORM.status == "waiting")
                )
            ).scalars()
            now = datetime.now(UTC)
            stale = []
            for r in rows:
                created_at = (
                    r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=UTC)
                )
                if (now - created_at).total_seconds() >= older_than_seconds:
                    stale.append(
                        {
                            "run_id": r.run_id,
                            "case_id": r.case_id,
                            "run_index": r.run_index,
                            "thread_id": r.thread_id,
                            "wait_key": r.wait_key,
                        }
                    )
            return stale


class HumanApprovalRepository:
    """`human_approvals` 表占位存取（docs/dev/22 详述具体业务字段与 API）。"""

    async def create(self, *, run_id: str, node_name: str, thread_id: str, wait_key: str) -> None:
        async with new_session() as session:
            now = datetime.now(UTC)
            stmt = pg_insert(HumanApprovalORM).values(
                run_id=run_id,
                node_name=node_name,
                thread_id=thread_id,
                wait_key=wait_key,
                status="waiting",
                created_at=now,
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["wait_key"])
            await session.execute(stmt)
            await session.commit()

    async def mark_resolved(self, wait_key: str, resume_payload: str) -> bool:
        async with new_session() as session:
            result = await session.execute(
                update(HumanApprovalORM)
                .where(HumanApprovalORM.wait_key == wait_key, HumanApprovalORM.status == "waiting")
                .values(
                    status="resolved", resume_payload=resume_payload, updated_at=datetime.now(UTC)
                )
            )
            await session.commit()
            return result.rowcount > 0  # type: ignore[attr-defined,no-any-return]


class RunInfo(TypedDict):
    run_id: str
    skill_id: str
    skill_version_ref: str
    suite_version_id: str | None
    generation_mode: str
    created_at: datetime


class RunRepository:
    """`runs` 表存取：记录一次流水线运行的身份信息（docs/dev/05 `ReportGenerator.build()`
    据此反查 skill_id/skill_version_ref/suite_version_id）。
    """

    async def create(
        self,
        *,
        run_id: str,
        skill_id: str,
        skill_version_ref: str,
        generation_mode: str,
        suite_version_id: str | None = None,
    ) -> None:
        async with new_session() as session:
            stmt = pg_insert(RunORM).values(
                run_id=run_id,
                skill_id=skill_id,
                skill_version_ref=skill_version_ref,
                suite_version_id=suite_version_id,
                generation_mode=generation_mode,
                created_at=datetime.now(UTC),
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["run_id"])
            await session.execute(stmt)
            await session.commit()

    async def set_suite_version(self, run_id: str, suite_version_id: str) -> None:
        async with new_session() as session:
            await session.execute(
                update(RunORM)
                .where(RunORM.run_id == run_id)
                .values(suite_version_id=suite_version_id)
            )
            await session.commit()

    async def get(self, run_id: str) -> RunInfo | None:
        async with new_session() as session:
            row = (
                await session.execute(select(RunORM).where(RunORM.run_id == run_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "run_id": row.run_id,
                "skill_id": row.skill_id,
                "skill_version_ref": row.skill_version_ref,
                "suite_version_id": row.suite_version_id,
                "generation_mode": row.generation_mode,
                "created_at": row.created_at,
            }


class DimensionResultRepository:
    """`dimension_results` 表存取（docs/dev/05 第 6 节 `record_dimension_result()` 的落库实现）。"""

    async def save(
        self,
        *,
        run_id: str,
        dimension: str,
        status: str,
        score: float | None,
        findings: list[str],
        blocking: bool,
    ) -> None:
        async with new_session() as session:
            stmt = pg_insert(DimensionResultORM).values(
                run_id=run_id,
                dimension=dimension,
                status=status,
                score=score,
                findings=findings,
                blocking=blocking,
                created_at=datetime.now(UTC),
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_dimension_results_run_dimension",
                set_={
                    "status": stmt.excluded.status,
                    "score": stmt.excluded.score,
                    "findings": stmt.excluded.findings,
                    "blocking": stmt.excluded.blocking,
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def list_by_run(self, run_id: str) -> list[dict[str, object]]:
        async with new_session() as session:
            rows = (
                await session.execute(
                    select(DimensionResultORM).where(DimensionResultORM.run_id == run_id)
                )
            ).scalars()
            return [
                {
                    "dimension": r.dimension,
                    "status": r.status,
                    "score": r.score,
                    "findings": r.findings or [],
                    "blocking": r.blocking,
                }
                for r in rows
            ]


def _raise_not_found(what: str, key: str) -> None:
    raise PersistenceError(f"{what} not found for key={key!r}")
