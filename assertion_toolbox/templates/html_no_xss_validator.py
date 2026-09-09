#!/usr/bin/env python3
"""断言脚本：扫描生成物中的 XSS 痕迹（模块五 / docs/dev/15 第 9 节）。

与 `sql_no_injection_validator.py` 同一套契约与两级扫描结构（Semgrep 可选 + 内置
规则永远执行），只是换了攻击面：这一份针对"报告生成器"类 Skill——它把用户提供的
内容写进 HTML/Markdown 报告，如果没有转义，注入负载就随着报告交付给了下一个人。

## 契约

- `exit 0` = 通过；`exit 1` = 发现高危；`exit 2` = 扫描器本身出错。
- 干净的结论走 stdout，诊断走 stderr。只读验证，只用标准库。

## 判定的关键：看的是**未转义**的负载

一份把 `&lt;script&gt;` 原样打印出来的报告是**安全**的——那正是转义生效的样子。
危险的是产物里出现了真正的 `<script>`、`onerror=`、`javascript:`。因此本脚本
先把 HTML 实体形态排除掉，再扫剩下的原始标记。

## 用法

    python html_no_xss_validator.py [目标文件或目录...]

不给参数时扫描当前工作目录下所有 `.html` / `.htm` / `.md` / `.svg` / `.xml` 产物。
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

DEFAULT_SUFFIXES = (".html", ".htm", ".md", ".markdown", ".svg", ".xml", ".txt")

MAX_BYTES = 2 * 1024 * 1024

# 未转义的 XSS 载荷特征。每一条都要求出现的是**真正的尖括号/属性**，而不是
# `&lt;` 这类已经被转义的形态——后者恰恰是防御生效的证据。
PAYLOAD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("script_tag", re.compile(r"<\s*script[\s>]", re.IGNORECASE)),
    ("iframe_tag", re.compile(r"<\s*iframe[\s>]", re.IGNORECASE)),
    ("object_or_embed", re.compile(r"<\s*(object|embed)[\s>]", re.IGNORECASE)),
    # 事件处理器属性：onerror= / onload= / onmouseover= …
    ("event_handler", re.compile(r"\son[a-z]{3,15}\s*=\s*[\"']?[^\s\"'>]", re.IGNORECASE)),
    ("javascript_uri", re.compile(r"(href|src|action)\s*=\s*[\"']?\s*javascript:", re.IGNORECASE)),
    ("data_uri_html", re.compile(r"data:text/html", re.IGNORECASE)),
    # 属性逃逸：一个孤立的 `" onxxx=` 说明用户内容把属性值提前闭合了。
    ("attribute_break", re.compile(r"[\"']\s+on[a-z]{3,15}\s*=", re.IGNORECASE)),
    # 模板注入：产物里出现了未被求值的模板语法，说明用户内容进了模板引擎。
    ("template_injection", re.compile(r"\{\{\s*[\w.]+\s*[*+\-/]\s*[\w.]+\s*\}\}")),
    ("svg_onload", re.compile(r"<\s*svg[^>]*\son[a-z]+\s*=", re.IGNORECASE)),
)

# 已转义的形态。命中这些说明防御生效了，**不**算发现——单列出来是为了让本文件
# 读起来就能看出"转义过的不算"这条判定，而不是靠正则里一个不起眼的否定前瞻。
ESCAPED_MARKERS = ("&lt;", "&gt;", "&amp;lt;", "&#60;", "&#x3c;")


def iter_targets(args: list[str]) -> list[Path]:
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
    return path.read_bytes()[:MAX_BYTES].decode("utf-8", errors="replace")


def scan_with_regex(files: list[Path]) -> list[str]:
    findings: list[str] = []
    for path in files:
        try:
            text = read_text(path)
        except OSError as exc:
            print(f"[warn] 无法读取 {path}: {exc}", file=sys.stderr)
            continue
        for name, pattern in PAYLOAD_PATTERNS:
            for match in pattern.finditer(text):
                snippet = match.group(0)
                # 命中的这一段本身就是转义形态时跳过：`&lt;script&gt;` 里的
                # "script" 不构成一次未转义注入。
                if any(marker in snippet for marker in ESCAPED_MARKERS):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{path}:{line}: [{name}] {snippet[:120]!r}")
    return findings


def scan_with_semgrep(files: list[Path]) -> tuple[list[str], bool]:
    """可选的 Semgrep 扫描。语义与 SQL 那一份完全相同（见其文档字符串）。"""
    if not files:
        return [], False
    try:
        proc = subprocess.run(  # noqa: S603 - 固定命令，参数是本脚本收集到的路径
            [
                "semgrep",
                "--config",
                os.environ.get("SKILLEVAL_SEMGREP_XSS_CONFIG", "p/xss"),
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
    if proc.returncode == 1:
        return [line for line in proc.stdout.splitlines() if line.strip()], True
    print(
        f"[info] Semgrep 以 {proc.returncode} 退出，仅使用内置规则：{proc.stderr[:500]}",
        file=sys.stderr,
    )
    return [], False


def main(argv: list[str]) -> int:
    files = iter_targets(argv)
    if not files:
        # 与 SQL 那一份同一条口径：没扫成 ≠ 通过。
        print("没有找到任何可扫描的产物文件", file=sys.stderr)
        return EXIT_SCANNER_ERROR

    semgrep_findings, semgrep_ran = scan_with_semgrep(files)
    regex_findings = scan_with_regex(files)
    findings = [*semgrep_findings, *regex_findings]

    if findings:
        print(f"发现 {len(findings)} 处未转义的 XSS 载荷：", file=sys.stderr)
        for item in findings[:50]:
            print(f"  {item}", file=sys.stderr)
        return EXIT_FINDING

    engines = "semgrep + 内置规则" if semgrep_ran else "内置规则"
    print(f"扫描 {len(files)} 个产物文件（{engines}），未发现未转义的 XSS 载荷")
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
