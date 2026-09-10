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
    """用例类别。

    docs/dev/13 追加了最后两项（渐进式披露**动态**探查用例）。它们与
    `ProgressiveDisclosureStaticOutput`（模块二的静态审查）测的不是一回事：
    静态版只看 SKILL.md 里"触发条件写没写清楚"，动态版真的把任务跑一遍，看
    Agent 到底有没有按条件去读 `references/` 下的文件。因此它们必须是独立的
    用例类别——把它们混进 POSITIVE 会被模块一的触发率规则按"该不该加载 Skill"
    误判（它们测的是"该不该加载**参考文件**"）。
    """

    POSITIVE = "positive"  # 正向触发用例 (should-trigger)
    NEGATIVE = "negative"  # 反向近脱靶用例 (should-not-trigger)
    ADVERSARIAL = "adversarial"  # 模块五：红队攻击用例
    MULTI_SKILL = "multi_skill"  # 模块十：多技能并发用例
    # 模块三（docs/dev/13）：场景精确匹配某个 references/ 文件的触发条件，
    # 期望 Agent 在执行中读取该文件；没读 = 渐进式遗漏（严重问题）。
    PROGRESSIVE_DISCLOSURE_TRIGGER = "progressive_disclosure_trigger"
    # 模块三：完全在常规范围内的任务，**不该**触发任何额外参考文件读取；
    # 读了 = 过度抓取（效率问题，非严重）。
    PROGRESSIVE_DISCLOSURE_REGULAR = "progressive_disclosure_regular"


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
    """Validator Agent 的断言生成策略（docs/dev/02 第 10 节 / docs/dev/10 第 5 节）。

    `NONE` 由 docs/dev/10 补全：它有两个来源，语义都是"这条用例不做确定性断言，
    完全交给 Judge Agent 的语义裁决"——
    1. `TestCase.expected_output is None`（架构文档"可以不通过代码检查的断言"）；
    2. 脚本生成连续失败后的降级（`AssertionSpec.failure_reason` 会写明原因，
       报告侧据此标记"断言生成失败"供人工关注，而不是让流水线整体中断）。
    """

    NONE = "none"
    TEMPLATE_LOOKUP = "template_lookup"
    TEMPLATE_INHERIT = "template_inherit"
    GENERATED_FROM_SCRATCH = "generated_from_scratch"


class AttackSubtype(StrEnum):
    """模块五（docs/dev/15 第 2 节）：一条对抗用例具体在打哪个攻击面。

    `TestCaseCategory.ADVERSARIAL` 只说明"这是一条红队用例"，但六类攻击的**判定
    方式完全不同**——提示词注入要看模型有没有被说服（语义裁决），目录穿越要看
    有没有一次成功的越权读（确定性扫描），DoS 要看超时与崩溃（确定性事实）。
    把它们混在一个类别里，探测节点就只能靠正则去猜"这条题想打什么"，而出题时
    Attacker Agent 是**知道**的——这个字段就是把那个确定的事实保存下来。

    七个取值对应七个 Attacker Prompt 模板（`agents/attacker/prompts/`），一一对应，
    新增攻击面 = 新增一个模板 + 一次 `register_attack_playbook()`。
    """

    DIRECT_PROMPT_INJECTION = "direct_prompt_injection"  # 用户输入里直接夹带越权指令
    DATA_POISONING = "data_poisoning"  # 恶意文件内容（CSV 公式注入等）间接投毒
    ENV_THEFT = "env_theft"  # 诱导 Agent 回读并泄露环境变量/凭据
    DIRECTORY_TRAVERSAL = "directory_traversal"  # 诱导 Agent 读取工作区之外的路径
    DOS_CONTEXT_FLOOD = "dos_context_flood"  # 超大输入撑爆上下文
    DOS_LOGIC_BOMB = "dos_logic_bomb"  # 死循环/指数级任务拖死沙箱
    # docs/dev/15 第 9 节追加的第七类：诱导 Skill 把注入负载写进**生成物**
    # （SQL 语句、HTML 报告），漏洞不在 Agent 的行为里而在它产出的文件里。
    ARTIFACT_INJECTION = "artifact_injection"


class SecurityFindingCategory(StrEnum):
    """模块五安全发现分类（docs/dev/02 第 9 节）。"""

    PROMPT_INJECTION = "prompt_injection"
    DATA_POISONING = "data_poisoning"
    ENV_LEAK = "env_leak"
    DIRECTORY_TRAVERSAL = "directory_traversal"
    DOS = "dos"
    ARTIFACT_SAST = "artifact_sast"


class Criticality(StrEnum):
    """一次裁量判定的重要度（docs/dev/08 第 4.1 节）。

    **由调用方（各评测维度节点）显式声明**，Judge Agent 不自己猜：不同维度对
    "重大负面判决"的定义不同（模块五看安全等级、模块八看覆盖率阈值），全局规则
    只会两头不讨好。与 `SeverityLevel` 正交——"要不要共识投票"和"判定结果多严重"
    是两个维度，不要合并成一个字段（docs/dev/08 第 7 节）。
    """

    ROUTINE = "routine"  # 单副本快速通行，成本基线
    CRITICAL = "critical"  # 触发 3 副本背靠背复核，3 倍 Token 成本


class PatchType(StrEnum):
    """Optimizer 产出的补丁类型（docs/dev/09 第 2 节）。

    契约定义在 `state/patch.py` 的语境里，但按项目约定（本文件是全局枚举的唯一
    落点）实体定义在这里，`state/patch.py` 原样再导出，两条 import 路径等价。
    """

    DESCRIPTION_PATCH = "description_patch"  # 重写 SKILL.md 的 description 字段（模块一）
    RIGID_CONSTRAINT = "rigid_constraint"  # 在 SKILL.md 正文追加刚性安全约束（模块五 Prompt 加固）
    CODE_PATCH = "code_patch"  # 修改 scripts/ 下代码（模块五代码防御）


class HookWaitStatus(StrEnum):
    """外部事件唤醒机制中 `pending_hooks`/`human_approvals` 的等待状态（docs/dev/04 第 5 节）。"""

    WAITING = "waiting"
    RESOLVED = "resolved"


class SuggestionType(StrEnum):
    """`test_case_suggestions` 的建议类型（docs/dev/17 第 6.1 节）。

    模块七只产出 `ORPHAN_RETIREMENT` 一种；做成枚举而不是裸字符串，是因为这张表
    的消费方是 docs/dev/22 的审查工作台——它要按类型渲染不同的操作按钮
    （"淘汰孤儿用例"和将来可能出现的"合并重复用例"要人确认的东西完全不同），
    而按裸字符串分支意味着新增类型时工作台会静默走进 else 分支。
    """

    ORPHAN_RETIREMENT = "orphan_retirement"  # 绑定的声明能力已从最新能力树中消失


class SuggestionStatus(StrEnum):
    """一条用例处置建议的人工决策状态（docs/dev/17 第 6.1 节）。

    ⚠️ 三态里**没有** "applied"：`CONFIRMED` 的语义是"人已确认可以淘汰"，真正的
    淘汰动作（从活跃用例集移除/归档）属于 docs/dev/22 的范畴。模块七只写 `PENDING`，
    另外两态由工作台写入——这正是架构文档"硬性的删除操作必须保留人类开发者的最终
    Review 确认权限"的落点：评测流水线里不存在任何把状态推进到 `CONFIRMED` 的代码
    路径。
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
