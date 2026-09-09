"""模块五节点的依赖容器（依赖注入点）。

与模块一/三/四同一套理由与写法（惰性构造、全部字段有默认、测试按需覆盖）。本维度
是全项目依赖**最多**的一个：Attacker（出题）、Executor（五条探测支路真实执行）、
Judge（一条语义裁决 + 五条量化规则 + 严重性定级）、Validator（生成物断言）、
Optimizer 闭环（AppSec 修复）、模块一与模块三的流水线实例（强制功能回归）、报告器。

依赖多不是设计失误，而是架构文档对模块五的定位——"对第 0/1 层几乎全部公共能力的
一次综合演练"。它一个新的执行机制都没有发明，全部是把已有能力按安全语义组合起来。

## 两条在装配期就核对的约束

放在这里而不是等运行时，理由与模块三的 `assert_backend_routing()` 相同：这两项配
错的后果都是"跑完才发现结论不成立"，而本维度跑一次的成本是全项目最高的。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from skill_evaluate.agents.attacker.service import AttackerService
from skill_evaluate.agents.judge.service import JudgeAgent
from skill_evaluate.agents.optimizer.loop import OptimizationLoop
from skill_evaluate.agents.optimizer.service import OptimizerAgent
from skill_evaluate.agents.validator.service import ValidatorAgent
from skill_evaluate.config import SecuritySettings, get_settings
from skill_evaluate.errors import ConfigurationError
from skill_evaluate.executors.base import ExecutorBackend
from skill_evaluate.executors.factory import get_backend_for_node
from skill_evaluate.executors.routing import resolve_backend_type
from skill_evaluate.nodes.security.state import DIMENSION
from skill_evaluate.observability.report_generator import ReportGenerator
from skill_evaluate.persistence.repository import (
    AssertionRepository,
    JudgeRepository,
    SecurityFindingRepository,
    SkillRepository,
    TestCaseRepository,
    TraceRepository,
)
from skill_evaluate.state.enums import Criticality, ExecutorBackendType

# **全部安全类语义裁决一律 CRITICAL**（docs/dev/15 第 7 节）。
#
# 这是本文档对 docs/dev/08"由调用方决定何为重大判决"这一设计原则的具体落实：安全
# 判定的假阴性（漏判真实漏洞为通过）后果远比其他维度严重，因此不做 ROUTINE/CRITICAL
# 的细分讨论，直接全量 3 副本共识投票。
#
# 它**刻意不是配置项**：做成配置等于给"这次先关掉共识省点钱"留了口子，而这正是
# 本维度最想防的那件事。要省成本请调 `SecuritySettings.adversarial_case_count`
# （少出几条题），而不是让每条题的判定都变得不可信。
SECURITY_CRITICALITY = Criticality.CRITICAL

# 两个评审模板的 key（注册在 `agents/mini/templates/security.py`）。
TEMPLATE_PROMPT_INJECTION_DEFENSE = "prompt_injection_defense"
TEMPLATE_SECURITY_SEVERITY_RATING = "security_severity_rating"


@dataclass(slots=True)
class SecurityDeps:
    """模块五全部外部依赖的注入点。"""

    executor_backend: ExecutorBackend | None = None
    judge_agent: JudgeAgent | None = None
    attacker_service: AttackerService | None = None
    validator_agent: ValidatorAgent | None = None
    report_generator: ReportGenerator | None = None
    optimizer_agent: OptimizerAgent | None = None
    optimization_loop: OptimizationLoop | None = None
    skill_repository: SkillRepository = field(default_factory=SkillRepository)
    test_case_repository: TestCaseRepository = field(default_factory=TestCaseRepository)
    trace_repository: TraceRepository = field(default_factory=TraceRepository)
    judge_repository: JudgeRepository = field(default_factory=JudgeRepository)
    assertion_repository: AssertionRepository = field(default_factory=AssertionRepository)
    finding_repository: SecurityFindingRepository = field(
        default_factory=SecurityFindingRepository
    )
    # None 表示"从配置读 SecuritySettings / ExecutorSettings"。
    security_settings: SecuritySettings | None = None
    max_concurrent_sandboxes: int | None = None

    def backend(self) -> ExecutorBackend:
        """本维度固定走路由表声明的 `PLUGGABLE` 后端（docs/dev/03 第 5 节）。

        红队攻击必须**真实执行**：我们要观测的是"它有没有真的把载荷交给 shell"、
        "有没有真的读到了工作区之外的文件"。Mini 后端产出的是文本级评审结论，
        这两件事一件都观测不到，拿它跑出来的"安全通过"是纯粹的假象。
        """
        if self.executor_backend is None:
            self.executor_backend = get_backend_for_node(DIMENSION)
        return self.executor_backend

    def judge(self) -> JudgeAgent:
        """Judge 单例（docs/dev/interfaces/08 第 0 节铁律）。

        本维度五条支路里有四条走 `quantitative_verdict()`、一条走
        `judgmental_verdict()`，加上定级节点又是一次 `judgmental_verdict()`——
        全部经同一个入口，正是为了让黄金盲测、共识投票、失误率冻结这三项可信度
        机制在安全判定上也生效。
        """
        if self.judge_agent is None:
            self.judge_agent = JudgeAgent()
        return self.judge_agent

    def attacker(self) -> AttackerService:
        if self.attacker_service is None:
            self.attacker_service = AttackerService()
        return self.attacker_service

    def validator(self) -> ValidatorAgent:
        """Validator 单例。只用于生成物注入那一条支路（docs/dev/15 第 9 节）。

        其余四条支路不规划断言：它们判的是"Agent 的行为"，而断言脚本查的是"产物的
        内容"，两者对不上。给它们硬塞一个断言只会得到一条永远失败的证据。
        """
        if self.validator_agent is None:
            self.validator_agent = ValidatorAgent()
        return self.validator_agent

    def reporter(self) -> ReportGenerator:
        if self.report_generator is None:
            self.report_generator = ReportGenerator()
        return self.report_generator

    def optimizer(self) -> OptimizerAgent:
        if self.optimizer_agent is None:
            self.optimizer_agent = OptimizerAgent()
        return self.optimizer_agent

    def loop(self) -> OptimizationLoop:
        if self.optimization_loop is None:
            self.optimization_loop = OptimizationLoop()
        return self.optimization_loop

    def settings(self) -> SecuritySettings:
        if self.security_settings is None:
            self.security_settings = get_settings().security
        return self.security_settings

    def concurrency_limit(self) -> int:
        """同时在飞的沙箱数上限，与模块一/三共用同一个配置项。

        本维度对它的需求同样迫切：五条探测支路是并行节点，各自又对自己那批用例
        `asyncio.gather`，不设上限足以压垮 Hermes 的调度器。
        """
        if self.max_concurrent_sandboxes is not None:
            return max(1, self.max_concurrent_sandboxes)
        return max(1, get_settings().executor.max_concurrent_sandboxes)

    def probe_timeout_s(self) -> int:
        return max(1, self.settings().probe_timeout_s)

    def dos_timeout_s(self) -> int:
        """DoS 探测用的墙钟硬上限（docs/dev/15 第 8 节）。

        显式取 `ExecutorSettings.sandbox_wall_clock_timeout_s` 而不是本维度的
        `probe_timeout_s`：DoS 判定里"超时 = 成功阻断"，而这个结论只有在超时来自
        **系统的硬上限**时才成立。用一个维度自己的、可以随手调小的超时值去得出
        "防御生效了"的结论，等于用配置制造了一个安全通过。
        """
        return max(1, get_settings().executor.sandbox_wall_clock_timeout_s)

    def adversarial_case_count(self) -> int | None:
        """本次出多少条对抗题。None = 交给 `AttackerService` 按攻击面数量自动算。"""
        return self.settings().adversarial_case_count

    @staticmethod
    def assert_backend_routing() -> None:
        """核对路由表仍然把本维度登记为 `PLUGGABLE`（docs/dev/03 第 5 节）。"""
        backend_type = resolve_backend_type(DIMENSION)
        if backend_type is not ExecutorBackendType.PLUGGABLE:
            raise ConfigurationError(
                f"维度 {DIMENSION!r} 的后端路由被声明为 {backend_type.value!r}，"
                "但模块五（docs/dev/15）的五条探测支路都要求真实执行环境："
                "载荷有没有真的被交给 shell、有没有真的读到工作区之外的文件，"
                "Mini 后端一件都观测不到。请改回 ExecutorBackendType.PLUGGABLE。"
            )

    @staticmethod
    def assert_no_outbound_network() -> None:
        """核对沙箱确实没有出站网络（docs/dev/15 第 3 节）。

        架构文档对模块五的要求是"**必须强制** Hermes Agent 在短暂的、无网络权限的
        Ephemeral Container 中执行"。`ExecutorSettings.outbound_network_allowlist`
        的默认值本就是空白名单，docs/dev/15 第 3 节明确规定**不允许任何评测维度为
        本维度覆盖该默认值**——这里就是那条规定的执行点。

        为什么值得在装配期硬报错、而不是打条警告接着跑：本维度会把凭据窃取、命令
        注入这类载荷真的喂给一个 Agent。有出站网络时，一次成功的注入就不只是"被我们
        记录下来的一条 finding"，而是一次真实的数据外泄——评测系统自己成了攻击载体。
        这是全项目唯一一处"配置不对就拒绝启动"的检查，它值得这个待遇。
        """
        allowlist = get_settings().executor.outbound_network_allowlist
        if allowlist:
            raise ConfigurationError(
                "SKILLEVAL_EXECUTOR_OUTBOUND_NETWORK_ALLOWLIST 非空："
                f"{allowlist!r}。模块五（docs/dev/15 第 3 节）要求红队探测**必须**在无出站"
                "网络的 Ephemeral 容器里执行——本维度会把凭据窃取与命令注入载荷真的"
                "喂给 Agent，一旦沙箱能出网，一次成功的注入就是一次真实的数据外泄。"
                "请清空白名单后再跑安全维度；确实需要网络的其他维度请在各自的运行里"
                "单独配置，不要设成全局默认。"
            )


__all__ = [
    "SECURITY_CRITICALITY",
    "TEMPLATE_PROMPT_INJECTION_DEFENSE",
    "TEMPLATE_SECURITY_SEVERITY_RATING",
    "SecurityDeps",
]
