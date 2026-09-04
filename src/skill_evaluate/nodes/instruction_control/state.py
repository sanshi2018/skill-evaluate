"""模块三的私有状态命名空间（docs/dev/13；命名约定沿用 docs/dev/11 第 3 节）。

三条约定，与模块一/二一致：

1. 私有键一律以下划线 + 维度前缀（`_ic_`）开头，表示"本维度临时状态"，不属于
   docs/dev/02 `PipelineState` 的正式字段；
2. 各维度**不得**读写其他维度的私有键；
3. **不得**省略前缀直接复用公共字段名——LangGraph 的默认 reducer 是"后写胜"，
   主图里多个维度并行写同名键时不会报错，只会让某个维度悄悄拿到别人的数据。

⚠️ 主图（docs/dev/24）的状态 schema 必须包含本文件声明的私有键，否则它们会在进入
节点前被 LangGraph **静默裁掉**。本维度的表现会是"A/B 对比跑了、但收尾节点看不到
任何结果"，然后给出一份看起来通过了的报告。见
`docs/dev/interfaces/13_instruction_control_pipeline.md` 第 2 节。

## 为什么本维度的私有键这么多

模块三是四个相对独立的子评测拼起来的（A/B 增值对比、Trace 效率诊断、控制标定
静态扫描、渐进式披露动态探查），四条支路并行跑完后要在一个收尾节点汇总。每条
支路的中间结论都必须过一遍图状态才能到达收尾节点——这正是它们必须各占一个键、
且必须出现在主图 schema 里的原因。

存的一律是**摘要**（`JudgmentOutcome` / `ProbeFinding` 的 dump）与 **id**，不是整个
`JudgeVerdict` 或 `ExecutionTrace`：verdict 与 Trace 都已经各自落库，状态里再放一份
只会让每个 Checkpoint 白背几十 KB（docs/dev/02 的"状态里只存引用"原则）。唯一的
例外是 `_ic_working_skill`，理由与模块一相同——优化闭环的内存工作副本不在库里，
除了随状态走没有第二条路。
"""

from __future__ import annotations

from skill_evaluate.state.pipeline_state import PipelineState
from skill_evaluate.state.skill import SkillDefinition

# 维度名。同时是 `NODE_BACKEND_ROUTING` 的键、`DimensionResult.dimension` 的取值、
# 以及各节点名的前缀——三处必须一致，因此收敛成一个常量。
DIMENSION = "instruction_control"

# ---- 私有状态键（字符串常量化，避免各节点各写各的字面量拼错） ----
KEY_AB_CASE_IDS = "_ic_ab_case_ids"
KEY_PD_CASE_IDS = "_ic_pd_case_ids"
KEY_AB_PAIRS = "_ic_ab_pairs"
KEY_ROI_OUTCOMES = "_ic_roi_outcomes"
KEY_EFFICIENCY_OUTCOMES = "_ic_efficiency_outcomes"
KEY_CALIBRATION_OUTCOME = "_ic_calibration_outcome"
KEY_PD_FINDINGS = "_ic_pd_findings"
KEY_FAILED_TRAIN_CASE_IDS = "_ic_failed_train_case_ids"
KEY_WORKING_SKILL = "_ic_working_skill"
KEY_APPLIED_PATCH_ID = "_ic_applied_patch_id"
KEY_SUITE_STALENESS_WARNING = "_ic_suite_staleness_warning"
# **输入型**私有键：常规探查用例的 Token 水位基线。本维度自己算得出一个（见
# `probe.resolve_token_watermark()`），但允许外部（docs/dev/24 主图，或将来某个
# 掌握真实历史水位的维度）预先塞一个更靠谱的值进来覆盖。
KEY_TOKEN_WATERMARK = "_ic_baseline_token_watermark"


class InstructionControlState(PipelineState, total=False):
    """`PipelineState` + 模块三私有键的类型视图。

    节点签名必须用本类型而不是 `PipelineState`：LangGraph 按节点函数第一个参数的
    类型注解推导输入 schema，写 `PipelineState` 会让私有键在进入节点前被裁掉
    （见模块头的 ⚠️）。

    值的类型大多写 `dict`/`list[dict]` 而不是对应的 Pydantic 模型：Checkpoint 反
    序列化后拿回来的可能是 dict（取决于 serde 实现），节点侧统一用
    `model_validate()` 收敛，不在类型注解上假装它一定还是模型实例。
    """

    _ic_ab_case_ids: list[str]  # 参与 A/B 对比的用例（POSITIVE ∩ 训练集）
    _ic_pd_case_ids: list[str]  # 渐进式披露动态探查用例（两个 PD 类别）
    _ic_ab_pairs: list[dict[str, object]]  # [{case_id, loaded_trace_id, baseline_trace_id}]
    _ic_roi_outcomes: list[dict[str, object]]  # ROI 判定摘要（JudgmentOutcome 的 dump）
    _ic_efficiency_outcomes: list[dict[str, object]]  # 效率诊断摘要
    _ic_calibration_outcome: dict[str, object]  # 控制标定摘要（整份 Skill 一条）
    _ic_pd_findings: list[dict[str, object]]  # 探查发现（ProbeFinding 的 dump）
    _ic_failed_train_case_ids: list[str]  # 可优化的失败信号（只含训练集用例）
    _ic_working_skill: SkillDefinition  # 优化闭环产出的内存工作副本
    _ic_applied_patch_id: str
    _ic_suite_staleness_warning: str | None
    _ic_baseline_token_watermark: int


__all__ = [
    "DIMENSION",
    "KEY_AB_CASE_IDS",
    "KEY_AB_PAIRS",
    "KEY_APPLIED_PATCH_ID",
    "KEY_CALIBRATION_OUTCOME",
    "KEY_EFFICIENCY_OUTCOMES",
    "KEY_FAILED_TRAIN_CASE_IDS",
    "KEY_PD_CASE_IDS",
    "KEY_PD_FINDINGS",
    "KEY_ROI_OUTCOMES",
    "KEY_SUITE_STALENESS_WARNING",
    "KEY_TOKEN_WATERMARK",
    "KEY_WORKING_SKILL",
    "InstructionControlState",
]
