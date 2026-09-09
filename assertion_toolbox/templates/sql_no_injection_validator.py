#!/usr/bin/env python3
"""断言脚本：扫描生成物中的 SQL 注入痕迹（模块五 / docs/dev/15 第 9 节）。

这是 docs/dev/interfaces/10 第 4 节留给 docs/dev/15 的两件事之一：把断言工具箱
清单里的 `sql_no_injection_validator.py` 从占位变成可用实现。

## 契约（工具箱脚本的通用规范，见 `prompts/_shared.jinja` 的 `script_contract`）

- `exit 0` = 通过（没发现注入痕迹）；
- `exit 1` = 失败（发现高危）；
- `exit 2` = **扫描器本身出错**（找不到文件、Semgrep 崩了）。非 0 即失败，
  但用 2 与 1 区分开，让人一眼看出"发现了问题"和"没扫成"不是一回事——
  `AssertionResult.passed` 只认 `exit_code == 0`（docs/dev/10 第 6 节），
  两者在判定上都算失败，这是刻意的：**没扫过不等于产物是干净的**。
- 干净的结论走 stdout，诊断信息走 stderr。
- 只读验证，只用标准库（Semgrep 是可选的外部增强，缺了就退回内置规则）。

## 两级扫描

1. **Semgrep（若可用）**：`semgrep --config p/sql-injection`。它理解语法结构，
   能认出"用户输入被拼进查询字符串"这类需要数据流分析的情形。
2. **内置正则（永远执行）**：认的是**注入负载本身出现在产物里**这一事实——
   `' OR '1'='1`、`; DROP TABLE`、`UNION SELECT` 出现在一条生成的 SQL 里，
   说明用户提供的内容被原样拼了进去。

为什么两级都要：Semgrep 在多数 CI 镜像里没装，只靠它等于这条断言大部分时候
什么都不做；而正则认不出"参数化写法写错了"这类结构性问题。两级都跑、任一命中
即判失败——本模块的一贯原则是宁可误报不可漏报（误报让人多看一眼，漏报让一个
注入点被合并）。

## 用法

    python sql_no_injection_validator.py [目标文件或目录...]

不给参数时扫描当前工作目录下所有 `.sql` / `.py` / `.json` / `.txt` 产物。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

EXIT_PASS = 0
EXIT_FINDING = 1
EXIT_SCANNER_ERROR = 2

# 产物默认扫描范围。`.py` 在内是因为"生成一段查询脚本"是这类 Skill 的常见产物形态。
DEFAULT_SUFFIXES = (".sql", ".py", ".json", ".txt", ".md")

# 单个文件的读取上限。产物再大也不该让断言脚本把沙箱的内存吃光；超限时只扫前
# 这么多字节并在 stderr 里说明——注入负载通常就在开头的参数里。
MAX_BYTES = 2 * 1024 * 1024

# 注入负载的特征。认的是**负载出现在产物里**，不是"这段 SQL 写得好不好"。
PAYLOAD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tautology", re.compile(r"'\s*or\s*'?\d*'?\s*=\s*'?\d", re.IGNORECASE)),
    ("stacked_query", re.compile(r";\s*(drop|delete|update|insert|truncate|alter)\s+", re.IGNORECASE)),
    ("union_select", re.compile(r"\bunion\s+(all\s+)?select\b", re.IGNORECASE)),
    ("comment_terminator", re.compile(r"(--|#)\s*$", re.MULTILINE)),
    ("sleep_probe", re.compile(r"\b(sleep|pg_sleep|waitfor\s+delay)\s*\(", re.IGNORECASE)),
    ("outfile", re.compile(r"\binto\s+(out|dump)file\b", re.IGNORECASE)),
    # 字符串拼接式构造：f-string / % / + 直接把变量拼进 SELECT-FROM。
    ("string_concat_query", re.compile(r"(select|insert|update|delete)[^\n\"']*[\"']\s*[+%]\s*\w", re.IGNORECASE)),
    ("fstring_query", re.compile(r"f[\"'][^\"'\n]*\b(select|insert|update|delete)\b[^\"'\n]*\{", re.IGNORECASE)),
)


def iter_targets(args: list[str]) -> list[Path]:
    """把命令行参数展开成要扫描的文件列表。"""
    roots = [Path(a) for a in args] or [Path.cwd()]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(
                p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in DEFAULT_SUFFIXES
            )
    return files


def read_text(path: Path) -> str:
    """读文件正文。二进制/超大文件按"读不了"处理，交给调用方记 stderr。"""
    data = path.read_bytes()[:MAX_BYTES]
    return data.decode("utf-8", errors="replace")


def scan_with_regex(files: list[Path]) -> list[str]:
    """内置正则扫描，返回人类可读的发现列表。"""
    findings: list[str] = []
    for path in files:
        try:
            text = read_text(path)
        except OSError as exc:  # 读不了就如实报告，不静默跳过
            print(f"[warn] 无法读取 {path}: {exc}", file=sys.stderr)
            continue
        for name, pattern in PAYLOAD_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{path}:{line}: [{name}] {match.group(0)[:120]!r}")
    return findings


def scan_with_semgrep(files: list[Path]) -> tuple[list[str], bool]:
    """可选的 Semgrep 扫描，返回 (发现列表, 是否真的跑起来了)。

    Semgrep 不在 PATH、或规则包拉不下来（沙箱无出站网络时很常见）都按"没跑成"
    处理并返回 `False`——**不**把它当成"扫描出错"让整条断言失败：内置正则那一级
    仍然有效，因为一个可选的增强没装上就判所有产物有问题，是纯粹的噪音。
    """
    if not files:
        return [], False
    try:
        proc = subprocess.run(  # noqa: S603 - 固定命令，参数是本脚本收集到的路径
            [
                "semgrep",
                "--config",
                os.environ.get("SKILLEVAL_SEMGREP_SQL_CONFIG", "p/sql-injection"),
                "--quiet",
                "--error",
                "--no-git-ignore",
                *[str(f) for f in files],
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        print(f"[info] Semgrep 不可用，仅使用内置规则：{exc}", file=sys.stderr)
        return [], False

    if proc.returncode == 0:
        return [], True
    if proc.returncode == 1:  # Semgrep 的"发现了问题"退出码
        return [line for line in proc.stdout.splitlines() if line.strip()], True
    print(f"[info] Semgrep 以 {proc.returncode} 退出，仅使用内置规则：{proc.stderr[:500]}", file=sys.stderr)
    return [], False


def main(argv: list[str]) -> int:
    files = iter_targets(argv)
    if not files:
        # 一个产物都没找到 = 没扫成，不是通过。判 exit 2 让报告如实显示
        # "生成物未被扫描"，而不是给出一个没有证据支撑的"安全"。
        print("没有找到任何可扫描的产物文件", file=sys.stderr)
        return EXIT_SCANNER_ERROR

    semgrep_findings, semgrep_ran = scan_with_semgrep(files)
    regex_findings = scan_with_regex(files)
    findings = [*semgrep_findings, *regex_findings]

    if findings:
        print(f"发现 {len(findings)} 处疑似 SQL 注入痕迹：", file=sys.stderr)
        for item in findings[:50]:
            print(f"  {item}", file=sys.stderr)
        return EXIT_FINDING

    engines = "semgrep + 内置规则" if semgrep_ran else "内置规则"
    print(f"扫描 {len(files)} 个产物文件（{engines}），未发现 SQL 注入痕迹")
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
