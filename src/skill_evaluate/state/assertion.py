"""断言规格（docs/dev/02 第 10 节，对应架构文档模块三第 5 节 Validator Agent）。"""

from __future__ import annotations

from pydantic import BaseModel

from skill_evaluate.state.enums import AssertionStrategy


class AssertionSpec(BaseModel):
    assertion_id: str
    case_id: str
    strategy: AssertionStrategy
    template_ref: str | None = None  # 命中的 Git 断言库模板路径
    script_path: str | None = None  # 落盘的校验脚本路径（沙箱内）
    language: str = "python"


class AssertionResult(BaseModel):
    assertion_id: str
    exit_code: int
    stdout: str
    stderr: str
    passed: bool
