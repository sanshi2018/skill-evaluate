"""模块三（docs/dev/13）新增的两个评审模板的注册。

**为什么单开一个文件而不是加进 `builtin.py`**：`builtin.py` 的定位是 docs/dev/07
落地时的"首批 7 个"，它自己的文档字符串写明"其余审查点由各自文档按
`register_template()` 自行新增，不需要修改本文件"。按维度分文件之后，"这个模板
是哪份文档引入的、为什么这么判"在文件层面就是清楚的。

导入本模块即完成注册（`agents/mini/__init__.py` 已导入），与量化规则表同一种
"导入即注册"模式：宁可在装配期因为忘了导入而立刻失败，也不要在跑了半小时的
评测中途才发现模板没挂上。
"""

from __future__ import annotations

from skill_evaluate.agents.mini.templates.registry import (
    ReviewTemplate,
    register_template,
    verdict_field_to_status,
)
from skill_evaluate.agents.mini.templates.schemas import (
    RoiComparisonOutput,
    TraceEfficiencyOutput,
)

ROI_COMPARISON = register_template(
    ReviewTemplate(
        key="roi_comparison",
        prompt_path="roi_comparison.jinja",
        output_schema=RoiComparisonOutput,
        # 直接读 `verdict` 字段：两个布尔项是给报告用的诊断细节，判定口径由模板
        # 正文规定（任一项有实质增益即 pass），不在这里再算一遍——算两遍必然漂移。
        to_status=verdict_field_to_status,
        required_variables=(
            "prompt",
            "loaded_final_response",
            "loaded_actions_count",
            "loaded_duration_ms",
            "loaded_total_tokens",
            "baseline_final_response",
            "baseline_actions_count",
            "baseline_duration_ms",
            "baseline_total_tokens",
        ),
        description="A/B 增值对比（ROI 判定）（模块三 / docs/dev/13 第 4.2 节）",
    )
)

TRACE_EFFICIENCY = register_template(
    ReviewTemplate(
        key="trace_efficiency",
        prompt_path="trace_efficiency.jinja",
        output_schema=TraceEfficiencyOutput,
        to_status=verdict_field_to_status,
        required_variables=("actions", "final_response"),
        description="执行轨迹效率损耗诊断（模块三 / docs/dev/13 第 5 节）",
    )
)

INSTRUCTION_CONTROL_TEMPLATE_KEYS: tuple[str, ...] = (
    ROI_COMPARISON.key,
    TRACE_EFFICIENCY.key,
)

__all__ = ["INSTRUCTION_CONTROL_TEMPLATE_KEYS"]
