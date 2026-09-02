"""docs/dev/02 数据契约的基础校验：happy-path 构造 + 加权覆盖率计算。"""

from datetime import UTC, datetime

from skill_evaluate.state import (
    CapabilityNode,
    CapabilityTier,
    CapabilityTree,
    DatasetSplit,
    TestCase,
    TestCaseCategory,
)


def test_test_case_happy_path() -> None:
    case = TestCase(
        case_id="c1",
        skill_id="s1",
        category=TestCaseCategory.POSITIVE,
        split=DatasetSplit.TRAIN,
        prompt="do the thing",
        generator_run_id="g1",
        created_at=datetime.now(UTC),
    )
    assert case.category == TestCaseCategory.POSITIVE
    assert case.target_capability_ids == []


def test_capability_tree_weighted_coverage_all_covered() -> None:
    tree = CapabilityTree(
        skill_id="s1",
        skill_version_ref="v1",
        nodes=[
            CapabilityNode(
                capability_id="p0",
                skill_id="s1",
                description="core",
                tier=CapabilityTier.P0_CORE,
                covered=True,
            ),
            CapabilityNode(
                capability_id="p2",
                skill_id="s1",
                description="fmt",
                tier=CapabilityTier.P2_DEFENSIVE,
                covered=True,
            ),
        ],
    )
    assert tree.weighted_coverage() == 1.0


def test_capability_tree_weighted_coverage_p0_uncovered_dominates() -> None:
    """架构文档模块八要求：即使 P2 全覆盖，P0 未覆盖也不能让整体覆盖率虚高。"""
    tree = CapabilityTree(
        skill_id="s1",
        skill_version_ref="v1",
        nodes=[
            CapabilityNode(
                capability_id="p0",
                skill_id="s1",
                description="core",
                tier=CapabilityTier.P0_CORE,
                covered=False,
            ),
            CapabilityNode(
                capability_id="p2",
                skill_id="s1",
                description="fmt",
                tier=CapabilityTier.P2_DEFENSIVE,
                covered=True,
            ),
        ],
    )
    coverage = tree.weighted_coverage()
    assert coverage == 0.1 / 0.7
    assert coverage < 0.9


def test_capability_tree_empty_nodes_is_zero() -> None:
    tree = CapabilityTree(skill_id="s1", skill_version_ref="v1")
    assert tree.weighted_coverage() == 0.0
