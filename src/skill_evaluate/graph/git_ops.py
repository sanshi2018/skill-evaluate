"""补丁转 PR 用到的 git / GitHub CLI 操作（docs/dev/24 第 5 节 `git_ops`）。

## 为什么走 git worktree，而不是在当前检出目录里切分支

`finalize.patch_pr` 跑在 CI 作业（或 API 进程）的工作目录里，那里还有本次评测要归档的
`benchmark.json` / `report.html`，以及可能正被其他进程读取的 Skill 目录。直接 `git checkout -b`
会把整个工作区换成另一个提交，报告归档、后续步骤都会读到错的文件。独立 worktree 让"准备
提交内容"与"当前检出"完全隔离，用完即删。

## 为什么用 `gh` 而不是 GitHub REST API

CI 里 `gh` 自带 `GH_TOKEN` 鉴权与重试，且同一套命令在 GitHub Enterprise 上不用改代码；本仓库
不想为了一个 `POST /pulls` 再引入一套 GitHub 客户端依赖。接口做成 `GitOps` 协议：换 GitLab
（`glab`）或 REST 实现时只替换实现类。

全部命令用参数列表调用（`create_subprocess_exec`，无 shell），分支名、提交信息、PR 正文里的
任何字符都不会被 shell 解释——PR 正文包含模型写的补丁理由，属于不可信文本。
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from skill_evaluate.config import get_settings
from skill_evaluate.errors import DeliveryError
from skill_evaluate.logging import get_logger

logger = get_logger(component="git_ops")

# 命令输出写进异常/日志时的截断长度：`git push` 失败时 stderr 可能带整段服务端提示。
_OUTPUT_EXCERPT_CHARS = 1000


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class GitOps(Protocol):
    """`finalize.patch_pr` 依赖的最小仓库操作集合。"""

    async def repo_root(self, path: str) -> str | None:
        """`path` 所在 git 仓库的根目录；不在仓库里返回 None。"""
        ...

    async def is_commit(self, repo: str, ref: str) -> bool:
        """`ref` 是否是仓库里一个真实存在的提交。"""
        ...

    async def find_open_pull_request(self, repo: str, branch: str) -> str | None:
        """以 `branch` 为 head 的未关闭 PR 链接；没有返回 None。"""
        ...

    async def create_worktree(self, repo: str, branch: str, base_ref: str) -> str:
        """基于 `base_ref` 新建（或重置）分支 `branch` 并检出到临时 worktree，返回其路径。"""
        ...

    async def commit_all(self, worktree: str, message: str) -> bool:
        """暂存 worktree 内全部改动并提交；没有任何改动返回 False。"""
        ...

    async def push(self, worktree: str, branch: str) -> None: ...

    async def create_pull_request(
        self, worktree: str, *, branch: str, title: str, body: str, base: str | None
    ) -> str:
        """创建 PR 并返回链接。"""
        ...

    async def remove_worktree(self, repo: str, worktree: str) -> None: ...


class SubprocessGitOps:
    """基于本机 `git` / `gh` 可执行文件的实现。"""

    def __init__(
        self,
        *,
        git_binary: str | None = None,
        gh_binary: str | None = None,
        remote: str | None = None,
        author_name: str | None = None,
        author_email: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        settings = get_settings().pipeline
        self._git = git_binary or settings.git_binary
        self._gh = gh_binary or settings.gh_binary
        self._remote = remote or settings.patch_pr_remote
        self._author_name = author_name or settings.patch_pr_commit_author_name
        self._author_email = author_email or settings.patch_pr_commit_author_email
        self._timeout_s = timeout_s or settings.git_command_timeout_s

    # ------------------------------------------------------------------ #
    # 协议实现
    # ------------------------------------------------------------------ #

    async def repo_root(self, path: str) -> str | None:
        result = await self._run([self._git, "-C", path, "rev-parse", "--show-toplevel"], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    async def is_commit(self, repo: str, ref: str) -> bool:
        # `^{commit}`：tag 等其他对象也能被 rev-parse 解析，这里只接受能作为分支起点的提交。
        result = await self._run(
            [self._git, "-C", repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            check=False,
        )
        return result.returncode == 0

    async def find_open_pull_request(self, repo: str, branch: str) -> str | None:
        result = await self._run(
            [
                self._gh, "pr", "list", "--head", branch, "--state", "open",
                "--json", "url", "--jq", ".[0].url // empty",
            ],
            cwd=repo,
            check=False,
        )
        if result.returncode != 0:
            # 查不到已有 PR 不致命：接下来 `gh pr create` 若因重复而失败会给出明确报错。
            logger.warning("git_ops_pr_lookup_failed", branch=branch, stderr=_excerpt(result.stderr))
            return None
        return result.stdout.strip() or None

    async def create_worktree(self, repo: str, branch: str, base_ref: str) -> str:
        parent = Path(tempfile.mkdtemp(prefix="skilleval-pr-"))
        worktree = parent / "worktree"
        # `-B`：分支已存在（上次收尾节点在推送后崩溃、断点恢复重跑）时重置到 base_ref，
        # 保证分支内容只由本次运行的补丁决定，不叠加上一次半途而废的提交。
        await self._run([self._git, "-C", repo, "worktree", "add", "-B", branch, str(worktree), base_ref])
        return str(worktree)

    async def commit_all(self, worktree: str, message: str) -> bool:
        await self._run([self._git, "-C", worktree, "add", "--all"])
        status = await self._run([self._git, "-C", worktree, "status", "--porcelain"])
        if not status.stdout.strip():
            return False
        await self._run(
            [
                self._git, "-C", worktree,
                "-c", f"user.name={self._author_name}",
                "-c", f"user.email={self._author_email}",
                "commit", "--no-verify", "-m", message,
            ]
        )
        return True

    async def push(self, worktree: str, branch: str) -> None:
        # `--force-with-lease`：分支位于评测系统专属的命名空间（`patch_pr_branch_prefix`），
        # 断点恢复重跑时需要覆盖上一次的提交；lease 防止覆盖掉**人**在这条分支上追加的提交。
        await self._run(
            [self._git, "-C", worktree, "push", "--force-with-lease", self._remote, f"HEAD:refs/heads/{branch}"]
        )

    async def create_pull_request(
        self, worktree: str, *, branch: str, title: str, body: str, base: str | None
    ) -> str:
        # 正文走临时文件而不是 `--body`：补丁理由可能很长，也可能以 `-` 开头被误读成参数。
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as fh:
            fh.write(body)
            body_file = fh.name
        try:
            argv = [
                self._gh, "pr", "create", "--head", branch, "--title", title, "--body-file", body_file,
            ]
            if base:
                argv += ["--base", base]
            result = await self._run(argv, cwd=worktree)
        finally:
            Path(body_file).unlink(missing_ok=True)
        # `gh pr create` 成功时最后一行是 PR 链接。
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            raise DeliveryError("gh pr create 成功返回但没有输出 PR 链接")
        return lines[-1]

    async def remove_worktree(self, repo: str, worktree: str) -> None:
        result = await self._run(
            [self._git, "-C", repo, "worktree", "remove", "--force", worktree], check=False
        )
        if result.returncode != 0:
            logger.warning("git_ops_worktree_cleanup_failed", worktree=worktree, stderr=_excerpt(result.stderr))

    # ------------------------------------------------------------------ #
    # 底层执行
    # ------------------------------------------------------------------ #

    async def _run(self, argv: list[str], *, cwd: str | None = None, check: bool = True) -> CommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise DeliveryError(f"找不到可执行文件 {argv[0]!r}（请安装或配置 SKILLEVAL_PIPELINE_*_BINARY）") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout_s)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise DeliveryError(f"命令超时（{self._timeout_s}s）：{_describe(argv)}") from exc

        result = CommandResult(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )
        if check and result.returncode != 0:
            raise DeliveryError(
                f"命令失败（exit={result.returncode}）：{_describe(argv)}\n{_excerpt(result.stderr)}"
            )
        return result


def _describe(argv: list[str]) -> str:
    """日志/异常里展示的命令：只保留前几个参数，避免把 PR 标题等长文本整段打出来。"""
    return " ".join(argv[:6]) + (" ..." if len(argv) > 6 else "")


def _excerpt(text: str) -> str:
    return text.strip()[:_OUTPUT_EXCERPT_CHARS]


__all__ = ["CommandResult", "GitOps", "SubprocessGitOps"]
