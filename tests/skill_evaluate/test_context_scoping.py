"""docs/dev/12：模块二——上下文利用率与范围界定静态评测。

覆盖：Token 计数器的精度标注与降级、行数/Token 卡线（含估算值不确定带）、渐进式
披露的正则初筛（语义单元边界、两种缺失成因、大文档未拆分）、给 Mini Agent 的
清单渲染、三项同行评审的并发调用与 criticality 声明、黄金盲测跳过、共识未达成时
挂起、报告口径（硬性超标才阻断、主观审查只告警）、以及子图结构。
全部用替身注入，不碰数据库、不发真实请求。
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from skill_evaluate.agents.judge.golden_injector import golden_subject_id
from skill_evaluate.config import ContextScopingSettings
from skill_evaluate.errors import ConfigurationError, PersistenceError, PipelineSuspended
from skill_evaluate.ingestion.token_counter import (
    TokenCount,
    count_tokens,
    heuristic_token_count,
)
from skill_evaluate.nodes.context_scoping import (
    DIMENSION,
    NODE_NAMES,
    PEER_REVIEW_TEMPLATE_KEYS,
    ContextScopingDeps,
    ContextScopingPipeline,
    build_context_scoping_subgraph,
    format_reference_files_for_review,
    scan_progressive_disclosure,
    scan_static_metrics,
)
from skill_evaluate.nodes.context_scoping.graph import INTERRUPT_BEFORE_NODES
from skill_evaluate.nodes.context_scoping.state import (
    KEY_DISCLOSURE_SCAN,
    KEY_PEER_REVIEW_OUTCOMES,
    KEY_STATIC_METRICS,
)
from skill_evaluate.state.enums import Criticality, JudgeVerdictStatus
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.skill import SkillDefinition, SkillReferenceFile

SKILL_ID = "csv-cleaner"
RUN_ID = "run-1"
VERSION_REF = "v1"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def _skill(
    body: str = "# CSV Cleaner\n\n本项目里的 id 一律指外部系统 id。\n",
    *,
    references: list[str] | None = None,
    line_count: int | None = None,
    token_count: int = 40,
) -> SkillDefinition:
    return SkillDefinition(
        skill_id=SKILL_ID,
        version_ref=VERSION_REF,
        root_path=".",
        description="清洗并校验 CSV 导出文件",
        body_markdown=body,
        line_count=len(body.splitlines()) if line_count is None else line_count,
        token_count=token_count,
        reference_files=[
            SkillReferenceFile(path=path) for path in (references or [])
        ],
    )


def _verdict(status: JudgeVerdictStatus, *, subject_id: str = SKILL_ID) -> JudgeVerdict:
    return JudgeVerdict(
        verdict_id=f"v-{subject_id}-{status.value}",
        subject_id=subject_id,
        status=status,
        reasoning="原文片段：……" + "补" * 300,
        temperature=0.1,
        model="anthropic/claude-haiku-4.5",
        created_at=datetime.now(UTC),
    )


class FakeSkillRepo:
    def __init__(self, skill: SkillDefinition | None) -> None:
        self.skill = skill

    async def get(self, skill_id: str, version_ref: str) -> SkillDefinition | None:
        return self.skill


class FakeReporter:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record_dimension_result(self, **kwargs: Any) -> None:
        self.recorded.append(kwargs)


class FakeJudge:
    """替代 `JudgeAgent`：按 template_key 回放判定，并记录调用参数。"""

    def __init__(self, results: dict[str, JudgeVerdict | ConsensusResult]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    async def judgmental_verdict(
        self,
        subject_id: str,
        template_key: str,
        content: dict[str, str],
        criticality: Criticality,
    ) -> JudgeVerdict | ConsensusResult:
        self.calls.append(
            {
                "subject_id": subject_id,
                "template_key": template_key,
                "content": content,
                "criticality": criticality,
            }
        )
        return self.results.get(template_key, _verdict(JudgeVerdictStatus.PASS))


def _deps(
    skill: SkillDefinition,
    *,
    judge: FakeJudge | None = None,
    reporter: FakeReporter | None = None,
    settings: ContextScopingSettings | None = None,
    token_counter: Any = count_tokens,
) -> ContextScopingDeps:
    return ContextScopingDeps(
        judge_agent=judge or FakeJudge({}),  # type: ignore[arg-type]  测试替身
        report_generator=reporter or FakeReporter(),  # type: ignore[arg-type]
        skill_repository=FakeSkillRepo(skill),  # type: ignore[arg-type]
        token_counter=token_counter,
        scoping_settings=settings or ContextScopingSettings(),
    )


def _state(**extra: Any) -> Any:
    return {
        "run_id": RUN_ID,
        "skill_id": SKILL_ID,
        "skill_version_ref": VERSION_REF,
        **extra,
    }


# --------------------------------------------------------------------------- #
# 1. Token 计数器
# --------------------------------------------------------------------------- #


class TokenCounterTests:
    def test_heuristic_is_marked_inexact(self) -> None:
        # 精度元信息是卡线判定的输入：估算值必须自报家门，否则会被当成精确值阻断。
        count = heuristic_token_count("hello world " * 10)
        assert count.exact is False
        assert count.method.startswith("heuristic")
        assert count.value > 0

    def test_default_counter_reports_its_method(self) -> None:
        count = count_tokens("abc")
        assert count.method in {"tiktoken:o200k_base", "heuristic:chars-x0.75"}
        # 两条路径的语义约定：tiktoken 精确，字符比例不精确。
        assert count.exact is count.method.startswith("tiktoken")

    def test_fallback_is_three_quarters_of_the_character_count(self) -> None:
        # 兜底口径就是字符数 × 3/4，不按中英文分档——见 token_counter.py 的说明。
        assert heuristic_token_count("x" * 100).value == 75
        assert heuristic_token_count("中" * 100).value == 75


# --------------------------------------------------------------------------- #
# 2. 硬性指标扫描
# --------------------------------------------------------------------------- #


def _exact(value: int) -> Any:
    """一个返回固定 Token 数的精确计数器替身。"""
    return lambda text: TokenCount(value=value, method="fake:exact", exact=True)


def _estimated(value: int) -> Any:
    return lambda text: TokenCount(value=value, method="fake:estimate", exact=False)


class StaticMetricsTests:
    def test_counts_body_lines_and_flags_limits(self) -> None:
        skill = _skill(body="line\n" * 12)
        metrics = scan_static_metrics(
            skill, line_limit=10, token_limit=100, token_counter=_exact(120)
        )
        assert metrics.line_count == 12
        assert metrics.line_limit_exceeded is True
        assert metrics.token_limit_exceeded is True
        assert metrics.hard_fail is True

    def test_within_limits_is_not_a_hard_fail(self) -> None:
        metrics = scan_static_metrics(
            _skill(), line_limit=500, token_limit=5000, token_counter=_exact(100)
        )
        assert metrics.hard_fail is False
        assert metrics.needs_human_confirmation is False

    def test_estimated_count_near_limit_defers_to_a_human(self) -> None:
        # 5200 落在 5000 ±15% 内且是估算值：不阻断，改为请人确认。
        metrics = scan_static_metrics(
            _skill(), token_limit=5000, token_counter=_estimated(5200)
        )
        assert metrics.token_limit_exceeded is True
        assert metrics.needs_human_confirmation is True
        assert metrics.hard_fail is False

    def test_estimated_count_far_beyond_limit_still_blocks(self) -> None:
        # 估算值也不是永远不作数：远超限额时偏差解释不了这个差距。
        metrics = scan_static_metrics(
            _skill(), token_limit=5000, token_counter=_estimated(9000)
        )
        assert metrics.needs_human_confirmation is False
        assert metrics.hard_fail is True

    def test_exact_count_near_limit_blocks_without_asking(self) -> None:
        metrics = scan_static_metrics(_skill(), token_limit=5000, token_counter=_exact(5200))
        assert metrics.needs_human_confirmation is False
        assert metrics.hard_fail is True

    def test_line_overflow_blocks_even_when_tokens_are_estimated(self) -> None:
        # 行数是数换行符数出来的，永远精确，不受计数器精度影响。
        metrics = scan_static_metrics(
            _skill(body="line\n" * 600), line_limit=500, token_counter=_estimated(10)
        )
        assert metrics.hard_fail is True

    def test_recomputes_instead_of_trusting_stored_fields(self) -> None:
        # 库里的字段可能是旧计数器算的，卡线必须以当场计算为准。
        skill = _skill(body="a\nb\nc\n", line_count=999, token_count=99999)
        metrics = scan_static_metrics(skill, token_counter=_exact(30))
        assert metrics.line_count == 3
        assert metrics.token_count == 30


# --------------------------------------------------------------------------- #
# 3. 渐进式披露初筛
# --------------------------------------------------------------------------- #


class ProgressiveDisclosureScanTests:
    def test_conditional_mention_passes(self) -> None:
        body = "# S\n\n遇到 4xx 报错时，读 references/errors.md 查对照表。\n"
        scan = scan_progressive_disclosure(_skill(body, references=["references/errors.md"]))
        assert scan.candidates == []

    def test_bare_pointer_is_flagged(self) -> None:
        body = "# S\n\n详见 references/errors.md。\n"
        scan = scan_progressive_disclosure(_skill(body, references=["references/errors.md"]))
        assert [c.reason for c in scan.candidates] == ["no_condition"]
        assert scan.candidates[0].evidence == "详见 references/errors.md。"

    def test_unmentioned_file_is_flagged_with_its_own_reason(self) -> None:
        # 与"提了但没说何时看"分开报：对作者而言这是两件不同的事。
        scan = scan_progressive_disclosure(
            _skill("# S\n\n没有提到任何附属文件。\n", references=["references/errors.md"])
        )
        assert [c.reason for c in scan.candidates] == ["not_mentioned"]
        assert scan.candidates[0].evidence is None

    def test_condition_from_a_neighbouring_list_item_does_not_count(self) -> None:
        # 关键边界：条件词写在**上一个**列表项里，不该让下一项蒙混过关。
        body = "# S\n\n- 当导出失败时，读 references/export.md\n- 详见 references/errors.md\n"
        scan = scan_progressive_disclosure(
            _skill(body, references=["references/errors.md", "references/export.md"])
        )
        assert scan.candidate_paths == ["references/errors.md"]

    def test_condition_elsewhere_in_the_same_paragraph_counts(self) -> None:
        # 同一自然段内的条件表述算数：作者常把条件写在前半句。
        body = "# S\n\n如果导出文件带 BOM 头，\n就去看 references/encodings.md。\n"
        scan = scan_progressive_disclosure(
            _skill(body, references=["references/encodings.md"])
        )
        assert scan.candidates == []

    def test_condition_in_an_unrelated_paragraph_does_not_count(self) -> None:
        body = "# S\n\n如果磁盘满了就报错。\n\n详见 references/errors.md。\n"
        scan = scan_progressive_disclosure(_skill(body, references=["references/errors.md"]))
        assert scan.candidate_paths == ["references/errors.md"]

    def test_large_body_without_references_is_flagged(self) -> None:
        scan = scan_progressive_disclosure(
            _skill(body="line\n" * 450), line_limit=500, bulk_inline_ratio=0.8
        )
        assert scan.bulk_inline_without_references is True

    def test_small_body_without_references_is_fine(self) -> None:
        scan = scan_progressive_disclosure(_skill(), line_limit=500, token_limit=5000)
        assert scan.bulk_inline_without_references is False

    def test_uses_the_passed_token_count_over_the_stored_field(self) -> None:
        skill = _skill(token_count=99999)  # 库里的旧值会误判为"体量巨大"
        scan = scan_progressive_disclosure(skill, token_limit=5000, token_count=100)
        assert scan.bulk_inline_without_references is False

    def test_review_listing_carries_the_screening_verdict(self) -> None:
        skill = _skill(
            "# S\n\n遇到编码问题时读 references/a.md。\n\n详见 references/b.md。\n",
            references=["references/a.md", "references/b.md"],
        )
        rendered = format_reference_files_for_review(
            skill, scan_progressive_disclosure(skill)
        )
        # 合格项也要出现在清单里：只喂可疑项会诱导模型把每一项都判成问题。
        assert "references/a.md" in rendered
        assert "存在条件性表述" in rendered
        assert "没有条件性表述" in rendered

    def test_review_listing_handles_no_references(self) -> None:
        skill = _skill()
        rendered = format_reference_files_for_review(skill, scan_progressive_disclosure(skill))
        assert "没有 references/" in rendered


# --------------------------------------------------------------------------- #
# 4. 节点行为
# --------------------------------------------------------------------------- #


class NodeTests:
    async def test_static_metrics_node_writes_its_private_key(self) -> None:
        pipeline = ContextScopingPipeline(_deps(_skill(), token_counter=_exact(4200)))
        result = await pipeline.static_metrics_scan(_state())
        assert result[KEY_STATIC_METRICS]["token_count"] == 4200

    async def test_missing_skill_is_rejected_loudly(self) -> None:
        pipeline = ContextScopingPipeline(_deps(_skill()))
        pipeline.deps.skill_repository = FakeSkillRepo(None)  # type: ignore[assignment]
        with pytest.raises(PersistenceError, match="未找到被测 Skill"):
            await pipeline.static_metrics_scan(_state())

    async def test_peer_review_calls_all_three_templates_as_routine(self) -> None:
        judge = FakeJudge({})
        pipeline = ContextScopingPipeline(_deps(_skill(), judge=judge))
        await pipeline.mini_agent_peer_review(_state())

        assert [c["template_key"] for c in judge.calls] == list(PEER_REVIEW_TEMPLATE_KEYS)
        # ROUTINE 而非 CRITICAL：静态审查是建议，不值三倍成本的共识投票。
        assert {c["criticality"] for c in judge.calls} == {Criticality.ROUTINE}
        assert {c["subject_id"] for c in judge.calls} == {SKILL_ID}

    async def test_peer_review_feeds_the_screening_result_to_the_template(self) -> None:
        judge = FakeJudge({})
        skill = _skill("# S\n\n详见 references/errors.md。\n", references=["references/errors.md"])
        pipeline = ContextScopingPipeline(_deps(skill, judge=judge))
        scan = await pipeline.progressive_disclosure_static_scan(_state())
        await pipeline.mini_agent_peer_review(_state(**scan))

        content = next(
            c["content"]
            for c in judge.calls
            if c["template_key"] == "progressive_disclosure_static"
        )
        assert "references/errors.md" in content["reference_files"]
        assert "没有条件性表述" in content["reference_files"]

    async def test_peer_review_records_verdict_ids_and_truncates_reasoning(self) -> None:
        judge = FakeJudge({"scoping_check": _verdict(JudgeVerdictStatus.FAIL)})
        pipeline = ContextScopingPipeline(_deps(_skill(), judge=judge))
        result = await pipeline.mini_agent_peer_review(_state())

        assert len(result["judge_verdict_ids"]) == 3
        outcomes = result[KEY_PEER_REVIEW_OUTCOMES]
        failed = [o for o in outcomes if o["status"] == JudgeVerdictStatus.FAIL]
        assert [o["template_key"] for o in failed] == ["scoping_check"]
        # 整段 reasoning 已随 verdict 落库，状态里只留摘要。
        assert len(failed[0]["reasoning_excerpt"]) == 200

    async def test_golden_blind_test_result_is_skipped_not_counted(self) -> None:
        judge = FakeJudge(
            {
                "omission_audit": _verdict(
                    JudgeVerdictStatus.FAIL, subject_id=golden_subject_id("g-1")
                )
            }
        )
        pipeline = ContextScopingPipeline(_deps(_skill(), judge=judge))
        result = await pipeline.mini_agent_peer_review(_state())

        outcomes = {o["template_key"]: o for o in result[KEY_PEER_REVIEW_OUTCOMES]}
        # 黄金用例的判决不属于这个 Skill：不计通过/失败，也不进 verdict id 列表。
        assert outcomes["omission_audit"]["status"] is None
        assert outcomes["omission_audit"]["skipped_reason"]
        assert len(result["judge_verdict_ids"]) == 2

    async def test_no_consensus_suspends_instead_of_downgrading(self) -> None:
        # 本维度声明 ROUTINE，正常拿不到 ConsensusResult；这条路径是给"运维把它
        # 调成 CRITICAL"准备的防线：NEEDS_HUMAN_REVIEW 不许被降级成 PASS/FAIL。
        judge = FakeJudge(
            {
                "scoping_check": ConsensusResult(
                    subject_id=SKILL_ID,
                    verdicts=[_verdict(JudgeVerdictStatus.FAIL)],
                    consensus_reached=False,
                    final_status=JudgeVerdictStatus.NEEDS_HUMAN_REVIEW,
                )
            }
        )
        pipeline = ContextScopingPipeline(_deps(_skill(), judge=judge))
        with pytest.raises(PipelineSuspended, match="未达成共识"):
            await pipeline.mini_agent_peer_review(_state())


# --------------------------------------------------------------------------- #
# 5. 报告口径
# --------------------------------------------------------------------------- #


async def _finalize(
    skill: SkillDefinition,
    *,
    judge: FakeJudge | None = None,
    settings: ContextScopingSettings | None = None,
    token_counter: Any = count_tokens,
) -> dict[str, Any]:
    """跑完四个节点，返回写进报告的那一条记录。"""
    reporter = FakeReporter()
    pipeline = ContextScopingPipeline(
        _deps(
            skill, judge=judge, reporter=reporter, settings=settings, token_counter=token_counter
        )
    )
    state = _state()
    state.update(await pipeline.static_metrics_scan(state))
    state.update(await pipeline.progressive_disclosure_static_scan(state))
    state.update(await pipeline.mini_agent_peer_review(state))
    await pipeline.finalize_dimension_report(state)
    return reporter.recorded[0]


class ReportTests:
    async def test_clean_skill_passes_without_blocking(self) -> None:
        recorded = await _finalize(_skill(), token_counter=_exact(100))
        assert recorded["dimension"] == DIMENSION
        assert recorded["status"] is JudgeVerdictStatus.PASS
        assert recorded["blocking"] is False
        assert recorded["score"] is None  # 本维度不产出连续分数

    async def test_hard_limit_overflow_blocks(self) -> None:
        recorded = await _finalize(
            _skill(body="line\n" * 600), settings=ContextScopingSettings(line_limit=500)
        )
        assert recorded["status"] is JudgeVerdictStatus.FAIL
        assert recorded["blocking"] is True
        assert any("行数超标" in f for f in recorded["findings"])

    async def test_peer_review_failure_reports_but_does_not_block(self) -> None:
        # docs/dev/12 第 6 节的关键工程化决策：主观判断只告警。
        judge = FakeJudge({"omission_audit": _verdict(JudgeVerdictStatus.FAIL)})
        recorded = await _finalize(_skill(), judge=judge, token_counter=_exact(100))
        assert recorded["status"] is JudgeVerdictStatus.FAIL
        assert recorded["blocking"] is False
        assert any("[omission_audit]" in f for f in recorded["findings"])

    async def test_uncertain_token_count_asks_for_a_human(self) -> None:
        recorded = await _finalize(_skill(), token_counter=_estimated(5200))
        assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        assert recorded["blocking"] is False
        assert any("估算值" in f for f in recorded["findings"])

    async def test_metrics_line_is_always_present(self) -> None:
        # 贴着线过还是绰绰有余、数字精不精确，读报告的人都得看得见。
        recorded = await _finalize(_skill(), token_counter=_exact(100))
        assert any("静态指标" in f for f in recorded["findings"])

    async def test_screening_candidates_reach_the_findings(self) -> None:
        skill = _skill("# S\n\n详见 references/errors.md。\n", references=["references/errors.md"])
        recorded = await _finalize(skill, token_counter=_exact(100))
        assert any("疑似缺少按需加载触发条件" in f for f in recorded["findings"])
        # 初筛不下定论：措辞上必须让位给同行评审的结论。
        assert any("以同行评审结论为准" in f for f in recorded["findings"])

    async def test_screening_tolerance_suppresses_the_whole_list(self) -> None:
        # 容忍度是整体阈值：命中数不超过它就一条都不报（"报三条里的后两条"对读报告
        # 的人毫无意义——他无从知道被吞掉的是哪一条）。
        skill = _skill("# S\n\n详见 references/errors.md。\n", references=["references/errors.md"])
        recorded = await _finalize(
            skill,
            settings=ContextScopingSettings(max_reference_files_without_trigger=1),
            token_counter=_exact(100),
        )
        assert not any("疑似缺少按需加载触发条件" in f for f in recorded["findings"])

    async def test_all_reviews_skipped_needs_human_review(self) -> None:
        judge = FakeJudge(
            {
                key: _verdict(JudgeVerdictStatus.PASS, subject_id=golden_subject_id(f"g-{key}"))
                for key in PEER_REVIEW_TEMPLATE_KEYS
            }
        )
        recorded = await _finalize(_skill(), judge=judge, token_counter=_exact(100))
        # 主观审查一项都没跑成，报 PASS 会掩盖这件事。
        assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW

    async def test_missing_private_state_is_surfaced_not_silently_passed(self) -> None:
        reporter = FakeReporter()
        pipeline = ContextScopingPipeline(_deps(_skill(), reporter=reporter))
        # 模拟"主图状态 schema 漏了私有键"：扫描结果没能传到收尾节点。
        await pipeline.finalize_dimension_report(_state())
        recorded = reporter.recorded[0]
        assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        assert any("私有键" in f for f in recorded["findings"])


# --------------------------------------------------------------------------- #
# 6. 图结构与路由约束
# --------------------------------------------------------------------------- #


class GraphTests:
    def test_subgraph_is_a_straight_line(self) -> None:
        graph = build_context_scoping_subgraph(_deps(_skill())).compile()
        nodes = set(graph.get_graph().nodes)
        assert set(NODE_NAMES.values()) <= nodes
        # 本维度不产生需要人工审批的动作。
        assert INTERRUPT_BEFORE_NODES == []

    def test_disclosure_scan_runs_after_metrics(self) -> None:
        # 顺序是有意义的：初筛要用前一个节点算出的 Token 数。
        graph = build_context_scoping_subgraph(_deps(_skill())).compile()
        edges = {(e.source, e.target) for e in graph.get_graph().edges}
        assert (
            NODE_NAMES["static_metrics_scan"],
            NODE_NAMES["progressive_disclosure_static_scan"],
        ) in edges

    def test_pluggable_routing_is_rejected_at_assembly_time(self, monkeypatch: Any) -> None:
        from skill_evaluate.executors import routing
        from skill_evaluate.state.enums import ExecutorBackendType

        monkeypatch.setitem(
            routing.NODE_BACKEND_ROUTING, DIMENSION, ExecutorBackendType.PLUGGABLE
        )
        with pytest.raises(ConfigurationError, match="纯静态文本审查"):
            ContextScopingPipeline(_deps(_skill()))
