"""`PluggableAgentBackend` 的默认实现：`HermesBackend`（docs/dev/03 第 4 节）。

对接外置可插拔完整执行 Agent（默认 [NousResearch/hermes-agent]
(https://github.com/NousResearch/hermes-agent)）。真实的沙箱创建 HTTP 调用
（`_create_sandbox`）当前是留给实际接入 Hermes 服务时补充的桩实现——协议、
字段映射、挂起/唤醒链路均已按文档完整实现，唯独"真的发一个 HTTP 请求给一个
真实存在的 Hermes 部署"这一步不可能在本仓库内验证，因此以 `HermesSandboxClient`
协议 + 可注入实现的方式留出接口，默认 `NotImplementedError`，不伪造网络成功。

参见 docs/dev/interfaces/03_hermes_sandbox_client.md。
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from skill_evaluate.config import get_settings
from skill_evaluate.errors import ExecutorBackendError
from skill_evaluate.executors.base import ExecutionRequest, ExecutorBackend
from skill_evaluate.executors.registry import register_backend
from skill_evaluate.executors.sanitize import truncate_field
from skill_evaluate.logging import get_logger
from skill_evaluate.state.assertion import AssertionResult, AssertionSpec
from skill_evaluate.state.enums import ExecutorBackendType
from skill_evaluate.state.trace import (
    ActionStep,
    ArtifactManifestEntry,
    ExecutionTrace,
    TimingCostMetrics,
)

logger = get_logger(component="hermes_backend")


class HermesUsage(BaseModel):
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0


class HermesTrajectoryItem(BaseModel):
    thought: str | None = None
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    ts: datetime


class HermesFsDiffItem(BaseModel):
    path: str
    sha256: str
    op: str


class HermesAssertionExecution(BaseModel):
    """沙箱内一条校验脚本的执行结果（docs/dev/10 第 4.3 节 Hook Payload 扩展）。

    与 `HermesTrajectoryItem` 分开建模是有意的：断言脚本不是被测 Agent 的行为，
    不该混进 `trajectory[]` 被当成"Agent 做了什么"来统计——它是评测系统自己注入
    的检查步骤，独立落 `assertion_results` 表。
    """

    assertion_id: str
    exit_code: int
    stdout: str | None = None
    stderr: str | None = None


class HermesHookPayload(BaseModel):
    """Hermes Hook 回调 body 的强类型 schema（docs/dev/03 第 4.3 节字段映射表左列）。"""

    usage: HermesUsage = Field(default_factory=HermesUsage)
    trajectory: list[HermesTrajectoryItem] = Field(default_factory=list)
    final_message: str = ""
    fs_diff: list[HermesFsDiffItem] = Field(default_factory=list)
    skill_md_loaded: bool | None = None  # None 时走 fallback 判定（见 map_hermes_payload_to_trace）
    started_at: datetime
    finished_at: datetime
    # docs/dev/10 第 4.3 节追加：任务主流程结束后、容器销毁前执行的校验脚本结果。
    # 默认空列表——没规划断言的用例（绝大多数维度）payload 形状完全不变。
    assertion_executions: list[HermesAssertionExecution] = Field(default_factory=list)


_SKILL_MD_BASENAME = "SKILL.md"


def map_hermes_payload_to_trace(
    payload: HermesHookPayload, *, case_id: str, run_index: int, trace_id: str | None = None
) -> ExecutionTrace:
    """docs/dev/03 第 4.3 节映射规则的唯一实现位置（docs/dev/05 第 2.2 节要求）。

    `loaded_skill_md` 的确定性要求：优先取 Hermes 显式上报的
    `skill_md_loaded` 布尔标志；若第三方 Agent 无法提供该显式信号
    （`payload.skill_md_loaded is None`），退化为扫描 `trajectory[]` 中是否存在
    `tool_name == "read_file"` 且 `tool_input.path` 命中 `SKILL.md` 的 fallback
    判定逻辑。
    """
    if payload.skill_md_loaded is not None:
        loaded_skill_md = payload.skill_md_loaded
    else:
        loaded_skill_md = any(
            item.tool_name == "read_file"
            and _SKILL_MD_BASENAME in str(item.tool_input.get("path", ""))
            for item in payload.trajectory
        )

    actions = [
        ActionStep(
            step_id=idx,
            timestamp=item.ts,
            thought=item.thought,
            action_type=item.tool_name,
            action_input=item.tool_input,
            exit_code=item.exit_code,
            stdout=truncate_field(item.stdout),
            stderr=truncate_field(item.stderr),
        )
        for idx, item in enumerate(payload.trajectory)
    ]

    manifest = [
        ArtifactManifestEntry(file_path=item.path, sha256=item.sha256, action=item.op)
        for item in payload.fs_diff
    ]

    return ExecutionTrace(
        trace_id=trace_id or str(uuid.uuid4()),
        case_id=case_id,
        run_index=run_index,
        backend_type=ExecutorBackendType.PLUGGABLE.value,
        loaded_skill_md=loaded_skill_md,
        timing=TimingCostMetrics(
            total_tokens=payload.usage.total_tokens,
            prompt_tokens=payload.usage.prompt_tokens,
            completion_tokens=payload.usage.completion_tokens,
            duration_ms=payload.usage.duration_ms,
        ),
        actions=actions,
        final_response=truncate_field(payload.final_message) or "",
        modified_files_manifest=manifest,
        started_at=payload.started_at,
        finished_at=payload.finished_at,
    )


def map_assertion_executions(payload: HermesHookPayload) -> list[AssertionResult]:
    """Hook payload 的 `assertion_executions[]` -> `AssertionResult` 列表。

    `passed` 一律走 `AssertionResult.from_exit_code()`（`exit_code == 0`），
    docs/dev/10 第 6 节要求这条判定在全局只有一种口径。
    """
    return [
        AssertionResult.from_exit_code(
            assertion_id=item.assertion_id,
            exit_code=item.exit_code,
            stdout=truncate_field(item.stdout) or "",
            stderr=truncate_field(item.stderr) or "",
        )
        for item in payload.assertion_executions
    ]


def executable_assertion_specs(specs: list[AssertionSpec]) -> list[AssertionSpec]:
    """过滤出真正可下发执行的 spec（docs/dev/10 第 4.2 节）。

    `strategy=NONE` 与"生成失败没有脚本正文"的 spec 不下发：把一个空 spec 交给
    沙箱只会拿回一条无意义的失败断言，而失败断言在 Judge 眼里是实打实的负面证据。
    """
    return [spec for spec in specs if spec.is_executable]


# 失败态 Trace 末尾那条动作的 `action_type`（docs/dev/15 第 8 节补充的约定）。
#
# 为什么要把"超时"与"其他故障"分成两个取值：模块五的 DoS 判定里，**超时即通过**
# ——墙钟约束把攻击挡住了，这正是我们期望的结果；而沙箱崩溃且没给出建设性报错是
# 不通过。两者都记成 `internal_error` 的话，这两个方向相反的结论就区分不出来，
# 一次成功的防御会被读成一次失守。
#
# 其余维度对这两个值一视同仁（都是"这次没跑成"），因此改动是向后兼容的。
ACTION_TYPE_INTERNAL_ERROR = "internal_error"
ACTION_TYPE_SANDBOX_TIMEOUT = "sandbox_timeout"


def build_failure_trace(
    *, case_id: str, run_index: int, reason: str, timed_out: bool = False
) -> ExecutionTrace:
    """保守失败态 Trace（docs/dev/03 第 4.4 节拉取兜底 / docs/dev/04 第 5.3 节超时兜底 共用）。

    `loaded_skill_md=False` 是有意的保守判定：宁可漏判触发也不可误判触发，
    避免模块一假阳性。

    `timed_out=True` 时末尾动作记为 `sandbox_timeout` 而不是 `internal_error`
    （docs/dev/15 第 8 节）。默认 False 保持既有调用方行为不变——只有确实知道
    "这是墙钟超时"的调用方（`scripts/pending_hooks_reaper.py`）才该传 True，
    在这里靠字符串猜 reason 里有没有 "timeout" 是不可靠的。
    """
    now = datetime.now(UTC)
    return ExecutionTrace(
        trace_id=str(uuid.uuid4()),
        case_id=case_id,
        run_index=run_index,
        backend_type=ExecutorBackendType.PLUGGABLE.value,
        loaded_skill_md=False,
        timing=TimingCostMetrics(
            total_tokens=0, prompt_tokens=0, completion_tokens=0, duration_ms=0
        ),
        actions=[
            ActionStep(
                step_id=0,
                timestamp=now,
                thought=None,
                action_type=(
                    ACTION_TYPE_SANDBOX_TIMEOUT if timed_out else ACTION_TYPE_INTERNAL_ERROR
                ),
                action_input={
                    "reason": "sandbox_wall_clock_timeout"
                    if timed_out
                    else "hermes_timeout_or_unreachable"
                },
                exit_code=1,
                stdout=None,
                stderr=truncate_field(reason),
            )
        ],
        final_response=f"[HermesBackend fallback] {reason}",
        modified_files_manifest=[],
        started_at=now,
        finished_at=now,
    )


class HermesSandboxHandle(BaseModel):
    sandbox_id: str


class HermesSandboxClient(Protocol):
    """真实网络交互留给具体部署时注入的实现（docs/dev/interfaces/03_hermes_sandbox_client.md）。"""

    async def create_sandbox(
        self,
        *,
        request: ExecutionRequest,
        callback_url: str,
        hook_secret: str,
    ) -> HermesSandboxHandle:
        """创建沙箱并下发任务。

        docs/dev/10 第 4.2 节对实现方追加了一条契约：`request.assertion_specs`
        非空时，实现必须要求 Hermes 在**任务主流程结束、容器销毁之前**，把每个
        spec 的 `script_content` 写到 `script_path` 并执行，按顺序收集
        `exit_code`/`stdout`/`stderr`，随**同一次** Hook 回调以
        `assertion_executions[]` 上报——不要为断言单独再发一次回调，两次网络往返
        之间沙箱状态可能已经变了。
        """

    async def poll_sandbox(self, sandbox_id: str) -> HermesHookPayload | None:
        """拉取兜底：查询当前沙箱状态，取到什么算什么（docs/dev/03 第 4.4 节第 4 点）。"""

    async def is_reachable(self) -> bool: ...


class UnconfiguredHermesSandboxClient:
    """默认占位客户端：未接入真实 Hermes 部署时使用，`create_sandbox` 显式报错而非
    伪造成功，`is_reachable` 恒为 False，供 `health_check()`/金丝雀探针（docs/dev/21）
    正确反映"后端不可用"。
    """

    async def create_sandbox(
        self, *, request: ExecutionRequest, callback_url: str, hook_secret: str
    ) -> HermesSandboxHandle:
        raise ExecutorBackendError(
            "HermesBackend 尚未接入真实的 Hermes 部署：请注入实现了 HermesSandboxClient "
            "协议的客户端（见 docs/dev/interfaces/03_hermes_sandbox_client.md）。"
        )

    async def poll_sandbox(self, sandbox_id: str) -> HermesHookPayload | None:
        return None

    async def is_reachable(self) -> bool:
        return False


@register_backend("hermes")
class HermesBackend(ExecutorBackend):
    backend_type = ExecutorBackendType.PLUGGABLE

    def __init__(
        self,
        endpoint: str | None = None,
        hook_secret: str | None = None,
        sandbox_client: HermesSandboxClient | None = None,
    ) -> None:
        settings = get_settings()
        self._endpoint = endpoint or settings.executor.hermes_endpoint
        self._hook_secret = hook_secret or settings.executor.hermes_hook_secret.get_secret_value()
        self._sandbox_client: HermesSandboxClient = (
            sandbox_client or UnconfiguredHermesSandboxClient()
        )

    async def execute(self, request: ExecutionRequest) -> ExecutionTrace:
        """完整流程：

        1. 在 `pending_hooks` 插入等待记录并向 Hermes 沙箱管理 API 发起容器创建请求
           （挂载 request.skill 及 background_skills，注入 case.prompt 作为初始任务）。
        2. 调用 `persistence.suspension.suspend_and_wait()` 挂起，等待 Hook 回调
           （docs/dev/04 第 5 节）或 `pending_hooks_reaper.py` 的超时兜底 resume。
        3. 取回 `trace_id` 后经 `TraceRepository` 读取完整 `ExecutionTrace`。

        这一步依赖 docs/dev/04 的 `persistence.suspension` 模块与 LangGraph 的
        `interrupt()`/`Command(resume=...)` 机制，只能在 LangGraph 节点的执行上下文
        （已挂载 checkpointer 的图运行时）中调用；脱离图上下文调用会抛
        `ExecutorBackendError`，避免静默产出不完整的 Trace。
        """
        # 延迟导入，避免 executors <-> persistence 之间的模块级循环依赖
        from skill_evaluate.persistence.checkpointer import thread_id_for
        from skill_evaluate.persistence.repository import PendingHookRepository
        from skill_evaluate.persistence.suspension import suspend_and_wait

        run_id = request.run_id
        if not run_id:
            raise ExecutorBackendError(
                "ExecutionRequest.run_id 未设置：HermesBackend 需要 run_id 拼装 Hook "
                "回调 URL 与 pending_hooks 唯一键（docs/dev/03 第 4.4 节）。"
            )

        start = time.monotonic()
        settings = get_settings()
        callback_url = f"{settings.api.internal_base_url}/hooks/hermes/{run_id}/{request.case.case_id}/{request.run_index}"
        wait_key = f"{run_id}:{request.case.case_id}:{request.run_index}"
        thread_id = thread_id_for(skill_id=request.skill.skill_id, run_id=run_id)

        # docs/dev/10 第 4.2 节：只把真正有脚本的 spec 交给沙箱客户端，客户端据此
        # 在任务结束、容器销毁前把脚本写到 spec.script_path 并执行，结果随同一次
        # Hook 回调以 `assertion_executions[]` 上报。
        dispatchable = executable_assertion_specs(request.assertion_specs)
        if len(dispatchable) != len(request.assertion_specs):
            logger.info(
                "hermes_assertion_specs_filtered",
                case_id=request.case.case_id,
                requested=len(request.assertion_specs),
                dispatchable=len(dispatchable),
            )
        if dispatchable != request.assertion_specs:
            request = request.model_copy(update={"assertion_specs": dispatchable})

        try:
            await self._sandbox_client.create_sandbox(
                request=request, callback_url=callback_url, hook_secret=self._hook_secret
            )
            await PendingHookRepository().create(
                run_id=run_id,
                case_id=request.case.case_id,
                run_index=request.run_index,
                thread_id=thread_id,
                wait_key=wait_key,
            )
            trace_id = await suspend_and_wait(
                reason=f"waiting for hermes hook: {wait_key}",
                wait_key=wait_key,
            )
        except ExecutorBackendError:
            raise
        except Exception as exc:  # noqa: BLE001 - 容错约定：execute() 不向上抛裸异常
            return build_failure_trace(
                case_id=request.case.case_id, run_index=request.run_index, reason=str(exc)
            )

        from skill_evaluate.persistence.repository import TraceRepository  # 延迟导入，避免循环依赖

        trace = await TraceRepository().get(str(trace_id))
        if trace is None:
            return build_failure_trace(
                case_id=request.case.case_id,
                run_index=request.run_index,
                reason=f"resume 后未能读取 trace_id={trace_id}",
            )
        duration_ms = int((time.monotonic() - start) * 1000)
        if duration_ms and not trace.timing.duration_ms:
            trace.timing.duration_ms = duration_ms
        return trace

    async def health_check(self) -> bool:
        return await self._sandbox_client.is_reachable()
