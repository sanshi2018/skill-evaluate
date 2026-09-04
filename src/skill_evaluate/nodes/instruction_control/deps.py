"""模块三节点的依赖容器（依赖注入点）。

与模块一 `TriggerAccuracyDeps` 同一套理由与写法（惰性构造、全部字段有默认、测试
按需覆盖）。本维度是目前依赖**最多**的一个：它同时用到执行后端（A/B 真实执行）、
Judge（三种判定）、Generator（补出渐进式披露探查用例）、Optimizer 闭环（失败重
写）与报告器。

四项 `Criticality` 声明放在本文件而不是散在节点里：它们是本维度最重要的成本/
可信度取舍，集中一处才能一眼看出"哪些判定值得花三倍 Token"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from skill_evaluate.agents.generator.service import TestSuiteService
from skill_evaluate.agents.judge.service import JudgeAgent
from skill_evaluate.agents.optimizer.loop import OptimizationLoop
from skill_evaluate.agents.optimizer.service import OptimizerAgent
from skill_evaluate.config import InstructionControlSettings, get_settings
from skill_evaluate.errors import ConfigurationError
from skill_evaluate.executors.base import ExecutorBackend
from skill_evaluate.executors.factory import get_backend_for_node
from skill_evaluate.executors.routing import resolve_backend_type
from skill_evaluate.nodes.instruction_control.state import DIMENSION
from skill_evaluate.observability.report_generator import ReportGenerator
from skill_evaluate.persistence.repository import (
    JudgeRepository,
    SkillRepository,
    TestCaseRepository,
    TraceRepository,
)
from skill_evaluate.state.enums import Criticality, ExecutorBackendType

# ROI 判定声明 CRITICAL，触发 docs/dev/08 的 3 副本共识投票（3 倍 Token 成本）。
# 理由（docs/dev/13 第 4.2 节）："这份 Skill 没有提供附加价值、必须打回重构"是会
# 直接决定要不要合并的重大负面结论，值得多花两倍成本换裁判可信度。它也是本项目
# 第一处大量使用 CRITICAL 的地方。
ROI_CRITICALITY = Criticality.CRITICAL

# 效率诊断与控制标定声明 ROUTINE：这两类结论是"发现问题供优化参考"，本身不阻断
# 合并（见 docs/dev/13 第 8 节：它们也**不**触发自动优化闭环）。给一个只作参考的
# 建议投三次票，是纯粹的浪费。
EFFICIENCY_CRITICALITY = Criticality.ROUTINE
CALIBRATION_CRITICALITY = Criticality.ROUTINE

# 单次执行的墙钟超时。与模块一同为 90s：A/B 对比跑的是同一批复杂任务用例，
# 超时口径不一致会让"基线更快"变成一个由配置制造出来的假象。
EXECUTION_TIMEOUT_S = 90

# 三个评审模板的 key（注册在 `agents/mini/templates/`）。
TEMPLATE_ROI_COMPARISON = "roi_comparison"
TEMPLATE_TRACE_EFFICIENCY = "trace_efficiency"
TEMPLATE_CONTROL_CALIBRATION = "control_calibration"  # 复用 docs/dev/07 模板 5.7


@dataclass(slots=True)
class InstructionControlDeps:
    """模块三全部外部依赖的注入点。"""

    executor_backend: ExecutorBackend | None = None
    judge_agent: JudgeAgent | None = None
    test_suite_service: TestSuiteService | None = None
    report_generator: ReportGenerator | None = None
    optimizer_agent: OptimizerAgent | None = None
    optimization_loop: OptimizationLoop | None = None
    skill_repository: SkillRepository = field(default_factory=SkillRepository)
    test_case_repository: TestCaseRepository = field(default_factory=TestCaseRepository)
    trace_repository: TraceRepository = field(default_factory=TraceRepository)
    judge_repository: JudgeRepository = field(default_factory=JudgeRepository)
    # None 表示"从配置读 InstructionControlSettings / ExecutorSettings"。
    control_settings: InstructionControlSettings | None = None
    max_concurrent_sandboxes: int | None = None
    execution_timeout_s: int = EXECUTION_TIMEOUT_S

    def backend(self) -> ExecutorBackend:
        """本维度固定走路由表声明的 `PLUGGABLE` 后端（docs/dev/03 第 5 节）。

        A/B 对比与渐进式披露动态探查都要求**真实执行**：前者比的是两条真实轨迹的
        质量与效率，后者看的是执行过程中有没有真的去读参考文件。Mini 后端两件事
        都做不到，因此本维度不做后端判断、也不允许降级。
        """
        if self.executor_backend is None:
            self.executor_backend = get_backend_for_node(DIMENSION)
        return self.executor_backend

    def judge(self) -> JudgeAgent:
        """Judge 单例。

        必须走 `JudgeAgent`、不能直接调 `MiniReviewAgent`：黄金基准盲测、共识投票、
        失误率冻结这三项可信度机制都挂在它的两个方法上，绕过入口 = 绕过机制，而
        报告读者看不出哪条判定绕过了（docs/dev/interfaces/08 第 0 节铁律）。
        """
        if self.judge_agent is None:
            self.judge_agent = JudgeAgent()
        return self.judge_agent

    def suite_service(self) -> TestSuiteService:
        if self.test_suite_service is None:
            self.test_suite_service = TestSuiteService()
        return self.test_suite_service

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

    def settings(self) -> InstructionControlSettings:
        if self.control_settings is None:
            self.control_settings = get_settings().instruction_control
        return self.control_settings

    def concurrency_limit(self) -> int:
        """同时在飞的沙箱数上限，与模块一共用同一个配置项。

        本维度对并发的需求比模块一更迫切：A/B 对比是"用例数 × 2 条分支"一次性
        打出去，再加上并行跑的渐进式披露探查，不设上限足以压垮 Hermes 的调度器。
        """
        if self.max_concurrent_sandboxes is not None:
            return max(1, self.max_concurrent_sandboxes)
        return max(1, get_settings().executor.max_concurrent_sandboxes)

    def run_count_per_arm(self) -> int:
        """A/B 每条分支跑几次（默认 1，见 `InstructionControlSettings` 的说明）。"""
        return max(1, self.settings().run_count_per_arm)

    @staticmethod
    def assert_backend_routing() -> None:
        """核对路由表仍然把本维度登记为 `PLUGGABLE`（docs/dev/03 第 5 节）。

        与模块二那条断言方向相反、理由相同：本维度的四项子评测里有两项**必须**
        真实执行，路由表若被改成 `MINI`，得到的会是一份基于假 Trace 的 ROI 结论
        ——那比没有结论更危险。与其运行到一半才发现，不如在建图时报错说清楚。
        """
        backend_type = resolve_backend_type(DIMENSION)
        if backend_type is not ExecutorBackendType.PLUGGABLE:
            raise ConfigurationError(
                f"维度 {DIMENSION!r} 的后端路由被声明为 {backend_type.value!r}，"
                "但模块三（docs/dev/13）的 A/B 增值对比与渐进式披露动态探查都要求真实"
                "执行环境：前者比较两条真实轨迹的质量与效率，后者观测执行中是否读取了"
                "参考文件，Mini 后端两件事都做不到。请改回 ExecutorBackendType.PLUGGABLE。"
            )


__all__ = [
    "CALIBRATION_CRITICALITY",
    "EFFICIENCY_CRITICALITY",
    "EXECUTION_TIMEOUT_S",
    "ROI_CRITICALITY",
    "TEMPLATE_CONTROL_CALIBRATION",
    "TEMPLATE_ROI_COMPARISON",
    "TEMPLATE_TRACE_EFFICIENCY",
    "InstructionControlDeps",
]
