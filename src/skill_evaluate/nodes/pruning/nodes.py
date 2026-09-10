"""模块七：用例集瘦身与动态演进的节点实现（docs/dev/17）。

```
（承接模块六 coverage.finalize_dimension_report 之后）
redundant_case_pruning            能力路径完全相同的簇 → 留一条代表，其余降级 COLD
        ↓
combinatorial_matrix_analysis     N×N 组合矩阵 → 未覆盖的能力对
        ↓
  [存在未覆盖组合对?] --是--> combinatorial_feedback_generation（定向补题，无回边）
        ↓否                              ↓
orphan_case_detection  ←────────────────┘   绑定能力已消失 → 非阻塞建议队列
        ↓
finalize_pruning_report
```

## 四条贯穿本文件的关键决策

1. **降级不删除**。架构文档模块七的"应对方案"原文：*瘦身节点默认只做"降级运行"
   或"建议剔除"，硬性的删除操作必须在内部审核工作台上，保留人类开发者的最终
   Review 确认权限。* 本文件因此**没有任何一条删除用例的代码路径**——最重的动作
   是把 `split` 改成 `COLD`（用例仍在 `case_ids` 里，消费方按 `split` 过滤时天然
   跳过它），以及往建议队列写一条 `pending`。

2. **全程零 LLM**。冗余度是"能力集合完全相等"、孤儿是"绑定 id 不在树上"、组合缺口
   是"两两组合的差集"——三件事都是精确的集合运算。docs/dev/17 第 4.1 节写明了理由：
   这类低风险决策交给 LLM，换来的是一个每次跑都可能不一样的瘦身结果，而瘦身恰恰
   是本项目里最需要可复现的动作（它会改变以后每一次评测跑哪些题）。

3. **本维度不产出 Fail**。它衡量的是测试集自身的健康度，不是 Skill 的质量。
   `blocking` 恒为 False，`status` 只在"关键计数取不到"时降为 NEEDS_HUMAN_REVIEW。

4. **无回边**。模块六是全项目唯一带环的子图（补盲 → 重新映射 → 直到达标）；模块七
   刻意不学它：组合缺口首次接入时几乎是全量未覆盖，回环会把"逐步收敛"变成"一次跑
   到底"，与 `max_combinatorial_patch_per_round` 想要的正好相反。补完这一轮就往下
   走，剩下的缺口交给后续评测轮次。

## 节点签名与返回值

与模块一~六同样的两条坑：签名必须写 `PruningState`（否则私有键会被 LangGraph 静默
裁掉，症状见 `state.py` 模块头），返回值只带增量。
"""

from __future__ import annotations

import itertools
import math
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import cast

from skill_evaluate.agents.analyzer.identity import parse_capability_tree_id
from skill_evaluate.agents.generator.schema import CapabilityFocus
from skill_evaluate.errors import GenerationError, PersistenceError
from skill_evaluate.logging import get_logger
from skill_evaluate.nodes.coverage.state import NODE_PREFIX
from skill_evaluate.nodes.pruning.deps import TRIGGERED_BY_COMBINATORIAL_GAP, PruningDeps
from skill_evaluate.nodes.pruning.state import (
    DIMENSION,
    KEY_ANALYZED_PAIR_COUNT,
    KEY_CLUSTER_COUNT,
    KEY_DEMOTED_CASE_IDS,
    KEY_DEMOTED_VALIDATION_COUNT,
    KEY_MATRIX_TIER_RANKED,
    KEY_MATRIX_TRUNCATED,
    KEY_NEW_SUGGESTION_COUNT,
    KEY_ORPHAN_CASE_IDS,
    KEY_PAIR_COVERAGE_RATIO,
    KEY_PATCH_FAILURE,
    KEY_PATCHED_PAIR_COUNT,
    KEY_TOTAL_PAIR_COUNT,
    KEY_UNCOVERED_PAIRS,
    PruningState,
)
from skill_evaluate.state.capability import CapabilityTree
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
from skill_evaluate.state.test_case import TestCase

logger = get_logger(component=DIMENSION)

# 节点名沿用模块六的 `coverage` 前缀（docs/dev/16 第 3 节：模块六/七/八在主图里
# 是同一个 `coverage` 分区）。四个名字都与 `nodes/coverage/nodes.py::NODE_NAMES`
# 里已有的五个不重复——**尤其是最后一个**：docs/dev/17 第 7 节把收尾节点也叫
# `finalize_dimension_report`，与模块六重名。同一张图里节点名必须唯一，重名的直接
# 后果是 `builder.add_node()` 抛错（好的情况）或后加的节点覆盖先加的（坏的情况，
# 表现为模块六的报告从此再也不写了）。这里改名为 `finalize_pruning_report`。
NODE_NAMES = {
    "redundant_case_pruning": f"{NODE_PREFIX}.redundant_case_pruning",
    "combinatorial_matrix_analysis": f"{NODE_PREFIX}.combinatorial_matrix_analysis",
    "combinatorial_feedback_generation": f"{NODE_PREFIX}.combinatorial_feedback_generation",
    "orphan_case_detection": f"{NODE_PREFIX}.orphan_case_detection",
    "finalize_pruning_report": f"{NODE_PREFIX}.finalize_pruning_report",
}

ENTRY_NODE = NODE_NAMES["redundant_case_pruning"]
TERMINAL_NODE = NODE_NAMES["finalize_pruning_report"]

# 代表性打分里"长度相当"的判定比例（docs/dev/17 第 4.1 节的"差异 < 20%"）。
# 用对数分桶实现：`log(len, 1.2)` 取整后落在同一桶里的两条 prompt，长度大致在
# 20% 以内。它是个启发式而非精确判据，作用只是让"两条长度差不多的用例"不要仅仅
# 因为多了三个字就压过 `split=TRAIN` 这条更有意义的偏好。
_LENGTH_BUCKET_RATIO = 1.2

# 组合矩阵截断时的能力权重次序。文档 18 落地前所有节点都是占位的 P1，此时这张表
# 对排序没有任何影响（见 `_tier_ranked()`），报告会如实标注"未做优先级筛选"。
_TIER_RANK = {
    CapabilityTier.P0_CORE: 0,
    CapabilityTier.P1_CONDITIONAL: 1,
    CapabilityTier.P2_DEFENSIVE: 2,
}


class PruningPipeline:
    """模块七的五个节点。做成类是为了让依赖注入只发生一次（构造时）。

    用法（docs/dev/24 装配主图时）见 `graph.py::add_pruning_nodes()`。
    """

    def __init__(self, deps: PruningDeps | None = None) -> None:
        self.deps = deps or PruningDeps()
        # 与模块六同一条装配期断言：本维度同样是纯分析（集合运算 + 一次可选的
        # Generator 调用），一次沙箱都不起，路由表必须仍然是 MINI。
        PruningDeps.assert_backend_routing()

    # ------------------------------------------------------------------ #
    # 1. redundant_case_pruning
    # ------------------------------------------------------------------ #

    async def redundant_case_pruning(self, state: PruningState) -> dict[str, object]:
        """冗余用例折叠（docs/dev/17 第 4 节）。

        聚类口径是**能力路径完全相等**（`frozenset(target_capability_ids)` 相同），
        不是"语义相似"。架构文档举的例子正是这个形状：*Case A 和 Case B 触发的原子
        能力路径完全一致（都仅仅测试了"读取 CSV"和"过滤空行"，只是换了不同的文件名
        和自然语言表述）*。用集合相等而不是语义聚类，是因为后者需要一个相似度阈值，
        而那个阈值定在哪都会同时产生两类相反的错误——把边界条件用例当冗余折掉、
        把真冗余留下——且两者都无法从报告里看出来
        （见 `docs/dev/interfaces/16` 第 7 节对 `normalize_capability_text()` 的同一条论证）。

        只处理 `POSITIVE`：`target_capability_ids` 只有正向用例才有（模块六只映射
        正向用例），其余类别的用例在这里一律是空集合，若不过滤会被聚成一个巨大的
        "空能力路径"簇然后几乎全被降级。

        本节点**幂等**：重复运行不会把整簇都降级，也不会重复计数，理由见
        `_representativeness_score()` 与下面对"整簇冷掉"的处理。
        """
        run_id = str(state["run_id"])
        cases = await self._positive_cases(state)

        clusters: dict[frozenset[str], list[TestCase]] = defaultdict(list)
        for case in cases:
            if not case.target_capability_ids:
                # 未映射的用例理论上不该出现（模块六应已全部映射）。跳过而不是
                # 报错：它可能是刚由 Generator 补出来、还没轮到映射的新题，把它当
                # 成"能力路径为空"聚进同一个簇会导致一批不相干的题被互相认作冗余。
                continue
            clusters[frozenset(case.target_capability_ids)].append(case)

        demoted_case_ids: list[str] = []
        demoted_validation = 0
        redundant_clusters = 0

        for capability_path, members in clusters.items():
            if len(members) <= 1:
                continue
            redundant_clusters += 1
            representative = max(members, key=_representativeness_score)

            # 整簇都已经是 COLD 的情形（能力映射变化导致两个旧簇合并时可能出现）：
            # 把代表恢复为 TRAIN。这不是"自动撤销人工决策"——`COLD` 在全项目里只有
            # 本节点一个生产者，人工决策走的是建议队列而不是 split；而一个能力路径
            # 若一条活跃用例都不剩，等于这条路径被静默地不再测了。
            if all(m.split is DatasetSplit.COLD for m in members):
                representative.split = DatasetSplit.TRAIN
                await self.deps.test_case_repository.save(representative)
                logger.warning(
                    "pruning_representative_restored",
                    run_id=run_id,
                    node_name=ENTRY_NODE,
                    case_id=representative.case_id,
                    capability_path=sorted(capability_path),
                )

            for case in members:
                if case.case_id == representative.case_id:
                    continue
                if case.split is DatasetSplit.COLD:
                    # 上一轮已经降级过。不重复落库、不重复计数——否则报告里"折叠
                    # 冗余用例 12 条"会在连续每一次评测中重复出现同样的 12 条，读
                    # 报告的人会以为测试集在持续膨胀。
                    continue
                if case.split is DatasetSplit.VALIDATION:
                    demoted_validation += 1
                case.split = DatasetSplit.COLD  # 降级，不删除（docs/dev/02 早已定义的枚举值）
                await self.deps.test_case_repository.save(case)
                demoted_case_ids.append(case.case_id)
                logger.info(
                    "pruning_case_demoted",
                    run_id=run_id,
                    node_name=ENTRY_NODE,
                    case_id=case.case_id,
                    representative_case_id=representative.case_id,
                    capability_path=sorted(capability_path),
                )

        logger.info(
            "pruning_redundancy_folded",
            run_id=run_id,
            node_name=ENTRY_NODE,
            positive_case_count=len(cases),
            cluster_count=len(clusters),
            redundant_cluster_count=redundant_clusters,
            demoted_count=len(demoted_case_ids),
            demoted_validation_count=demoted_validation,
        )
        return {
            KEY_DEMOTED_CASE_IDS: demoted_case_ids,
            KEY_DEMOTED_VALIDATION_COUNT: demoted_validation,
            KEY_CLUSTER_COUNT: redundant_clusters,
        }

    # ------------------------------------------------------------------ #
    # 2. combinatorial_matrix_analysis
    # ------------------------------------------------------------------ #

    async def combinatorial_matrix_analysis(self, state: PruningState) -> dict[str, object]:
        """组合能力覆盖矩阵（docs/dev/17 第 5 节）。

        架构文档模块七第 2 节的意图：*即使单个能力的覆盖率都是 100%，"能力 1 + 能力 2
        并发调用"的场景仍可能一条题都没有。*

        ## 三处需要说清楚的口径

        1. **已降级用例不计入覆盖**。否则会出现一个悖论：刚被折叠掉的冗余用例仍在
           为组合覆盖率贡献分子，于是"瘦身"反而看不出任何代价，而实际上那条题以后
           只在 Nightly 里跑。

        2. **组合爆炸靠截断而不是采样**。两两组合是 N² 级别（20 个能力就是 190 对）。
           超过 `max_capability_pairs_for_matrix` 时按能力权重排序后截断取前 N 对。
           不采样的理由是可比性：随机采样会让同一份测试集在两次运行中得到不同的组合
           覆盖率，那个数字就再也没法拿来比较了。权重分级（文档 18）落地前所有 tier
           都是占位值，此时排序退化为按 id 排——报告会如实标注"未做优先级筛选的截断
           分析"，这是过渡策略而非最终形态。

        3. **只统计仍在能力树上的 id**。用例可能带着指向已消失能力的旧绑定
           （那正是下一个节点要处理的孤儿），把它们算进 `combinatorial_pairs_covered`
           会在库里留下一批指向不存在节点的组合对。
        """
        run_id = str(state["run_id"])
        tree = await self._load_tree(state)
        settings = self.deps.settings()

        known_ids = {node.capability_id for node in tree.nodes}
        tier_ranked = _tier_ranked(tree)
        ordered_pairs = _prioritized_pairs(tree)
        total_pairs = len(ordered_pairs)
        limit = max(0, settings.max_capability_pairs_for_matrix)
        analyzed_pairs = ordered_pairs[:limit]
        truncated = total_pairs > len(analyzed_pairs)

        covered_pairs: set[frozenset[str]] = set()
        for case in await self._positive_cases(state):
            if case.split is DatasetSplit.COLD:
                continue
            ids = sorted({cid for cid in case.target_capability_ids if cid in known_ids})
            for pair in itertools.combinations(ids, 2):
                covered_pairs.add(frozenset(pair))

        uncovered = [pair for pair in analyzed_pairs if frozenset(pair) not in covered_pairs]
        covered_in_scope = len(analyzed_pairs) - len(uncovered)
        # 分母是**分析范围内**的组合对而不是全量：截断之后拿全量做分母，得到的数字
        # 既不是"分析范围的覆盖情况"也不是"全量的覆盖情况"，谁也解释不了。
        ratio = covered_in_scope / len(analyzed_pairs) if analyzed_pairs else 1.0

        # 落库的是**全部**已覆盖组合对（含截断范围之外的），这是 `CapabilityTree`
        # 上的事实记录，不该因为本轮的分析预算而缺一块。文档 18 的加权覆盖率与
        # docs/dev/22 的展示都直接读它。
        tree.combinatorial_pairs_covered = sorted(_as_sorted_pair(p) for p in covered_pairs)
        await self.deps.capability_repository.save(tree)

        logger.info(
            "pruning_combinatorial_matrix_analyzed",
            run_id=run_id,
            node_name=NODE_NAMES["combinatorial_matrix_analysis"],
            capability_count=len(tree.nodes),
            total_pair_count=total_pairs,
            analyzed_pair_count=len(analyzed_pairs),
            covered_pair_count=len(covered_pairs),
            uncovered_pair_count=len(uncovered),
            truncated=truncated,
            tier_ranked=tier_ranked,
            pair_coverage_ratio=round(ratio, 4),
        )
        return {
            KEY_UNCOVERED_PAIRS: [list(pair) for pair in uncovered],
            KEY_ANALYZED_PAIR_COUNT: len(analyzed_pairs),
            KEY_TOTAL_PAIR_COUNT: total_pairs,
            KEY_PAIR_COVERAGE_RATIO: ratio,
            KEY_MATRIX_TRUNCATED: truncated,
            KEY_MATRIX_TIER_RANKED: tier_ranked,
        }

    # ------------------------------------------------------------------ #
    # 3. combinatorial_feedback_generation
    # ------------------------------------------------------------------ #

    async def combinatorial_feedback_generation(self, state: PruningState) -> dict[str, object]:
        """把未覆盖的组合对反馈给 Generator（docs/dev/17 第 5 节）。

        ## 为什么是一个新节点而不是复用模块六的 `feedback_driven_generation`

        docs/dev/17 第 3 节的流程图写的是"复用文档 16 同名节点，传入
        combinatorial_pairs 而非 capability_ids"。落到 LangGraph 上这条路走不通，
        有两个各自独立的原因：

        1. 同一张图里节点名唯一。模块六/七在主图里是同一个 `coverage` 分区，让两条
           不同的边都指向那一个节点，等于把模块六的补盲回环接进模块七的直线流程——
           那个节点的两个出口都会把控制权交回 `coverage.map_case_coverage`。
        2. 那个节点的输入是 `_coverage_blind_spots`（模块六的私有键），而模块七
           读它就违反了"各维度不得读写其他维度私有键"的约定。

        真正该复用的是**服务**而不是**节点**：两者调的都是
        `TestSuiteService.incremental_patch()`，只是 focus 的填法不同。

        ## 单轮截断

        `max_combinatorial_patch_per_round`（默认 5）。首次接入组合矩阵时几乎所有
        组合对都未覆盖，不设上限就会一次性生成上百条题——测试集被撑爆的同时账单也
        被撑爆。截断之后剩下的缺口交由后续评测轮次逐步收敛。

        补题失败**不抛异常**（与模块六同一处理）：`incremental_patch()` 会在"这个
        Skill 还没有任何 active 用例集"等情形下抛 `GenerationError`，而本维度不阻断
        合并，为一次补题失败掀掉整条流水线不成比例。
        """
        run_id = str(state["run_id"])
        node_name = NODE_NAMES["combinatorial_feedback_generation"]
        uncovered = _uncovered_pairs(state)
        if not uncovered:
            # 防御性：路由函数已经拦过一次。空 focus 会被 `incremental_patch()` 拒绝。
            return {}

        budget = max(0, self.deps.settings().max_combinatorial_patch_per_round)
        selected = uncovered[:budget]
        if not selected:
            return {}

        tree = await self._load_tree(state)
        descriptions = {node.capability_id: node.description for node in tree.nodes}
        skill = await self._load_skill(state)

        focus = CapabilityFocus(
            combinatorial_pairs=[(a, b) for a, b in selected],
            # 与模块六同一条经验（docs/dev/interfaces/06 第 1 节）：Prompt 里给模型
            # 看的必须是描述而不是裸 id——`capability_id` 是描述文本的哈希。只带上
            # 本轮真正用到的那几项，避免把整棵树塞进 Prompt。
            descriptions={
                cid: descriptions[cid]
                for pair in selected
                for cid in pair
                if cid in descriptions
            },
        )

        try:
            new_suite = await self.deps.generator().incremental_patch(
                skill, focus=focus, triggered_by=TRIGGERED_BY_COMBINATORIAL_GAP
            )
        except GenerationError as exc:
            logger.error(
                "pruning_combinatorial_patch_failed",
                run_id=run_id,
                node_name=node_name,
                skill_id=skill.skill_id,
                requested_pair_count=len(selected),
                error=str(exc)[:500],
            )
            return {KEY_PATCH_FAILURE: str(exc)[:500]}

        logger.info(
            "pruning_combinatorial_patch_generated",
            run_id=run_id,
            node_name=node_name,
            skill_id=skill.skill_id,
            patched_pair_count=len(selected),
            remaining_uncovered=len(uncovered) - len(selected),
            suite_version_id=new_suite.suite_version_id,
        )
        return {
            # 公共字段：补题落了一个新的 active 版本。排在本维度之后的维度会看到
            # 补过组合缺口的用例集——与模块六一样，这是数据飞轮的意图而非副作用。
            "active_suite_version_id": new_suite.suite_version_id,
            KEY_PATCHED_PAIR_COUNT: len(selected),
        }

    # ------------------------------------------------------------------ #
    # 4. orphan_case_detection
    # ------------------------------------------------------------------ #

    async def orphan_case_detection(self, state: PruningState) -> dict[str, object]:
        """能力漂移与弃用侦测（docs/dev/17 第 6 节）。

        判定口径是**全部**绑定能力都已从能力树消失（`set(...) & current_ids` 为空），
        而不是"有任意一项消失"。后者会把"三项能力里改写了一项描述"的用例也判成孤儿
        ——`capability_id` 是描述文本的哈希，改一个字就是一个新 id，那种用例仍然测得
        到另外两项能力，淘汰它是纯粹的损失。

        **不挂起**（不调 `suspend_and_wait()`）。孤儿用例的存在不影响本次运行任何
        结论的正确性：模块一/三/五等消费方按 `split` 取题后各自独立判定，没有谁会去
        检查"这条题绑的能力还在不在"。它只是一条注定失去意义、需要人找时间清理的题。
        本项目由此明确区分两种人机协作模式——**不确认就无法继续算下去的用阻塞式挂起**
        （如模块六的能力树粒度确认），**可以先继续跑、但需要人类找时间清理的用非阻塞
        建议队列**（本节点）。

        写入用 `save_if_absent()`：同一条孤儿用例连续三次评测都会被检出，但人只需要
        处理一次；已被人 `rejected` 的也不会被重新推回待办。
        """
        run_id = str(state["run_id"])
        node_name = NODE_NAMES["orphan_case_detection"]
        tree = await self._load_tree(state)
        current_ids = {node.capability_id for node in tree.nodes}

        orphans = [
            case
            for case in await self._positive_cases(state)
            if case.target_capability_ids and not (set(case.target_capability_ids) & current_ids)
        ]

        new_suggestions = 0
        for case in orphans:
            created = await self.deps.suggestion_repository.save_if_absent(
                TestCaseSuggestion(
                    suggestion_id=str(uuid.uuid4()),
                    case_id=case.case_id,
                    suggestion_type=SuggestionType.ORPHAN_RETIREMENT,
                    # 把消失的 id **和**用例 prompt 的开头一起写进去：工作台上的人
                    # 要判断的是"这条题该淘汰还是该重新绑定到改名后的能力上"，只给
                    # 一串哈希等于把这个判断重新推回给人自己去查库。
                    reason=(
                        f"绑定能力 {sorted(case.target_capability_ids)} 已从最新能力树"
                        f"（skill_version_ref={tree.skill_version_ref}）中消失；"
                        f"用例内容：{case.prompt[:200]}"
                    ),
                    status=SuggestionStatus.PENDING,
                    created_at=datetime.now(UTC),
                )
            )
            new_suggestions += int(created)
            logger.info(
                "pruning_orphan_case_detected",
                run_id=run_id,
                node_name=node_name,
                case_id=case.case_id,
                stale_capability_ids=sorted(case.target_capability_ids),
                suggestion_created=created,
            )

        return {
            KEY_ORPHAN_CASE_IDS: [case.case_id for case in orphans],
            KEY_NEW_SUGGESTION_COUNT: new_suggestions,
        }

    # ------------------------------------------------------------------ #
    # 5. finalize_pruning_report
    # ------------------------------------------------------------------ #

    async def finalize_pruning_report(self, state: PruningState) -> dict[str, object]:
        """聚合结论写进 `dimension_results`（docs/dev/17 第 7 节）。

        判定口径：

        | 情形 | status | blocking |
        |---|---|---|
        | 组合覆盖率缺失（私有键被裁掉/节点被跳过） | NEEDS_HUMAN_REVIEW | False |
        | 其余（含存在孤儿用例、组合缺口未补齐） | PASS | False |

        **本维度不产生 FAIL**（docs/dev/17 第 7 节）：瘦身、组合缺口、能力漂移都是
        测试集自身的健康度建议，不是 Skill 的质量门禁。判 FAIL 会让一个功能完全正确
        的 Skill 因为"测试集还不够全"而被拦下，最终结果是所有人都学会绕过这条门禁。

        `NEEDS_HUMAN_REVIEW` 那一档不是门禁，是**故障信号**：它只在"这次分析根本没
        跑出数"时出现，判 PASS 等于把整个维度悄悄关掉（症状见 `state.py` 模块头）。

        `score` 填组合覆盖率：本维度唯一有连续取值的量。折叠条数与孤儿条数是计数
        不是比率，塞进 `score` 会让 `BenchmarkReport` 里的分数失去可比性。
        """
        run_id = str(state["run_id"])
        raw_ratio = state.get(KEY_PAIR_COVERAGE_RATIO)
        ratio = float(cast("float", raw_ratio)) if raw_ratio is not None else None
        demoted = _str_list(state, KEY_DEMOTED_CASE_IDS)
        orphans = _str_list(state, KEY_ORPHAN_CASE_IDS)
        uncovered = _uncovered_pairs(state)
        analyzed = _int_from_state(state, KEY_ANALYZED_PAIR_COUNT)

        findings: list[str] = []
        status = JudgeVerdictStatus.PASS

        if ratio is None:
            status = JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
            findings.append(
                "未取到组合覆盖矩阵的计算结果：请确认主图状态 schema 包含本维度私有键"
                f"（{KEY_PAIR_COVERAGE_RATIO}），见 docs/dev/interfaces/17 第 2 节。"
                "注意瘦身与孤儿检测仍然实际执行了（它们直接改库），本条只说明报告里的"
                "计数不可信。"
            )

        findings.append(
            f"折叠冗余用例 {len(demoted)} 条（本轮新降级至 COLD 冷数据区，"
            f"涉及 {_int_from_state(state, KEY_CLUSTER_COUNT)} 个能力路径完全相同的用例簇）。"
        )
        demoted_validation = _int_from_state(state, KEY_DEMOTED_VALIDATION_COUNT)
        if demoted_validation:
            findings.append(
                f"其中 {demoted_validation} 条原属验证集：验证集是优化闭环"
                "（docs/dev/09）判断补丁是否真的改好的依据，缩小时请留意。"
            )

        if ratio is not None:
            findings.append(
                f"能力组合覆盖率 {ratio:.1%}：分析范围内 {analyzed} 对组合中"
                f"{analyzed - len(uncovered)} 对已被同一条活跃用例同时触发，"
                f"未覆盖 {len(uncovered)} 对。"
            )
        if state.get(KEY_MATRIX_TRUNCATED):
            total = _int_from_state(state, KEY_TOTAL_PAIR_COUNT)
            note = (
                "已按能力权重优先级排序后截断"
                if state.get(KEY_MATRIX_TIER_RANKED)
                else "未做优先级筛选的截断分析（能力权重分级尚未落地，建议待模块八"
                "（docs/dev/18）完成后重新评估）"
            )
            findings.append(f"能力组合共 {total} 对，超出单轮分析上限，{note}，本轮只分析前 {analyzed} 对。")

        patched = _int_from_state(state, KEY_PATCHED_PAIR_COUNT)
        patch_failure = state.get(KEY_PATCH_FAILURE)
        if patch_failure:
            findings.append(f"组合缺口定向补题未能执行，缺口维持原状：{patch_failure}")
        elif patched:
            findings.append(
                f"已针对 {patched} 对未覆盖组合定向补题，其余缺口交由后续评测轮次逐步收敛。"
            )

        findings.append(
            f"检测到孤儿用例 {len(orphans)} 条（绑定能力已从最新能力树中消失），"
            f"其中 {_int_from_state(state, KEY_NEW_SUGGESTION_COUNT)} 条为本轮新增待办；"
            "所有淘汰动作均需人工在审查工作台（docs/dev/22）确认，本维度只降级与建议，"
            "不删除任何用例。"
        )

        await self.deps.reporter().record_dimension_result(
            run_id=run_id,
            dimension=DIMENSION,
            status=status,
            score=ratio,
            findings=findings,
            # 测试集健康度不阻断合并，理由见本方法的 docstring 与 docs/dev/17 第 7 节。
            blocking=False,
        )
        logger.info(
            "pruning_dimension_recorded",
            run_id=run_id,
            node_name=TERMINAL_NODE,
            status=status.value,
            pair_coverage_ratio=ratio,
            demoted_count=len(demoted),
            orphan_count=len(orphans),
            findings=len(findings),
        )
        return {}

    # ------------------------------------------------------------------ #
    # 条件路由
    # ------------------------------------------------------------------ #

    def route_after_matrix(self, state: PruningState) -> str:
        """`combinatorial_matrix_analysis` 之后：定向补题，还是直接去孤儿检测。

        两条分支最终都汇到 `orphan_case_detection`，**没有回边**——补完这一轮就往下
        走，不重新分析矩阵。理由见模块头第 4 条：回环会把"逐步收敛"变成"一次跑到底"。

        预算为 0（`max_combinatorial_patch_per_round=0`）时也直接跳过：把这个旋钮
        调到 0 的语义是"只分析、不自动补题"，那是一个合理的运维选择（例如测试集正在
        人工整理期间），路由必须尊重它，否则会走进一个立刻空转返回的节点。
        """
        if not _uncovered_pairs(state):
            return NODE_NAMES["orphan_case_detection"]
        if self.deps.settings().max_combinatorial_patch_per_round <= 0:
            return NODE_NAMES["orphan_case_detection"]
        return NODE_NAMES["combinatorial_feedback_generation"]

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    async def _positive_cases(self, state: PruningState) -> list[TestCase]:
        """取当前 active 版本里的全部正向用例。

        三个分析节点都用它，口径必须完全一致：`suite_version_id` 缺失时抛
        `PersistenceError` 而不是静默返回空列表——"这个测试集没有冗余"和"我根本没拿
        到测试集"是两件完全不同的事，后者若被当成前者，报告里会写"折叠 0 条"，看起来
        像是测试集很健康（与模块六 `map_case_coverage` 同一条理由）。
        """
        suite_version_id = state.get("active_suite_version_id")
        if not suite_version_id:
            raise PersistenceError(
                f"{ENTRY_NODE}：状态里没有 active_suite_version_id，无法确定该拿哪一版"
                "用例集做瘦身分析。请确认主图入口节点已调用 "
                "`TestSuiteService.ensure_test_suite()` 并把版本号写进状态"
                "（docs/dev/interfaces/06 第 6 节）。"
            )
        return await self.deps.test_case_repository.list_by_categories(
            str(suite_version_id), [TestCaseCategory.POSITIVE]
        )

    async def _load_skill(self, state: PruningState) -> SkillDefinition:
        skill = await self.deps.skill_repository.get(
            str(state["skill_id"]), str(state["skill_version_ref"])
        )
        if skill is None:
            raise PersistenceError(
                f"未找到被测 Skill：skill_id={state['skill_id']!r} "
                f"version_ref={state['skill_version_ref']!r}。"
                "请先经 `ingestion.load_skill()` + `SkillRepository.save()` 入库。"
            )
        return skill

    async def _load_tree(self, state: PruningState) -> CapabilityTree:
        """按 `capability_tree_id` 读回能力树（与模块六同一套：状态里只存 id）。

        取不到就抛：本维度的三件事全都建立在能力树之上，没有树时"没有冗余、没有组合
        缺口、没有孤儿"这三个结论都是假的。
        """
        tree_id = state.get("capability_tree_id")
        if not tree_id:
            raise PersistenceError(
                f"状态里没有 capability_tree_id：模块七的节点必须排在模块六的 "
                "`coverage.finalize_dimension_report` 之后（docs/dev/interfaces/16 第 4 节）。"
            )
        skill_id, version_ref = parse_capability_tree_id(str(tree_id))
        tree = await self.deps.capability_repository.get(skill_id, version_ref)
        if tree is None:
            raise PersistenceError(
                f"未找到能力树：capability_tree_id={tree_id!r}。"
                "它应当由 `coverage.extract_capability_tree` 节点落库。"
            )
        return tree


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


def _representativeness_score(case: TestCase) -> tuple[int, int, int, int, str]:
    """选择"最具代表性"的用例留在主干（docs/dev/17 第 4.1 节）。

    元组按优先级从高到低比较，`max()` 取最大者：

    1. **仍是活跃用例**（`split != COLD`）。文档 4.1 的原始打分没有这一项，缺了它
       本节点就不幂等：第二轮跑时，若某条已降级的 COLD 用例 prompt 更长，它会被选
       成代表，于是上一轮留下的那条活跃用例也被降级——整簇冷掉，这条能力路径从此
       没有任何活跃用例覆盖。
    2. **prompt 长度分桶**。文档的表述是"更长通常意味着包含更复杂的边界条件/多步骤
       上下文"，符合架构文档"保留一个最具代表性（或包含最复杂边界条件）的用例"。
       分桶（而非直接比长度）落实的是文档"若长度相近（差异 < 20%）"这一条：长度
       相当的两条题不该仅仅因为多了三个字就压过下面那条更有意义的偏好。
    3. **`split == TRAIN`**。文档 4.1：优先保留训练集用例，保证降级不误伤训练集主力
       （被降级的验证集用例会在报告里单独点名，见 finalize）。
    4. **精确 prompt 长度**，同桶内仍以更长者为代表。
    5. **`case_id`**（字典序）。纯粹为了**确定性**：仓储返回的用例顺序由数据库决定，
       没有这一项时 `max()` 会返回"第一个达到最大值的元素"，同一批数据在两次运行中
       可能选出不同的代表，于是每次评测都会降级一批不同的用例。

    刻意不是 LLM 判断：冗余度本身已经是精确的集合相等关系（能力路径完全一致），
    "选谁做代表"这一步不需要语义理解——为这种低风险决策消耗 LLM 成本，换来的是一个
    不可复现的瘦身结果。
    """
    return (
        0 if case.split is DatasetSplit.COLD else 1,
        _length_bucket(case.prompt),
        1 if case.split is DatasetSplit.TRAIN else 0,
        len(case.prompt),
        case.case_id,
    )


def _length_bucket(prompt: str) -> int:
    """把 prompt 长度按 20% 的比例分桶（见 `_LENGTH_BUCKET_RATIO`）。"""
    return int(math.log(max(len(prompt), 1), _LENGTH_BUCKET_RATIO))


def _tier_ranked(tree: CapabilityTree) -> bool:
    """能力权重分级是否已经有意义。

    文档 18 落地前，`AnalyzerAgent` 给所有节点填的都是同一个占位 tier
    （`agents.analyzer.service.PLACEHOLDER_TIER`）。判据用"树上是否出现了不止一档
    tier"而不是"是否等于那个占位常量"：前者不依赖占位值具体取哪一档，文档 18 若改用
    别的占位策略也不会让这里悄悄给出错误答案。
    """
    return len({node.tier for node in tree.nodes}) > 1


def _prioritized_pairs(tree: CapabilityTree) -> list[tuple[str, str]]:
    """全部两两组合，按"应当优先分析"的次序排列。

    排序键 `(tier 之和, 两者中较低的优先级, id 对)`：P0×P0 最前，其次 P0×P1，
    再次 P1×P1（`max` 更小）而后 P0×P2，依此类推。docs/dev/17 第 5 节要求超限时
    "只分析 P0 核心能力两两组合"，按本序截断即可自然得到这个效果。

    最后一项 `id 对` 保证**确定性**：同一棵树每次给出同一个次序，截断范围因此稳定，
    组合覆盖率才可以在两次运行之间比较。
    """
    ranked = [(_TIER_RANK[node.tier], node.capability_id) for node in tree.nodes]
    ranked.sort()
    pairs = itertools.combinations(ranked, 2)
    ordered = sorted(pairs, key=lambda p: (p[0][0] + p[1][0], max(p[0][0], p[1][0]), p))
    return [(a[1], b[1]) for a, b in ordered]


def _as_sorted_pair(pair: frozenset[str]) -> tuple[str, str]:
    """`frozenset` → 有序二元组。

    落库的组合对必须**有序**：`(a, b)` 和 `(b, a)` 是同一对，不排序的话同一份数据
    在两次运行中会写出两种不同的 JSON，读它的人（文档 18 的加权覆盖率、docs/dev/22
    的展示）就得各自再做一次归一。
    """
    first, second = sorted(pair)
    return (first, second)


def _uncovered_pairs(state: PruningState) -> list[tuple[str, str]]:
    """从图状态里取回未覆盖组合对。

    存进去的是 `list[list[str]]`（JSON 友好），取出来收敛回二元组，让调用方不必各自
    处理"Checkpoint 反序列化之后元组变成了列表"这件事。长度不为 2 的条目直接跳过：
    那只可能来自被外部污染的状态，静默当成一对组合会构造出一个非法的 focus。
    """
    raw = cast("list[object] | None", state.get(KEY_UNCOVERED_PAIRS))
    pairs: list[tuple[str, str]] = []
    for item in raw or []:
        values = list(cast("list[str]", item))
        if len(values) == 2:
            pairs.append((values[0], values[1]))
    return pairs


def _str_list(state: PruningState, key: str) -> list[str]:
    """从图状态里取一个字符串列表，缺键/空值时返回空列表。"""
    raw = cast("list[str] | None", state.get(key))
    return [str(item) for item in (raw or [])]


def _int_from_state(state: PruningState, key: str) -> int:
    """从图状态里取一个整数，缺键/空值时返回 0。

    `PipelineState` 是 TypedDict，用**变量**作键时静态类型会退化成 `object`；
    这里集中收窄一次，好过在每个调用点各写一行 cast（与模块三/五/六同一处理）。
    """
    value = state.get(key)
    return int(cast("int", value)) if value is not None else 0


__all__ = [
    "ENTRY_NODE",
    "NODE_NAMES",
    "TERMINAL_NODE",
    "PruningPipeline",
]
