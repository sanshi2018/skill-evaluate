"""模块六的私有状态命名空间（docs/dev/16；命名约定沿用 docs/dev/11 第 3 节）。

三条约定，与模块一~五一致：

1. 私有键一律以下划线 + 维度前缀（`_coverage_`）开头，表示"本维度临时状态"，
   不属于 docs/dev/02 `PipelineState` 的正式字段；
2. 各维度**不得**读写其他维度的私有键；
3. **不得**省略前缀直接复用公共字段名——LangGraph 的默认 reducer 是"后写胜"，
   主图里多个维度并行写同名键时不会报错，只会让某个维度悄悄拿到别人的数据。

⚠️ 主图（docs/dev/24）的状态 schema 必须包含本文件声明的私有键，否则它们会在进入
节点前被 LangGraph **静默裁掉**：本维度的表现会是"能力树抽出来了、盲区一个都没
检出、覆盖率显示 100%"，然后给出一份看起来完美的报告。见
`docs/dev/interfaces/16_capability_coverage.md` 第 2 节。

## 三个名字，三种用途，不要合并

本维度是全项目**唯一**一个"路由键 / 节点名前缀 / 报告维度名"三者不相同的维度，
因此收敛成三个常量，各自写清楚给谁用：

| 常量 | 取值 | 谁在读 |
|---|---|---|
| `ROUTING_KEY` | `coverage_analysis` | `NODE_BACKEND_ROUTING`（docs/dev/03 第 5 节） |
| `NODE_PREFIX` | `coverage` | 图节点名前缀（docs/dev/16 第 3 节） |
| `DIMENSION` | `capability_coverage` | `DimensionResult.dimension`（docs/dev/16 第 8 节） |

前两个是**模块六/七/八共用的**：三份文档共享同一棵能力树，在主图里被装配成同一个
`coverage` 子图分区，用同一条后端路由声明。第三个必须**各自不同**——
`dimension_results` 表的唯一约束是 `(run_id, dimension)`，三份文档若都写
`coverage_analysis`，后跑完的那个会把先跑完的那个整行覆盖掉，报告里只剩一个维度
而没有任何报错。文档 17/18 接入时请各自取 `test_suite_pruning` /
`weighted_coverage` 这类名字，不要复用本常量。

## 为什么盲区存的是"id + 描述"而不是裸 id 列表

docs/dev/16 第 6 节的伪码里 `_blind_spot_ids` 只存 id。真跑起来这不够用，有两个
下游都需要描述：

1. `feedback_driven_generation` 要构造 `CapabilityFocus.descriptions`——
   `docs/dev/interfaces/06` 第 1 节把它列为**必填**："Prompt 里给模型看的是描述，
   不是裸 id。缺失的 id 会退化为 id 原文，那对模型没有任何信息量，生成出来的用例
   也就补不到真正的盲区。"
2. 报告 findings 里写一行 `零覆盖能力: skill-x:cap-3f9ac21b0d47` 对读报告的人等于
   没写——`capability_id` 是描述文本的哈希，不可读也不可猜。

存整棵树到状态里则违反"状态里只存 ID/引用"的约定（docs/dev/02 第 11 节）；只存
盲区这一小段（通常几条、每条一句话）是两者之间正确的位置。
"""

from __future__ import annotations

from pydantic import BaseModel

from skill_evaluate.state.pipeline_state import PipelineState

# `NODE_BACKEND_ROUTING` 的键（docs/dev/03 第 5 节），模块六/七/八共用。
ROUTING_KEY = "coverage_analysis"

# 节点名前缀（docs/dev/16 第 3 节），模块六/七/八共用同一个 `coverage` 分区。
NODE_PREFIX = "coverage"

# `DimensionResult.dimension` 的取值。**本文档独有**，理由见模块头的表。
DIMENSION = "capability_coverage"

# ---- 私有状态键（字符串常量化，避免各节点各写各的字面量拼错） ----
# 零覆盖能力（`BlindSpot` 的 dump 列表）。为什么不是裸 id 见模块头。
KEY_BLIND_SPOTS = "_coverage_blind_spots"
# 未加权覆盖率，[0, 1]。文档 18 接入后这个键的**含义**会变成加权覆盖率，
# 键名与消费方（finalize）都不变——见 `rules.py` 的说明。
KEY_COVERAGE_RATIO = "_coverage_ratio"
# 已执行的补盲回环次数，用于兑现 `max_patch_iterations` 硬上限。
KEY_PATCH_ITERATIONS = "_coverage_patch_iterations"
# 补盲次数已耗尽（或补盲本身失败），不再回环，直接进 finalize 并如实标注未达标。
KEY_PATCH_EXHAUSTED = "_coverage_patch_exhausted"
# 补盲调用失败的原因。失败**不阻断**本维度（覆盖率本身就不阻断合并），但必须
# 出现在报告里——否则表现为"盲区还在、系统却安静地不补了"。
KEY_PATCH_FAILURE = "_coverage_patch_failure"
# 能力树节点总数。finalize 要用它区分"没有盲区"与"根本没抽出能力"这两种
# coverage_ratio 都等于 1.0 的情形。
KEY_TREE_NODE_COUNT = "_coverage_tree_node_count"
# 本轮参与映射的正向用例数，供报告说明这个覆盖率是拿多少条题算出来的。
KEY_MAPPED_CASE_COUNT = "_coverage_mapped_case_count"
# 能力树规模超阈值、走过人工审核卡片。报告里要标注，否则读报告的人不知道这棵树
# 是被人确认过的。
KEY_TREE_REVIEW_CONFIRMED = "_coverage_tree_review_confirmed"


class BlindSpot(BaseModel):
    """一项零覆盖能力。`KEY_BLIND_SPOTS` 里存的就是它的 dump。"""

    capability_id: str
    description: str


class CoverageState(PipelineState, total=False):
    """`PipelineState` + 模块六私有键的类型视图。

    节点签名必须用本类型而不是 `PipelineState`：LangGraph 按节点函数第一个参数的
    类型注解推导输入 schema，写 `PipelineState` 会让私有键在进入节点前被裁掉
    （见模块头的 ⚠️）。

    值的类型写 `list[dict]` 而不是 `list[BlindSpot]`：Checkpoint 反序列化后拿回来
    的可能是 dict（取决于 serde 实现），节点侧统一用 `model_validate()` 收敛，
    不在类型注解上假装它一定还是模型实例。
    """

    _coverage_blind_spots: list[dict[str, object]]
    _coverage_ratio: float
    _coverage_patch_iterations: int
    _coverage_patch_exhausted: bool
    _coverage_patch_failure: str | None
    _coverage_tree_node_count: int
    _coverage_mapped_case_count: int
    _coverage_tree_review_confirmed: bool


__all__ = [
    "DIMENSION",
    "KEY_BLIND_SPOTS",
    "KEY_COVERAGE_RATIO",
    "KEY_MAPPED_CASE_COUNT",
    "KEY_PATCH_EXHAUSTED",
    "KEY_PATCH_FAILURE",
    "KEY_PATCH_ITERATIONS",
    "KEY_TREE_NODE_COUNT",
    "KEY_TREE_REVIEW_CONFIRMED",
    "NODE_PREFIX",
    "ROUTING_KEY",
    "BlindSpot",
    "CoverageState",
]
