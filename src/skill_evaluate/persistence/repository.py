"""统一 Repository 层（docs/dev/04 第 4 节）。

`nodes/`、`agents/` 下的代码一律通过 Repository 存取，不直接写 SQL/ORM
查询语句。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NotRequired, TypedDict

from sqlalchemy import delete, desc, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql.elements import ColumnClause

from skill_evaluate.errors import PersistenceError
from skill_evaluate.persistence.db import new_session
from skill_evaluate.persistence.models import (
    ApprovalDecisionORM,
    AssertionResultORM,
    AssertionSpecORM,
    CanaryProbeHistoryORM,
    CapabilityTreeORM,
    CaseEmbeddingORM,
    ConsensusResultORM,
    DimensionResultORM,
    ExecutionTraceORM,
    GenerationCollapseEventORM,
    GoldenCaseORM,
    HumanApprovalORM,
    JudgeHealthStatusORM,
    JudgeMissRecordORM,
    JudgeVerdictORM,
    NodeRetryCountORM,
    PatchApplicationResultORM,
    PatchORM,
    PendingApprovalORM,
    PendingHookORM,
    RunORM,
    SearchDocumentORM,
    SecurityFindingORM,
    SkillORM,
    TestCaseORM,
    TestCaseSuggestionORM,
    TestSuiteVersionORM,
)
from skill_evaluate.state.approval import (
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalStatus,
    PendingApproval,
)
from skill_evaluate.state.assertion import AssertionResult, AssertionSpec
from skill_evaluate.state.capability import CapabilityTree
from skill_evaluate.state.enums import (
    AssertionStrategy,
    DatasetSplit,
    SuggestionStatus,
    SuggestionType,
    TestCaseCategory,
)
from skill_evaluate.state.generator_trust import CanaryProbeRecord, GenerationCollapseEvent
from skill_evaluate.state.golden import GoldenCase, JudgeMissRecord
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.memory import SearchDocument, StoredSearchDocument
from skill_evaluate.state.patch import Patch, PatchApplicationResult
from skill_evaluate.state.security import SecurityFinding
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.suggestion import TestCaseSuggestion
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
            return _orm_to_skill(row) if row is not None else None

    async def get_latest(self, skill_id: str) -> SkillDefinition | None:
        """取某个 Skill **最近一次入库**的版本（docs/dev/20 第 4 节）。

        模块十首次以"技能库"而不是"单一被测技能"的方式使用本仓储：基准干扰包与基石
        Skill 由运维侧按 skill_id 维护，不绑定 version_ref——它们代表的是"系统里此刻
        真实在用的那一版"，而那一版就是最近入库的。

        以 `created_at` 排序而不是 version_ref：version_ref 是 git sha / tag，没有可比较
        的先后语义。注意 `save()` 的 upsert 不改写 `created_at`，同一版本重复入库不会
        让它"变新"——这正是想要的语义。
        """
        async with new_session() as session:
            row = (
                await session.execute(
                    select(SkillORM)
                    .where(SkillORM.skill_id == skill_id)
                    .order_by(desc(SkillORM.created_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
            return _orm_to_skill(row) if row is not None else None


def _orm_to_skill(row: SkillORM) -> SkillDefinition:
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

    async def list_by_category(
        self, suite_version_id: str, category: TestCaseCategory
    ) -> list[TestCase]:
        """按类别取某一版用例集里的用例（docs/dev/13 第 3 节要求的查询）。"""
        return await self.list_by_categories(suite_version_id, [category])

    async def list_by_categories(
        self, suite_version_id: str, categories: list[TestCaseCategory]
    ) -> list[TestCase]:
        """按若干类别取某一版用例集里的用例。

        **以 `suite_version_id` 而不是 `skill_id` 为口径**：同一个 Skill 下会存在
        多版用例集（force_regenerate 保留历史、incremental_patch 叠加新版），按
        skill_id 查会把已经不在 active 版本里的历史用例一起捞回来，评测就跑了一批
        没人再维护的旧题。

        版本不存在时返回空列表而不是报错：调用方（各维度的 prepare 节点）拿到空
        列表后自己决定是"这个维度这次没得测"（写 NEEDS_HUMAN_REVIEW）还是"正常
        情况"，比在仓储层替它们决定要合适。
        """
        if not categories:
            return []
        async with new_session() as session:
            suite = (
                await session.execute(
                    select(TestSuiteVersionORM.case_ids).where(
                        TestSuiteVersionORM.suite_version_id == suite_version_id
                    )
                )
            ).scalar_one_or_none()
            if not suite:
                return []
            rows = (
                await session.execute(
                    select(TestCaseORM).where(
                        TestCaseORM.case_id.in_(list(suite)),
                        TestCaseORM.category.in_([c.value for c in categories]),
                    )
                )
            ).scalars()
            return [_orm_to_test_case(r) for r in rows]

    async def retire(self, case_id: str) -> bool:
        """把一条用例归档为 `COLD`（docs/dev/22 第 7 节：孤儿用例退役的真正动作）。

        **归档而不是删除**：`execution_traces` / `judge_verdicts` / `case_embeddings` 都以
        case_id 关联这条用例，物理删除会让历史报告里的证据链断掉。`COLD` 早在 docs/dev/02
        就被设计为"惰性过滤"区——按 TRAIN/VALIDATION 取题的维度天然看不到它，也就"不再
        参与任何主动评测"，同时它仍在 `test_suite_versions.case_ids` 里，历史版本可复现。

        只有工作台的人工确认路径会调用本方法（docs/dev/17 第 5.2 节的约束）。
        返回是否真的改动了一行（用例不存在或已是 COLD 时为 False，调用方据此记日志，天然幂等）。
        """
        async with new_session() as session:
            result = await session.execute(
                update(TestCaseORM)
                .where(
                    TestCaseORM.case_id == case_id,
                    TestCaseORM.split != DatasetSplit.COLD.value,
                )
                .values(split=DatasetSplit.COLD.value)
            )
            await session.commit()
            return result.rowcount > 0  # type: ignore[attr-defined,no-any-return]


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
        "probe_target_reference": case.probe_target_reference,
        "attack_subtype": case.attack_subtype.value if case.attack_subtype else None,
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
        probe_target_reference=row.probe_target_reference,
        attack_subtype=row.attack_subtype,
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
                # docs/dev/15：只有声明了 to_severity 的模板会填这一列，其余为 NULL。
                severity=verdict.severity.value if verdict.severity else None,
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
                    severity=r.severity,
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

    async def list_by_case_ids(self, case_ids: list[str]) -> list[SecurityFinding]:
        """按用例回读安全发现（docs/dev/15 第 11、12 节）。

        两处用得到：优化闭环要把 `remediation_patch_id` 回填到对应的发现上（先读
        回来再 `save()` 覆盖），报告节点要在图状态之外再核对一次落库结果。以
        `case_id` 为口径而不是 run_id，是因为 `security_findings` 表本身不带
        run_id——一条发现属于"这条用例在这份 Skill 上的问题"，跨运行是同一件事。
        """
        if not case_ids:
            return []
        async with new_session() as session:
            rows = (
                await session.execute(
                    select(SecurityFindingORM).where(SecurityFindingORM.case_id.in_(case_ids))
                )
            ).scalars()
            return [
                SecurityFinding(
                    finding_id=r.finding_id,
                    case_id=r.case_id,
                    category=r.category,
                    severity=r.severity,
                    evidence=r.evidence,
                    remediation_patch_id=r.remediation_patch_id,
                )
                for r in rows
            ]

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
    """`human_approvals` 表存取：阻塞式人工审批的**挂起账本**（docs/dev/04 第 5 节）。

    docs/dev/22 落地后本表的定位没有变——它只记 `wait_key` 的 waiting → resolved，供
    `resolve_suspension()` 做幂等唤醒；卡片的业务内容（decision_type、摘要、证据引用）在
    `pending_approvals`（`PendingApprovalRepository`）。挂起点不应再直接调用 `create()`，
    统一走 `persistence/approval_service.py::request_human_approval()`，它会把两张表一起写好。
    """

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
    # docs/dev/24 追加：NotRequired——既有调用方/测试替身构造的 RunInfo 不带它也合法。
    pr_url: NotRequired[str | None]


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
                "pr_url": row.pr_url,
            }

    async def record_pr_url(self, run_id: str, pr_url: str) -> None:
        """记录本次运行自动创建的修复 PR（docs/dev/24 第 5 节 `record_pr_url`）。

        幂等覆盖写：收尾节点断点恢复后重跑时会复用同一个 PR（按分支名查到已有 PR），写入的
        仍是同一个 URL。
        """
        async with new_session() as session:
            await session.execute(
                update(RunORM).where(RunORM.run_id == run_id).values(pr_url=pr_url)
            )
            await session.commit()


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

    async def get_application_result(self, patch_id: str) -> PatchApplicationResult | None:
        """某个补丁的应用/重测结果（docs/dev/22 工作台展开 ACCEPT_PATCH 卡片时读取）。"""
        async with new_session() as session:
            row = (
                await session.execute(
                    select(PatchApplicationResultORM).where(
                        PatchApplicationResultORM.patch_id == patch_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return PatchApplicationResult(
                patch_id=row.patch_id,
                applied=row.applied,
                regression_passed=row.regression_passed,
                working_skill_version_ref=row.working_skill_version_ref,
                detail=row.detail,
            )

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


# --------------------------------------------------------------------------- #
# docs/dev/17：模块七——用例集瘦身与动态演进
# --------------------------------------------------------------------------- #


class TestCaseSuggestionRepository:
    """`test_case_suggestions` 表存取（docs/dev/17 第 6.1 节）。

    这张表是模块七与 docs/dev/22 审查工作台之间唯一的接口面：模块七**只**写
    `pending`，工作台负责把它推进到 `confirmed`/`rejected` 并执行真正的淘汰动作。
    因此本类刻意**不提供** `delete()` / `confirm()` 之类的方法——架构文档要求"硬性
    的删除操作必须保留人类开发者的最终 Review 确认权限"，少写一个方法就少一条被
    某次"顺手自动化一下"绕过这条约束的路径。`update_status()` 供工作台使用，它
    拒绝把 `pending` 之外的状态再改回去（见该方法）。
    """

    async def save_if_absent(self, suggestion: TestCaseSuggestion) -> bool:
        """写入一条建议；`(case_id, suggestion_type)` 已存在时**什么都不做**。

        返回是否真的插入了新行，供节点统计"本轮新增了几条待办"——报告里"检出 3 条
        孤儿用例"和"新增 3 条待办"是两个不同的数：连续三次评测检出同一条孤儿用例，
        前者每次都是 1，后者只有第一次是 1。

        去重靠库层唯一约束而不是"先 SELECT 再 INSERT"：多个 run 并发评测同一个
        Skill 时，后者必然产生重复待办，而人在工作台上会看到同一条建议的若干副本。

        已被人 `rejected` 的建议同样不会被重新插入——这是**期望行为**而非副作用：
        人已经判断过"这条孤儿用例要留着"，评测系统不该每跑一次就把它重新推回待办
        列表。要重开只能由工作台显式操作。
        """
        async with new_session() as session:
            now = datetime.now(UTC)
            stmt = pg_insert(TestCaseSuggestionORM).values(
                suggestion_id=suggestion.suggestion_id,
                case_id=suggestion.case_id,
                suggestion_type=suggestion.suggestion_type.value,
                reason=suggestion.reason,
                status=suggestion.status.value,
                created_at=suggestion.created_at,
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_nothing(constraint="uq_test_case_suggestions_case_type")
            result = await session.execute(stmt)
            await session.commit()
            # `Result` 的静态类型上没有 rowcount（只有 `CursorResult` 有），与本文件
            # 其余 `rowcount` 用法同一处理：忽略这一条，不为它把返回类型放宽。
            return bool(result.rowcount)  # type: ignore[attr-defined]

    async def get(self, suggestion_id: str) -> TestCaseSuggestion | None:
        """按 id 取一条建议（docs/dev/22 工作台决策前读取当前状态）。"""
        async with new_session() as session:
            row = (
                await session.execute(
                    select(TestCaseSuggestionORM).where(
                        TestCaseSuggestionORM.suggestion_id == suggestion_id
                    )
                )
            ).scalar_one_or_none()
            return _orm_to_suggestion(row) if row else None

    async def list_by_status(
        self, status: SuggestionStatus, *, suggestion_type: SuggestionType | None = None
    ) -> list[TestCaseSuggestion]:
        """按状态列出建议（工作台的主查询：`status=pending`）。"""
        async with new_session() as session:
            query = select(TestCaseSuggestionORM).where(
                TestCaseSuggestionORM.status == status.value
            )
            if suggestion_type is not None:
                query = query.where(TestCaseSuggestionORM.suggestion_type == suggestion_type.value)
            rows = (await session.execute(query)).scalars()
            return [_orm_to_suggestion(r) for r in rows]

    async def list_by_case_ids(self, case_ids: list[str]) -> list[TestCaseSuggestion]:
        """按用例 id 批量取建议，供节点判断"这些用例是不是已经有待办了"。"""
        if not case_ids:
            return []
        async with new_session() as session:
            rows = (
                await session.execute(
                    select(TestCaseSuggestionORM).where(TestCaseSuggestionORM.case_id.in_(case_ids))
                )
            ).scalars()
            return [_orm_to_suggestion(r) for r in rows]

    async def update_status(self, suggestion_id: str, status: SuggestionStatus) -> bool:
        """人工决策落库（docs/dev/22 的工作台调用），返回是否真的改动了一行。

        `WHERE status = 'pending'` 不是可有可无的：它让这个方法天然幂等，并且挡住
        "把已经确认淘汰的建议改回 pending"这类会让审计线索断掉的操作。真正的重开
        应当是一条新建议（新的 `suggestion_id`），而不是把旧记录改回去。
        """
        if status is SuggestionStatus.PENDING:
            raise PersistenceError(
                "update_status() 只用于把建议从 pending 推进到 confirmed/rejected；"
                "把已决策的建议改回 pending 会让审计线索断掉，如需重开请新建一条建议。"
            )
        async with new_session() as session:
            result = await session.execute(
                update(TestCaseSuggestionORM)
                .where(
                    TestCaseSuggestionORM.suggestion_id == suggestion_id,
                    TestCaseSuggestionORM.status == SuggestionStatus.PENDING.value,
                )
                .values(status=status.value, updated_at=datetime.now(UTC))
            )
            await session.commit()
            return result.rowcount > 0  # type: ignore[attr-defined,no-any-return]


def _orm_to_suggestion(row: TestCaseSuggestionORM) -> TestCaseSuggestion:
    return TestCaseSuggestion(
        suggestion_id=row.suggestion_id,
        case_id=row.case_id,
        suggestion_type=SuggestionType(row.suggestion_type),
        reason=row.reason,
        status=SuggestionStatus(row.status),
        created_at=row.created_at,
    )


# --------------------------------------------------------------------------- #
# docs/dev/21：Generator 可信度与前置门禁
# --------------------------------------------------------------------------- #


class CaseEmbeddingRepository:
    """`case_embeddings` 表存取（docs/dev/21 第 2.1 节）。

    所有查询都带 `embedding_model` 过滤：换了 embedding 模型后，新旧向量不在同一个空间里，
    拿它们算距离得到的是一个毫无意义却看起来很正常的数字。按模型过滤让旧向量自然退出
    参照系，历史分布从新模型开始重新积累（冷启动阈值兜底这段时间）。
    """

    async def save_many(
        self, *, skill_id: str, embedding_model: str, vectors: dict[str, list[float]]
    ) -> None:
        """批量 upsert。调用方必须保证这些 case_id 已经写进 `test_cases`（外键）。"""
        if not vectors:
            return
        now = datetime.now(UTC)
        async with new_session() as session:
            for case_id, vector in vectors.items():
                stmt = pg_insert(CaseEmbeddingORM).values(
                    case_id=case_id,
                    skill_id=skill_id,
                    embedding=vector,
                    embedding_model=embedding_model,
                    created_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["case_id"],
                    set_={
                        "embedding": stmt.excluded.embedding,
                        "embedding_model": stmt.excluded.embedding_model,
                        "created_at": stmt.excluded.created_at,
                    },
                )
                await session.execute(stmt)
            await session.commit()

    async def get_recent(
        self,
        *,
        skill_id: str,
        embedding_model: str,
        exclude_case_ids: list[str] | None = None,
        limit: int = 50,
    ) -> list[list[float]]:
        """该 Skill 最近写入的 `limit` 条历史向量（新 → 旧）。"""
        async with new_session() as session:
            query = select(CaseEmbeddingORM.embedding).where(
                CaseEmbeddingORM.skill_id == skill_id,
                CaseEmbeddingORM.embedding_model == embedding_model,
            )
            if exclude_case_ids:
                query = query.where(CaseEmbeddingORM.case_id.not_in(exclude_case_ids))
            rows = (
                await session.execute(
                    query.order_by(desc(CaseEmbeddingORM.created_at)).limit(limit)
                )
            ).scalars()
            return [list(vector) for vector in rows]

    async def count_by_skill(self, *, skill_id: str, embedding_model: str) -> int:
        """该 Skill 的历史向量总数——弹性阈值的"数据飞轮成熟度"就按它插值。"""
        async with new_session() as session:
            result = await session.execute(
                select(func.count())
                .select_from(CaseEmbeddingORM)
                .where(
                    CaseEmbeddingORM.skill_id == skill_id,
                    CaseEmbeddingORM.embedding_model == embedding_model,
                )
            )
            return int(result.scalar_one())

    async def missing_case_ids(self, case_ids: list[str], *, embedding_model: str) -> list[str]:
        """`case_ids` 里还没有（当前模型）向量的那些，供存量用例回填历史分布。"""
        if not case_ids:
            return []
        async with new_session() as session:
            rows = (
                await session.execute(
                    select(CaseEmbeddingORM.case_id).where(
                        CaseEmbeddingORM.case_id.in_(case_ids),
                        CaseEmbeddingORM.embedding_model == embedding_model,
                    )
                )
            ).scalars()
            present = set(rows)
            return [case_id for case_id in case_ids if case_id not in present]


class GenerationCollapseEventRepository:
    """`generation_collapse_events` 表存取（docs/dev/21 第 2.2 节）。"""

    async def record(self, event: GenerationCollapseEvent) -> None:
        async with new_session() as session:
            stmt = pg_insert(GenerationCollapseEventORM).values(
                event_id=event.event_id,
                skill_id=event.skill_id,
                generator_run_id=event.generator_run_id,
                generation_mode=event.generation_mode,
                triggered_by=event.triggered_by,
                reason=event.reason.value,
                avg_distance_to_history=event.avg_distance_to_history,
                intra_batch_distance=event.intra_batch_distance,
                threshold=event.threshold,
                historical_count=event.historical_count,
                new_case_count=event.new_case_count,
                occurred_at=event.occurred_at,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["event_id"])
            await session.execute(stmt)
            await session.commit()

    async def count_since(self, *, skill_id: str, since: datetime | None) -> int:
        """`since` 之后该 Skill 的坍塌事件数；`since=None` 表示从未成功激活过，数全部。

        调用方传"当前 active 用例集版本的 created_at"——成功激活会刷新它，于是这个数就是
        "连续坍塌次数"，不需要额外维护一个会与真实激活状态不同步的计数器。
        """
        async with new_session() as session:
            query = (
                select(func.count())
                .select_from(GenerationCollapseEventORM)
                .where(GenerationCollapseEventORM.skill_id == skill_id)
            )
            if since is not None:
                query = query.where(GenerationCollapseEventORM.occurred_at > since)
            return int((await session.execute(query)).scalar_one())

    async def list_recent(self, *, skill_id: str, limit: int = 10) -> list[dict[str, object]]:
        """最近若干次坍塌事件（docs/dev/22 工作台展开 INJECT_NEW_SEED 卡片时读取）。

        返回 dict 而不是 `GenerationCollapseEvent`：工作台只做展示，直接 JSON 化即可。
        """
        async with new_session() as session:
            rows = (
                await session.execute(
                    select(GenerationCollapseEventORM)
                    .where(GenerationCollapseEventORM.skill_id == skill_id)
                    .order_by(desc(GenerationCollapseEventORM.occurred_at))
                    .limit(limit)
                )
            ).scalars()
            return [
                {
                    "event_id": r.event_id,
                    "generator_run_id": r.generator_run_id,
                    "generation_mode": r.generation_mode,
                    "triggered_by": r.triggered_by,
                    "reason": r.reason,
                    "avg_distance_to_history": r.avg_distance_to_history,
                    "intra_batch_distance": r.intra_batch_distance,
                    "threshold": r.threshold,
                    "historical_count": r.historical_count,
                    "new_case_count": r.new_case_count,
                    "occurred_at": r.occurred_at.isoformat(),
                }
                for r in rows
            ]


class CanaryProbeHistoryRepository:
    """`canary_probe_history` 表存取（docs/dev/21 第 6 节）。"""

    async def record(self, record: CanaryProbeRecord) -> None:
        async with new_session() as session:
            session.add(
                CanaryProbeHistoryORM(
                    probe_id=record.probe_id,
                    run_id=record.run_id,
                    image_ref=record.image_ref,
                    passed=record.passed,
                    reasons=list(record.reasons),
                    probed_at=record.probed_at,
                )
            )
            await session.commit()

    async def latest_success(self, *, image_ref: str) -> CanaryProbeRecord | None:
        """同一镜像下最近一次**成功**的探针。失败记录不参与跳过判定。"""
        async with new_session() as session:
            row = (
                await session.execute(
                    select(CanaryProbeHistoryORM)
                    .where(
                        CanaryProbeHistoryORM.image_ref == image_ref,
                        CanaryProbeHistoryORM.passed.is_(True),
                    )
                    .order_by(desc(CanaryProbeHistoryORM.probed_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return CanaryProbeRecord(
                probe_id=row.probe_id,
                run_id=row.run_id,
                image_ref=row.image_ref,
                passed=row.passed,
                reasons=list(row.reasons or []),
                probed_at=row.probed_at,
            )


# --------------------------------------------------------------------------- #
# docs/dev/22：容错机制与人工审批闭环
# --------------------------------------------------------------------------- #


class PendingApprovalRepository:
    """`pending_approvals` 表存取：统一审批卡片（docs/dev/22 第 2 节）。

    写入方只有 `persistence/approval_service.py`（挂起点经由它），推进状态的只有决策 API。
    """

    async def save(self, approval: PendingApproval) -> bool:
        """写入一张卡片；`wait_key` 已存在时什么都不做，返回是否真的插入了新行。

        返回值决定"要不要发 Discord 卡片"：挂起节点恢复时会整体重跑并再次调到这里，
        若不以"是否新插入"为准，人会在每次唤醒后再收到一张同样的卡片。
        """
        async with new_session() as session:
            stmt = pg_insert(PendingApprovalORM).values(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                wait_key=approval.wait_key,
                decision_type=approval.decision_type.value,
                node_name=approval.node_name,
                thread_id=approval.thread_id,
                context_summary=approval.context_summary,
                context_ref=approval.context_ref,
                blocking=approval.blocking,
                status=approval.status.value,
                created_at=approval.created_at,
                resolved_at=approval.resolved_at,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["wait_key"])
            result = await session.execute(stmt)
            await session.commit()
            return bool(result.rowcount)  # type: ignore[attr-defined]

    async def get(self, approval_id: str) -> PendingApproval | None:
        async with new_session() as session:
            row = (
                await session.execute(
                    select(PendingApprovalORM).where(PendingApprovalORM.approval_id == approval_id)
                )
            ).scalar_one_or_none()
            return _orm_to_pending_approval(row) if row else None

    async def list_by_status(
        self, status: ApprovalStatus | None = None, *, run_id: str | None = None
    ) -> list[PendingApproval]:
        """工作台主查询（`GET /api/approvals?status=pending`），按创建时间倒序。

        `status=None` 表示不过滤——审计场景需要看到已处理的卡片。
        """
        async with new_session() as session:
            query = select(PendingApprovalORM)
            if status is not None:
                query = query.where(PendingApprovalORM.status == status.value)
            if run_id is not None:
                query = query.where(PendingApprovalORM.run_id == run_id)
            rows = (
                await session.execute(query.order_by(desc(PendingApprovalORM.created_at)))
            ).scalars()
            return [_orm_to_pending_approval(r) for r in rows]

    async def find_pending_by_node(self, run_id: str, node_name: str) -> PendingApproval | None:
        """某次运行某个节点上最新的一张 pending 卡片（HMAC 回调端点按路径参数定位卡片用）。"""
        async with new_session() as session:
            row = (
                await session.execute(
                    select(PendingApprovalORM)
                    .where(
                        PendingApprovalORM.run_id == run_id,
                        PendingApprovalORM.node_name == node_name,
                        PendingApprovalORM.status == ApprovalStatus.PENDING.value,
                    )
                    .order_by(desc(PendingApprovalORM.created_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
            return _orm_to_pending_approval(row) if row else None

    async def mark_resolved(self, approval_id: str) -> bool:
        """pending → resolved，`WHERE status='pending'` 使其幂等，返回是否真的改动了一行。"""
        async with new_session() as session:
            result = await session.execute(
                update(PendingApprovalORM)
                .where(
                    PendingApprovalORM.approval_id == approval_id,
                    PendingApprovalORM.status == ApprovalStatus.PENDING.value,
                )
                .values(status=ApprovalStatus.RESOLVED.value, resolved_at=datetime.now(UTC))
            )
            await session.commit()
            return result.rowcount > 0  # type: ignore[attr-defined,no-any-return]


def _orm_to_pending_approval(row: PendingApprovalORM) -> PendingApproval:
    return PendingApproval(
        approval_id=row.approval_id,
        run_id=row.run_id,
        wait_key=row.wait_key,
        decision_type=ApprovalDecisionType(row.decision_type),
        context_summary=row.context_summary,
        context_ref=dict(row.context_ref or {}),
        node_name=row.node_name,
        thread_id=row.thread_id,
        blocking=row.blocking,
        status=ApprovalStatus(row.status),
        created_at=row.created_at,
        resolved_at=row.resolved_at,
    )


class ApprovalDecisionRepository:
    """`approval_decisions` 表存取：人工决定的审计记录（一张卡片一条）。"""

    async def save(self, decision: ApprovalDecision) -> bool:
        """写入决定；该卡片已有决定时什么都不做并返回 False。

        决策 API 以本方法的返回值作为"抢占决策权"的原子操作：并发的两个决定只有先到者
        返回 True，后到者据此回 409，不会继续去唤醒图。
        """
        async with new_session() as session:
            stmt = pg_insert(ApprovalDecisionORM).values(
                approval_id=decision.approval_id,
                decided_by=decision.decided_by,
                outcome=decision.outcome,
                note=decision.note,
                decided_at=decision.decided_at,
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["approval_id"])
            result = await session.execute(stmt)
            await session.commit()
            return bool(result.rowcount)  # type: ignore[attr-defined]

    async def get_by_approval(self, approval_id: str) -> ApprovalDecision | None:
        async with new_session() as session:
            row = (
                await session.execute(
                    select(ApprovalDecisionORM).where(
                        ApprovalDecisionORM.approval_id == approval_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return ApprovalDecision(
                approval_id=row.approval_id,
                decided_by=row.decided_by,
                outcome=row.outcome,
                note=row.note,
                decided_at=row.decided_at,
            )


# --------------------------------------------------------------------------- #
# docs/dev/23：长时记忆与数据飞轮
# --------------------------------------------------------------------------- #

# 全文检索生成列（迁移 0011 声明、ORM 不映射，见 SearchDocumentORM 类注释）。
_TEXT_SEARCH_COLUMN: ColumnClause[Any] = literal_column("search_documents.text_search")


class SearchDocumentRepository:
    """`search_documents` 表存取：`memory.hybrid_search.SearchDocumentStore` 协议的 Postgres 实现。

    两路召回各自是一条 SQL，**融合与重排不在库里做**：Reranker 是本地模型，RRF 需要两路的
    名次，放在服务层做可以让"换一种融合策略"不必改 SQL，也让单测可以用内存替身覆盖全部排序逻辑。
    """

    async def get_fingerprints(
        self, collection: str, doc_ids: list[str]
    ) -> dict[str, tuple[str, str]]:
        """已入库文档的 `(content_hash, embedding_model)`，供索引时跳过未变化的文本。"""
        if not doc_ids:
            return {}
        async with new_session() as session:
            rows = await session.execute(
                select(
                    SearchDocumentORM.doc_id,
                    SearchDocumentORM.content_hash,
                    SearchDocumentORM.embedding_model,
                ).where(
                    SearchDocumentORM.collection == collection,
                    SearchDocumentORM.doc_id.in_(doc_ids),
                )
            )
            return {row.doc_id: (row.content_hash, row.embedding_model) for row in rows}

    async def upsert_many(self, docs: list[StoredSearchDocument]) -> None:
        """批量 upsert（主键 `(collection, doc_id)`），一个事务内完成。

        `created_at` 冲突时保留原值：它表达"这条记忆最初是什么时候进库的"，重新同步一次
        工具箱不该让所有模板看起来都是刚刚才出现的。
        """
        if not docs:
            return
        now = datetime.now(UTC)
        async with new_session() as session, session.begin():
            for item in docs:
                stmt = pg_insert(SearchDocumentORM).values(
                    collection=item.document.collection,
                    doc_id=item.document.doc_id,
                    text=item.document.text,
                    lexical_text=item.lexical_text,
                    doc_metadata=item.document.metadata,
                    embedding=item.embedding,
                    embedding_model=item.embedding_model,
                    content_hash=item.content_hash,
                    created_at=now,
                    updated_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["collection", "doc_id"],
                    set_={
                        "text": stmt.excluded.text,
                        "lexical_text": stmt.excluded.lexical_text,
                        "metadata": stmt.excluded.metadata,
                        "embedding": stmt.excluded.embedding,
                        "embedding_model": stmt.excluded.embedding_model,
                        "content_hash": stmt.excluded.content_hash,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
                await session.execute(stmt)

    async def update_metadata(self, collection: str, doc_id: str, metadata: dict[str, object]) -> None:
        """只更新元数据（文本未变时同步 commit_sha 等溯源字段，不必重新 embed）。"""
        async with new_session() as session, session.begin():
            await session.execute(
                update(SearchDocumentORM)
                .where(
                    SearchDocumentORM.collection == collection,
                    SearchDocumentORM.doc_id == doc_id,
                )
                .values(doc_metadata=metadata, updated_at=datetime.now(UTC))
            )

    async def delete_missing(self, collection: str, keep_doc_ids: list[str]) -> int:
        """删除集合里不在 `keep_doc_ids` 中的文档，返回删除条数（集合同步用）。"""
        async with new_session() as session, session.begin():
            stmt = delete(SearchDocumentORM).where(SearchDocumentORM.collection == collection)
            if keep_doc_ids:
                stmt = stmt.where(SearchDocumentORM.doc_id.not_in(keep_doc_ids))
            result = await session.execute(stmt)
            return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def dense_search(
        self,
        collection: str,
        query_embedding: list[float],
        *,
        embedding_model: str,
        limit: int,
        metadata_filter: dict[str, object] | None = None,
    ) -> list[tuple[SearchDocument, float]]:
        """pgvector 余弦近邻，返回 `(文档, 余弦相似度)`，相似度降序。

        带 `embedding_model` 过滤：不同模型的向量不在同一空间，混算得到的是一个看起来正常
        却毫无意义的相似度。注意 HNSW 与 WHERE 过滤叠加时，候选数受 `hnsw.ef_search`（默认 40）
        限制，`limit` 保持在几十条以内即可。
        """
        distance = SearchDocumentORM.embedding.cosine_distance(query_embedding)
        async with new_session() as session:
            query = select(SearchDocumentORM, distance.label("distance")).where(
                SearchDocumentORM.collection == collection,
                SearchDocumentORM.embedding_model == embedding_model,
            )
            if metadata_filter:
                query = query.where(SearchDocumentORM.doc_metadata.contains(metadata_filter))
            rows = await session.execute(query.order_by(distance).limit(limit))
            return [(_orm_to_search_document(row[0]), 1.0 - float(row[1])) for row in rows]

    async def lexical_search(
        self,
        collection: str,
        tokens: list[str],
        *,
        limit: int,
        metadata_filter: dict[str, object] | None = None,
    ) -> list[tuple[SearchDocument, float]]:
        """Postgres 全文检索，返回 `(文档, 归一化 ts_rank_cd)`，得分降序。

        查询词元以 OR 连接：BM25 路的职责是"低频专有词（如 `pdfplumber`）一旦出现就要命中"，
        AND 语义会让一个多词查询因为缺一个词而整体落空。`ts_rank_cd(..., 32)` 把得分归一化为
        `rank / (rank + 1)`，落在 0~1。词元由 `lexical_tokens()` 产出，只含字母数字下划线与汉字，
        不含 tsquery 运算符，可以安全拼接。
        """
        if not tokens:
            return []
        tsquery = func.to_tsquery("simple", " | ".join(tokens))
        rank = func.ts_rank_cd(_TEXT_SEARCH_COLUMN, tsquery, 32)
        async with new_session() as session:
            query = select(SearchDocumentORM, rank.label("rank")).where(
                SearchDocumentORM.collection == collection,
                _TEXT_SEARCH_COLUMN.op("@@")(tsquery),
            )
            if metadata_filter:
                query = query.where(SearchDocumentORM.doc_metadata.contains(metadata_filter))
            rows = await session.execute(query.order_by(desc("rank")).limit(limit))
            return [(_orm_to_search_document(row[0]), float(row[1])) for row in rows]

    async def list_documents(
        self,
        collection: str,
        *,
        metadata_filter: dict[str, object] | None = None,
        limit: int = 100,
    ) -> list[SearchDocument]:
        """按元数据过滤列出文档（如取某份归档范本下的全部用例），按写入时间先后。"""
        async with new_session() as session:
            query = select(SearchDocumentORM).where(SearchDocumentORM.collection == collection)
            if metadata_filter:
                query = query.where(SearchDocumentORM.doc_metadata.contains(metadata_filter))
            rows = (
                await session.execute(
                    query.order_by(SearchDocumentORM.created_at, SearchDocumentORM.doc_id).limit(
                        limit
                    )
                )
            ).scalars()
            return [_orm_to_search_document(row) for row in rows]

    async def count(
        self, collection: str, *, metadata_filter: dict[str, object] | None = None
    ) -> int:
        async with new_session() as session:
            query = (
                select(func.count())
                .select_from(SearchDocumentORM)
                .where(SearchDocumentORM.collection == collection)
            )
            if metadata_filter:
                query = query.where(SearchDocumentORM.doc_metadata.contains(metadata_filter))
            return int((await session.execute(query)).scalar_one())


def _orm_to_search_document(row: SearchDocumentORM) -> SearchDocument:
    # 不回传 embedding：见 SearchDocument.embedding 字段注释。
    return SearchDocument(
        doc_id=row.doc_id,
        collection=row.collection,
        text=row.text,
        metadata=dict(row.doc_metadata or {}),
    )
