"""对抗测试集的生命周期入口（docs/dev/15 第 2 节）。

## 这一层几乎什么都不做，这正是它存在的意义

架构文档要求 Attacker"同样支持缓存复用"。缓存复用、版本化、60/40 划分、反坍塌
校验、staleness 告警——这些 `TestSuiteService`（docs/dev/06）全都已经实现了，而且
是全项目唯一的实现。本类只是给它换一个出题智能体（`AttackerAgent`）并固定几个
参数，**不重新实现三态语义**。

如果哪天有人想在这里加一句"对抗用例每次都重新生成"，请先读 docs/dev/06 第 4.1 节：
自动重生会让"这次改动到底影响了什么"永远无法归因（旧题换新题，分数变化说明不了
任何问题），这条约束对红队用例同样成立——一份 Skill 加固之后安全分上升了，我们
必须能确定那是因为它真的挡住了**同一批**攻击。

## 相对 docs/dev/15 正文的一处实现修正

正文写的是"按 `(skill_id, category=ADVERSARIAL)` 维度管理**独立的** active 版本"。
现有实现里 `test_suite_versions` 的 active 版本是按 `(skill_id, skill_version_ref)`
唯一的，一个 Skill 只有一版 active 用例集，对抗用例作为其中的一个类别存在
（`_ensure_extra_categories()` 的机制，docs/dev/13 已经这么用了）。

按现有实现走而不是新开一条并行的版本维度，理由有两条：

1. 并行的 active 版本意味着 `runs.suite_version_id` 要变成一个列表，报告里"本次用
   的是哪一版题"就不再有唯一答案；
2. 现有机制已经满足需求——"对抗用例一条都没有时才补生成，之后一直复用"正是
   `_ensure_extra_categories()` 的口径。

docs/dev/15 正文已按此修正（见该文档第 2 节）。
"""

from __future__ import annotations

from skill_evaluate.agents.attacker.agent import AttackerAgent
from skill_evaluate.agents.attacker.playbook import allocate_counts, registered_subtypes
from skill_evaluate.agents.generator.service import EnsureTestSuiteResult, TestSuiteService
from skill_evaluate.logging import get_logger
from skill_evaluate.state.enums import TestCaseCategory
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestSuiteVersion

logger = get_logger(component="attacker_service")

# 审计字段取值（docs/dev/interfaces/06 第 2 节的约定表在此追加一项）。纯审计用途，
# 不影响生成逻辑，供排查"对抗用例集为什么变了"。
TRIGGERED_BY_ATTACKER_BOOTSTRAP = "attacker_bootstrap"

# 一次 bootstrap 出多少条对抗题的默认值。
#
# 为什么是"每个攻击面至少 2 条"而不是一个拍脑袋的总数：条数的意义完全取决于攻击面
# 的数量——七个攻击面出 10 条，等于有三个攻击面各只有一条题，那一条一旦出得不好，
# 整个攻击面这次就是空白。按攻击面定量之后，将来注册第八个攻击面时总数自动跟上，
# 不需要有人记得回来改这个常量。
CASES_PER_ATTACK_SUBTYPE = 2


def default_adversarial_count() -> int:
    """本次该出多少条对抗用例（= 已注册攻击面数 × `CASES_PER_ATTACK_SUBTYPE`）。"""
    return len(registered_subtypes()) * CASES_PER_ATTACK_SUBTYPE


class AttackerService:
    """对抗测试集的 REUSE / FORCE_REGENERATE 入口。"""

    def __init__(self, *, test_suite_service: TestSuiteService | None = None) -> None:
        # 注入点：单测传一个假的 `TestSuiteService` 就能完全不碰库、不发请求。
        # 默认构造时**必须**把 generator 换成 AttackerAgent——用默认的
        # `GeneratorAgent` 会在遇到 ADVERSARIAL 时抛 `GenerationError`（那条错误
        # 信息里点名了应该改用 AttackerAgent，见 generator 的模板注册表）。
        self._suite = test_suite_service or TestSuiteService(generator=AttackerAgent())

    async def ensure_adversarial_suite(
        self, skill: SkillDefinition, *, count: int | None = None
    ) -> EnsureTestSuiteResult:
        """默认路径：有对抗用例就复用，一条都没有才生成（REUSE 语义）。

        判定口径是"一条都没有"而不是"条数够不够"（`_ensure_extra_categories()` 的
        约定）：后者没有客观答案，做成自动触发条件等于给流水线开了个每次运行都可能
        悄悄再出一批题的口子。要加题请显式走 `force_regenerate()`。
        """
        requested = default_adversarial_count() if count is None else count
        result = await self._suite.ensure_test_suite(
            skill,
            extra_categories=[TestCaseCategory.ADVERSARIAL],
            category_counts={TestCaseCategory.ADVERSARIAL: requested},
            extra_triggered_by=TRIGGERED_BY_ATTACKER_BOOTSTRAP,
        )
        logger.info(
            "attacker_suite_ensured",
            skill_id=skill.skill_id,
            suite_version_id=result.suite_version.suite_version_id,
            requested_count=requested,
            generated=result.generated,
            stale=result.staleness_warning is not None,
            allocation={s.value: n for s, n in allocate_counts(requested).items() if n > 0},
        )
        return result

    async def force_regenerate(
        self, skill: SkillDefinition, *, count: int | None = None
    ) -> TestSuiteVersion:
        """全量重出一套对抗题。**只有人能触发**（CLI / CI 显式参数）。

        与 `TestSuiteService.force_regenerate()` 的区别：那个会连正/反向用例一起
        重出，本方法只重出对抗用例（`positive_count=negative_count=0`），因为红队
        用例的更新节奏与功能用例不同——新增一类攻击手法不该让触发准确度的历史分数
        失去可比性。

        实现上走 `INCREMENTAL_PATCH`（继承现有用例 + 追加新的一批对抗题），而不是
        真的把旧对抗用例删掉：旧版本保留为非 active，历史结论仍然可回查。
        """
        requested = default_adversarial_count() if count is None else count
        version = await self._suite.incremental_patch_categories(
            skill,
            categories=[TestCaseCategory.ADVERSARIAL],
            category_counts={TestCaseCategory.ADVERSARIAL: requested},
            triggered_by="manual_cli",
        )
        logger.info(
            "attacker_suite_regenerated",
            skill_id=skill.skill_id,
            suite_version_id=version.suite_version_id,
            requested_count=requested,
        )
        return version


__all__ = [
    "CASES_PER_ATTACK_SUBTYPE",
    "TRIGGERED_BY_ATTACKER_BOOTSTRAP",
    "AttackerService",
    "default_adversarial_count",
]
