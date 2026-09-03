"""从磁盘目录解析出 `SkillDefinition`（最小可用版本）。

**归属说明**：这个模块的最终归属是 docs/dev/12（模块二），该文档已明确规划
`src/skill_evaluate/ingestion/skill_loader.py` 作为"一切需要读取 SKILL.md 的
文档共用的解析层"。但 docs/dev/06 的 CLI 子命令 `skill-evaluate generate
--skill-path <path>` 必须先有一个能把路径变成 `SkillDefinition` 的入口，否则
Generator 无法从命令行驱动。

因此本文件按 docs/dev/12 指定的路径与函数名先落一个**最小可用实现**，把口径
明确的部分（行数、目录扫描、frontmatter 解析）做实，把需要 docs/dev/12 定稿的
部分做成显式可替换的桩。

docs/dev/12 已接入，两处变化（均为"替换函数体、保持签名"，调用方不受影响）：

- **Token 计数**改由 `ingestion/token_counter.py` 承担：有 `tiktoken` 时离线精确
  计数，否则退化为"字符数 × 3/4"并**如实标注不精确**（`TokenCount.exact`），
  由 docs/dev/12 的卡线节点据此决定要不要在限额附近让人复核。
- `_extract_trigger_condition()` **维持"只捞证据行、不做判定"**：判定留在
  docs/dev/12 的 `nodes/context_scoping/static_scan.py`（正则初筛）与 Mini Agent
  的 `progressive_disclosure_static` 模板（语义复核）两级里。理由见该函数注释。

仍未接入的桩：`SkillScript.supports_help_flag` / `is_mutating`（docs/dev/14）。
接入清单见 docs/dev/interfaces/06_skill_loader_minimal.md。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from skill_evaluate.errors import ConfigurationError
from skill_evaluate.ingestion.token_counter import count_tokens
from skill_evaluate.logging import get_logger
from skill_evaluate.state.skill import SkillDefinition, SkillReferenceFile, SkillScript

logger = get_logger(component="skill_loader")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*\n?", re.DOTALL)
_SCALAR_RE = re.compile(r"^(?P<key>[A-Za-z0-9_-]+)\s*:\s*(?P<value>.*)$")
_SCRIPT_SUFFIXES = {".py", ".sh", ".js", ".ts", ".rb", ".ps1"}


def load_skill(skill_path: str | Path) -> SkillDefinition:
    """解析一份 Skill（`SKILL.md` 文件本身，或包含它的目录）。

    `skill_id` 取目录名的 slug（docs/dev/02 建议"用仓库路径的 slug"）；
    `version_ref` 优先取该目录最后一次 git commit 的 sha，拿不到时退化为
    `SKILL.md` 正文的 sha256 前 12 位——**必须有一个稳定可追溯的值**，否则
    docs/dev/06 的 staleness 检测（版本漂移告警）会永远误报。
    """
    path = Path(skill_path).resolve()
    if path.is_dir():
        root = path
        skill_md = root / "SKILL.md"
    else:
        root = path.parent
        skill_md = path

    if not skill_md.is_file():
        raise ConfigurationError(f"未找到 SKILL.md：{skill_md}")

    raw = skill_md.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(raw)

    description = frontmatter.get("description", "").strip()
    if not description:
        # description 是模块一触发准确度的**被测对象本身**，缺失时不应静默继续。
        raise ConfigurationError(
            f"{skill_md} 的 YAML frontmatter 缺少 description 字段，"
            "触发准确度评测（docs/dev/11）没有被测对象，无法继续。"
        )

    skill_id = _slugify(frontmatter.get("name", "").strip() or root.name)

    return SkillDefinition(
        skill_id=skill_id,
        version_ref=_resolve_version_ref(root, raw),
        root_path=str(root),
        description=description,
        body_markdown=body,
        line_count=len(body.splitlines()),
        token_count=estimate_token_count(body),
        reference_files=_scan_reference_files(root, body),
        scripts=_scan_scripts(root, body),
    )


def estimate_token_count(text: str) -> int:
    """Token 估算的向后兼容入口。

    实现已由 docs/dev/12 迁到 `ingestion/token_counter.py`（有 `tiktoken` 则离线
    精确计数，否则按字符数 × 3/4 估算）。这里保留原签名，是因为 docs/dev/06 的
    Generator 侧调用点只关心一个整数、不关心精度。

    **需要拿这个数字做卡线判定的调用方请改用 `token_counter.count_tokens()`**，
    它会一并返回"用的哪种计数器、精不精确"——拿一个可能是估算值的数字去阻断
    别人的合并请求，是 docs/dev/interfaces/06 明确警告过的误判来源。
    """
    return count_tokens(text).value


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _split_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """解析 YAML frontmatter。

    只支持标量键值对（`name:` / `description:`），这正是 Skill 规范里
    frontmatter 的全部内容；遇到嵌套结构直接忽略该行而不是引入 yaml 依赖。
    """
    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        return {}, raw

    fields: dict[str, str] = {}
    for line in match.group("body").splitlines():
        scalar = _SCALAR_RE.match(line)
        if scalar is None:
            continue
        value = scalar.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        fields[scalar.group("key")] = value
    return fields, raw[match.end() :]


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unnamed-skill"


def _resolve_version_ref(root: Path, raw: str) -> str:
    content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    commit = _git(root, "log", "-1", "--format=%H", "--", ".")
    if commit is None:
        logger.info("skill_version_ref_fallback_to_content_hash", root=str(root))
        return f"sha256:{content_hash}"

    # 工作区有未提交改动时，光用 commit sha 会让 staleness 检测在本地永远失效
    # （改了 SKILL.md 却报告"版本没变"）。追加内容哈希后缀，本地改一次就能被
    # 检测到；CI 检出的干净工作区仍然拿到纯 commit sha。
    if _git(root, "status", "--porcelain", "--", "."):
        return f"{commit}+dirty:{content_hash}"
    return commit


def _git(root: Path, *args: str) -> str | None:
    """跑一条只读 git 命令，返回 stdout（strip 后）；不可用/失败时返回 None。"""
    try:
        result = subprocess.run(  # 固定参数，无用户输入拼接
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - 无 git 环境
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _scan_reference_files(root: Path, body: str) -> list[SkillReferenceFile]:
    references_dir = root / "references"
    if not references_dir.is_dir():
        return []
    files: list[SkillReferenceFile] = []
    for file in sorted(references_dir.rglob("*")):
        if not file.is_file():
            continue
        rel = file.relative_to(root).as_posix()
        files.append(
            SkillReferenceFile(
                path=rel,
                trigger_condition=_extract_trigger_condition(body, rel),
                token_estimate=estimate_token_count(_safe_read(file)),
            )
        )
    return files


def _scan_scripts(root: Path, body: str) -> list[SkillScript]:
    scripts_dir = root / "scripts"
    if not scripts_dir.is_dir():
        return []
    scripts: list[SkillScript] = []
    for file in sorted(scripts_dir.rglob("*")):
        if not file.is_file() or file.suffix not in _SCRIPT_SUFFIXES:
            continue
        rel = file.relative_to(root).as_posix()
        scripts.append(
            SkillScript(
                path=rel,
                exposed_tool_name=file.stem,
                # `--help` 支持与否属于 docs/dev/14 的黑盒探测结论，静态解析拿不到
                # 可信答案，保持 None（"未知"）而不是猜一个 True/False。
                supports_help_flag=None,
            )
        )
    return scripts


def _extract_trigger_condition(body: str, relative_path: str) -> str | None:
    """从正文里找出提及该参考文件的那一行，作为"按需加载触发条件"的候选原文。

    **本函数刻意不做判定**（docs/dev/12 复核后维持原样，不是遗留的桩）：
    "这句话算不算一个明确的触发条件"是语义问题，交给两级复核——
    `nodes/context_scoping/static_scan.py` 的条件词正则做初筛，Mini Agent 的
    `progressive_disclosure_static` 模板做语义定夺。若把判定收紧到这里，得到的
    只会是一个更容易误伤的正则，而且是在**所有**读 Skill 的模块之间共享的那一份。

    所以本字段的正确读法是"给下游的证据行"，不是"已确认的触发条件"。正文完全
    没提到该文件时返回 None——那本身就是 docs/dev/12 要报的问题之一。
    """
    stem = Path(relative_path).name
    for line in body.splitlines():
        if relative_path in line or stem in line:
            return line.strip()
    return None


def _safe_read(file: Path) -> str:
    try:
        return file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
