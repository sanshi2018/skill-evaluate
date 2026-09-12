"""模块六：能力覆盖率与测试完备性评测的节点实现（docs/dev/16）。

```
extract_capability_tree          Analyzer 拆解声明能力（节点数超阈值 → 人工审核卡片）
        ↓
map_case_coverage  ←──────────────┐   正向用例 → 能力 的双向追溯矩阵
        ↓                         │
blind_spot_detection              │   纯算术：零覆盖节点 + 覆盖率 + Judge 量化判定
        ↓                         │
  [有盲区 且 未达补盲上限?] --是--> feedback_driven_generation
        ↓否 / 补盲耗尽
finalize_dimension_report
```

## 四条贯穿本文件的关键决策

1. **本维度是全项目唯一一个带回边的维度**。回边不是为了"重试直到通过"，而是架构
   文档"反向驱动与数据飞轮闭环"的字面实现：检出盲区 → 定向出题 → 重新映射。终止
   由 `max_patch_iterations` 硬上限保证（第 7 节），而不是靠"覆盖率总会涨上去"这
   种乐观假设——架构文档明确把"拆得过细导致无限重试死锁"列为本模块的主要风险。

2. **每一轮映射都从零重算覆盖**，而不是在上一轮的树上追加。`map_case_coverage`
   进来的第一件事是把所有节点的 `covered` / `covering_case_ids` 清空。理由见该
   节点的文档——增量累加会让回环第二轮开始出现重复的 `covering_case_ids`，而
   模块七要拿这个列表做聚类，重复项会直接扭曲重叠度计算。

3. **覆盖率不阻断合并**（`blocking=False`，第 8 节）。覆盖率反映的是"测试是否测得
   全"，不是"Skill 本身是否有质量问题"；一个 Skill 完全可以功能正确而测试集尚不
   完备。让测试基础设施的不完善去拖垮一次正常合并，最终结果是所有人都学会绕过这
   条门禁。

4. **判定仍然经 `JudgeAgent`**（`quantitative_verdict()`）。纯算术也要走那个入口，
   理由见 `deps.py::CoverageDeps.judge()`。

## 节点签名与返回值

与模块一~五同样的两条坑：签名必须写 `CoverageState`（否则私有键会被 LangGraph
静默裁掉，本维度的表现会是"能力树抽出来了、覆盖率却恒等于 100%"），返回值只带
增量（`judge_verdict_ids` 的 reducer 是 `operator.add`，回抛整个旧状态会让 id 翻倍）。
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from skill_evaluate.agents.analyzer.identity import (
    build_capability_tree_id,
    parse_capability_tree_id,
)
from skill_evaluate.agents.generator.schema import CapabilityFocus
from skill_evaluate.errors import GenerationError, PersistenceError, PipelineSuspended
from skill_evaluate.logging import get_logger
from skill_evaluate.nodes.coverage import rules
from skill_evaluate.nodes.coverage.deps import TRIGGERED_BY_COVERAGE_GAP, CoverageDeps
from skill_evaluate.nodes.coverage.state import (
    DIMENSION,
    KEY_BLIND_SPOTS,
    KEY_COVERAGE_RATIO,
    KEY_MAPPED_CASE_COUNT,
    KEY_PATCH_EXHAUSTED,
    KEY_PATCH_FAILURE,
    KEY_PATCH_ITERATIONS,
    KEY_TREE_NODE_COUNT,
    KEY_TREE_REVIEW_CONFIRMED,
    NODE_PREFIX,
    BlindSpot,
    CoverageState,
)
from skill_evaluate.persistence.suspension import suspend_and_wait
from skill_evaluate.state.capability import CapabilityTree
from skill_evaluate.state.enums import JudgeVerdictStatus, TestCaseCategory
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase

logger = get_logger(component=DIMENSION)

# 节点名前缀用 `NODE_PREFIX`（`coverage`）而不是 `DIMENSION`
# （`capability_coverage`）：模块六/七/八在主图里被装配为同一个 `coverage` 子图
# 分区（docs/dev/16 第 3 节），三份文档的节点必须共用前缀才能被一眼归到一起。
# 文档 17/18 追加节点时请沿用 `f"{NODE_PREFIX}.<node>"`，并确认名字不与下表重复。
NODE_NAMES = {
    "extract_capability_tree": f"{NODE_PREFIX}.extract_capability_tree",
    "map_case_coverage": f"{NODE_PREFIX}.map_case_coverage",
    "blind_spot_detection": f"{NODE_PREFIX}.blind_spot_detection",
    "feedback_driven_generation": f"{NODE_PREFIX}.feedback_driven_generation",
    "finalize_dimension_report": f"{NODE_PREFIX}.finalize_dimension_report",
}

ENTRY_NODE = NODE_NAMES["extract_capability_tree"]
TERMINAL_NODE = NODE_NAMES["finalize_dimension_report"]

# 覆盖率判定的 `subject_id` 前缀（约定同 docs/dev/interfaces/13 第 6.1 节）。
# 判定对象是**整个 Skill 的测试集**而不是某一条用例，裸用 skill_id 会与将来
# 任何"以 skill 为主体"的判定（文档 17 的瘦身结论、文档 18 的加权覆盖率）在
# `JudgeRepository.list_verdicts()` 里混成一堆。
SUBJECT_PREFIX_COVERAGE = "coverage:"

# 人工审核卡片的 resume payload 里被认作"确认继续"的取值（docs/dev/16 第 4 节）。
# 形状约定尽量宽容，留给 docs/dev/22 的审批工作台；解析逻辑见 `_is_tree_confirmed()`。
RESUME_CONFIRM = "confirm"


class CoveragePipeline:
    """模块六的五个节点。做成类是为了让依赖注入只发生一次（构造时）。

    用法（docs/dev/24 装配主图时）见 `graph.py::add_coverage_nodes()`。
    """

    def __init__(self, deps: CoverageDeps | None = None) -> None:
        self.deps = deps or CoverageDeps()
        # 装配期就核对路由声明。配错的表现不是报错而是"主图为一个永远用不上的执行
        # 后端做准备"，跑完也没人发现，因此只能在建图时拦。
        CoverageDeps.assert_backend_routing()

    # ------------------------------------------------------------------ #
    # 1. extract_capability_tree
    # ------------------------------------------------------------------ #

    async def extract_capability_tree(self, state: CoverageState) -> dict[str, object]:
        """拆解声明能力树并落库（docs/dev/16 第 4 节）。

        节点数超过 `capability_count_review_threshold` 时挂起等人工确认——这是架构
        文档给模块六的"应对方案"原文：**当系统提取出的能力树超过一定层级或数量时，
        交由人类开发者确认后再进行映射计算**。复用 docs/dev/04/09 已建立的
        `suspend_and_wait()` 通用挂起机制，不新造一套审批逻辑。

        ⚠️ **挂起后本节点会整体重跑一次**。这是 LangGraph 动态 `interrupt()` 的语义
        （恢复时从节点开头重新执行，而不是从 `interrupt()` 那一行继续），全项目所有
        挂起点都是如此。对本节点的实际影响是多花一次抽取调用；能力树本身不会因此
        错乱，因为 `capability_id` 是描述文本的确定性哈希，同一份 SKILL.md 重抽得到
        同一批 id（见 `agents/analyzer/identity.py`），落库又是按
        `(skill_id, skill_version_ref)` 的 upsert。
        """
        run_id = str(state["run_id"])
        skill = await self._load_skill(state)

        tree = await self.deps.analyzer().extract_capability_tree(skill)
        await self.deps.capability_repository.save(tree)
        tree_id = build_capability_tree_id(tree.skill_id, tree.skill_version_ref)

        updates: dict[str, object] = {
            "capability_tree_id": tree_id,
            KEY_TREE_NODE_COUNT: len(tree.nodes),
        }

        threshold = self.deps.settings().capability_count_review_threshold
        if len(tree.nodes) > threshold:
            await self._suspend_for_tree_review(run_id, tree, threshold)
            updates[KEY_TREE_REVIEW_CONFIRMED] = True

        logger.info(
            "coverage_capability_tree_ready",
            run_id=run_id,
            node_name=NODE_NAMES["extract_capability_tree"],
            skill_id=tree.skill_id,
            capability_tree_id=tree_id,
            capability_count=len(tree.nodes),
            human_reviewed=bool(updates.get(KEY_TREE_REVIEW_CONFIRMED)),
        )
        return updates

    async def _suspend_for_tree_review(
        self, run_id: str, tree: CapabilityTree, threshold: int
    ) -> None:
        """能力树规模超阈值的人工审核卡片。

        与 `OptimizationLoop._suspend()` 同一套写法：先在 `human_approvals` 落一条
        待办（docs/dev/22 的审批工作台按 `wait_key` 找到它），再调
        `suspend_and_wait()` 交出控制权。

        **默认不通过**（`_is_tree_confirmed()` 只认明确的确认信号）：一棵没被明确
        确认过的树若被当成确认过的继续算下去，得到的是一份看起来正常、实际建立在
        错误粒度之上的覆盖率报告。而拒绝的代价只是这个维度停在这里等人——本维度
        不阻断合并，停下来不会卡住任何人的 CI。
        """
        node_name = NODE_NAMES["extract_capability_tree"]
        wait_key = f"{run_id}:{NODE_PREFIX}:tree_review"
        await self.deps.approval_repository.create(
            run_id=run_id,
            node_name=node_name,
            # thread_id 沿用 run_id：主图以 run_id 作 LangGraph thread_id
            # （docs/dev/04），审批工作台唤醒时要拿它去 `Command(resume=...)`。
            thread_id=run_id,
            wait_key=wait_key,
        )
        logger.warning(
            "coverage_capability_tree_size_exceeded",
            run_id=run_id,
            node_name=node_name,
            skill_id=tree.skill_id,
            capability_count=len(tree.nodes),
            threshold=threshold,
            wait_key=wait_key,
        )

        decision: Any = await suspend_and_wait(
            reason=f"capability_tree_size_exceeds_threshold:{len(tree.nodes)}",
            wait_key=wait_key,
        )
        if not _is_tree_confirmed(decision):
            raise PipelineSuspended(
                f"{node_name}：能力树含 {len(tree.nodes)} 项能力（阈值 {threshold}），"
                f"人工未确认该拆解粒度，run_id={run_id}。"
                "继续按这棵树计算覆盖率会得到一份永远补不满的盲区清单，因此就此停下。"
            )

    # ------------------------------------------------------------------ #
    # 2. map_case_coverage
    # ------------------------------------------------------------------ #

    async def map_case_coverage(self, state: CoverageState) -> dict[str, object]:
        """双向追溯矩阵：正向用例 → 能力（docs/dev/16 第 5 节）。

        **只用正向用例**，与架构文档"遍历所有的正向测试用例"一致。反向用例
        （should-not-trigger）的语义是"这类请求不该由本 Skill 处理"，拿它去证明某项
        能力被覆盖是自相矛盾的；对抗用例、渐进式披露探查用例同理各有各的判定维度。

        ## 两处相对 docs/dev/16 第 5 节伪码的修正

        1. **每轮清空覆盖状态后重算**。伪码直接在读回来的树上 `covered = True` /
           `covering_case_ids.append(...)`，而这个节点在补盲回环里会被跑第二、第三
           次，读回来的树已经带着上一轮的结果——同一条用例会被重复 append 进
           `covering_case_ids`。模块七要拿这个列表做用例聚类，重复项会直接扭曲重叠
           度。清空重算是幂等的，代价只是一个内存循环。

        2. **"要不要发 LLM 请求"与"要不要标记覆盖"分开判断**。伪码用
           `if case.target_capability_ids: continue` 一次性跳过了两件事，于是
           已经带着 `target_capability_ids` 的用例（补盲生成的新用例就是这样——
           Generator 按 `CapabilityFocus` 的要求在出题时就回填了）永远不会去标记它
           们本该覆盖的节点。后果是补盲回环怎么跑覆盖率都不涨，一直空转到迭代上限。
           正确的语义是：**已有映射的跳过 LLM 调用，但照常参与覆盖标记。**
        """
        run_id = str(state["run_id"])
        tree = await self._load_tree(state)
        suite_version_id = state.get("active_suite_version_id")
        if not suite_version_id:
            raise PersistenceError(
                f"{NODE_NAMES['map_case_coverage']}：状态里没有 active_suite_version_id，"
                "无法确定该拿哪一版用例集做覆盖映射。请确认主图入口节点已调用 "
                "`TestSuiteService.ensure_test_suite()` 并把版本号写进状态"
                "（docs/dev/interfaces/06 第 6 节）。"
            )

        cases = await self.deps.test_case_repository.list_by_categories(
            str(suite_version_id), [TestCaseCategory.POSITIVE]
        )

        # 修正 1：清空后重算，保证本节点幂等。
        for node in tree.nodes:
            node.covered = False
            node.covering_case_ids = []

        # 修正 2：只有"还没有映射"的用例需要一次 LLM 调用。
        pending = [case for case in cases if not case.target_capability_ids]
        await self._map_pending_cases(pending, tree)

        known = {node.capability_id: node for node in tree.nodes}
        for case in cases:
            for capability_id in case.target_capability_ids:
                # 变量名与上面的清空循环刻意不同：那一个是 `CapabilityNode`，
                # 这一个可能是 None（旧绑定指向已消失的节点）。
                target = known.get(capability_id)
                if target is None:
                    # 能力树重抽后 id 变了（SKILL.md 改写过某项能力的描述），旧绑定
                    # 指向一个已不存在的节点。本文档只是跳过并记日志；这正是模块七
                    # "反向孤儿用例检测"的输入信号，由那份文档决定要不要淘汰该用例。
                    logger.info(
                        "coverage_orphan_case_binding",
                        run_id=run_id,
                        case_id=case.case_id,
                        capability_id=capability_id,
                    )
                    continue
                target.covered = True
                if case.case_id not in target.covering_case_ids:
                    target.covering_case_ids.append(case.case_id)

        await self.deps.capability_repository.save(tree)
        logger.info(
            "coverage_cases_mapped",
            run_id=run_id,
            node_name=NODE_NAMES["map_case_coverage"],
            suite_version_id=str(suite_version_id),
            positive_case_count=len(cases),
            newly_mapped=len(pending),
            covered_nodes=sum(1 for n in tree.nodes if n.covered),
        )
        return {KEY_MAPPED_CASE_COUNT: len(cases)}

    async def _map_pending_cases(self, pending: list[TestCase], tree: CapabilityTree) -> None:
        """并发做用例→能力映射，并把结果回填进 `TestCase.target_capability_ids`。

        并发有上限（`max_concurrent_mappings`）：每条用例一次 LLM 调用，几十条题
        全量 `gather` 打出去会撞供应商限速，与模块一/三给沙箱设并发上限是同一种
        "评测系统自身的资源节流"。

        落库是**逐条**的（映射一条存一条）而不是最后批量存：本节点可能被挂起或
        崩溃在中途，已经花掉的调用应当留下结果，下一轮回环靠 `target_capability_ids`
        非空就能跳过它们。这也是 docs/dev/02 说 `TestCase.target_capability_ids`
        "由模块六首个真实写入"的落点。
        """
        if not pending:
            return
        semaphore = asyncio.Semaphore(max(1, self.deps.settings().max_concurrent_mappings))
        analyzer = self.deps.analyzer()

        async def _map_one(case: TestCase) -> None:
            async with semaphore:
                capability_ids = await analyzer.map_case_to_capabilities(case, tree)
            case.target_capability_ids = capability_ids
            await self.deps.test_case_repository.save(case)

        await asyncio.gather(*(_map_one(case) for case in pending))

    # ------------------------------------------------------------------ #
    # 3. blind_spot_detection
    # ------------------------------------------------------------------ #

    async def blind_spot_detection(self, state: CoverageState) -> dict[str, object]:
        """计算零覆盖节点与覆盖率，并经 Judge 给出量化判定（docs/dev/16 第 6 节）。

        docs/dev/16 第 6 节把这个节点写成同步函数（伪码里是 `def` + 一句
        `tree = ...  # 同步读取`）。这里落地为 `async def`：能力树在 Postgres 里，
        本项目的仓储层只有异步接口，硬做成同步节点就得在事件循环里阻塞一次数据库
        读——为了一个纯算术函数的形式好看，换来一次会拖住整张图的阻塞调用，不划算。
        节点体内的判定逻辑本身仍然是纯算术，没有 LLM。

        空能力树（`nodes` 为空）时覆盖率按第 6 节的公式取 1.0。这个数字是个陷阱：
        它并不意味着"测得很全"，而意味着"根本没抽出能力"。因此把节点数一并放进
        状态（`KEY_TREE_NODE_COUNT`），由 finalize 据此判 `NEEDS_HUMAN_REVIEW` 而
        不是 PASS——详见 `finalize_dimension_report`。

        ## 覆盖率算法：`CapabilityTree.weighted_coverage()`（docs/dev/18 接入）

        docs/dev/16 落地时这里算的是未加权的简单比例；docs/dev/18 落地后改为调
        `weighted_coverage()`，与量化规则、与模块八的重算口径**共用同一个算法**。

        本节点跑在权重分级**之前**（分级是模块八子图的第一个节点，排在模块六全部
        跑完之后），因此此刻树上全是占位 tier，加权结果与等权结果数值相同。即便
        如此也要调同一个方法：分级一旦生效，"判定用的算法"与"报告里写的数"就必须
        是同一个来源——`docs/dev/interfaces/16` 第 4.2 节点名了这条坑（规则改了而
        节点里的算法没改，判定与报告会给出两个不同的数）。

        口径标记 `tier_weighted` 按树的实际状态传（`tier_grading_applied()`），
        不写死：写死 True 会让这条"其实没分过级"的记录看起来像加权判定。
        """
        run_id = str(state["run_id"])
        tree = await self._load_tree(state)
        threshold = self.deps.settings().min_coverage_ratio

        blind_spots = [
            BlindSpot(capability_id=node.capability_id, description=node.description)
            for node in tree.nodes
            if not node.covered
        ]
        # 空树取 1.0 而不是 weighted_coverage() 的 0.0：docs/dev/16 第 6 节的公式
        # 如此，且"没有盲区"与"没有能力"由 finalize 靠节点数区分（见上）。把空树
        # 判成 0% 会让它显示为一个刺眼却同样错误的分数，并直接触发一轮补不出任何
        # 东西的补盲回环。
        coverage_ratio = tree.weighted_coverage() if tree.nodes else 1.0

        verdict = self.deps.judge().quantitative_verdict(
            subject_id=f"{SUBJECT_PREFIX_COVERAGE}{tree.skill_id}",
            rule_name=rules.RULE_CAPABILITY_COVERAGE,
            inputs=rules.coverage_inputs(
                coverage_ratio=coverage_ratio,
                threshold=threshold,
                tier_weighted=tree.tier_grading_applied(),
            ),
        )
        # `quantitative_verdict()` 按约定不落库（docs/dev/interfaces/08 第 1 节），
        # 但本维度每轮只产生一条判定，而状态里要带走它的 id——不存的话那个 id 指向
        # 一条谁也查不到的记录。与模块四/五同样的处理。
        await self.deps.judge_repository.save_verdict(verdict)

        logger.info(
            "coverage_blind_spots_detected",
            run_id=run_id,
            node_name=NODE_NAMES["blind_spot_detection"],
            skill_id=tree.skill_id,
            capability_count=len(tree.nodes),
            blind_spot_count=len(blind_spots),
            coverage_ratio=round(coverage_ratio, 4),
            threshold=threshold,
            tier_weighted=tree.tier_grading_applied(),
            status=verdict.status.value,
        )
        return {
            KEY_BLIND_SPOTS: [spot.model_dump() for spot in blind_spots],
            KEY_COVERAGE_RATIO: coverage_ratio,
            KEY_TREE_NODE_COUNT: len(tree.nodes),
            "judge_verdict_ids": [verdict.verdict_id],
        }

    # ------------------------------------------------------------------ #
    # 4. feedback_driven_generation
    # ------------------------------------------------------------------ #

    async def feedback_driven_generation(self, state: CoverageState) -> dict[str, object]:
        """把盲区打包成硬性约束反馈给 Generator（docs/dev/16 第 7 节）。

        这是 `docs/dev/interfaces/06` 第 0 节允许流水线**自动**触发生成的唯一场景：
        `INCREMENTAL_PATCH` 有明确理由（覆盖率盲区）且只补盲区。不要顺手把它当成
        "变相的自动重生"——构造一个覆盖全部能力的 focus 会破坏整个复用约束。

        `descriptions` 是必填的（同一份接口文档第 1 节）：Prompt 里给模型看的是描述
        而不是裸 id，缺失的 id 会退化成 id 原文，而 `capability_id` 是一串哈希，
        对模型没有任何信息量——出出来的题也就补不到真正的盲区。docs/dev/16 第 7 节
        的伪码漏了这个参数，这里补上。

        补盲失败**不抛异常**：`incremental_patch()` 会在"这个 Skill 还没有任何
        active 用例集"等情形下抛 `GenerationError`。本维度不阻断合并，为一次补题
        失败把整条流水线掀掉不成比例；正确处理是标记耗尽、把原因写进报告，让覆盖率
        以当前的真实值收尾。
        """
        run_id = str(state["run_id"])
        iteration = _int_from_state(state, KEY_PATCH_ITERATIONS)
        max_iterations = self.deps.settings().max_patch_iterations

        # 防御性上限。正常路径上路由函数已经拦过一次（见 `route_after_blind_spots`），
        # 这里再拦是因为"无限重试死锁"是架构文档给本模块点名的风险：这条上限值得
        # 在唯一会推高迭代次数的地方再写一遍，而不是完全托付给图的连线。
        if iteration >= max_iterations:
            logger.warning(
                "coverage_patch_iterations_exhausted",
                run_id=run_id,
                node_name=NODE_NAMES["feedback_driven_generation"],
                iteration=iteration,
                max_iterations=max_iterations,
            )
            return {KEY_PATCH_EXHAUSTED: True}

        blind_spots = self._blind_spots(state)
        if not blind_spots:
            # 同样是防御性的：没有盲区就没有 focus，而空 focus 会被
            # `incremental_patch()` 拒绝（"无盲区可补"）。
            return {KEY_PATCH_EXHAUSTED: True}

        skill = await self._load_skill(state)
        focus = CapabilityFocus(
            capability_ids=[spot.capability_id for spot in blind_spots],
            descriptions={spot.capability_id: spot.description for spot in blind_spots},
        )

        try:
            new_suite = await self.deps.generator().incremental_patch(
                skill, focus=focus, triggered_by=TRIGGERED_BY_COVERAGE_GAP
            )
        except GenerationError as exc:
            logger.error(
                "coverage_patch_generation_failed",
                run_id=run_id,
                node_name=NODE_NAMES["feedback_driven_generation"],
                skill_id=skill.skill_id,
                blind_spot_count=len(blind_spots),
                error=str(exc)[:500],
            )
            return {KEY_PATCH_EXHAUSTED: True, KEY_PATCH_FAILURE: str(exc)[:500]}

        logger.info(
            "coverage_patch_generated",
            run_id=run_id,
            node_name=NODE_NAMES["feedback_driven_generation"],
            skill_id=skill.skill_id,
            blind_spot_count=len(blind_spots),
            iteration=iteration + 1,
            suite_version_id=new_suite.suite_version_id,
        )
        return {
            # 公共字段：补盲落了一个新的 active 版本，后续节点（包括本维度的下一轮
            # 映射）都要用它。主图里跑在本维度之后的维度也会看到这个新版本——这正是
            # 数据飞轮的意图，不是副作用。
            "active_suite_version_id": new_suite.suite_version_id,
            KEY_PATCH_ITERATIONS: iteration + 1,
        }

    # ------------------------------------------------------------------ #
    # 5. finalize_dimension_report
    # ------------------------------------------------------------------ #

    async def finalize_dimension_report(self, state: CoverageState) -> dict[str, object]:
        """聚合结论写进 `dimension_results`（docs/dev/16 第 8 节）。

        判定口径：

        | 情形 | status | blocking |
        |---|---|---|
        | 能力树为空（一项都没抽出来） | NEEDS_HUMAN_REVIEW | False |
        | 覆盖率缺失（私有键被裁掉/节点被跳过） | NEEDS_HUMAN_REVIEW | False |
        | 覆盖率 < 阈值 | FAIL | False |
        | 其余 | PASS | False |

        **`blocking` 恒为 False**（docs/dev/16 第 8 节的阻断策略）。覆盖率反映"测试
        是否测得全"，而不是"Skill 本身是否有质量问题"。这一策略在 docs/dev/24 落地
        CI 门禁时可按项目成熟度收紧，本文档只定默认值。

        `score` 填覆盖率本身：本维度是少数几个有真实连续分数的维度之一，报告里
        `coverage_summary` 也从这里取数。
        """
        run_id = str(state["run_id"])
        blind_spots = self._blind_spots(state)
        node_count = _int_from_state(state, KEY_TREE_NODE_COUNT)
        threshold = self.deps.settings().min_coverage_ratio
        raw_ratio = state.get(KEY_COVERAGE_RATIO)
        coverage_ratio = float(cast("float", raw_ratio)) if raw_ratio is not None else None

        findings: list[str] = []
        needs_human = False

        if coverage_ratio is None:
            # 只可能出现在"主图状态 schema 漏了本维度私有键"或节点被跳过时。判 PASS
            # 等于把整个维度悄悄关掉，所以显式暴露给人。
            needs_human = True
            findings.append(
                "未取到覆盖率计算结果：请确认主图状态 schema 包含本维度私有键"
                f"（{KEY_COVERAGE_RATIO}），见 docs/dev/interfaces/16 第 2 节。"
            )
        elif node_count == 0:
            # 空树时公式给出 1.0，但那不是"测得全"而是"没抽出能力"。报 PASS 会让一
            # 份根本没做覆盖分析的运行看起来满分。
            needs_human = True
            findings.append(
                "Analyzer 未从 SKILL.md 抽出任何声明能力，本次覆盖率不具参考意义："
                "请人工确认这份 SKILL.md 是否缺少能力描述，或抽取调用是否异常。"
            )
        else:
            findings.append(
                f"能力覆盖率 {coverage_ratio:.1%}（阈值 {threshold:.0%}）："
                f"{node_count - len(blind_spots)}/{node_count} 项声明能力被正向用例覆盖，"
                f"参与映射的正向用例 {_int_from_state(state, KEY_MAPPED_CASE_COUNT)} 条。"
                # 本维度跑在权重分级之前，这里的百分比恒为等权口径；加权口径由
                # 模块八（docs/dev/18）在自己的 `weighted_coverage` 维度里另算一份。
                "（等权口径：每项能力权重相同；按重要性加权的口径见 weighted_coverage 维度。）"
            )

        findings.extend(
            f"零覆盖能力 {spot.capability_id}：{spot.description}" for spot in blind_spots
        )

        if state.get(KEY_TREE_REVIEW_CONFIRMED):
            findings.append(f"能力树规模超过阈值（{node_count} 项），已由人工确认拆解粒度后继续。")
        patch_failure = state.get(KEY_PATCH_FAILURE)
        if patch_failure:
            findings.append(f"定向补盲未能执行，盲区维持原状：{patch_failure}")
        elif state.get(KEY_PATCH_EXHAUSTED) or (
            blind_spots
            and _int_from_state(state, KEY_PATCH_ITERATIONS)
            >= self.deps.settings().max_patch_iterations
        ):
            findings.append(
                f"已达最大补盲迭代次数（{self.deps.settings().max_patch_iterations} 次）"
                "仍存在零覆盖能力：请人工判断是能力树切分过细，还是测试集确实不足"
                "（docs/dev/16 第 7 节）。"
            )

        status = self._resolve_status(
            coverage_ratio=coverage_ratio, threshold=threshold, needs_human=needs_human
        )
        await self.deps.reporter().record_dimension_result(
            run_id=run_id,
            dimension=DIMENSION,
            status=status,
            score=coverage_ratio,
            findings=findings,
            # 覆盖率不足不阻断合并，理由见本方法的 docstring 与 docs/dev/16 第 8 节。
            blocking=False,
        )
        logger.info(
            "coverage_dimension_recorded",
            run_id=run_id,
            node_name=TERMINAL_NODE,
            status=status.value,
            coverage_ratio=coverage_ratio,
            blind_spot_count=len(blind_spots),
            findings=len(findings),
        )
        return {}

    @staticmethod
    def _resolve_status(
        *, coverage_ratio: float | None, threshold: float, needs_human: bool
    ) -> JudgeVerdictStatus:
        """维度级状态的优先级：NEEDS_HUMAN_REVIEW > FAIL > PASS。

        与模块二（FAIL 优先）**相反**，因为这里的 `needs_human` 表示的是"这次覆盖率
        根本没算出来/不具参考意义"，而不是"另有一项要人确认"。在一个算不出来的数字
        上判 FAIL，等于用一个假结论替掉"我不知道"。
        """
        if needs_human or coverage_ratio is None:
            return JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        return JudgeVerdictStatus.PASS if coverage_ratio >= threshold else JudgeVerdictStatus.FAIL

    # ------------------------------------------------------------------ #
    # 条件路由
    # ------------------------------------------------------------------ #

    def route_after_blind_spots(self, state: CoverageState) -> str:
        """`blind_spot_detection` 之后：回环补盲，还是直接收尾。

        两个"否"条件（没有盲区 / 补盲次数已用尽）都直接去 finalize。次数上限是
        架构文档点名风险"无限重试死锁"的唯一防线，因此写在图的路由上而不是藏在
        节点里——图结构本身就能回答"这个环会不会一直转下去"。
        """
        blind_spots = self._blind_spots(state)
        if not blind_spots:
            return NODE_NAMES["finalize_dimension_report"]
        if state.get(KEY_PATCH_EXHAUSTED):
            return NODE_NAMES["finalize_dimension_report"]
        if (
            _int_from_state(state, KEY_PATCH_ITERATIONS)
            >= self.deps.settings().max_patch_iterations
        ):
            return NODE_NAMES["finalize_dimension_report"]
        return NODE_NAMES["feedback_driven_generation"]

    @staticmethod
    def route_after_feedback(state: CoverageState) -> str:
        """`feedback_driven_generation` 之后：回到映射，还是收尾。

        补盲成功 → 回 `map_case_coverage` 重新映射（新用例带着出题时回填的
        `target_capability_ids`，不会再花一次 LLM 调用）。补盲被判定耗尽/失败 →
        直接收尾，**不能**回环，否则那个环就没有出口了。
        """
        if state.get(KEY_PATCH_EXHAUSTED):
            return NODE_NAMES["finalize_dimension_report"]
        return NODE_NAMES["map_case_coverage"]

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    async def _load_skill(self, state: CoverageState) -> SkillDefinition:
        """取被测 Skill。

        本维度**不**支持"用 Optimizer 的工作副本"那套（模块一的 `_working_skill`）：
        能力树描述的是仓库里那份 SKILL.md 声明了什么，拿一份内存里改过的版本来抽，
        覆盖率就与人能看到的文件对不上了。
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

    async def _load_tree(self, state: CoverageState) -> CapabilityTree:
        """按 `capability_tree_id` 读回能力树。

        状态里只存 id、每次用时回读，而不是把整棵树塞进 `CoverageState`：这是
        docs/dev/02 第 11 节"状态里只存 ID/引用"的约定，直接决定了 Checkpoint 体积。
        树在补盲回环里会被写回多次，回读也保证了拿到的一定是最新一版。
        """
        tree_id = state.get("capability_tree_id")
        if not tree_id:
            raise PersistenceError(
                f"状态里没有 capability_tree_id：{NODE_NAMES['extract_capability_tree']} "
                "未执行或其返回值被丢弃。"
            )
        skill_id, version_ref = parse_capability_tree_id(str(tree_id))
        tree = await self.deps.capability_repository.get(skill_id, version_ref)
        if tree is None:
            raise PersistenceError(
                f"未找到能力树：capability_tree_id={tree_id!r}。"
                "它应当由 extract_capability_tree 节点落库。"
            )
        return tree

    @staticmethod
    def _blind_spots(state: CoverageState) -> list[BlindSpot]:
        raw = cast("list[object] | None", state.get(KEY_BLIND_SPOTS))
        return [BlindSpot.model_validate(item) for item in (raw or [])]


def _int_from_state(state: CoverageState, key: str) -> int:
    """从图状态里取一个整数，缺键/空值时返回 0。

    `PipelineState` 是 TypedDict，用**变量**作键时静态类型会退化成 `object`。
    这里集中收窄一次，好过在每个调用点各写一行 cast（与模块三/五同一处理）。
    """
    value = state.get(key)
    return int(cast("int", value)) if value is not None else 0


def _is_tree_confirmed(decision: Any) -> bool:
    """解析人工回传的 resume payload。

    形状约定留给 docs/dev/22 的审批工作台，尽量宽容：`"confirm"`、
    `{"decision": "confirm"}`、`{"confirmed": true}` 都算确认；其余一律算未确认。
    **默认未确认**是刻意的——一棵没被明确确认过的能力树不该因为 payload 形状没对上
    就被当成确认过的（与 `optimizer/loop.py::_is_adopt()` 同一条理由）。
    """
    if decision is None:
        return False
    if isinstance(decision, str):
        return decision.strip().lower() == RESUME_CONFIRM
    if isinstance(decision, dict):
        if decision.get("confirmed") is True:
            return True
        value = decision.get("decision")
        return isinstance(value, str) and value.strip().lower() == RESUME_CONFIRM
    return False


__all__ = [
    "ENTRY_NODE",
    "NODE_NAMES",
    "RESUME_CONFIRM",
    "SUBJECT_PREFIX_COVERAGE",
    "TERMINAL_NODE",
    "CoveragePipeline",
]
