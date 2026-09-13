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
# 模块九（docs/dev/19）：跨模型泛化的三条对照实验 + 共识门控。
#
# 与前面各维度"一个号一条分支"不同，这里**每条分支占 10 个号**（号段起点 + 0..9）：
# 单次执行的对照结论噪声很大，`CrossModelSettings.runs_per_arm` 允许按需把每条分支
# 加到多次冗余执行（上限 10，正是由这张表的号段宽度决定的）。
#
# 两两对照的两条分支必须各占号段：`(case_id, run_index)` 唯一，同号的话"原版"与
# "变体"的 Trace 会互相覆盖，对照实验就退化成拿同一条 Trace 和自己比。
RUN_INDEX_XMODEL_ARM_WIDTH = 10
RUN_INDEX_XMODEL_PRIMARY = 150  # 异构矩阵：主代理（路由表上的 PLUGGABLE 后端，默认 Hermes）
RUN_INDEX_XMODEL_SECONDARY = 160  # 异构矩阵：备用代理（默认 llama_control）
RUN_INDEX_XMODEL_PERTURB_BASELINE = 170  # 参数扰动：贪心基线（temperature=0）
RUN_INDEX_XMODEL_PERTURB_VARIANT = 180  # 参数扰动：扰动分支（temperature=0.2, top_p=0.9）
RUN_INDEX_XMODEL_ABLATION_ORIGINAL = 190  # 消融测试：原版 SKILL.md
RUN_INDEX_XMODEL_ABLATION_ABLATED = 200  # 消融测试：剥离"咒语"后的 SKILL.md
# 共识门控（`agents/optimizer/consensus_gate.py`）：补丁前基线 / 补丁后候选，都跑在
# 备用代理上。候选号段在闭环的每一轮复用——与模块五闭环重测同一语义，判定只看
# "当前这一版补丁"的表现。
RUN_INDEX_XMODEL_GATE_BASELINE = 210
RUN_INDEX_XMODEL_GATE_CANDIDATE = 220
# 模块十（docs/dev/20）：多技能并发加载。每条执行分支只跑一次（成本由各探测的条数
# 上限控制，而不是冗余次数），因此一个分支一个号，与模块三/五同形。
#
# "单测"与"并发"两条分支必须各占一个号：同一条用例在同一次运行里既要跑"无背景技能"
# 的基线、又要跑"挂载干扰包"的并发版本，同号的话两条 Trace 会互相覆盖，对照就退化成
# 拿同一条 Trace 和自己比。
RUN_INDEX_MULTI_SKILL_HIJACK_SOLO = 230  # 触发劫持：目标 Skill 单独加载（基线）
RUN_INDEX_MULTI_SKILL_HIJACK_CROWDED = 231  # 触发劫持：挂载基准干扰包
RUN_INDEX_MULTI_SKILL_ANTAGONISM = 232  # 指令拮抗 / 语义断层：MULTI_SKILL 复合用例并发执行
RUN_INDEX_MULTI_SKILL_ATTENTION_SOLO = 233  # 注意力衰减：Gotchas 探针用例单测
RUN_INDEX_MULTI_SKILL_ATTENTION_CROWDED = 234  # 注意力衰减：同一条用例并发执行
RUN_INDEX_MULTI_SKILL_TEMPORAL = 235  # 时序扰动：打乱步骤顺序后的并发执行
# 基石回归：跑的是**核心 Skill 自己的**用例（case_id 属于核心 Skill），与核心 Skill 自身
# 评测的 0~2 号冗余执行不冲突；基线与并发仍需各占一个号。
RUN_INDEX_MULTI_SKILL_CORE_BASELINE = 236  # 核心 Skill 独立执行
RUN_INDEX_MULTI_SKILL_CORE_CROWDED = 237  # 核心 Skill 以被测 Skill 为背景执行
# docs/dev/24：Nightly COLD 用例回归（`graph/cold_suite.py`）。重跑的是模块七降级为
# `COLD` 的正/反向用例，沿用模块一的冗余执行骨架（240 .. 240+redundant_runs-1，默认 240~242）。
# 必须与模块一的 0~2 分开：同一条用例若日后被恢复为 TRAIN，模块一按 `run_index < redundant_runs`
# 统计触发率时不能把 Nightly 那几次执行算进去。
RUN_INDEX_COLD_SUITE = 240
# 后续维度从 250 起申领（240~249 已被 docs/dev/24 的 COLD 回归占用）。
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
