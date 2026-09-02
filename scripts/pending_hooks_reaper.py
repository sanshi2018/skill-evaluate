"""定时巡检 `pending_hooks` 中超时未回调的记录，触发拉取兜底 / 保守失败态 resume
（docs/dev/04 第 5 节第 3 点、docs/dev/03 第 4.4 节）。

调度方式待接入：docs/dev/24 会以独立 CI job 或常驻 worker 的定时任务形式接入
（见 docs/dev/interfaces/04_pending_hooks_reaper.md）。当前可手动运行：

    python scripts/pending_hooks_reaper.py --older-than 60
"""

from __future__ import annotations

import argparse
import asyncio

from skill_evaluate.config import get_settings
from skill_evaluate.executors.hermes_backend import build_failure_trace
from skill_evaluate.logging import get_logger
from skill_evaluate.persistence.repository import PendingHookRepository, TraceRepository
from skill_evaluate.persistence.suspension import resolve_suspension

logger = get_logger(node_name="pending_hooks_reaper")


async def reap_once(older_than_seconds: int) -> int:
    """扫描并处理一批超时的 pending_hooks 记录，返回处理条数。

    对每条超时记录：保守构造失败态 ExecutionTrace（`loaded_skill_md=False`），
    落库后调用 `resolve_suspension()` 唤醒对应挂起节点，避免图状态永久挂起。
    真实的"拉取兜底"（调用 Hermes 查询 API 获取当前沙箱状态）需要接入具体的
    `HermesSandboxClient` 实现后才能在此处替换保守失败态的分支。
    """
    repo = PendingHookRepository()
    trace_repo = TraceRepository()
    stale = await repo.list_stale_waiting(older_than_seconds)

    for item in stale:
        trace = build_failure_trace(
            case_id=item["case_id"],
            run_index=item["run_index"],
            reason=f"wall-clock timeout after {older_than_seconds}s waiting for hermes hook",
        )
        await trace_repo.save(trace)
        await resolve_suspension(
            wait_key=item["wait_key"], resume_payload=trace.trace_id, thread_id=item["thread_id"]
        )
        logger.info("reaped_stale_pending_hook", wait_key=item["wait_key"], trace_id=trace.trace_id)

    return len(stale)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--older-than",
        type=int,
        default=get_settings().executor.sandbox_wall_clock_timeout_s,
        help="超过多少秒仍处于 waiting 状态即视为超时（默认取 ExecutorSettings.sandbox_wall_clock_timeout_s）",
    )
    args = parser.parse_args()
    count = asyncio.run(reap_once(args.older_than))
    logger.info("reaper_run_complete", reaped_count=count)


if __name__ == "__main__":
    main()
