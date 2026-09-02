# 接入文档：pending_hooks_reaper 的调度接入

> 由谁接入：docs/dev/24（CI/CD 落地）。
> 当前状态：`scripts/pending_hooks_reaper.py` 可手动运行（`python
> scripts/pending_hooks_reaper.py --older-than 60`），逻辑完整（扫描
> `pending_hooks` 中超时的 `waiting` 记录 → 构造保守失败态 `ExecutionTrace` →
> `resolve_suspension()` 唤醒），但没有接入任何定时调度器。

## 接入方式（任选其一，docs/dev/24 决定）

- **GitHub Actions 定时 job**：新增 `.github/workflows/reaper.yml`，
  `schedule: cron: "*/2 * * * *"`（每 2 分钟），步骤里跑
  `python scripts/pending_hooks_reaper.py`。
- **常驻 worker**：在 CI 之外用 systemd/supervisor 跑一个循环调用
  `reap_once()` 的常驻进程（`reap_once` 已是可直接 `await` 的协程，见脚本
  `main()` 函数）。

## 前置依赖

调度接入前必须先完成 docs/dev/interfaces/04_graph_resumer.md（否则
`resolve_suspension()` 会抛 `ConfigurationError`）。
