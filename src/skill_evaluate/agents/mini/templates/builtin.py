"""首批 7 个模板的注册（docs/dev/07 第 5 节）。

覆盖模块二全部（5.1~5.3）、模块四两项（5.4~5.5）、模块三一项（5.7）、模块九一项
（5.6）。其余审查点由各自文档按 `registry.register_template()` 自行新增，不需要
修改本文件——本文件只是"首批"，不是白名单。

导入本模块即完成注册（`agents/mini/__init__.py` 已导入）。
"""

from __future__ import annotations

from skill_evaluate.agents.mini.templates.registry import (
    ReviewTemplate,
    register_template,
    verdict_field_to_status,
)
from skill_evaluate.agents.mini.templates.schemas import (
    ConstructiveErrorOutput,
    ControlCalibrationOutput,
    HelpDocQualityOutput,
    LinguisticSmellOutput,
    OmissionAuditOutput,
    ProgressiveDisclosureStaticOutput,
    ScopingCheckOutput,
)

OMISSION_AUDIT = register_template(
    ReviewTemplate(
        key="omission_audit",
        prompt_path="omission_audit.jinja",
        output_schema=OmissionAuditOutput,
        to_status=verdict_field_to_status,
        required_variables=("skill_md",),
        description="常识剥离度审计（模块二 / docs/dev/12）",
    )
)

SCOPING_CHECK = register_template(
    ReviewTemplate(
        key="scoping_check",
        prompt_path="scoping_check.jinja",
        output_schema=ScopingCheckOutput,
        to_status=verdict_field_to_status,
        required_variables=("skill_md",),
        description="范围连贯性审查（模块二 / docs/dev/12）",
    )
)

PROGRESSIVE_DISCLOSURE_STATIC = register_template(
    ReviewTemplate(
        key="progressive_disclosure_static",
        prompt_path="progressive_disclosure_static.jinja",
        output_schema=ProgressiveDisclosureStaticOutput,
        to_status=verdict_field_to_status,
        required_variables=("skill_md", "reference_files"),
        description="渐进式披露触发条件审查（模块二静态版 / docs/dev/12）",
    )
)

HELP_DOC_QUALITY = register_template(
    ReviewTemplate(
        key="help_doc_quality",
        prompt_path="help_doc_quality.jinja",
        output_schema=HelpDocQualityOutput,
        to_status=verdict_field_to_status,
        required_variables=("script_path", "help_output"),
        description="Help 文档质量审查（模块四 / docs/dev/14）",
    )
)

CONSTRUCTIVE_ERROR = register_template(
    ReviewTemplate(
        key="constructive_error",
        prompt_path="constructive_error.jinja",
        output_schema=ConstructiveErrorOutput,
        to_status=verdict_field_to_status,
        required_variables=("invocation", "error_output"),
        description="建设性报错审查（模块四 / docs/dev/14）",
    )
)

LINGUISTIC_SMELL = register_template(
    ReviewTemplate(
        key="linguistic_smell",
        prompt_path="linguistic_smell.jinja",
        output_schema=LinguisticSmellOutput,
        to_status=verdict_field_to_status,
        required_variables=("skill_md",),
        description="语言坏味道审查（模块九 / docs/dev/19）",
    )
)

CONTROL_CALIBRATION = register_template(
    ReviewTemplate(
        key="control_calibration",
        prompt_path="control_calibration.jinja",
        output_schema=ControlCalibrationOutput,
        to_status=verdict_field_to_status,
        required_variables=("skill_md",),
        description="刚性/柔性控制标定（模块三 / docs/dev/13）",
    )
)

BUILTIN_TEMPLATE_KEYS: tuple[str, ...] = (
    OMISSION_AUDIT.key,
    SCOPING_CHECK.key,
    PROGRESSIVE_DISCLOSURE_STATIC.key,
    HELP_DOC_QUALITY.key,
    CONSTRUCTIVE_ERROR.key,
    LINGUISTIC_SMELL.key,
    CONTROL_CALIBRATION.key,
)

__all__ = ["BUILTIN_TEMPLATE_KEYS"]
