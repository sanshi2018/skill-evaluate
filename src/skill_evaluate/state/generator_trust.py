"""Generator 可信度与前置门禁的数据契约（docs/dev/21）。

两类记录都属于"评测系统自身可信度"的审计线索，不属于任何评测维度的 `DimensionResult`：

- `GenerationCollapseEvent`：一批用例因语义坍塌被拒绝激活；
- `CanaryProbeRecord`：一次金丝雀探针的结论（成功与失败都记）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from skill_evaluate.state.enums import CollapseReason


class GenerationCollapseEvent(BaseModel):
    event_id: str
    skill_id: str
    generator_run_id: str
    generation_mode: str  # GenerationMode 枚举值
    triggered_by: str  # 与 GenerationRequest.triggered_by 同一取值表，排查"谁让出的这批废题"
    reason: CollapseReason
    # 两个距离都可能为 None：冷启动时没有历史可比、批太小时不做批内检查。
    # None 与 0.0 必须区分——0.0 是"完全重复"，None 是"没算"。
    avg_distance_to_history: float | None = None
    intra_batch_distance: float | None = None
    threshold: float
    historical_count: int
    new_case_count: int
    occurred_at: datetime


class CanaryProbeRecord(BaseModel):
    probe_id: str
    run_id: str
    image_ref: str | None = None  # 探针执行时的基础镜像标识；None = 无从判断镜像是否变更
    passed: bool
    reasons: list[str] = Field(default_factory=list)  # 失败原因清单；成功时为空
    probed_at: datetime


__all__ = ["CanaryProbeRecord", "GenerationCollapseEvent"]
