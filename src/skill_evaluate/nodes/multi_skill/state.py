"""模块十的私有状态命名空间（docs/dev/20；命名约定沿用 docs/dev/11 第 3 节）。

| 常量 | 取值 | 用途 |
|---|---|---|
| `DIMENSION` | `multi_skill_conflict` | `DimensionResult.dimension`，也是 `NODE_BACKEND_ROUTING` 的键（docs/dev/03 第 5 节早已登记） |
| `ROUTING_KEY` | 同上 | 单列一个名字，读代码的人不必猜"这里用 DIMENSION 是巧合还是约定" |
| `NODE_PREFIX` | `multi_skill` | 节点名前缀（docs/dev/20 第 3 节、docs/dev/24 主图按 `multi_skill.*` 引用） |

⚠️ 主图（docs/dev/24）的状态 schema 必须包含本文件声明的私有键：LangGraph 按节点签名
的类型注解裁剪状态，漏了它们的表现是"所有探测结果丢失"，收尾节点会如实报
NEEDS_HUMAN_REVIEW 并点名缺了哪个键——不会假装通过，但整趟沙箱白跑。

## 与 docs/dev/20 正文的差异

1. 正文把 `_noise_pack` / `_core_skills` 整个 `SkillDefinition` 列表塞进状态；这里只存
   `(skill_id, version_ref)` 引用（docs/dev/02：状态里只存引用，Checkpoint 体积可控），
   各节点回库读取——干扰包里每份 SKILL.md 全文进 Checkpoint，每个超步都要序列化一遍。
2. 正文用 `_hijack_findings` 这类无前缀的键、各写一个 findings 列表；这里统一
   `_multi_skill_` 前缀，并且每条探测写**自己的**结构化结果键：三条并行支路写同一个
   默认 reducer（后写胜）的键会互相覆盖。
3. 正文把"角色冲突"的判定 id 直接写进 `judge_verdict_ids`、把时序发现写进
   `_temporal_findings`；这里两者拆成两个结果键，报告里能区分"静态审查没过"和
   "打乱顺序后崩了"。
"""

from __future__ import annotations

from skill_evaluate.state.pipeline_state import PipelineState

DIMENSION = "multi_skill_conflict"
ROUTING_KEY = DIMENSION
NODE_PREFIX = "multi_skill"

# ---- 私有状态键 ----
KEY_NOISE_PACK_REFS = "_multi_skill_noise_pack_refs"
KEY_CORE_SKILL_REFS = "_multi_skill_core_skill_refs"
KEY_CONTEXT_NOTES = "_multi_skill_context_notes"
KEY_CASE_IDS = "_multi_skill_case_ids"
KEY_SUITE_STALENESS_WARNING = "_multi_skill_suite_staleness_warning"
KEY_NAMESPACE_OUTCOME = "_multi_skill_namespace_outcome"
KEY_HIJACK_OUTCOME = "_multi_skill_hijack_outcome"
KEY_ANTAGONISM_OUTCOME = "_multi_skill_antagonism_outcome"
KEY_ATTENTION_OUTCOME = "_multi_skill_attention_outcome"
KEY_ROLE_OUTCOME = "_multi_skill_role_outcome"
KEY_TEMPORAL_OUTCOME = "_multi_skill_temporal_outcome"
KEY_CORE_REGRESSION_OUTCOME = "_multi_skill_core_regression_outcome"
KEY_ALERT_DISPATCHED = "_multi_skill_alert_dispatched"
# docs/dev/22：收尾节点把"达到告警条件时的告警 payload"写进状态，供审批闸门节点决定
# 阻塞挂起还是只发通知。与 KEY_ALERT_DISPATCHED 分开：告警通道故障（dispatched=False）
# 不应让人工介入一起消失——卡片走的是 pending_approvals，不依赖告警通道。
KEY_DEEP_CONFLICT_ALERT = "_multi_skill_deep_conflict_alert"
# 闸门节点的处理结果：None（未达告警条件）/ "notified"（非阻塞通知）/ "acknowledge"（人工已知悉放行）
KEY_DEEP_CONFLICT_RESOLUTION = "_multi_skill_deep_conflict_resolution"


class MultiSkillState(PipelineState, total=False):
    """`PipelineState` + 模块十私有键的类型视图（节点签名必须用它，见模块头 ⚠️）。

    结果值写 `dict` 而不是 Pydantic 模型：Checkpoint 反序列化后拿回来的可能是 dict，
    节点侧统一 `model_validate()` 收敛（与模块二/九同一处理）。
    """

    _multi_skill_noise_pack_refs: list[dict[str, str]]  # [{"skill_id", "version_ref"}]
    _multi_skill_core_skill_refs: list[dict[str, str]]  # 同上
    _multi_skill_context_notes: list[str]  # 准备阶段的说明（缺失技能、干扰包规模等）
    _multi_skill_case_ids: list[str]  # MULTI_SKILL 复合用例
    _multi_skill_suite_staleness_warning: str | None
    _multi_skill_namespace_outcome: dict[str, object]  # ProbeOutcome
    _multi_skill_hijack_outcome: dict[str, object]
    _multi_skill_antagonism_outcome: dict[str, object]
    _multi_skill_attention_outcome: dict[str, object]
    _multi_skill_role_outcome: dict[str, object]
    _multi_skill_temporal_outcome: dict[str, object]
    _multi_skill_core_regression_outcome: dict[str, object]
    _multi_skill_alert_dispatched: bool  # 本次运行是否发出了深度冲突告警（22/24 读取）
    _multi_skill_deep_conflict_alert: dict[str, object] | None  # 告警 payload（22 闸门读取）
    _multi_skill_deep_conflict_resolution: str | None  # 22 闸门的处理结果


__all__ = [
    "DIMENSION",
    "KEY_ALERT_DISPATCHED",
    "KEY_ANTAGONISM_OUTCOME",
    "KEY_ATTENTION_OUTCOME",
    "KEY_CASE_IDS",
    "KEY_CONTEXT_NOTES",
    "KEY_CORE_REGRESSION_OUTCOME",
    "KEY_CORE_SKILL_REFS",
    "KEY_DEEP_CONFLICT_ALERT",
    "KEY_DEEP_CONFLICT_RESOLUTION",
    "KEY_HIJACK_OUTCOME",
    "KEY_NAMESPACE_OUTCOME",
    "KEY_NOISE_PACK_REFS",
    "KEY_ROLE_OUTCOME",
    "KEY_SUITE_STALENESS_WARNING",
    "KEY_TEMPORAL_OUTCOME",
    "NODE_PREFIX",
    "ROUTING_KEY",
    "MultiSkillState",
]
