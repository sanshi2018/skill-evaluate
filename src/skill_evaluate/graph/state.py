"""主图状态 schema（docs/dev/24 第 2 节；interfaces/11~22 第 2 节"主图 schema 必须并入私有键"）。

## 为什么必须把各维度的 `*State` 全部并进来

LangGraph 的图状态**只保留 schema 里声明过的通道**。各维度节点把结论写进自己的私有键
（`_trigger_*`、`_sec_findings`、`_coverage_ratio`……），主图若只用裸 `PipelineState` 做 schema，
这些写入会被**静默丢弃**：节点照常跑、照常改库，收尾节点却一条发现都看不到，最坏的情况是
给出一份 `status=PASS` 的安全报告（interfaces/15 第 2 节）。因此 `MainGraphState` 多重继承全部
维度的状态类型（它们都是 `PipelineState` 的 `total=False` 扩展，私有键带维度前缀、互不重名——
`tests/skill_evaluate/test_main_graph.py` 有一条回归测试守住"不重名"这件事）。

## 为什么要覆写 `active_suite_version_id` 的 reducer

`PipelineState` 里它是普通字段（LangGraph 默认 `LastValue` 通道：**同一超步内收到两次写入
直接抛 `InvalidUpdateError`**）。而主图里写它的节点有七个（模块一/三/五/十的准备节点，模块
六/七/八的补题节点）。主图的边已经把"会改用例集的节点"串行化了（见 `graph/main.py` 模块头），
但并行分支里仍可能出现"两个节点在同一超步各自回写**同一个**版本号"的情况——那不是冲突，
不应该让整条流水线崩掉。所以这里换成"非 None 的后写者胜"的 reducer：

- 两次写入值相同（常态）→ 结果不变；
- 值不同 → 取后写的，与串行执行语义一致（用例集版本是单调前进的：新版本继承旧版本全部用例）；
- 写 None → 忽略，避免某个节点的"我没拿到"把已知版本号抹掉。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from skill_evaluate.nodes.context_scoping import ContextScopingState
from skill_evaluate.nodes.coverage import CoverageState
from skill_evaluate.nodes.cross_model import CrossModelState
from skill_evaluate.nodes.instruction_control import InstructionControlState
from skill_evaluate.nodes.multi_skill import MultiSkillState
from skill_evaluate.nodes.preflight import PreflightState
from skill_evaluate.nodes.pruning import PruningState
from skill_evaluate.nodes.script_usability import ScriptUsabilityState
from skill_evaluate.nodes.security import SecurityState
from skill_evaluate.nodes.trigger_accuracy import TriggerAccuracyState
from skill_evaluate.nodes.weighted_coverage import WeightedCoverageState

# 主图节点名前缀：不属于任何评测维度的编排节点（入口登记、收尾、Nightly）。
NODE_PREFIX_PIPELINE = "pipeline"
NODE_PREFIX_FINALIZE = "finalize"
NODE_PREFIX_NIGHTLY = "nightly"

# ---- 运行模式（`_pipeline_mode`）----
# full：PR / 手动触发的完整评测；cold_suite：Nightly 只重跑模块七降级为 COLD 的用例
# （架构文档"仅在周末的 Nightly Build 中运行"）。两种模式共用**同一张**编译好的主图：
# API 进程的 GraphResumer 只持有一个已编译图（interfaces/04 注意事项），若 Nightly 另编一张图，
# 它挂起后被 Hook 回调唤醒时会用错图恢复 checkpoint。
PipelineMode = Literal["full", "cold_suite"]
MODE_FULL: PipelineMode = "full"
MODE_COLD_SUITE: PipelineMode = "cold_suite"

# ---- 主图私有键（与维度私有键同一套命名约定：下划线 + 前缀）----
KEY_SKILL_PATH = "_pipeline_skill_path"  # 输入：SKILL.md 或其目录
KEY_MODE = "_pipeline_mode"  # 输入：PipelineMode
KEY_REPORT_PATHS = "_pipeline_report_paths"  # finalize.report 写：{"json": ..., "html": ...}
KEY_REPORT_OVERALL_STATUS = "_pipeline_report_overall_status"  # finalize.report 写
KEY_REPORT_BLOCKING = "_pipeline_report_blocking"  # finalize.report 写：CI 退出码的依据
KEY_PULL_REQUEST = "_pipeline_pull_request"  # finalize.patch_pr 写：PullRequestOutcome dump
KEY_ARCHIVE_OUTCOME = "_pipeline_archive_outcome"  # finalize.rag_archive 写：ArchiveOutcome dump
KEY_COLD_SUITE_SUMMARY = "_pipeline_cold_suite_summary"  # nightly.cold_suite_regression 写


def keep_latest_suite_version(left: str | None, right: str | None) -> str | None:
    """`active_suite_version_id` 的 reducer：非 None 的后写者胜（理由见模块头）。"""
    return right if right is not None else left


class PipelineRunState(TypedDict, total=False):
    """主图自身（编排层）的私有键。"""

    _pipeline_skill_path: str
    _pipeline_mode: PipelineMode
    _pipeline_report_paths: dict[str, str]
    _pipeline_report_overall_status: str
    _pipeline_report_blocking: bool
    _pipeline_pull_request: dict[str, Any]
    _pipeline_archive_outcome: dict[str, Any]
    _pipeline_cold_suite_summary: dict[str, Any]


class MainGraphState(
    PreflightState,
    TriggerAccuracyState,
    ContextScopingState,
    InstructionControlState,
    ScriptUsabilityState,
    SecurityState,
    CoverageState,
    PruningState,
    WeightedCoverageState,
    CrossModelState,
    MultiSkillState,
    PipelineRunState,
    total=False,
):
    """主图状态 = `PipelineState` 公共字段 + 前置门禁 + 十个维度的私有键 + 编排层私有键。

    覆写 `active_suite_version_id`：换成可接受并发写入的 reducer（见模块头）。主图 schema 先于
    各节点的输入 schema 注册通道，因此节点签名里那份普通字段声明不会把通道类型改回去。
    """

    active_suite_version_id: Annotated[str | None, keep_latest_suite_version]


DIMENSION_STATE_TYPES: tuple[type, ...] = (
    PreflightState,
    TriggerAccuracyState,
    ContextScopingState,
    InstructionControlState,
    ScriptUsabilityState,
    SecurityState,
    CoverageState,
    PruningState,
    WeightedCoverageState,
    CrossModelState,
    MultiSkillState,
)
"""并进主图的全部子状态类型。测试据此核对"每个维度的每个私有键都在主图 schema 里"。"""


__all__ = [
    "DIMENSION_STATE_TYPES",
    "KEY_ARCHIVE_OUTCOME",
    "KEY_COLD_SUITE_SUMMARY",
    "KEY_MODE",
    "KEY_PULL_REQUEST",
    "KEY_REPORT_BLOCKING",
    "KEY_REPORT_OVERALL_STATUS",
    "KEY_REPORT_PATHS",
    "KEY_SKILL_PATH",
    "MODE_COLD_SUITE",
    "MODE_FULL",
    "NODE_PREFIX_FINALIZE",
    "NODE_PREFIX_NIGHTLY",
    "NODE_PREFIX_PIPELINE",
    "MainGraphState",
    "PipelineMode",
    "PipelineRunState",
    "keep_latest_suite_version",
]
