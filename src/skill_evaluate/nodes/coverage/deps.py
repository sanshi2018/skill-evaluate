"""模块六节点的依赖容器（依赖注入点）。

与模块一~五同一套理由与写法：全部字段有默认值、真实依赖惰性构造（构造 Agent 会
读 API Key 并可能抛 `ConfigurationError`，不该让只做静态检查的调用方受影响）、
测试按需覆盖其中任意一项。

本维度的依赖清单比前几个维度长一截，因为它是**第一个既读又写测试集**的维度：
除了常规的 Judge/报告器/Skill 仓储，它还要读用例（映射）、写用例
（回填 `target_capability_ids`）、读写能力树，并在检出盲区时反向调用 Generator。
这正是架构文档"反向驱动与数据飞轮闭环"的形状。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from skill_evaluate.agents.analyzer.service import AnalyzerAgent
from skill_evaluate.agents.generator.service import TestSuiteService
from skill_evaluate.agents.judge.service import JudgeAgent
from skill_evaluate.config import CoverageSettings, get_settings
from skill_evaluate.errors import ConfigurationError
from skill_evaluate.executors.routing import resolve_backend_type
from skill_evaluate.nodes.coverage.state import ROUTING_KEY
from skill_evaluate.observability.report_generator import ReportGenerator
from skill_evaluate.persistence.repository import (
    CapabilityRepository,
    HumanApprovalRepository,
    JudgeRepository,
    SkillRepository,
    TestCaseRepository,
)
from skill_evaluate.state.enums import ExecutorBackendType

# 反向补盲时写进 `TestSuiteVersion.triggered_by` 的审计值。
# 取值表见 docs/dev/interfaces/06 第 2 节，`coverage_gap` 是那张表里预留给模块
# 六/七的那一项——不要另造新词，排查"测试集为什么突然变了"时靠的就是这张表。
TRIGGERED_BY_COVERAGE_GAP = "coverage_gap"


@dataclass(slots=True)
class CoverageDeps:
    """模块六全部外部依赖的注入点。"""

    analyzer_agent: AnalyzerAgent | None = None
    judge_agent: JudgeAgent | None = None
    suite_service: TestSuiteService | None = None
    report_generator: ReportGenerator | None = None
    skill_repository: SkillRepository = field(default_factory=SkillRepository)
    test_case_repository: TestCaseRepository = field(default_factory=TestCaseRepository)
    capability_repository: CapabilityRepository = field(default_factory=CapabilityRepository)
    judge_repository: JudgeRepository = field(default_factory=JudgeRepository)
    approval_repository: HumanApprovalRepository = field(default_factory=HumanApprovalRepository)
    # None 表示"从配置读 CoverageSettings"。测试与"某个仓库想把达标线放宽到 0.8"
    # 都可以整体覆盖一份 settings，而不是逐个参数传。
    coverage_settings: CoverageSettings | None = None

    def analyzer(self) -> AnalyzerAgent:
        if self.analyzer_agent is None:
            self.analyzer_agent = AnalyzerAgent()
        return self.analyzer_agent

    def judge(self) -> JudgeAgent:
        """Judge 单例。

        本维度只用它的 `quantitative_verdict()`（覆盖率阈值是纯算术），但仍然必须
        走 `JudgeAgent` 而不是在节点里自己写 `if ratio >= threshold`：
        docs/dev/interfaces/08 第 0 节的铁律要求所有通过/失败结论格式统一、可归档、
        可与其他维度并排比较。绕过入口省下的那一行 if，代价是报告读者看不出这条
        结论是怎么来的。
        """
        if self.judge_agent is None:
            self.judge_agent = JudgeAgent()
        return self.judge_agent

    def generator(self) -> TestSuiteService:
        """反向补盲用的测试集服务。

        本维度只调 `incremental_patch()`——那是 `docs/dev/interfaces/06` 第 0 节
        允许流水线**自动**触发的唯一生成模式（它有明确理由：覆盖率盲区）。
        `ensure_test_suite()` / `force_regenerate` 都不在本维度的职责范围内：
        前者是主图入口的事，后者只有人能触发。
        """
        if self.suite_service is None:
            self.suite_service = TestSuiteService()
        return self.suite_service

    def reporter(self) -> ReportGenerator:
        if self.report_generator is None:
            self.report_generator = ReportGenerator()
        return self.report_generator

    def settings(self) -> CoverageSettings:
        if self.coverage_settings is None:
            self.coverage_settings = get_settings().coverage
        return self.coverage_settings

    @staticmethod
    def assert_backend_routing() -> None:
        """核对路由表仍然把 `coverage_analysis` 登记为 `MINI`（docs/dev/03 第 5 节）。

        与模块一/三/四/五那几条断言方向**相反**：那几个维度断言"必须是
        PLUGGABLE"（结论来自真实执行），本维度断言"必须是 MINI"。

        理由不是省钱，而是防一种具体的误解：覆盖率听起来像是"把用例跑一遍看覆盖到
        哪"，但模块六测的是**测试集与声明能力的映射关系**，全程只读 SKILL.md 文本
        与用例 prompt，一次沙箱都不起。若有人把它改成 PLUGGABLE，本维度不会因此
        产出更强的证据（它根本没有调用 `ExecutorBackend` 的代码路径），只会让主图
        为一个永远用不上的执行后端做资源准备，并让读路由表的人以为这里有真实执行。
        """
        backend_type = resolve_backend_type(ROUTING_KEY)
        if backend_type is not ExecutorBackendType.MINI:
            raise ConfigurationError(
                f"维度 {ROUTING_KEY!r} 的后端路由被声明为 {backend_type.value!r}，"
                "但模块六（docs/dev/16）是纯分析维度：能力拆解与用例-能力映射全部基于"
                "SKILL.md 文本与用例 prompt，不执行任何被测代码，也没有调用 "
                "ExecutorBackend 的代码路径。请改回 ExecutorBackendType.MINI。"
            )


__all__ = ["TRIGGERED_BY_COVERAGE_GAP", "CoverageDeps"]
