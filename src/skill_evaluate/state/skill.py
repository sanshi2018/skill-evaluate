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
    # docs/dev/14 第 7 节新增（追加式扩展：可选字段 + 默认 None，不改已有字段语义，
    # `skills.scripts` 是 JSON 列因此无需迁移）。
    #
    # 语义是**三态**而不是布尔：
    # - True  = 静态启发式在脚本正文里看到了写操作（落盘/删除/网络写/DB 写等），
    #           模块四会对它做"连续执行两次"的幂等性探测；
    # - False = 扫过了，没看到任何写操作特征；
    # - None  = **无法判定**（文件读不到、二进制、或该语言不在启发式覆盖范围内）。
    #
    # None 必须与 False 区分开：模块四对 False 是"确认无需做幂等性测试"，对 None
    # 是"跳过并在报告里标注'无法自动判定是否为突变脚本，建议人工确认'"。合并成
    # 布尔会让"没扫出来"被当成"确认安全"，正好是幂等性缺陷最容易溜走的口子。
    is_mutating: bool | None = None


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
