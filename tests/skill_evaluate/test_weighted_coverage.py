"""docs/dev/18：模块八——多维加权覆盖率算法与隐式边界追踪。

覆盖：权重分级的三条口径（id 不变、清单外 id 丢弃、漏判保留原值）、负向约束抽取
与 `constraint_id` 的稳定性、反事实覆盖映射的四条口径（只扫正向+对抗、不含 COLD、
自带绑定不花判定调用、预算耗尽记未判定而非未覆盖）、补题走正向模板且带描述、
加权覆盖率与模块六共用同一条规则且口径标记如实、组合缺口按真实权重重排、
可追溯性制品的 schema 与双格式、报告恒不阻断且缺键降级为 NEEDS_HUMAN_REVIEW，
以及图结构（无环、分支只通向声明过的两个去处、没有挂起点）。
全部用替身注入，不碰数据库、不发真实请求、不写项目目录。
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from skill_evaluate.agents.analyzer.identity import (
    build_capability_id,
    build_capability_tree_id,
    build_constraint_id,
)
from skill_evaluate.agents.analyzer.schema import TierAssignment, TierClassification
from skill_evaluate.agents.analyzer.service import PLACEHOLDER_TIER, AnalyzerAgent
from skill_evaluate.agents.generator.schema import CapabilityFocus
from skill_evaluate.agents.judge.rules import get_rule
from skill_evaluate.config import CoverageSettings
from skill_evaluate.errors import GenerationError, PersistenceError
from skill_evaluate.nodes.coverage import NODE_NAMES as COVERAGE_NODE_NAMES
from skill_evaluate.nodes.coverage import RULE_CAPABILITY_COVERAGE
from skill_evaluate.nodes.coverage.deps import CoverageDeps
from skill_evaluate.nodes.pruning import NODE_NAMES as PRUNING_NODE_NAMES
from skill_evaluate.nodes.weighted_coverage import (
    DIMENSION,
    ENTRY_NODE,
    INTERRUPT_BEFORE_NODES,
    KEY_ARTIFACT_FAILURE,
    KEY_ARTIFACT_PATH,
    KEY_CONSTRAINT_COUNT,
    KEY_CONSTRAINT_PATCHED_COUNT,
    KEY_CONSTRAINT_RATIO,
    KEY_NODE_COUNT,
    KEY_PATCH_FAILURE,
    KEY_PROBE_BUDGET_EXHAUSTED,
    KEY_PROBE_CALL_COUNT,
    KEY_RANKED_PAIR_COUNT,
    KEY_RANKED_UNCOVERED_PAIRS,
    KEY_RATIO,
    KEY_TIER_DISTRIBUTION,
    KEY_TIER_GRADED,
    KEY_TOTAL_PAIR_COUNT,
    KEY_UNCOVERED_CONSTRAINT_IDS,
    KEY_UNDETERMINED_CONSTRAINT_IDS,
    KEY_VERDICT_STATUS,
    NODE_NAMES,
    TERMINAL_NODE,
    WeightedCoverageDeps,
    WeightedCoveragePipeline,
    build_traceability_matrix,
    build_weighted_coverage_subgraph,
    flatten_for_csv,
    prioritized_pairs,
    prioritized_uncovered_pairs,
)
from skill_evaluate.nodes.weighted_coverage.deps import TRIGGERED_BY_NEGATIVE_CONSTRAINT_GAP
from skill_evaluate.state.capability import (
    CapabilityNode,
    CapabilityTree,
    NegativeConstraint,
)
from skill_evaluate.state.enums import (
    CapabilityTier,
    DatasetSplit,
    JudgeVerdictStatus,
    TestCaseCategory,
)
from skill_evaluate.state.judge import JudgeVerdict
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion

SKILL_ID = "csv-cleaner"
RUN_ID = "run-wcov-1"
BASE_REF = "v1"
SUITE_ID = "suite-1"

CAP_CSV = build_capability_id(SKILL_ID, "支持读取 CSV 文件")
CAP_XLSX = build_capability_id(SKILL_ID, "支持读取 Excel 文件")
CAP_PIVOT = build_capability_id(SKILL_ID, "支持输出数据透视表")

CONSTRAINT_SOFT_DELETE = build_constraint_id(SKILL_ID, "查询 users 表时必须过滤软删除记录")
CONSTRAINT_NO_FULL_SORT = build_constraint_id(SKILL_ID, "不要对超过百万行的表做全表排序")


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def _skill() -> SkillDefinition:
    return SkillDefinition(
        skill_id=SKILL_ID,
        version_ref=BASE_REF,
        root_path=".",
        description="清洗并校验 CSV / Excel 导出文件",
        body_markdown="# CSV Cleaner\n\n## Gotchas\n\n- 查询 users 表时必须过滤软删除记录\n",
        line_count=5,
        token_count=40,
    )


def _node(
    capability_id: str,
    description: str,
    *,
    tier: CapabilityTier = PLACEHOLDER_TIER,
    covered: bool = False,
) -> CapabilityNode:
    return CapabilityNode(
        capability_id=capability_id,
        skill_id=SKILL_ID,
        description=description,
        tier=tier,
        covered=covered,
        covering_case_ids=["c1"] if covered else [],
    )


def _constraint(constraint_id: str, description: str) -> NegativeConstraint:
    return NegativeConstraint(constraint_id=constraint_id, description=description)


def _tree(
    *nodes: CapabilityNode,
    constraints: list[NegativeConstraint] | None = None,
    covered_pairs: list[tuple[str, str]] | None = None,
) -> CapabilityTree:
    return CapabilityTree(
        skill_id=SKILL_ID,
        skill_version_ref=BASE_REF,
        nodes=list(nodes),
        negative_constraints=list(constraints or []),
        combinatorial_pairs_covered=list(covered_pairs or []),
    )


def _default_tree() -> CapabilityTree:
    return _tree(
        _node(CAP_CSV, "支持读取 CSV 文件", tier=CapabilityTier.P0_CORE, covered=True),
        _node(CAP_XLSX, "支持读取 Excel 文件", tier=CapabilityTier.P1_CONDITIONAL),
        _node(CAP_PIVOT, "支持输出数据透视表", tier=CapabilityTier.P2_DEFENSIVE, covered=True),
    )


def _case(
    case_id: str,
    *,
    category: TestCaseCategory = TestCaseCategory.POSITIVE,
    split: DatasetSplit = DatasetSplit.TRAIN,
    constraint_ids: list[str] | None = None,
    capability_ids: list[str] | None = None,
    prompt: str | None = None,
) -> TestCase:
    return TestCase(
        case_id=case_id,
        skill_id=SKILL_ID,
        category=category,
        split=split,
        prompt=prompt if prompt is not None else f"帮我把这份导出理一下（{case_id}）",
        target_capability_ids=list(capability_ids or []),
        negative_constraint_ids=list(constraint_ids or []),
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
        # 深拷贝：真实仓储写的是 JSONB，读回来是新对象。
        self.tree = tree.model_copy(deep=True)
        self.saves.append(self.tree)
        return "row-1"

    async def get(self, skill_id: str, skill_version_ref: str) -> CapabilityTree | None:
        return self.tree.model_copy(deep=True) if self.tree else None


class FakeCaseRepo:
    def __init__(self, cases: list[TestCase]) -> None:
        self.cases = cases
        self.requested_categories: list[list[TestCaseCategory]] = []

    async def list_by_categories(
        self, suite_version_id: str, categories: list[TestCaseCategory]
    ) -> list[TestCase]:
        self.requested_categories.append(list(categories))
        return [c.model_copy(deep=True) for c in self.cases if c.category in categories]


class FakeJudgeRepo:
    def __init__(self) -> None:
        self.saved: list[JudgeVerdict] = []

    async def save_verdict(self, verdict: JudgeVerdict) -> None:
        self.saved.append(verdict)


class FakeReporter:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record_dimension_result(self, **kwargs: Any) -> None:
        self.recorded.append(kwargs)


class FakeAnalyzer:
    """替代 `AnalyzerAgent`：分级与约束抽取的结果都由测试直接给定。"""

    def __init__(
        self,
        *,
        tiers: dict[str, CapabilityTier] | None = None,
        constraints: list[NegativeConstraint] | None = None,
    ) -> None:
        self.tiers = tiers or {}
        self.constraints = constraints or []
        self.classify_calls = 0

    async def classify_tiers(self, tree: CapabilityTree, skill: SkillDefinition) -> CapabilityTree:
        self.classify_calls += 1
        for node in tree.nodes:
            node.tier = self.tiers.get(node.capability_id, node.tier)
        return tree

    async def extract_negative_constraints(
        self, skill: SkillDefinition
    ) -> list[NegativeConstraint]:
        return [c.model_copy(deep=True) for c in self.constraints]


class FakeJudge:
    """替代 `JudgeAgent`。

    - **量化判定走真实规则表**（与模块六的测试同一条理由：需要隔离的是 LLM 与
      数据库，规则表是纯函数，把它换成替身等于什么都没测）；
    - **裁量判定由测试给定**：`probe` 回调按 (约束描述, 用例 id) 决定 pass/fail，
      返回 None 表示"这次被黄金盲测占用"。
    """

    def __init__(self, probe: Any | None = None) -> None:
        self.quantitative_calls: list[dict[str, Any]] = []
        self.probe_calls: list[dict[str, Any]] = []
        self._probe = probe

    def quantitative_verdict(
        self, subject_id: str, rule_name: str, inputs: dict[str, Any]
    ) -> JudgeVerdict:
        self.quantitative_calls.append(
            {"subject_id": subject_id, "rule_name": rule_name, "inputs": inputs}
        )
        return JudgeVerdict(
            verdict_id=f"v-{len(self.quantitative_calls)}",
            subject_id=subject_id,
            status=get_rule(rule_name)(inputs),
            reasoning=f"quantitative rule {rule_name!r} over inputs={inputs!r}",
            temperature=0.0,
            model=f"rule:{rule_name}",
            created_at=datetime.now(UTC),
        )

    async def judgmental_verdict(
        self,
        subject_id: str,
        template_key: str,
        content: dict[str, str],
        criticality: Any,
    ) -> JudgeVerdict:
        self.probe_calls.append(
            {
                "subject_id": subject_id,
                "template_key": template_key,
                "content": content,
                "criticality": criticality,
            }
        )
        decision = self._probe(content) if self._probe else False
        if decision is None:
            # 黄金基准盲测：subject_id 带前缀，调用方必须跳过。
            return JudgeVerdict(
                verdict_id=f"g-{len(self.probe_calls)}",
                subject_id=f"__golden__:{subject_id}",
                status=JudgeVerdictStatus.PASS,
                reasoning="golden",
                temperature=0.0,
                model="mini",
                created_at=datetime.now(UTC),
            )
        return JudgeVerdict(
            verdict_id=f"p-{len(self.probe_calls)}",
            subject_id=subject_id,
            status=JudgeVerdictStatus.PASS if decision else JudgeVerdictStatus.FAIL,
            reasoning="probe",
            temperature=0.0,
            model="mini",
            created_at=datetime.now(UTC),
        )


class FakeSuiteService:
    """替代 `TestSuiteService`：只记录 `incremental_patch()` 的入参。"""

    def __init__(self, *, error: GenerationError | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def incremental_patch(
        self,
        skill: SkillDefinition,
        focus: CapabilityFocus,
        triggered_by: str,
        *,
        positive_count: int | None = None,
        negative_count: int | None = None,
    ) -> TestSuiteVersion:
        self.calls.append(
            {
                "skill": skill,
                "focus": focus,
                "triggered_by": triggered_by,
                "positive_count": positive_count,
                "negative_count": negative_count,
            }
        )
        if self.error is not None:
            raise self.error
        return TestSuiteVersion(
            suite_version_id="suite-patch-1",
            skill_id=skill.skill_id,
            skill_version_ref=skill.version_ref,
            generation_mode="incremental_patch",
            case_ids=[],
            created_at=datetime.now(UTC),
        )


def _pipeline(
    *,
    tree: CapabilityTree | None = None,
    cases: list[TestCase] | None = None,
    analyzer: FakeAnalyzer | None = None,
    judge: FakeJudge | None = None,
    suite_service: FakeSuiteService | None = None,
    skill: SkillDefinition | None = None,
    settings: CoverageSettings | None = None,
) -> tuple[WeightedCoveragePipeline, dict[str, Any]]:
    """装一条全替身的流水线，并把替身一并交回给测试做断言。"""
    parts: dict[str, Any] = {
        "skill_repo": FakeSkillRepo(skill if skill is not None else _skill()),
        "case_repo": FakeCaseRepo(cases if cases is not None else []),
        "capability_repo": FakeCapabilityRepo(tree if tree is not None else _default_tree()),
        "judge_repo": FakeJudgeRepo(),
        "reporter": FakeReporter(),
        "analyzer": analyzer or FakeAnalyzer(),
        "judge": judge or FakeJudge(),
        "suite_service": suite_service or FakeSuiteService(),
    }
    deps = WeightedCoverageDeps(
        analyzer_agent=cast("Any", parts["analyzer"]),
        judge_agent=cast("Any", parts["judge"]),
        suite_service=cast("Any", parts["suite_service"]),
        report_generator=cast("Any", parts["reporter"]),
        skill_repository=cast("Any", parts["skill_repo"]),
        test_case_repository=cast("Any", parts["case_repo"]),
        capability_repository=cast("Any", parts["capability_repo"]),
        judge_repository=cast("Any", parts["judge_repo"]),
        coverage_settings=settings or CoverageSettings(),
    )
    return WeightedCoveragePipeline(deps), parts


# --------------------------------------------------------------------------- #
# 0. 命名与装配约定（docs/dev/interfaces/16 第 1/4 节）
# --------------------------------------------------------------------------- #


def test_维度名与模块六七都不同否则报告会被整行覆盖() -> None:
    # `dimension_results` 的唯一约束是 (run_id, dimension)：三份覆盖率文档若共用
    # 一个维度名，后跑完的会把先跑完的整行覆盖掉，且不会有任何报错。
    from skill_evaluate.nodes.coverage import DIMENSION as COVERAGE_DIMENSION
    from skill_evaluate.nodes.pruning import DIMENSION as PRUNING_DIMENSION

    assert DIMENSION == "weighted_coverage"
    assert len({DIMENSION, COVERAGE_DIMENSION, PRUNING_DIMENSION}) == 3


def test_节点名沿用coverage前缀但不与模块六七重名() -> None:
    # 同一张图里节点名必须唯一（重名的直接后果是先加的节点被静默覆盖）。
    assert all(name.startswith("coverage.") for name in NODE_NAMES.values())
    taken = set(COVERAGE_NODE_NAMES.values()) | set(PRUNING_NODE_NAMES.values())
    assert not (set(NODE_NAMES.values()) & taken)


def test_本维度没有挂起点() -> None:
    # 显式导出空列表而不是干脆不定义：docs/dev/24 逐个维度取这个常量。
    assert INTERRUPT_BEFORE_NODES == []


def test_deps_from_coverage复用上游已构造的实例() -> None:
    analyzer = FakeAnalyzer()
    upstream = CoverageDeps(analyzer_agent=cast("Any", analyzer))
    deps = WeightedCoverageDeps.from_coverage(upstream)

    # 共享的是**实例本身**：各自实例化会让两个 Agent 持有不同的 trace_handle，
    # Langfuse 上就会出现两条彼此无关的调用线。
    assert deps.analyzer() is analyzer
    # 已经是本类型时原样返回，不悄悄换掉实例。
    assert WeightedCoverageDeps.from_coverage(deps) is deps


# --------------------------------------------------------------------------- #
# 1. extract_tier_and_negative_constraints
# --------------------------------------------------------------------------- #


async def test_分级原地更新tier且capability_id不变() -> None:
    """`docs/dev/interfaces/16` 第 4.1 节：改 id 的后果是覆盖率无征兆地从 92% 掉到 40%。"""
    tree = _tree(
        _node(CAP_CSV, "支持读取 CSV 文件"),
        _node(CAP_XLSX, "支持读取 Excel 文件"),
    )
    analyzer = FakeAnalyzer(
        tiers={CAP_CSV: CapabilityTier.P0_CORE, CAP_XLSX: CapabilityTier.P2_DEFENSIVE}
    )
    pipeline, parts = _pipeline(tree=tree, analyzer=analyzer)

    result = await pipeline.extract_tier_and_negative_constraints(_state())

    saved = parts["capability_repo"].tree
    assert [n.capability_id for n in saved.nodes] == [CAP_CSV, CAP_XLSX]
    assert [n.tier for n in saved.nodes] == [
        CapabilityTier.P0_CORE,
        CapabilityTier.P2_DEFENSIVE,
    ]
    assert result[KEY_TIER_GRADED] is True
    assert result[KEY_TIER_DISTRIBUTION] == {"p0_core": 1, "p1_conditional": 0, "p2_defensive": 1}


async def test_负向约束抽取结果落在同一棵树上() -> None:
    analyzer = FakeAnalyzer(
        constraints=[_constraint(CONSTRAINT_SOFT_DELETE, "查询 users 表时必须过滤软删除记录")]
    )
    pipeline, parts = _pipeline(analyzer=analyzer)

    result = await pipeline.extract_tier_and_negative_constraints(_state())

    assert result[KEY_CONSTRAINT_COUNT] == 1
    assert parts["capability_repo"].tree.negative_constraints[0].constraint_id == (
        CONSTRAINT_SOFT_DELETE
    )
    # 抽取阶段对"有没有用例诱导过这个坑"一无所知。
    assert parts["capability_repo"].tree.negative_constraints[0].covered is False


async def test_分级全落在同一档时如实标注未分级() -> None:
    """全填同一档等于没分级：加权口径退化成等权，报告必须说出来。"""
    tree = _tree(_node(CAP_CSV, "读 CSV"), _node(CAP_XLSX, "读 Excel"))
    analyzer = FakeAnalyzer(
        tiers={CAP_CSV: CapabilityTier.P0_CORE, CAP_XLSX: CapabilityTier.P0_CORE}
    )
    pipeline, _ = _pipeline(tree=tree, analyzer=analyzer)

    result = await pipeline.extract_tier_and_negative_constraints(_state())

    assert result[KEY_TIER_GRADED] is False


async def test_没有能力树时立刻失败并点名顺序约束() -> None:
    pipeline, _ = _pipeline()

    with pytest.raises(PersistenceError, match="capability_tree_id"):
        await pipeline.extract_tier_and_negative_constraints(_state(capability_tree_id=None))


# --------------------------------------------------------------------------- #
# 2. AnalyzerAgent 的两个新方法（不发真实请求）
# --------------------------------------------------------------------------- #


async def test_分级丢弃清单外的id并保留漏判节点的原值() -> None:
    """两条口径：模型编的 id 不能改写能力树结构；漏判的保留占位值而不是默认 P0。"""
    agent = AnalyzerAgent(llm_client=cast("Any", object()))
    tree = _tree(_node(CAP_CSV, "读 CSV"), _node(CAP_XLSX, "读 Excel"))

    async def _fake_call(prompt: str, schema: Any, *, system: str | None = None) -> Any:
        return TierClassification(
            assignments=[
                TierAssignment(capability_id=CAP_CSV, tier="p0_core", reason="核心路径"),
                TierAssignment(capability_id="伪造的-id", tier="p0_core", reason="编的"),
            ]
        )

    agent._call_llm = _fake_call  # type: ignore[method-assign]
    updated = await agent.classify_tiers(tree, _skill())

    assert updated.nodes[0].tier is CapabilityTier.P0_CORE
    # 漏判的那一项保留占位值：默认填 P0 会让加权覆盖率朝最激进的口径偏。
    assert updated.nodes[1].tier is PLACEHOLDER_TIER
    assert len(updated.nodes) == 2


async def test_空能力树不发分级请求() -> None:
    agent = AnalyzerAgent(llm_client=cast("Any", object()))
    called = False

    async def _fake_call(prompt: str, schema: Any, *, system: str | None = None) -> Any:
        nonlocal called
        called = True
        raise AssertionError("空树不该发请求")

    agent._call_llm = _fake_call  # type: ignore[method-assign]
    result = await agent.classify_tiers(_tree(), _skill())

    assert result.nodes == []
    assert called is False


def test_约束id是描述文本的确定性哈希且与能力id不同空间() -> None:
    # 同一句话重抽得到同一个 id（历史绑定才不会集体失效）。
    assert build_constraint_id(SKILL_ID, "查询 users 表时必须过滤软删除记录") == (
        CONSTRAINT_SOFT_DELETE
    )
    # 书写差异（标点/空白/大小写）被归一掉。
    assert build_constraint_id(SKILL_ID, " 查询 users 表时必须过滤软删除记录 ") == (
        CONSTRAINT_SOFT_DELETE
    )
    # 中缀不同 → 即使同一句话既当能力又当约束，也不会撞 id。
    assert build_capability_id(SKILL_ID, "x") != build_constraint_id(SKILL_ID, "x")
    assert ":neg-" in CONSTRAINT_SOFT_DELETE


# --------------------------------------------------------------------------- #
# 3. map_negative_constraint_coverage
# --------------------------------------------------------------------------- #


def _tree_with_constraints() -> CapabilityTree:
    return _tree(
        _node(CAP_CSV, "支持读取 CSV 文件", tier=CapabilityTier.P0_CORE, covered=True),
        constraints=[
            _constraint(CONSTRAINT_SOFT_DELETE, "查询 users 表时必须过滤软删除记录"),
            _constraint(CONSTRAINT_NO_FULL_SORT, "不要对超过百万行的表做全表排序"),
        ],
    )


async def test_自带绑定的用例直接算覆盖不花判定调用() -> None:
    """出题时回填的 `negative_constraint_ids` 是事实，再让模型判一次既贵又可能判错。"""
    cases = [_case("c1", constraint_ids=[CONSTRAINT_SOFT_DELETE])]
    judge = FakeJudge(probe=lambda content: False)
    pipeline, parts = _pipeline(tree=_tree_with_constraints(), cases=cases, judge=judge)

    result = await pipeline.map_negative_constraint_coverage(_state())

    saved = {c.constraint_id: c for c in parts["capability_repo"].tree.negative_constraints}
    assert saved[CONSTRAINT_SOFT_DELETE].covered is True
    assert saved[CONSTRAINT_SOFT_DELETE].covering_case_ids == ["c1"]
    # 只有"另一条约束 × 这条用例"这一个组合需要判定。
    assert len(judge.probe_calls) == 1
    assert result[KEY_PROBE_CALL_COUNT] == 1
    assert result[KEY_UNCOVERED_CONSTRAINT_IDS] == [CONSTRAINT_NO_FULL_SORT]


async def test_只扫正向与对抗用例且不含冷用例() -> None:
    cases = [
        _case("cold", split=DatasetSplit.COLD),
        _case("adv", category=TestCaseCategory.ADVERSARIAL),
    ]
    judge = FakeJudge(probe=lambda content: True)
    pipeline, parts = _pipeline(tree=_tree_with_constraints(), cases=cases, judge=judge)

    await pipeline.map_negative_constraint_coverage(_state())

    # 反向近脱靶用例的语义是"这类请求不该由本 Skill 处理"，拿它证明禁令被测到了
    # 自相矛盾；COLD 用例已被模块七降级，靠它撑覆盖率同样是悖论。
    assert parts["case_repo"].requested_categories == [
        [TestCaseCategory.POSITIVE, TestCaseCategory.ADVERSARIAL]
    ]
    probed_cases = {call["subject_id"].rsplit(":", 1)[-1] for call in judge.probe_calls}
    assert probed_cases == {"adv"}


async def test_判定预算耗尽记未判定而不是未覆盖() -> None:
    """未覆盖是一个结论，未判定是"这次没算出结论"——混为一谈会凭空生成补题需求。"""
    cases = [_case("c1"), _case("c2")]
    judge = FakeJudge(probe=lambda content: False)
    pipeline, _ = _pipeline(
        tree=_tree_with_constraints(),
        cases=cases,
        judge=judge,
        settings=CoverageSettings(max_constraint_probe_calls=2),
    )

    result = await pipeline.map_negative_constraint_coverage(_state())

    assert len(judge.probe_calls) == 2
    assert result[KEY_PROBE_BUDGET_EXHAUSTED] is True
    # 两条约束各判了一次（用例优先的次序保证机会均等），剩下的组合没轮到 →
    # 两条都记未判定，一条都不进补题清单。
    assert set(cast("list[str]", result[KEY_UNDETERMINED_CONSTRAINT_IDS])) == {
        CONSTRAINT_SOFT_DELETE,
        CONSTRAINT_NO_FULL_SORT,
    }
    assert result[KEY_UNCOVERED_CONSTRAINT_IDS] == []


async def test_被黄金盲测占用的判定不算结论() -> None:
    """黄金注入判的是另一份文本，拿它决定这条约束覆盖没覆盖等于张冠李戴。"""
    cases = [_case("c1")]
    judge = FakeJudge(probe=lambda content: None)
    pipeline, _ = _pipeline(tree=_tree_with_constraints(), cases=cases, judge=judge)

    result = await pipeline.map_negative_constraint_coverage(_state())

    assert result[KEY_UNCOVERED_CONSTRAINT_IDS] == []
    assert len(cast("list[str]", result[KEY_UNDETERMINED_CONSTRAINT_IDS])) == 2


async def test_裁判判定pass才算诱导了陷阱() -> None:
    cases = [_case("c1", prompt="统计活跃用户，上个月批量注销过一批账号")]
    judge = FakeJudge(probe=lambda content: "软删除" in content["constraint_description"])
    pipeline, parts = _pipeline(tree=_tree_with_constraints(), cases=cases, judge=judge)

    result = await pipeline.map_negative_constraint_coverage(_state())

    saved = {c.constraint_id: c for c in parts["capability_repo"].tree.negative_constraints}
    assert saved[CONSTRAINT_SOFT_DELETE].covering_case_ids == ["c1"]
    assert saved[CONSTRAINT_NO_FULL_SORT].covered is False
    assert result[KEY_UNCOVERED_CONSTRAINT_IDS] == [CONSTRAINT_NO_FULL_SORT]
    assert result[KEY_CONSTRAINT_RATIO] == pytest.approx(0.5)
    # 判定走 ROUTINE：误判的后果是覆盖率统计偏一点，不值三倍 Token 的共识投票。
    from skill_evaluate.state.enums import Criticality

    assert judge.probe_calls[0]["criticality"] is Criticality.ROUTINE
    assert judge.probe_calls[0]["template_key"] == "negative_constraint_probe"


async def test_映射每轮从零重算保证幂等() -> None:
    tree = _tree_with_constraints()
    tree.negative_constraints[0].covered = True
    tree.negative_constraints[0].covering_case_ids = ["c1"]
    cases = [_case("c1", constraint_ids=[CONSTRAINT_SOFT_DELETE])]
    pipeline, parts = _pipeline(tree=tree, cases=cases, judge=FakeJudge(probe=lambda c: False))

    await pipeline.map_negative_constraint_coverage(_state())

    saved = parts["capability_repo"].tree.negative_constraints[0]
    # 不是 ["c1", "c1"]——增量累加会让重跑时出现重复项，制品的 diff 从此全是噪音。
    assert saved.covering_case_ids == ["c1"]


async def test_没有负向约束时覆盖率取一而不是零() -> None:
    """没写禁令 ≠ 该测的都没测。报告靠约束总数把两者分开。"""
    pipeline, parts = _pipeline(tree=_default_tree(), cases=[_case("c1")])

    result = await pipeline.map_negative_constraint_coverage(_state())

    assert result[KEY_CONSTRAINT_RATIO] == 1.0
    assert result[KEY_PROBE_CALL_COUNT] == 0
    # 一条用例都不用读。
    assert parts["case_repo"].requested_categories == []


async def test_缺少用例集版本号时抛错而不是当成全都没覆盖() -> None:
    pipeline, _ = _pipeline(tree=_tree_with_constraints())

    with pytest.raises(PersistenceError, match="active_suite_version_id"):
        await pipeline.map_negative_constraint_coverage(_state(active_suite_version_id=None))


# --------------------------------------------------------------------------- #
# 4. constraint_feedback_generation
# --------------------------------------------------------------------------- #


async def test_补题走正向模板并带上人类可读描述() -> None:
    """`incremental_patch()` 的默认映射会把约束补成 NEGATIVE 近脱靶题，那是错的。"""
    suite = FakeSuiteService()
    pipeline, _ = _pipeline(tree=_tree_with_constraints(), suite_service=suite)
    state = _state(**{KEY_UNCOVERED_CONSTRAINT_IDS: [CONSTRAINT_SOFT_DELETE]})

    result = await pipeline.constraint_feedback_generation(state)

    call = suite.calls[0]
    focus = cast("CapabilityFocus", call["focus"])
    assert focus.negative_constraint_ids == [CONSTRAINT_SOFT_DELETE]
    assert focus.describe(CONSTRAINT_SOFT_DELETE) == "查询 users 表时必须过滤软删除记录"
    # 反事实用例是**该由本 Skill 处理**的真实请求，只是场景里埋了坑 → 正向模板。
    assert call["positive_count"] == 1
    assert call["negative_count"] == 0
    assert call["triggered_by"] == TRIGGERED_BY_NEGATIVE_CONSTRAINT_GAP
    assert result["active_suite_version_id"] == "suite-patch-1"
    assert result[KEY_CONSTRAINT_PATCHED_COUNT] == 1


async def test_补题失败只降级不掀掉流水线() -> None:
    suite = FakeSuiteService(error=GenerationError("还没有 active 测试集"))
    pipeline, _ = _pipeline(tree=_tree_with_constraints(), suite_service=suite)
    state = _state(**{KEY_UNCOVERED_CONSTRAINT_IDS: [CONSTRAINT_SOFT_DELETE]})

    result = await pipeline.constraint_feedback_generation(state)

    assert "还没有 active 测试集" in str(result[KEY_PATCH_FAILURE])
    assert "active_suite_version_id" not in result


def test_只有明确未覆盖才进补题节点() -> None:
    pipeline, _ = _pipeline()

    assert (
        pipeline.route_after_constraint_mapping(_state())
        == (NODE_NAMES["recompute_weighted_coverage"])
    )
    # 未判定的不触发补题：为一条可能本来就覆盖着的约束补题，既浪费一次生成调用，
    # 又往测试集里塞一条多余的题。
    assert (
        pipeline.route_after_constraint_mapping(
            _state(**{KEY_UNDETERMINED_CONSTRAINT_IDS: [CONSTRAINT_SOFT_DELETE]})
        )
        == NODE_NAMES["recompute_weighted_coverage"]
    )
    assert (
        pipeline.route_after_constraint_mapping(
            _state(**{KEY_UNCOVERED_CONSTRAINT_IDS: [CONSTRAINT_SOFT_DELETE]})
        )
        == NODE_NAMES["constraint_feedback_generation"]
    )


# --------------------------------------------------------------------------- #
# 5. recompute_weighted_coverage
# --------------------------------------------------------------------------- #


async def test_加权重算复用模块六那条规则且标注加权口径() -> None:
    """规则名只有一个（`register_rule()` 遇重名直接抛错），区别在调用时机与口径标记。"""
    tree = _default_tree()  # P0 已覆盖(0.6) + P2 已覆盖(0.1) / 总权重 1.0
    judge = FakeJudge()
    pipeline, parts = _pipeline(tree=tree, judge=judge)

    result = await pipeline.recompute_weighted_coverage(_state())

    assert result[KEY_RATIO] == pytest.approx(0.7)
    call = judge.quantitative_calls[0]
    assert call["rule_name"] == RULE_CAPABILITY_COVERAGE
    assert call["inputs"]["tier_weighted"] is True
    # 与模块六的判定分开归档，否则两种口径的历史记录混成一堆。
    assert call["subject_id"].startswith("wcoverage:")
    assert result[KEY_VERDICT_STATUS] == JudgeVerdictStatus.FAIL.value
    # 量化判定本身不落库（docs/dev/interfaces/08 第 1 节），要归档得调用方自己存。
    assert parts["judge_repo"].saved[0].verdict_id == result["judge_verdict_ids"][0]


async def test_加权口径能把等权口径的及格线拉下来() -> None:
    """一份 P0 全空、P2 全满的测试集：等权刚好 50%，加权只剩 14%。"""
    tree = _tree(
        _node("p0", "核心", tier=CapabilityTier.P0_CORE),
        _node("p2", "格式", tier=CapabilityTier.P2_DEFENSIVE, covered=True),
    )
    pipeline, _ = _pipeline(tree=tree)

    result = await pipeline.recompute_weighted_coverage(_state())

    assert result[KEY_RATIO] == pytest.approx(0.1 / 0.7)


# --------------------------------------------------------------------------- #
# 6. upgrade_combinatorial_priority
# --------------------------------------------------------------------------- #


async def test_组合缺口按真实权重重排P0组合在最前() -> None:
    tree = _tree(
        _node("p2", "防御", tier=CapabilityTier.P2_DEFENSIVE),
        _node("p0a", "核心 A", tier=CapabilityTier.P0_CORE),
        _node("p0b", "核心 B", tier=CapabilityTier.P0_CORE),
    )
    pipeline, _ = _pipeline(tree=tree)

    result = await pipeline.upgrade_combinatorial_priority(_state())

    pairs = cast("list[list[str]]", result[KEY_RANKED_UNCOVERED_PAIRS])
    assert pairs[0] == ["p0a", "p0b"]
    assert result[KEY_TOTAL_PAIR_COUNT] == 3
    assert result[KEY_RANKED_PAIR_COUNT] == 3


async def test_已覆盖组合直接读模块七落库的那份() -> None:
    """docs/dev/interfaces/17 第 4.1 节：直接读它即可，不必重扫用例。"""
    tree = _tree(
        _node("p0a", "核心 A", tier=CapabilityTier.P0_CORE),
        _node("p0b", "核心 B", tier=CapabilityTier.P0_CORE),
        covered_pairs=[("p0a", "p0b")],
    )
    pipeline, parts = _pipeline(tree=tree, cases=[_case("c1")])

    result = await pipeline.upgrade_combinatorial_priority(_state())

    assert result[KEY_RANKED_UNCOVERED_PAIRS] == []
    # 一条用例都没读。
    assert parts["case_repo"].requested_categories == []


def test_排序函数的确定性与未知id的兜底() -> None:
    tree = _tree(
        _node("p0", "核心", tier=CapabilityTier.P0_CORE),
        _node("p1", "条件", tier=CapabilityTier.P1_CONDITIONAL),
    )
    # 同一棵树两次调用给出同一个次序（截断范围稳定，覆盖率才可比较）。
    assert prioritized_pairs(tree) == prioritized_pairs(tree)
    # 树上已不存在的 id 按权重 0 处理、排在最后，而不是抛异常掀掉流水线——
    # Checkpoint 恢复时状态里可能带着上一版能力树的组合对。
    ranked = prioritized_uncovered_pairs(tree, [("p0", "已消失"), ("p0", "p1")], limit=2)
    assert ranked == [("p0", "p1"), ("p0", "已消失")]
    assert prioritized_uncovered_pairs(tree, [("p0", "p1")], limit=0) == []


# --------------------------------------------------------------------------- #
# 7. generate_traceability_artifact
# --------------------------------------------------------------------------- #


async def test_制品同时产出json与csv且内容自洽(tmp_path: Path) -> None:
    tree = _tree(
        _node(CAP_CSV, "支持读取 CSV 文件", tier=CapabilityTier.P0_CORE, covered=True),
        _node(CAP_XLSX, "支持读取 Excel 文件", tier=CapabilityTier.P1_CONDITIONAL),
        constraints=[_constraint(CONSTRAINT_SOFT_DELETE, "必须过滤软删除")],
        covered_pairs=[(CAP_CSV, CAP_XLSX)],
    )
    pipeline, _ = _pipeline(tree=tree, settings=CoverageSettings(artifacts_dir=str(tmp_path)))

    result = await pipeline.generate_traceability_artifact(_state())

    json_path = Path(cast("str", result[KEY_ARTIFACT_PATH]))
    assert json_path == tmp_path / RUN_ID / "traceability_matrix.json"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["skill_id"] == SKILL_ID
    assert [n["id"] for n in payload["nodes"]] == [CAP_CSV, CAP_XLSX]
    assert payload["nodes"][0]["weight"] == 0.6
    assert payload["negative_constraints"][0]["id"] == CONSTRAINT_SOFT_DELETE
    # 百分比一并写进制品，下游可视化工具不必重新实现一遍加权算法（重实现就会漂移）。
    assert payload["combinatorial_coverage"]["weighted_coverage_ratio"] == pytest.approx(
        tree.weighted_coverage()
    )
    csv_path = json_path.with_suffix(".csv")
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    assert csv_text.splitlines()[0].startswith("kind,id,description")
    # 组合对也各占一行，否则 CSV 读者会以为组合覆盖这件事不存在。
    assert f"combinatorial_pair,{CAP_CSV}|{CAP_XLSX}" in csv_text


async def test_制品写盘失败不阻断评测但必须进报告() -> None:
    pipeline, _ = _pipeline(
        # 指向一个已存在的**文件**，mkdir 必然失败。
        settings=CoverageSettings(artifacts_dir="pyproject.toml")
    )

    result = await pipeline.generate_traceability_artifact(_state())

    assert KEY_ARTIFACT_PATH not in result
    assert result[KEY_ARTIFACT_FAILURE]


def test_制品的csv把一对多关系摊平成分号分隔() -> None:
    tree = _tree(_node(CAP_CSV, "读 CSV", tier=CapabilityTier.P0_CORE))
    tree.nodes[0].covered = True
    tree.nodes[0].covering_case_ids = ["c1", "c2"]
    rows = flatten_for_csv(build_traceability_matrix(tree))

    # 用分号而不是逗号：逗号是 CSV 的字段分隔符，而 covering_cases 恰恰是人最常
    # 复制粘贴的一列。
    assert rows[0]["covering_cases"] == "c1;c2"
    assert rows[0]["covered"] == "true"


# --------------------------------------------------------------------------- #
# 8. finalize_weighted_coverage_report
# --------------------------------------------------------------------------- #


async def test_报告恒不阻断且直接取判定结论() -> None:
    pipeline, parts = _pipeline()
    state = _state(
        **{
            KEY_RATIO: 0.95,
            KEY_NODE_COUNT: 3,
            KEY_TIER_GRADED: True,
            KEY_VERDICT_STATUS: JudgeVerdictStatus.PASS.value,
            KEY_TIER_DISTRIBUTION: {"p0_core": 1, "p1_conditional": 1, "p2_defensive": 1},
            KEY_CONSTRAINT_COUNT: 0,
            KEY_ARTIFACT_PATH: "artifacts/run/traceability_matrix.json",
        }
    )

    await pipeline.finalize_weighted_coverage_report(state)

    recorded = parts["reporter"].recorded[0]
    assert recorded["dimension"] == DIMENSION
    assert recorded["status"] is JudgeVerdictStatus.PASS
    assert recorded["score"] == pytest.approx(0.95)
    # 覆盖率类维度一律不阻断合并：让测试基础设施的不完善拖垮一次正常合并，
    # 最终结果是所有人都学会绕过这条门禁。
    assert recorded["blocking"] is False


async def test_判定为不达标时如实记FAIL但仍不阻断() -> None:
    pipeline, parts = _pipeline()
    state = _state(
        **{
            KEY_RATIO: 0.3,
            KEY_NODE_COUNT: 3,
            KEY_VERDICT_STATUS: JudgeVerdictStatus.FAIL.value,
        }
    )

    await pipeline.finalize_weighted_coverage_report(state)

    assert parts["reporter"].recorded[0]["status"] is JudgeVerdictStatus.FAIL
    assert parts["reporter"].recorded[0]["blocking"] is False


async def test_私有键被裁掉时判需人工复核而不是满分通过() -> None:
    pipeline, parts = _pipeline()

    await pipeline.finalize_weighted_coverage_report(_state())

    recorded = parts["reporter"].recorded[0]
    assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
    assert any(KEY_RATIO in f for f in recorded["findings"])


async def test_空能力树不按零分处理而是交人工() -> None:
    """空树时 `weighted_coverage()` 给 0.0，但那是"没抽出能力"不是"一项都没测到"。"""
    pipeline, parts = _pipeline()
    state = _state(**{KEY_RATIO: 0.0, KEY_NODE_COUNT: 0})

    await pipeline.finalize_weighted_coverage_report(state)

    recorded = parts["reporter"].recorded[0]
    assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
    assert any("能力树为空" in f for f in recorded["findings"])


async def test_报告点名未覆盖与未判定的约束并区分两者() -> None:
    pipeline, parts = _pipeline()
    state = _state(
        **{
            KEY_RATIO: 0.9,
            KEY_NODE_COUNT: 3,
            KEY_VERDICT_STATUS: JudgeVerdictStatus.PASS.value,
            KEY_CONSTRAINT_COUNT: 3,
            KEY_CONSTRAINT_RATIO: 1 / 3,
            KEY_UNCOVERED_CONSTRAINT_IDS: [CONSTRAINT_SOFT_DELETE],
            KEY_UNDETERMINED_CONSTRAINT_IDS: [CONSTRAINT_NO_FULL_SORT],
            KEY_PROBE_BUDGET_EXHAUSTED: True,
            KEY_PROBE_CALL_COUNT: 200,
        }
    )

    await pipeline.finalize_weighted_coverage_report(state)

    findings = parts["reporter"].recorded[0]["findings"]
    assert any(CONSTRAINT_SOFT_DELETE in f and "未覆盖负向约束 id" in f for f in findings)
    assert any(CONSTRAINT_NO_FULL_SORT in f and "未判定" in f for f in findings)
    assert any("上限" in f for f in findings)


async def test_分级退化为单一档位时报告要说出来() -> None:
    pipeline, parts = _pipeline()
    state = _state(
        **{
            KEY_RATIO: 1.0,
            KEY_NODE_COUNT: 3,
            KEY_TIER_GRADED: False,
            KEY_VERDICT_STATUS: JudgeVerdictStatus.PASS.value,
        }
    )

    await pipeline.finalize_weighted_coverage_report(state)

    findings = parts["reporter"].recorded[0]["findings"]
    assert any("退化为等权" in f for f in findings)


async def test_制品路径缺失时报告要点名而不是默默略过() -> None:
    pipeline, parts = _pipeline()
    state = _state(
        **{KEY_RATIO: 1.0, KEY_NODE_COUNT: 1, KEY_VERDICT_STATUS: JudgeVerdictStatus.PASS.value}
    )

    await pipeline.finalize_weighted_coverage_report(state)

    assert any(KEY_ARTIFACT_PATH in f for f in parts["reporter"].recorded[0]["findings"])


# --------------------------------------------------------------------------- #
# 9. 图结构
# --------------------------------------------------------------------------- #


def test_子图无环且分支只通向声明过的两个去处() -> None:
    graph = build_weighted_coverage_subgraph().compile()
    drawn = graph.get_graph()

    assert ENTRY_NODE in drawn.nodes
    assert TERMINAL_NODE in drawn.nodes
    edges = {(e.source, e.target) for e in drawn.edges}
    # 补题之后**不回**映射节点：回环的代价是又一轮"约束数 × 用例数"次裁判调用。
    assert (
        NODE_NAMES["constraint_feedback_generation"],
        NODE_NAMES["map_negative_constraint_coverage"],
    ) not in edges
    assert (
        NODE_NAMES["constraint_feedback_generation"],
        NODE_NAMES["recompute_weighted_coverage"],
    ) in edges
    branch_targets = {
        target
        for source, target in edges
        if source == NODE_NAMES["map_negative_constraint_coverage"]
    }
    assert branch_targets == {
        NODE_NAMES["constraint_feedback_generation"],
        NODE_NAMES["recompute_weighted_coverage"],
    }
