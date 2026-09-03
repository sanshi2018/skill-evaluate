"""模块二节点的依赖容器（依赖注入点）。

与模块一的 `TriggerAccuracyDeps` 同一套理由与写法（惰性构造、全部字段有默认、
测试按需覆盖），差别只在**本维度依赖的东西少得多**：

- 没有 `ExecutorBackend`：本维度评审的是 `SKILL.md` 的静态文本，不执行任何任务，
  因此不需要沙箱，也就不需要 Hermes（docs/dev/12 第 1 节）。
- 没有 `TestSuiteService` / `OptimizerAgent`：不出题、不重试。架构文档模块二本身
  也没有失败重试机制——静态审查发现的问题直接报告给人改。

多出来的一项是 `token_counter`：Token 计数口径是本维度**唯一一个会阻断合并**的
判定的输入，做成注入点，将来要换成官方 API 计数（或在测试里给一个确定性替身）
都不必改节点代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from skill_evaluate.agents.judge.service import JudgeAgent
from skill_evaluate.config import ContextScopingSettings, get_settings
from skill_evaluate.errors import ConfigurationError
from skill_evaluate.executors.routing import resolve_backend_type
from skill_evaluate.ingestion.token_counter import TokenCounter, count_tokens
from skill_evaluate.nodes.context_scoping.state import DIMENSION
from skill_evaluate.observability.report_generator import ReportGenerator
from skill_evaluate.persistence.repository import SkillRepository
from skill_evaluate.state.enums import Criticality, ExecutorBackendType

# 三项同行评审的重要度（docs/dev/12 第 5 节的关键决策）。
#
# 全部声明 ROUTINE 而不是 CRITICAL：架构文档模块二自己就承认"静态审查可能存在
# 纸上谈兵的风险"，应对方式是**降低这些结论的强制力**（非阻断，见 nodes.py 的
# `BLOCKING`），而不是花三倍 Token 去给一个本来就只作参考的建议投票。CRITICAL
# 是留给"高危安全问题""覆盖率不足以合并"这类后果严重的判定的
# （docs/dev/interfaces/08 第 2 节）。
PEER_REVIEW_CRITICALITY = Criticality.ROUTINE

# 本维度并发发起的评审数量。三个模板互不依赖，一次 gather 打完即可——数字写在
# 这里只是为了让"3"这个值有个名字，本维度不存在需要节流的规模（对比模块一的
# `用例数 × 3` 个沙箱）。
PEER_REVIEW_TEMPLATE_KEYS: tuple[str, ...] = (
    "omission_audit",  # 5.1 常识剥离度审计
    "scoping_check",  # 5.2 范围连贯性审查
    "progressive_disclosure_static",  # 5.3 渐进式披露触发条件审查
)


@dataclass(slots=True)
class ContextScopingDeps:
    """模块二全部外部依赖的注入点。"""

    judge_agent: JudgeAgent | None = None
    report_generator: ReportGenerator | None = None
    skill_repository: SkillRepository = field(default_factory=SkillRepository)
    token_counter: TokenCounter = count_tokens
    # None 表示"从配置读 ContextScopingSettings"。测试与"某个仓库想用更严的阈值"
    # 都可以整体覆盖一份 settings，而不是逐个参数传。
    scoping_settings: ContextScopingSettings | None = None

    def judge(self) -> JudgeAgent:
        """Judge 单例。

        必须走 `JudgeAgent`、不能直接调 `MiniReviewAgent`：黄金基准盲测、失误率
        冻结这两项可信度机制都挂在 `judgmental_verdict()` 上，绕过入口 = 绕过机制，
        而报告读者看不出哪条判定绕过了（docs/dev/interfaces/08 第 0 节铁律）。
        """
        if self.judge_agent is None:
            self.judge_agent = JudgeAgent()
        return self.judge_agent

    def reporter(self) -> ReportGenerator:
        if self.report_generator is None:
            self.report_generator = ReportGenerator()
        return self.report_generator

    def settings(self) -> ContextScopingSettings:
        if self.scoping_settings is None:
            self.scoping_settings = get_settings().context_scoping
        return self.scoping_settings

    @staticmethod
    def assert_backend_routing() -> None:
        """核对路由表仍然把本维度登记为 `MINI`（docs/dev/03 第 5 节）。

        本维度**没有**执行后端可用：它不产出 `ExecutionTrace`，也没有任务可以在
        沙箱里跑。若有人把 `NODE_BACKEND_ROUTING["context_scoping"]` 改成
        `PLUGGABLE`，那份声明在这里是无法兑现的——与其让路由表和实现悄悄不一致，
        不如在装配期就报错说清楚。
        """
        backend_type = resolve_backend_type(DIMENSION)
        if backend_type is not ExecutorBackendType.MINI:
            raise ConfigurationError(
                f"维度 {DIMENSION!r} 的后端路由被声明为 {backend_type.value!r}，"
                "但模块二（docs/dev/12）是纯静态文本审查，不执行任何任务、不产出 "
                "ExecutionTrace，无法兑现 PLUGGABLE 声明。"
                "请改回 ExecutorBackendType.MINI，或另开一个维度承载需要真实执行的检查。"
            )


__all__ = [
    "PEER_REVIEW_CRITICALITY",
    "PEER_REVIEW_TEMPLATE_KEYS",
    "ContextScopingDeps",
]
