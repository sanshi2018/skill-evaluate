"""`ScriptSandboxRunner`：裸调脚本子进程的轻量执行组件（docs/dev/14 第 2 节）。

## 为什么不复用 `ExecutorBackend`

`ExecutorBackend`（docs/dev/03）面向的是"Agent 执行一个任务，产出完整 Trace"：
有 thought、有工具调用轨迹、有 Hook 回调与挂起唤醒。而模块四的核心操作是
**直接裸调一次脚本子进程并检查它的行为**——没有 LLM、没有推理循环，只有一次
确定性的进程调用与它的 exit_code / stdout / stderr。

硬把它塞进 `ExecutorBackend.execute()` 会得到一个"假 Trace"（凭空捏造 actions
与 timing），而 Trace 是模块一触发率、模块三效率诊断的统计输入——往里灌假数据
的代价远大于多一个类。因此本模块是与 `ExecutorBackend` **同级但不同用途**的第
二个执行组件，同样落在 `executors/` 包下。

## 共用的部分

- **沙箱安全约定**与 docs/dev/03 第 7 节一致：Ephemeral 容器（`--rm`，每次调用
  一个全新容器，用完即毁）、**默认无出站网络**（`--network=none`）、Wall-clock
  超时墙（到期 SIGKILL 并如实标记 `timed_out`）。
- **输出截断**走同一个 `executors/sanitize.truncate_field()`，不另写一份。
  但截断前的**原始字节数**会如实记录在 `ProcessResult.stdout_bytes` /
  `stderr_bytes` 里——模块四的"防刷屏检测"要判的正是"脚本自己吐了多少"，
  拿截断后的长度去判等于用我们自己的截断掩盖了脚本没有截断这件事。

## 为什么没有"退化到宿主机直接跑"的分支

被测脚本是**外部输入**，模块四还会故意给它喂脏数据。在宿主机上跑等于让一份未
经审查的脚本以评测进程的权限执行任意代码。因此 docker 不可用时本模块抛
`ExecutorBackendError`（与 `UnconfiguredHermesSandboxClient` 同一种处理：报错，
不伪造成功、也不偷偷降级），由节点层翻译成"本维度无法评测"的报告结论。
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from skill_evaluate.config import ScriptUsabilitySettings, get_settings
from skill_evaluate.errors import ExecutorBackendError
from skill_evaluate.executors.sanitize import DEFAULT_MAX_FIELD_BYTES, truncate_field
from skill_evaluate.logging import get_logger

logger = get_logger(component="script_sandbox")


class ProcessResult(BaseModel):
    """一次子进程调用的完整结果（docs/dev/14 第 2 节）。

    相对 docs/dev/14 正文多出三个字段，都是实现期发现"不记就没法判"的：

    - `stdout_bytes` / `stderr_bytes`：**截断前**的原始字节数。正文第 7 节用
      `len(r.stdout) + len(r.stderr)` 判输出是否超过防刷屏上限，但 stdout/stderr
      在写进本对象时已经被我们自己截断到 32KB，那个判断永远不会成立。
    - `truncated`：本次结果是否被我们截断过。报告里要说清"这段输出不完整"。
    """

    exit_code: int | None  # None 表示超时被杀（进程没有正常给出退出码）
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    truncated: bool = False

    @property
    def total_output_bytes(self) -> int:
        """脚本一次调用吐出的原始字节总量（防刷屏检测的输入）。"""
        return self.stdout_bytes + self.stderr_bytes

    @property
    def combined_output(self) -> str:
        """stdout + stderr 的拼接，供"help 输出到哪个流都接受"这类场景取用。"""
        return "\n".join(part for part in (self.stdout, self.stderr) if part)


class ScriptRuntime(BaseModel):
    """一个脚本的运行时推断结果：用哪个镜像、用什么解释器启动。"""

    language: str  # "python" / "node" / "bash" / "ruby" / "powershell"
    image: str  # 容器镜像 tag
    interpreter: list[str] = Field(default_factory=list)  # 解释器 argv 前缀

    def command_for(self, script_path: str, *args: str) -> list[str]:
        """拼出容器内的完整命令。

        脚本路径**放在解释器后面**而不是依赖脚本自身的可执行位/shebang：仓库检出
        后的执行位在不同平台（尤其 Windows 检出）上并不可靠，显式指定解释器能让
        "脚本挂起没挂起"这件事不受文件权限干扰。
        """
        return [*self.interpreter, script_path, *args]


# 扩展名 -> (语言, 默认镜像, 解释器 argv)。
#
# 镜像一律选**官方最小运行时镜像**：模块四测的是脚本自身的接口行为，不是它的依赖
# 装得全不全。装依赖会引入网络（与"无出站网络"的沙箱约定直接冲突），因此这里的
# 前提是"脚本要么只用标准库、要么在缺依赖时以一条**建设性报错**退出"——后者恰恰
# 是本维度要考核的能力之一。
_RUNTIME_TABLE: dict[str, ScriptRuntime] = {
    ".py": ScriptRuntime(language="python", image="python:3.13-slim", interpreter=["python"]),
    ".sh": ScriptRuntime(language="bash", image="bash:5", interpreter=["bash"]),
    ".bash": ScriptRuntime(language="bash", image="bash:5", interpreter=["bash"]),
    ".js": ScriptRuntime(language="node", image="node:22-slim", interpreter=["node"]),
    ".mjs": ScriptRuntime(language="node", image="node:22-slim", interpreter=["node"]),
    ".cjs": ScriptRuntime(language="node", image="node:22-slim", interpreter=["node"]),
    # Node 22 能直接跑 TypeScript，但要显式打开类型剥离开关，否则 `node x.ts` 报
    # 语法错误——那会被误判成"脚本连 --help 都不响应"。
    ".ts": ScriptRuntime(
        language="node", image="node:22-slim", interpreter=["node", "--experimental-strip-types"]
    ),
    ".rb": ScriptRuntime(language="ruby", image="ruby:3.3-slim", interpreter=["ruby"]),
    ".ps1": ScriptRuntime(
        language="powershell",
        image="mcr.microsoft.com/powershell:latest",
        interpreter=["pwsh", "-NonInteractive", "-File"],
    ),
}

# shebang 关键字 -> 扩展名。shebang 优先于扩展名：无扩展名的 `scripts/deploy` 或
# 后缀与实际解释器不符的脚本（`.sh` 里写 `#!/usr/bin/env python3`）都靠它纠正。
_SHEBANG_HINTS: tuple[tuple[str, str], ...] = (
    ("python", ".py"),
    ("node", ".js"),
    ("bash", ".sh"),
    ("/bin/sh", ".sh"),
    ("zsh", ".sh"),
    ("ruby", ".rb"),
    ("pwsh", ".ps1"),
)


def infer_runtime(
    script_path: str,
    *,
    shebang: str | None = None,
    image_overrides: dict[str, str] | None = None,
) -> ScriptRuntime | None:
    """按 shebang（优先）或扩展名推断运行时；无法识别时返回 `None`。

    返回 `None` 而不是"猜一个 bash"：拿错解释器跑出来的失败会被上层误读成"脚本
    有缺陷"，而事实是我们根本没把它跑起来。上层节点收到 `None` 会如实记一条
    "无法推断运行时，已跳过"的报告项。

    `image_overrides` 按**语言名**覆盖镜像（`{"python": "my-registry/py:3.13"}`），
    供内网环境替换成自己的镜像源，不必改代码。
    """
    suffix: str | None = None
    if shebang:
        lowered = shebang.lower()
        suffix = next((suf for hint, suf in _SHEBANG_HINTS if hint in lowered), None)
    if suffix is None:
        suffix = Path(script_path).suffix.lower()

    runtime = _RUNTIME_TABLE.get(suffix)
    if runtime is None:
        return None
    override = (image_overrides or {}).get(runtime.language)
    return runtime.model_copy(update={"image": override}) if override else runtime


def infer_runtime_image(script_path: str, *, shebang: str | None = None) -> str | None:
    """docs/dev/14 第 2 节点名的便捷入口：只要镜像名。"""
    runtime = infer_runtime(script_path, shebang=shebang)
    return runtime.image if runtime else None


def interpreter_for(script_path: str, *, shebang: str | None = None) -> list[str] | None:
    """docs/dev/14 第 4 节点名的便捷入口：只要解释器 argv 前缀。"""
    runtime = infer_runtime(script_path, shebang=shebang)
    return list(runtime.interpreter) if runtime else None


def read_shebang(file_path: Path) -> str | None:
    """读脚本首行的 shebang（没有 / 读不到时返回 None）。"""
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as handle:
            first_line = handle.readline().strip()
    except OSError:
        return None
    return first_line if first_line.startswith("#!") else None


class ScriptSandboxRunner(Protocol):
    """裸调一条命令的执行组件协议。

    做成 Protocol 而不是抽象基类：模块四的节点只需要"给我一个能跑命令的东西"，
    测试注入一个记录调用参数的替身即可，不必继承任何东西；docs/dev/15 的红队
    脚本注入测试要复用它时，也可以换一个带更严格 seccomp 策略的实现。
    """

    async def run(
        self,
        *,
        image: str,
        command: list[str],
        stdin_data: bytes | None = None,
        cwd: str | None = None,
        timeout_s: int = 10,
    ) -> ProcessResult:
        """执行一条命令并返回子进程结果。实现必须保证：

        1. **无 TTY**：不分配伪终端，避免脚本继承任何交互终端（docs/dev/14 第 1 节
           的"非交互性测试"前提）；
        2. `timeout_s` 到期后强杀，并以 `timed_out=True` + `exit_code=None` 如实
           上报，**不抛异常**——"超时"在本维度是一条要被判定的观测事实，不是故障；
        3. 环境本身出问题（拉不起容器）时抛 `ExecutorBackendError`，与"脚本行为
           不合格"区分开。
        """
        ...

    async def is_available(self) -> bool:
        """沙箱运行时是否可用，供节点在开跑前先探一次。"""
        ...


class DockerScriptSandboxRunner:
    """默认实现：一次调用 = 一个 `docker run --rm` 的 Ephemeral 容器。

    容器参数与 docs/dev/03 第 7 节的安全边界逐条对应：

    | 参数 | 对应约定 |
    |---|---|
    | `--rm` + 每次新容器 | Ephemeral，执行完立即销毁，不复用文件系统状态 |
    | `--network=none` | 默认无出站网络（被测脚本不该在探测中把数据发出去） |
    | `--memory` / `--cpus` / `--pids-limit` | 资源墙，防"逻辑炸弹"把评测机拖垮 |
    | `-i` 且**不带** `-t` | 有 stdin 但无 TTY：脚本读 stdin 立刻拿到 EOF |
    | `--name` + 超时后 `rm -f` | 超时时确保容器真的死掉，而不是留一个孤儿在后台 |

    工作目录以 `-v <cwd>:/workspace` 挂载并 `-w /workspace`：脚本的副作用全部落在
    调用方给的临时目录里，用完即删，宿主机不受污染（架构文档模块四第 5 节点名的
    那条权衡）。
    """

    #: 容器内的工作目录挂载点。脚本路径一律以它为根传入，因此命令里出现的都是
    #: 相对路径，与宿主机的真实目录名解耦（报告里也不会泄露评测机的目录结构）。
    WORKDIR = "/workspace"

    def __init__(self, settings: ScriptUsabilitySettings | None = None) -> None:
        self._settings = settings or get_settings().script_usability

    async def run(
        self,
        *,
        image: str,
        command: list[str],
        stdin_data: bytes | None = None,
        cwd: str | None = None,
        timeout_s: int = 10,
    ) -> ProcessResult:
        container_name = f"skilleval-script-{uuid.uuid4().hex[:12]}"
        argv = self._docker_argv(
            image=image, command=command, cwd=cwd, container_name=container_name
        )
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:  # docker 不在 PATH 上等
            raise ExecutorBackendError(
                f"无法启动脚本沙箱（{self._settings.docker_binary} 不可用）：{exc}。"
                "模块四拒绝退化到宿主机直接执行——被测脚本是外部输入，且探测过程会"
                "故意给它喂脏数据。请安装/配置容器运行时，或在 CI 中跳过本维度。"
            ) from exc

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=stdin_data if stdin_data is not None else b""),
                timeout=timeout_s,
            )
        except TimeoutError:
            timed_out = True
            stdout_bytes, stderr_bytes = await self._kill(process, container_name)

        duration_ms = int((time.monotonic() - started) * 1000)
        return self._build_result(
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            # 超时被杀时 `returncode` 是 docker 客户端被杀的信号码，不是脚本的退出
            # 码。硬填成 None，让下游不可能把它误当成"脚本自己以 137 退出了"。
            exit_code=None if timed_out else process.returncode,
            timed_out=timed_out,
            duration_ms=duration_ms,
        )

    async def is_available(self) -> bool:
        """`docker version` 能跑通即视为可用。

        用 `version` 而不是 `info`：前者不需要连上 daemon 之外的任何东西，且在
        daemon 没起来时会以非 0 退出——这正是我们要区分的两种情况。
        """
        try:
            process = await asyncio.create_subprocess_exec(
                self._settings.docker_binary,
                "version",
                "--format",
                "{{.Server.Version}}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return False
        try:
            return await asyncio.wait_for(process.wait(), timeout=10) == 0
        except TimeoutError:
            process.kill()
            return False

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _docker_argv(
        self, *, image: str, command: list[str], cwd: str | None, container_name: str
    ) -> list[str]:
        settings = self._settings
        argv = [
            settings.docker_binary,
            "run",
            "--rm",
            "--name",
            container_name,
            # -i 但**没有** -t：给 stdin 但不给 TTY。这是"非交互性测试"成立的前提
            # ——有 TTY 的话，一个 `read -p` 会一直等下去，我们测的就不再是脚本
            # 在真实 Agent 调用场景（无终端）下的行为了。
            "-i",
            "--network=none",
            f"--memory={settings.container_memory_limit}",
            f"--cpus={settings.container_cpu_limit}",
            f"--pids-limit={settings.container_pids_limit}",
        ]
        if cwd:
            argv += ["-v", f"{Path(cwd).resolve()}:{self.WORKDIR}", "-w", self.WORKDIR]
        argv += [image, *command]
        return argv

    async def _kill(
        self, process: asyncio.subprocess.Process, container_name: str
    ) -> tuple[bytes, bytes]:
        """超时兜底：杀掉 docker 客户端进程，再确保容器本体也死掉。

        只杀客户端进程是不够的——`docker run` 的客户端退出后容器仍在 daemon 里
        跑着，下一个探测就会撞上一个还在写文件的"上一次运行"。因此追加一次
        `docker rm -f`（幂等，容器已经没了也不报错）。
        """
        process.kill()
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        except (TimeoutError, ProcessLookupError):  # pragma: no cover - 极端情况
            stdout, stderr = b"", b""

        try:
            remover = await asyncio.create_subprocess_exec(
                self._settings.docker_binary,
                "rm",
                "-f",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(remover.wait(), timeout=10)
        except (OSError, TimeoutError):  # pragma: no cover - 清理失败不改变判定
            logger.warning("script_sandbox_container_cleanup_failed", container=container_name)
        return stdout, stderr

    @staticmethod
    def _build_result(
        *,
        stdout_bytes: bytes,
        stderr_bytes: bytes,
        exit_code: int | None,
        timed_out: bool,
        duration_ms: int,
    ) -> ProcessResult:
        """把原始字节转成 `ProcessResult`，并在截断前记下真实体量。"""
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        return ProcessResult(
            exit_code=exit_code,
            stdout=truncate_field(stdout) or "",
            stderr=truncate_field(stderr) or "",
            timed_out=timed_out,
            duration_ms=duration_ms,
            stdout_bytes=len(stdout_bytes),
            stderr_bytes=len(stderr_bytes),
            truncated=(
                len(stdout_bytes) > DEFAULT_MAX_FIELD_BYTES
                or len(stderr_bytes) > DEFAULT_MAX_FIELD_BYTES
            ),
        )


def prepare_workspace(source_root: str | Path, target_root: str | Path) -> Path:
    """把被测 Skill 目录整份复制到一个独立工作区，返回工作区路径。

    每次探测一个独立工作区（docs/dev/14 第 3 节的硬性要求）：脚本 A 的副作用不得
    污染脚本 B 的幂等性判定，同一个脚本的"第一次执行"与"脏数据探测"之间也不该
    互相看见对方留下的文件。唯一刻意共享工作区的场景是幂等性探测自己的两次连续
    执行——那正是它要观测的东西。

    复制而不是直接挂载源目录：容器里的写操作会真的落到宿主机上，直接挂载等于让
    一次评测改动开发者的工作区。
    """
    source = Path(source_root)
    target = Path(target_root)
    shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
    return target


__all__ = [
    "DockerScriptSandboxRunner",
    "ProcessResult",
    "ScriptRuntime",
    "ScriptSandboxRunner",
    "infer_runtime",
    "infer_runtime_image",
    "interpreter_for",
    "prepare_workspace",
    "read_shebang",
]
