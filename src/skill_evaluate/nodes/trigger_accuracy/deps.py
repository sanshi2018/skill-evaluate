"""模块一节点的依赖容器（依赖注入点）。

为什么不在节点里直接 `TraceRepository()` / `JudgeAgent()`：

- docs/dev/03 第 2 节要求节点只依赖 `ExecutorBackend` 抽象，后端由路由表决定；
- docs/dev/interfaces/08 第 0 节要求 `JudgeAgent` 用**单例注入**——各节点各自
  实例化不会报错，但会让 `review_agent_factory` / `trace_handle` 这类旁路依赖在
  节点之间不一致；
- 单测要能在不碰 Postgres、不发真实请求的前提下跑完整条子图。

所有字段都有默认工厂，`TriggerAccuracyDeps()` 即得到生产配置；测试与
docs/dev/19（跨模型矩阵复用本维度执行逻辑）按需覆盖其中几项即可。

**惰性构造**：默认值走 `default=None` + `__post_init__` 懒实例化，而不是
`default_factory`——`get_backend_for_node()` 会读配置、可能建 LLM 客户端，
不该在 import 一个 dataclass 的时候就发生。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from skill_evaluate.agents.generator.service import TestSuiteService
from skill_evaluate.agents.judge.service import JudgeAgent
from skill_evaluate.agents.optimizer.loop import OptimizationLoop
from skill_evaluate.agents.optimizer.service import OptimizerAgent
from skill_evaluate.config import get_settings
from skill_evaluate.executors.base import ExecutorBackend
from skill_evaluate.executors.factory import get_backend_for_node
from skill_evaluate.nodes.trigger_accuracy.state import DIMENSION
from skill_evaluate.observability.report_generator import ReportGenerator
from skill_evaluate.persistence.repository import (
    JudgeRepository,
    RunRepository,
    SkillRepository,
    TestCaseRepository,
    TraceRepository,
)

# 架构文档模块一："考虑到 LLM 行为的非确定性，Executor Agent 会针对每一个生成的
# 测试用例运行 3 次"。做成常量而不是配置项：3 这个数字与判定阈值（>= 0.5 即
# 3 次中至少 2 次）是绑死的，单独调其中一个会让判定语义悄悄变化。
REDUNDANT_RUNS = 3

# 单次执行的墙钟超时（docs/dev/11 第 4 节写死 90s）。比 `ExecutorSettings.
# sandbox_wall_clock_timeout_s` 的默认 60s 宽松：触发判定要跑完一整个真实
# Agent 任务，而不是只看它有没有加载 SKILL.md。
EXECUTION_TIMEOUT_S = 90


@dataclass(slots=True)
class TriggerAccuracyDeps:
    """模块一全部外部依赖的注入点。"""

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
    run_repository: RunRepository = field(default_factory=RunRepository)
    redundant_runs: int = REDUNDANT_RUNS
    execution_timeout_s: int = EXECUTION_TIMEOUT_S
    # None 表示"从配置读 ExecutorSettings.max_concurrent_sandboxes"。
    max_concurrent_sandboxes: int | None = None

    def backend(self) -> ExecutorBackend:
        """本维度固定走路由表声明的 `PLUGGABLE` 后端（docs/dev/03 第 5 节）。

        触发判定的唯一依据是"有没有真的加载 SKILL.md"，这件事只有真实执行环境
        观测得到，因此不做后端判断、也不允许降级到 Mini 后端。
        """
        if self.executor_backend is None:
            self.executor_backend = get_backend_for_node(DIMENSION)
        return self.executor_backend

    def judge(self) -> JudgeAgent:
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

    def concurrency_limit(self) -> int:
        if self.max_concurrent_sandboxes is not None:
            return max(1, self.max_concurrent_sandboxes)
        return max(1, get_settings().executor.max_concurrent_sandboxes)


__all__ = ["EXECUTION_TIMEOUT_S", "REDUNDANT_RUNS", "TriggerAccuracyDeps"]
