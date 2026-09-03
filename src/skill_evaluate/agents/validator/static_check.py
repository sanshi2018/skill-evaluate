"""生成脚本的本地静态检查（docs/dev/10 第 6 节）。

`generated_from_scratch` / `template_inherit` 产出的脚本本身也是"LLM 写的代码"，
架构文档点名了"验证器自身幻觉"的风险。这里做的是最低成本、零副作用的那一道
防线：**只做语法检查，不执行任何代码**。

- Python：`ast.parse()`（纯解析，不 import、不执行模块级语句）。
- Bash：`bash -n`（只解析不执行）。宿主没有 bash 时降级为 `SKIPPED`——报告里
  能看到"这条断言没做过语法检查"，比假装检查过要诚实。

检查在**本地进程**做，不进沙箱：一段语法就不对的脚本没必要为它开一次容器。
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from skill_evaluate.logging import get_logger

logger = get_logger(component="assertion_static_check")

LANGUAGE_PYTHON = "python"
LANGUAGE_BASH = "bash"
SUPPORTED_LANGUAGES = frozenset({LANGUAGE_PYTHON, LANGUAGE_BASH})


class StaticCheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"  # 无法在当前宿主上验证（例如没装 bash），不视为失败


@dataclass(frozen=True, slots=True)
class StaticCheckResult:
    status: StaticCheckStatus
    detail: str = ""

    @property
    def ok(self) -> bool:
        """SKIPPED 也算通过：无法验证 != 验证不通过。"""
        return self.status is not StaticCheckStatus.FAILED


def check_script_syntax(script: str, language: str) -> StaticCheckResult:
    """按语言做语法检查。未知语言直接判 FAILED——不认识的语言不该被下发执行。"""
    if not script.strip():
        return StaticCheckResult(StaticCheckStatus.FAILED, "脚本为空")
    if language == LANGUAGE_PYTHON:
        return _check_python(script)
    if language == LANGUAGE_BASH:
        return _check_bash(script)
    return StaticCheckResult(
        StaticCheckStatus.FAILED,
        f"不支持的脚本语言 {language!r}（支持：{sorted(SUPPORTED_LANGUAGES)}）",
    )


def _check_python(script: str) -> StaticCheckResult:
    try:
        ast.parse(script)
    except SyntaxError as exc:
        return StaticCheckResult(
            StaticCheckStatus.FAILED,
            f"Python 语法错误：第 {exc.lineno} 行 {exc.msg}",
        )
    return StaticCheckResult(StaticCheckStatus.PASSED)


def _check_bash(script: str) -> StaticCheckResult:
    bash = shutil.which("bash")
    if bash is None:
        logger.warning("assertion_bash_check_skipped", reason="宿主未安装 bash")
        return StaticCheckResult(StaticCheckStatus.SKIPPED, "宿主未安装 bash，跳过 `bash -n`")

    with tempfile.TemporaryDirectory(prefix="skilleval-assert-") as tmp:
        path = Path(tmp) / "candidate.sh"
        path.write_text(script, encoding="utf-8")
        try:
            completed = subprocess.run(
                [bash, "-n", str(path)],  # 固定命令，`-n` 只解析不执行
                capture_output=True,
                text=True,
                timeout=30,
                check=False,  # 非 0 就是"语法不过"，是本函数的正常返回路径
            )
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - 环境异常
            return StaticCheckResult(StaticCheckStatus.SKIPPED, f"`bash -n` 无法执行：{exc}")

    if completed.returncode != 0:
        return StaticCheckResult(
            StaticCheckStatus.FAILED,
            f"Bash 语法错误：{(completed.stderr or completed.stdout).strip()[:500]}",
        )
    return StaticCheckResult(StaticCheckStatus.PASSED)


__all__ = [
    "LANGUAGE_BASH",
    "LANGUAGE_PYTHON",
    "SUPPORTED_LANGUAGES",
    "StaticCheckResult",
    "StaticCheckStatus",
    "check_script_syntax",
]
