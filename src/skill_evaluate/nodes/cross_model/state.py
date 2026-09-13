"""模块九的私有状态命名空间（docs/dev/19；命名约定沿用 docs/dev/11 第 3 节）。

三个名字，刻意分开：

| 常量 | 取值 | 用途 |
|---|---|---|
| `DIMENSION` | `cross_model_generalization` | `DimensionResult.dimension`，也是 `NODE_BACKEND_ROUTING` 的键（docs/dev/03 第 5 节早已登记） |
| `ROUTING_KEY` | 同上 | 单列一个名字，读代码的人不必猜"这里用 DIMENSION 是巧合还是约定" |
| `NODE_PREFIX` | `cross_model` | 节点名前缀（docs/dev/19 第 2 节、docs/dev/24 主图都按 `cross_model.*` 引用） |

⚠️ 主图（docs/dev/24）的状态 schema 必须包含本文件声明的私有键：LangGraph 按节点
签名的类型注解裁剪状态，漏了它们的表现是"三条对照实验的结果全部丢失、收尾节点看到
一片空白"，而本维度会把"一个结果都没拿到"如实报成 NEEDS_HUMAN_REVIEW——不会假装
通过，但也跑了一趟白工。

## 与 docs/dev/19 正文的差异

正文用 `_cross_model_sample` / `_hetero_findings` 这类不带维度前缀的键、且直接存
`TestCase` 对象列表。这里改为：

- 统一 `_xmodel_` 前缀（与 `_trigger_*`、`_sec_*` 同一约定，主图合并 schema 时不撞名）；
- 抽样只存 case_id（docs/dev/02：状态里只存引用），三条并行支路各自回库读取；
- 三条支路各写**自己的**结果键，而不是往同一个 findings 列表里追加：并行节点写同一个
  默认 reducer（后写胜）的键，先跑完那条支路的结论会被悄悄覆盖。
"""

from __future__ import annotations

from skill_evaluate.state.pipeline_state import PipelineState

DIMENSION = "cross_model_generalization"
ROUTING_KEY = DIMENSION
NODE_PREFIX = "cross_model"

# ---- 私有状态键 ----
KEY_SAMPLE_CASE_IDS = "_xmodel_sample_case_ids"
KEY_SAMPLE_NOTE = "_xmodel_sample_note"
KEY_HETERO_OUTCOME = "_xmodel_hetero_outcome"
KEY_PERTURBATION_OUTCOME = "_xmodel_perturbation_outcome"
KEY_ABLATION_OUTCOME = "_xmodel_ablation_outcome"
KEY_LINGUISTIC_OUTCOME = "_xmodel_linguistic_outcome"


class CrossModelState(PipelineState, total=False):
    """`PipelineState` + 模块九私有键的类型视图（节点签名必须用它，见模块头 ⚠️）。

    结果值写 `dict` 而不是 Pydantic 模型：Checkpoint 反序列化后拿回来的可能是 dict，
    节点侧统一 `model_validate()` 收敛（与模块二同一处理）。
    """

    _xmodel_sample_case_ids: list[str]  # 从验证集确定性抽样出的用例
    _xmodel_sample_note: str | None  # 抽样为空时的原因（写进报告）
    _xmodel_hetero_outcome: dict[str, object]  # ProbeOutcome：异构执行矩阵
    _xmodel_perturbation_outcome: dict[str, object]  # ProbeOutcome：参数扰动
    _xmodel_ablation_outcome: dict[str, object]  # ProbeOutcome：随机消融
    _xmodel_linguistic_outcome: dict[str, object]  # LinguisticOutcome：语言坏味道审查


__all__ = [
    "DIMENSION",
    "KEY_ABLATION_OUTCOME",
    "KEY_HETERO_OUTCOME",
    "KEY_LINGUISTIC_OUTCOME",
    "KEY_PERTURBATION_OUTCOME",
    "KEY_SAMPLE_CASE_IDS",
    "KEY_SAMPLE_NOTE",
    "NODE_PREFIX",
    "ROUTING_KEY",
    "CrossModelState",
]
