"""CI 脚本细节（docs/dev/24 第 6 节"CHANGED_SKILL_PATH 的解析"）。

正文把"从 PR diff 里找出哪个 Skill 目录变了"留作 CI 脚本细节。写成包内纯函数 + CLI 子命令
（`skill-evaluate internal changed-skills`）而不是 workflow 里的一段 bash：路径前缀匹配的边界情况
（Skill 嵌套、SKILL.md 被删除、改的是 references/ 深层文件）要能单测。

判定规则：一个改动文件归属于"离它最近的、含 SKILL.md 的祖先目录"，且该目录必须位于 `skills_root`
之下。SKILL.md 本身被删除（目录里已找不到 SKILL.md）的改动不产生评测目标——没有东西可测。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path, PurePosixPath

SKILL_MD = "SKILL.md"

# 基础镜像相关文件：命中任一即视为"镜像变更"，CI 强制金丝雀实跑（interfaces/21 第 3.3 节）。
BASE_IMAGE_PATTERNS: tuple[str, ...] = ("Dockerfile", "*.Dockerfile", "golden_fingerprint.json")


def resolve_changed_skills(
    changed_files: Iterable[str], *, repo_root: Path, skills_root: str = "skills"
) -> list[str]:
    """把改动文件列表映射为需要评测的 Skill 目录（相对仓库根，去重、排序）。"""
    root = repo_root.resolve()
    skills_prefix = PurePosixPath(skills_root.strip("/")) if skills_root.strip("/") else None
    found: set[str] = set()
    for raw in changed_files:
        rel = PurePosixPath(raw.strip())
        if not raw.strip() or rel.is_absolute() or ".." in rel.parts:
            continue
        if skills_prefix is not None and not _is_under(rel, skills_prefix):
            continue
        skill_dir = _nearest_skill_dir(root, rel, stop_at=skills_prefix)
        if skill_dir is not None:
            found.add(skill_dir.as_posix())
    return sorted(found)


def list_all_skills(*, repo_root: Path, skills_root: str = "skills") -> list[str]:
    """`skills_root` 下全部 Skill 目录（Nightly 用：对每个 Skill 跑一遍 COLD 回归）。"""
    base = repo_root.resolve() / skills_root
    if not base.is_dir():
        return []
    return sorted(
        path.parent.relative_to(repo_root.resolve()).as_posix()
        for path in base.rglob(SKILL_MD)
        if path.is_file()
    )


def touches_base_image(changed_files: Iterable[str]) -> bool:
    """改动里是否包含基础镜像定义（docs/dev/24 第 6 节"Dockerfile 变更时强制金丝雀"）。

    正文示例 `contains(github.event.pull_request.changed_files, 'Dockerfile')` 在 GitHub Actions 里
    不成立：`changed_files` 是个整数（改动文件数），不是文件列表。因此改由本函数判定后写进步骤输出。
    """
    for raw in changed_files:
        name = PurePosixPath(raw.strip())
        if any(name.match(pattern) for pattern in BASE_IMAGE_PATTERNS):
            return True
    return False


def _is_under(path: PurePosixPath, prefix: PurePosixPath) -> bool:
    return path.parts[: len(prefix.parts)] == prefix.parts


def _nearest_skill_dir(
    root: Path, rel: PurePosixPath, *, stop_at: PurePosixPath | None
) -> PurePosixPath | None:
    # 从文件所在目录往上找（改动本身就是 SKILL.md 时，第一个候选就是它所在的目录）。
    candidate = rel.parent
    while True:
        if stop_at is not None and not _is_under(candidate, stop_at):
            return None
        if (root / candidate / SKILL_MD).is_file():
            return candidate
        if candidate == candidate.parent or str(candidate) in ("", "."):
            return None
        candidate = candidate.parent


__all__ = [
    "BASE_IMAGE_PATTERNS",
    "list_all_skills",
    "resolve_changed_skills",
    "touches_base_image",
]
