"""全局枚举（docs/dev/02 第 3 节）。

枚举优先于字符串字面量：状态机的各种"判定结果""严重级别""执行后端类型"
一律用 StrEnum，避免后续模块用字符串比较时出现拼写不一致的隐性 bug。
"""

from __future__ import annotations

from enum import StrEnum


class ExecutorBackendType(StrEnum):
    MINI = "mini"  # 内置 Mini Agent，静态/文本级评审
    PLUGGABLE = "pluggable"  # 外置可插拔完整执行 Agent（默认 Hermes）


class TestCaseCategory(StrEnum):
    POSITIVE = "positive"  # 正向触发用例 (should-trigger)
    NEGATIVE = "negative"  # 反向近脱靶用例 (should-not-trigger)
    ADVERSARIAL = "adversarial"  # 模块五：红队攻击用例
    MULTI_SKILL = "multi_skill"  # 模块十：多技能并发用例


class DatasetSplit(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    COLD = "cold"  # 模块七：被瘦身降级的用例进入冷数据区


class GenerationMode(StrEnum):
    REUSE = "reuse"  # 默认：复用已有测试集
    FORCE_REGENERATE = "force_regenerate"  # 手动强制全量重新生成
    INCREMENTAL_PATCH = "incremental_patch"  # 针对覆盖率盲区定向补生成（模块六/七驱动）


class JudgeVerdictStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_HUMAN_REVIEW = "needs_human_review"  # 共识未达成 / 触发黄金基准告警阈值


class SeverityLevel(StrEnum):
    """模块五安全定级，同时复用于其他维度的问题分级。"""

    CRITICAL = "critical"  # 阻断流水线
    HIGH = "high"  # 阻断流水线
    MEDIUM = "medium"  # 视策略告警或阻断
    LOW = "low"  # 仅告警


class NodeExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUSPENDED = "suspended"  # interrupt_before 挂起，等待人工审批


class CapabilityTier(StrEnum):
    """模块八：能力权重分级。定义与 capability.py 中的 TIER_WEIGHTS 一一对应。"""

    P0_CORE = "p0_core"  # 权重 0.6
    P1_CONDITIONAL = "p1_conditional"  # 权重 0.3
    P2_DEFENSIVE = "p2_defensive"  # 权重 0.1


class AssertionStrategy(StrEnum):
    """Validator Agent 的断言生成策略（docs/dev/02 第 10 节 / docs/dev/10）。"""

    TEMPLATE_LOOKUP = "template_lookup"
    TEMPLATE_INHERIT = "template_inherit"
    GENERATED_FROM_SCRATCH = "generated_from_scratch"


class SecurityFindingCategory(StrEnum):
    """模块五安全发现分类（docs/dev/02 第 9 节）。"""

    PROMPT_INJECTION = "prompt_injection"
    DATA_POISONING = "data_poisoning"
    ENV_LEAK = "env_leak"
    DIRECTORY_TRAVERSAL = "directory_traversal"
    DOS = "dos"
    ARTIFACT_SAST = "artifact_sast"


class HookWaitStatus(StrEnum):
    """外部事件唤醒机制中 `pending_hooks`/`human_approvals` 的等待状态（docs/dev/04 第 5 节）。"""

    WAITING = "waiting"
    RESOLVED = "resolved"
