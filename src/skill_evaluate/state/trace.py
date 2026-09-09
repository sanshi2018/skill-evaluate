"""Trace Tree（docs/dev/02 第 6 节，对应架构文档模块三第 2 节四大类字段）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# run_index 命名空间分配表（docs/dev/13 落地时新增的全局约定）
# --------------------------------------------------------------------------- #
# `execution_traces` 的唯一键是 `(case_id, run_index)`（docs/dev/04 第 3 节），
# 而**同一条用例会被多个维度反复执行**：模块一对每条 POSITIVE 用例跑 3 次冗余，
# 模块三又拿同一批用例跑 A/B 两条分支。若各维度都从 0 开始编号，后跑的维度会
# 静默覆盖先跑的维度的 Trace，并且互相污染判定输入——模块一按 `list_by_case()`
# 统计触发率时，会把模块三那条"故意不加载 Skill"的基线分支也算成一次"没触发"，
# 于是一份完全正常的 Skill 会在下一次运行里莫名其妙地触发率不达标。
#
# 因此 run_index 是一张**全局分配表**：各维度在自己的号段内编号，0~99 留给
# "同一维度内的冗余执行"，100 起是各维度的专用号段。新维度要落 Trace 时在此
# 申领一个号，不要就地写字面量。
RUN_INDEX_REDUNDANT_BASE = 0  # 模块一（docs/dev/11）：0 .. redundant_runs-1
RUN_INDEX_DIMENSION_BASE = 100  # 100 起为各维度专用号段的起点
RUN_INDEX_AB_LOADED = 100  # 模块三（docs/dev/13）：A/B 对比的"加载 Skill"分支
RUN_INDEX_AB_BASELINE = 101  # 模块三：A/B 对比的基线分支（不加载 Skill）
RUN_INDEX_PD_PROBE = 110  # 模块三：渐进式披露动态探查（用例类别本身就是独占的）
# 模块五（docs/dev/15）：五条探测支路各占一个号，闭环重测**复用同一个号**
# ——`(case_id, run_index)` 唯一，重测会覆盖上一轮的 Trace，而这正是我们要的语义：
# 判定永远只看"当前这版 Skill 的表现"（与模块三的闭环重测同一处理）。
RUN_INDEX_SEC_PROMPT_INJECTION = 120
RUN_INDEX_SEC_DATA_POISONING = 121
RUN_INDEX_SEC_ENV_AND_TRAVERSAL = 122
RUN_INDEX_SEC_DOS = 123
RUN_INDEX_SEC_ARTIFACT_SAST = 124
# 模块五的**强制功能回归**（docs/dev/15 第 11.2 节）：安全补丁必须证明自己没有
# 把正常业务改坏，为此要拿 working_skill 重跑一遍模块一的触发率与模块三的 A/B。
# 它们必须落在自己的号段里，否则回归跑出来的 Trace 会覆盖模块一/三本次运行的
# 真实结果——一次"为了验证补丁"的重跑，把被验证对象的原始证据抹掉了。
RUN_INDEX_SEC_REGRESSION_TRIGGER = 130  # 130 .. 130+redundant_runs-1（默认 130~132）
RUN_INDEX_SEC_REGRESSION_AB_LOADED = 140  # A/B 加载分支，按 140 + 2*i 编号
RUN_INDEX_SEC_REGRESSION_AB_BASELINE = 141  # A/B 基线分支
# 模块四（docs/dev/14）**不占号段**：它裸调脚本子进程（`executors/script_sandbox.py`
# 的 `ScriptSandboxRunner`），一条 `ExecutionTrace` 都不落，与本表无关。登记在这里
# 是为了让下一个来申领号段的人不必再翻一遍模块四的代码确认这件事。


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
