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


class PipelineSuspended(SkillEvaluateError):
    """需要 interrupt_before 挂起等待人工审批（见 docs/dev/04、09、22）。"""


class PersistenceError(SkillEvaluateError):
    """持久化层读写失败（见 docs/dev/04）。"""


class ObservabilityError(SkillEvaluateError):
    """报告生成/双写适配器相关错误（见 docs/dev/05）。"""
