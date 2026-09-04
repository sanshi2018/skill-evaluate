"""首批 7 个评审模板的结构化输出契约（docs/dev/07 第 5 节）。

**字段是契约，措辞不是**：docs/dev/12/13/14/19 接入时编写具体 Prompt 措辞与
`to_status` 判定细则，但**不改变这些 Schema 的字段**——报告聚合与后续的共识
投票都按字段读取。新增审查场景请新建 Schema 类，不要往已有类里塞字段。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 所有模板共用的判定字面量。用 Literal 而不是 str，让"模型返回了 'PASS'/'ok'
# 这种没约定过的值"在 Pydantic 校验阶段就失败并触发重试，而不是被
# `verdict == "pass"` 悄悄判成 FAIL。
Verdict = Literal["pass", "fail"]


class BaseReviewOutput(BaseModel):
    """全部评审输出的公共部分。

    `reasoning` 强制要求引用被评审文本的具体原文片段（见 docs/dev/07 第 6 节），
    便于报告展示与人工复核时快速定位。
    """

    reasoning: str
    verdict: Verdict


class OmissionAuditOutput(BaseReviewOutput):
    """5.1 常识剥离度审计（模块二）。"""

    common_sense_statements: list[str] = Field(default_factory=list)


class ScopingCheckOutput(BaseReviewOutput):
    """5.2 范围连贯性审查（模块二）。"""

    scope_issue: Literal["too_broad", "too_fragmented"] | None = None


class ProgressiveDisclosureStaticOutput(BaseReviewOutput):
    """5.3 渐进式披露触发条件审查（模块二静态版）。"""

    reference_files_without_trigger_condition: list[str] = Field(default_factory=list)


class HelpDocQualityOutput(BaseReviewOutput):
    """5.4 Help 文档质量审查（模块四）。"""

    lists_all_flags: bool
    documents_env_vars: bool
    has_usage_example: bool


class ConstructiveErrorOutput(BaseReviewOutput):
    """5.5 建设性报错审查（模块四）。"""

    states_what_went_wrong: bool
    states_expected_input: bool
    states_next_action: bool


class LinguisticSmellOutput(BaseReviewOutput):
    """5.6 语言坏味道审查（模块九）。"""

    overuse_of_caps_lock_emphasis: bool
    model_specific_incantations: list[str] = Field(default_factory=list)
    tone_issue: Literal["intimidating", "sycophantic"] | None = None


class ControlCalibrationOutput(BaseReviewOutput):
    """5.7 刚性/柔性控制标定（模块三）。"""

    task_fragility: Literal["fragile", "tolerant"]
    control_style_observed: Literal["rigid", "flexible", "mismatched"]
    has_default_recommendation: bool  # 是否避免了"甩出等价工具菜单"
    has_checklist_or_plan_verify_loop: bool


class RoiComparisonOutput(BaseReviewOutput):
    """A/B 增值对比（模块三 / docs/dev/13 第 4.2 节）。

    `verdict` 的语义在本模板里是**反过来读**的：`pass` = 加载 Skill 有显著增值，
    `fail` = 没有附加价值、该 Skill 应被打回重构。这是架构文档模块三"ROI 判定"
    的原话，不是笔误。
    """

    quality_delta_significant: bool  # 产出质量是否有肉眼可见的差距
    efficiency_delta_significant: bool  # 步数/耗时/Token 是否有肉眼可见的差距


class TraceEfficiencyOutput(BaseReviewOutput):
    """执行轨迹效率损耗诊断（模块三 / docs/dev/13 第 5 节）。

    三个布尔项一一对应架构文档模块三点名的三种效率损耗，**不合并成一个
    `efficiency_issue` 枚举**：三者可以同时发生，合并后报告里就只剩一个最"像"的
    结论，读的人无从知道另外两条有没有出现。
    """

    thrashing_detected: bool  # 指令模糊导致反复试不同命令
    blind_execution_detected: bool  # 指令不适用于当前情形却仍照做
    decision_paralysis_detected: bool  # 等价选项太多导致反复权衡/来回换方案


__all__ = [
    "BaseReviewOutput",
    "ConstructiveErrorOutput",
    "ControlCalibrationOutput",
    "HelpDocQualityOutput",
    "LinguisticSmellOutput",
    "OmissionAuditOutput",
    "ProgressiveDisclosureStaticOutput",
    "RoiComparisonOutput",
    "ScopingCheckOutput",
    "TraceEfficiencyOutput",
    "Verdict",
]
