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
    """


class PatchApplyError(SkillEvaluateError):
    """unified diff 与当前内容不匹配，补丁无法应用（见 docs/dev/09 第 7 节）。

    典型成因是并发场景下 `base_skill_version_ref` 已过期。调用方
    （`OptimizationLoop`）把它视为本轮尝试失败，**不重试同一个 patch**，直接进入
    下一次 `propose_patch()`。
    """


class PipelineSuspended(SkillEvaluateError):
    """需要 interrupt_before 挂起等待人工审批（见 docs/dev/04、09、22）。"""


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


class ReviewTemplateError(AgentError):
    """Mini Agent 评审模板注册/查找错误（见 docs/dev/07 第 4 节）。"""
