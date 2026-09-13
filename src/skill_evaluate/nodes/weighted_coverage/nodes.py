"""模块八：多维加权覆盖率与隐式边界追踪的节点实现（docs/dev/18）。

```
（承接模块七 coverage.finalize_pruning_report 之后）
extract_tier_and_negative_constraints   Analyzer 补齐 tier 分级 + 抽取负向约束
        ↓
map_negative_constraint_coverage        用例 → 约束 的反事实覆盖映射（走 Judge）
        ↓
  [存在未覆盖约束?] --是--> constraint_feedback_generation（定向补反事实用例，无回边）
        ↓否                              ↓
recompute_weighted_coverage  ←──────────┘   按真实权重重算覆盖率 + 量化判定
        ↓
upgrade_combinatorial_priority          用真实 tier 重排组合缺口（替换模块七的过渡策略）
        ↓
generate_traceability_artifact          traceability_matrix.json / .csv
        ↓
finalize_weighted_coverage_report
```

## 五条贯穿本文件的关键决策

1. **本维度不引入新的执行节点类型**（docs/dev/18 第 1 节）。它全部是"升级已有
   产出"：模块六抽的树补上分级、模块七的组合缺口补上优先级、外加一项模块六/七
   都没做的负向约束追踪。一次沙箱都不起，也不落 `ExecutionTrace`。

2. **加权口径只有一个实现**。覆盖率算法是 `CapabilityTree.weighted_coverage()`，
   判定走模块六已注册的 `capability_coverage_threshold` 规则（**不另注册同名
   规则**——`register_rule()` 遇重名直接抛错，理由见 `nodes/coverage/rules.py`
   模块头）。本维度与模块六的区别不在算法，而在**调用时机**：模块六跑在分级之前
   （等权口径），本维度跑在分级之后（真实加权口径），两条判定记录靠 `inputs`
   里的 `tier_weighted` 标记区分。

3. **无回边**。约束补题之后直接往下走，不回头重新映射。与模块七同一条理由：
   首次接入时几乎所有约束都未覆盖，回环会把"逐步收敛"变成"一次跑到底"。补出来的
   反事实用例带着出题时回填的 `negative_constraint_ids`，**下一轮**评测的映射
   节点不花一次 LLM 调用就能认出它们——这正是数据飞轮的形状。

4. **覆盖率不阻断合并**（`blocking=False`）。延续模块六/七的一贯策略：覆盖率
   反映"测试是否测得全"，不是"Skill 本身是否有质量问题"。

5. **"未覆盖"与"未判定"严格分开**。判定预算打满、或候选用例恰好被黄金盲测占用
   时，得到的是"这次没算出结论"，不是"没覆盖"。把后者混进前者会凭空生成一批补题
   需求，而那些约束可能本来就覆盖着。

## 节点签名与返回值

与模块一~七同样的两条坑：签名必须写 `WeightedCoverageState`（否则私有键会被
LangGraph 静默裁掉，症状见 `state.py` 模块头），返回值只带增量
（`judge_verdict_ids` 的 reducer 是 `operator.add`，回抛整个旧状态会让 id 翻倍）。
"""

from __future__ import annotations

import asyncio
from typing import cast

from skill_evaluate.agents.analyzer.identity import parse_capability_tree_id
from skill_evaluate.agents.generator.schema import CapabilityFocus
from skill_evaluate.agents.judge.golden_injector import is_golden_subject
from skill_evaluate.errors import GenerationError, PersistenceError
from skill_evaluate.logging import get_logger
from skill_evaluate.nodes.coverage import rules
from skill_evaluate.nodes.coverage.state import NODE_PREFIX
from skill_evaluate.nodes.weighted_coverage.artifact import (
    build_traceability_matrix,
    write_matrix,
)
from skill_evaluate.nodes.weighted_coverage.deps import (
    TRIGGERED_BY_NEGATIVE_CONSTRAINT_GAP,
    WeightedCoverageDeps,
)
from skill_evaluate.nodes.weighted_coverage.priority import (
    as_sorted_pair,
    prioritized_pairs,
    prioritized_uncovered_pairs,
)
from skill_evaluate.nodes.weighted_coverage.state import (
    DIMENSION,
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
    WeightedCoverageState,
)
from skill_evaluate.state.capability import CapabilityTree, NegativeConstraint
from skill_evaluate.state.enums import (
    CapabilityTier,
    Criticality,
    DatasetSplit,
    JudgeVerdictStatus,
    TestCaseCategory,
)
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase

logger = get_logger(component=DIMENSION)

# 节点名沿用模块六的 `coverage` 前缀（docs/dev/16 第 3 节：模块六/七/八在主图里是
# 同一个 `coverage` 分区）。七个名字都与 `nodes/coverage/nodes.py::NODE_NAMES` 的
# 五个、`nodes/pruning/nodes.py::NODE_NAMES` 的五个不重复——**尤其是收尾节点**：
# 那两份文档的收尾节点分别叫 `finalize_dimension_report` / `finalize_pruning_report`，
# 同一张图里节点名必须唯一，重名的直接后果是先加的节点被覆盖（表现为某个维度的
# 报告从此再也不写了，且没有任何报错）。这里叫 `finalize_weighted_coverage_report`。
NODE_NAMES = {
    "extract_tier_and_negative_constraints": (
        f"{NODE_PREFIX}.extract_tier_and_negative_constraints"
    ),
    "map_negative_constraint_coverage": f"{NODE_PREFIX}.map_negative_constraint_coverage",
    "constraint_feedback_generation": f"{NODE_PREFIX}.constraint_feedback_generation",
    "recompute_weighted_coverage": f"{NODE_PREFIX}.recompute_weighted_coverage",
    "upgrade_combinatorial_priority": f"{NODE_PREFIX}.upgrade_combinatorial_priority",
    "generate_traceability_artifact": f"{NODE_PREFIX}.generate_traceability_artifact",
    "finalize_weighted_coverage_report": f"{NODE_PREFIX}.finalize_weighted_coverage_report",
}

ENTRY_NODE = NODE_NAMES["extract_tier_and_negative_constraints"]
TERMINAL_NODE = NODE_NAMES["finalize_weighted_coverage_report"]

# 加权覆盖率判定的 `subject_id` 前缀。与模块六的 `coverage:` **分开**：两者判的是
# 同一个 Skill 的同一条阈值，但口径不同（等权 / 加权），共用一个 subject_id 会让
# `JudgeRepository.list_verdicts()` 里两种口径的历史记录混成一堆，谁也分不出某条
# 记录当时算的是哪个数。
SUBJECT_PREFIX_WEIGHTED = "wcoverage:"

# 反事实覆盖判定的 `subject_id` 前缀，形如 `neg_probe:<constraint_id>:<case_id>`。
# 带上两端的 id 是为了可回查：这类判定一轮可能有上百条，出现可疑的约束覆盖率时，
# 靠它才能在 `judge_verdicts` 里精确捞出"这条约束对这条用例当时判了什么"。
SUBJECT_PREFIX_CONSTRAINT_PROBE = "neg_probe:"

# 反事实覆盖判定的模板 key（`agents/mini/templates/weighted_coverage.py` 注册）。
TEMPLATE_NEGATIVE_CONSTRAINT_PROBE = "negative_constraint_probe"

# 反事实覆盖判定的重要度（docs/dev/18 第 4.1 节）。
#
# `ROUTINE` 而不是 `CRITICAL`：误判的后果是"漏标一条未覆盖约束"或"多补一条反事实
# 用例"，属于覆盖率统计场景，不是阻断合并的重大判决。三副本共识要三倍 Token，而
# 这一步的调用量是"约束数 × 用例数"，上共识等于把本维度的成本直接抬高一个量级去
# 换一个不阻断任何事情的数字。
CONSTRAINT_PROBE_CRITICALITY = Criticality.ROUTINE

# 报告里逐条列出组合缺口的上限。超出部分只报数量——一份写着 100 行组合对哈希的
# 报告没有人会读，而前几对（按权重排序后正是最该先补的那几对）是真会被看的。
_MAX_LISTED_PAIRS = 10


class WeightedCoveragePipeline:
    """模块八的七个节点。做成类是为了让依赖注入只发生一次（构造时）。

    用法（docs/dev/24 装配主图时）见 `graph.py::add_weighted_coverage_nodes()`。
    """

    def __init__(self, deps: WeightedCoverageDeps | None = None) -> None:
        self.deps = deps or WeightedCoverageDeps()
        # 与模块六/七同一条装配期断言：本维度同样是纯分析（LLM 抽取 + 集合运算 +
        # 一次可选的 Generator 调用），一次沙箱都不起，路由表必须仍然是 MINI。
        WeightedCoverageDeps.assert_backend_routing()

    # ------------------------------------------------------------------ #
    # 1. extract_tier_and_negative_constraints
    # ------------------------------------------------------------------ #

    async def extract_tier_and_negative_constraints(
        self, state: WeightedCoverageState
    ) -> dict[str, object]:
        """补齐模块六留下的两处占位：权重分级与负向约束（docs/dev/18 第 3 节）。

        两件事合在一个节点里，是因为它们的输入完全相同（同一棵树 + 同一份
        SKILL.md）、产出落在同一次 `CapabilityRepository.save()` 上。拆成两个节点
        会多一次读树 + 多一次写树，且中间态（分级好了但约束还没抽）对任何人都没有
        意义。

        **`capability_id` 不变**：分级是原地更新 `tier`，不重新生成 id
        （`docs/dev/interfaces/16` 第 4.1 节点名了这条坑——换一套 id 方案的后果是
        覆盖率在某次运行后毫无征兆地从 92% 掉到 40%，且没有任何报错）。

        **约束是整批重抽、覆盖旧的那批**，不做"保留上一轮 covered 标记"的合并：
        下一个节点本来就会把全部约束的覆盖情况从零重算（与模块六
        `map_case_coverage` 同一条幂等性理由），保留旧标记只会在两轮之间留下一份
        没人更新的陈旧数据。
        """
        run_id = str(state["run_id"])
        tree = await self._load_tree(state)
        skill = await self._load_skill(state)
        analyzer = self.deps.analyzer()

        tree = await analyzer.classify_tiers(tree, skill)
        tree.negative_constraints = await analyzer.extract_negative_constraints(skill)
        await self.deps.capability_repository.save(tree)

        distribution = {
            tier.value: sum(1 for node in tree.nodes if node.tier is tier)
            for tier in CapabilityTier
        }
        logger.info(
            "weighted_coverage_tiers_and_constraints_ready",
            run_id=run_id,
            node_name=ENTRY_NODE,
            skill_id=tree.skill_id,
            capability_count=len(tree.nodes),
            tier_distribution=distribution,
            tier_graded=tree.tier_grading_applied(),
            constraint_count=len(tree.negative_constraints),
        )
        return {
            KEY_NODE_COUNT: len(tree.nodes),
            KEY_TIER_DISTRIBUTION: distribution,
            KEY_TIER_GRADED: tree.tier_grading_applied(),
            KEY_CONSTRAINT_COUNT: len(tree.negative_constraints),
        }

    # ------------------------------------------------------------------ #
    # 2. map_negative_constraint_coverage
    # ------------------------------------------------------------------ #

    async def map_negative_constraint_coverage(
        self, state: WeightedCoverageState
    ) -> dict[str, object]:
        """反事实覆盖映射：哪些用例**故意诱导**智能体去踩某条禁令（docs/dev/18 第 4 节）。

        ## 候选用例的口径

        扫 `POSITIVE` **与** `ADVERSARIAL` 两类（docs/dev/18 第 4 节）：常规正向
        用例可能碰巧构造出了陷阱场景，模块五的对抗用例则更可能专门设计诱导。
        其余类别不扫——反向近脱靶用例的语义是"这类请求不该由本 Skill 处理"，拿它
        证明某条禁令被测到了是自相矛盾的。

        `COLD` 用例同样不计入（与模块七组合矩阵同一条理由）：靠一批已经被降级、
        以后只在 Nightly 里跑的用例去撑约束覆盖率，是个悖论。

        ## 两级判定：先看绑定，再问裁判

        用例自带的 `negative_constraint_ids`（Generator 按 `CapabilityFocus` 出题时
        回填的）**直接认**，不花判定调用：那是出题时就确定的事实，再让模型判一次
        既贵又可能把它判掉。其余组合才走 `judgmental_verdict()`。

        这也是补题闭环能收敛的原因：本轮补出来的反事实用例，在**下一轮**评测里靠
        绑定就能认出来，一次 LLM 调用都不花。

        ## 预算与"未判定"

        判定量是"约束数 × 用例数"，因此有一道总闸门
        （`max_constraint_probe_calls`）。超限的组合**不判成未覆盖**，而是让相关
        约束进入"未判定"集合：未覆盖是一个结论，未判定是"这次没算出结论"，混为
        一谈会凭空生成一批补题需求（每条又是一次生成调用）。

        候选组合按**用例优先**的次序排（每条用例先跟全部约束比一遍，再换下一条），
        这样预算被打满时，每条约束拿到的判定机会是均等的；按约束优先排则会出现
        "前两条约束判得很细、后面几条一次都没判"的倾斜。
        """
        run_id = str(state["run_id"])
        node_name = NODE_NAMES["map_negative_constraint_coverage"]
        tree = await self._load_tree(state)
        constraints = tree.negative_constraints

        if not constraints:
            # 这份 SKILL.md 一条禁止性规则都没写。覆盖率取 1.0（"没有约束可测"不是
            # "该测的都没测"），报告靠 `KEY_CONSTRAINT_COUNT == 0` 把两者分开。
            logger.info(
                "weighted_coverage_no_negative_constraints",
                run_id=run_id,
                node_name=node_name,
                skill_id=tree.skill_id,
            )
            return {
                KEY_UNCOVERED_CONSTRAINT_IDS: [],
                KEY_UNDETERMINED_CONSTRAINT_IDS: [],
                KEY_CONSTRAINT_RATIO: 1.0,
                KEY_PROBE_CALL_COUNT: 0,
                KEY_PROBE_BUDGET_EXHAUSTED: False,
            }

        cases = await self._candidate_cases(state)
        # 每轮从零重算，保证节点幂等（与模块六 `map_case_coverage` 同一条理由：
        # 增量累加会让 `covering_case_ids` 在重跑时出现重复项）。
        for constraint in constraints:
            constraint.covered = False
            constraint.covering_case_ids = []

        # case有相应的负向约束
        declared: dict[str, list[str]] = {c.constraint_id: [] for c in constraints}
        # case待分配负向约束
        pending: list[tuple[NegativeConstraint, TestCase]] = []
        for case in cases:
            bound = set(case.negative_constraint_ids)
            for constraint in constraints:
                if constraint.constraint_id in bound:
                    declared[constraint.constraint_id].append(case.case_id)
                else:
                    pending.append((constraint, case))

        budget = max(0, self.deps.settings().max_constraint_probe_calls)
        budgeted, skipped = pending[:budget], pending[budget:]
        probed = await self._probe_pairs(run_id, budgeted)

        # 预算没轮到的那些组合，其所属约束记为"未判定"（除非已经由别的证据覆盖）。
        deferred_ids = {constraint.constraint_id for constraint, _ in skipped}
        undetermined_ids: list[str] = []
        for constraint in constraints:
            cid = constraint.constraint_id
            covering = [*declared[cid], *probed.confirmed.get(cid, [])]
            constraint.covered = bool(covering)
            constraint.covering_case_ids = covering
            if not constraint.covered and (cid in deferred_ids or cid in probed.undetermined):
                undetermined_ids.append(cid)

        await self.deps.capability_repository.save(tree)

        uncovered_ids = [
            c.constraint_id
            for c in constraints
            if not c.covered and c.constraint_id not in set(undetermined_ids)
        ]
        logger.info(
            "weighted_coverage_constraints_mapped",
            run_id=run_id,
            node_name=node_name,
            skill_id=tree.skill_id,
            constraint_count=len(constraints),
            candidate_case_count=len(cases),
            probe_calls=len(budgeted),
            budget_exhausted=bool(skipped),
            covered_count=sum(1 for c in constraints if c.covered),
            uncovered_count=len(uncovered_ids),
            undetermined_count=len(undetermined_ids),
        )
        return {
            KEY_UNCOVERED_CONSTRAINT_IDS: uncovered_ids,
            KEY_UNDETERMINED_CONSTRAINT_IDS: undetermined_ids,
            KEY_CONSTRAINT_RATIO: tree.negative_constraint_coverage(),
            KEY_PROBE_CALL_COUNT: len(budgeted),
            KEY_PROBE_BUDGET_EXHAUSTED: bool(skipped),
        }

    async def _probe_pairs(
        self, run_id: str, pairs: list[tuple[NegativeConstraint, TestCase]]
    ) -> _ProbeOutcome:
        """并发判定若干 (约束, 用例) 组合，返回"确认覆盖"与"未判定"两份结果。

        并发上限沿用 `max_concurrent_mappings`（模块六给用例映射设的那个）：口径
        一致——两者都是"评测系统自己发出的 LLM 请求"，全量 `gather` 打出去会撞上
        供应商限速。

        结果里刻意**不含**"判定为不覆盖"那一类：调用方要的是"谁覆盖了"和"谁没
        算出结论"，第三类（明确判了不覆盖）只要不出现在前两者里就够了，单独带出来
        只会让调用方多一个要对齐的集合。
        """
        outcome = _ProbeOutcome()
        if not pairs:
            return outcome

        semaphore = asyncio.Semaphore(max(1, self.deps.settings().max_concurrent_mappings))
        judge = self.deps.judge()

        async def _one(constraint: NegativeConstraint, case: TestCase) -> None:
            async with semaphore:
                result = await judge.judgmental_verdict(
                    subject_id=(
                        f"{SUBJECT_PREFIX_CONSTRAINT_PROBE}{constraint.constraint_id}:{case.case_id}"
                    ),
                    template_key=TEMPLATE_NEGATIVE_CONSTRAINT_PROBE,
                    content={
                        "constraint_description": constraint.description,
                        "case_category": case.category.value,
                        "case_prompt": case.prompt,
                        # 模板走 `StrictUndefined`，"这条用例没有期望产出"必须由
                        # 调用方显式传空串表达（见模板的 required_variables 注释）。
                        "case_expected_output": case.expected_output or "",
                    },
                    criticality=CONSTRAINT_PROBE_CRITICALITY,
                )
            status = self._unwrap_probe(result, constraint=constraint, case=case, run_id=run_id)
            if status is None:
                outcome.undetermined.add(constraint.constraint_id)
            elif status is JudgeVerdictStatus.PASS:
                outcome.confirmed.setdefault(constraint.constraint_id, []).append(case.case_id)

        await asyncio.gather(*(_one(constraint, case) for constraint, case in pairs))
        # 排序保证确定性：`gather` 的完成次序取决于网络，不排的话同一批数据两次
        # 运行会写出两种 `covering_case_ids` 顺序，制品的 diff 就全是噪音。
        for case_ids in outcome.confirmed.values():
            case_ids.sort()
        return outcome

    def _unwrap_probe(
        self,
        result: JudgeVerdict | ConsensusResult,
        *,
        constraint: NegativeConstraint,
        case: TestCase,
        run_id: str,
    ) -> JudgeVerdictStatus | None:
        """把裁判返回值收敛成 PASS/FAIL，两种特殊情形返回 None（= 未判定）。

        1. **黄金基准盲测**：`judgmental_verdict()` 有一定概率把请求整个换成一条
           人类标定过的黄金用例来考核裁判自己。这类结果的 `subject_id` 带
           `__golden__:` 前缀，**必须跳过**——把它当成本次组合的结论，等于用另一份
           文本的判决来决定这条约束覆盖没覆盖（docs/dev/interfaces/08 第 3 节）。
        2. **共识未达成**：`ROUTINE` 走单副本，正常不会出现 `ConsensusResult`；
           真出现了也**不降级**成 PASS/FAIL（docs/dev/08 的明令禁止项）。这里记为
           未判定而不是挂起流水线——本维度不阻断合并，为一个覆盖率统计的中间结论
           把整条流水线停下来不成比例，如实报告"这条约束这次没算出结论"才是对的。
        """
        if isinstance(result, ConsensusResult) and not result.consensus_reached:
            logger.warning(
                "weighted_coverage_probe_no_consensus",
                run_id=run_id,
                constraint_id=constraint.constraint_id,
                case_id=case.case_id,
            )
            return None
        if is_golden_subject(result.subject_id):
            logger.info(
                "weighted_coverage_probe_consumed_by_golden_case",
                run_id=run_id,
                constraint_id=constraint.constraint_id,
                case_id=case.case_id,
            )
            return None
        return result.final_status if isinstance(result, ConsensusResult) else result.status

    # ------------------------------------------------------------------ #
    # 3. constraint_feedback_generation
    # ------------------------------------------------------------------ #

    async def constraint_feedback_generation(
        self, state: WeightedCoverageState
    ) -> dict[str, object]:
        """把未覆盖的负向约束反馈给 Generator（docs/dev/18 第 4 节）。

        这是 `CapabilityFocus.negative_constraint_ids`（docs/dev/06 定义、此前一直
        空着）的**首个真实调用方**。

        ## 为什么是新节点而不是复用模块六的 `feedback_driven_generation`

        与模块七遇到的是同一件事（见 `nodes/pruning/nodes.py` 里的同名说明）：
        同一张图里节点名唯一，让两条边都指向模块六那个节点，等于把它的补盲回环
        接进本维度的直线流程；而且那个节点读的是 `_coverage_blind_spots`——模块六的
        私有键，本维度读它就违反了"各维度不得读写其他维度私有键"的约定。真正该
        复用的是**服务**（`TestSuiteService.incremental_patch()`）而不是**节点**。

        ## 为什么显式把补题数写进 `positive_count`

        `incremental_patch()` 的默认映射是"每条负向约束出一条 **NEGATIVE** 用例"
        （docs/dev/06 第 4.3 节）。那个默认对本维度是错的：本项目的 `NEGATIVE`
        指的是"不该触发本 Skill"的近脱靶题，而反事实用例恰恰是**该由本 Skill
        处理**的真实请求——只是场景里埋了个坑。因此这里显式 `positive_count=约束
        条数`、`negative_count=0`，让它走正向模板（那里才有 docs/dev/18 第 4 节
        要求的"反事实场景构造"指令分支，见 `prompts/_shared.jinja`）。

        补题失败**不抛异常**（与模块六/七同一处理）：`incremental_patch()` 会在
        "这个 Skill 还没有任何 active 用例集"等情形下抛 `GenerationError`，而本
        维度不阻断合并，为一次补题失败掀掉整条流水线不成比例。
        """
        run_id = str(state["run_id"])
        node_name = NODE_NAMES["constraint_feedback_generation"]
        uncovered = _str_list(state, KEY_UNCOVERED_CONSTRAINT_IDS)
        if not uncovered:
            # 防御性：路由函数已经拦过一次。空 focus 会被 `incremental_patch()` 拒绝。
            return {}

        tree = await self._load_tree(state)
        skill = await self._load_skill(state)
        descriptions = {c.constraint_id: c.description for c in tree.negative_constraints}
        focus = CapabilityFocus(
            negative_constraint_ids=uncovered,
            # `descriptions` 是必填的（docs/dev/interfaces/06 第 1 节）：Prompt 里
            # 给模型看的必须是描述而不是裸 id——`constraint_id` 是描述文本的哈希，
            # "请为 csv-cleaner:neg-3f9ac21b0d47 构造反事实场景"没有任何信息量。
            descriptions={cid: descriptions[cid] for cid in uncovered if cid in descriptions},
        )

        try:
            new_suite = await self.deps.generator().incremental_patch(
                skill,
                focus=focus,
                triggered_by=TRIGGERED_BY_NEGATIVE_CONSTRAINT_GAP,
                positive_count=len(uncovered),
                negative_count=0,
            )
        except GenerationError as exc:
            logger.error(
                "weighted_coverage_constraint_patch_failed",
                run_id=run_id,
                node_name=node_name,
                skill_id=skill.skill_id,
                uncovered_constraint_count=len(uncovered),
                error=str(exc)[:500],
            )
            return {KEY_PATCH_FAILURE: str(exc)[:500]}

        logger.info(
            "weighted_coverage_constraint_patch_generated",
            run_id=run_id,
            node_name=node_name,
            skill_id=skill.skill_id,
            patched_constraint_count=len(uncovered),
            suite_version_id=new_suite.suite_version_id,
        )
        return {
            # 公共字段：补题落了一个新的 active 版本。本轮**不**回头重新映射
            # （无回边，见模块头第 3 条），这批新题在下一轮评测里靠自带的
            # `negative_constraint_ids` 绑定被直接认作覆盖，不花判定调用。
            "active_suite_version_id": new_suite.suite_version_id,
            KEY_CONSTRAINT_PATCHED_COUNT: len(uncovered),
        }

    # ------------------------------------------------------------------ #
    # 4. recompute_weighted_coverage
    # ------------------------------------------------------------------ #

    async def recompute_weighted_coverage(self, state: WeightedCoverageState) -> dict[str, object]:
        """按真实权重重算能力覆盖率并经 Judge 给出量化判定（docs/dev/18 第 5 节）。

        与模块六 `blind_spot_detection` 调的是**同一条规则**
        （`capability_coverage_threshold`）、**同一个算法**
        （`CapabilityTree.weighted_coverage()`），区别只在时机：那次跑在分级之前，
        这次跑在分级之后。两条判定记录靠 `inputs.tier_weighted` 区分口径。

        本节点**不再检测盲区、也不回环补盲**：那是模块六的职责，它已经带着补盲
        回环跑完了。这里只回答"把重要性算进去之后，这个覆盖率还达标吗"——一份
        P0 能力全空、P2 能力全满的测试集，在等权口径下可能刚好及格，加权之后就
        原形毕露，而那正是本维度存在的意义。

        判定结论写进状态（`KEY_VERDICT_STATUS`）供 finalize 直接取用，finalize
        **不再自己比一次大小**：同一个口径只允许有一处实现，两处早晚会漂移
        （`docs/dev/interfaces/16` 第 4.2 节点名过这条坑）。
        """
        run_id = str(state["run_id"])
        tree = await self._load_tree(state)
        threshold = self.deps.settings().min_coverage_ratio
        ratio = tree.weighted_coverage()

        verdict = self.deps.judge().quantitative_verdict(
            subject_id=f"{SUBJECT_PREFIX_WEIGHTED}{tree.skill_id}",
            rule_name=rules.RULE_CAPABILITY_COVERAGE,
            inputs=rules.coverage_inputs(
                coverage_ratio=ratio,
                threshold=threshold,
                tier_weighted=tree.tier_grading_applied(),
            ),
        )
        # `quantitative_verdict()` 按约定不落库（docs/dev/interfaces/08 第 1 节），
        # 但状态里要带走它的 id——不存的话那个 id 指向一条谁也查不到的记录。
        await self.deps.judge_repository.save_verdict(verdict)

        logger.info(
            "weighted_coverage_recomputed",
            run_id=run_id,
            node_name=NODE_NAMES["recompute_weighted_coverage"],
            skill_id=tree.skill_id,
            capability_count=len(tree.nodes),
            weighted_ratio=round(ratio, 4),
            threshold=threshold,
            tier_graded=tree.tier_grading_applied(),
            status=verdict.status.value,
        )
        return {
            KEY_RATIO: ratio,
            KEY_NODE_COUNT: len(tree.nodes),
            KEY_TIER_GRADED: tree.tier_grading_applied(),
            KEY_VERDICT_STATUS: verdict.status.value,
            "judge_verdict_ids": [verdict.verdict_id],
        }

    # ------------------------------------------------------------------ #
    # 5. upgrade_combinatorial_priority
    # ------------------------------------------------------------------ #

    async def upgrade_combinatorial_priority(
        self, state: WeightedCoverageState
    ) -> dict[str, object]:
        """用真实权重重排组合缺口（docs/dev/18 第 6 节）。

        模块七的组合矩阵跑在权重分级**之前**，那时树上全是占位 tier，它的截断
        只能退化成"按 id 排"，报告会如实写"未做优先级筛选的截断分析"。本节点在
        分级之后重做一次排序，第一次得到"P0×P0 排在最前"的真实缺口清单。

        ## 为什么读 `combinatorial_pairs_covered` 而不是重扫用例

        模块七已经把**全部**已覆盖组合对落在了树上（`docs/dev/interfaces/17`
        第 4.1 节："加权覆盖率若要把组合覆盖计入，直接读它即可，不必重算"）。
        重扫一遍用例不会得到更多信息，只会多一次全表读，还会因为口径实现了两遍
        而慢慢漂移。

        ## 为什么只重排、不重新补题

        模块七在同一轮里已经补过一次组合缺口（`max_combinatorial_patch_per_round`）。
        这里再补一次，等于同一个问题在一轮评测里触发两批生成，而第二批用的还是
        第一批尚未参与映射的用例集——补出来的题大概率与第一批重复。重排的产出是
        **给人看的优先级**：下一轮评测的模块七会在真实分级的树上做截断（树已经
        落库），缺口自然按这个次序收敛。
        """
        run_id = str(state["run_id"])
        tree = await self._load_tree(state)
        limit = max(0, self.deps.settings().max_capability_pairs_for_matrix)

        covered = {as_sorted_pair(*pair) for pair in tree.combinatorial_pairs_covered}
        all_pairs = prioritized_pairs(tree)
        # 分析范围与模块七保持一致（同一个 limit、同一套排序）：本节点报的是
        # "同一个问题按新次序重排之后长什么样"，换一个范围就没法与模块七的那份
        # 报告对照着看了。
        in_scope = all_pairs[:limit]
        uncovered = [pair for pair in in_scope if pair not in covered]
        ranked = prioritized_uncovered_pairs(tree, uncovered, limit)

        logger.info(
            "weighted_coverage_pairs_reprioritized",
            run_id=run_id,
            node_name=NODE_NAMES["upgrade_combinatorial_priority"],
            skill_id=tree.skill_id,
            total_pair_count=len(all_pairs),
            in_scope_pair_count=len(in_scope),
            uncovered_pair_count=len(ranked),
            tier_graded=tree.tier_grading_applied(),
            top_pairs=[list(pair) for pair in ranked[:_MAX_LISTED_PAIRS]],
        )
        return {
            KEY_RANKED_UNCOVERED_PAIRS: [list(pair) for pair in ranked],
            KEY_RANKED_PAIR_COUNT: len(in_scope),
            KEY_TOTAL_PAIR_COUNT: len(all_pairs),
        }

    # ------------------------------------------------------------------ #
    # 6. generate_traceability_artifact
    # ------------------------------------------------------------------ #

    async def generate_traceability_artifact(
        self, state: WeightedCoverageState
    ) -> dict[str, object]:
        """落盘可追溯性矩阵 JSON/CSV（docs/dev/18 第 7 节）。

        这两份制品与 docs/dev/05 的 `benchmark.json`/HTML 走**同一套 CI 归档
        机制**——本文档不新建归档管道，只保证文件落在一个可被 `upload-artifact`
        按路径捞走的位置（docs/dev/24 统一配置）。

        **写盘失败不阻断**：结论全都在库里，制品只是一份导出。为了一个只读副本
        把整条流水线掀掉不成比例；但失败原因必须进报告，否则 CI 归档那一步会捞
        不到文件，而没有人知道为什么。
        """
        run_id = str(state["run_id"])
        node_name = NODE_NAMES["generate_traceability_artifact"]
        tree = await self._load_tree(state)
        matrix = build_traceability_matrix(tree)

        try:
            path = write_matrix(
                matrix, artifacts_dir=self.deps.settings().artifacts_dir, run_id=run_id
            )
        except OSError as exc:
            # 只接 `OSError`（目录不可写、磁盘满、路径非法）。别的异常说明矩阵
            # 构造本身出了问题，那是真 bug，不该被一个"制品可选"的借口吞掉。
            logger.error(
                "weighted_coverage_artifact_write_failed",
                run_id=run_id,
                node_name=node_name,
                artifacts_dir=self.deps.settings().artifacts_dir,
                error=str(exc)[:500],
            )
            return {KEY_ARTIFACT_FAILURE: str(exc)[:500]}

        logger.info(
            "weighted_coverage_artifact_written",
            run_id=run_id,
            node_name=node_name,
            skill_id=tree.skill_id,
            path=path,
            node_count=len(matrix["nodes"]),
            constraint_count=len(matrix["negative_constraints"]),
        )
        return {KEY_ARTIFACT_PATH: path}

    # ------------------------------------------------------------------ #
    # 7. finalize_weighted_coverage_report
    # ------------------------------------------------------------------ #

    async def finalize_weighted_coverage_report(
        self, state: WeightedCoverageState
    ) -> dict[str, object]:
        """聚合结论写进 `dimension_results`（docs/dev/18 第 8 节）。

        判定口径：

        | 情形 | status | blocking |
        |---|---|---|
        | 加权覆盖率缺失（私有键被裁掉/节点被跳过） | NEEDS_HUMAN_REVIEW | False |
        | 能力树为空（一项都没抽出来） | NEEDS_HUMAN_REVIEW | False |
        | 其余 | 取 `recompute_weighted_coverage` 那条判定的结论 | False |

        **`blocking` 恒为 False**：延续模块六/七的一贯策略（docs/dev/18 第 8 节）。
        覆盖率类维度评估的是测试是否测得全，判 FAIL 并阻断会让一个功能完全正确的
        Skill 因为"测试集还不够全"被拦下，最终结果是所有人都学会绕过这条门禁。

        `score` 填**加权**能力覆盖率。约束覆盖率不进 `score`——`BenchmarkReport`
        的分数要能横向比较，一个字段里混着两种分母的百分比就没法比了；它作为
        findings 的一行如实呈现。
        """
        run_id = str(state["run_id"])
        node_count = _int_from_state(state, KEY_NODE_COUNT)
        threshold = self.deps.settings().min_coverage_ratio
        raw_ratio = state.get(KEY_RATIO)
        ratio = float(cast("float", raw_ratio)) if raw_ratio is not None else None

        findings: list[str] = []
        needs_human = False

        if ratio is None:
            # 只可能出现在"主图状态 schema 漏了本维度私有键"或节点被跳过时。判 PASS
            # 等于把整个维度悄悄关掉，所以显式暴露给人。
            needs_human = True
            findings.append(
                "未取到加权覆盖率计算结果：请确认主图状态 schema 包含本维度私有键"
                f"（{KEY_RATIO}），见 docs/dev/interfaces/18 第 2 节。"
            )
        elif node_count == 0:
            # 空树时 `weighted_coverage()` 给 0.0。那不是"一项都没测到"，而是
            # "根本没抽出能力"——判 FAIL 会把一个评测系统自身的问题记成被测 Skill
            # 的问题。
            needs_human = True
            findings.append(
                "能力树为空，加权覆盖率不具参考意义：请人工确认这份 SKILL.md 是否"
                "缺少能力描述，或模块六的抽取调用是否异常。"
            )
        else:
            findings.append(
                f"加权能力覆盖率 {ratio:.1%}（阈值 {threshold:.0%}）："
                f"{node_count} 项声明能力按 P0=0.6 / P1=0.3 / P2=0.1 加权计算。"
            )
            findings.append(f"能力权重分布：{_format_tier_distribution(state)}。")
            if not state.get(KEY_TIER_GRADED):
                findings.append(
                    "⚠️ 本次分级后树上只出现了一个档位，加权口径实际退化为等权："
                    "请复核 Analyzer 的分级结果是否把所有能力都判成了同一档"
                    "（那等于没有分级，这个百分比与模块六的等权覆盖率完全相同）。"
                )

        findings.extend(self._constraint_findings(state))
        findings.extend(self._combinatorial_findings(state))

        artifact_failure = state.get(KEY_ARTIFACT_FAILURE)
        artifact_path = state.get(KEY_ARTIFACT_PATH)
        if artifact_failure:
            findings.append(
                f"可追溯性矩阵制品写盘失败（评测结论不受影响，全部已落库）：{artifact_failure}"
            )
        elif artifact_path:
            findings.append(f"可追溯性制品：{artifact_path}（同名 .csv 供表格工具打开）。")
        else:
            findings.append(
                "未取到可追溯性制品路径：请确认主图状态 schema 包含"
                f"{KEY_ARTIFACT_PATH}，或该节点是否被跳过。"
            )

        status = self._resolve_status(state, needs_human=needs_human)
        await self.deps.reporter().record_dimension_result(
            run_id=run_id,
            dimension=DIMENSION,
            status=status,
            score=ratio,
            findings=findings,
            # 覆盖率不足不阻断合并，理由见本方法的 docstring 与 docs/dev/18 第 8 节。
            blocking=False,
        )
        logger.info(
            "weighted_coverage_dimension_recorded",
            run_id=run_id,
            node_name=TERMINAL_NODE,
            status=status.value,
            weighted_ratio=ratio,
            findings=len(findings),
        )
        return {}

    def _constraint_findings(self, state: WeightedCoverageState) -> list[str]:
        """负向约束那几行。单独拆出来只是为了让 finalize 读得下去。"""
        count = _int_from_state(state, KEY_CONSTRAINT_COUNT)
        if count == 0:
            return [
                (
                    "未从 SKILL.md 中抽出任何负向约束（Gotchas / 避坑指南类的禁止性规则）："
                    "本项不计入评分。若这份 Skill 确有需要规避的陷阱，建议在文档中明确写出来"
                    "——没写出来的规则，测试系统无从追踪，使用它的智能体同样无从遵守。"
                )
            ]

        uncovered = _str_list(state, KEY_UNCOVERED_CONSTRAINT_IDS)
        undetermined = _str_list(state, KEY_UNDETERMINED_CONSTRAINT_IDS)
        raw_ratio = state.get(KEY_CONSTRAINT_RATIO)
        ratio = float(cast("float", raw_ratio)) if raw_ratio is not None else None
        lines = [
            (
                f"负向约束覆盖率 {ratio:.1%}：共 {count} 条禁止性规则，"
                f"未被任何反事实用例诱导的有 {len(uncovered)} 条。"
                if ratio is not None
                else f"共 {count} 条负向约束，本次未取到覆盖率计算结果。"
            )
        ]
        # 逐条列出未覆盖的约束 id，与模块六列盲区同一个理由：只写一个数字，读报告
        # 的人不知道该去补哪条规则。描述不重复写在这里（它在 traceability 制品里
        # 逐条带着），报告只给 id + 数量。
        if uncovered:
            lines.append(f"未覆盖负向约束 id：{uncovered}")
        if undetermined:
            lines.append(
                f"本次未判定的负向约束 {len(undetermined)} 条（判定预算耗尽或候选用例"
                f"被黄金基准盲测占用）：{undetermined}。它们既不算覆盖也不算盲区，"
                "下一轮评测会继续判定；若反复出现，请调大 "
                "SKILLEVAL_COVERAGE_MAX_CONSTRAINT_PROBE_CALLS。"
            )
        if state.get(KEY_PROBE_BUDGET_EXHAUSTED):
            lines.append(
                f"反事实覆盖判定调用达到单轮上限（本轮已用 "
                f"{_int_from_state(state, KEY_PROBE_CALL_COUNT)} 次）。"
            )

        patch_failure = state.get(KEY_PATCH_FAILURE)
        patched = _int_from_state(state, KEY_CONSTRAINT_PATCHED_COUNT)
        if patch_failure:
            lines.append(f"反事实用例定向补题未能执行，约束盲区维持原状：{patch_failure}")
        elif patched:
            lines.append(
                f"已针对 {patched} 条未覆盖约束补生成反事实用例；它们在**下一轮**评测中"
                "被计入约束覆盖率（本轮不回头重新映射，见 docs/dev/18 第 4 节）。"
            )
        return lines

    @staticmethod
    def _combinatorial_findings(state: WeightedCoverageState) -> list[str]:
        """组合缺口重排那几行。"""
        total = _int_from_state(state, KEY_TOTAL_PAIR_COUNT)
        if total == 0:
            return []
        in_scope = _int_from_state(state, KEY_RANKED_PAIR_COUNT)
        pairs = [
            tuple(p) for p in cast("list[list[str]]", state.get(KEY_RANKED_UNCOVERED_PAIRS) or [])
        ]
        lines = [
            (
                "能力组合缺口已按真实权重重排（替换模块七的过渡截断策略）："
                f"分析范围 {in_scope}/{total} 对，其中 {len(pairs)} 对仍未被任何一条活跃用例"
                "同时触发，P0×P0 组合排在最前。"
            )
        ]
        if pairs:
            shown = pairs[:_MAX_LISTED_PAIRS]
            lines.append(
                "最该优先补齐的组合："
                + "；".join(f"{a} + {b}" for a, b in shown)
                + (f"（其余 {len(pairs) - len(shown)} 对从略）" if len(pairs) > len(shown) else "")
            )
        return lines

    @staticmethod
    def _resolve_status(state: WeightedCoverageState, *, needs_human: bool) -> JudgeVerdictStatus:
        """维度级状态的优先级：NEEDS_HUMAN_REVIEW > 判定结论。

        与模块六 `_resolve_status()` 同一条理由：`needs_human` 表示的是"这次覆盖率
        根本没算出来/不具参考意义"，在一个算不出来的数字上判 FAIL，等于用一个假
        结论替掉"我不知道"。

        非 needs_human 的那一档**直接取判定结论**而不是重新比一次阈值：那条判定
        已经由 `JudgeAgent.quantitative_verdict()` 产出并落库，报告与判定记录必须
        是同一个结论。判定状态取不到（键被裁掉）时同样降为 NEEDS_HUMAN_REVIEW。
        """
        if needs_human:
            return JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        raw_status = state.get(KEY_VERDICT_STATUS)
        if not raw_status:
            return JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        return JudgeVerdictStatus(str(raw_status))

    # ------------------------------------------------------------------ #
    # 条件路由
    # ------------------------------------------------------------------ #

    def route_after_constraint_mapping(self, state: WeightedCoverageState) -> str:
        """`map_negative_constraint_coverage` 之后：补反事实用例，还是直接重算。

        只有"明确判定为未覆盖"的约束才触发补题。未判定的（预算耗尽 / 被黄金盲测
        占用）**不**触发：为一条可能本来就覆盖着的约束补题，既浪费一次生成调用，
        又会往测试集里塞一条多余的题——而测试集只增不减是本项目最不想要的走向。
        """
        if _str_list(state, KEY_UNCOVERED_CONSTRAINT_IDS):
            return NODE_NAMES["constraint_feedback_generation"]
        return NODE_NAMES["recompute_weighted_coverage"]

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    async def _candidate_cases(self, state: WeightedCoverageState) -> list[TestCase]:
        """取当前 active 版本里可用于反事实判定的用例（正向 + 对抗，不含 COLD）。

        `suite_version_id` 缺失时抛 `PersistenceError` 而不是静默返回空列表——
        "这些约束一条都没被测到"和"我根本没拿到测试集"是两件完全不同的事，后者
        若被当成前者，会立刻触发一轮针对**全部**约束的补题（与模块六
        `map_case_coverage` 同一条理由）。
        """
        suite_version_id = state.get("active_suite_version_id")
        if not suite_version_id:
            raise PersistenceError(
                f"{NODE_NAMES['map_negative_constraint_coverage']}：状态里没有 "
                "active_suite_version_id，无法确定该拿哪一版用例集做反事实覆盖映射。"
                "请确认主图入口节点已调用 `TestSuiteService.ensure_test_suite()` 并把"
                "版本号写进状态（docs/dev/interfaces/06 第 6 节）。"
            )
        cases = await self.deps.test_case_repository.list_by_categories(
            str(suite_version_id),
            [TestCaseCategory.POSITIVE, TestCaseCategory.ADVERSARIAL],
        )
        return [case for case in cases if case.split is not DatasetSplit.COLD]

    async def _load_skill(self, state: WeightedCoverageState) -> SkillDefinition:
        """取被测 Skill。

        与模块六一样**不**支持"用 Optimizer 的工作副本"：权重分级与负向约束描述的
        是仓库里那份 SKILL.md 声明了什么，拿一份内存里改过的版本来抽，制品就与人
        能看到的文件对不上了。
        """
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

    async def _load_tree(self, state: WeightedCoverageState) -> CapabilityTree:
        """按 `capability_tree_id` 读回能力树（与模块六/七同一套：状态里只存 id）。

        取不到就抛：本维度全部结论都建立在能力树之上，没有树时"加权覆盖率 0%""没有
        负向约束"这些结论都是假的。错误信息里点名顺序约束——本维度必须排在模块六
        之后（docs/dev/interfaces/16 第 4 节）。
        """
        tree_id = state.get("capability_tree_id")
        if not tree_id:
            raise PersistenceError(
                "状态里没有 capability_tree_id：模块八的节点必须排在模块六的 "
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
# 纯数据 / 纯函数
# --------------------------------------------------------------------------- #


class _ProbeOutcome:
    """一批反事实判定的结果汇总。

    做成一个小类而不是返回二元组：两份数据的含义完全不同（"谁覆盖了" vs "谁没
    算出结论"），元组的下标读起来全靠记。用 `dataclass` 也行，但它只在本文件内部
    流动、没有字段默认值之外的语义，普通类足够。
    """

    def __init__(self) -> None:
        # constraint_id -> 确认诱导了该约束的 case_id 列表
        self.confirmed: dict[str, list[str]] = {}
        # 至少有一次判定没能得出结论的 constraint_id
        self.undetermined: set[str] = set()


def _format_tier_distribution(state: WeightedCoverageState) -> str:
    """把权重分布格式化成报告里的一行，例如 `P0 核心 3 项 / P1 条件 5 项 / P2 防御 2 项`。"""
    raw = cast("dict[str, int] | None", state.get(KEY_TIER_DISTRIBUTION)) or {}
    labels = {
        CapabilityTier.P0_CORE.value: "P0 核心",
        CapabilityTier.P1_CONDITIONAL.value: "P1 条件",
        CapabilityTier.P2_DEFENSIVE.value: "P2 防御",
    }
    return " / ".join(f"{label} {int(raw.get(key, 0))} 项" for key, label in labels.items())


def _str_list(state: WeightedCoverageState, key: str) -> list[str]:
    """从图状态里取一个字符串列表，缺键/空值时返回空列表。"""
    raw = cast("list[str] | None", state.get(key))
    return [str(item) for item in (raw or [])]


def _int_from_state(state: WeightedCoverageState, key: str) -> int:
    """从图状态里取一个整数，缺键/空值时返回 0。

    `PipelineState` 是 TypedDict，用**变量**作键时静态类型会退化成 `object`；
    这里集中收窄一次，好过在每个调用点各写一行 cast（与模块三/五/六/七同一处理）。
    """
    value = state.get(key)
    return int(cast("int", value)) if value is not None else 0


__all__ = [
    "CONSTRAINT_PROBE_CRITICALITY",
    "ENTRY_NODE",
    "NODE_NAMES",
    "SUBJECT_PREFIX_CONSTRAINT_PROBE",
    "SUBJECT_PREFIX_WEIGHTED",
    "TEMPLATE_NEGATIVE_CONSTRAINT_PROBE",
    "TERMINAL_NODE",
    "WeightedCoveragePipeline",
]
