"""外部"配置型 Git 仓库"的本地缓存同步工具（docs/dev/10 第 3.3 节模式的公共化）。

本项目有两个同形态的外部仓库：docs/dev/10 的断言工具箱与 docs/dev/21 的种子锚点库。
二者的同步语义完全一致——**锁定 ref、浅克隆、记录 commit sha 以保证可追溯、同步失败降级为
"不可用"而不是中断评测**。docs/dev/21 第 3.1 节明确要求"直接复用文档 10 已建立的模式，不重复
设计同步逻辑"，因此把原先私有在 `agents/validator/toolbox.py` 里的 git 与 YAML 记录列表解析
函数提到这里，两处共用（toolbox 保留原私有名作为别名，行为不变）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

UNKNOWN_REF = "unknown"


class RecordListParseError(ValueError):
    """YAML 记录列表形状非法。调用方按自己的领域异常重新包装（工具箱 → ToolboxError）。"""


def run_git(args: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],  # 参数来自配置，非用户输入
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return completed.stdout.strip()


def git_sync(repo_url: str, ref: str, dest: Path) -> None:
    """浅克隆或 fetch 到 `dest` 并检出 `ref`。

    已存在 `.git` 时 fetch + checkout FETCH_HEAD，而不是删掉重克隆：CI 缓存目录可复用，
    同步一个没变化的仓库只是一次很轻的网络往返。
    """
    if (dest / ".git").is_dir():
        run_git(["fetch", "--depth", "1", "origin", ref], cwd=dest)
        run_git(["checkout", "--force", "FETCH_HEAD"], cwd=dest)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_git(
        ["clone", "--depth", "1", "--branch", ref, repo_url, str(dest)],
        cwd=dest.parent,
    )


def git_head_sha(root: Path) -> str:
    """缓存目录当前 commit sha；非 git 目录（或环境没有 git）返回 `unknown`。"""
    if not (root / ".git").is_dir():
        return UNKNOWN_REF
    try:
        return run_git(["rev-parse", "HEAD"], cwd=root) or UNKNOWN_REF
    except (subprocess.SubprocessError, OSError):  # pragma: no cover - 环境无 git
        return UNKNOWN_REF


def parse_record_list(raw: str, *, source_name: str) -> list[dict[str, Any]]:
    """解析"顶层是记录列表"的 YAML 文件。

    优先 PyYAML（可选依赖，装了就用），否则退化为只认 `- key: value` 形状的极简解析器
    （标量 / 行内列表 / 块列表）。为一个固定形状的小配置文件引入必需的运行期依赖不划算。
    """
    try:  # pragma: no cover - 取决于环境是否装了 PyYAML
        import yaml
    except ImportError:
        return parse_record_list_minimal(raw, source_name=source_name)
    loaded = yaml.safe_load(raw)
    if loaded is None:
        return []
    if not isinstance(loaded, list):
        raise RecordListParseError(f"{source_name} 顶层必须是记录列表，实际是 {type(loaded)}")
    return [dict(item) for item in loaded]


def parse_record_list_minimal(raw: str, *, source_name: str) -> list[dict[str, Any]]:
    """极简 YAML 子集解析：`- key: value` 记录列表，值支持标量、行内列表、块列表。

    靠**缩进**区分"新记录"与"块列表项"：记录的 `- ` 位于最外层缩进（由第一条记录确定），
    块列表项一定比它更深。不这么做的话，`- template: b` 紧跟在 `params:` 的块列表后面时
    会被误吞成上一条记录的列表项。
    """
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    pending_list_key: str | None = None
    record_indent: int | None = None

    for line_no, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())

        if stripped.startswith("- "):
            if record_indent is None:
                record_indent = indent
            if indent <= record_indent:  # 新记录
                current = {}
                records.append(current)
                pending_list_key = None
                stripped = stripped[2:].strip()
                record_indent = indent
            else:  # 块列表项
                if current is None or pending_list_key is None:
                    raise RecordListParseError(f"{source_name} 第 {line_no} 行：列表项没有归属的键")
                current[pending_list_key].append(_scalar(stripped[2:].strip()))
                continue

        if current is None:
            raise RecordListParseError(f"{source_name} 第 {line_no} 行：记录必须以 `- ` 开头")
        if ":" not in stripped:
            raise RecordListParseError(f"{source_name} 第 {line_no} 行无法解析：{line!r}")

        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:  # 块列表的起始行：`keywords:`
            current[key] = []
            pending_list_key = key
        elif value.startswith("[") and value.endswith("]"):
            current[key] = [_scalar(v) for v in value[1:-1].split(",") if v.strip()]
            pending_list_key = None
        else:
            current[key] = _scalar(value)
            pending_list_key = None

    return records


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


__all__ = [
    "UNKNOWN_REF",
    "RecordListParseError",
    "git_head_sha",
    "git_sync",
    "parse_record_list",
    "parse_record_list_minimal",
    "run_git",
]
