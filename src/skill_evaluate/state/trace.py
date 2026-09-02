"""Trace Tree（docs/dev/02 第 6 节，对应架构文档模块三第 2 节四大类字段）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TimingCostMetrics(BaseModel):
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int


class ActionStep(BaseModel):
    step_id: int
    timestamp: datetime
    thought: str | None = None
    action_type: str  # 如 "bash" / "python" / "api_call" / "read_file"
    action_input: dict[str, Any]  # 结构随 action_type 变化，不强约束子 schema
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None


class ArtifactManifestEntry(BaseModel):
    file_path: str
    sha256: str
    action: str  # "created" | "modified" | "deleted"


class ExecutionTrace(BaseModel):
    """统一 Trace Tree，MiniAgentBackend 与 PluggableAgentBackend 都必须产出此结构（见 docs/dev/03）。"""

    trace_id: str
    case_id: str
    run_index: int  # 同一用例的第几次冗余执行（架构要求跑 3 次）
    backend_type: str  # ExecutorBackendType 枚举值
    loaded_skill_md: bool  # 是否检测到加载了目标 SKILL.md（触发判定核心依据）
    timing: TimingCostMetrics
    actions: list[ActionStep] = Field(default_factory=list)
    final_response: str
    modified_files_manifest: list[ArtifactManifestEntry] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime
