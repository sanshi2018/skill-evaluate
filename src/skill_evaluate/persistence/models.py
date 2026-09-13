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

from pgvector.sqlalchemy import VECTOR
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
    # docs/dev/13：渐进式披露动态探查用例指向的参考文件路径；其余类别恒为 NULL。
    # nullable 是刻意的——它只对两类新用例有意义，给其余几千条用例强塞一个空串
    # 会让"没有探查目标"和"探查目标是空路径"在查询里分不开。
    probe_target_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    # docs/dev/15：ADVERSARIAL 用例的攻击子类型（`AttackSubtype` 枚举值）。
    # 建索引是因为五条探测支路每次都要按它切分用例子集，而对抗用例会随着
    # 模块五反复补题而增长。其余类别恒为 NULL。
    attack_subtype: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
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
    # docs/dev/15 第 10.1 节：声明了 `to_severity` 的评审模板（当前只有
    # `security_severity_rating`）会回填严重级别。nullable 且无默认值——绝大多数
    # 判定只有 pass/fail，给它们塞一个 "low" 会让"没有严重级别"和"判定为低危"
    # 在查询里分不开。
    severity: Mapped[str | None] = mapped_column(String, nullable=True)


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
    # docs/dev/10 追加（revision 0005）：脚本正文与 NONE 策略的原因。
    # 脚本正文必须落库——断点恢复后重新下发沙箱时要用同一份脚本，重新生成一份
    # 会让恢复前后的断言不是同一条断言。
    script_content: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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


# --------------------------------------------------------------------------- #
# docs/dev/08：裁判可信度机制（黄金基准盲测 + 失误率冻结）
# --------------------------------------------------------------------------- #


class GoldenCaseORM(Base):
    """人类专家预标定的黄金用例（docs/dev/08 第 3.1 节）。

    本表**只被消费不被生产**：数据由运维/资深工程师通过审查工作台（docs/dev/22）
    或直接写库补充。
    """

    __tablename__ = "golden_cases"

    golden_id: Mapped[str] = mapped_column(String, primary_key=True)
    template_key: Mapped[str] = mapped_column(String, index=True, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    human_labeled_status: Mapped[str] = mapped_column(String, nullable=False)
    human_labeled_reasoning: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JudgeMissRecordORM(Base):
    """每一次黄金用例判决的记账（命中与失误都记，见 `state/golden.py`）。"""

    __tablename__ = "judge_miss_records"

    miss_id: Mapped[str] = mapped_column(String, primary_key=True)
    golden_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    judge_output_status: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, index=True, nullable=False)
    temperature: Mapped[float] = mapped_column(Float, nullable=False)
    # 冻结粒度是 (model, temperature_bucket)，因此分桶值直接落库而不是每次现算，
    # 保证"当时按哪个桶统计的"可回溯（分桶规则将来变了也不会改写历史）。
    temperature_bucket: Mapped[str] = mapped_column(String, index=True, nullable=False)
    is_miss: Mapped[bool] = mapped_column(Boolean, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JudgeHealthStatusORM(Base):
    """按 `(model, temperature_bucket)` 独立冻结/解冻的裁判健康状态（docs/dev/08 第 3.3 节）。"""

    __tablename__ = "judge_health_status"
    __table_args__ = (
        UniqueConstraint("model", "temperature_bucket", name="uq_judge_health_config"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid_str)
    model: Mapped[str] = mapped_column(String, nullable=False)
    temperature_bucket: Mapped[str] = mapped_column(String, nullable=False)
    frozen: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    miss_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    window_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --------------------------------------------------------------------------- #
# docs/dev/09：Optimizer 补丁与闭环重试
# --------------------------------------------------------------------------- #


class PatchORM(Base):
    """候选补丁（docs/dev/09 第 2 节）。不是 git commit——转正式提交是 docs/dev/24 的事。"""

    __tablename__ = "patches"

    patch_id: Mapped[str] = mapped_column(String, primary_key=True)
    skill_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    base_skill_version_ref: Mapped[str] = mapped_column(String, nullable=False)
    patch_type: Mapped[str] = mapped_column(String, nullable=False)
    target_path: Mapped[str] = mapped_column(String, nullable=False)
    diff: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str] = mapped_column(String, nullable=False)
    triggered_by_finding_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PatchApplicationResultORM(Base):
    """一次补丁应用 + 回归的结果（docs/dev/09 第 2、5 节）。

    同一个 patch 只会被应用一次（`OptimizationLoop` 不重试同一个 patch），故以
    `patch_id` 为主键。
    """

    __tablename__ = "patch_application_results"

    patch_id: Mapped[str] = mapped_column(String, primary_key=True)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False)
    regression_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    working_skill_version_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str] = mapped_column(String, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NodeRetryCountORM(Base):
    """`PipelineState.retry_counts` 的落盘形态（docs/dev/09 第 5 节）。

    为什么不直接改 checkpoint 里的 `PipelineState.retry_counts`：`OptimizationLoop`
    的重试发生在**一个节点内部**的循环里，此时该节点的状态更新还没有被 LangGraph
    合并回图状态；把计数写进一张独立小表，既能让循环中途崩溃后的重启看到真实的
    已重试次数，也能让审批工作台（docs/dev/22）在节点挂起时直接查到"它试了几次"。
    """

    __tablename__ = "node_retry_counts"
    __table_args__ = (UniqueConstraint("run_id", "node_name", name="uq_node_retry_counts_key"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid_str)
    run_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    node_name: Mapped[str] = mapped_column(String, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --------------------------------------------------------------------------- #
# docs/dev/17：模块七——用例集瘦身与动态演进
# --------------------------------------------------------------------------- #


class TestCaseSuggestionORM(Base):
    """孤儿用例等"建议人工处置"的非阻塞待办（docs/dev/17 第 6.1 节）。

    与 `human_approvals` 的分工见 `state/suggestion.py` 的模块头：那张表是**阻塞式**
    挂起点的账本（以 `wait_key` 为锚、绑一次运行），本表是**非阻塞**建议队列
    （以 `case_id` 为锚、跨运行长期存活）。

    `(case_id, suggestion_type)` 唯一：同一条孤儿用例连续三次评测都会被检出，但人
    只需要处理一次。把去重放在库层面而不是节点里先查后写，是因为多个 run 可能并发
    跑同一个 Skill，"先 SELECT 再 INSERT"这条路径在并发下必然产生重复待办。

    没有 `run_id` 列也是刻意的：建议的生命周期比一次运行长得多（人可能过一周才来
    处理），挂上 run_id 会诱使工作台按运行过滤，从而漏掉上周检出、至今没人管的那些。
    要追溯"哪次运行检出的"，看结构化日志事件 `pruning_orphan_case_detected`。
    """

    __tablename__ = "test_case_suggestions"
    __table_args__ = (
        UniqueConstraint("case_id", "suggestion_type", name="uq_test_case_suggestions_case_type"),
    )

    suggestion_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    suggestion_type: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    # `status` 建索引：工作台的主查询是"列出所有 pending 的建议"，而 pending 只占
    # 全表的一小部分（处理过的会长期留存作审计），这正是索引最划算的形状。
    status: Mapped[str] = mapped_column(String, default="pending", index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --------------------------------------------------------------------------- #
# docs/dev/21：Generator 可信度（最小向量基础设施 + 坍塌事件）与前置门禁
# --------------------------------------------------------------------------- #

# `case_embeddings.embedding` 的定长维度。必须与 `GeneratorTrustSettings.embedding_dimensions`
# 一致（客户端会校验），改维度 = 新 revision 重建列 + 全量重算，不是改一个配置项的事。
CASE_EMBEDDING_DIMENSIONS = 1536


class CaseEmbeddingORM(Base):
    """测试用例 prompt 的向量（docs/dev/21 第 2.1 节）。

    外键指向 `test_cases`：向量只为**已落库**的用例而存。这条约束直接决定了写入时序——
    被判定坍塌、未激活的那批用例不落 `test_cases`，也就不会把自己的向量混进"历史分布"，
    否则下一次生成会拿一批废题当参照系，越比越像。

    docs/dev/23 的 `search_documents` 是独立的姊妹表（带全文检索与元数据的长期记忆库），
    本表只服务坍塌检测这一类"新旧分布距离"计算。
    """

    __tablename__ = "case_embeddings"

    case_id: Mapped[str] = mapped_column(
        String, ForeignKey("test_cases.case_id", ondelete="CASCADE"), primary_key=True
    )
    skill_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        VECTOR(CASE_EMBEDDING_DIMENSIONS), nullable=False
    )
    # 记录是哪个模型算的：换 embedding 模型后新旧向量不在同一空间，距离毫无意义。
    # 查询历史时按当前模型过滤，旧模型的向量自然退出参照系（而不是悄悄混算）。
    embedding_model: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GenerationCollapseEventORM(Base):
    """一次被阻断的生成坍塌（docs/dev/21 第 2.2 节）。

    只记坍塌、不记成功：连续坍塌次数 = "自该 Skill 当前 active 用例集版本创建以来的坍塌
    事件数"。成功激活本身就会刷新 `test_suite_versions.created_at`，不需要再维护一个会与
    真实激活状态不同步的计数器。
    """

    __tablename__ = "generation_collapse_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid_str)
    skill_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    generator_run_id: Mapped[str] = mapped_column(String, nullable=False)
    generation_mode: Mapped[str] = mapped_column(String, nullable=False)
    triggered_by: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)  # CollapseReason 枚举值
    avg_distance_to_history: Mapped[float | None] = mapped_column(Float, nullable=True)
    intra_batch_distance: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    historical_count: Mapped[int] = mapped_column(Integer, nullable=False)
    new_case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CanaryProbeHistoryORM(Base):
    """金丝雀探针的执行记录（docs/dev/21 第 6 节）。

    `nightly_or_image_change` 模式据此判断"镜像没变 + 24h 内成功过"即跳过。失败记录同样
    落库：它们不参与跳过判定，但运维排查"沙箱从什么时候开始坏的"时是第一手线索。
    """

    __tablename__ = "canary_probe_history"

    probe_id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid_str)
    run_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    image_ref: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reasons: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    probed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
