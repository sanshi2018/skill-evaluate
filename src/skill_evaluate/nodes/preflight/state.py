"""前置门禁的私有状态命名空间（docs/dev/21 第 7 节；命名约定沿用 docs/dev/11 第 3 节）。

前置门禁**不属于任何评测维度**：它不写 `dimension_results`、不产出报告条目，失败即整条
流水线挂起。写进状态的只有两份"证明材料"的摘要，供 docs/dev/24 的报告头部展示"本次评测是在
一个被证明可信的环境里跑的"——或者如实写明"本次跳过了哪道证明"。

⚠️ 与各维度同一条坑：主图（docs/dev/24）的状态 schema 必须包含这里声明的私有键，否则会在
进入节点前被 LangGraph 静默裁掉，金丝雀拿不到指纹摘要，只能每次都实跑。
"""

from __future__ import annotations

from typing import TypedDict

from skill_evaluate.state.pipeline_state import PipelineState

# 路由表键（`NODE_BACKEND_ROUTING["preflight"]`）与节点名前缀。
ROUTING_KEY = "preflight"
NODE_PREFIX = "preflight"

KEY_FINGERPRINT_OUTCOME = "_preflight_fingerprint_outcome"
KEY_CANARY_OUTCOME = "_preflight_canary_outcome"


class FingerprintOutcome(TypedDict, total=False):
    status: str  # "passed" | "off"
    digest: str | None  # 当前指纹摘要（off 时为 None）
    golden_path: str


class CanaryOutcome(TypedDict, total=False):
    status: str  # "passed" | "skipped" | "off"
    image_ref: str | None
    reasons: list[str]  # skipped 时写跳过依据
    trace_id: str | None


class PreflightState(PipelineState, total=False):
    """`PipelineState` + 前置门禁私有键。节点签名必须用本类型（理由同各维度的 state.py）。"""

    _preflight_fingerprint_outcome: FingerprintOutcome
    _preflight_canary_outcome: CanaryOutcome


__all__ = [
    "KEY_CANARY_OUTCOME",
    "KEY_FINGERPRINT_OUTCOME",
    "NODE_PREFIX",
    "ROUTING_KEY",
    "CanaryOutcome",
    "FingerprintOutcome",
    "PreflightState",
]
