"""模块八的私有状态命名空间（docs/dev/18；命名约定沿用 docs/dev/11 第 3 节）。

三条约定，与模块一~七一致：

1. 私有键一律以下划线 + 维度前缀（`_weighted_coverage_`）开头，表示"本维度临时
   状态"，不属于 docs/dev/02 `PipelineState` 的正式字段；
2. 各维度**不得**读写其他维度的私有键（本维度尤其要克制：它与模块六/七共用
   `coverage` 图分区与同一棵能力树，但**不读** `_coverage_*` / `_pruning_*` 的
   任何一个键——耦合点只有落库的 `CapabilityTree` 与 `TestCase`）；
3. **不得**省略前缀直接复用公共字段名。

> docs/dev/18 的伪码里这些键写作 `_uncovered_constraint_ids` /
> `_traceability_artifact_path`（没有维度前缀）。实现按项目统一约定加上了
> `_weighted_coverage_` 前缀：主图把十个维度的状态并成一张 schema，LangGraph 的
> 默认 reducer 是"后写胜"，一个没有前缀的 `_uncovered_constraint_ids` 早晚会与
> 别的维度撞名，而撞名不报错，只是让某个维度悄悄拿到别人的数据。

⚠️ 主图（docs/dev/24）的状态 schema 必须包含本文件声明的私有键，否则它们会在进入
节点前被 LangGraph **静默裁掉**。本维度被裁掉的症状：分级与约束抽取照常跑、照常
改库（tier 真的被改写、约束真的落表），只是 finalize 拿不到任何数——报告里显示
"加权覆盖率未取到"。finalize 因此对"覆盖率取不到"判 `NEEDS_HUMAN_REVIEW` 并点名
该键，而不是判 PASS。

## 三个名字：两个沿用模块六，一个必须不同

| 常量 | 取值 | 与模块六的关系 |
|---|---|---|
| `ROUTING_KEY` | `coverage_analysis` | **沿用**（`nodes.coverage.state.ROUTING_KEY`） |
| `NODE_PREFIX` | `coverage` | **沿用**（同一个图分区） |
| `DIMENSION` | `weighted_coverage` | **必须独有** |

前两个直接从 `nodes.coverage.state` 导入而不是重新定义一份同值常量：三份覆盖率
文档共享同一棵能力树、同一条后端路由、同一个图分区，各写各的字面量会让某天有人
改了其中一处而另外两处保持沉默。

`DIMENSION` 则必须不同——`dimension_results` 表的唯一约束是 `(run_id, dimension)`，
两份文档若都写 `capability_coverage`，后跑完的会把先跑完的**整行覆盖掉**，报告里
只剩一个维度，且不会有任何报错。取值 `weighted_coverage` 沿用
`docs/dev/interfaces/16` 第 1 节当初给本文档的建议。
"""

from __future__ import annotations

from skill_evaluate.nodes.coverage.state import NODE_PREFIX, ROUTING_KEY
from skill_evaluate.state.pipeline_state import PipelineState

# `DimensionResult.dimension` 的取值。**本文档独有**，理由见模块头的表。
DIMENSION = "weighted_coverage"

# ---- 私有状态键 ----
# 能力树节点总数。finalize 要用它区分"覆盖率很高"与"根本没抽出能力"
# （空树时 `weighted_coverage()` 给 0.0，但那不是"一项都没测"而是"没东西可测"）。
KEY_NODE_COUNT = "_weighted_coverage_node_count"
# 权重分级后各档位的节点数，形如 `{"p0_core": 3, ...}`。进报告是为了让人一眼看出
# 这次分级是不是又退化成了"全填 P0"——那等于没分级，加权覆盖率也就失去意义。
KEY_TIER_DISTRIBUTION = "_weighted_coverage_tier_distribution"
# 分级是否真的生效（`CapabilityTree.tier_grading_applied()`）。为 False 时报告要
# 如实标注这个百分比其实是等权口径。
KEY_TIER_GRADED = "_weighted_coverage_tier_graded"
# 负向约束总数。0 条时覆盖率取 1.0，报告靠这个数区分"全覆盖"与"没有约束可测"。
KEY_CONSTRAINT_COUNT = "_weighted_coverage_constraint_count"
# 未被任何对抗性/正向用例诱导过的约束 id。它同时是补题节点的输入。
# 只存 id 不存描述（与模块六的 `BlindSpot` 不同）：补题节点反正要读回能力树拿
# 别的字段，描述顺手从树上取即可，没必要在 Checkpoint 里再存一份正文。
KEY_UNCOVERED_CONSTRAINT_IDS = "_weighted_coverage_uncovered_constraint_ids"
# **本轮没能判完**的约束 id（判定预算耗尽，或候选用例全被黄金盲测占用）。
# 与"未覆盖"分开：未覆盖是一个结论，未判定是"这次没算出结论"。把后者混进前者
# 会凭空生成一批补题需求，而那些约束可能本来就是覆盖着的。
KEY_UNDETERMINED_CONSTRAINT_IDS = "_weighted_coverage_undetermined_constraint_ids"
# 本轮实际消耗的约束判定调用次数（供成本回溯与阈值调参）。
KEY_PROBE_CALL_COUNT = "_weighted_coverage_probe_call_count"
# 判定预算是否被打满（`max_constraint_probe_calls`）。报告要如实标注。
KEY_PROBE_BUDGET_EXHAUSTED = "_weighted_coverage_probe_budget_exhausted"
# 本轮针对未覆盖约束定向补了几条反事实用例。
KEY_CONSTRAINT_PATCHED_COUNT = "_weighted_coverage_constraint_patched_count"
# 约束补题失败的原因。失败**不阻断**本维度，但必须出现在报告里——否则表现为
# "约束还没覆盖、系统却安静地不补了"。
KEY_PATCH_FAILURE = "_weighted_coverage_patch_failure"
# 加权能力覆盖率，[0, 1]。它是本维度 `DimensionResult.score` 的取值。
KEY_RATIO = "_weighted_coverage_ratio"
# 负向约束覆盖率，[0, 1]。**不并入** `KEY_RATIO`：两者的分母是两件不同的东西，
# 合成一个百分比后，未达标时读报告的人无从判断该补能力用例还是补反事实用例。
KEY_CONSTRAINT_RATIO = "_weighted_coverage_constraint_ratio"
# 加权覆盖率判定（走 `capability_coverage_threshold` 量化规则）的结论。
# finalize 直接用它而不是自己再比一次大小——同一个口径只允许有一处实现。
KEY_VERDICT_STATUS = "_weighted_coverage_verdict_status"
# 真实分级之后重排出来的未覆盖组合对（前 N 对，P0×P0 在最前）。
# 存 `list[list[str]]` 而不是 `list[tuple[str, str]]`：Checkpoint 走 JSON 序列化，
# 元组回来就是列表，在类型注解上假装它还是元组只会让比较逻辑在某个分支上悄悄失败。
KEY_RANKED_UNCOVERED_PAIRS = "_weighted_coverage_ranked_uncovered_pairs"
# 重排范围内的组合对总数与全量总数（截断与否靠两者是否相等判断）。
KEY_RANKED_PAIR_COUNT = "_weighted_coverage_ranked_pair_count"
KEY_TOTAL_PAIR_COUNT = "_weighted_coverage_total_pair_count"
# 可追溯性矩阵制品的落盘路径（JSON；CSV 与它同名不同后缀）。
KEY_ARTIFACT_PATH = "_weighted_coverage_artifact_path"
# 制品写盘失败的原因。写不出制品**不阻断**评测（结论都在库里），但报告必须说，
# 否则 CI 归档那一步会捞不到文件而且没人知道为什么。
KEY_ARTIFACT_FAILURE = "_weighted_coverage_artifact_failure"


class WeightedCoverageState(PipelineState, total=False):
    """`PipelineState` + 模块八私有键的类型视图。

    节点签名必须用本类型而不是 `PipelineState`：LangGraph 按节点函数第一个参数的
    类型注解推导输入 schema，写 `PipelineState` 会让私有键在进入节点前被裁掉
    （见模块头的 ⚠️）。

    它**不继承** `CoverageState` / `PruningState`：模块六/七的私有键不是本维度的
    输入，继承过来只会让"本维度到底读了什么"变得含糊。主图那边三个 `*State` 都要
    并进状态 schema，但那是主图的事（三者都是 `PipelineState` 的 `total=False`
    扩展，可以安全地多重继承）。
    """

    _weighted_coverage_node_count: int
    _weighted_coverage_tier_distribution: dict[str, int]
    _weighted_coverage_tier_graded: bool
    _weighted_coverage_constraint_count: int
    _weighted_coverage_uncovered_constraint_ids: list[str]
    _weighted_coverage_undetermined_constraint_ids: list[str]
    _weighted_coverage_probe_call_count: int
    _weighted_coverage_probe_budget_exhausted: bool
    _weighted_coverage_constraint_patched_count: int
    _weighted_coverage_patch_failure: str | None
    _weighted_coverage_ratio: float
    _weighted_coverage_constraint_ratio: float
    _weighted_coverage_verdict_status: str
    _weighted_coverage_ranked_uncovered_pairs: list[list[str]]
    _weighted_coverage_ranked_pair_count: int
    _weighted_coverage_total_pair_count: int
    _weighted_coverage_artifact_path: str | None
    _weighted_coverage_artifact_failure: str | None


__all__ = [
    "DIMENSION",
    "KEY_ARTIFACT_FAILURE",
    "KEY_ARTIFACT_PATH",
    "KEY_CONSTRAINT_COUNT",
    "KEY_CONSTRAINT_PATCHED_COUNT",
    "KEY_CONSTRAINT_RATIO",
    "KEY_NODE_COUNT",
    "KEY_PATCH_FAILURE",
    "KEY_PROBE_BUDGET_EXHAUSTED",
    "KEY_PROBE_CALL_COUNT",
    "KEY_RANKED_PAIR_COUNT",
    "KEY_RANKED_UNCOVERED_PAIRS",
    "KEY_RATIO",
    "KEY_TIER_DISTRIBUTION",
    "KEY_TIER_GRADED",
    "KEY_TOTAL_PAIR_COUNT",
    "KEY_UNCOVERED_CONSTRAINT_IDS",
    "KEY_UNDETERMINED_CONSTRAINT_IDS",
    "KEY_VERDICT_STATUS",
    "NODE_PREFIX",
    "ROUTING_KEY",
    "WeightedCoverageState",
]
