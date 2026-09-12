"""模块八（docs/dev/18）新增的评审模板的注册。

按维度分文件，与 `instruction_control.py` / `security.py` 同一种组织方式：
`builtin.py` 是 docs/dev/07 落地时的"首批 7 个"，后续文档各自新增各自的文件，
这样"这个模板是哪份文档引入的、为什么这么判"在文件层面就是清楚的。

导入本模块即完成注册（`agents/mini/__init__.py` 已导入），与量化规则表同一种
"导入即注册"模式。

## 为什么这条判定要经 Judge 而能力映射不用

`AnalyzerAgent.map_case_to_capabilities()` 刻意**不**走 `JudgeAgent`：它产出的是
一组 id，没有通过/失败语义（理由见 `agents/analyzer/service.py` 模块头）。

反事实覆盖判定不一样——它的产出就是一个"这条用例到底算不算测到了这条约束"的
二元结论，形状与 docs/dev/interfaces/08 第 0 节铁律管辖的判定完全一致，因此走
`judgmental_verdict()`（`Criticality.ROUTINE`，docs/dev/18 第 4.1 节）。走这条路
顺带拿到黄金基准盲测：约束覆盖率是模块八的两个分数之一，用一个从没被考核过的
裁判去算它，那个数字就没有可信度可言。
"""

from __future__ import annotations

from skill_evaluate.agents.mini.templates.registry import (
    ReviewTemplate,
    register_template,
    verdict_field_to_status,
)
from skill_evaluate.agents.mini.templates.schemas import NegativeConstraintProbeOutput

NEGATIVE_CONSTRAINT_PROBE = register_template(
    ReviewTemplate(
        key="negative_constraint_probe",
        prompt_path="negative_constraint_probe.jinja",
        output_schema=NegativeConstraintProbeOutput,
        # 直接读 `verdict`：两个布尔项是给报告与人工复核用的诊断细节，判定口径
        # （两条判据同时满足才算覆盖）由模板正文规定，不在这里再算一遍——算两遍
        # 必然漂移（与 docs/dev/13/15 的模板同一条约定）。
        to_status=verdict_field_to_status,
        # `case_expected_output` 也列为必填：模板用 `StrictUndefined` 渲染，
        # "这条用例没有期望产出"必须由调用方显式传一个空串表达，而不是靠不传。
        # 不传的表现是渲染期 `UndefinedError`，正好在装配/自测阶段暴露；列进来
        # 则让"忘了传"在 `render()` 的第一行就带着模板 key 报出来。
        required_variables=(
            "constraint_description",
            "case_category",
            "case_prompt",
            "case_expected_output",
        ),
        description="负向约束的反事实覆盖判定（模块八 / docs/dev/18 第 4.1 节）",
    )
)

WEIGHTED_COVERAGE_TEMPLATE_KEYS: tuple[str, ...] = (NEGATIVE_CONSTRAINT_PROBE.key,)

__all__ = ["NEGATIVE_CONSTRAINT_PROBE", "WEIGHTED_COVERAGE_TEMPLATE_KEYS"]
