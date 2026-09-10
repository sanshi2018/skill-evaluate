"""docs/dev/17：模块七——用例集瘦身与动态演进。

覆盖：冗余聚类的口径（能力路径完全相等、只看正向、未映射的不参与）、代表性打分的
五档优先级与**幂等性**（重跑不会整簇冷掉、不重复计数）、组合矩阵的三处口径
（COLD 不计入、按权重排序后截断、只统计仍在树上的 id）、组合覆盖率的分母、
组合缺口补题的单轮截断与 `descriptions` 必填、`GenerationError` 降级、孤儿检测的
"全部消失才算孤儿"与非阻塞建议队列（不挂起）、报告恒不阻断且缺键降级为
NEEDS_HUMAN_REVIEW，以及图结构（无环、分支只通向声明过的两个去处）。
全部用替身注入，不碰数据库、不发真实请求。
"""

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from skill_evaluate.agents.analyzer.identity import build_capability_id, build_capability_tree_id
from skill_evaluate.agents.analyzer.service import PLACEHOLDER_TIER
from skill_evaluate.agents.generator.schema import CapabilityFocus
from skill_evaluate.config import CoverageSettings
from skill_evaluate.errors import GenerationError, PersistenceError
from skill_evaluate.nodes.coverage import NODE_NAMES as COVERAGE_NODE_NAMES
from skill_evaluate.nodes.coverage.deps import CoverageDeps
from skill_evaluate.nodes.pruning import (
    DIMENSION,
    ENTRY_NODE,
    INTERRUPT_BEFORE_NODES,
    KEY_ANALYZED_PAIR_COUNT,
    KEY_CLUSTER_COUNT,
    KEY_DEMOTED_CASE_IDS,
    KEY_DEMOTED_VALIDATION_COUNT,
    KEY_MATRIX_TIER_RANKED,
    KEY_MATRIX_TRUNCATED,
    KEY_NEW_SUGGESTION_COUNT,
    KEY_ORPHAN_CASE_IDS,
    KEY_PAIR_COVERAGE_RATIO,
    KEY_PATCHED_PAIR_COUNT,
    KEY_PATCH_FAILURE,
    KEY_TOTAL_PAIR_COUNT,
    KEY_UNCOVERED_PAIRS,
    NODE_NAMES,
    TERMINAL_NODE,
    PruningDeps,
    PruningPipeline,
    build_pruning_subgraph,
)
from skill_evaluate.nodes.pruning.deps import TRIGGERED_BY_COMBINATORIAL_GAP
from skill_evaluate.state.capability import CapabilityNode, CapabilityTree
from skill_evaluate.state.enums import (
    CapabilityTier,
    DatasetSplit,
    JudgeVerdictStatus,
    SuggestionStatus,
    SuggestionType,
    TestCaseCategory,
)
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.suggestion import TestCaseSuggestion
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion

SKILL_ID = "csv-cleaner"
RUN_ID = "run-prune-1"
BASE_REF = "v1"
SUITE_ID = "suite-1"

CAP_CSV = build_capability_id(SKILL_ID, "支持读取 CSV 文件")
CAP_XLSX = build_capability_id(SKILL_ID, "支持读取 Excel 文件")
CAP_PIVOT = build_capability_id(SKILL_ID, "支持输出数据透视表")
CAP_GONE = build_capability_id(SKILL_ID, "支持读取 Access 数据库")


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def _skill() -> SkillDefinition:
    return SkillDefinition(
        skill_id=SKILL_ID,
        version_ref=BASE_REF,
        root_path=".",
        description="清洗并校验 CSV / Excel 导出文件",
        body_markdown="# CSV Cleaner\n",
        line_count=2,
        token_count=20,
    )


def _node(
    capability_id: str, description: str, *, tier: CapabilityTier = PLACEHOLDER_TIER
) -> CapabilityNode:
    return CapabilityNode(
        capability_id=capability_id, skill_id=SKILL_ID, description=description, tier=tier
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
    split: DatasetSplit = DatasetSplit.TRAIN,
    prompt: str | None = None,
    category: TestCaseCategory = TestCaseCategory.POSITIVE,
) -> TestCase:
    return TestCase(
        case_id=case_id,
        skill_id=SKILL_ID,
        category=category,
        split=split,
        prompt=prompt if prompt is not None else f"帮我把这份导出理一下（{case_id}）",
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


def _pairs(result: dict[str, object]) -> set[frozenset[str]]:
    raw = cast("list[list[str]]", result[KEY_UNCOVERED_PAIRS])
    return {frozenset(pair) for pair in raw}


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
    """替代 `TestCaseRepository`。

    `save()` 就地覆盖 `self.cases` 里的同 id 用例，模拟真实仓储的 upsert——节点的
    幂等性测试要靠"再跑一遍时读到的是上一轮写回去的 split"才测得出来。
    """

    def __init__(self, cases: list[TestCase]) -> None:
        self.cases = cases
        self.saved: list[TestCase] = []

    async def list_by_categories(
        self, suite_version_id: str, categories: list[TestCaseCategory]
    ) -> list[TestCase]:
        return [c.model_copy(deep=True) for c in self.cases if c.category in categories]

    async def save(self, case: TestCase) -> None:
        self.saved.append(case.model_copy(deep=True))
        for index, existing in enumerate(self.cases):
            if existing.case_id == case.case_id:
                self.cases[index] = case.model_copy(deep=True)
                return
        self.cases.append(case.model_copy(deep=True))


class FakeSuggestionRepo:
    """替代 `TestCaseSuggestionRepository`，含 `(case_id, type)` 唯一去重语义。"""

    def __init__(self) -> None:
        self.rows: list[TestCaseSuggestion] = []
        self.attempts: list[TestCaseSuggestion] = []

    async def save_if_absent(self, suggestion: TestCaseSuggestion) -> bool:
        self.attempts.append(suggestion)
        key = (suggestion.case_id, suggestion.suggestion_type)
        if any((r.case_id, r.suggestion_type) == key for r in self.rows):
            return False
        self.rows.append(suggestion)
        return True


class FakeReporter:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record_dimension_result(self, **kwargs: Any) -> None:
        self.recorded.append(kwargs)


class FakeSuiteService:
    """替代 `TestSuiteService`：只记录 `incremental_patch()` 的入参。"""

    def __init__(self, *, error: GenerationError | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def incremental_patch(
        self, skill: SkillDefinition, focus: CapabilityFocus, triggered_by: str
    ) -> TestSuiteVersion:
        self.calls.append({"skill": skill, "focus": focus, "triggered_by": triggered_by})
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
    cases: list[TestCase] | None = None,
    tree: CapabilityTree | None = None,
    suite_service: FakeSuiteService | None = None,
    skill: SkillDefinition | None = None,
    settings: CoverageSettings | None = None,
) -> tuple[PruningPipeline, dict[str, Any]]:
    """装一条全替身的流水线，并把替身一并交回给测试做断言。"""
    parts: dict[str, Any] = {
        "skill_repo": FakeSkillRepo(skill if skill is not None else _skill()),
        "case_repo": FakeCaseRepo(cases if cases is not None else []),
        "capability_repo": FakeCapabilityRepo(tree if tree is not None else _default_tree()),
        "suggestion_repo": FakeSuggestionRepo(),
        "reporter": FakeReporter(),
        "suite_service": suite_service or FakeSuiteService(),
    }
    deps = PruningDeps(
        suite_service=cast("Any", parts["suite_service"]),
        report_generator=cast("Any", parts["reporter"]),
        skill_repository=cast("Any", parts["skill_repo"]),
        test_case_repository=cast("Any", parts["case_repo"]),
        capability_repository=cast("Any", parts["capability_repo"]),
        suggestion_repository=cast("Any", parts["suggestion_repo"]),
        coverage_settings=settings or CoverageSettings(),
    )
    return PruningPipeline(deps), parts


# --------------------------------------------------------------------------- #
# 0. 命名与装配约定（docs/dev/interfaces/16 第 1/4 节）
# --------------------------------------------------------------------------- #


def test_维度名与模块六不同否则报告会被整行覆盖() -> None:
    # `dimension_results` 的唯一约束是 (run_id, dimension)：两份文档写同一个维度名
    # 时，后跑完的会把先跑完的整行覆盖掉，且不会有任何报错。
    from skill_evaluate.nodes.coverage import DIMENSION as COVERAGE_DIMENSION

    assert DIMENSION == "test_suite_health"
    assert DIMENSION != COVERAGE_DIMENSION


def test_节点名沿用coverage前缀但不与模块六重名() -> None:
    # 同一个图分区（前缀相同）……
    assert all(name.startswith("coverage.") for name in NODE_NAMES.values())
    # ……但同一张图里节点名必须唯一。尤其是收尾节点：docs/dev/17 原文也叫
    # finalize_dimension_report，与模块六撞名。
    assert not set(NODE_NAMES.values()) & set(COVERAGE_NODE_NAMES.values())
    assert TERMINAL_NODE == "coverage.finalize_pruning_report"


def test_本维度没有挂起点() -> None:
    # 孤儿用例走的是非阻塞建议队列，不是 suspend_and_wait()。显式导出空列表，
    # 让 docs/dev/24 汇总时"没有挂起点"与"忘了写"分得开。
    assert INTERRUPT_BEFORE_NODES == []


def test_deps_from_coverage复用模块六已构造的实例() -> None:
    coverage_deps = CoverageDeps()
    analyzer = coverage_deps.analyzer()  # 触发惰性构造
    pruning_deps = PruningDeps.from_coverage(coverage_deps)

    # 共享的是实例本身而不是"各自再造一个"——否则 Langfuse 上会出现两条彼此无关的
    # Agent 调用线，而它们本该是同一次评测里的同一个分析基座。
    assert pruning_deps.analyzer() is analyzer
    assert pruning_deps.skill_repository is coverage_deps.skill_repository
    # 已经是 PruningDeps 时原样返回，不会悄悄换掉建议队列仓储。
    assert PruningDeps.from_coverage(pruning_deps) is pruning_deps


# --------------------------------------------------------------------------- #
# 1. redundant_case_pruning（docs/dev/17 第 4 节）
# --------------------------------------------------------------------------- #


async def test_能力路径完全相同的簇只留一条代表其余降级cold() -> None:
    cases = [
        _case("c1", capability_ids=[CAP_CSV, CAP_XLSX], prompt="短"),
        _case("c2", capability_ids=[CAP_XLSX, CAP_CSV], prompt="这是一条包含更复杂边界条件的长用例" * 3),
        _case("c3", capability_ids=[CAP_CSV, CAP_XLSX], prompt="也短"),
        _case("c4", capability_ids=[CAP_PIVOT], prompt="独苗，不该被动"),
    ]
    pipeline, parts = _pipeline(cases=cases)

    result = await pipeline.redundant_case_pruning(_state())

    # 顺序无关：frozenset 相等即同簇，c1/c2/c3 是一簇，c4 自成一簇（成员数 1，不折叠）。
    assert result[KEY_CLUSTER_COUNT] == 1
    assert sorted(cast("list[str]", result[KEY_DEMOTED_CASE_IDS])) == ["c1", "c3"]
    by_id = {c.case_id: c for c in parts["case_repo"].cases}
    assert by_id["c2"].split is DatasetSplit.TRAIN  # prompt 最长 → 最具代表性
    assert by_id["c1"].split is DatasetSplit.COLD
    assert by_id["c3"].split is DatasetSplit.COLD
    assert by_id["c4"].split is DatasetSplit.TRAIN


async def test_降级只改split不删除任何用例() -> None:
    # 架构文档模块七"应对方案"：硬性删除必须保留人类最终 Review 权限。
    cases = [
        _case("c1", capability_ids=[CAP_CSV], prompt="短"),
        _case("c2", capability_ids=[CAP_CSV], prompt="长一些的用例内容" * 5),
    ]
    pipeline, parts = _pipeline(cases=cases)

    await pipeline.redundant_case_pruning(_state())

    assert len(parts["case_repo"].cases) == 2  # 一条都没少
    assert {c.split for c in parts["case_repo"].cases} == {
        DatasetSplit.TRAIN,
        DatasetSplit.COLD,
    }


async def test_未映射与非正向用例不参与聚类() -> None:
    cases = [
        # 未映射：可能是刚补出来还没轮到映射的新题，把它们聚成一个"空能力路径"簇
        # 会导致一批不相干的题被互相认作冗余。
        _case("no-map-1", capability_ids=[]),
        _case("no-map-2", capability_ids=[]),
        # 非正向：`target_capability_ids` 只有正向用例才有。
        _case("adv", capability_ids=[CAP_CSV], category=TestCaseCategory.ADVERSARIAL),
        _case("pos", capability_ids=[CAP_CSV]),
    ]
    pipeline, parts = _pipeline(cases=cases)

    result = await pipeline.redundant_case_pruning(_state())

    assert result[KEY_DEMOTED_CASE_IDS] == []
    assert result[KEY_CLUSTER_COUNT] == 0
    assert parts["case_repo"].saved == []


async def test_重跑不会把整簇都降级也不重复计数() -> None:
    """幂等性：本节点会在每次评测中重跑，第二轮必须什么都不做。"""
    cases = [
        _case("c1", capability_ids=[CAP_CSV], prompt="短"),
        _case("c2", capability_ids=[CAP_CSV], prompt="长得多的用例内容" * 5),
    ]
    pipeline, parts = _pipeline(cases=cases)

    first = await pipeline.redundant_case_pruning(_state())
    saves_after_first = len(parts["case_repo"].saved)
    second = await pipeline.redundant_case_pruning(_state())

    assert first[KEY_DEMOTED_CASE_IDS] == ["c1"]
    # 第二轮：c1 已是 COLD，不重复落库、不重复计数——否则报告里"折叠 12 条"会在
    # 连续每一次评测中重复出现同样的 12 条。
    assert second[KEY_DEMOTED_CASE_IDS] == []
    assert len(parts["case_repo"].saved) == saves_after_first
    by_id = {c.case_id: c for c in parts["case_repo"].cases}
    assert by_id["c2"].split is DatasetSplit.TRAIN  # 代表没有被反过来降级


async def test_更长的冷用例不会把活跃代表挤下去() -> None:
    """代表性打分的第一档：活跃优先于长度。

    缺这一档时，第二轮会选中更长的那条 COLD 用例做代表，于是上一轮留下的活跃用例
    也被降级——整簇冷掉，这条能力路径从此没有任何活跃用例覆盖。
    """
    cases = [
        _case("cold-long", capability_ids=[CAP_CSV], split=DatasetSplit.COLD, prompt="很长" * 50),
        _case("warm-short", capability_ids=[CAP_CSV], prompt="短"),
    ]
    pipeline, parts = _pipeline(cases=cases)

    result = await pipeline.redundant_case_pruning(_state())

    assert result[KEY_DEMOTED_CASE_IDS] == []
    by_id = {c.case_id: c for c in parts["case_repo"].cases}
    assert by_id["warm-short"].split is DatasetSplit.TRAIN


async def test_整簇都已冷掉时把代表恢复为训练集() -> None:
    # 能力映射变化导致两个旧簇合并时可能出现。一条能力路径若一条活跃用例都不剩，
    # 等于这条路径被静默地不再测了。
    cases = [
        _case("c1", capability_ids=[CAP_CSV], split=DatasetSplit.COLD, prompt="短"),
        _case("c2", capability_ids=[CAP_CSV], split=DatasetSplit.COLD, prompt="更长的内容" * 5),
    ]
    pipeline, parts = _pipeline(cases=cases)

    await pipeline.redundant_case_pruning(_state())

    by_id = {c.case_id: c for c in parts["case_repo"].cases}
    assert by_id["c2"].split is DatasetSplit.TRAIN  # 恢复
    assert by_id["c1"].split is DatasetSplit.COLD  # 其余保持


async def test_长度相当时优先保留训练集用例() -> None:
    # docs/dev/17 第 4.1 节："若长度相近（差异 < 20%），优先保留 split=TRAIN"。
    cases = [
        _case("val", capability_ids=[CAP_CSV], split=DatasetSplit.VALIDATION, prompt="一二三四五六七八九十"),
        _case("train", capability_ids=[CAP_CSV], split=DatasetSplit.TRAIN, prompt="一二三四五六七八九"),
    ]
    pipeline, parts = _pipeline(cases=cases)

    result = await pipeline.redundant_case_pruning(_state())

    assert result[KEY_DEMOTED_CASE_IDS] == ["val"]
    # 被降级的验证集用例单独计数：验证集是优化闭环判断补丁是否真的改好的依据。
    assert result[KEY_DEMOTED_VALIDATION_COUNT] == 1
    by_id = {c.case_id: c for c in parts["case_repo"].cases}
    assert by_id["train"].split is DatasetSplit.TRAIN


async def test_代表选择在用例顺序变化时保持确定() -> None:
    # 仓储返回顺序由数据库决定；没有 case_id 这一档兜底时，同一批数据在两次运行中
    # 可能选出不同的代表，于是每次评测都会降级一批不同的用例。
    same_prompt = "完全一样的一条用例"
    ids = ["a", "b", "c"]
    demoted_sets = []
    for order in ([0, 1, 2], [2, 0, 1], [1, 2, 0]):
        cases = [_case(ids[i], capability_ids=[CAP_CSV], prompt=same_prompt) for i in order]
        pipeline, _ = _pipeline(cases=cases)
        result = await pipeline.redundant_case_pruning(_state())
        demoted_sets.append(sorted(cast("list[str]", result[KEY_DEMOTED_CASE_IDS])))

    assert demoted_sets[0] == demoted_sets[1] == demoted_sets[2] == ["a", "b"]


async def test_缺少用例集版本号时抛错而不是当成没有冗余() -> None:
    pipeline, _ = _pipeline(cases=[])
    with pytest.raises(PersistenceError, match="active_suite_version_id"):
        await pipeline.redundant_case_pruning(_state(active_suite_version_id=None))


# --------------------------------------------------------------------------- #
# 2. combinatorial_matrix_analysis（docs/dev/17 第 5 节）
# --------------------------------------------------------------------------- #


async def test_组合矩阵检出未被同一条用例同时触发的能力对() -> None:
    cases = [_case("c1", capability_ids=[CAP_CSV, CAP_XLSX])]
    pipeline, parts = _pipeline(cases=cases)

    result = await pipeline.combinatorial_matrix_analysis(_state())

    # 三个能力 → 3 对；c1 覆盖了 (CSV, XLSX)，剩下两对未覆盖。
    assert result[KEY_TOTAL_PAIR_COUNT] == 3
    assert result[KEY_ANALYZED_PAIR_COUNT] == 3
    assert _pairs(result) == {
        frozenset({CAP_CSV, CAP_PIVOT}),
        frozenset({CAP_XLSX, CAP_PIVOT}),
    }
    assert result[KEY_PAIR_COVERAGE_RATIO] == pytest.approx(1 / 3)
    # 已覆盖组合对落回能力树，供文档 18 与 docs/dev/22 直接读。
    assert parts["capability_repo"].tree is not None
    assert parts["capability_repo"].tree.combinatorial_pairs_covered == [
        tuple(sorted((CAP_CSV, CAP_XLSX)))
    ]


async def test_已降级用例不计入组合覆盖() -> None:
    # 否则会出现悖论：刚被折叠掉的冗余用例仍在为组合覆盖率贡献分子，"瘦身"看不出
    # 任何代价，而那条题以后只在 Nightly 里跑。
    cases = [_case("cold", capability_ids=[CAP_CSV, CAP_XLSX], split=DatasetSplit.COLD)]
    pipeline, parts = _pipeline(cases=cases)

    result = await pipeline.combinatorial_matrix_analysis(_state())

    assert len(_pairs(result)) == 3
    assert result[KEY_PAIR_COVERAGE_RATIO] == 0.0
    assert parts["capability_repo"].tree is not None
    assert parts["capability_repo"].tree.combinatorial_pairs_covered == []


async def test_指向已消失能力的旧绑定不进组合统计() -> None:
    # 否则库里会留下一批指向不存在节点的组合对。
    cases = [_case("c1", capability_ids=[CAP_CSV, CAP_GONE])]
    pipeline, parts = _pipeline(cases=cases)

    result = await pipeline.combinatorial_matrix_analysis(_state())

    assert result[KEY_PAIR_COVERAGE_RATIO] == 0.0
    assert parts["capability_repo"].tree is not None
    assert parts["capability_repo"].tree.combinatorial_pairs_covered == []


async def test_组合数超限时截断且如实标注未做优先级筛选() -> None:
    tree = _tree(*(_node(f"cap-{i}", f"能力 {i}") for i in range(10)))  # 45 对
    pipeline, _ = _pipeline(tree=tree, settings=CoverageSettings(max_capability_pairs_for_matrix=5))

    result = await pipeline.combinatorial_matrix_analysis(_state())

    assert result[KEY_TOTAL_PAIR_COUNT] == 45
    assert result[KEY_ANALYZED_PAIR_COUNT] == 5
    assert result[KEY_MATRIX_TRUNCATED] is True
    # 所有 tier 都还是占位值 → 排序退化为按 id 排，报告要标注"未做优先级筛选"。
    assert result[KEY_MATRIX_TIER_RANKED] is False


async def test_权重分级落地后截断优先分析P0组合() -> None:
    # 文档 18 接入后的形态：tier 有意义时，P0×P0 排在最前。
    tree = _tree(
        _node("p2", "防御能力", tier=CapabilityTier.P2_DEFENSIVE),
        _node("p0a", "核心能力 A", tier=CapabilityTier.P0_CORE),
        _node("p0b", "核心能力 B", tier=CapabilityTier.P0_CORE),
        _node("p1", "条件能力", tier=CapabilityTier.P1_CONDITIONAL),
    )
    pipeline, _ = _pipeline(tree=tree, settings=CoverageSettings(max_capability_pairs_for_matrix=1))

    result = await pipeline.combinatorial_matrix_analysis(_state())

    assert result[KEY_MATRIX_TIER_RANKED] is True
    assert _pairs(result) == {frozenset({"p0a", "p0b"})}


async def test_组合覆盖率的分母是分析范围而不是全量() -> None:
    # 截断之后拿全量做分母，得到的数字既不是"分析范围的覆盖情况"也不是"全量的
    # 覆盖情况"，谁也解释不了。
    tree = _tree(*(_node(f"cap-{i}", f"能力 {i}") for i in range(5)))  # 10 对
    cases = [_case("c1", capability_ids=["cap-0", "cap-1"])]
    pipeline, _ = _pipeline(
        cases=cases, tree=tree, settings=CoverageSettings(max_capability_pairs_for_matrix=2)
    )

    result = await pipeline.combinatorial_matrix_analysis(_state())

    assert result[KEY_ANALYZED_PAIR_COUNT] == 2
    assert result[KEY_PAIR_COVERAGE_RATIO] == pytest.approx(0.5)


async def test_能力树只有一个节点时没有组合可分析() -> None:
    pipeline, _ = _pipeline(tree=_tree(_node(CAP_CSV, "支持读取 CSV 文件")))

    result = await pipeline.combinatorial_matrix_analysis(_state())

    assert result[KEY_TOTAL_PAIR_COUNT] == 0
    # 空范围取 1.0：没有组合可测不是"组合覆盖率为 0"。报告里靠组合对总数区分。
    assert result[KEY_PAIR_COVERAGE_RATIO] == 1.0


async def test_模块七必须排在模块六之后否则立刻失败() -> None:
    pipeline, _ = _pipeline()
    with pytest.raises(PersistenceError, match="capability_tree_id"):
        await pipeline.combinatorial_matrix_analysis(_state(capability_tree_id=None))


# --------------------------------------------------------------------------- #
# 3. combinatorial_feedback_generation（docs/dev/17 第 5 节）
# --------------------------------------------------------------------------- #


async def test_组合缺口补题带上人类可读描述并单轮截断() -> None:
    uncovered = [[CAP_CSV, CAP_PIVOT], [CAP_XLSX, CAP_PIVOT], [CAP_CSV, CAP_XLSX]]
    pipeline, parts = _pipeline(settings=CoverageSettings(max_combinatorial_patch_per_round=2))

    result = await pipeline.combinatorial_feedback_generation(
        _state(**{KEY_UNCOVERED_PAIRS: uncovered})
    )

    call = parts["suite_service"].calls[0]
    focus: CapabilityFocus = call["focus"]
    # 单轮截断：默认 5，这里配成 2。
    assert len(focus.combinatorial_pairs) == 2
    assert result[KEY_PATCHED_PAIR_COUNT] == 2
    # descriptions 必填——capability_id 是描述文本的哈希，裸 id 对模型没有信息量。
    assert focus.describe(CAP_CSV) == "支持读取 CSV 文件"
    assert focus.describe(CAP_PIVOT) == "支持输出数据透视表"
    # 审计字段与模块六的 coverage_gap 分开：两者是不同的补题原因。
    assert call["triggered_by"] == TRIGGERED_BY_COMBINATORIAL_GAP
    # 新版本写回公共字段，后续维度看到补过缺口的用例集。
    assert result["active_suite_version_id"] == "suite-patch-1"


async def test_补题失败只降级不掀掉流水线() -> None:
    pipeline, parts = _pipeline(
        suite_service=FakeSuiteService(error=GenerationError("还没有任何 active 测试集版本"))
    )

    result = await pipeline.combinatorial_feedback_generation(
        _state(**{KEY_UNCOVERED_PAIRS: [[CAP_CSV, CAP_PIVOT]]})
    )

    assert "active" in str(result[KEY_PATCH_FAILURE])
    assert KEY_PATCHED_PAIR_COUNT not in result
    assert parts["suite_service"].calls  # 确实试过了


def test_没有缺口或预算为零时不进补题节点() -> None:
    pipeline, _ = _pipeline()
    assert pipeline.route_after_matrix(_state(**{KEY_UNCOVERED_PAIRS: []})) == (
        NODE_NAMES["orphan_case_detection"]
    )
    assert pipeline.route_after_matrix(
        _state(**{KEY_UNCOVERED_PAIRS: [[CAP_CSV, CAP_PIVOT]]})
    ) == NODE_NAMES["combinatorial_feedback_generation"]

    # 预算调成 0 的语义是"只分析、不自动补题"，路由必须尊重它。
    zero_budget, _ = _pipeline(settings=CoverageSettings(max_combinatorial_patch_per_round=0))
    assert zero_budget.route_after_matrix(
        _state(**{KEY_UNCOVERED_PAIRS: [[CAP_CSV, CAP_PIVOT]]})
    ) == NODE_NAMES["orphan_case_detection"]


# --------------------------------------------------------------------------- #
# 4. orphan_case_detection（docs/dev/17 第 6 节）
# --------------------------------------------------------------------------- #


async def test_全部绑定能力消失才算孤儿() -> None:
    cases = [
        _case("orphan", capability_ids=[CAP_GONE]),
        # 三项里改写了一项描述 → 一个新 id，但这条题仍然测得到另外两项能力，
        # 淘汰它是纯粹的损失。
        _case("partial", capability_ids=[CAP_CSV, CAP_GONE]),
        _case("healthy", capability_ids=[CAP_CSV]),
    ]
    pipeline, parts = _pipeline(cases=cases)

    result = await pipeline.orphan_case_detection(_state())

    assert result[KEY_ORPHAN_CASE_IDS] == ["orphan"]
    assert result[KEY_NEW_SUGGESTION_COUNT] == 1
    row = parts["suggestion_repo"].rows[0]
    assert row.suggestion_type is SuggestionType.ORPHAN_RETIREMENT
    assert row.status is SuggestionStatus.PENDING
    # 理由里要能看出"哪几项能力消失了"和"这条题在测什么"，人才判断得了是淘汰
    # 还是重新绑定。
    assert CAP_GONE in row.reason
    assert "orphan" in row.reason


async def test_孤儿检测不挂起流水线() -> None:
    # 与模块六"能力树粒度确认"的阻塞式挂起形成对照：孤儿用例不影响本次运行任何
    # 结论的正确性，只需要人找时间清理。
    pipeline, _ = _pipeline(cases=[_case("orphan", capability_ids=[CAP_GONE])])
    result = await pipeline.orphan_case_detection(_state())  # 不抛 PipelineSuspended
    assert result[KEY_ORPHAN_CASE_IDS] == ["orphan"]


async def test_同一条孤儿用例只产生一条待办() -> None:
    cases = [_case("orphan", capability_ids=[CAP_GONE])]
    pipeline, parts = _pipeline(cases=cases)

    first = await pipeline.orphan_case_detection(_state())
    second = await pipeline.orphan_case_detection(_state())

    # 检出条数每轮都是 1（它确实还是孤儿），但待办只在第一次产生。
    assert first[KEY_ORPHAN_CASE_IDS] == second[KEY_ORPHAN_CASE_IDS] == ["orphan"]
    assert first[KEY_NEW_SUGGESTION_COUNT] == 1
    assert second[KEY_NEW_SUGGESTION_COUNT] == 0
    assert len(parts["suggestion_repo"].rows) == 1


async def test_孤儿用例不会被自动删除或降级() -> None:
    # 淘汰动作属于 docs/dev/22 的审查工作台，本模块只写建议。
    cases = [_case("orphan", capability_ids=[CAP_GONE])]
    pipeline, parts = _pipeline(cases=cases)

    await pipeline.orphan_case_detection(_state())

    assert parts["case_repo"].saved == []
    assert parts["case_repo"].cases[0].split is DatasetSplit.TRAIN


# --------------------------------------------------------------------------- #
# 5. finalize_pruning_report（docs/dev/17 第 7 节）
# --------------------------------------------------------------------------- #


async def test_报告恒不阻断且不产出Fail() -> None:
    pipeline, parts = _pipeline()

    await pipeline.finalize_pruning_report(
        _state(
            **{
                KEY_DEMOTED_CASE_IDS: ["c1", "c3"],
                KEY_CLUSTER_COUNT: 1,
                KEY_UNCOVERED_PAIRS: [[CAP_CSV, CAP_PIVOT]],
                KEY_ANALYZED_PAIR_COUNT: 3,
                KEY_TOTAL_PAIR_COUNT: 3,
                KEY_PAIR_COVERAGE_RATIO: 2 / 3,
                KEY_ORPHAN_CASE_IDS: ["orphan"],
                KEY_NEW_SUGGESTION_COUNT: 1,
            }
        )
    )

    recorded = parts["reporter"].recorded[0]
    assert recorded["dimension"] == DIMENSION
    # 存在孤儿、存在组合缺口，仍然 PASS：本维度是健康度建议而不是质量门禁。
    assert recorded["status"] is JudgeVerdictStatus.PASS
    assert recorded["blocking"] is False
    assert recorded["score"] == pytest.approx(2 / 3)
    joined = "\n".join(recorded["findings"])
    assert "折叠冗余用例 2 条" in joined
    assert "未覆盖 1 对" in joined
    assert "孤儿用例 1 条" in joined


async def test_私有键被裁掉时判需人工复核而不是满分通过() -> None:
    # 症状：三个分析节点照常改库（用例真的被降级、建议真的落表），只是报告里
    # 显示"折叠 0 条 / 未覆盖 0 对 / 孤儿 0 条"——一份看起来非常健康的假报告。
    pipeline, parts = _pipeline()

    await pipeline.finalize_pruning_report(_state())

    recorded = parts["reporter"].recorded[0]
    assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
    assert recorded["blocking"] is False  # 故障信号也不阻断
    assert recorded["score"] is None
    assert KEY_PAIR_COVERAGE_RATIO in recorded["findings"][0]


async def test_报告点名被降级的验证集用例与截断口径() -> None:
    pipeline, parts = _pipeline()

    await pipeline.finalize_pruning_report(
        _state(
            **{
                KEY_DEMOTED_CASE_IDS: ["v1"],
                KEY_DEMOTED_VALIDATION_COUNT: 1,
                KEY_UNCOVERED_PAIRS: [],
                KEY_ANALYZED_PAIR_COUNT: 5,
                KEY_TOTAL_PAIR_COUNT: 45,
                KEY_PAIR_COVERAGE_RATIO: 1.0,
                KEY_MATRIX_TRUNCATED: True,
                KEY_MATRIX_TIER_RANKED: False,
                KEY_ORPHAN_CASE_IDS: [],
                KEY_NEW_SUGGESTION_COUNT: 0,
            }
        )
    )

    joined = "\n".join(parts["reporter"].recorded[0]["findings"])
    assert "原属验证集" in joined
    assert "未做优先级筛选的截断分析" in joined
    assert "docs/dev/18" in joined


async def test_补题失败必须出现在报告里() -> None:
    # 否则表现为"组合缺口还在、系统却安静地不补了"。
    pipeline, parts = _pipeline()

    await pipeline.finalize_pruning_report(
        _state(
            **{
                KEY_PAIR_COVERAGE_RATIO: 0.0,
                KEY_ANALYZED_PAIR_COUNT: 3,
                KEY_UNCOVERED_PAIRS: [[CAP_CSV, CAP_PIVOT]],
                KEY_PATCH_FAILURE: "还没有任何 active 测试集版本",
            }
        )
    )

    joined = "\n".join(parts["reporter"].recorded[0]["findings"])
    assert "定向补题未能执行" in joined
    assert "active 测试集版本" in joined


# --------------------------------------------------------------------------- #
# 6. 图结构
# --------------------------------------------------------------------------- #


def test_子图无环且两条分支在孤儿检测汇合() -> None:
    pipeline_deps = PruningDeps(
        skill_repository=cast("Any", FakeSkillRepo(_skill())),
        test_case_repository=cast("Any", FakeCaseRepo([])),
        capability_repository=cast("Any", FakeCapabilityRepo()),
        suggestion_repository=cast("Any", FakeSuggestionRepo()),
        report_generator=cast("Any", FakeReporter()),
        suite_service=cast("Any", FakeSuiteService()),
    )
    graph = build_pruning_subgraph(pipeline_deps).compile()
    edges = {(e.source, e.target) for e in graph.get_graph().edges}

    assert ENTRY_NODE in {e[0] for e in edges}
    # 补题之后不回矩阵分析：模块七刻意不带环（与模块六相反）。
    assert (
        NODE_NAMES["combinatorial_feedback_generation"],
        NODE_NAMES["combinatorial_matrix_analysis"],
    ) not in edges
    assert (
        NODE_NAMES["combinatorial_feedback_generation"],
        NODE_NAMES["orphan_case_detection"],
    ) in edges
    assert (NODE_NAMES["orphan_case_detection"], TERMINAL_NODE) in edges
    # 分支只可能去声明过的两个地方。
    targets = {t for s, t in edges if s == NODE_NAMES["combinatorial_matrix_analysis"]}
    assert targets == {
        NODE_NAMES["combinatorial_feedback_generation"],
        NODE_NAMES["orphan_case_detection"],
    }
