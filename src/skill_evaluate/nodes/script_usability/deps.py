"""模块四节点的依赖容器（依赖注入点）。

与模块一/二/三同一套理由与写法（惰性构造、全部字段有默认、测试按需覆盖），
本维度的特殊之处只有一个：**它不持有 `ExecutorBackend`**，而是持有一个
`ScriptSandboxRunner`。理由见 `executors/script_sandbox.py` 的模块头——本维度做的
是裸调脚本子进程，没有 Agent 推理循环，硬塞进 `ExecutorBackend` 只会产出假 Trace。

因此路由表里 `script_usability` 那条 `PLUGGABLE` 声明，在本维度的兑现方式是
"真的起容器执行"，而不是"走 HermesBackend"（见 `assert_backend_routing()`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from skill_evaluate.agents.judge.service import JudgeAgent
from skill_evaluate.config import ScriptUsabilitySettings, get_settings
from skill_evaluate.errors import ConfigurationError
from skill_evaluate.executors.routing import resolve_backend_type
from skill_evaluate.executors.script_sandbox import DockerScriptSandboxRunner, ScriptSandboxRunner
from skill_evaluate.nodes.script_usability.state import DIMENSION
from skill_evaluate.observability.report_generator import ReportGenerator
from skill_evaluate.persistence.repository import JudgeRepository, SkillRepository
from skill_evaluate.state.enums import Criticality, ExecutorBackendType

# 两项 LLM 审查的重要度（docs/dev/14 第 8 节的阻断策略在成本侧的对应物）。
#
# 都声明 ROUTINE 而不是 CRITICAL：这两条结论**本来就不阻断合并**（"文档写得好不好"
# "报错够不够建设性"是主观审查），给一个只作参考的建议投三次票是纯粹的浪费。
# CRITICAL 留给"会直接决定要不要合并"的判定（模块三的 ROI、模块五的安全等级）。
HELP_REVIEW_CRITICALITY = Criticality.ROUTINE
ERROR_REVIEW_CRITICALITY = Criticality.ROUTINE

# 两个评审模板的 key（docs/dev/07 模板 5.4 / 5.5，早已注册在 `agents/mini/templates/`）。
TEMPLATE_HELP_DOC_QUALITY = "help_doc_quality"
TEMPLATE_CONSTRUCTIVE_ERROR = "constructive_error"


@dataclass(slots=True)
class ScriptUsabilityDeps:
    """模块四全部外部依赖的注入点。"""

    sandbox_runner: ScriptSandboxRunner | None = None
    judge_agent: JudgeAgent | None = None
    report_generator: ReportGenerator | None = None
    skill_repository: SkillRepository = field(default_factory=SkillRepository)
    judge_repository: JudgeRepository = field(default_factory=JudgeRepository)
    # None 表示"从配置读 ScriptUsabilitySettings"。测试与"某个仓库想用更长的超时"
    # 都可以整体覆盖一份 settings，而不是逐个参数传。
    usability_settings: ScriptUsabilitySettings | None = None
    max_concurrent_scripts: int | None = None

    def sandbox(self) -> ScriptSandboxRunner:
        """脚本沙箱执行器。

        默认是 `DockerScriptSandboxRunner`（Ephemeral 容器 + 无出站网络 + 资源墙）。
        docs/dev/15 的红队脚本注入测试要复用本维度的执行链路时，注入自己的实现
        即可，节点代码不需要改动。
        """
        if self.sandbox_runner is None:
            self.sandbox_runner = DockerScriptSandboxRunner(self.settings())
        return self.sandbox_runner

    def judge(self) -> JudgeAgent:
        """Judge 单例。

        必须走 `JudgeAgent`、不能直接调 `MiniReviewAgent`，也不能在节点里自己写
        if/else 下结论：黄金基准盲测、共识投票、失误率冻结这三项可信度机制都挂在
        它的两个方法上，绕过入口 = 绕过机制，而报告读者看不出哪条判定绕过了
        （docs/dev/interfaces/08 第 0 节铁律）。
        """
        if self.judge_agent is None:
            self.judge_agent = JudgeAgent()
        return self.judge_agent

    def reporter(self) -> ReportGenerator:
        if self.report_generator is None:
            self.report_generator = ReportGenerator()
        return self.report_generator

    def settings(self) -> ScriptUsabilitySettings:
        if self.usability_settings is None:
            self.usability_settings = get_settings().script_usability
        return self.usability_settings

    def concurrency_limit(self) -> int:
        """同时在飞的探测容器数上限。

        默认复用 `ExecutorSettings.max_concurrent_sandboxes`：脚本探测容器比 Agent
        沙箱轻得多，但它们跑在同一台机器上，两处各设一套上限只会让"到底能同时跑
        多少个容器"没人算得清。确实需要区别对待时用
        `SKILLEVAL_SCRIPT_USABILITY_MAX_CONCURRENT_SCRIPTS` 单独覆盖。
        """
        if self.max_concurrent_scripts is not None:
            return max(1, self.max_concurrent_scripts)
        configured = self.settings().max_concurrent_scripts
        if configured is not None:
            return max(1, configured)
        return max(1, get_settings().executor.max_concurrent_sandboxes)

    @staticmethod
    def assert_backend_routing() -> None:
        """核对路由表仍然把本维度登记为 `PLUGGABLE`（docs/dev/03 第 5 节）。

        与模块三那条断言方向相同、兑现方式不同：本维度**必须**有真实执行环境
        （黑盒探测的全部结论都来自真实的 exit_code/stdout/stderr），但它兑现
        `PLUGGABLE` 的方式是 `ScriptSandboxRunner` 起容器，而不是走 HermesBackend
        ——路由表登记的是"这个维度需要真实执行环境"，不是"必须用哪个类"。

        若有人把它改成 `MINI`，得到的会是一份基于静态猜测的脚本质量报告，那比没有
        报告更危险。与其运行到一半才发现，不如在建图时报错说清楚。
        """
        backend_type = resolve_backend_type(DIMENSION)
        if backend_type is not ExecutorBackendType.PLUGGABLE:
            raise ConfigurationError(
                f"维度 {DIMENSION!r} 的后端路由被声明为 {backend_type.value!r}，"
                "但模块四（docs/dev/14）是黑盒探测：挂起、--help 输出、脏数据报错、"
                "幂等性全部来自真实子进程的执行结果，Mini 后端一项都给不出。"
                "请改回 ExecutorBackendType.PLUGGABLE（本维度以 ScriptSandboxRunner "
                "起 Ephemeral 容器的方式兑现该声明，不经 HermesBackend）。"
            )


__all__ = [
    "ERROR_REVIEW_CRITICALITY",
    "HELP_REVIEW_CRITICALITY",
    "TEMPLATE_CONSTRUCTIVE_ERROR",
    "TEMPLATE_HELP_DOC_QUALITY",
    "ScriptUsabilityDeps",
]
