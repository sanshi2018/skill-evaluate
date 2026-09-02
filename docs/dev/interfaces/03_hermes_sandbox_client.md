# 接入文档：HermesSandboxClient（真实 Hermes 沙箱网络交互）

> 由谁接入：实际接入 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)（或等价
> Hermes 部署）时补充；架构上属于 docs/dev/03 遗留、docs/dev/19（异构后端）之前必须补齐的一环。
> 当前状态：协议、字段映射、挂起/唤醒链路均已实现，唯独"发一个真实 HTTP 请求给一个真实
> 存在的 Hermes 部署"这一步是桩实现。

## 现状

`src/skill_evaluate/executors/hermes_backend.py` 定义了：

```python
class HermesSandboxClient(Protocol):
    async def create_sandbox(
        self, *, request: ExecutionRequest, callback_url: str, hook_secret: str,
    ) -> HermesSandboxHandle: ...

    async def poll_sandbox(self, sandbox_id: str) -> HermesHookPayload | None: ...

    async def is_reachable(self) -> bool: ...
```

默认注入 `UnconfiguredHermesSandboxClient`：`create_sandbox()` 显式抛出
`ExecutorBackendError`，`is_reachable()` 恒为 `False`，`poll_sandbox()` 恒为 `None`。
`HermesBackend.execute()` 的其余流程（`pending_hooks` 落库 → `interrupt()` 挂起 →
`resolve_suspension()` 唤醒 → `TraceRepository` 读回）已完整可跑，只是永远走不到
"真的创建了一个沙箱"这一步。

## 接入方式

1. 新建 `src/skill_evaluate/executors/hermes_sandbox_client.py`，实现
   `HermesSandboxClient` 协议：
   - `create_sandbox()`：POST 到 Hermes 的沙箱创建 API，挂载 `request.skill`
     （及 `request.background_skills`），注入 `request.case.prompt` 作为初始任务，
     传入 `callback_url`/`hook_secret`（Hermes 侧据此在执行结束后回调
     `POST {callback_url}`，body 为 `HermesHookPayload` 结构，签名走
     `X-Hermes-Signature` header，见 `docs/dev/03` 第 4.4 节、`api/security.py`）。
   - `poll_sandbox()`：供"拉取兜底"复用——调用 Hermes 的沙箱查询 API，把响应体
     解析为 `HermesHookPayload`（若沙箱仍在运行返回 `None`）。
   - `is_reachable()`：供 `HermesBackend.health_check()`（docs/dev/21 金丝雀探针）
     调用，建议命中 Hermes 的健康检查端点。
2. 在 `HermesBackend.__init__` 的默认构造处（或 `executors/factory.py`）把
   `UnconfiguredHermesSandboxClient()` 换成新实现的实例。
3. `scripts/pending_hooks_reaper.py` 中 `reap_once()` 当前对所有超时记录直接
   构造保守失败态 `ExecutionTrace`；接入真实 client 后，应先调用
   `sandbox_client.poll_sandbox(sandbox_id)` 做一次拉取兜底，取到什么算什么，
   仍取不到再退化为失败态（docs/dev/03 第 4.4 节第 4 点）。这要求
   `pending_hooks` 表补充记录 `sandbox_id`（当前表结构未存，需要新增 Alembic
   revision 追加该列）。

## 不要做的事

- 不要在 `UnconfiguredHermesSandboxClient` 里伪造一个"成功"响应——那会让
  `HermesBackend` 在没有真实沙箱的情况下产出看似合法但完全虚构的
  `ExecutionTrace`，污染下游所有依赖真实执行证据的评测维度（模块一/三/四/五）。
