"""模块十节点的依赖容器（依赖注入点）。

与模块一/九同一套写法（惰性构造、全部字段有默认、测试按需覆盖）。本维度特有的依赖：

- `capability_repository`：注意力衰减探测复用模块八已映射好的负向约束覆盖用例
  （docs/dev/20 第 8 节），从能力树上取；
- `alert_dispatcher`：深度冲突告警（docs/dev/20 第 12 节）。默认取进程级注册的分发器
  （未注册时是只写结构化日志的 `LoggingAlertDispatcher`），docs/dev/22 注册真实通道后
  本维度不用改一行代码。

装配期硬校验 `assert_backend_routing()`：多技能并发必须真实沙箱。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from skill_evaluate.agents.generator.service import TestSuiteService
from skill_evaluate.agents.judge.service import JudgeAgent
from skill_evaluate.config import MultiSkillSettings, get_settings
from skill_evaluate.errors import ConfigurationError
from skill_evaluate.executors.base import ExecutorBackend
from skill_evaluate.executors.factory import get_backend_for_node
from skill_evaluate.executors.routing import resolve_backend_type
from skill_evaluate.nodes.multi_skill.state import ROUTING_KEY
from skill_evaluate.observability.alerts import AlertDispatcher, get_alert_dispatcher
from skill_evaluate.observability.report_generator import ReportGenerator
from skill_evaluate.persistence.repository import (
    CapabilityRepository,
    JudgeRepository,
    SkillRepository,
    TestCaseRepository,
    TestSuiteRepository,
    TraceRepository,
)
from skill_evaluate.state.enums import Criticality, ExecutorBackendType

# 三个裁量判定全部 ROUTINE（docs/dev/20 第 7、9 节原文口径）：它们都只产生**软性**冲突
# 发现（告警、不阻断），为不阻断的结论花三倍 Token 投票不划算。唯一阻断的基石回归熔断
# 是纯量化判定，不经 LLM。
MULTI_SKILL_CRITICALITY = Criticality.ROUTINE

TEMPLATE_SEMANTIC_FLOW_FRICTION = "semantic_flow_friction"
TEMPLATE_ROLE_PERSONA_CONFLICT = "role_persona_conflict"
TEMPLATE_NEGATIVE_CONSTRAINT_ADHERENCE = "negative_constraint_adherence"

# 深度冲突告警的 `alert_type`（docs/dev/22 第 8 节按它选择 `RESOLVE_DEEP_CONFLICT` 处理路径）。
ALERT_TYPE_DEEP_CONFLICT = "deep_multi_skill_conflict"


@dataclass(slots=True)
class MultiSkillDeps:
    """模块十全部外部依赖的注入点。"""

    executor_backend: ExecutorBackend | None = None
    judge_agent: JudgeAgent | None = None
    test_suite_service: TestSuiteService | None = None
    report_generator: ReportGenerator | None = None
    alert_dispatcher: AlertDispatcher | None = None
    skill_repository: SkillRepository = field(default_factory=SkillRepository)
    test_case_repository: TestCaseRepository = field(default_factory=TestCaseRepository)
    test_suite_repository: TestSuiteRepository = field(default_factory=TestSuiteRepository)
    trace_repository: TraceRepository = field(default_factory=TraceRepository)
    judge_repository: JudgeRepository = field(default_factory=JudgeRepository)
    capability_repository: CapabilityRepository = field(default_factory=CapabilityRepository)
    multi_skill_settings: MultiSkillSettings | None = None
    # None 表示"从配置读 ExecutorSettings.max_concurrent_sandboxes"。
    max_concurrent_sandboxes: int | None = None
    _semaphores: dict[int, asyncio.Semaphore] = field(default_factory=dict)

    def backend(self) -> ExecutorBackend:
        """路由表上的 PLUGGABLE 后端（默认 Hermes）。

        多技能并发要观测的是"沙箱里同时挂着 5 个 Skill 时 Agent 到底加载了哪个"，
        Mini 后端把整篇 SKILL.md 当 prompt、`loaded_skill_md` 恒为 True、也不理会
        `background_skills`——拿它跑出来的是一份永远"零冲突"的报告。
        """
        if self.executor_backend is None:
            self.executor_backend = get_backend_for_node(ROUTING_KEY)
        return self.executor_backend

    def judge(self) -> JudgeAgent:
        """Judge 单例（docs/dev/interfaces/08 第 0 节铁律）。"""
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

    def alerts(self) -> AlertDispatcher:
        """每次调用都回落到**当前**注册的分发器（未注入时）。

        不在构造时缓存：主图通常在应用启动早期装配，而 docs/dev/22 的真实通道可能在
        之后才注册；缓存住就会一直用那个只写日志的默认实现。
        """
        return self.alert_dispatcher or get_alert_dispatcher()

    def settings(self) -> MultiSkillSettings:
        if self.multi_skill_settings is None:
            self.multi_skill_settings = get_settings().multi_skill
        return self.multi_skill_settings

    def semaphore(self) -> asyncio.Semaphore:
        """三条并行探测支路**共用**的沙箱并发信号量（理由同 `CrossModelDeps.semaphore()`）。"""
        loop_id = id(asyncio.get_running_loop())
        if loop_id not in self._semaphores:
            limit = (
                self.max_concurrent_sandboxes
                if self.max_concurrent_sandboxes is not None
                else get_settings().executor.max_concurrent_sandboxes
            )
            self._semaphores = {loop_id: asyncio.Semaphore(max(1, limit))}
        return self._semaphores[loop_id]

    @staticmethod
    def assert_backend_routing() -> None:
        """核对路由表仍把本维度登记为 `PLUGGABLE`（docs/dev/03 第 5 节）。"""
        backend_type = resolve_backend_type(ROUTING_KEY)
        if backend_type is not ExecutorBackendType.PLUGGABLE:
            raise ConfigurationError(
                f"维度 {ROUTING_KEY!r} 的后端路由被声明为 {backend_type.value!r}，但模块十"
                "（docs/dev/20）要求真实沙箱：Mini 后端忽略 background_skills 且 "
                "loaded_skill_md 恒为 True，任何劫持/衰减都观测不到。"
                "请改回 ExecutorBackendType.PLUGGABLE。"
            )


__all__ = [
    "ALERT_TYPE_DEEP_CONFLICT",
    "MULTI_SKILL_CRITICALITY",
    "TEMPLATE_NEGATIVE_CONSTRAINT_ADHERENCE",
    "TEMPLATE_ROLE_PERSONA_CONFLICT",
    "TEMPLATE_SEMANTIC_FLOW_FRICTION",
    "MultiSkillDeps",
]
