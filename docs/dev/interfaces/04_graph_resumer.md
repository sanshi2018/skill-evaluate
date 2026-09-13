# 接入文档：GraphResumer（唤醒挂起的主图节点）

> ✅ **docs/dev/24 已接入**：`src/skill_evaluate/graph/resumer.py::CompiledGraphResumer`，由 API 进程 lifespan、CLI `run` /
> `internal run-cold-suite`、`internal reap-pending-hooks` 在主图编译后注册。相对下文示例的增强：`GraphResumer.resume()`
> 追加可选参数 `wait_key`（`resolve_suspension()` 已传入），据此在多个并行中断里按中断 id 精确唤醒；同一 thread 进程内串行；
> 可选后台执行（`SKILLEVAL_PIPELINE_RESUME_IN_BACKGROUND`）。checkpointer 必须用 `build_async_checkpointer()`。详见 `docs/dev/interfaces/24_main_graph_and_ci_cd.md` 第 5 节。

> 由谁接入：docs/dev/24（主图编排与 CI/CD 落地），主图 `compile()` 完成后。
> 当前状态：`persistence/suspension.py` 已实现完整的挂起-唤醒记账逻辑
> （`pending_hooks`/`human_approvals` 状态迁移），唯独"真正调用已编译主图的
> `graph.invoke(Command(resume=...))`"这一步留空。

## 现状

`src/skill_evaluate/persistence/suspension.py`：

```python
class GraphResumer(Protocol):
    async def resume(self, *, thread_id: str, resume_payload: Any) -> None: ...

def register_graph_resumer(resumer: GraphResumer) -> None: ...
```

`resolve_suspension()` 在成功把 `pending_hooks`/`human_approvals` 记录状态从
`waiting` 迁移到 `resolved` 后，会调用 `_get_graph_resumer().resume(...)`；如果
尚未 `register_graph_resumer()`，直接抛 `ConfigurationError`（不会静默失败，
Hook 端点会因此返回 500，明确暴露"主图还没接好"这一事实，而不是悄悄丢弃唤醒）。

> **docs/dev/22 落地后**：人工审批决策 API（`POST /api/approvals/{id}/decide`）会在写任何状态
> **之前**先调 `is_graph_resumer_registered()`，未注册时对阻塞卡片返回 **503**——因为
> `resolve_suspension()` 先迁账本再调 resumer，抛错那一刻账本已改，卡片就再也唤醒不了。
> 注册仍是 `24` 的职责；挂起点统一的 thread_id 口径为 `f"{skill_id}:{run_id}"`，
> `CompiledGraphResumer` 直接使用传入的 `thread_id` 即可。

## 接入方式

在 `docs/dev/24` 完成 `graph = builder.compile(checkpointer=checkpointer,
interrupt_before=[...])` 之后，新建一个实现：

```python
# src/skill_evaluate/graph/resumer.py（示例，由 24 文档落地）
from skill_evaluate.persistence.suspension import register_graph_resumer

class CompiledGraphResumer:
    def __init__(self, graph):
        self._graph = graph

    async def resume(self, *, thread_id: str, resume_payload):
        from langgraph.types import Command
        await self._graph.ainvoke(
            Command(resume=resume_payload),
            config={"configurable": {"thread_id": thread_id}},
        )

register_graph_resumer(CompiledGraphResumer(graph))
```

在进程启动时（API 层 `app.py` 的 lifespan、或 CLI `run` 命令入口）执行一次
`register_graph_resumer(...)`，保证 `api/hooks_hermes.py`、`api/hooks_approval.py`
调用 `resolve_suspension()` 时该注册已完成。

## 注意事项

- `register_graph_resumer` 是进程级全局单例，一个进程只应持有一个已编译主图
  实例；如果 API 进程与图执行进程分离部署，需要改为跨进程 RPC 唤醒（如通过
  一个内部队列），届时替换 `CompiledGraphResumer` 的实现而不改
  `suspension.py` 的接口。
