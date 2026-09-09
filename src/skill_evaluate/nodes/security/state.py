"""模块五的私有状态命名空间（docs/dev/15；命名约定沿用 docs/dev/11 第 3 节）。

三条约定，与模块一/二/三/四一致：

1. 私有键一律以下划线 + 维度前缀（`_sec_`）开头，表示"本维度临时状态"，不属于
   docs/dev/02 `PipelineState` 的正式字段；
2. 各维度**不得**读写其他维度的私有键；
3. **不得**省略前缀直接复用公共字段名——LangGraph 的默认 reducer 是"后写胜"，
   主图里多个维度并行写同名键时不会报错，只会让某个维度悄悄拿到别人的数据。

⚠️ 主图（docs/dev/24）的状态 schema 必须包含本文件声明的私有键，否则它们会在进入
节点前被 LangGraph **静默裁掉**。本维度的表现会是"五条探测支路都跑了、定级节点
一条发现都看不到"，然后给出一份**安全通过**的报告——这是全项目十个维度里，私有键
漏声明后果最严重的一个。见
`docs/dev/interfaces/15_security_red_team.md` 第 2 节。

## 为什么发现列表要有 `_sec_findings` 和 `_sec_scored_findings` 两个键

五条探测支路是**并行**的，它们各自往状态里写自己那批发现。LangGraph 里并行分支
写同一个键会触发 reducer；本维度给 `_sec_findings` 挂的是 `operator.add`（追加），
这样五条支路的发现能自然汇总，而不需要五个键。

定级节点的产物却必须换一个键：它读全部发现、逐条重新裁定严重级别，再写回去。
若写回同一个 `operator.add` 键，结果会是"原始发现 + 定级后的发现"两份并存，
报告里每条问题都会出现两次（一次带初始等级，一次带最终等级）。
"""

from __future__ import annotations

import operator
from typing import Annotated

from skill_evaluate.state.pipeline_state import PipelineState
from skill_evaluate.state.skill import SkillDefinition

# 维度名。同时是 `NODE_BACKEND_ROUTING` 的键、`DimensionResult.dimension` 的取值、
# 以及各节点名的前缀——三处必须一致，因此收敛成一个常量。
DIMENSION = "security"

# ---- 私有状态键（字符串常量化，避免各节点各写各的字面量拼错） ----
KEY_ADVERSARIAL_CASE_IDS = "_sec_adversarial_case_ids"
KEY_FINDINGS = "_sec_findings"
KEY_SCORED_FINDINGS = "_sec_scored_findings"
KEY_SCORING_SKIPPED = "_sec_scoring_skipped"
KEY_BLOCKED_BY_VALIDATION_ONLY = "_sec_blocked_by_validation_only"
KEY_APPLIED_PATCH_ID = "_sec_applied_patch_id"
KEY_REGRESSION_DETAIL = "_sec_regression_detail"
KEY_WORKING_SKILL = "_sec_working_skill"
KEY_SUITE_STALENESS_WARNING = "_sec_suite_staleness_warning"


class SecurityState(PipelineState, total=False):
    """`PipelineState` + 模块五私有键的类型视图。

    节点签名必须用本类型而不是 `PipelineState`（见模块头的 ⚠️）。

    值的类型大多写 `list[dict]` 而不是 `list[SecurityFinding]`：Checkpoint 反序列化
    后拿回来的可能是 dict（取决于 serde 实现），节点侧统一用 `model_validate()`
    收敛，不在类型注解上假装它一定还是模型实例。
    """

    _sec_adversarial_case_ids: list[str]  # 本次参与探测的对抗用例
    # 五条并行探测支路各自追加自己的发现，reducer 是 `operator.add`。
    # 不用默认的"后写胜"：那会让先跑完的那条支路的发现被后跑完的悄悄覆盖，
    # 而报告里看不出少了什么——恰恰是安全维度最不能出的错。
    _sec_findings: Annotated[list[dict[str, object]], operator.add]
    _sec_scored_findings: list[dict[str, object]]  # 定级节点最终裁定后的发现（换键，见模块头）
    _sec_scoring_skipped: list[str]  # 被黄金盲测占用、本次没定成级的 finding_id
    _sec_blocked_by_validation_only: bool  # 阻断项全部落在验证集，不触发自动修复
    _sec_applied_patch_id: str
    _sec_regression_detail: str  # 强制功能回归的结论摘要，进报告 findings
    _sec_working_skill: SkillDefinition  # 优化闭环产出的内存工作副本
    _sec_suite_staleness_warning: str | None


__all__ = [
    "DIMENSION",
    "KEY_ADVERSARIAL_CASE_IDS",
    "KEY_APPLIED_PATCH_ID",
    "KEY_BLOCKED_BY_VALIDATION_ONLY",
    "KEY_FINDINGS",
    "KEY_REGRESSION_DETAIL",
    "KEY_SCORED_FINDINGS",
    "KEY_SCORING_SKIPPED",
    "KEY_SUITE_STALENESS_WARNING",
    "KEY_WORKING_SKILL",
    "SecurityState",
]
