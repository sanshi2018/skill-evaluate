"""模块五（docs/dev/15）新增的两个评审模板的注册。

按维度分文件，与 `instruction_control.py` 同一种组织方式：`builtin.py` 是
docs/dev/07 落地时的"首批 7 个"，后续文档各自新增各自的文件，这样"这个模板是哪份
文档引入的、为什么这么判"在文件层面就是清楚的。

导入本模块即完成注册（`agents/mini/__init__.py` 已导入），与量化规则表同一种
"导入即注册"模式。

## 本文件是 `ReviewTemplate.to_severity` 的第一个（也是目前唯一的）使用者

docs/dev/07 在 `ReviewTemplate` 上预留了 `to_severity`，注释里写明"docs/dev/15
接入时不需要修改 ReviewTemplate 或 MiniReviewAgent"。落地时对后半句做了**一处
收窄**：`MiniReviewAgent.review_detailed()` 里加了一行，把 `to_severity` 的结果
回填进 `JudgeVerdict.severity`。

为什么必须这么改：docs/dev/07 的设想是"需要严重级别的调用方走 `review_detailed()`
自己算"，但 docs/dev/interfaces/08 第 0 节的铁律要求**一切判定走
`judgmental_verdict()`**，而它只返回 `JudgeVerdict` / `ConsensusResult`。两条约定
放在一起，就只剩"让 JudgeVerdict 带上 severity"这一条路——否则模块五要拿到定级
就必须绕过黄金盲测与共识投票，而安全定级恰恰是最不该绕过它们的那一类判定。
"""

from __future__ import annotations

from typing import Any

from skill_evaluate.agents.mini.templates.registry import (
    ReviewTemplate,
    register_template,
    verdict_field_to_status,
)
from skill_evaluate.agents.mini.templates.schemas import (
    PromptInjectionDefenseOutput,
    SecuritySeverityRatingOutput,
)
from skill_evaluate.state.enums import SeverityLevel


def severity_field_to_severity(output: Any) -> SeverityLevel:
    """把结构化输出里的 `severity` 字面量收敛成 `SeverityLevel`。

    Schema 层用的是 `Literal["critical", "high", "medium", "low"]`，取值范围已经在
    Pydantic 校验阶段兜住了，这里只做一次枚举构造，不再重复校验——重复校验意味着
    两处口径，早晚会漂移。
    """
    return SeverityLevel(output.severity)


PROMPT_INJECTION_DEFENSE = register_template(
    ReviewTemplate(
        key="prompt_injection_defense",
        prompt_path="prompt_injection_defense.jinja",
        output_schema=PromptInjectionDefenseOutput,
        # 直接读 `verdict`：三个布尔项是给报告与人工复核用的诊断细节，判定口径由
        # 模板正文规定（拒绝或约束回范围内即 pass），不在这里再算一遍。
        to_status=verdict_field_to_status,
        required_variables=("skill_description", "prompt", "actions", "final_response"),
        description="提示词注入防御判定（模块五 / docs/dev/15 第 4 节）",
    )
)

SECURITY_SEVERITY_RATING = register_template(
    ReviewTemplate(
        key="security_severity_rating",
        prompt_path="security_severity_rating.jinja",
        output_schema=SecuritySeverityRatingOutput,
        # 两条映射并存（docs/dev/15 第 10.1 节）：
        # - `to_status` 供常规报告聚合（`dimension_results` 只认 pass/fail）；
        # - `to_severity` 供定级节点做最终裁定。
        # 刻意**不**让 to_status 去读 severity 再折算一遍：模板正文已经规定了
        # "low 填 pass、medium 及以上填 fail"，在代码里再算一次就是两处口径。
        to_status=verdict_field_to_status,
        to_severity=severity_field_to_severity,
        required_variables=("category", "initial_severity", "evidence"),
        description="安全发现的严重性最终裁定（模块五 / docs/dev/15 第 10 节）",
    )
)

SECURITY_TEMPLATE_KEYS: tuple[str, ...] = (
    PROMPT_INJECTION_DEFENSE.key,
    SECURITY_SEVERITY_RATING.key,
)

__all__ = ["SECURITY_TEMPLATE_KEYS", "severity_field_to_severity"]
