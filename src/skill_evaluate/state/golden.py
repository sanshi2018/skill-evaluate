"""黄金基准盲测的数据契约（docs/dev/08 第 3.1 节）。

本文件是 docs/dev/02（核心数据契约）的一次**追加**：只新增子域，不改动任何既有
模型的字段语义，符合 docs/dev/01 第 9 节"向后兼容只能追加"的约定。

为什么黄金用例复用 `ReviewRequest.content` 的同构结构（`dict[str, str]`）而不是
自建一套载荷格式：盲测的全部意义在于 Judge Agent **无法区分**自己正在被考核。
只要黄金用例与真实请求的结构不同，注入点就必然要做一次形状转换，转换痕迹迟早
会泄漏进 Prompt（多一个字段、少一段上下文），盲测就退化成了明测。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from skill_evaluate.state.enums import JudgeVerdictStatus


class GoldenCase(BaseModel):
    """一条由人类专家预先标定了"绝对正确判决"的考题。

    数据来源不属于任何单一开发文档：由运维/资深工程师通过审查工作台（docs/dev/22）
    或直接写库持续补充。本项目的代码只**消费**黄金用例，不生产。
    """

    golden_id: str
    template_key: str  # 复用 docs/dev/07 的模板体系：黄金用例总是针对某个具体审查模板构造
    content: dict[str, str] = Field(default_factory=dict)  # 与 ReviewRequest.content 同构
    human_labeled_status: JudgeVerdictStatus  # 人类专家预标定的绝对正确判决
    human_labeled_reasoning: str
    active: bool = True  # 停用而不删除：失误率窗口需要按历史记录回溯


class JudgeMissRecord(BaseModel):
    """一次黄金用例判决的记账。

    只记"不一致"的那些次算不出失误率（分母会丢），所以命中与未命中**都记**，用
    `is_miss` 区分——见 `agents/judge/health.py`。
    """

    miss_id: str
    golden_id: str
    judge_output_status: JudgeVerdictStatus
    occurred_at: datetime
    # 以下三个字段是 docs/dev/08 第 3.1 节数据模型的实现期追加：失误率按
    # `(model, temperature_bucket)` 这对 Judge 配置分别统计、分别冻结（第 3.3 节），
    # 不记录配置就无法定位到底该冻结谁。
    model: str = ""
    temperature: float = 0.0
    is_miss: bool = True


__all__ = ["GoldenCase", "JudgeMissRecord"]
