"""项目级异常体系。

约定（见 docs/dev/01_项目脚手架与技术栈基线.md 第 7 节）：
- 所有模块禁止裸抛 `Exception`，一律继承自 `SkillEvaluateError`。
- 后续文档新增子类需继承本文件中的基类，不得脱离体系新建根异常。
"""

from __future__ import annotations


class SkillEvaluateError(Exception):
    """项目级异常基类。"""


class ConfigurationError(SkillEvaluateError):
    """配置缺失或非法。"""


class ExecutorBackendError(SkillEvaluateError):
    """执行引擎适配层错误（超时/挂起/协议不匹配等，见 docs/dev/03）。"""


class JudgeConsensusError(SkillEvaluateError):
    """裁判未达成共识，需要人工仲裁（见 docs/dev/08）。"""


class JudgeRuleError(SkillEvaluateError):
    """量化判定规则注册/查找错误（见 docs/dev/08 第 2 节 `judge/rules.py`）。

    与 `ReviewTemplateError` 对称：注册期的重名、调用期的未知 `rule_name` 都在这里
    显式报错，而不是返回一个"默认 PASS"——判定规则找不到时静默放行，等于把一个
    评测维度悄悄关掉了。
    """


class JudgeFrozenError(SkillEvaluateError):
    """该 Judge 配置（model + temperature 分桶）因黄金基准失误率超阈值被冻结（见 docs/dev/08 第 3.3 节）。

    语义约定：抛出后**不允许**调用方降级为"那就当它 PASS 吧"继续跑——一个已被
    证明会误判的裁判给出的任何结论都不该进报告。正确处理是让流水线整体挂起，
    等人工调整 Prompt / 更换模型后解冻（docs/dev/22 审批工作台）。

    `model` / `temperature`（docs/dev/22 追加，可选、向后兼容）：审批卡片据此定位要解冻的
    是哪一个 `(model, temperature_bucket)` 配置——解冻 API 需要这两个值，只有一段报错文本
    的话，人得自己去 `judge_health_status` 表里猜。
    """

    def __init__(
        self, message: str, *, model: str | None = None, temperature: float | None = None
    ) -> None:
        super().__init__(message)
        self.model = model
        self.temperature = temperature


class PatchApplyError(SkillEvaluateError):
    """unified diff 与当前内容不匹配，补丁无法应用（见 docs/dev/09 第 7 节）。

    典型成因是并发场景下 `base_skill_version_ref` 已过期。调用方
    （`OptimizationLoop`）把它视为本轮尝试失败，**不重试同一个 patch**，直接进入
    下一次 `propose_patch()`。
    """


class PipelineSuspended(SkillEvaluateError):
    """需要 interrupt_before 挂起等待人工审批（见 docs/dev/04、09、22）。"""


class HumanRejectedSuspension(PipelineSuspended):
    """人工已经在审批卡片上明确说"不"（放弃补丁 / 不确认能力树 / 放弃本次评测）后抛出（docs/dev/22）。

    继承 `PipelineSuspended`：对流水线而言语义不变（整体停下、不产出假结论），既有
    `except PipelineSuspended` / `pytest.raises(PipelineSuspended)` 的调用方无需改动。

    单独建子类是为了让 `nodes/approval_guard.py` 分得清两种挂起：
    - 普通 `PipelineSuspended`（如共识未达成）——**还没问过人**，guard 应当发起一张
      `ABANDON_RUN` 审批卡片让人决定重试还是放弃；
    - 本类——**人已经决定过了**，guard 必须原样放行，否则人刚点完"放弃"又会收到一张
      "要不要放弃"的卡片。
    """


class InfrastructureEnvironmentError(PipelineSuspended):
    """评测系统**自身**的基础设施异常：沙箱指纹漂移 / 金丝雀探针失败（docs/dev/21 第 4、5 节）。

    继承 `PipelineSuspended` 而不是另起一支：对流水线而言它的语义就是"整体挂起、不产出
    任何维度结论"，现有接住 `PipelineSuspended` 的调用方（docs/dev/24 主图入口）不需要
    改动就能正确处理。单独建子类是为了让 docs/dev/22 的审批工作台能把它与"裁判未达成
    共识"这类**业务性**挂起区分开：前者该找运维修环境，后者该找评测负责人仲裁，而这次
    失败与被测 Skill 的质量无关，绝不能记成某个维度 FAIL。

    `gate` 标明是哪道门禁拦下的；`details` 是给人看的差异/失败原因清单（字符串列表，
    可直接进告警 payload 与审批卡片）。
    """

    def __init__(self, message: str, *, gate: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.gate = gate  # "sandbox_fingerprint" | "canary_probe"
        self.details = list(details or [])


class PersistenceError(SkillEvaluateError):
    """持久化层读写失败（见 docs/dev/04）。"""


class ObservabilityError(SkillEvaluateError):
    """报告生成/双写适配器相关错误（见 docs/dev/05）。"""


class AgentError(SkillEvaluateError):
    """Agent 层通用错误基类（见 docs/dev/06~10）。"""


class AgentResponseFormatError(AgentError):
    """LLM 输出无法解析为约定的结构化 schema，且已用尽重试次数（见 docs/dev/06 第 5.3 节）。"""


class GenerationError(AgentError):
    """Generator Agent 生成失败（docs/dev/06 第 5.3 节 `GenerationFailure`）。

    语义约定：一旦抛出，本批次**不产出任何半成品用例集**——调用方不得吞掉本异常
    然后继续用一个残缺的 TestSuiteVersion 跑流水线。
    """


class GenerationCollapseError(GenerationError):
    """本批用例被判定为语义坍塌，已阻断激活（docs/dev/21 第 2 节）。

    继承 `GenerationError`：既有调用方（模块六/十等"接住 GenerationError 标记补盲耗尽"）
    不改一行代码即可正确降级。需要区分坍塌与其他生成失败的调用方（docs/dev/22 的
    `INJECT_NEW_SEED` 审批路径）按本子类捕获，读下面两个字段：

    - `consecutive_collapses`：同一 skill_id 自上一次成功激活以来的连续坍塌次数；
    - `requires_human_seed`：已达到 `GeneratorTrustSettings.max_consecutive_collapses`，
      继续让机器重试只会原地打转，应当挂起等人工注入新种子。
    """

    def __init__(
        self,
        message: str,
        *,
        skill_id: str,
        consecutive_collapses: int,
        requires_human_seed: bool,
    ) -> None:
        super().__init__(message)
        self.skill_id = skill_id
        self.consecutive_collapses = consecutive_collapses
        self.requires_human_seed = requires_human_seed


class ReviewTemplateError(AgentError):
    """Mini Agent 评审模板注册/查找错误（见 docs/dev/07 第 4 节）。"""
