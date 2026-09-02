"""Skill 结构化表示（docs/dev/02 第 4 节）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SkillReferenceFile(BaseModel):
    path: str  # 相对 skill 根目录的路径，如 "references/errors.md"
    trigger_condition: str | None = None  # 主 SKILL.md 中声明的按需加载触发条件（模块二审查用）
    token_estimate: int | None = None


class SkillScript(BaseModel):
    path: str  # 如 "scripts/parse_csv.py"
    exposed_tool_name: str | None = None  # 模块十命名空间冲突检测用
    supports_help_flag: bool | None = None  # 模块四黑盒探测填充


class SkillDefinition(BaseModel):
    """对一份被测 Skill 的静态结构化快照，来自解析 SKILL.md 及其目录。"""

    skill_id: str  # 稳定标识，建议用仓库路径的 slug
    version_ref: str  # git commit sha 或 tag，保证可追溯
    root_path: str
    description: str  # SKILL.md 顶部 description 字段原文
    body_markdown: str  # SKILL.md 正文全文
    line_count: int
    token_count: int
    reference_files: list[SkillReferenceFile] = Field(default_factory=list)
    scripts: list[SkillScript] = Field(default_factory=list)
