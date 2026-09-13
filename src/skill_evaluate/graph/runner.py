"""流水线运行入口（CLI `run` / `internal run-cold-suite` / `internal reap-pending-hooks` 共用）。

## 跨进程执行模型

```
CLI `run`（CI 作业）                         API 进程（uvicorn，同一 Postgres）
  ainvoke(初始状态) ──跑到第一个挂起点──▶ 返回 __interrupt__
  轮询 checkpoint（aget_state）                Hermes Hook / 审批决策到达
     …                                         resolve_suspension → CompiledGraphResumer
     …                                         ainvoke(Command(resume)) 继续跑到下一个挂起点/结束
  snapshot.next 为空 → 流水线已结束
  从库重写 benchmark.json / report.html，按 blocking 给出退出码
```

CLI 不是"唯一执行者"而是"发起者 + 观察者"：真实沙箱回调、人工审批都打到 API 进程，由那里的主图
实例接着跑（interfaces/04、22）。因此 CLI 等待期间**不需要**持有执行权，只要轮询同一个 thread 的
checkpoint 即可；两边写的是同一套 checkpoint 表。

## 退出码（CI 据此判定）

| 码 | 含义 |
|---|---|
| 0 | 流水线结束，没有阻断项 |
| 1 | 流水线结束，存在 `blocking=True` 且 `status=FAIL` 的维度（阻断合并） |
| 2 | 评测系统自身错误（配置缺失、数据库不可达等），与 Skill 质量无关 |
| 3 | 等待超时：流水线仍挂起在 Hook 回调 / 人工审批上（不是失败，工作台上有待处理卡片） |
| 4 | 流水线被停下：人工在审批卡片上选择放弃，或前置门禁判定基础设施不可信 |
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from skill_evaluate.config import get_settings
from skill_evaluate.errors import PipelineSuspended
from skill_evaluate.graph.nodes import write_report_files
from skill_evaluate.graph.resumer import CompiledGraphResumer, graph_config, pending_wait_keys
from skill_evaluate.graph.state import (
    KEY_MODE,
    KEY_REPORT_BLOCKING,
    KEY_SKILL_PATH,
    MODE_FULL,
    PipelineMode,
)
from skill_evaluate.ingestion import load_skill
from skill_evaluate.logging import get_logger
from skill_evaluate.observability.report_generator import ReportGenerator
from skill_evaluate.persistence.checkpointer import build_async_checkpointer, thread_id_for
from skill_evaluate.persistence.suspension import register_graph_resumer
from skill_evaluate.state.enums import GenerationMode

logger = get_logger(component="pipeline_runner")

EXIT_OK = 0
EXIT_BLOCKING = 1
EXIT_SYSTEM_ERROR = 2
EXIT_SUSPENDED = 3
EXIT_STOPPED = 4

RunStatus = Literal["completed", "suspended", "stopped"]


@dataclass(slots=True)
class RunOutcome:
    """一次 `PipelineRunner.start()` 的结果。"""

    status: RunStatus
    run_id: str
    skill_id: str
    thread_id: str
    values: dict[str, Any] = field(default_factory=dict)
    pending_wait_keys: list[str] = field(default_factory=list)
    stop_reason: str | None = None

    @property
    def blocking(self) -> bool:
        return bool(self.values.get(KEY_REPORT_BLOCKING))

    def exit_code(self) -> int:
        if self.status == "stopped":
            return EXIT_STOPPED
        if self.status == "suspended":
            return EXIT_SUSPENDED
        return EXIT_BLOCKING if self.blocking else EXIT_OK


def initial_state(
    *,
    run_id: str,
    skill_id: str,
    skill_version_ref: str,
    skill_path: str,
    mode: PipelineMode,
    force_regenerate: bool,
) -> dict[str, Any]:
    """主图的输入状态：只有身份字段与两个编排开关，其余全部由节点产出。"""
    return {
        "run_id": run_id,
        "skill_id": skill_id,
        "skill_version_ref": skill_version_ref,
        "generation_mode": (
            GenerationMode.FORCE_REGENERATE.value if force_regenerate else GenerationMode.REUSE.value
        ),
        KEY_SKILL_PATH: skill_path,
        KEY_MODE: mode,
    }


class PipelineRunner:
    """发起一次运行并等待它在任意进程里跑完。"""

    def __init__(
        self,
        graph: Any,
        *,
        wait_timeout_s: float | None = None,
        poll_interval_s: float | None = None,
        report_generator: ReportGenerator | None = None,
        report_dir: str | None = None,
    ) -> None:
        settings = get_settings().pipeline
        self._graph = graph
        self._wait_timeout_s = settings.wait_timeout_s if wait_timeout_s is None else wait_timeout_s
        self._poll_interval_s = settings.poll_interval_s if poll_interval_s is None else poll_interval_s
        self._reporter = report_generator
        self._report_dir = settings.report_dir if report_dir is None else report_dir

    async def start(
        self,
        skill_path: str,
        *,
        mode: PipelineMode = MODE_FULL,
        force_regenerate: bool = False,
        run_id: str | None = None,
    ) -> RunOutcome:
        """发起（或续跑）一次运行。

        传入已有 `run_id` 时按 checkpoint 状态决定：
        - 线程已结束 → 不重跑，直接返回结果（CI 重试作业时不会把同一次评测再跑一遍）；
        - 线程停在某处（进程崩溃、等待超时后重新观察）→ `ainvoke(None)` 从断点继续；
        - 线程不存在 → 以该 run_id 新建一次运行。
        """
        skill = load_skill(skill_path)
        run_id = run_id or str(uuid.uuid4())
        thread_id = thread_id_for(skill_id=skill.skill_id, run_id=run_id)
        config = graph_config(thread_id)

        snapshot = await self._graph.aget_state(config)
        try:
            if snapshot.values and not snapshot.next:
                logger.info("pipeline_run_already_finished", run_id=run_id, thread_id=thread_id)
            elif snapshot.values:
                logger.info("pipeline_run_continuing", run_id=run_id, next=list(snapshot.next))
                await self._invoke_unless_waiting(snapshot, config)
            else:
                logger.info("pipeline_run_starting", run_id=run_id, skill_id=skill.skill_id, mode=mode)
                await self._graph.ainvoke(
                    initial_state(
                        run_id=run_id,
                        skill_id=skill.skill_id,
                        skill_version_ref=skill.version_ref,
                        skill_path=skill_path,
                        mode=mode,
                        force_regenerate=force_regenerate,
                    ),
                    config=config,
                )
        except PipelineSuspended as exc:
            # `PipelineSuspended` 家族（人工放弃 / 基础设施不可信）会穿过 guard 原样冒出 ainvoke：
            # 流水线停下了，但这不是评测系统故障。其余异常原样上抛（退出码 2 由 CLI 决定）。
            stop_reason = f"{type(exc).__name__}: {exc}"
            logger.error("pipeline_run_stopped", run_id=run_id, reason=stop_reason[:1000])
            return RunOutcome(
                status="stopped",
                run_id=run_id,
                skill_id=skill.skill_id,
                thread_id=thread_id,
                stop_reason=stop_reason,
            )

        return await self.wait(run_id=run_id, skill_id=skill.skill_id, thread_id=thread_id)

    async def _invoke_unless_waiting(self, snapshot: Any, config: dict[str, Any]) -> None:
        """续跑：线程若停在等外部事件的中断上，`ainvoke(None)` 只会立刻再次停下——不必再发一次；
        停在普通断点（进程崩溃留下的半截超步）上才需要续跑。"""
        if pending_wait_keys(snapshot):
            return
        await self._graph.ainvoke(None, config=config)

    async def wait(self, *, run_id: str, skill_id: str, thread_id: str) -> RunOutcome:
        """轮询 checkpoint 直到线程结束或超时。"""
        config = graph_config(thread_id)
        deadline = time.monotonic() + self._wait_timeout_s
        announced: list[str] | None = None
        while True:
            snapshot = await self._graph.aget_state(config)
            if not snapshot.next:
                values = dict(snapshot.values or {})
                await self._rewrite_report(values)
                logger.info(
                    "pipeline_run_completed",
                    run_id=run_id,
                    blocking=bool(values.get(KEY_REPORT_BLOCKING)),
                )
                return RunOutcome(
                    status="completed",
                    run_id=run_id,
                    skill_id=skill_id,
                    thread_id=thread_id,
                    values=values,
                )
            waiting = pending_wait_keys(snapshot)
            # 唤醒发生在别的进程里时，节点抛出的异常不会传到这里，只会作为任务错误留在 checkpoint 上。
            # 没有待处理中断却有任务错误 = 流水线已经停下、不会再有人唤醒它，继续等只会耗到超时。
            task_errors = [
                f"{task.name}: {task.error}"
                for task in (snapshot.tasks or ())
                if getattr(task, "error", None)
            ]
            if task_errors and not waiting:
                reason = "；".join(task_errors)
                logger.error("pipeline_run_stopped_elsewhere", run_id=run_id, reason=reason[:1000])
                return RunOutcome(
                    status="stopped",
                    run_id=run_id,
                    skill_id=skill_id,
                    thread_id=thread_id,
                    values=dict(snapshot.values or {}),
                    stop_reason=reason,
                )
            if waiting != announced:
                logger.info("pipeline_run_waiting", run_id=run_id, wait_keys=waiting, next=list(snapshot.next))
                announced = waiting
            if time.monotonic() >= deadline:
                return RunOutcome(
                    status="suspended",
                    run_id=run_id,
                    skill_id=skill_id,
                    thread_id=thread_id,
                    values=dict(snapshot.values or {}),
                    pending_wait_keys=waiting,
                )
            await asyncio.sleep(self._poll_interval_s)

    async def _rewrite_report(self, values: Mapping[str, Any]) -> None:
        """在**本进程**的工作目录重写报告（收尾节点可能跑在 API 进程里，文件不在 CI 工作区）。

        Nightly 与完整评测同样适用。收尾节点根本没跑到（例如状态里缺 run_id）时静默跳过。
        """
        if "run_id" not in values:
            return
        reporter = self._reporter or ReportGenerator()
        try:
            await write_report_files(reporter, values, self._report_dir)
        except Exception as exc:  # noqa: BLE001 - 报告在收尾节点里已写过一份，这里是给 CI 的冗余副本
            logger.warning("pipeline_report_local_rewrite_failed", error=str(exc)[:300])


# --------------------------------------------------------------------------- #
# 进程级装配（CLI / 脚本入口共用）
# --------------------------------------------------------------------------- #


async def run_pipeline(
    skill_path: str,
    *,
    mode: PipelineMode = MODE_FULL,
    force_regenerate: bool = False,
    run_id: str | None = None,
    wait_timeout_s: float | None = None,
    report_dir: str | None = None,
) -> RunOutcome:
    """打开异步 checkpointer → 装配主图 → 注册 GraphResumer → 发起并等待。

    `report_dir` 同时作用于本进程里跑的收尾节点与结束后的本地重写；在 API 进程里被唤醒执行的收尾
    节点使用那个进程自己的配置（CLI 结束时总会在这里再写一份，CI 归档不受影响）。
    """
    from skill_evaluate.graph.main import MainGraphDeps, build_main_graph
    from skill_evaluate.graph.nodes import PipelineDeps

    async with build_async_checkpointer() as saver:
        graph = build_main_graph(saver, MainGraphDeps(pipeline=PipelineDeps(report_dir=report_dir)))
        # 本进程内发生的 resolve_suspension（例如 Mini 后端环境下直接调用决策 API 的集成测试）
        # 也需要唤醒器；真实部署里的回调主要打到 API 进程，那边在 lifespan 里注册。
        register_graph_resumer(CompiledGraphResumer(graph))
        runner = PipelineRunner(graph, wait_timeout_s=wait_timeout_s, report_dir=report_dir)
        return await runner.start(
            skill_path, mode=mode, force_regenerate=force_regenerate, run_id=run_id
        )


async def reap_pending_hooks_with_graph(older_than_seconds: int) -> int:
    """定时巡检入口：先装配主图并注册唤醒器，再处理超时的 pending_hooks（interfaces/04 前置依赖）。

    ⚠️ `resume_in_background=False`（默认）时，巡检进程会在唤醒后把流水线一直跑到下一个挂起点，
    与 API 进程处理 Hook 回调的行为一致。定时作业的超时应当按"单个维度最长耗时"留足。
    """
    from skill_evaluate.graph.main import build_main_graph
    from skill_evaluate.persistence.reaper import reap_once

    async with build_async_checkpointer() as saver:
        graph = build_main_graph(saver)
        register_graph_resumer(CompiledGraphResumer(graph))
        return await reap_once(older_than_seconds)


__all__ = [
    "EXIT_BLOCKING",
    "EXIT_OK",
    "EXIT_STOPPED",
    "EXIT_SUSPENDED",
    "EXIT_SYSTEM_ERROR",
    "PipelineRunner",
    "RunOutcome",
    "initial_state",
    "reap_pending_hooks_with_graph",
    "run_pipeline",
]
