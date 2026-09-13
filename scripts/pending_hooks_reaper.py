"""定时巡检 `pending_hooks` 中超时未回调的记录，触发拉取兜底 / 保守失败态 resume
（docs/dev/04 第 5 节第 3 点、docs/dev/03 第 4.4 节）。

✅ docs/dev/24 已接入调度：GitHub Actions `scheduled_maintenance.yml` 每 5 分钟调用
`skill-evaluate internal reap-pending-hooks`。核心逻辑已移到
`skill_evaluate.persistence.reaper.reap_once`；本脚本保留为手动运行入口：

    python scripts/pending_hooks_reaper.py --older-than 60

与 CLI 子命令一样，运行前会装配主图并注册 GraphResumer——否则 `resolve_suspension()`
迁完账本后找不到唤醒器，挂起的线程再也醒不过来。
"""

from __future__ import annotations

import argparse
import asyncio

from skill_evaluate.config import get_settings
from skill_evaluate.graph.runner import reap_pending_hooks_with_graph
from skill_evaluate.logging import configure_logging, get_logger

logger = get_logger(node_name="pending_hooks_reaper")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--older-than",
        type=int,
        default=get_settings().executor.sandbox_wall_clock_timeout_s,
        help="超过多少秒仍处于 waiting 状态即视为超时（默认取 ExecutorSettings.sandbox_wall_clock_timeout_s）",
    )
    args = parser.parse_args()
    configure_logging()
    count = asyncio.run(reap_pending_hooks_with_graph(args.older_than))
    logger.info("reaper_run_complete", reaped_count=count)


if __name__ == "__main__":
    main()
