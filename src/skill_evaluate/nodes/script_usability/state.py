"""模块四的私有状态命名空间（docs/dev/14；命名约定沿用 docs/dev/11 第 3 节）。

三条约定，与模块一/二/三一致：

1. 私有键一律以下划线 + 维度前缀（`_su_`）开头，表示"本维度临时状态"，不属于
   docs/dev/02 `PipelineState` 的正式字段；
2. 各维度**不得**读写其他维度的私有键；
3. **不得**省略前缀直接复用公共字段名——LangGraph 的默认 reducer 是"后写胜"，
   主图里多个维度并行写同名键时不会报错，只会让某个维度悄悄拿到别人的数据。

⚠️ 主图（docs/dev/24）的状态 schema 必须包含本文件声明的私有键，否则它们会在进入
节点前被 LangGraph **静默裁掉**：本维度的表现会是"三条探测支路都跑了、收尾节点
却一条发现都看不到"，然后给出一份看起来通过了的报告。见
`docs/dev/interfaces/14_script_usability_probing.md` 第 2 节。

## 为什么 `_su_targets` 存的是对象而不是 id

其余键存的都是摘要/发现字符串，只有 `_su_targets` 存了一份结构化对象列表
（`ScriptProbeTarget`）。理由与模块一的 `_working_skill` 相同——**它不在库里**：
"这个脚本用哪个镜像、哪个解释器、是不是突变脚本、文件在不在盘上"是 `prepare_scripts`
现场推断出来的，三条并行支路都要用，除了随状态走没有第二条路。它是 KB 级的小
对象（每个脚本几个短字符串），Checkpoint 体积可接受。
"""

from __future__ import annotations

from skill_evaluate.state.pipeline_state import PipelineState

# 维度名。同时是 `NODE_BACKEND_ROUTING` 的键、`DimensionResult.dimension` 的取值、
# 以及各节点名的前缀——三处必须一致，因此收敛成一个常量。
DIMENSION = "script_usability"

# ---- 私有状态键（字符串常量化，避免各节点各写各的字面量拼错） ----
KEY_TARGETS = "_su_targets"  # 本次要探测的脚本清单（ScriptProbeTarget 的 dump）
KEY_PREPARE_FINDINGS = "_su_prepare_findings"  # 准备阶段就有结论的问题（文件缺失/运行时未知）
KEY_HARD_FAILURE_FINDINGS = "_su_hard_failure_findings"  # 非交互性挂起（致命，阻断）
KEY_HELP_OUTCOMES = "_su_help_outcomes"  # --help 文档质量判定摘要
KEY_ERROR_OUTCOMES = "_su_error_outcomes"  # 建设性报错 + 流隔离判定摘要
KEY_IDEMPOTENCY_FINDINGS = "_su_idempotency_findings"  # 幂等性与输出截断发现
KEY_SANDBOX_UNAVAILABLE = "_su_sandbox_unavailable"  # 沙箱运行时不可用的原因（整维度降级）


class ScriptUsabilityState(PipelineState, total=False):
    """`PipelineState` + 模块四私有键的类型视图。

    节点签名必须用本类型而不是 `PipelineState`：LangGraph 按节点函数第一个参数的
    类型注解推导输入 schema，写 `PipelineState` 会让私有键在进入节点前被裁掉
    （见模块头的 ⚠️）。

    值的类型写 `dict`/`list[dict]` 而不是对应的 Pydantic 模型：Checkpoint 反序列化
    后拿回来的可能是 dict（取决于 serde 实现），节点侧统一用 `model_validate()`
    收敛，不在类型注解上假装它一定还是模型实例。
    """

    _su_targets: list[dict[str, object]]
    _su_prepare_findings: list[str]
    _su_hard_failure_findings: list[str]
    _su_help_outcomes: list[dict[str, object]]
    _su_error_outcomes: list[dict[str, object]]
    _su_idempotency_findings: list[str]
    _su_sandbox_unavailable: str | None


__all__ = [
    "DIMENSION",
    "KEY_ERROR_OUTCOMES",
    "KEY_HARD_FAILURE_FINDINGS",
    "KEY_HELP_OUTCOMES",
    "KEY_IDEMPOTENCY_FINDINGS",
    "KEY_PREPARE_FINDINGS",
    "KEY_SANDBOX_UNAVAILABLE",
    "KEY_TARGETS",
    "ScriptUsabilityState",
]
