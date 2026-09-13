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
     （及 `request.background_skills`，挂载方式见下文「追加契约：多技能挂载」），注入 `request.case.prompt` 作为初始任务，
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

## 追加契约：断言脚本的注入执行（docs/dev/10 第 4 节）

`create_sandbox()` 的实现方还必须处理 `request.assertion_specs`（docs/dev/10 给
`ExecutionRequest` 追加的字段，默认空列表，绝大多数用例形状不变）：

- 非空时，要求 Hermes 在**任务主流程结束、容器销毁之前**，把每个 spec 的
  `script_content` 写到 `script_path`，在**同一个沙箱**里按顺序执行
  （建议超时取 `ValidatorSettings.assertion_timeout_s`），工作目录与任务一致。
- 收集每条的 `exit_code` / `stdout` / `stderr`，随**同一次** Hook 回调以
  `assertion_executions[]` 上报（`HermesHookPayload` 已建模）。
  **不要为断言单独再发一次回调**——两次网络往返之间沙箱状态可能已经变了，那正是
  "在同一沙箱、任务之后执行"这条要求想避免的情况。
- `HermesBackend` 已经在下发前用 `executable_assertion_specs()` 滤掉了
  `strategy=NONE` 与没有脚本正文的 spec，client 拿到的一定是能跑的。
- `poll_sandbox()` 的拉取兜底路径同样应当把 `assertion_executions` 一起带回来，
  否则超时兜底场景下断言证据会静默丢失。

## 追加契约：采样参数与消融正文（docs/dev/19）

模块九对 `create_sandbox()` 的实现方追加两条要求：

- **`request.sampling_overrides` 必须原样下发给执行模型**（`temperature` / `top_p`）。参数扰动
  实验靠它；不下发的话"贪心基线"与"扰动分支"其实是同一种解码，实验永远得出"没有脆弱性"。
  模型不接受采样参数时，请在 Hermes 侧日志里记明。
- **以 `request.skill.body_markdown` 为准写出 SKILL.md**，不要从 `root_path` 重新读仓库文件。
  随机消融实验下发的是剥离了"咒语"的正文（`version_ref` 带 `+ablation` 后缀），从磁盘读会让
  消融版本与原版完全相同。`scripts/`、`references/` 仍按 `root_path` 挂载。

备用代理 `llama_control` 的运行时遵守同样两条（`docs/dev/interfaces/19_cross_model_generalization.md`
第 3.2 节）。

## 追加契约：多技能挂载（docs/dev/20）

模块十对 `create_sandbox()` 的实现方追加三条要求（`request.background_skills` 为空时形状不变）：

- **目标与每个背景 Skill 分别挂载到以 `skill_id` 命名的独立目录**（例如
  `/workspace/skills/<skill_id>/SKILL.md`，`scripts/`、`references/` 挂在同一目录下），全部注入
  Agent 可见的技能列表——不是只挂目标。
- **显式上报 `skill_md_loaded`，语义是"目标 Skill 是否被加载"**。沙箱里有多份 SKILL.md 时，
  `map_hermes_payload_to_trace()` 的路径兜底（路径含 `SKILL.md` 即视为加载）会被背景技能的读取误导。
- **轨迹里保留读取动作的真实路径**。模块十靠 SKILL.md 的直接父目录名把读取归到具体 Skill
  （`executors/skill_attribution.py`），据此判定触发劫持与"干扰技能被意外激活"。

不满足目录约定时不会得出错误结论，但劫持/熔断判定会大量落入"证据不足"。详见
`docs/dev/interfaces/20_multi_skill_conflict.md` 第 5 节。

## 不要做的事

- 不要在 `UnconfiguredHermesSandboxClient` 里伪造一个"成功"响应——那会让
  `HermesBackend` 在没有真实沙箱的情况下产出看似合法但完全虚构的
  `ExecutionTrace`，污染下游所有依赖真实执行证据的评测维度（模块一/三/四/五）。
