"""第二个异构可插拔后端：`LlamaControlBackend`（docs/dev/19 第 3 节，落地 docs/dev/03 的待接入项）。

架构文档模块九要求"除了常规的执行引擎，还必须配置至少一个具有底层架构差异的备用
可插拔代理，例如通过 Hugging Face 等渠道接入开源 Llama 系列模型"。本模块就是那个
备用代理的执行后端：它与 `HermesBackend` 实现**同一套** `ExecutionRequest ->
ExecutionTrace` 协议，评测节点对两者一视同仁。

## 回传协议：与 Hermes Hook Payload 同构

Llama 运行时回传的结果体直接复用 `HermesHookPayload` 的字段结构（`LlamaRunPayload`
是它的别名），并经同一个 `map_hermes_payload_to_trace()` 映射成 Trace。理由：

1. 对照实验比的是两条臂的 Trace，**映射口径必须完全一致**——两个后端各写一份映射，
   "备用代理没触发"就可能只是两份映射对 `SKILL.md` 路径的判定写法不同；
2. `loaded_skill_md` 的 fallback 判定（扫描 `read_file` 动作）docs/dev/03 本来就是为
   "第三方 Agent 无法提供显式信号"预留的，Llama 运行时正是那个第三方。

## 两种等待机制（docs/dev/19 要求"类比 docs/dev/03 第 4.4 节自行设计"）

| 模式 | 流程 | 适用 |
|---|---|---|
| `poll`（默认） | 提交任务 → 进程内按间隔轮询 → 完成即映射；超过墙钟上限记超时失败态 | HF Inference Endpoint 这类只提供查询接口的托管方案；不依赖图上下文 |
| `callback` | 提交任务 → 落 `pending_hooks` → `suspend_and_wait()` 挂起 → 运行时回调 `/hooks/llama_control/...` → `resolve_suspension()` 唤醒 | 长任务；与 Hermes 共用 docs/dev/04 的挂起框架 |

`callback` 模式复用的是**同一套** `suspend_and_wait / resolve_suspension`，wait_key
形状也与 Hermes 一致（`run_id:case_id:run_index`）——两个后端的 Trace 落在不同的
run_index 号段（`state/trace.py`），wait_key 天然不会冲突。

## 不伪造成功

未配置 `SKILLEVAL_EXECUTOR_LLAMA_CONTROL_ENDPOINT` 时注入
`UnconfiguredLlamaControlClient`：提交任务抛 `ExecutorBackendError`，`health_check()`
恒为 False。模块九据此在报告里写明"备用代理不可用、异构矩阵未执行"，而不是拿一个
虚构的 Trace 去和主代理比（与 `UnconfiguredHermesSandboxClient` 同一原则）。

真实部署的接入说明见 docs/dev/interfaces/19_cross_model_generalization.md 第 3 节。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel

from skill_evaluate.config import get_settings
from skill_evaluate.errors import ConfigurationError, ExecutorBackendError
from skill_evaluate.executors.base import ExecutionRequest, ExecutorBackend
from skill_evaluate.executors.hermes_backend import (
    HermesHookPayload,
    build_failure_trace,
    map_hermes_payload_to_trace,
)
from skill_evaluate.executors.registry import register_backend
from skill_evaluate.logging import get_logger
from skill_evaluate.state.enums import ExecutorBackendType
from skill_evaluate.state.trace import ExecutionTrace

logger = get_logger(component="llama_control_backend")

BACKEND_NAME = "llama_control"

# 结果体与 Hermes Hook Payload 同构（见模块头"回传协议"）。起别名而不是另建模型：
# 字段一旦分叉，两个后端的 Trace 映射口径就会漂移，而对照实验最怕的正是这个。
LlamaRunPayload = HermesHookPayload

WaitMode = Literal["poll", "callback"]
_WAIT_MODES: frozenset[str] = frozenset({"poll", "callback"})

# 轮询模式下，墙钟超时之外再多等的宽限秒数：运行时自己的超时判定与本进程的时钟
# 不同步，刚好卡在边界上的任务不该被本地抢先判成超时（那会把一次正常完成的执行
# 记成失败态，进而在对照实验里记成"证据不足"）。
POLL_GRACE_S = 15.0


class LlamaTaskHandle(BaseModel):
    task_id: str


class LlamaTaskStatus(BaseModel):
    """轮询接口的返回体。

    `status=succeeded` 时 `result` 必须非空；`failed` 时 `error` 给出原因——这是运行时
    报告的"任务没跑成"，与"任务跑完了但没触发 Skill"是两回事，前者记失败态 Trace。
    """

    status: Literal["queued", "running", "succeeded", "failed"]
    result: LlamaRunPayload | None = None
    error: str | None = None


class LlamaControlClient(Protocol):
    """与 Llama 运行时的网络交互协议（真实部署注入实现，测试注入替身）。"""

    async def submit_task(
        self,
        *,
        request: ExecutionRequest,
        callback_url: str | None,
        hook_secret: str | None,
    ) -> LlamaTaskHandle:
        """提交一次执行：挂载 `request.skill`，以 `request.case.prompt` 为初始任务。

        必须把 `request.sampling_overrides` 原样下发给模型——模块九的参数扰动实验靠它；
        `callback_url` 非空（callback 模式）时要求运行时在结束后回调该地址。
        """

    async def get_task(self, task_id: str) -> LlamaTaskStatus:
        """查询任务状态（poll 模式的主路径，callback 模式的拉取兜底）。"""

    async def is_reachable(self) -> bool: ...


class UnconfiguredLlamaControlClient:
    """默认占位客户端：没有真实的 Llama 部署时使用，显式报错而非伪造成功。"""

    async def submit_task(
        self,
        *,
        request: ExecutionRequest,
        callback_url: str | None,
        hook_secret: str | None,
    ) -> LlamaTaskHandle:
        raise ExecutorBackendError(
            "LlamaControlBackend 尚未接入真实的 Llama 运行时：请配置 "
            "SKILLEVAL_EXECUTOR_LLAMA_CONTROL_ENDPOINT，或注入实现了 LlamaControlClient "
            "协议的客户端（见 docs/dev/interfaces/19_cross_model_generalization.md 第 3 节）。"
        )

    async def get_task(self, task_id: str) -> LlamaTaskStatus:
        return LlamaTaskStatus(status="failed", error="llama_control 未配置")

    async def is_reachable(self) -> bool:
        return False


class HttpLlamaControlClient:
    """基于一份最小 REST 契约的通用 HTTP 客户端。

    契约（运行时侧需要提供的三个端点，详见接入文档第 3 节）：

    - `POST {endpoint}/v1/tasks`      -> `{"task_id": "..."}`
    - `GET  {endpoint}/v1/tasks/{id}` -> `LlamaTaskStatus`
    - `GET  {endpoint}/healthz`       -> 2xx 即可达

    为什么自定一份契约而不是直接调 HF Inference API：HF Endpoint 提供的是**补全接口**，
    不是 Agent 运行时——它不会自己读文件、调工具，也就产不出 Trace 树。无论底下用 HF
    还是 vLLM 托管模型，中间都需要一层"Agent 循环 + 沙箱"的适配服务，本契约规定的就是
    那层服务的对外形状。
    """

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str = "",
        model: str,
        timeout_s: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s
        # 可注入：测试用 `httpx.MockTransport` 构造，不发真实请求。
        self._http_client = http_client

    async def submit_task(
        self,
        *,
        request: ExecutionRequest,
        callback_url: str | None,
        hook_secret: str | None,
    ) -> LlamaTaskHandle:
        body: dict[str, Any] = {
            "model": self._model,
            "case_id": request.case.case_id,
            "run_index": request.run_index,
            "prompt": request.case.prompt,
            "skill": _skill_payload(request.skill) if request.load_skill else None,
            "background_skills": [_skill_payload(s) for s in request.background_skills],
            "sampling_overrides": request.sampling_overrides,
            "wall_clock_timeout_s": request.wall_clock_timeout_s,
            "callback_url": callback_url,
            "callback_secret": hook_secret,
        }
        response = await self._request("POST", "/v1/tasks", json=body)
        return LlamaTaskHandle.model_validate(response.json())

    async def get_task(self, task_id: str) -> LlamaTaskStatus:
        response = await self._request("GET", f"/v1/tasks/{task_id}")
        return LlamaTaskStatus.model_validate(response.json())

    async def is_reachable(self) -> bool:
        try:
            await self._request("GET", "/healthz")
        except (httpx.HTTPError, ExecutorBackendError):
            return False
        return True

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        if self._http_client is not None:
            response = await self._http_client.request(
                method, f"{self._endpoint}{path}", headers=headers, **kwargs
            )
        else:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.request(
                    method, f"{self._endpoint}{path}", headers=headers, **kwargs
                )
        if response.status_code >= 500:
            # 5xx = 运行时自身故障：按"评测系统自身故障"抛出，由节点层决定重试或挂起，
            # 而不是记一条失败态 Trace 让它混进对照实验的证据里。
            raise ExecutorBackendError(
                f"llama_control 运行时返回 {response.status_code}：{response.text[:200]}"
            )
        response.raise_for_status()
        return response


def _skill_payload(skill: Any) -> dict[str, Any]:
    """下发给运行时的 Skill 描述。

    带 `root_path` 与文件清单而不是文件内容：`scripts/`、`references/` 的实体由运行时
    侧按自己的挂载方式获取（共享卷 / 制品仓库），把整个目录塞进 JSON 会让一次提交
    动辄几 MB，且二进制文件没法可靠地走 JSON。
    """
    return {
        "skill_id": skill.skill_id,
        "version_ref": skill.version_ref,
        "root_path": skill.root_path,
        "description": skill.description,
        "body_markdown": skill.body_markdown,
        "reference_files": [ref.path for ref in skill.reference_files],
        "scripts": [script.path for script in skill.scripts],
    }


def build_default_llama_client() -> LlamaControlClient:
    """按配置构造客户端：配了 endpoint 用 HTTP 客户端，否则用显式报错的占位实现。"""
    settings = get_settings().executor
    if not settings.llama_control_endpoint:
        return UnconfiguredLlamaControlClient()
    return HttpLlamaControlClient(
        settings.llama_control_endpoint,
        api_key=settings.llama_control_api_key.get_secret_value(),
        model=settings.llama_control_model,
    )


@register_backend(BACKEND_NAME)
class LlamaControlBackend(ExecutorBackend):
    """备用代理执行后端。`backend_type=PLUGGABLE`：它是一个完整执行 Agent，不是静态评审。"""

    backend_type = ExecutorBackendType.PLUGGABLE

    def __init__(
        self,
        client: LlamaControlClient | None = None,
        *,
        wait_mode: str | None = None,
        poll_interval_s: float | None = None,
        hook_secret: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        settings = get_settings().executor
        self._client: LlamaControlClient = client or build_default_llama_client()
        mode = wait_mode or settings.llama_control_wait_mode
        if mode not in _WAIT_MODES:
            raise ConfigurationError(
                f"未知的 llama_control 等待模式 {mode!r}，可选：{sorted(_WAIT_MODES)}"
            )
        self._wait_mode: str = mode
        self._poll_interval_s = (
            settings.llama_control_poll_interval_s if poll_interval_s is None else poll_interval_s
        )
        self._hook_secret = (
            hook_secret
            if hook_secret is not None
            else settings.llama_control_hook_secret.get_secret_value()
        )
        # 时钟与 sleep 可注入：轮询/超时逻辑必须能在单测里毫秒级跑完。
        self._sleep = sleep
        self._clock = clock

    async def execute(self, request: ExecutionRequest) -> ExecutionTrace:
        if request.assertion_specs:
            # 模块九的对照实验不规划断言；备用代理也没有义务支持 docs/dev/10 的同沙箱
            # 断言执行。忽略并告警（与 MiniAgentBackend 同一处理），不让它整次失败。
            logger.warning(
                "llama_control_assertion_specs_ignored",
                case_id=request.case.case_id,
                spec_count=len(request.assertion_specs),
            )
            request = request.model_copy(update={"assertion_specs": []})
        if self._wait_mode == "callback":
            return await self._execute_with_callback(request)
        return await self._execute_with_polling(request)

    async def health_check(self) -> bool:
        return await self._client.is_reachable()

    # ------------------------------------------------------------------ #
    # poll 模式
    # ------------------------------------------------------------------ #

    async def _execute_with_polling(self, request: ExecutionRequest) -> ExecutionTrace:
        """提交 → 轮询 → 映射。超过 `wall_clock_timeout_s + POLL_GRACE_S` 记超时失败态。"""
        case_id = request.case.case_id
        try:
            handle = await self._client.submit_task(
                request=request, callback_url=None, hook_secret=None
            )
        except ExecutorBackendError:
            raise
        except Exception as exc:  # noqa: BLE001 - 容错约定：execute() 不向上抛裸异常
            return self._failure(request, reason=f"提交任务失败：{exc}")

        deadline = self._clock() + request.wall_clock_timeout_s + POLL_GRACE_S
        while True:
            try:
                status = await self._client.get_task(handle.task_id)
            except ExecutorBackendError:
                raise
            except Exception as exc:  # noqa: BLE001
                return self._failure(request, reason=f"查询任务失败：{exc}")

            if status.status == "succeeded":
                if status.result is None:
                    return self._failure(request, reason="运行时报告成功但未返回结果体")
                return map_hermes_payload_to_trace(
                    status.result, case_id=case_id, run_index=request.run_index
                )
            if status.status == "failed":
                return self._failure(request, reason=f"运行时报告任务失败：{status.error}")
            if self._clock() >= deadline:
                logger.warning(
                    "llama_control_poll_timeout",
                    case_id=case_id,
                    run_index=request.run_index,
                    task_id=handle.task_id,
                )
                return self._failure(
                    request,
                    reason=f"轮询超过墙钟上限 {request.wall_clock_timeout_s}s",
                    timed_out=True,
                )
            await self._sleep(self._poll_interval_s)

    # ------------------------------------------------------------------ #
    # callback 模式
    # ------------------------------------------------------------------ #

    async def _execute_with_callback(self, request: ExecutionRequest) -> ExecutionTrace:
        """与 `HermesBackend.execute()` 同构的挂起-唤醒流程（docs/dev/04 第 5 节）。

        只能在已挂载 checkpointer 的图节点上下文里调用；run_id 必填（拼回调 URL 与
        pending_hooks 唯一键）。
        """
        # 延迟导入，避免 executors <-> persistence 之间的模块级循环依赖
        from skill_evaluate.persistence.checkpointer import thread_id_for
        from skill_evaluate.persistence.repository import PendingHookRepository, TraceRepository
        from skill_evaluate.persistence.suspension import suspend_and_wait

        run_id = request.run_id
        if not run_id:
            raise ExecutorBackendError(
                "ExecutionRequest.run_id 未设置：llama_control 的 callback 模式需要 run_id "
                "拼装回调 URL 与 pending_hooks 唯一键。"
            )
        case_id = request.case.case_id
        callback_url = (
            f"{get_settings().api.internal_base_url}/hooks/{BACKEND_NAME}/"
            f"{run_id}/{case_id}/{request.run_index}"
        )
        wait_key = llama_wait_key(run_id, case_id, request.run_index)
        try:
            await self._client.submit_task(
                request=request, callback_url=callback_url, hook_secret=self._hook_secret
            )
            await PendingHookRepository().create(
                run_id=run_id,
                case_id=case_id,
                run_index=request.run_index,
                thread_id=thread_id_for(skill_id=request.skill.skill_id, run_id=run_id),
                wait_key=wait_key,
            )
            trace_id = await suspend_and_wait(
                reason=f"waiting for llama_control hook: {wait_key}", wait_key=wait_key
            )
        except ExecutorBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            return self._failure(request, reason=str(exc))

        trace = await TraceRepository().get(str(trace_id))
        if trace is None:
            return self._failure(request, reason=f"resume 后未能读取 trace_id={trace_id}")
        return trace

    @staticmethod
    def _failure(
        request: ExecutionRequest, *, reason: str, timed_out: bool = False
    ) -> ExecutionTrace:
        """保守失败态 Trace，复用 Hermes 的构造（`loaded_skill_md=False`、末尾动作标记故障）。

        只把 `final_response` 的前缀改成本后端的名字：沿用 `[HermesBackend fallback]`
        会让排查的人去翻 Hermes 的日志。
        """
        trace = build_failure_trace(
            case_id=request.case.case_id,
            run_index=request.run_index,
            reason=reason,
            timed_out=timed_out,
        )
        return trace.model_copy(
            update={
                "trace_id": str(uuid.uuid4()),
                "final_response": f"[LlamaControlBackend fallback] {reason}",
            }
        )


def llama_wait_key(run_id: str, case_id: str, run_index: int) -> str:
    """callback 模式的 wait_key。Hook 端点与后端各自拼这个字符串必然漂移，收敛成一处。"""
    return f"{run_id}:{case_id}:{run_index}"


__all__ = [
    "BACKEND_NAME",
    "POLL_GRACE_S",
    "HttpLlamaControlClient",
    "LlamaControlBackend",
    "LlamaControlClient",
    "LlamaRunPayload",
    "LlamaTaskHandle",
    "LlamaTaskStatus",
    "UnconfiguredLlamaControlClient",
    "WaitMode",
    "build_default_llama_client",
    "llama_wait_key",
]
