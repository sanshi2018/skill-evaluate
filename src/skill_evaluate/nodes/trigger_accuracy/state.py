"""模块一的私有状态命名空间（docs/dev/11 第 3 节的约定，供 12~20 参照）。

约定三条，后续维度文档遵循同一模式：

1. 私有键一律以下划线前缀开头，表示"本维度临时状态"，不属于 docs/dev/02 的
   `PipelineState` 正式字段；
2. 各维度**不得**读写其他维度的私有键；
3. **不得**省略前缀直接复用公共字段名——`skill`、`cases` 这类名字在主图里由多个
   维度并行写入时会互相覆盖，而 LangGraph 的默认 reducer 是"后写胜"，串扰不会
   报错，只会让某个维度悄悄拿到另一个维度的数据。

关于"存对象还是存 ID"：docs/dev/02 的原则是状态里只存引用。这里有两个刻意的例外，
`_working_skill` 与 `_trigger_case_ids` 之外的大对象一律不进状态：

- `_working_skill` 存的是 Optimizer 打完补丁后的**内存版本**——它不在库里（Patch 只
  是 diff，且多轮补丁是逐轮叠加的，光凭最后一个 patch_id 无法从原始版本重建），
  所以只能随状态走。一份 SkillDefinition 通常几 KB，Checkpoint 体积可接受。
- 其余全部存 ID，节点各自按需回库读取。
"""

from __future__ import annotations

from typing import TypedDict

from skill_evaluate.state.pipeline_state import PipelineState
from skill_evaluate.state.skill import SkillDefinition

# 维度名。同时是 `NODE_BACKEND_ROUTING` 的键、`DimensionResult.dimension` 的取值、
# 以及各节点名的前缀——三处必须一致，因此收敛成一个常量。
DIMENSION = "trigger_accuracy"

# ---- 私有状态键（字符串常量化，避免各节点各写各的字面量拼错） ----
KEY_CASE_IDS = "_trigger_case_ids"
KEY_TRAIN_CASE_IDS = "_trigger_train_case_ids"
KEY_VALIDATION_CASE_IDS = "_trigger_validation_case_ids"
KEY_TRAIN_FAILED_CASE_IDS = "_train_failed_case_ids"
KEY_VALIDATION_FAILED_CASE_IDS = "_validation_failed_case_ids"
KEY_WORKING_SKILL = "_working_skill"
KEY_APPLIED_PATCH_ID = "_applied_patch_id"
KEY_SUITE_STALENESS_WARNING = "_trigger_suite_staleness_warning"


class TriggerAccuracyState(PipelineState, total=False):
    """`PipelineState` + 模块一私有键的类型视图。

    节点签名仍按 docs/dev/11 写成 `PipelineState`（主图的状态类型只有一个），本
    TypedDict 只用于本包内部的静态检查与文档化，让"这个维度到底往状态里放了什么"
    有一处可查，而不是散落在各节点的字典字面量里。
    """

    _trigger_case_ids: list[str]  # 本维度关心的全部用例（POSITIVE + NEGATIVE）
    _trigger_train_case_ids: list[str]
    _trigger_validation_case_ids: list[str] # 验证数据集
    _train_failed_case_ids: list[str]  # 训练数据集，驱动 Optimizer 闭环
    _validation_failed_case_ids: list[str]  # **只**用于最终报告，不驱动任何重试
    _working_skill: SkillDefinition  # 打了 description 补丁的内存版本
    _applied_patch_id: str
    # `ensure_test_suite()` 的 staleness 告警：用例集与当前 SKILL.md 版本不匹配但
    # 按约定没有自动重新出题（docs/dev/06 第 4.1 节）。放进状态是为了让 docs/dev/24
    # 的收尾节点能透传给 `ReportGenerator.build()`——读报告的人必须知道这份分数是
    # 拿旧题跑出来的。
    _trigger_suite_staleness_warning: str | None


__all__ = [
    "DIMENSION",
    "KEY_APPLIED_PATCH_ID",
    "KEY_CASE_IDS",
    "KEY_SUITE_STALENESS_WARNING",
    "KEY_TRAIN_CASE_IDS",
    "KEY_TRAIN_FAILED_CASE_IDS",
    "KEY_VALIDATION_CASE_IDS",
    "KEY_VALIDATION_FAILED_CASE_IDS",
    "KEY_WORKING_SKILL",
    "TriggerAccuracyState",
]
