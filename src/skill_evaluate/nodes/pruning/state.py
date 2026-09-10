"""模块七的私有状态命名空间（docs/dev/17；命名约定沿用 docs/dev/11 第 3 节）。

三条约定，与模块一~六一致：

1. 私有键一律以下划线 + 维度前缀（`_pruning_`）开头，表示"本维度临时状态"，
   不属于 docs/dev/02 `PipelineState` 的正式字段；
2. 各维度**不得**读写其他维度的私有键（本维度尤其要克制：它与模块六共用
   `coverage` 图分区、共用能力树，但**不读** `_coverage_*` 的任何一个键——
   两者的耦合点只有落库的 `CapabilityTree` 与 `TestCase`）；
3. **不得**省略前缀直接复用公共字段名。

⚠️ 主图（docs/dev/24）的状态 schema 必须包含本文件声明的私有键，否则它们会在进入
节点前被 LangGraph **静默裁掉**。本维度被裁掉的症状比模块六更隐蔽：三个分析节点
照常跑、照常改库（用例真的被降级、建议真的落表），只是 finalize 拿不到任何计数，
报告里显示"折叠 0 条 / 未覆盖 0 对 / 孤儿 0 条"——一份看起来"测试集非常健康"的
报告，而实际上刚刚有一批用例被移进了冷数据区。finalize 因此对"关键键取不到"判
`NEEDS_HUMAN_REVIEW` 并点名该键，而不是判 PASS。

## 三个名字：两个沿用模块六，一个必须不同

| 常量 | 取值 | 与模块六的关系 |
|---|---|---|
| `ROUTING_KEY` | `coverage_analysis` | **沿用**（`nodes.coverage.state.ROUTING_KEY`） |
| `NODE_PREFIX` | `coverage` | **沿用**（同一个图分区） |
| `DIMENSION` | `test_suite_health` | **必须独有** |

前两个直接从 `nodes.coverage.state` 导入而不是重新定义一份同值常量：模块六/七/八
共享同一棵能力树、同一条后端路由、同一个图分区，各写各的字面量会让某天有人改了
其中一处而另外两处保持沉默。

`DIMENSION` 则必须不同——`dimension_results` 表的唯一约束是 `(run_id, dimension)`，
两份文档若都写 `capability_coverage`，后跑完的会把先跑完的**整行覆盖掉**，报告里
只剩一个维度，且不会有任何报错。

取值定为 `test_suite_health`（docs/dev/17 第 7 节的原文），而不是
`docs/dev/interfaces/16` 第 1 节当初建议的 `test_suite_pruning`：本维度报告的不只是
"瘦身了多少"，还有组合覆盖缺口与孤儿用例——它衡量的是**测试集自身的健康度**，
而 pruning 只是三件事之一。接入文档已同步更新。
"""

from __future__ import annotations

from skill_evaluate.nodes.coverage.state import NODE_PREFIX, ROUTING_KEY
from skill_evaluate.state.pipeline_state import PipelineState

# `DimensionResult.dimension` 的取值。**本文档独有**，理由见模块头的表。
DIMENSION = "test_suite_health"

# ---- 私有状态键 ----
# 本轮新降级到 COLD 的用例 id。"新"是关键：上几轮已经是 COLD 的不再计入，
# 否则报告里"折叠冗余用例 12 条"会在连续三次评测里重复出现同样的 12 条。
KEY_DEMOTED_CASE_IDS = "_pruning_demoted_case_ids"
# 其中原本属于验证集的条数。单列一项是因为它值得被人看见：验证集是优化闭环
# （docs/dev/09）判断"补丁有没有真的改好"的依据，被瘦身缩小时应当有人知情。
KEY_DEMOTED_VALIDATION_COUNT = "_pruning_demoted_validation_count"
# 检出的冗余簇数量（成员 >= 2 的簇）。供报告说明这次折叠是在多少个"能力路径完全
# 一致"的簇上发生的。
KEY_CLUSTER_COUNT = "_pruning_redundant_cluster_count"
# 未覆盖的能力组合对。存 `list[list[str]]` 而不是 `list[tuple[str, str]]`：
# Checkpoint 走 JSON 序列化，元组回来就是列表，在类型注解上假装它还是元组只会让
# 反序列化后的比较逻辑在某个分支上悄悄失败。
KEY_UNCOVERED_PAIRS = "_pruning_uncovered_pairs"
# 本轮真正参与分析的组合对总数（截断之后的）。分母就是它，见 `KEY_PAIR_RATIO`。
KEY_ANALYZED_PAIR_COUNT = "_pruning_analyzed_pair_count"
# 截断之前的组合对总数。与上一个不同时说明发生了截断，报告要如实标注。
KEY_TOTAL_PAIR_COUNT = "_pruning_total_pair_count"
# 组合覆盖率 = (已覆盖的分析范围内组合对) / (分析范围内组合对)，[0, 1]。
# 它就是本维度 `DimensionResult.score` 的取值。
KEY_PAIR_COVERAGE_RATIO = "_pruning_pair_coverage_ratio"
# 组合矩阵因超过 `max_capability_pairs_for_matrix` 而被截断。
KEY_MATRIX_TRUNCATED = "_pruning_matrix_truncated"
# 截断时是否用上了真实的能力权重分级。文档 18 落地前所有 `tier` 都是占位值，
# 此时排序退化为"按 id 排"，报告必须说明这次截断没做优先级筛选。
KEY_MATRIX_TIER_RANKED = "_pruning_matrix_tier_ranked"
# 本轮定向补生成覆盖了几对组合。
KEY_PATCHED_PAIR_COUNT = "_pruning_patched_pair_count"
# 组合补题失败的原因。失败**不阻断**本维度，但必须出现在报告里——否则表现为
# "组合缺口还在、系统却安静地不补了"。
KEY_PATCH_FAILURE = "_pruning_patch_failure"
# 本轮检出的孤儿用例 id（绑定的能力全部已从能力树消失）。
KEY_ORPHAN_CASE_IDS = "_pruning_orphan_case_ids"
# 其中**新**落表的建议条数。与上一项通常不等：同一条孤儿用例连续三次评测都会被
# 检出，但待办只在第一次产生（`(case_id, suggestion_type)` 唯一）。
KEY_NEW_SUGGESTION_COUNT = "_pruning_new_suggestion_count"


class PruningState(PipelineState, total=False):
    """`PipelineState` + 模块七私有键的类型视图。

    节点签名必须用本类型而不是 `PipelineState`：LangGraph 按节点函数第一个参数的
    类型注解推导输入 schema，写 `PipelineState` 会让私有键在进入节点前被裁掉
    （见模块头的 ⚠️）。

    它**不继承** `CoverageState`：模块六的私有键不是本维度的输入，继承过来只会让
    "本维度到底读了什么"这件事变得含糊，还会诱使后来人顺手去读 `_coverage_ratio`。
    主图那边两个 `*State` 都要并进状态 schema，但那是主图的事（两者都是
    `PipelineState` 的 `total=False` 扩展，可以安全地多重继承）。
    """

    _pruning_demoted_case_ids: list[str]
    _pruning_demoted_validation_count: int
    _pruning_redundant_cluster_count: int
    _pruning_uncovered_pairs: list[list[str]]
    _pruning_analyzed_pair_count: int
    _pruning_total_pair_count: int
    _pruning_pair_coverage_ratio: float
    _pruning_matrix_truncated: bool
    _pruning_matrix_tier_ranked: bool
    _pruning_patched_pair_count: int
    _pruning_patch_failure: str | None
    _pruning_orphan_case_ids: list[str]
    _pruning_new_suggestion_count: int


__all__ = [
    "DIMENSION",
    "KEY_ANALYZED_PAIR_COUNT",
    "KEY_CLUSTER_COUNT",
    "KEY_DEMOTED_CASE_IDS",
    "KEY_DEMOTED_VALIDATION_COUNT",
    "KEY_MATRIX_TIER_RANKED",
    "KEY_MATRIX_TRUNCATED",
    "KEY_NEW_SUGGESTION_COUNT",
    "KEY_ORPHAN_CASE_IDS",
    "KEY_PAIR_COVERAGE_RATIO",
    "KEY_PATCHED_PAIR_COUNT",
    "KEY_PATCH_FAILURE",
    "KEY_TOTAL_PAIR_COUNT",
    "KEY_UNCOVERED_PAIRS",
    "NODE_PREFIX",
    "ROUTING_KEY",
    "PruningState",
]
