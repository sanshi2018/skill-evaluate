"""Optimizer 的失败上下文与 LLM 输出契约（docs/dev/09 第 3、4 节）。

`FailureContext` 只能经 `service.build_failure_context()` 构造——那里强制执行
"验证集不参与优化"的约束。直接 `FailureContext(...)` 绕过校验的写法在 code
review 阶段视为违反本文档约定（docs/dev/09 第 4 节原文）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from skill_evaluate.state.judge import JudgeVerdict
from skill_evaluate.state.security import SecurityFinding
from skill_evaluate.state.skill import SkillDefinition

# 角色名。`prompt_engineer` 走 description/正文优化，`appsec_expert` 走安全加固
# （docs/dev/09 第 6 节）。用字符串而不是枚举，是为了让后续文档能在不改本文件的
# 前提下注册新角色（见 `service.register_role()`）。
ROLE_PROMPT_ENGINEER = "prompt_engineer"
ROLE_APPSEC_EXPERT = "appsec_expert"


class FailureContext(BaseModel):
    """一次优化闭环的输入：哪份 Skill、哪些用例失败了、裁判怎么说的。"""

    skill: SkillDefinition
    failed_case_ids: list[str]  # 必须全部来自 TRAIN split，由 build_failure_context() 强制
    verdicts: list[JudgeVerdict] = Field(default_factory=list)  # ConsensusResult 请展开后传入
    role: str = ROLE_PROMPT_ENGINEER

    # ---- 以下为实现期追加字段，默认值保证 docs/dev/09 原始构造方式仍然合法 ----
    # 只给 id 的话，Prompt 里就只能写"用例 c-17 失败了"——对模型毫无信息量。
    # 这些内容 `build_failure_context()` 手上本来就有（它收的是 TestCase 对象），
    # 顺手带上比让每个调用方各自再查一次库更省事，也更不容易查错。
    failed_case_prompts: list[str] = Field(default_factory=list)
    triggered_by_finding_id: str | None = None  # 模块五：关联 SecurityFinding
    # docs/dev/15 第 11.1 节追加：安全闭环的"失败原因"是具体的攻击证据，不是常规的
    # Judge reasoning。`appsec_patch.jinja` 在这个列表非空时优先渲染它——一份写着
    # "检测到成功的越权路径读取，证据：read_file(/etc/passwd) exit_code=0" 的上下文，
    # 比一段裁判的自然语言推理更能让模型改对地方。
    # 其余角色恒为空列表，`description_patch.jinja` 不渲染这个变量。
    security_findings: list[SecurityFinding] = Field(default_factory=list)
    target_path: str = "SKILL.md"  # 代码补丁场景由 docs/dev/15 指定具体脚本路径
    extra_instructions: str = ""  # 各维度补充的场景化要求，原样拼进 Prompt


class PatchProposal(BaseModel):
    """模型为一次失败提出的补丁（结构化输出契约）。

    字段是契约，措辞不是：docs/dev/11/15 接入时改 `.jinja` 里的措辞与 few-shot，
    **不要**改这些字段——`Patch` 落库、人工审批卡片、回归结果关联都按字段读取。
    """

    patch_type: Literal["description_patch", "rigid_constraint", "code_patch"]
    target_path: str
    diff: str  # unified diff；整篇重写会在 patch_applier 里因为"没有 @@ hunk"被拒
    rationale: str  # 为什么这么改能解决这次失败
    # docs/dev/09 第 6 节：Prompt 加固场景必须让模型自己评估"这条刚性约束会不会
    # 误伤正常功能路径"，作为人工审查的参考，缓解架构文档点名的"过度杀伤力"风险。
    functional_risk: str = ""


__all__ = [
    "ROLE_APPSEC_EXPERT",
    "ROLE_PROMPT_ENGINEER",
    "FailureContext",
    "PatchProposal",
]
