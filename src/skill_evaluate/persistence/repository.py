"""统一 Repository 层（docs/dev/04 第 4 节）。

`nodes/`、`agents/` 下的代码一律通过 Repository 存取，不直接写 SQL/ORM
查询语句。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TypedDict

from sqlalchemy import desc, select, update
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
    GoldenCaseORM,
    HumanApprovalORM,
    JudgeHealthStatusORM,
    JudgeMissRecordORM,
    JudgeVerdictORM,
    NodeRetryCountORM,
    PatchApplicationResultORM,
    PatchORM,
    PendingHookORM,
    RunORM,
    SecurityFindingORM,
    SkillORM,
    TestCaseORM,
    TestSuiteVersionORM,
)
from skill_evaluate.state.assertion import AssertionResult, AssertionSpec
from skill_evaluate.state.capability import CapabilityTree
from skill_evaluate.state.enums import AssertionStrategy
from skill_evaluate.state.golden import GoldenCase, JudgeMissRecord
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.patch import Patch, PatchApplicationResult
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
                script_content=spec.script_content,
                failure_reason=spec.failure_reason,
                created_at=spec.created_at or datetime.now(UTC),
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["assertion_id"])
            await session.execute(stmt)
            await session.commit()

    async def get_spec(self, assertion_id: str) -> AssertionSpec | None:
        """按 id 读回 spec（docs/dev/10 第 4.3 节：Hook 端点落断言结果前的外键前置检查）。"""
        async with new_session() as session:
            row = await session.get(AssertionSpecORM, assertion_id)
            if row is None:
                return None
            return AssertionSpec(
                assertion_id=row.assertion_id,
                case_id=row.case_id,
                strategy=AssertionStrategy(row.strategy),
                template_ref=row.template_ref,
                script_path=row.script_path,
                language=row.language,
                script_content=row.script_content,
                failure_reason=row.failure_reason,
                created_at=row.created_at,
            )

    async def list_specs_for_case(self, case_id: str) -> list[AssertionSpec]:
        """一条用例的全部 spec，供断点恢复后重新下发同一份脚本（docs/dev/13/15）。"""
        async with new_session() as session:
            rows = (
                await session.execute(
                    select(AssertionSpecORM).where(AssertionSpecORM.case_id == case_id)
                )
            ).scalars()
            return [
                AssertionSpec(
                    assertion_id=row.assertion_id,
                    case_id=row.case_id,
                    strategy=AssertionStrategy(row.strategy),
                    template_ref=row.template_ref,
                    script_path=row.script_path,
                    language=row.language,
                    script_content=row.script_content,
                    failure_reason=row.failure_reason,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    async def list_results(self, assertion_id: str) -> list[AssertionResult]:
        """一条断言的全部执行结果，供 Judge 组合证据（docs/dev/10 第 7 节）。"""
        async with new_session() as session:
            rows = (
                await session.execute(
                    select(AssertionResultORM).where(
                        AssertionResultORM.assertion_id == assertion_id
                    )
                )
            ).scalars()
            return [
                AssertionResult(
                    assertion_id=row.assertion_id,
                    exit_code=row.exit_code,
                    stdout=row.stdout,
                    stderr=row.stderr,
                    passed=row.passed,
                )
                for row in rows
            ]

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


# --------------------------------------------------------------------------- #
# docs/dev/08：裁判可信度机制
# --------------------------------------------------------------------------- #


class GoldenCaseRepository:
    """黄金用例存取（docs/dev/08 第 3.1 节）。

    `save()` 存在是为了让运维侧的补录脚本/审查工作台有一个受控入口——评测流水线
    本身只读不写。
    """

    async def save(self, case: GoldenCase) -> None:
        async with new_session() as session:
            stmt = pg_insert(GoldenCaseORM).values(
                golden_id=case.golden_id,
                template_key=case.template_key,
                content=case.content,
                human_labeled_status=case.human_labeled_status.value,
                human_labeled_reasoning=case.human_labeled_reasoning,
                active=case.active,
                created_at=datetime.now(UTC),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["golden_id"],
                set_={
                    "content": stmt.excluded.content,
                    "human_labeled_status": stmt.excluded.human_labeled_status,
                    "human_labeled_reasoning": stmt.excluded.human_labeled_reasoning,
                    "active": stmt.excluded.active,
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def list_active(self, template_key: str | None = None) -> list[GoldenCase]:
        """按模板筛选可用黄金用例。

        注入点只会拿**同一 template_key** 的黄金用例去替换真实请求：换了模板就
        换了 content 的字段形状，渲染会直接因 `StrictUndefined` 报错，伪装也就
        无从谈起。
        """
        async with new_session() as session:
            query = select(GoldenCaseORM).where(GoldenCaseORM.active.is_(True))
            if template_key is not None:
                query = query.where(GoldenCaseORM.template_key == template_key)
            rows = (await session.execute(query)).scalars()
            return [
                GoldenCase(
                    golden_id=r.golden_id,
                    template_key=r.template_key,
                    content=r.content or {},
                    human_labeled_status=r.human_labeled_status,
                    human_labeled_reasoning=r.human_labeled_reasoning,
                    active=r.active,
                )
                for r in rows
            ]


class JudgeMissRepository:
    """黄金用例判决的记账与滑动窗口查询（docs/dev/08 第 3.3 节）。"""

    async def record(self, record: JudgeMissRecord, *, temperature_bucket: str) -> None:
        async with new_session() as session:
            stmt = pg_insert(JudgeMissRecordORM).values(
                miss_id=record.miss_id,
                golden_id=record.golden_id,
                judge_output_status=record.judge_output_status.value,
                model=record.model,
                temperature=record.temperature,
                temperature_bucket=temperature_bucket,
                is_miss=record.is_miss,
                occurred_at=record.occurred_at,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["miss_id"])
            await session.execute(stmt)
            await session.commit()

    async def recent_window(
        self, *, model: str, temperature_bucket: str, window_size: int
    ) -> list[bool]:
        """最近 `window_size` 次该 Judge 配置的黄金判决，返回 `is_miss` 序列（新 -> 旧）。"""
        async with new_session() as session:
            rows = (
                await session.execute(
                    select(JudgeMissRecordORM.is_miss)
                    .where(
                        JudgeMissRecordORM.model == model,
                        JudgeMissRecordORM.temperature_bucket == temperature_bucket,
                    )
                    .order_by(desc(JudgeMissRecordORM.occurred_at))
                    .limit(window_size)
                )
            ).scalars()
            return list(rows)


class JudgeHealthRepository:
    """`judge_health_status` 表：按 `(model, temperature_bucket)` 独立冻结/解冻。"""

    async def get(self, *, model: str, temperature_bucket: str) -> dict[str, object] | None:
        async with new_session() as session:
            row = (
                await session.execute(
                    select(JudgeHealthStatusORM).where(
                        JudgeHealthStatusORM.model == model,
                        JudgeHealthStatusORM.temperature_bucket == temperature_bucket,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "model": row.model,
                "temperature_bucket": row.temperature_bucket,
                "frozen": row.frozen,
                "miss_rate": row.miss_rate,
                "window_size": row.window_size,
                "reason": row.reason,
            }

    async def upsert(
        self,
        *,
        model: str,
        temperature_bucket: str,
        frozen: bool,
        miss_rate: float,
        window_size: int,
        reason: str | None,
    ) -> None:
        async with new_session() as session:
            stmt = pg_insert(JudgeHealthStatusORM).values(
                model=model,
                temperature_bucket=temperature_bucket,
                frozen=frozen,
                miss_rate=miss_rate,
                window_size=window_size,
                reason=reason,
                updated_at=datetime.now(UTC),
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_judge_health_config",
                set_={
                    "frozen": stmt.excluded.frozen,
                    "miss_rate": stmt.excluded.miss_rate,
                    "window_size": stmt.excluded.window_size,
                    "reason": stmt.excluded.reason,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def is_frozen(self, *, model: str, temperature_bucket: str) -> bool:
        status = await self.get(model=model, temperature_bucket=temperature_bucket)
        return bool(status and status["frozen"])


# --------------------------------------------------------------------------- #
# docs/dev/09：Optimizer 补丁与重试计数
# --------------------------------------------------------------------------- #


class PatchRepository:
    async def save(self, patch: Patch) -> None:
        async with new_session() as session:
            stmt = pg_insert(PatchORM).values(
                patch_id=patch.patch_id,
                skill_id=patch.skill_id,
                base_skill_version_ref=patch.base_skill_version_ref,
                patch_type=patch.patch_type.value,
                target_path=patch.target_path,
                diff=patch.diff,
                rationale=patch.rationale,
                triggered_by_finding_id=patch.triggered_by_finding_id,
                created_at=patch.created_at,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["patch_id"])
            await session.execute(stmt)
            await session.commit()

    async def get(self, patch_id: str) -> Patch | None:
        async with new_session() as session:
            row = (
                await session.execute(select(PatchORM).where(PatchORM.patch_id == patch_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            return _orm_to_patch(row)

    async def save_application_result(self, result: PatchApplicationResult) -> None:
        async with new_session() as session:
            stmt = pg_insert(PatchApplicationResultORM).values(
                patch_id=result.patch_id,
                applied=result.applied,
                regression_passed=result.regression_passed,
                working_skill_version_ref=result.working_skill_version_ref,
                detail=result.detail,
                created_at=datetime.now(UTC),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["patch_id"],
                set_={
                    "applied": stmt.excluded.applied,
                    "regression_passed": stmt.excluded.regression_passed,
                    "working_skill_version_ref": stmt.excluded.working_skill_version_ref,
                    "detail": stmt.excluded.detail,
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def list_by_skill(self, skill_id: str) -> list[Patch]:
        async with new_session() as session:
            rows = (
                await session.execute(select(PatchORM).where(PatchORM.skill_id == skill_id))
            ).scalars()
            return [_orm_to_patch(r) for r in rows]


def _orm_to_patch(row: PatchORM) -> Patch:
    return Patch(
        patch_id=row.patch_id,
        skill_id=row.skill_id,
        base_skill_version_ref=row.base_skill_version_ref,
        patch_type=row.patch_type,
        target_path=row.target_path,
        diff=row.diff,
        rationale=row.rationale,
        triggered_by_finding_id=row.triggered_by_finding_id,
        created_at=row.created_at,
    )


class PipelineStateRepository:
    """`PipelineState.retry_counts` 的落盘读写（docs/dev/09 第 5 节要求的 Repository 追加方法）。

    与 LangGraph checkpoint 里的 `PipelineState.retry_counts` 是**互补**关系而不是
    竞争关系：checkpoint 记的是节点之间的图状态，这里记的是某个节点内部闭环循环
    的进度。节点结束时是否把本表的值回写进图状态，由调用方（docs/dev/11/15 的
    节点）决定，本层不擅自改图状态。
    """

    async def increment_retry(self, run_id: str, node_name: str) -> int:
        async with new_session() as session:
            stmt = pg_insert(NodeRetryCountORM).values(
                run_id=run_id,
                node_name=node_name,
                retry_count=1,
                updated_at=datetime.now(UTC),
            )
            upsert_stmt = stmt.on_conflict_do_update(
                constraint="uq_node_retry_counts_key",
                set_={
                    "retry_count": NodeRetryCountORM.retry_count + 1,
                    "updated_at": stmt.excluded.updated_at,
                },
            ).returning(NodeRetryCountORM.retry_count)
            result = await session.execute(upsert_stmt)
            await session.commit()
            return int(result.scalar_one())

    async def get_retry_counts(self, run_id: str) -> dict[str, int]:
        async with new_session() as session:
            rows = (
                await session.execute(
                    select(NodeRetryCountORM).where(NodeRetryCountORM.run_id == run_id)
                )
            ).scalars()
            return {r.node_name: r.retry_count for r in rows}
