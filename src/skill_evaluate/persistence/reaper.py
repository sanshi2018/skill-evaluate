"""`pending_hooks` 超时巡检（docs/dev/04 第 5 节第 3 点、docs/dev/03 第 4.4 节）。

原实现在 `scripts/pending_hooks_reaper.py`；docs/dev/24 把核心逻辑挪进包内，原因是调度入口
变成了 CLI 子命令 `skill-evaluate internal reap-pending-hooks`（GitHub Actions 定时 job 调它），
而 `scripts/` 不是可导入的包。脚本保留为薄包装，手动运行方式不变。

⚠️ 调用前进程里必须已注册 GraphResumer（interfaces/04_graph_resumer.md）：`resolve_suspension()`
在迁完账本之后才调 resumer，未注册时账本已改、线程却没被唤醒。CLI 入口负责先装配主图再调用本函数。
"""

from __future__ import annotations

from skill_evaluate.executors.hermes_backend import build_failure_trace
from skill_evaluate.logging import get_logger
from skill_evaluate.persistence.repository import PendingHookRepository, TraceRepository
from skill_evaluate.persistence.suspension import resolve_suspension

logger = get_logger(node_name="pending_hooks_reaper")


async def reap_once(
    older_than_seconds: int,
    *,
    hook_repository: PendingHookRepository | None = None,
    trace_repository: TraceRepository | None = None,
) -> int:
    """扫描并处理一批超时的 pending_hooks 记录，返回处理条数。

    对每条超时记录：保守构造失败态 ExecutionTrace（`loaded_skill_md=False`），
    落库后调用 `resolve_suspension()` 唤醒对应挂起节点，避免图状态永久挂起。
    真实的"拉取兜底"（调用 Hermes 查询 API 获取当前沙箱状态）需要接入具体的
    `HermesSandboxClient` 实现后才能在此处替换保守失败态的分支。

    仓储参数可注入（docs/dev/24 追加，默认值 = 原行为），便于不碰库地单测调度入口。
    """
    repo = hook_repository or PendingHookRepository()
    trace_repo = trace_repository or TraceRepository()
    stale = await repo.list_stale_waiting(older_than_seconds)

    for item in stale:
        trace = build_failure_trace(
            case_id=str(item["case_id"]),
            run_index=int(str(item["run_index"])),
            reason=f"wall-clock timeout after {older_than_seconds}s waiting for hermes hook",
            # docs/dev/15 第 8 节：本巡检**就是**墙钟超时那条路径，因此末尾动作记为
            # `sandbox_timeout` 而不是 `internal_error`。模块五的 DoS 判定据此把
            # "超时 = 成功阻断挂起"与"沙箱崩了"区分开——两者判定方向相反。
            timed_out=True,
        )
        await trace_repo.save(trace)
        await resolve_suspension(
            wait_key=str(item["wait_key"]),
            resume_payload=trace.trace_id,
            thread_id=str(item["thread_id"]),
        )
        logger.info("reaped_stale_pending_hook", wait_key=item["wait_key"], trace_id=trace.trace_id)

    return len(stale)


__all__ = ["reap_once"]
