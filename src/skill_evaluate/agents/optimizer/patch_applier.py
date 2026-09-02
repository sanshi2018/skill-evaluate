"""补丁应用机制（docs/dev/09 第 7 节）。

**应用范围仅限评测流水线的临时工作副本**：`apply_patch()` 产出的是一个新的
`SkillDefinition`（外加必要时的临时目录），既不写回被测仓库，也不产生 git
commit。把通过回归的补丁转成真实提交/PR 是 docs/dev/24 的职责。

为什么坚持 unified diff 而不是让模型整篇重写（docs/dev/09 第 3 节）：
- 人工审查阶段（docs/dev/22）要看的是"改了哪几行"，整篇重写会把一处三行的加固
  淹没在几百行无关的措辞漂移里；
- 回归失败时要能精确定位是哪处改动导致的，diff 天然带这个信息；
- 模型整篇重写时极易顺手"优化"掉与本次失败无关的内容，那属于未经审查的偷渡。

## 对 LLM 产出的 diff 做定位容错

模型写 diff 时行号常常是错的（差一两行是常态）。所以本模块**不信任 `@@` 里的
行号**，只用它做起点提示：真正的定位方式是拿 hunk 的"旧内容块"（上下文行 +
被删除行）去原文里找。找不到就报 `PatchApplyError`，找到多处就取离声明行号最近
的那处。这是"容错"而不是"放宽"——旧内容块必须逐字符匹配，只是允许它整体位移。
"""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from skill_evaluate.errors import PatchApplyError
from skill_evaluate.ingestion.skill_loader import estimate_token_count
from skill_evaluate.logging import get_logger
from skill_evaluate.state.enums import PatchType
from skill_evaluate.state.patch import Patch
from skill_evaluate.state.skill import SkillDefinition

logger = get_logger(component="patch_applier")

SKILL_MD_TARGET = "SKILL.md"
WORKING_COPY_MARKER = ".skilleval-working-copy"

_HUNK_HEADER_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*\n?", re.DOTALL)
_DESCRIPTION_LINE_RE = re.compile(r"^(?P<prefix>description\s*:\s*)(?P<value>.*)$", re.MULTILINE)


def working_version_ref(base_ref: str, patch_id: str) -> str:
    """临时工作版本的标识。

    形如 `abc1234+patch:<uuid>`——**不是**真实的 git ref，刻意带上 `+patch:` 这个
    在 git 里非法的片段，任何把它当成 commit sha 去 checkout 的代码都会立刻失败，
    而不是悄悄检出一个错误的版本。
    """
    return f"{base_ref}+patch:{patch_id}"


# --------------------------------------------------------------------------- #
# unified diff 解析与应用
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Hunk:
    declared_start: int  # `@@ -N` 里的 N（1-based），仅作定位提示
    old_lines: list[str] = field(default_factory=list)  # 上下文行 + 被删除行
    new_lines: list[str] = field(default_factory=list)  # 上下文行 + 新增行


def _parse_hunks(diff: str) -> list[_Hunk]:
    hunks: list[_Hunk] = []
    current: _Hunk | None = None

    for raw_line in diff.splitlines():
        header = _HUNK_HEADER_RE.match(raw_line)
        if header is not None:
            current = _Hunk(declared_start=int(header.group(1)))
            hunks.append(current)
            continue
        if current is None:
            # hunk 之前的内容是 `--- a/x` / `+++ b/x` 这类文件头，忽略。
            continue
        if raw_line.startswith(("--- ", "+++ ", "\\")):
            continue  # 文件头、以及 "\ No newline at end of file"
        if raw_line == "":
            # 空行按"空的上下文行"处理：不少模型会把 " " 前缀的空上下文行写成空行。
            current.old_lines.append("")
            current.new_lines.append("")
            continue
        tag, text = raw_line[0], raw_line[1:]
        if tag == " ":
            current.old_lines.append(text)
            current.new_lines.append(text)
        elif tag == "-":
            current.old_lines.append(text)
        elif tag == "+":
            current.new_lines.append(text)
        else:
            raise PatchApplyError(f"无法解析的 diff 行（未知前缀 {tag!r}）：{raw_line!r}")

    if not hunks:
        raise PatchApplyError("diff 中没有任何 @@ hunk，无法应用（模型可能输出了整篇重写）")
    return hunks


def _locate(lines: list[str], hunk: _Hunk, search_from: int) -> int:
    """在 `lines[search_from:]` 中定位 hunk 的旧内容块，返回 0-based 起始下标。"""
    if not hunk.old_lines:
        # 纯新增 hunk：没有可匹配的旧内容，只能信行号。
        position = max(search_from, min(hunk.declared_start - 1, len(lines)))
        return position

    span = len(hunk.old_lines)
    matches = [
        index
        for index in range(search_from, len(lines) - span + 1)
        if lines[index : index + span] == hunk.old_lines
    ]
    if not matches:
        raise PatchApplyError(
            "diff 与当前内容不匹配：以下上下文在目标文件中找不到"
            f"（hunk 声明起始行 {hunk.declared_start}）：\n" + "\n".join(hunk.old_lines[:5])
        )
    if len(matches) == 1:
        return matches[0]
    declared = hunk.declared_start - 1
    return min(matches, key=lambda index: abs(index - declared))


def apply_unified_diff(original: str, diff: str) -> str:
    """把 unified diff 应用到一段文本，返回新文本。不匹配即抛 `PatchApplyError`。"""
    lines = original.splitlines()
    result: list[str] = []
    cursor = 0

    for hunk in _parse_hunks(diff):
        position = _locate(lines, hunk, cursor)
        if position < cursor:
            raise PatchApplyError(
                f"diff 的 hunk 顺序错乱（定位到第 {position + 1} 行，但已处理到第 {cursor} 行）"
            )
        result.extend(lines[cursor:position])
        result.extend(hunk.new_lines)
        cursor = position + len(hunk.old_lines)

    result.extend(lines[cursor:])
    text = "\n".join(result)
    # 保持原文的末尾换行习惯：多一个/少一个换行会让下一轮 diff 的上下文错位。
    if original.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text


# --------------------------------------------------------------------------- #
# 补丁应用到 SkillDefinition
# --------------------------------------------------------------------------- #


def apply_patch(skill: SkillDefinition, patch: Patch) -> SkillDefinition:
    """在内存（必要时加一份临时工作副本）中应用补丁，产出新的 `SkillDefinition`。

    三类补丁的落点不同：

    | patch_type | 作用对象 | 是否需要磁盘工作副本 |
    |---|---|---|
    | `DESCRIPTION_PATCH` | `skill.description` | 否 |
    | `RIGID_CONSTRAINT` | `skill.body_markdown` | 否 |
    | `CODE_PATCH` | `scripts/` 下的脚本文件 | **是**——脚本要真的被沙箱执行 |

    代码补丁会把整个 skill 目录复制到一个临时目录再改，原目录只读。返回的
    `SkillDefinition.root_path` 指向该副本，`version_ref` 带 `+patch:` 后缀。
    同一条闭环里的后续补丁会**复用**同一份工作副本（靠副本根目录下的
    `.skilleval-working-copy` 标记识别），不会每打一次补丁复制一次。

    `base_skill_version_ref` 与当前 skill 版本不一致时直接报错：对着一个已经漂移
    的版本打补丁，即使 diff 侥幸能应用上，语义也已经不是模型当初看到的那份文件了。
    """
    _assert_base_version(skill, patch)

    if patch.patch_type is PatchType.DESCRIPTION_PATCH:
        return _apply_description_patch(skill, patch)
    if patch.patch_type is PatchType.RIGID_CONSTRAINT:
        return _apply_body_patch(skill, patch)
    return _apply_code_patch(skill, patch)


def _assert_base_version(skill: SkillDefinition, patch: Patch) -> None:
    if patch.base_skill_version_ref != skill.version_ref:
        raise PatchApplyError(
            f"补丁基线版本已过期：patch.base_skill_version_ref="
            f"{patch.base_skill_version_ref!r}，当前 skill.version_ref={skill.version_ref!r}。"
            "请基于当前版本重新生成补丁。"
        )


def _apply_description_patch(skill: SkillDefinition, patch: Patch) -> SkillDefinition:
    new_description = apply_unified_diff(skill.description, patch.diff).strip()
    if not new_description:
        raise PatchApplyError("description 补丁应用后内容为空——触发准确度评测会失去被测对象")
    updated = skill.model_copy(
        update={
            "description": new_description,
            "version_ref": working_version_ref(skill.version_ref, patch.patch_id),
        }
    )
    logger.info("patch_applied", patch_type=patch.patch_type.value, target=patch.target_path)
    return updated


def _apply_body_patch(skill: SkillDefinition, patch: Patch) -> SkillDefinition:
    new_body = apply_unified_diff(skill.body_markdown, patch.diff)
    updated = skill.model_copy(
        update={
            "body_markdown": new_body,
            "line_count": len(new_body.splitlines()),
            "token_count": estimate_token_count(new_body),
            "version_ref": working_version_ref(skill.version_ref, patch.patch_id),
        }
    )
    logger.info("patch_applied", patch_type=patch.patch_type.value, target=patch.target_path)
    return updated


def _apply_code_patch(skill: SkillDefinition, patch: Patch) -> SkillDefinition:
    root = ensure_working_copy(skill)
    target = (root / patch.target_path).resolve()
    if not target.is_relative_to(root):
        # 补丁的 target_path 是模型输出的字符串，必须当成不可信输入处理：
        # `../../etc/passwd` 这类路径穿越不能因为"是我们自己的模型写的"就放行。
        raise PatchApplyError(f"补丁目标路径越出 skill 根目录：{patch.target_path!r}")
    if not target.is_file():
        raise PatchApplyError(f"补丁目标文件不存在：{target}")

    original = target.read_text(encoding="utf-8")
    target.write_text(apply_unified_diff(original, patch.diff), encoding="utf-8")

    updated = skill.model_copy(
        update={
            "root_path": str(root),
            "version_ref": working_version_ref(skill.version_ref, patch.patch_id),
        }
    )
    logger.info(
        "patch_applied",
        patch_type=patch.patch_type.value,
        target=patch.target_path,
        working_root=str(root),
    )
    return updated


def ensure_working_copy(skill: SkillDefinition) -> Path:
    """确保存在一份可写的临时 skill 目录，返回其根路径。

    已经是工作副本时原地返回；否则复制一份，并把内存中可能已被前几轮补丁改过的
    `description`/`body_markdown` 一并写回副本的 `SKILL.md`——否则代码补丁的工作
    副本会带着一份**过期的 SKILL.md** 去跑回归，测出来的结果对不上当前候选。
    """
    source = Path(skill.root_path)
    if (source / WORKING_COPY_MARKER).is_file():
        _sync_skill_md(source, skill)
        return source
    if not source.is_dir():
        raise PatchApplyError(
            f"代码补丁需要一个真实存在的 skill 目录，但 root_path={skill.root_path!r} 不是目录"
        )

    destination = Path(tempfile.mkdtemp(prefix="skilleval-working-"))
    root = destination / source.name
    shutil.copytree(source, root)
    (root / WORKING_COPY_MARKER).write_text(
        "评测流水线的临时工作副本（docs/dev/09 第 7 节），可安全删除。\n", encoding="utf-8"
    )
    _sync_skill_md(root, skill)
    return root


def _sync_skill_md(root: Path, skill: SkillDefinition) -> None:
    """把内存中的 description/body 写回工作副本的 SKILL.md。

    保留原 frontmatter 的其余键（`name` 等）：只替换 description 的值和正文，
    而不是按已解析出的字段重新生成一份 frontmatter——后者会把解析器不认识的键
    悄悄丢掉。
    """
    skill_md = root / SKILL_MD_TARGET
    if not skill_md.is_file():
        return
    raw = skill_md.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        skill_md.write_text(skill.body_markdown, encoding="utf-8")
        return

    frontmatter = raw[: match.end()]
    patched_frontmatter, count = _DESCRIPTION_LINE_RE.subn(
        lambda m: f"{m.group('prefix')}{skill.description}", frontmatter, count=1
    )
    if count == 0:
        patched_frontmatter = frontmatter
    skill_md.write_text(patched_frontmatter + skill.body_markdown, encoding="utf-8")


def cleanup_working_copy(skill: SkillDefinition) -> None:
    """删除临时工作副本。回归结束、补丁被采纳或放弃后由调用方调用。

    只删带标记文件的目录——传进来一个真实仓库路径时什么都不做，绝不误删被测代码。
    """
    root = Path(skill.root_path)
    if not (root / WORKING_COPY_MARKER).is_file():
        return
    shutil.rmtree(root.parent, ignore_errors=True)
    logger.info("working_copy_cleaned", working_root=str(root))


__all__ = [
    "SKILL_MD_TARGET",
    "WORKING_COPY_MARKER",
    "apply_patch",
    "apply_unified_diff",
    "cleanup_working_copy",
    "ensure_working_copy",
    "working_version_ref",
]
