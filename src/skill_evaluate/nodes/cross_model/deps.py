"""模块九节点的依赖容器（依赖注入点）。

与模块一 `TriggerAccuracyDeps` 同一套写法（惰性构造、全部字段有默认、测试按需覆盖），
本维度特有的是**两个**执行后端：

- `primary_backend`：主代理，走路由表（`NODE_BACKEND_ROUTING["cross_model_generalization"]`
  = PLUGGABLE → `ExecutorSettings.backend`，默认 Hermes）；
- `secondary_backend`：备用代理，按名字从注册表取（`CrossModelSettings.secondary_backend`，
  默认 `llama_control`）。

装配期做两条硬校验（见 `assert_backend_routing()` / `assert_heterogeneous()`）：路由必须
是 PLUGGABLE，且主备后端不能是同一个名字——主备相同时"异构矩阵"会拿同一个模型和
自己比，产出一份永远"没有代理差异"的报告，那比不跑更糟。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from skill_evaluate.agents.judge.service import JudgeAgent
from skill_evaluate.config import CrossModelSettings, get_settings
from skill_evaluate.errors import ConfigurationError
from skill_evaluate.executors.base import ExecutorBackend
from skill_evaluate.executors.factory import get_backend_for_node
from skill_evaluate.executors.registry import get_backend, list_registered_backends
from skill_evaluate.executors.routing import resolve_backend_type
from skill_evaluate.nodes.cross_model.state import ROUTING_KEY
from skill_evaluate.observability.report_generator import ReportGenerator
from skill_evaluate.persistence.repository import (
    JudgeRepository,
    SkillRepository,
    TestCaseRepository,
    TestSuiteRepository,
    TraceRepository,
)
from skill_evaluate.state.enums import Criticality, ExecutorBackendType

# 语言坏味道审查的重要度（docs/dev/19 第 8 节）：ROUTINE。本维度整体非阻断，为一条
# 只告警的主观审查花三倍 Token 投票不划算（与模块二同行评审同一取舍）。
LINGUISTIC_SMELL_CRITICALITY = Criticality.ROUTINE
LINGUISTIC_SMELL_TEMPLATE_KEY = "linguistic_smell"


@dataclass(slots=True)
class CrossModelDeps:
    """模块九全部外部依赖的注入点。"""

    primary_backend: ExecutorBackend | None = None
    secondary_backend: ExecutorBackend | None = None
    judge_agent: JudgeAgent | None = None
    report_generator: ReportGenerator | None = None
    skill_repository: SkillRepository = field(default_factory=SkillRepository)
    test_case_repository: TestCaseRepository = field(default_factory=TestCaseRepository)
    test_suite_repository: TestSuiteRepository = field(default_factory=TestSuiteRepository)
    trace_repository: TraceRepository = field(default_factory=TraceRepository)
    judge_repository: JudgeRepository = field(default_factory=JudgeRepository)
    cross_model_settings: CrossModelSettings | None = None
    # None 表示"从配置读 ExecutorSettings.max_concurrent_sandboxes"。
    max_concurrent_sandboxes: int | None = None
    # 按事件循环缓存的共享信号量（见 `semaphore()`）。
    _semaphores: dict[int, asyncio.Semaphore] = field(default_factory=dict)

    def primary(self) -> ExecutorBackend:
        if self.primary_backend is None:
            self.primary_backend = get_backend_for_node(ROUTING_KEY)
        return self.primary_backend

    def secondary(self) -> ExecutorBackend:
        if self.secondary_backend is None:
            self.secondary_backend = get_backend(self.settings().secondary_backend)
        return self.secondary_backend

    def secondary_name(self) -> str:
        """报告里写的备用代理名字。注入了实例时用类名，免得报告写着一个根本没用上的配置值。"""
        if self.secondary_backend is not None:
            return type(self.secondary_backend).__name__
        return self.settings().secondary_backend

    def judge(self) -> JudgeAgent:
        """Judge 单例（docs/dev/interfaces/08 第 0 节铁律）。"""
        if self.judge_agent is None:
            self.judge_agent = JudgeAgent()
        return self.judge_agent

    def reporter(self) -> ReportGenerator:
        if self.report_generator is None:
            self.report_generator = ReportGenerator()
        return self.report_generator

    def settings(self) -> CrossModelSettings:
        if self.cross_model_settings is None:
            self.cross_model_settings = get_settings().cross_model
        return self.cross_model_settings

    def semaphore(self) -> asyncio.Semaphore:
        """三条并行支路**共用**的沙箱并发信号量。

        各支路各建一个的话，并行执行时真实并发会是配置值的三倍——`max_concurrent_sandboxes`
        限的是"整个评测系统同时在飞的沙箱数"，不是"每个节点"。按事件循环缓存：
        `asyncio.Semaphore` 一旦在某个循环里发生过等待就绑定到该循环，跨循环复用会报错
        （单测里每个用例都是新循环）。
        """
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
        """核对路由表仍把本维度登记为 `PLUGGABLE`（docs/dev/03 第 5 节）。

        对照实验要观测的是"真实 Agent 有没有加载 SKILL.md"，Mini 后端的 `loaded_skill_md`
        恒为 True（整篇 SKILL.md 就是它的 prompt），拿它跑出来的每一组对照都"完全一致"。
        """
        backend_type = resolve_backend_type(ROUTING_KEY)
        if backend_type is not ExecutorBackendType.PLUGGABLE:
            raise ConfigurationError(
                f"维度 {ROUTING_KEY!r} 的后端路由被声明为 {backend_type.value!r}，但模块九"
                "（docs/dev/19）的对照实验要求真实执行环境：Mini 后端的 loaded_skill_md 恒为 "
                "True，任何对照都会得出'完全一致'。请改回 ExecutorBackendType.PLUGGABLE。"
            )

    def assert_heterogeneous(self) -> None:
        """主备后端必须是两个不同的已注册后端（只在没有注入实例时按配置名核对）。"""
        if self.secondary_backend is not None:
            return
        secondary = self.settings().secondary_backend
        if secondary not in list_registered_backends():
            raise ConfigurationError(
                f"备用代理后端 {secondary!r} 未注册，已注册：{list_registered_backends()}。"
                "请检查 SKILLEVAL_CROSS_MODEL_SECONDARY_BACKEND。"
            )
        if self.primary_backend is not None:
            return
        configured = get_settings().executor.backend
        # 与 `executors/factory.py::build_backend()` 同一映射：PLUGGABLE 路由下 "mini" 落到 hermes。
        primary = "hermes" if configured == "mini" else configured
        if primary == secondary:
            raise ConfigurationError(
                f"主代理与备用代理是同一个后端 {primary!r}：异构执行矩阵会拿同一个模型和自己"
                "比，永远得出'没有代理差异'。请为 SKILLEVAL_CROSS_MODEL_SECONDARY_BACKEND "
                "配置一个底层架构不同的后端（docs/dev/19 第 3 节）。"
            )


__all__ = [
    "LINGUISTIC_SMELL_CRITICALITY",
    "LINGUISTIC_SMELL_TEMPLATE_KEY",
    "CrossModelDeps",
]
