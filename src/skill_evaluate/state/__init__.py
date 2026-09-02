"""跨模块共享的数据契约（docs/dev/02_核心状态模型与数据契约.md）。

本包是全项目唯一的接口真理来源：任何 Agent / Node / Executor / Judge 都只允许
通过这里定义的模型互相传递数据，不允许私自新增"临时字段"绕过契约。
"""

from skill_evaluate.state.assertion import AssertionResult, AssertionSpec
from skill_evaluate.state.capability import (
    TIER_WEIGHTS,
    CapabilityNode,
    CapabilityTree,
    NegativeConstraint,
)
from skill_evaluate.state.enums import (
    AssertionStrategy,
    CapabilityTier,
    Criticality,
    DatasetSplit,
    ExecutorBackendType,
    GenerationMode,
    HookWaitStatus,
    JudgeVerdictStatus,
    NodeExecutionStatus,
    PatchType,
    SecurityFindingCategory,
    SeverityLevel,
    TestCaseCategory,
)
from skill_evaluate.state.golden import GoldenCase, JudgeMissRecord
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.patch import Patch, PatchApplicationResult
from skill_evaluate.state.pipeline_state import PipelineState
from skill_evaluate.state.security import SecurityFinding
from skill_evaluate.state.skill import SkillDefinition, SkillReferenceFile, SkillScript
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion
from skill_evaluate.state.trace import (
    ActionStep,
    ArtifactManifestEntry,
    ExecutionTrace,
    TimingCostMetrics,
)

__all__ = [
    "TIER_WEIGHTS",
    "ActionStep",
    "ArtifactManifestEntry",
    "AssertionResult",
    "AssertionSpec",
    "AssertionStrategy",
    "CapabilityNode",
    "CapabilityTier",
    "CapabilityTree",
    "ConsensusResult",
    "Criticality",
    "DatasetSplit",
    "ExecutionTrace",
    "ExecutorBackendType",
    "GenerationMode",
    "GoldenCase",
    "HookWaitStatus",
    "JudgeMissRecord",
    "JudgeVerdict",
    "JudgeVerdictStatus",
    "NegativeConstraint",
    "NodeExecutionStatus",
    "Patch",
    "PatchApplicationResult",
    "PatchType",
    "PipelineState",
    "SecurityFinding",
    "SecurityFindingCategory",
    "SeverityLevel",
    "SkillDefinition",
    "SkillReferenceFile",
    "SkillScript",
    "TestCase",
    "TestCaseCategory",
    "TestSuiteVersion",
    "TimingCostMetrics",
]
