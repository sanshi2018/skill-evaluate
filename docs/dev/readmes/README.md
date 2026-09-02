# 实现说明索引（docs/dev/readmes）

本目录记录**已落地代码**与 `docs/dev/00~05` 设计文档之间的对应关系：每份设计
文档对应一份实现说明，描述实际交付了哪些文件、对外暴露了什么接口、和设计
文档相比有哪些必要的补充/偏差、如何验证，以及留给后续模块的接入点。

设计文档本身（`docs/dev/01`~`05`）不因代码落地而改写；本目录是"设计 → 实现"
的落地记录，供后续模块开发者/审阅者核对代码是否忠实履行了设计文档。

| 文档 | 对应设计文档 | 状态 |
|---|---|---|
| [01_项目脚手架与技术栈基线.md](./01_项目脚手架与技术栈基线.md) | `docs/dev/01` | 已实现 |
| [02_核心状态模型与数据契约.md](./02_核心状态模型与数据契约.md) | `docs/dev/02` | 已实现 |
| [03_执行引擎适配层与Hermes_Hook协议.md](./03_执行引擎适配层与Hermes_Hook协议.md) | `docs/dev/03` | 已实现（Hermes 真实网络调用桩化） |
| [04_PostgresSaver持久化与Checkpoint恢复机制.md](./04_PostgresSaver持久化与Checkpoint恢复机制.md) | `docs/dev/04` | 已实现（图唤醒桩化，待 24 接入） |
| [05_可观测性骨架_双写报告与Langfuse集成.md](./05_可观测性骨架_双写报告与Langfuse集成.md) | `docs/dev/05` | 已实现（人工审批端点桩化，待 22 接入） |

## 未落地部分

`docs/dev/06` 及之后（Generator/Mini/Judge/Optimizer/Validator Agent、各评测
维度节点、主图编排）尚未实现，仅在 `src/skill_evaluate/agents/`、
`src/skill_evaluate/nodes/`、`src/skill_evaluate/graph/`、
`src/skill_evaluate/memory/` 下预留了占位包（每个 `__init__.py` 均标注了归属
文档）。

## 已知接口留白

三处"必须等待后续模块才能完全跑通"的接口留白，均已在
`docs/dev/interfaces/` 下沉淀为独立接入文档：

- [`03_hermes_sandbox_client.md`](../interfaces/03_hermes_sandbox_client.md) — 真实 Hermes 沙箱网络客户端
- [`04_graph_resumer.md`](../interfaces/04_graph_resumer.md) — 唤醒挂起主图节点
- [`04_pending_hooks_reaper_scheduling.md`](../interfaces/04_pending_hooks_reaper_scheduling.md) — 超时巡检脚本的调度接入
- [`05_hooks_approval.md`](../interfaces/05_hooks_approval.md) — 人工审批回调端点
- [`05_langfuse_hook_and_agent_base.md`](../interfaces/05_langfuse_hook_and_agent_base.md) — Agent 基类挂载 Langfuse / MiniLLMClient

## 质量基线

```bash
pip install -e ".[dev]"
ruff check src tests scripts
ruff format --check src tests scripts
mypy src
pytest
```

以上四项在本次实现完成时全部通过（31 个测试用例）。持久化/API 层缺少真实
Postgres 集成测试（本地沙箱无 Docker），仅通过单元测试 + `mypy --strict` +
人工代码审查验证。
