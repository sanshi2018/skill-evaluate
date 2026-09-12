"""docs/dev/16：模块六——能力覆盖率与测试完备性评测。

覆盖：`capability_id` 的稳定性与归一化边界、`AnalyzerAgent` 的抽取去重与占位 tier、
映射结果按能力树过滤幻觉 id、五个节点的判定口径（人工审核卡片的挂起与默认不通过、
每轮清空重算的幂等性、"已有映射的用例照常参与覆盖标记"这条修正、孤儿绑定跳过、
覆盖率量化规则经 Judge 且落库、补盲的 `descriptions` 必填与 `GenerationError` 降级、
空能力树判 NEEDS_HUMAN_REVIEW 而不是满分 PASS、`blocking` 恒为 False）、两道环出口的
路由，以及图结构（回边存在、条件分支只通向声明过的两个去处）。
全部用替身注入，不碰数据库、不发真实请求。
"""

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from skill_evaluate.agents.analyzer import (
    AnalyzerAgent,
    CapabilityExtraction,
    CaseCapabilityMapping,
    ExtractedCapability,
    build_capability_id,
    build_capability_tree_id,
    normalize_capability_text,
    parse_capability_tree_id,
)
from skill_evaluate.agents.analyzer.service import PLACEHOLDER_TIER
from skill_evaluate.agents.generator.schema import CapabilityFocus
from skill_evaluate.agents.judge.rules import get_rule
from skill_evaluate.config import CoverageSettings
from skill_evaluate.errors import ConfigurationError, GenerationError, PipelineSuspended
from skill_evaluate.executors.routing import NODE_BACKEND_ROUTING
from skill_evaluate.nodes.coverage import (
    DIMENSION,
    NODE_NAMES,
    NODE_PREFIX,
    ROUTING_KEY,
    RULE_CAPABILITY_COVERAGE,
    SUBJECT_PREFIX_COVERAGE,
    BlindSpot,
    CoverageDeps,
    CoveragePipeline,
    build_coverage_subgraph,
    coverage_inputs,
)
from skill_evaluate.nodes.coverage.deps import TRIGGERED_BY_COVERAGE_GAP
from skill_evaluate.nodes.coverage.graph import INTERRUPT_BEFORE_NODES
from skill_evaluate.nodes.coverage.state import (
    KEY_BLIND_SPOTS,
    KEY_COVERAGE_RATIO,
    KEY_MAPPED_CASE_COUNT,
    KEY_PATCH_EXHAUSTED,
    KEY_PATCH_FAILURE,
    KEY_PATCH_ITERATIONS,
    KEY_TREE_NODE_COUNT,
    KEY_TREE_REVIEW_CONFIRMED,
)
from skill_evaluate.state.capability import CapabilityNode, CapabilityTree
from skill_evaluate.state.enums import (
    CapabilityTier,
    DatasetSplit,
    ExecutorBackendType,
    JudgeVerdictStatus,
    TestCaseCategory,
)
from skill_evaluate.state.judge import JudgeVerdict
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion

SKILL_ID = "csv-cleaner"
RUN_ID = "run-cov-1"
BASE_REF = "v1"
SUITE_ID = "suite-1"

CAP_CSV = build_capability_id(SKILL_ID, "支持读取 CSV 文件")
CAP_XLSX = build_capability_id(SKILL_ID, "支持读取 Excel 文件")
CAP_PIVOT = build_capability_id(SKILL_ID, "支持输出数据透视表")


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def _skill(version_ref: str = BASE_REF) -> SkillDefinition:
    return SkillDefinition(
        skill_id=SKILL_ID,
        version_ref=version_ref,
        root_path=".",
        description="清洗并校验 CSV / Excel 导出文件",
        body_markdown="# CSV Cleaner\n\n支持 .csv 与 .xlsx 输入，可输出数据透视表。\n",
        line_count=3,
        token_count=40,
    )


def _node(capability_id: str, description: str, *, covered: bool = False) -> CapabilityNode:
    return CapabilityNode(
        capability_id=capability_id,
        skill_id=SKILL_ID,
        description=description,
        tier=PLACEHOLDER_TIER,
        covered=covered,
    )


def _tree(*nodes: CapabilityNode) -> CapabilityTree:
    return CapabilityTree(skill_id=SKILL_ID, skill_version_ref=BASE_REF, nodes=list(nodes))


def _default_tree() -> CapabilityTree:
    return _tree(
        _node(CAP_CSV, "支持读取 CSV 文件"),
        _node(CAP_XLSX, "支持读取 Excel 文件"),
        _node(CAP_PIVOT, "支持输出数据透视表"),
    )


def _case(
    case_id: str,
    *,
    capability_ids: list[str] | None = None,
    category: TestCaseCategory = TestCaseCategory.POSITIVE,
) -> TestCase:
    return TestCase(
        case_id=case_id,
        skill_id=SKILL_ID,
        category=category,
        split=DatasetSplit.TRAIN,
        prompt=f"帮我把这份导出理一下（{case_id}）",
        target_capability_ids=list(capability_ids or []),
        generator_run_id="gen-1",
        created_at=datetime.now(UTC),
    )


def _state(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "run_id": RUN_ID,
        "skill_id": SKILL_ID,
        "skill_version_ref": BASE_REF,
        "active_suite_version_id": SUITE_ID,
        "capability_tree_id": build_capability_tree_id(SKILL_ID, BASE_REF),
    }
    base.update(overrides)
    return base


def _blind_spots(result: dict[str, object]) -> list[BlindSpot]:
    raw = cast("list[dict[str, Any]]", result[KEY_BLIND_SPOTS])
    return [BlindSpot.model_validate(item) for item in raw]


class FakeSkillRepo:
    def __init__(self, skill: SkillDefinition | None) -> None:
        self.skill = skill

    async def get(self, skill_id: str, version_ref: str) -> SkillDefinition | None:
        return self.skill


class FakeCapabilityRepo:
    def __init__(self, tree: CapabilityTree | None = None) -> None:
        self.tree = tree
        self.saves: list[CapabilityTree] = []

    async def save(self, tree: CapabilityTree) -> str:
        # 深拷贝：真实仓储写的是 JSONB，读回来是新对象。不拷贝的话测试会因为
        # "节点改的是同一个内存对象"而看不出漏写的落库调用。
        self.tree = tree.model_copy(deep=True)
        self.saves.append(self.tree)
        return "row-1"

    async def get(self, skill_id: str, skill_version_ref: str) -> CapabilityTree | None:
        return self.tree.model_copy(deep=True) if self.tree else None


class FakeCaseRepo:
    def __init__(self, cases: list[TestCase]) -> None:
        self.cases = cases
        self.saved: list[TestCase] = []

    async def list_by_categories(
        self, suite_version_id: str, categories: list[TestCaseCategory]
    ) -> list[TestCase]:
        return [c for c in self.cases if c.category in categories]

    async def save(self, case: TestCase) -> None:
        self.saved.append(case)


class FakeJudgeRepo:
    def __init__(self) -> None:
        self.saved: list[JudgeVerdict] = []

    async def save_verdict(self, verdict: JudgeVerdict) -> None:
        self.saved.append(verdict)


class FakeApprovalRepo:
    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []

    async def create(self, *, run_id: str, node_name: str, thread_id: str, wait_key: str) -> None:
        self.created.append(
            {
                "run_id": run_id,
                "node_name": node_name,
                "thread_id": thread_id,
                "wait_key": wait_key,
            }
        )


class FakeReporter:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record_dimension_result(self, **kwargs: Any) -> None:
        self.recorded.append(kwargs)


class FakeAnalyzer:
    """替代 `AnalyzerAgent`：抽取结果与映射结果都由测试直接给定。"""

    def __init__(
        self,
        *,
        tree: CapabilityTree | None = None,
        mapping: dict[str, list[str]] | None = None,
    ) -> None:
        self.tree = tree or _default_tree()
        self.mapping = mapping or {}
        self.mapped_case_ids: list[str] = []

    async def extract_capability_tree(self, skill: SkillDefinition) -> CapabilityTree:
        return self.tree.model_copy(deep=True)

    async def map_case_to_capabilities(self, case: TestCase, tree: CapabilityTree) -> list[str]:
        self.mapped_case_ids.append(case.case_id)
        return self.mapping.get(case.case_id, [])


class FakeSuiteService:
    """替代 `TestSuiteService`：只记录 `incremental_patch()` 的入参。"""

    def __init__(self, *, error: GenerationError | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.version_counter = 0

    async def incremental_patch(
        self, skill: SkillDefinition, focus: CapabilityFocus, triggered_by: str
    ) -> TestSuiteVersion:
        self.calls.append({"skill": skill, "focus": focus, "triggered_by": triggered_by})
        if self.error is not None:
            raise self.error
        self.version_counter += 1
        return TestSuiteVersion(
            suite_version_id=f"suite-patch-{self.version_counter}",
            skill_id=skill.skill_id,
            skill_version_ref=skill.version_ref,
            generation_mode="incremental_patch",
            case_ids=[],
            created_at=datetime.now(UTC),
        )


class FakeJudge:
    """替代 `JudgeAgent`：量化判定走**真实**规则表，只记录入参。

    刻意不伪造判定结果：本维度唯一的判定就是覆盖率阈值，把它也换成替身等于什么都
    没测。真正需要隔离的是 LLM 与数据库，规则表是纯函数。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def quantitative_verdict(
        self, subject_id: str, rule_name: str, inputs: dict[str, Any]
    ) -> JudgeVerdict:
        self.calls.append({"subject_id": subject_id, "rule_name": rule_name, "inputs": inputs})
        status = get_rule(rule_name)(inputs)
        return JudgeVerdict(
            verdict_id=f"v-{len(self.calls)}",
            subject_id=subject_id,
            status=status,
            reasoning=f"quantitative rule {rule_name!r} over inputs={inputs!r}",
            temperature=0.0,
            model=f"rule:{rule_name}",
            created_at=datetime.now(UTC),
        )


def _pipeline(
    *,
    analyzer: FakeAnalyzer | None = None,
    cases: list[TestCase] | None = None,
    tree: CapabilityTree | None = None,
    suite_service: FakeSuiteService | None = None,
    skill: SkillDefinition | None = None,
    settings: CoverageSettings | None = None,
) -> tuple[CoveragePipeline, dict[str, Any]]:
    """装一条全替身的流水线，并把替身一并交回给测试做断言。"""
    parts: dict[str, Any] = {
        "analyzer": analyzer or FakeAnalyzer(),
        "skill_repo": FakeSkillRepo(skill if skill is not None else _skill()),
        "case_repo": FakeCaseRepo(cases or []),
        "capability_repo": FakeCapabilityRepo(tree),
        "judge_repo": FakeJudgeRepo(),
        "approval_repo": FakeApprovalRepo(),
        "reporter": FakeReporter(),
        "judge": FakeJudge(),
        "suite_service": suite_service or FakeSuiteService(),
    }
    deps = CoverageDeps(
        analyzer_agent=cast("Any", parts["analyzer"]),
        judge_agent=cast("Any", parts["judge"]),
        suite_service=cast("Any", parts["suite_service"]),
        report_generator=cast("Any", parts["reporter"]),
        skill_repository=cast("Any", parts["skill_repo"]),
        test_case_repository=cast("Any", parts["case_repo"]),
        capability_repository=cast("Any", parts["capability_repo"]),
        judge_repository=cast("Any", parts["judge_repo"]),
        approval_repository=cast("Any", parts["approval_repo"]),
        coverage_settings=settings or CoverageSettings(),
    )
    return CoveragePipeline(deps), parts


# --------------------------------------------------------------------------- #
# 1. capability_id 的稳定性（docs/dev/16 第 2.1 节）
# --------------------------------------------------------------------------- #


def test_capability_id对书写差异稳定但对语义改写敏感() -> None:
    # 空白、全角标点、大小写都是抽取噪声，不该产生新 id——否则文档 18 补跑分级时
    # 全部历史绑定会集体失效。
    assert build_capability_id(SKILL_ID, "支持读取 CSV 文件") == build_capability_id(
        SKILL_ID, "  支持读取  CSV 文件 "
    )
    assert build_capability_id(SKILL_ID, "Read CSV files") == build_capability_id(
        SKILL_ID, "read csv FILES"
    )
    assert build_capability_id(SKILL_ID, "支持读取 CSV 文件（含 BOM）") == build_capability_id(
        SKILL_ID, "支持读取 CSV 文件(含 BOM)"
    )
    # 语义换了就该是新 id：那本来就是另一项能力，旧用例不该继续算作它的覆盖。
    assert build_capability_id(SKILL_ID, "支持读取 CSV 文件") != build_capability_id(
        SKILL_ID, "支持读取 Excel 文件"
    )


def test_capability_id带skill前缀避免跨Skill撞车() -> None:
    same_text = "支持读取 CSV 文件"
    assert build_capability_id("skill-a", same_text) != build_capability_id("skill-b", same_text)
    assert build_capability_id("skill-a", same_text).startswith("skill-a:cap-")


def test_归一化不做语义等价判断() -> None:
    # 只吸收书写噪声。同义改写必须落到不同 id——把语义等价交给启发式会同时产生
    # 两类相反的错误，而两者都无法从报告里看出来。
    assert normalize_capability_text("支持读取 CSV 文件") != normalize_capability_text(
        "可以读取逗号分隔文件"
    )


def test_capability_tree_id能无损还原含冒号的skill_id() -> None:
    tree_id = build_capability_tree_id("org:repo/skills/foo", "abc123")
    assert parse_capability_tree_id(tree_id) == ("org:repo/skills/foo", "abc123")
    with pytest.raises(ValueError, match="非法的 capability_tree_id"):
        parse_capability_tree_id("没有冒号")


# --------------------------------------------------------------------------- #
# 2. AnalyzerAgent
# --------------------------------------------------------------------------- #


async def test_抽取按id去重且tier统一填占位值(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = AnalyzerAgent()

    async def fake_call(prompt: str, schema: Any, *, system: str | None = None) -> Any:
        return CapabilityExtraction(
            capabilities=[
                ExtractedCapability(description="支持读取 CSV 文件", evidence_quote="支持 .csv"),
                # 只差一个空白：`build_capability_id` 会归一成同一个 id。不去重的话
                # 覆盖率分母被虚增，且其中一份永远标不上 covered。
                ExtractedCapability(description="支持读取  CSV 文件", evidence_quote="支持 .csv"),
                ExtractedCapability(description="  ", evidence_quote="x"),
                ExtractedCapability(description="支持读取 Excel 文件", evidence_quote="与 .xlsx"),
            ]
        )

    monkeypatch.setattr(agent, "_call_llm", fake_call)
    tree = await agent.extract_capability_tree(_skill())

    assert [n.capability_id for n in tree.nodes] == [CAP_CSV, CAP_XLSX]
    # 本文档阶段 tier 是占位值，不代表真实分级（docs/dev/16 第 9 节，由文档 18 接入）。
    assert {n.tier for n in tree.nodes} == {CapabilityTier.P1_CONDITIONAL}
    assert PLACEHOLDER_TIER is CapabilityTier.P1_CONDITIONAL
    # 覆盖情况是映射阶段的结论，抽取阶段一无所知。
    assert all(not n.covered and not n.covering_case_ids for n in tree.nodes)
    # 反事实约束是文档 18 的职责，本阶段留空。
    assert tree.negative_constraints == []


async def test_映射结果按能力树过滤幻觉id(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = AnalyzerAgent()

    async def fake_call(prompt: str, schema: Any, *, system: str | None = None) -> Any:
        return CaseCapabilityMapping(
            capability_ids=[CAP_CSV, "cap-不存在", CAP_CSV, CAP_PIVOT],
            reasoning="读 CSV 再出透视表",
        )

    monkeypatch.setattr(agent, "_call_llm", fake_call)
    accepted = await agent.map_case_to_capabilities(_case("c-1"), _default_tree())

    # 编造的 id 被丢弃（留着会把覆盖率抬到一个不存在的节点上），重复项被折叠。
    assert accepted == [CAP_CSV, CAP_PIVOT]


async def test_空能力树时映射不发请求(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = AnalyzerAgent()
    called = False

    async def fake_call(prompt: str, schema: Any, *, system: str | None = None) -> Any:
        nonlocal called
        called = True
        raise AssertionError("不该发请求")

    monkeypatch.setattr(agent, "_call_llm", fake_call)
    assert await agent.map_case_to_capabilities(_case("c-1"), _tree()) == []
    assert called is False


# --------------------------------------------------------------------------- #
# 3. extract_capability_tree 节点与人工审核卡片
# --------------------------------------------------------------------------- #


async def test_能力树规模未超阈值时不挂起() -> None:
    pipeline, parts = _pipeline()
    result = await pipeline.extract_capability_tree(_state())

    assert result["capability_tree_id"] == build_capability_tree_id(SKILL_ID, BASE_REF)
    assert result[KEY_TREE_NODE_COUNT] == 3
    assert KEY_TREE_REVIEW_CONFIRMED not in result
    assert parts["approval_repo"].created == []
    assert len(parts["capability_repo"].saves) == 1


async def test_能力树超阈值时先落审批待办再挂起(monkeypatch: pytest.MonkeyPatch) -> None:
    """架构文档的"人工审核卡片"应对方案：拆得太细就交人确认，不自行继续。"""
    captured: dict[str, Any] = {}

    async def fake_suspend(reason: str, wait_key: str) -> Any:
        captured["reason"] = reason
        captured["wait_key"] = wait_key
        return "confirm"

    monkeypatch.setattr("skill_evaluate.nodes.coverage.nodes.suspend_and_wait", fake_suspend)
    pipeline, parts = _pipeline(settings=CoverageSettings(capability_count_review_threshold=2))

    result = await pipeline.extract_capability_tree(_state())

    assert captured["reason"] == "capability_tree_size_exceeds_threshold:3"
    assert captured["wait_key"] == f"{RUN_ID}:{NODE_PREFIX}:tree_review"
    # 待办必须在挂起**之前**落库，否则审批工作台看不到这张卡片，也就没人能唤醒它。
    assert parts["approval_repo"].created[0]["wait_key"] == captured["wait_key"]
    assert parts["approval_repo"].created[0]["thread_id"] == RUN_ID
    assert result[KEY_TREE_REVIEW_CONFIRMED] is True


@pytest.mark.parametrize(
    "payload",
    [None, "reject", {"decision": "reject"}, {"confirmed": False}, 42],
    ids=["none", "reject-str", "reject-dict", "confirmed-false", "unexpected-shape"],
)
async def test_人工未明确确认时拒绝继续(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    """默认不通过：形状没对上也算未确认，不能当成"那就继续吧"。"""

    async def fake_suspend(reason: str, wait_key: str) -> Any:
        return payload

    monkeypatch.setattr("skill_evaluate.nodes.coverage.nodes.suspend_and_wait", fake_suspend)
    pipeline, _ = _pipeline(settings=CoverageSettings(capability_count_review_threshold=2))

    with pytest.raises(PipelineSuspended, match="人工未确认该拆解粒度"):
        await pipeline.extract_capability_tree(_state())


@pytest.mark.parametrize(
    "payload",
    ["confirm", " Confirm ", {"decision": "confirm"}, {"confirmed": True}],
    ids=["str", "str-padded", "dict-decision", "dict-flag"],
)
async def test_确认信号的几种形状都被接受(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    async def fake_suspend(reason: str, wait_key: str) -> Any:
        return payload

    monkeypatch.setattr("skill_evaluate.nodes.coverage.nodes.suspend_and_wait", fake_suspend)
    pipeline, _ = _pipeline(settings=CoverageSettings(capability_count_review_threshold=2))
    result = await pipeline.extract_capability_tree(_state())
    assert result[KEY_TREE_REVIEW_CONFIRMED] is True


# --------------------------------------------------------------------------- #
# 4. map_case_coverage
# --------------------------------------------------------------------------- #


async def test_只用正向用例做覆盖映射() -> None:
    cases = [
        _case("c-pos", category=TestCaseCategory.POSITIVE),
        _case("c-neg", category=TestCaseCategory.NEGATIVE),
        _case("c-adv", category=TestCaseCategory.ADVERSARIAL),
    ]
    analyzer = FakeAnalyzer(mapping={"c-pos": [CAP_CSV], "c-neg": [CAP_XLSX]})
    pipeline, parts = _pipeline(analyzer=analyzer, cases=cases, tree=_default_tree())

    result = await pipeline.map_case_coverage(_state())

    assert analyzer.mapped_case_ids == ["c-pos"]
    assert result[KEY_MAPPED_CASE_COUNT] == 1
    saved = parts["capability_repo"].tree
    covered = {n.capability_id for n in saved.nodes if n.covered}
    # 反向用例即使"提到"了某项能力也不算覆盖：它的语义是"这类请求不该由本 Skill 处理"。
    assert covered == {CAP_CSV}


async def test_已有映射的用例跳过LLM调用但照常参与覆盖标记() -> None:
    """docs/dev/16 第 5 节伪码的关键修正。

    伪码用一句 `if case.target_capability_ids: continue` 同时跳过了"发请求"和
    "标记覆盖"。补盲生成的新用例在出题时就带着 `target_capability_ids`，于是它们
    永远不会去标记本该覆盖的节点——补盲回环怎么跑覆盖率都不涨，一路空转到迭代上限。
    """
    cases = [
        _case("c-old", capability_ids=[CAP_CSV]),  # 上一轮已映射
        _case("c-new", capability_ids=[CAP_XLSX]),  # 补盲时由 Generator 回填
        _case("c-fresh"),  # 从未映射过
    ]
    analyzer = FakeAnalyzer(mapping={"c-fresh": [CAP_PIVOT]})
    pipeline, parts = _pipeline(analyzer=analyzer, cases=cases, tree=_default_tree())

    await pipeline.map_case_coverage(_state())

    # 只为没有映射的那条发请求。
    assert analyzer.mapped_case_ids == ["c-fresh"]
    saved = parts["capability_repo"].tree
    # 三条用例的绑定全部计入覆盖，包括两条跳过了 LLM 调用的。
    assert {n.capability_id for n in saved.nodes if n.covered} == {CAP_CSV, CAP_XLSX, CAP_PIVOT}
    # 新映射的结果回填并落库，供下一轮回环跳过。
    assert [c.case_id for c in parts["case_repo"].saved] == ["c-fresh"]
    assert parts["case_repo"].saved[0].target_capability_ids == [CAP_PIVOT]


async def test_重复执行不会累积重复的covering_case_ids() -> None:
    """回环第二轮读回来的树已经带着上一轮的结果，必须清空重算。

    不清空的话 `covering_case_ids` 会出现重复项，而模块七要拿这个列表做用例聚类。
    """
    tree = _default_tree()
    tree.nodes[0].covered = True
    tree.nodes[0].covering_case_ids = ["c-1"]
    # 上一轮标过、这一轮不该再被标记的节点：验证"清空"而不只是"不重复 append"。
    tree.nodes[1].covered = True
    tree.nodes[1].covering_case_ids = ["c-gone"]

    pipeline, parts = _pipeline(cases=[_case("c-1", capability_ids=[CAP_CSV])], tree=tree)
    await pipeline.map_case_coverage(_state())

    saved = parts["capability_repo"].tree
    assert saved.nodes[0].covering_case_ids == ["c-1"]
    assert saved.nodes[1].covered is False
    assert saved.nodes[1].covering_case_ids == []


async def test_指向已消失能力的旧绑定被跳过而不报错() -> None:
    """SKILL.md 改写过某项能力的描述 → id 变了 → 旧绑定成为孤儿。

    本文档只跳过并记日志；淘汰与否是模块七"反向孤儿用例检测"的职责。
    """
    pipeline, parts = _pipeline(
        cases=[_case("c-1", capability_ids=[CAP_CSV, "csv-cleaner:cap-deadbeef1234"])],
        tree=_default_tree(),
    )
    await pipeline.map_case_coverage(_state())

    saved = parts["capability_repo"].tree
    assert {n.capability_id for n in saved.nodes if n.covered} == {CAP_CSV}


async def test_缺少active_suite_version_id时明确报错() -> None:
    from skill_evaluate.errors import PersistenceError

    pipeline, _ = _pipeline(tree=_default_tree())
    with pytest.raises(PersistenceError, match="active_suite_version_id"):
        await pipeline.map_case_coverage(_state(active_suite_version_id=None))


# --------------------------------------------------------------------------- #
# 5. blind_spot_detection
# --------------------------------------------------------------------------- #


async def test_盲区检测走Judge量化规则并落库() -> None:
    tree = _default_tree()
    tree.nodes[0].covered = True
    pipeline, parts = _pipeline(tree=tree)

    result = await pipeline.blind_spot_detection(_state())

    assert result[KEY_COVERAGE_RATIO] == pytest.approx(1 / 3)
    # 盲区带着描述而不只是 id：Generator 需要它做补盲指令，报告读者需要它读懂结论。
    assert {s.capability_id for s in _blind_spots(result)} == {CAP_XLSX, CAP_PIVOT}
    assert {s.description for s in _blind_spots(result)} == {
        "支持读取 Excel 文件",
        "支持输出数据透视表",
    }

    call = parts["judge"].calls[0]
    assert call["rule_name"] == RULE_CAPABILITY_COVERAGE
    # 判定主体是整个 Skill 的测试集，带前缀避免与其他"以 skill 为主体"的判定混淆。
    assert call["subject_id"] == f"{SUBJECT_PREFIX_COVERAGE}{SKILL_ID}"
    # 量化判定按约定不落库，本维度每轮只有一条、且状态要带走它的 id，因此显式存一次。
    assert [v.verdict_id for v in parts["judge_repo"].saved] == list(
        cast("list[str]", result["judge_verdict_ids"])
    )
    assert parts["judge_repo"].saved[0].status is JudgeVerdictStatus.FAIL


async def test_全覆盖时判定通过() -> None:
    tree = _default_tree()
    for node in tree.nodes:
        node.covered = True
    pipeline, parts = _pipeline(tree=tree)

    result = await pipeline.blind_spot_detection(_state())

    assert result[KEY_COVERAGE_RATIO] == 1.0
    assert _blind_spots(result) == []
    assert parts["judge_repo"].saved[0].status is JudgeVerdictStatus.PASS


def test_覆盖率规则以大于等于为口径() -> None:
    rule = get_rule(RULE_CAPABILITY_COVERAGE)
    # 阈值 0.9 的语义是"达到九成"，恰好 0.9 应当通过。
    inputs = {"coverage_ratio": 0.9, "threshold": 0.9, "tier_weighted": False}
    assert rule(inputs) is JudgeVerdictStatus.PASS
    assert rule({**inputs, "coverage_ratio": 0.89}) is JudgeVerdictStatus.FAIL


def test_口径标记由调用方按树的实际分级状态传入() -> None:
    """docs/dev/18 落地后 `tier_weighted` 不再写死（见 `nodes/coverage/rules.py`）。

    本维度跑在权重分级**之前**，树上全是占位 tier，因此这里传 False——加权算出来
    的数此刻与等权完全相同，但那个"相同"是巧合而不是结论，标成 True 会让读报告的
    人以为这个百分比已经体现了能力的重要性差异。
    """
    assert coverage_inputs(coverage_ratio=0.5, threshold=0.9, tier_weighted=False)[
        "tier_weighted"
    ] is False
    assert coverage_inputs(coverage_ratio=0.5, threshold=0.9, tier_weighted=True)[
        "tier_weighted"
    ] is True


async def test_覆盖率算法与加权口径共用同一个实现() -> None:
    """docs/dev/interfaces/16 第 4.2 节点名的坑：规则换成加权而节点里的算法没换，
    判定与报告会给出两个不同的数。因此 `blind_spot_detection` 调的就是
    `CapabilityTree.weighted_coverage()`，与模块八重算时是同一个方法。
    """
    tree = _default_tree()
    tree.nodes[0].tier = CapabilityTier.P0_CORE
    tree.nodes[0].covered = True
    pipeline, parts = _pipeline(tree=tree)

    result = await pipeline.blind_spot_detection(_state())

    assert result[KEY_COVERAGE_RATIO] == pytest.approx(tree.weighted_coverage())
    # 树上出现了不止一档 tier → 这次判定如实标注为加权口径。
    assert "'tier_weighted': True" in parts["judge_repo"].saved[0].reasoning


# --------------------------------------------------------------------------- #
# 6. feedback_driven_generation
# --------------------------------------------------------------------------- #


async def test_补盲把描述一并交给Generator() -> None:
    """`CapabilityFocus.descriptions` 是必填的（docs/dev/interfaces/06 第 1 节）。

    只给裸 id 的话，Prompt 里出现的是一串哈希，对模型没有任何信息量，补出来的题
    也就补不到真正的盲区。docs/dev/16 第 7 节的伪码漏了这个参数。
    """
    suite = FakeSuiteService()
    pipeline, _ = _pipeline(suite_service=suite)
    state = _state(
        **{
            KEY_BLIND_SPOTS: [
                BlindSpot(capability_id=CAP_XLSX, description="支持读取 Excel 文件").model_dump()
            ]
        }
    )

    result = await pipeline.feedback_driven_generation(state)

    focus = cast("CapabilityFocus", suite.calls[0]["focus"])
    assert focus.capability_ids == [CAP_XLSX]
    assert focus.describe(CAP_XLSX) == "支持读取 Excel 文件"
    assert suite.calls[0]["triggered_by"] == TRIGGERED_BY_COVERAGE_GAP
    # 新版本进公共字段：本维度的下一轮映射与主图后续维度都要用它（数据飞轮）。
    assert result["active_suite_version_id"] == "suite-patch-1"
    assert result[KEY_PATCH_ITERATIONS] == 1


async def test_达到迭代上限后不再补盲() -> None:
    """架构文档点名的"无限重试死锁"风险，靠这条硬上限兜住。"""
    suite = FakeSuiteService()
    pipeline, _ = _pipeline(suite_service=suite, settings=CoverageSettings(max_patch_iterations=2))
    state = _state(
        **{
            KEY_BLIND_SPOTS: [BlindSpot(capability_id=CAP_XLSX, description="d").model_dump()],
            KEY_PATCH_ITERATIONS: 2,
        }
    )

    result = await pipeline.feedback_driven_generation(state)

    assert result == {KEY_PATCH_EXHAUSTED: True}
    assert suite.calls == []


async def test_补盲失败降级为标记耗尽而不是掀掉流水线() -> None:
    """覆盖率不阻断合并，为一次补题失败把整条流水线掀掉不成比例。"""
    suite = FakeSuiteService(error=GenerationError("还没有任何 active 测试集版本"))
    pipeline, _ = _pipeline(suite_service=suite)
    state = _state(
        **{KEY_BLIND_SPOTS: [BlindSpot(capability_id=CAP_XLSX, description="d").model_dump()]}
    )

    result = await pipeline.feedback_driven_generation(state)

    assert result[KEY_PATCH_EXHAUSTED] is True
    assert "active 测试集版本" in str(result[KEY_PATCH_FAILURE])
    # 失败也不推进迭代计数——推进了会让"还剩几次机会"变得不可解释。
    assert KEY_PATCH_ITERATIONS not in result


async def test_没有盲区时不构造空focus() -> None:
    """空 `CapabilityFocus` 会被 `incremental_patch()` 拒绝（"无盲区可补"）。"""
    suite = FakeSuiteService()
    pipeline, _ = _pipeline(suite_service=suite)
    result = await pipeline.feedback_driven_generation(_state())
    assert result == {KEY_PATCH_EXHAUSTED: True}
    assert suite.calls == []


# --------------------------------------------------------------------------- #
# 7. 条件路由：环的两道出口
# --------------------------------------------------------------------------- #


def test_有盲区且有配额时回环补盲() -> None:
    pipeline, _ = _pipeline()
    state = _state(
        **{KEY_BLIND_SPOTS: [BlindSpot(capability_id=CAP_XLSX, description="d").model_dump()]}
    )
    assert pipeline.route_after_blind_spots(state) == NODE_NAMES["feedback_driven_generation"]


@pytest.mark.parametrize(
    "overrides",
    [
        {},  # 没有盲区
        {
            KEY_BLIND_SPOTS: [BlindSpot(capability_id=CAP_XLSX, description="d").model_dump()],
            KEY_PATCH_ITERATIONS: 3,
        },  # 配额用尽
        {
            KEY_BLIND_SPOTS: [BlindSpot(capability_id=CAP_XLSX, description="d").model_dump()],
            KEY_PATCH_EXHAUSTED: True,
        },  # 上一轮补盲失败
    ],
    ids=["no-blind-spots", "iterations-exhausted", "patch-failed"],
)
def test_三种情形直接收尾(overrides: dict[str, Any]) -> None:
    pipeline, _ = _pipeline()
    assert (
        pipeline.route_after_blind_spots(_state(**overrides))
        == NODE_NAMES["finalize_dimension_report"]
    )


def test_补盲之后的第二道出口() -> None:
    # 补盲成功 → 回映射节点重新计算覆盖。
    assert (
        CoveragePipeline.route_after_feedback(_state(**{KEY_PATCH_ITERATIONS: 1}))
        == NODE_NAMES["map_case_coverage"]
    )
    # 补盲被判定耗尽/失败 → 收尾。少了这道出口，环就没有出口了：补盲失败不推进
    # 迭代计数，无条件回边会让它一直转下去。
    assert (
        CoveragePipeline.route_after_feedback(_state(**{KEY_PATCH_EXHAUSTED: True}))
        == NODE_NAMES["finalize_dimension_report"]
    )


# --------------------------------------------------------------------------- #
# 8. finalize_dimension_report
# --------------------------------------------------------------------------- #


async def test_覆盖率达标时报告PASS且不阻断() -> None:
    pipeline, parts = _pipeline()
    await pipeline.finalize_dimension_report(
        _state(**{KEY_COVERAGE_RATIO: 0.95, KEY_TREE_NODE_COUNT: 20, KEY_MAPPED_CASE_COUNT: 9})
    )

    recorded = parts["reporter"].recorded[0]
    assert recorded["dimension"] == DIMENSION
    assert recorded["status"] is JudgeVerdictStatus.PASS
    assert recorded["score"] == pytest.approx(0.95)
    assert recorded["blocking"] is False


async def test_覆盖率不足判FAIL但仍不阻断合并() -> None:
    """docs/dev/16 第 8 节的阻断策略：测得全不全 ≠ Skill 有没有质量问题。"""
    pipeline, parts = _pipeline()
    await pipeline.finalize_dimension_report(
        _state(
            **{
                KEY_COVERAGE_RATIO: 0.5,
                KEY_TREE_NODE_COUNT: 2,
                KEY_BLIND_SPOTS: [
                    BlindSpot(
                        capability_id=CAP_XLSX, description="支持读取 Excel 文件"
                    ).model_dump()
                ],
            }
        )
    )

    recorded = parts["reporter"].recorded[0]
    assert recorded["status"] is JudgeVerdictStatus.FAIL
    assert recorded["blocking"] is False
    # findings 里带描述而不是只带哈希 id——只写 id 对读报告的人等于没写。
    assert any("支持读取 Excel 文件" in f for f in recorded["findings"])


async def test_空能力树判NEEDS_HUMAN_REVIEW而不是满分() -> None:
    """空树时公式给出 1.0，但那不是"测得全"而是"根本没抽出能力"。"""
    pipeline, parts = _pipeline()
    await pipeline.finalize_dimension_report(
        _state(**{KEY_COVERAGE_RATIO: 1.0, KEY_TREE_NODE_COUNT: 0})
    )

    recorded = parts["reporter"].recorded[0]
    assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
    assert any("未从 SKILL.md 抽出任何声明能力" in f for f in recorded["findings"])


async def test_私有键被裁掉时暴露给人而不是默默判PASS() -> None:
    """主图状态 schema 漏了本维度私有键的典型症状。"""
    pipeline, parts = _pipeline()
    await pipeline.finalize_dimension_report(_state())

    recorded = parts["reporter"].recorded[0]
    assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
    assert recorded["score"] is None
    assert any(KEY_COVERAGE_RATIO in f for f in recorded["findings"])


async def test_补盲耗尽在报告里如实标注() -> None:
    pipeline, parts = _pipeline(settings=CoverageSettings(max_patch_iterations=3))
    await pipeline.finalize_dimension_report(
        _state(
            **{
                KEY_COVERAGE_RATIO: 0.5,
                KEY_TREE_NODE_COUNT: 2,
                KEY_BLIND_SPOTS: [BlindSpot(capability_id=CAP_XLSX, description="d").model_dump()],
                KEY_PATCH_ITERATIONS: 3,
            }
        )
    )
    findings = parts["reporter"].recorded[0]["findings"]
    assert any("最大补盲迭代次数" in f for f in findings)


async def test_人工确认过的能力树在报告里留痕() -> None:
    pipeline, parts = _pipeline()
    await pipeline.finalize_dimension_report(
        _state(
            **{KEY_COVERAGE_RATIO: 1.0, KEY_TREE_NODE_COUNT: 25, KEY_TREE_REVIEW_CONFIRMED: True}
        )
    )
    assert any("已由人工确认" in f for f in parts["reporter"].recorded[0]["findings"])


# --------------------------------------------------------------------------- #
# 9. 路由声明与图结构
# --------------------------------------------------------------------------- #


def test_三个名字各司其职不得混用() -> None:
    """路由键 / 节点前缀 / 报告维度名三者不同，是本维度独有的情况。

    `dimension_results` 的唯一约束是 `(run_id, dimension)`：模块六/七/八若都写
    `coverage_analysis`，后跑完的那个会把先跑完的整行覆盖掉，且没有任何报错。
    """
    assert ROUTING_KEY == "coverage_analysis"
    assert NODE_PREFIX == "coverage"
    assert DIMENSION == "capability_coverage"
    assert DIMENSION != ROUTING_KEY
    assert all(name.startswith(f"{NODE_PREFIX}.") for name in NODE_NAMES.values())


def test_路由表被改成PLUGGABLE时装配期就报错(monkeypatch: pytest.MonkeyPatch) -> None:
    """本维度断言方向与模块一/三/四/五**相反**：它必须是 MINI。

    它根本没有调用 `ExecutorBackend` 的代码路径，改成 PLUGGABLE 不会产出更强的
    证据，只会让读路由表的人以为这里有真实执行。
    """
    monkeypatch.setitem(NODE_BACKEND_ROUTING, ROUTING_KEY, ExecutorBackendType.PLUGGABLE)
    with pytest.raises(ConfigurationError, match="纯分析维度"):
        CoverageDeps.assert_backend_routing()


def test_子图含回边且条件分支只通向声明过的去处() -> None:
    graph = build_coverage_subgraph().compile()
    drawable = graph.get_graph()
    edges = {(e.source, e.target) for e in drawable.edges}

    assert (NODE_NAMES["extract_capability_tree"], NODE_NAMES["map_case_coverage"]) in edges
    assert (NODE_NAMES["map_case_coverage"], NODE_NAMES["blind_spot_detection"]) in edges
    # 回边：本维度是全项目唯一带环的子图，环是"直到覆盖率达标"的字面实现。
    assert (
        NODE_NAMES["feedback_driven_generation"],
        NODE_NAMES["map_case_coverage"],
    ) in edges
    # 两道出口都存在，缺任何一道环都可能转不出来。
    assert (
        NODE_NAMES["blind_spot_detection"],
        NODE_NAMES["finalize_dimension_report"],
    ) in edges
    assert (
        NODE_NAMES["feedback_driven_generation"],
        NODE_NAMES["finalize_dimension_report"],
    ) in edges


def test_人工审核卡片节点进入interrupt_before清单() -> None:
    # 动态 interrupt 不加进静态列表也能挂起；列出来是为了让 docs/dev/24 汇总时
    # "这个节点可能停在人工审核上"在编译期就是显式的。
    assert INTERRUPT_BEFORE_NODES == [NODE_NAMES["extract_capability_tree"]]
