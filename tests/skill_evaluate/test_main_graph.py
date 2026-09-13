"""docs/dev/24：主图编排与 CI/CD 落地。

覆盖：主图 schema（私有键并集、reducer 覆写）、拓扑（每个节点只跑一次、同步屏障、用例集准备节点
串行化、Nightly 分流、无静态中断、全部节点套 guard）、编排层节点（入口登记、报告、归档兜底）、补丁转
PR（合成、冲突、代码补丁、交付流程）、COLD 回归、GraphResumer 按 wait_key 唤醒、运行入口退出码、
CI 辅助函数、报告覆盖率摘要、巡检入口。全部用替身注入，不碰数据库、不起 git/gh、不发真实请求。
"""

from __future__ import annotations

import asyncio
import collections
import typing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from langgraph.types import interrupt

from skill_evaluate.agents.judge.service import JudgeAgent
from skill_evaluate.agents.optimizer.patch_applier import WORKING_COPY_MARKER, render_skill_md
from skill_evaluate.errors import ConfigurationError, DeliveryError, HumanRejectedSuspension
from skill_evaluate.graph import ci_support
from skill_evaluate.graph.cold_suite import DIMENSION as COLD_DIMENSION
from skill_evaluate.graph.cold_suite import SUBJECT_PREFIX, ColdSuiteRegression
from skill_evaluate.graph.main import (
    DIMENSION_TERMINAL_NODES,
    INTERRUPT_BEFORE_NODES,
    SUSPENDABLE_NODES,
    MainGraphDeps,
    build_main_graph,
    build_main_graph_builder,
)
from skill_evaluate.graph.nodes import (
    NODE_NAMES,
    REPORT_HTML_NAME,
    REPORT_JSON_NAME,
    PipelineDeps,
    PipelineNodes,
)
from skill_evaluate.graph.patch_pr import (
    AcceptedPatch,
    PatchToPullRequest,
    branch_name_for,
    collect_accepted_patches,
    compose_changes,
)
from skill_evaluate.graph.resumer import CompiledGraphResumer
from skill_evaluate.graph.runner import (
    EXIT_BLOCKING,
    EXIT_OK,
    EXIT_STOPPED,
    EXIT_SUSPENDED,
    PipelineRunner,
)
from skill_evaluate.graph.state import (
    DIMENSION_STATE_TYPES,
    KEY_ARCHIVE_OUTCOME,
    KEY_MODE,
    KEY_PULL_REQUEST,
    KEY_REPORT_BLOCKING,
    KEY_SKILL_PATH,
    MODE_COLD_SUITE,
    MainGraphState,
    keep_latest_suite_version,
)
from skill_evaluate.ingestion import load_skill
from skill_evaluate.nodes import (
    coverage,
    instruction_control,
    multi_skill,
    preflight,
    security,
    trigger_accuracy,
)
from skill_evaluate.nodes.approval_guard import ApprovalGuard
from skill_evaluate.nodes.instruction_control import state as ic_state
from skill_evaluate.nodes.security import state as sec_state
from skill_evaluate.nodes.trigger_accuracy import state as trigger_state
from skill_evaluate.observability.report_generator import ReportGenerator, summarize_coverage
from skill_evaluate.observability.report_schema import BenchmarkReport, DimensionResult
from skill_evaluate.persistence import suspension
from skill_evaluate.persistence.reaper import reap_once
from skill_evaluate.state.enums import (
    DatasetSplit,
    ExecutorBackendType,
    JudgeVerdictStatus,
    PatchType,
    TestCaseCategory,
)
from skill_evaluate.state.memory import ArchiveOutcome
from skill_evaluate.state.patch import Patch, PatchApplicationResult
from skill_evaluate.state.pipeline_state import PipelineState
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion
from skill_evaluate.state.trace import RUN_INDEX_COLD_SUITE, ExecutionTrace, TimingCostMetrics

SKILL_ID = "csv-cleaner"
RUN_ID = "12345678-aaaa-bbbb-cccc-000000000000"
VERSION = "abc123"

SKILL_MD = """---
name: csv-cleaner
description: 清洗 CSV 导出文件
---
# CSV Cleaner

## 步骤
1. 读取文件
2. 去重
3. 输出结果

## 注意
- 不要覆盖原文件
"""


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def _skill(root: str = ".", *, description: str = "清洗 CSV 导出文件", body: str | None = None) -> SkillDefinition:
    body_text = SKILL_MD.split("---\n", 2)[2] if body is None else body
    return SkillDefinition(
        skill_id=SKILL_ID,
        version_ref=VERSION,
        root_path=root,
        description=description,
        body_markdown=body_text,
        line_count=len(body_text.splitlines()),
        token_count=40,
    )


def _patch(patch_id: str, patch_type: PatchType, target: str = "SKILL.md", diff: str = "") -> Patch:
    return Patch(
        patch_id=patch_id,
        skill_id=SKILL_ID,
        base_skill_version_ref=VERSION,
        patch_type=patch_type,
        target_path=target,
        diff=diff,
        rationale=f"{patch_id} 的修复理由",
        created_at=datetime.now(UTC),
    )


def _accepted(
    dimension: str,
    patch: Patch,
    working: SkillDefinition | None,
    *,
    regression_passed: bool | None = True,
) -> AcceptedPatch:
    return AcceptedPatch(
        dimension=dimension,
        patch=patch,
        application=PatchApplicationResult(
            patch_id=patch.patch_id, applied=True, regression_passed=regression_passed, detail="回归 3/3 通过"
        ),
        working_skill=working,
    )


def _reader(files: dict[str, str]) -> Any:
    return lambda rel: files.get(rel)


def _report(*, blocking: bool = False) -> BenchmarkReport:
    return BenchmarkReport(
        run_id=RUN_ID,
        skill_id=SKILL_ID,
        skill_version_ref=VERSION,
        generated_at=datetime.now(UTC),
        overall_status=JudgeVerdictStatus.FAIL if blocking else JudgeVerdictStatus.PASS,
        dimensions=[
            DimensionResult(
                dimension="trigger_accuracy",
                status=JudgeVerdictStatus.FAIL if blocking else JudgeVerdictStatus.PASS,
                blocking=True,
            )
        ],
        suite_version_id="suite-1",
    )


class FakeReporter(ReportGenerator):
    """真实的 to_json / to_html，替身的 build / record_dimension_result。"""

    def __init__(self, *, blocking: bool = False) -> None:
        super().__init__()
        self.blocking = blocking
        self.build_calls: list[dict[str, Any]] = []
        self.recorded: list[dict[str, Any]] = []

    async def build(self, run_id: str, **kwargs: Any) -> BenchmarkReport:  # type: ignore[override]
        self.build_calls.append({"run_id": run_id, **kwargs})
        return _report(blocking=self.blocking).model_copy(update=kwargs)

    async def record_dimension_result(self, **kwargs: Any) -> None:  # type: ignore[override]
        self.recorded.append(kwargs)


class FakeSkillRepo:
    def __init__(self, skill: SkillDefinition | None = None) -> None:
        self.skill = skill
        self.saved: list[SkillDefinition] = []

    async def save(self, skill: SkillDefinition) -> None:
        self.saved.append(skill)
        self.skill = skill

    async def get(self, skill_id: str, version_ref: str) -> SkillDefinition | None:
        return self.skill


class FakeRunRepo:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.pr_urls: dict[str, str] = {}
        self.suite_versions: dict[str, str] = {}

    async def create(self, **kwargs: Any) -> None:
        self.created.append(kwargs)

    async def record_pr_url(self, run_id: str, pr_url: str) -> None:
        self.pr_urls[run_id] = pr_url

    async def set_suite_version(self, run_id: str, suite_version_id: str) -> None:
        self.suite_versions[run_id] = suite_version_id


class FakeSuiteService:
    def __init__(self) -> None:
        self.forced: list[tuple[str, str]] = []

    async def force_regenerate(self, skill: SkillDefinition, triggered_by: str = "") -> TestSuiteVersion:
        self.forced.append((skill.skill_id, triggered_by))
        return TestSuiteVersion(
            suite_version_id="suite-forced",
            skill_id=skill.skill_id,
            skill_version_ref=skill.version_ref,
            generation_mode="force_regenerate",
            case_ids=["c1"],
            created_at=datetime.now(UTC),
        )


class FakeLangfuse:
    def __init__(self) -> None:
        self.traces: list[tuple[str, str]] = []

    def start_run_trace(self, run_id: str, skill_id: str) -> None:
        self.traces.append((run_id, skill_id))


class FakePatchRepo:
    def __init__(self, patches: dict[str, Patch], results: dict[str, PatchApplicationResult]) -> None:
        self.patches = patches
        self.results = results

    async def get(self, patch_id: str) -> Patch | None:
        return self.patches.get(patch_id)

    async def get_application_result(self, patch_id: str) -> PatchApplicationResult | None:
        return self.results.get(patch_id)


def _write_skill_dir(tmp_path: Path, name: str = "csv-cleaner") -> Path:
    root = tmp_path / name
    (root / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (root / "scripts" / "clean.py").write_text("print('clean')\n", encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# 1. 主图 schema
# --------------------------------------------------------------------------- #


class MainGraphSchemaTests:
    def test_main_schema_contains_every_dimension_private_key(self) -> None:
        main_keys = set(typing.get_type_hints(MainGraphState, include_extras=True))
        for state_type in DIMENSION_STATE_TYPES:
            missing = set(typing.get_type_hints(state_type)) - main_keys
            assert not missing, f"{state_type.__name__} 的私有键没有并进主图：{missing}"

    def test_dimension_private_keys_never_collide(self) -> None:
        """多重继承时同名键"后写胜"不会报错，只会让某个维度悄悄读到别人的数据。"""
        public = set(typing.get_type_hints(PipelineState))
        owners: dict[str, list[str]] = collections.defaultdict(list)
        for state_type in DIMENSION_STATE_TYPES:
            for key in typing.get_type_hints(state_type):
                if key not in public:
                    owners[key].append(state_type.__name__)
        assert {k: v for k, v in owners.items() if len(v) > 1} == {}

    def test_active_suite_version_reducer_accepts_concurrent_writes(self) -> None:
        assert keep_latest_suite_version("v1", "v2") == "v2"
        assert keep_latest_suite_version("v1", None) == "v1"
        assert keep_latest_suite_version(None, "v1") == "v1"

    async def test_parallel_nodes_writing_suite_version_do_not_crash_the_graph(self) -> None:
        async def write_a(state: MainGraphState) -> dict[str, Any]:
            return {"active_suite_version_id": "suite-a"}

        async def write_b(state: MainGraphState) -> dict[str, Any]:
            return {"active_suite_version_id": "suite-a"}

        builder: Any = StateGraph(MainGraphState)
        builder.add_node("a", write_a)
        builder.add_node("b", write_b)
        builder.add_edge("__start__", "a")
        builder.add_edge("__start__", "b")
        graph = builder.compile()
        result = await graph.ainvoke({"run_id": RUN_ID})
        assert result["active_suite_version_id"] == "suite-a"


# --------------------------------------------------------------------------- #
# 2. 拓扑
# --------------------------------------------------------------------------- #


async def _execute_topology(state: dict[str, Any]) -> tuple[collections.Counter[str], list[str]]:
    """用真实主图的边/条件边/同步屏障 + 替身节点跑一遍，记录每个节点的执行次数与顺序。"""
    real = build_main_graph_builder()
    runs: collections.Counter[str] = collections.Counter()
    order: list[str] = []
    clone: Any = StateGraph(MainGraphState)
    # 条件边的输入 schema 引用了各维度的状态类型，克隆图里要先注册。
    for state_type in DIMENSION_STATE_TYPES:
        clone._add_schema(state_type)

    def stub(name: str) -> Any:
        async def node(_: MainGraphState) -> dict[str, Any]:
            runs[name] += 1
            order.append(name)
            return {}

        return node

    for name in real.nodes:
        clone.add_node(name, stub(name))
    clone.edges = set(real.edges)
    clone.waiting_edges = set(real.waiting_edges)
    clone.branches = real.branches
    graph = clone.compile(checkpointer=InMemorySaver())
    await graph.ainvoke(state, {"configurable": {"thread_id": "t"}, "recursion_limit": 250})
    return runs, order


class MainGraphTopologyTests:
    async def test_full_mode_runs_each_node_once_and_report_waits_for_all_terminals(self) -> None:
        runs, order = await _execute_topology(
            {"run_id": RUN_ID, "skill_id": SKILL_ID, "skill_version_ref": VERSION}
        )
        # 条件分支里没走到的节点（闭环、补题、Nightly）之外，每个节点恰好执行一次。
        assert {name: count for name, count in runs.items() if count != 1} == {}
        report = order.index(NODE_NAMES["report"])
        for terminal in DIMENSION_TERMINAL_NODES:
            assert order.index(terminal) < report, terminal
        assert order[-2:] == [NODE_NAMES["patch_pr"], NODE_NAMES["rag_archive"]]
        assert "nightly.cold_suite_regression" not in runs

    async def test_suite_mutating_prepare_nodes_are_serialized(self) -> None:
        _, order = await _execute_topology(
            {"run_id": RUN_ID, "skill_id": SKILL_ID, "skill_version_ref": VERSION}
        )
        sequence = [
            trigger_accuracy.ENTRY_NODE,
            security.ENTRY_NODE,
            instruction_control.ENTRY_NODE,
            coverage.ENTRY_NODE,
        ]
        positions = [order.index(name) for name in sequence]
        assert positions == sorted(positions)
        # 模块三同时等模块一训练集判定；模块九等模块一终节点；模块十等模块八。
        assert order.index(trigger_accuracy.NODE_NAMES["judge_train_cases"]) < positions[2]
        assert order.index(trigger_accuracy.TERMINAL_NODE) < order.index("cross_model.prepare_cross_model_sample")
        assert order.index("coverage.finalize_weighted_coverage_report") < order.index(multi_skill.ENTRY_NODE)
        assert order[:3] == [NODE_NAMES["bootstrap_run"], preflight.ENTRY_NODE, preflight.TERMINAL_NODE]

    async def test_cold_suite_mode_only_runs_preflight_and_cold_regression(self) -> None:
        _, order = await _execute_topology(
            {"run_id": RUN_ID, "skill_id": SKILL_ID, "skill_version_ref": VERSION, KEY_MODE: MODE_COLD_SUITE}
        )
        assert order == [
            NODE_NAMES["bootstrap_run"],
            preflight.ENTRY_NODE,
            preflight.TERMINAL_NODE,
            "nightly.cold_suite_regression",
            NODE_NAMES["report"],
            NODE_NAMES["patch_pr"],
            NODE_NAMES["rag_archive"],
        ]

    def test_compiled_graph_has_no_static_interrupts_but_suspendable_nodes_are_listed(self) -> None:
        assert INTERRUPT_BEFORE_NODES == []
        graph = build_main_graph(InMemorySaver())
        assert not graph.interrupt_before_nodes
        assert set(SUSPENDABLE_NODES) == {
            "trigger_accuracy.optimizer_loop",
            "instruction_control.optimizer_loop",
            "security.appsec_optimizer_loop",
            "coverage.extract_capability_tree",
        }

    def test_every_node_is_wrapped_by_the_approval_guard(self) -> None:
        wrapped: list[str] = []

        class SpyGuard(ApprovalGuard):
            def wrap(self, node_name: str, fn: Any) -> Any:
                wrapped.append(node_name)
                return super().wrap(node_name, fn)

        builder = build_main_graph_builder(MainGraphDeps(approval_guard=SpyGuard()))
        assert set(wrapped) == set(builder.nodes)

    def test_multi_skill_terminal_is_the_deep_conflict_gate(self) -> None:
        assert multi_skill.TERMINAL_NODE in DIMENSION_TERMINAL_NODES
        assert multi_skill.TERMINAL_NODE == "multi_skill.deep_conflict_approval_gate"


# --------------------------------------------------------------------------- #
# 3. 编排层节点
# --------------------------------------------------------------------------- #


def _pipeline_nodes(tmp_path: Path, **overrides: Any) -> tuple[PipelineNodes, PipelineDeps]:
    deps = PipelineDeps(
        skill_repository=overrides.pop("skill_repository", FakeSkillRepo()),  # type: ignore[arg-type]
        run_repository=overrides.pop("run_repository", FakeRunRepo()),  # type: ignore[arg-type]
        report_generator=overrides.pop("report_generator", FakeReporter()),
        test_suite_service=overrides.pop("test_suite_service", FakeSuiteService()),  # type: ignore[arg-type]
        langfuse_adapter=FakeLangfuse(),  # type: ignore[arg-type]
        report_dir=str(tmp_path / "reports"),
        **overrides,
    )
    return PipelineNodes(deps), deps


class PipelineNodeTests:
    async def test_bootstrap_saves_skill_and_creates_run(self, tmp_path: Path) -> None:
        root = _write_skill_dir(tmp_path)
        skill = load_skill(root)
        nodes, deps = _pipeline_nodes(tmp_path)
        await nodes.bootstrap_run(
            {
                "run_id": RUN_ID,
                "skill_id": skill.skill_id,
                "skill_version_ref": skill.version_ref,
                KEY_SKILL_PATH: str(root),
            }
        )
        assert deps.skill_repository.saved[0].skill_id == skill.skill_id  # type: ignore[attr-defined]
        assert deps.run_repository.created[0]["generation_mode"] == "reuse"  # type: ignore[attr-defined]
        assert deps.test_suite_service.forced == []  # type: ignore[union-attr]

    async def test_bootstrap_force_regenerates_only_when_asked(self, tmp_path: Path) -> None:
        root = _write_skill_dir(tmp_path)
        skill = load_skill(root)
        nodes, deps = _pipeline_nodes(tmp_path)
        await nodes.bootstrap_run(
            {
                "run_id": RUN_ID,
                "skill_id": skill.skill_id,
                "skill_version_ref": skill.version_ref,
                "generation_mode": "force_regenerate",
                KEY_SKILL_PATH: str(root),
            }
        )
        assert deps.test_suite_service.forced == [(skill.skill_id, "pipeline_force_regenerate")]  # type: ignore[union-attr]

    async def test_bootstrap_rejects_identity_drift(self, tmp_path: Path) -> None:
        root = _write_skill_dir(tmp_path)
        nodes, _ = _pipeline_nodes(tmp_path)
        with pytest.raises(ConfigurationError):
            await nodes.bootstrap_run(
                {"run_id": RUN_ID, "skill_id": "csv-cleaner", "skill_version_ref": "old", KEY_SKILL_PATH: str(root)}
            )

    async def test_finalize_report_writes_files_and_passes_header_fields(self, tmp_path: Path) -> None:
        reporter = FakeReporter(blocking=True)
        nodes, _ = _pipeline_nodes(tmp_path, report_generator=reporter)
        update = await nodes.finalize_report(
            {
                "run_id": RUN_ID,
                sec_state.KEY_SUITE_STALENESS_WARNING: "用例集版本漂移",
                preflight.KEY_CANARY_OUTCOME: {"status": "skipped", "reasons": ["24h 内成功过"]},
            }
        )
        assert update[KEY_REPORT_BLOCKING] is True
        assert Path(tmp_path / "reports" / REPORT_JSON_NAME).is_file()
        assert Path(tmp_path / "reports" / REPORT_HTML_NAME).read_text(encoding="utf-8").count("24h 内成功过") == 1
        call = reporter.build_calls[0]
        assert call["test_suite_staleness_warning"] == "用例集版本漂移"
        assert call["preflight_summary"]["canary_probe"]["status"] == "skipped"

    async def test_rag_archive_swallows_infrastructure_errors_and_rewrites_report(self, tmp_path: Path) -> None:
        async def broken_archive(run_id: str) -> ArchiveOutcome:
            raise RuntimeError("embedding 通道不可用")

        reporter = FakeReporter()
        nodes, _ = _pipeline_nodes(tmp_path, report_generator=reporter, archive_fn=broken_archive)
        update = await nodes.rag_archive({"run_id": RUN_ID, KEY_PULL_REQUEST: {"status": "skipped"}})
        outcome = update[KEY_ARCHIVE_OUTCOME]
        assert outcome["archived"] is False and outcome["reason"].startswith("archive_error")  # type: ignore[index]
        assert reporter.build_calls[-1]["archive_outcome"]["archived"] is False
        assert reporter.build_calls[-1]["pull_request"] == {"status": "skipped"}

    async def test_cold_suite_node_requires_regression_component(self, tmp_path: Path) -> None:
        nodes, _ = _pipeline_nodes(tmp_path)
        with pytest.raises(ConfigurationError):
            await nodes.cold_suite_regression({"run_id": RUN_ID})


# --------------------------------------------------------------------------- #
# 4. 补丁转 PR：合成
# --------------------------------------------------------------------------- #


class ComposeChangesTests:
    def test_description_and_non_overlapping_body_patches_are_stacked(self) -> None:
        original = _skill()
        trigger_working = original.model_copy(update={"description": "清洗 CSV/Excel 导出文件，去重与规范化"})
        sec_body = original.body_markdown + "\n## 安全约束\n- 禁止把用户输入拼进 shell 命令\n"
        ic_body = original.body_markdown.replace("2. 去重", "2. 按主键去重")
        composed = compose_changes(
            original,
            [
                _accepted("security", _patch("p-sec", PatchType.RIGID_CONSTRAINT), original.model_copy(update={"body_markdown": sec_body})),
                _accepted("trigger_accuracy", _patch("p-trg", PatchType.DESCRIPTION_PATCH), trigger_working),
                _accepted("instruction_control", _patch("p-ic", PatchType.RIGID_CONSTRAINT), original.model_copy(update={"body_markdown": ic_body})),
            ],
            _reader({"SKILL.md": SKILL_MD}),
        )
        assert composed.conflicts == []
        assert composed.included_patch_ids == ["p-sec", "p-trg", "p-ic"]
        new_md = composed.files["SKILL.md"]
        assert "name: csv-cleaner" in new_md  # frontmatter 其余键保留
        assert "description: 清洗 CSV/Excel 导出文件，去重与规范化" in new_md
        assert "禁止把用户输入拼进 shell 命令" in new_md and "按主键去重" in new_md

    def test_overlapping_body_patch_is_reported_not_silently_dropped(self) -> None:
        original = _skill()
        first = original.body_markdown.replace("2. 去重", "2. 按主键去重")
        second = original.body_markdown.replace("2. 去重", "2. 按全部列去重")
        composed = compose_changes(
            original,
            [
                _accepted("security", _patch("p-sec", PatchType.RIGID_CONSTRAINT), original.model_copy(update={"body_markdown": first})),
                _accepted("instruction_control", _patch("p-ic", PatchType.RIGID_CONSTRAINT), original.model_copy(update={"body_markdown": second})),
            ],
            _reader({"SKILL.md": SKILL_MD}),
        )
        assert composed.included_patch_ids == ["p-sec"]
        assert len(composed.conflicts) == 1 and "instruction_control" in composed.conflicts[0]
        assert "按主键去重" in composed.files["SKILL.md"]

    def test_body_only_change_keeps_frontmatter_verbatim(self) -> None:
        raw = '---\nname: csv-cleaner\ndescription: "清洗 CSV 导出文件"\n---\n# 正文\n'
        assert render_skill_md(raw, description=None, body="# 新正文\n") == (
            '---\nname: csv-cleaner\ndescription: "清洗 CSV 导出文件"\n---\n# 新正文\n'
        )

    def test_code_patch_is_read_back_from_the_working_copy(self, tmp_path: Path) -> None:
        working_root = tmp_path / "working" / "csv-cleaner"
        (working_root / "scripts").mkdir(parents=True)
        (working_root / WORKING_COPY_MARKER).write_text("x", encoding="utf-8")
        (working_root / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
        (working_root / "scripts" / "clean.py").write_text("import shlex\nprint('clean')\n", encoding="utf-8")
        original = _skill()
        composed = compose_changes(
            original,
            [_accepted("security", _patch("p-code", PatchType.CODE_PATCH, "scripts/clean.py"), original.model_copy(update={"root_path": str(working_root)}))],
            _reader({"SKILL.md": SKILL_MD, "scripts/clean.py": "print('clean')\n"}),
        )
        assert composed.files == {"scripts/clean.py": "import shlex\nprint('clean')\n"}
        assert composed.included_patch_ids == ["p-code"]

    def test_code_patch_falls_back_to_diff_replay_when_working_copy_is_gone(self, tmp_path: Path) -> None:
        diff = "--- a/scripts/clean.py\n+++ b/scripts/clean.py\n@@ -1,1 +1,2 @@\n+import shlex\n print('clean')\n"
        original = _skill()
        composed = compose_changes(
            original,
            [_accepted("security", _patch("p-code", PatchType.CODE_PATCH, "scripts/clean.py", diff), original.model_copy(update={"root_path": str(tmp_path / "gone")}))],
            _reader({"SKILL.md": SKILL_MD, "scripts/clean.py": "print('clean')\n"}),
        )
        assert composed.files["scripts/clean.py"] == "import shlex\nprint('clean')\n"
        assert any("工作副本已不存在" in note for note in composed.notes)

    async def test_collect_accepted_patches_reads_state_keys_and_excludes_unapplied(self) -> None:
        patches = {
            "p-trg": _patch("p-trg", PatchType.DESCRIPTION_PATCH),
            "p-ic": _patch("p-ic", PatchType.RIGID_CONSTRAINT),
        }
        results = {
            "p-trg": PatchApplicationResult(patch_id="p-trg", applied=True, regression_passed=False),
            "p-ic": PatchApplicationResult(patch_id="p-ic", applied=False),
        }
        state = {
            trigger_state.KEY_APPLIED_PATCH_ID: "p-trg",
            trigger_state.KEY_WORKING_SKILL: _skill().model_dump(),  # checkpoint 反序列化后可能是 dict
            ic_state.KEY_APPLIED_PATCH_ID: "p-ic",
        }
        accepted, excluded = await collect_accepted_patches(state, FakePatchRepo(patches, results))  # type: ignore[arg-type]
        assert [a.patch.patch_id for a in accepted] == ["p-trg"]
        assert accepted[0].regression_passed is False  # 人工采纳
        assert isinstance(accepted[0].working_skill, SkillDefinition)
        assert len(excluded) == 1 and "p-ic" in excluded[0]

    def test_branch_name_sanitizes_skill_id(self) -> None:
        assert branch_name_for("my skill:v2", RUN_ID, prefix="skill-evaluate/auto-fix") == (
            "skill-evaluate/auto-fix/my-skill-v2/12345678"
        )


# --------------------------------------------------------------------------- #
# 5. 补丁转 PR：交付流程
# --------------------------------------------------------------------------- #


class FakeGitOps:
    def __init__(self, source_root: Path, *, is_commit: bool = True, existing_pr: str | None = None) -> None:
        self.source_root = source_root
        self._is_commit = is_commit
        self.existing_pr = existing_pr
        self.worktree: Path | None = None
        self.calls: list[str] = []
        self.pr_body = ""
        self.push_error: Exception | None = None

    async def repo_root(self, path: str) -> str | None:
        return str(self.source_root.parent)

    async def is_commit(self, repo: str, ref: str) -> bool:
        return self._is_commit

    async def find_open_pull_request(self, repo: str, branch: str) -> str | None:
        return self.existing_pr

    async def create_worktree(self, repo: str, branch: str, base_ref: str) -> str:
        import shutil

        self.calls.append(f"worktree:{branch}@{base_ref}")
        self.worktree = self.source_root.parent.parent / "worktree"
        shutil.copytree(self.source_root.parent, self.worktree)
        return str(self.worktree)

    async def commit_all(self, worktree: str, message: str) -> bool:
        self.calls.append("commit")
        return True

    async def push(self, worktree: str, branch: str) -> None:
        if self.push_error:
            raise self.push_error
        self.calls.append("push")

    async def create_pull_request(self, worktree: str, *, branch: str, title: str, body: str, base: str | None) -> str:
        self.calls.append("pr")
        self.pr_body = body
        return "https://github.com/org/repo/pull/7"

    async def remove_worktree(self, repo: str, worktree: str) -> None:
        self.calls.append("remove")


def _pr_setup(tmp_path: Path, **git_kwargs: Any) -> tuple[PatchToPullRequest, FakeGitOps, FakeRunRepo, dict[str, Any]]:
    repo = tmp_path / "repo"
    skill_root = repo / "skills" / "csv-cleaner"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    original = _skill(str(skill_root))
    git = FakeGitOps(repo / "skills", **git_kwargs)
    runs = FakeRunRepo()
    patch = _patch("p-trg", PatchType.DESCRIPTION_PATCH)
    converter = PatchToPullRequest(
        git_ops=git,
        patch_repository=FakePatchRepo(  # type: ignore[arg-type]
            {"p-trg": patch}, {"p-trg": PatchApplicationResult(patch_id="p-trg", applied=True, regression_passed=True, detail="ok")}
        ),
        skill_repository=FakeSkillRepo(original),  # type: ignore[arg-type]
        run_repository=runs,  # type: ignore[arg-type]
        enabled=True,
    )
    state = {
        "run_id": RUN_ID,
        "skill_id": SKILL_ID,
        "skill_version_ref": VERSION,
        trigger_state.KEY_APPLIED_PATCH_ID: "p-trg",
        trigger_state.KEY_WORKING_SKILL: original.model_copy(update={"description": "清洗并规范化 CSV 导出文件"}),
    }
    return converter, git, runs, state


class PatchToPullRequestTests:
    async def test_creates_pr_with_composed_files_and_records_url(self, tmp_path: Path) -> None:
        converter, git, runs, state = _pr_setup(tmp_path)
        outcome = await converter.run(state, report_link="https://ci/run/1")
        assert outcome.status == "created" and outcome.url == "https://github.com/org/repo/pull/7"
        assert runs.pr_urls[RUN_ID] == outcome.url
        assert git.calls == [f"worktree:skill-evaluate/auto-fix/{SKILL_ID}/12345678@{VERSION}", "commit", "push", "pr", "remove"]
        assert git.worktree is not None
        written = (git.worktree / "skills" / "csv-cleaner" / "SKILL.md").read_text(encoding="utf-8")
        assert "description: 清洗并规范化 CSV 导出文件" in written
        assert "https://ci/run/1" in git.pr_body and "不会自动合并" in git.pr_body

    async def test_reuses_existing_pr_on_rerun(self, tmp_path: Path) -> None:
        converter, git, _, state = _pr_setup(tmp_path, existing_pr="https://github.com/org/repo/pull/3")
        outcome = await converter.run(state, report_link="r")
        assert outcome.status == "reused" and "pr" not in git.calls

    async def test_skips_when_version_ref_is_not_a_commit(self, tmp_path: Path) -> None:
        converter, git, _, state = _pr_setup(tmp_path, is_commit=False)
        outcome = await converter.run(state, report_link="r")
        assert outcome.status == "skipped" and git.calls == []

    async def test_disabled_lists_candidates_without_touching_git(self, tmp_path: Path) -> None:
        converter, git, _, state = _pr_setup(tmp_path)
        converter.enabled = False
        outcome = await converter.run(state, report_link="r")
        assert outcome.status == "skipped" and outcome.patches[0]["patch_id"] == "p-trg"
        assert git.calls == []

    async def test_delivery_failure_is_reported_not_raised(self, tmp_path: Path) -> None:
        converter, git, runs, state = _pr_setup(tmp_path)
        git.push_error = DeliveryError("gh 鉴权过期")
        outcome = await converter.run(state, report_link="r")
        assert outcome.status == "failed" and "鉴权过期" in outcome.reason
        assert "remove" in git.calls and runs.pr_urls == {}

    async def test_no_patches_is_skipped(self, tmp_path: Path) -> None:
        converter, _, _, _ = _pr_setup(tmp_path)
        outcome = await converter.run({"run_id": RUN_ID}, report_link="r")
        assert outcome.status == "skipped"


# --------------------------------------------------------------------------- #
# 6. Nightly COLD 回归
# --------------------------------------------------------------------------- #


def _trace(case_id: str, run_index: int, loaded: bool) -> ExecutionTrace:
    now = datetime.now(UTC)
    return ExecutionTrace(
        trace_id=f"t-{case_id}-{run_index}",
        case_id=case_id,
        run_index=run_index,
        backend_type=ExecutorBackendType.PLUGGABLE.value,
        loaded_skill_md=loaded,
        timing=TimingCostMetrics(total_tokens=1, prompt_tokens=1, completion_tokens=0, duration_ms=1),
        final_response="ok",
        started_at=now,
        finished_at=now,
    )


def _case(case_id: str, split: DatasetSplit, category: TestCaseCategory = TestCaseCategory.POSITIVE) -> TestCase:
    return TestCase(
        case_id=case_id,
        skill_id=SKILL_ID,
        category=category,
        split=split,
        prompt=case_id,
        generator_run_id="g",
        created_at=datetime.now(UTC),
    )


class FakeColdDeps:
    def __init__(self, cases: list[TestCase]) -> None:
        self.reporter_obj = FakeReporter()
        self.skill_repository = FakeSkillRepo(_skill())
        self.run_repository = FakeRunRepo()
        self.cases = cases
        self.saved_verdicts: list[Any] = []
        outer = self

        class CaseRepo:
            async def list_by_ids(self, ids: list[str]) -> list[TestCase]:
                return [c for c in outer.cases if c.case_id in ids]

        class VerdictRepo:
            async def save_verdict(self, verdict: Any) -> None:
                outer.saved_verdicts.append(verdict)

        self.test_case_repository = CaseRepo()
        self.judge_repository = VerdictRepo()
        self._judge = JudgeAgent()

    def reporter(self) -> FakeReporter:
        return self.reporter_obj

    def judge(self) -> JudgeAgent:
        return self._judge


class FakeTriggerPipeline:
    def __init__(self, deps: FakeColdDeps, loaded: dict[str, bool]) -> None:
        self.deps = deps
        self.loaded = loaded
        self.calls: list[tuple[list[str], int]] = []

    async def run_cases(self, run_id: str, skill: SkillDefinition, cases: list[TestCase], *, run_index_base: int) -> dict[str, list[ExecutionTrace]]:
        self.calls.append(([c.case_id for c in cases], run_index_base))
        return {c.case_id: [_trace(c.case_id, run_index_base + i, self.loaded[c.case_id]) for i in range(3)] for c in cases}


class FakeSuiteRepo:
    def __init__(self, suite: TestSuiteVersion | None) -> None:
        self.suite = suite

    async def get_active_version(self, skill_id: str, skill_version_ref: str | None = None) -> TestSuiteVersion | None:
        return self.suite


def _suite(case_ids: list[str]) -> TestSuiteVersion:
    return TestSuiteVersion(
        suite_version_id="suite-1", skill_id=SKILL_ID, skill_version_ref=VERSION,
        generation_mode="reuse", case_ids=case_ids, created_at=datetime.now(UTC),
    )


class ColdSuiteTests:
    async def test_only_cold_trigger_cases_are_rerun_in_their_own_run_index_segment(self) -> None:
        cases = [
            _case("cold-ok", DatasetSplit.COLD),
            _case("cold-bad", DatasetSplit.COLD),
            _case("train", DatasetSplit.TRAIN),
            _case("adv", DatasetSplit.COLD, TestCaseCategory.ADVERSARIAL),
        ]
        deps = FakeColdDeps(cases)
        pipeline = FakeTriggerPipeline(deps, {"cold-ok": True, "cold-bad": False})
        regression = ColdSuiteRegression(trigger_pipeline=pipeline, suite_repository=FakeSuiteRepo(_suite([c.case_id for c in cases])))  # type: ignore[arg-type]
        update = await regression.run({"run_id": RUN_ID, "skill_id": SKILL_ID, "skill_version_ref": VERSION})
        assert pipeline.calls == [(["cold-ok", "cold-bad"], RUN_INDEX_COLD_SUITE)]
        recorded = deps.reporter_obj.recorded[0]
        assert recorded["dimension"] == COLD_DIMENSION and recorded["blocking"] is False
        assert recorded["status"] is JudgeVerdictStatus.FAIL and recorded["score"] == 0.5
        assert [v.subject_id for v in deps.saved_verdicts] == [f"{SUBJECT_PREFIX}cold-bad"]
        assert update["_pipeline_cold_suite_summary"]["failed_case_ids"] == ["cold-bad"]  # type: ignore[index]

    async def test_missing_active_suite_is_not_a_pass(self) -> None:
        deps = FakeColdDeps([])
        regression = ColdSuiteRegression(trigger_pipeline=FakeTriggerPipeline(deps, {}), suite_repository=FakeSuiteRepo(None))  # type: ignore[arg-type]
        await regression.run({"run_id": RUN_ID, "skill_id": SKILL_ID, "skill_version_ref": VERSION})
        assert deps.reporter_obj.recorded[0]["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW

    async def test_no_cold_cases_is_a_pass_with_note(self) -> None:
        deps = FakeColdDeps([_case("train", DatasetSplit.TRAIN)])
        pipeline = FakeTriggerPipeline(deps, {})
        regression = ColdSuiteRegression(trigger_pipeline=pipeline, suite_repository=FakeSuiteRepo(_suite(["train"])))  # type: ignore[arg-type]
        await regression.run({"run_id": RUN_ID, "skill_id": SKILL_ID, "skill_version_ref": VERSION})
        assert deps.reporter_obj.recorded[0]["status"] is JudgeVerdictStatus.PASS
        assert pipeline.calls == []


# --------------------------------------------------------------------------- #
# 7. GraphResumer
# --------------------------------------------------------------------------- #


class _SuspendState(TypedDict, total=False):
    run_id: str
    a: str
    b: str
    blocking: bool
    _pipeline_report_blocking: bool


def _two_hook_graph() -> Any:
    async def wait_a(state: _SuspendState) -> dict[str, Any]:
        return {"a": interrupt({"reason": "hook", "wait_key": "k-a"})}

    async def wait_b(state: _SuspendState) -> dict[str, Any]:
        return {"b": interrupt({"reason": "hook", "wait_key": "k-b"})}

    async def done(state: _SuspendState) -> dict[str, Any]:
        return {KEY_REPORT_BLOCKING: state.get("a") == "fail"}

    builder: Any = StateGraph(_SuspendState)
    builder.add_node("wait_a", wait_a)
    builder.add_node("wait_b", wait_b)
    builder.add_node("done", done)
    builder.add_edge("__start__", "wait_a")
    builder.add_edge("__start__", "wait_b")
    builder.add_edge(["wait_a", "wait_b"], "done")
    builder.add_edge("done", "__end__")
    return builder.compile(checkpointer=InMemorySaver())


class ResumerTests:
    async def test_parallel_interrupts_are_resumed_individually_by_wait_key(self) -> None:
        graph = _two_hook_graph()
        config: Any = {"configurable": {"thread_id": "t"}}
        await graph.ainvoke({"run_id": RUN_ID}, config)
        resumer = CompiledGraphResumer(graph, background=False)
        await resumer.resume(thread_id="t", resume_payload="trace-b", wait_key="k-b")
        assert (await graph.aget_state(config)).next  # 另一条仍在等
        await resumer.resume(thread_id="t", resume_payload="trace-a", wait_key="k-a")
        snapshot = await graph.aget_state(config)
        assert not snapshot.next and snapshot.values["a"] == "trace-a" and snapshot.values["b"] == "trace-b"

    async def test_background_mode_returns_immediately(self) -> None:
        graph = _two_hook_graph()
        config: Any = {"configurable": {"thread_id": "t"}}
        await graph.ainvoke({"run_id": RUN_ID}, config)
        resumer = CompiledGraphResumer(graph, background=True)
        await resumer.resume(thread_id="t", resume_payload="x", wait_key="k-a")
        await resumer.resume(thread_id="t", resume_payload="y", wait_key="k-b")
        for _ in range(50):
            if not (await graph.aget_state(config)).next:
                break
            await asyncio.sleep(0.01)
        assert not (await graph.aget_state(config)).next

    async def test_resolve_suspension_passes_wait_key_to_resumer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        received: dict[str, Any] = {}

        class Ledger:
            async def mark_resolved(self, wait_key: str, payload: str) -> bool:
                return True

        class Resumer:
            async def resume(self, *, thread_id: str, resume_payload: Any, wait_key: str | None = None) -> None:
                received.update(thread_id=thread_id, payload=resume_payload, wait_key=wait_key)

        monkeypatch.setattr(suspension, "PendingHookRepository", Ledger)
        monkeypatch.setattr(suspension, "_graph_resumer", Resumer())
        await suspension.resolve_suspension("k-1", "trace-1", "s:r")
        assert received == {"thread_id": "s:r", "payload": "trace-1", "wait_key": "k-1"}


# --------------------------------------------------------------------------- #
# 8. 运行入口
# --------------------------------------------------------------------------- #


def _runner(graph: Any, tmp_path: Path, **kwargs: Any) -> PipelineRunner:
    return PipelineRunner(
        graph, poll_interval_s=0.01, report_generator=FakeReporter(), report_dir=str(tmp_path / "out"), **kwargs
    )


class RunnerTests:
    async def test_completed_run_exit_code_follows_blocking(self, tmp_path: Path) -> None:
        root = _write_skill_dir(tmp_path)

        async def finish(state: dict[str, Any]) -> dict[str, Any]:
            return {KEY_REPORT_BLOCKING: True}

        builder: Any = StateGraph(MainGraphState)
        builder.add_node("finish", finish)
        builder.add_edge("__start__", "finish")
        graph = builder.compile(checkpointer=InMemorySaver())
        outcome = await _runner(graph, tmp_path, wait_timeout_s=1).start(str(root), run_id=RUN_ID)
        assert outcome.status == "completed" and outcome.exit_code() == EXIT_BLOCKING
        assert (tmp_path / "out" / REPORT_JSON_NAME).is_file()  # 本进程重写了一份报告
        # 同一 run_id 再跑一次：线程已结束，不重跑
        again = await _runner(graph, tmp_path, wait_timeout_s=1).start(str(root), run_id=RUN_ID)
        assert again.status == "completed"

    async def test_suspended_run_times_out_with_pending_wait_keys_then_completes_after_resume(self, tmp_path: Path) -> None:
        root = _write_skill_dir(tmp_path)
        graph = _two_hook_graph()
        runner = _runner(graph, tmp_path, wait_timeout_s=0)
        outcome = await runner.start(str(root), run_id=RUN_ID)
        assert outcome.status == "suspended" and outcome.exit_code() == EXIT_SUSPENDED
        assert sorted(outcome.pending_wait_keys) == ["k-a", "k-b"]

        resumer = CompiledGraphResumer(graph, background=False)
        await resumer.resume(thread_id=outcome.thread_id, resume_payload="ok", wait_key="k-a")
        await resumer.resume(thread_id=outcome.thread_id, resume_payload="ok", wait_key="k-b")
        final = await runner.wait(run_id=RUN_ID, skill_id=outcome.skill_id, thread_id=outcome.thread_id)
        assert final.status == "completed" and final.exit_code() == EXIT_OK

    async def test_human_rejection_stops_the_run(self, tmp_path: Path) -> None:
        root = _write_skill_dir(tmp_path)

        async def reject(state: dict[str, Any]) -> dict[str, Any]:
            raise HumanRejectedSuspension("人工选择放弃")

        builder: Any = StateGraph(MainGraphState)
        builder.add_node("reject", reject)
        builder.add_edge("__start__", "reject")
        graph = builder.compile(checkpointer=InMemorySaver())
        outcome = await _runner(graph, tmp_path, wait_timeout_s=1).start(str(root))
        assert outcome.status == "stopped" and outcome.exit_code() == EXIT_STOPPED
        assert "人工选择放弃" in (outcome.stop_reason or "")


# --------------------------------------------------------------------------- #
# 9. CI 辅助、报告摘要、巡检入口
# --------------------------------------------------------------------------- #


class CiSupportTests:
    def test_changed_files_map_to_nearest_skill_dir(self, tmp_path: Path) -> None:
        for name in ("a", "nested/b"):
            (tmp_path / "skills" / name / "references").mkdir(parents=True)
            (tmp_path / "skills" / name / "SKILL.md").write_text("x", encoding="utf-8")
        changed = [
            "skills/a/SKILL.md",
            "skills/a/references/deep.md",
            "skills/nested/b/references/x.md",
            "skills/removed/SKILL.md",  # 已删除：没有东西可测
            "src/app.py",
            "../escape/SKILL.md",
        ]
        assert ci_support.resolve_changed_skills(changed, repo_root=tmp_path) == ["skills/a", "skills/nested/b"]
        assert ci_support.list_all_skills(repo_root=tmp_path) == ["skills/a", "skills/nested/b"]

    def test_base_image_change_detection(self) -> None:
        assert ci_support.touches_base_image(["docker/sandbox/Dockerfile"])
        assert ci_support.touches_base_image(["golden_fingerprint.json"])
        assert not ci_support.touches_base_image(["skills/a/SKILL.md"])


class ReportSummaryTests:
    def test_coverage_summary_only_takes_coverage_dimensions_with_scores(self) -> None:
        dims = [
            DimensionResult(dimension="capability_coverage", status=JudgeVerdictStatus.PASS, score=0.92, blocking=False),
            DimensionResult(dimension="weighted_coverage", status=JudgeVerdictStatus.NEEDS_HUMAN_REVIEW, score=None, blocking=False),
            DimensionResult(dimension="test_suite_health", status=JudgeVerdictStatus.PASS, score=0.5, blocking=False),
            DimensionResult(dimension="trigger_accuracy", status=JudgeVerdictStatus.PASS, score=0.9, blocking=True),
        ]
        assert summarize_coverage(dims) == {
            "capability_coverage_ratio": 0.92,
            "combinatorial_pair_coverage_ratio": 0.5,
        }


class ReaperTests:
    async def test_reap_once_builds_timeout_trace_and_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resolved: list[dict[str, Any]] = []

        class Hooks:
            async def list_stale_waiting(self, older_than: int) -> list[dict[str, object]]:
                return [{"case_id": "c1", "run_index": 0, "wait_key": "r:c1:0", "thread_id": "s:r"}]

        class Traces:
            def __init__(self) -> None:
                self.saved: list[ExecutionTrace] = []

            async def save(self, trace: ExecutionTrace) -> None:
                self.saved.append(trace)

        async def fake_resolve(*, wait_key: str, resume_payload: Any, thread_id: str) -> None:
            resolved.append({"wait_key": wait_key, "thread_id": thread_id})

        monkeypatch.setattr("skill_evaluate.persistence.reaper.resolve_suspension", fake_resolve)
        traces = Traces()
        count = await reap_once(60, hook_repository=Hooks(), trace_repository=traces)  # type: ignore[arg-type]
        assert count == 1 and resolved == [{"wait_key": "r:c1:0", "thread_id": "s:r"}]
        assert traces.saved[0].loaded_skill_md is False
