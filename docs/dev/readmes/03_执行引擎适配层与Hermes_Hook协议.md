# 实现说明：03 执行引擎适配层与 Hermes Hook 协议

> 对应设计文档：`docs/dev/03_执行引擎适配层与Hermes_Hook协议.md`
> 状态：已实现（真实 Hermes 网络调用留桩，见下方"已知留白"）

## 交付了什么

`src/skill_evaluate/executors/`：

| 文件 | 内容 | 对应设计文档章节 |
|---|---|---|
| `base.py` | `ExecutionRequest` / `ExecutorBackend` 抽象基类 | 第 2 节 |
| `mini_backend.py` | `MiniAgentBackend`、`MiniLLMClient` 协议、`StubMiniLLMClient` | 第 3 节 |
| `hermes_backend.py` | `HermesHookPayload`、`map_hermes_payload_to_trace()`、`HermesBackend`、`HermesSandboxClient` 协议 | 第 4 节 |
| `registry.py` | `register_backend()` / `get_backend()` 可插拔注册机制 | 第 4.1 节 |
| `routing.py` | `NODE_BACKEND_ROUTING` 后端路由表 | 第 5 节 |
| `sanitize.py` | `truncate_field()` 防刷屏截断 | 第 8 节 |
| `factory.py` | `build_backend()` / `get_backend_for_node()`（设计文档未显式命名，见下方说明） | 第 2/5 节 |

## 核心行为与设计文档的对应关系

- **`ExecutorBackend.execute()` 容错约定**（第 6 节）：`MiniAgentBackend` 与
  `HermesBackend` 均在内部捕获全部异常，返回结构合法的失败态 `ExecutionTrace`
  （`actions` 末尾追加 `action_type="internal_error"`），从不向上抛出裸异常。
  `MiniAgentBackend` 有对应单测
  `test_mini_agent_backend_fallback_on_llm_error_still_valid_trace`。
- **`loaded_skill_md` 判定**（第 4.3 节）：`map_hermes_payload_to_trace()` 优先
  取 `payload.skill_md_loaded` 显式布尔值，为 `None` 时才退化为扫描
  `trajectory[]` 里 `tool_name=="read_file"` 且路径命中 `SKILL.md` 的启发式。
  三种情形（显式 True、fallback 命中、fallback 未命中）均有单测覆盖。
- **后端路由表**（第 5 节）：`NODE_BACKEND_ROUTING` 与设计文档表格逐项一致，
  `resolve_backend_type()` 对未登记的节点名抛 `KeyError`，防止节点自行选择
  后端。
- **防刷屏**（第 8 节）：`truncate_field()` 默认 32KB 上限，头尾各留一半 +
  省略标记，`hermes_backend.py` 在映射 `stdout`/`stderr` 时复用同一函数，
  未各自实现一遍。

## 与设计文档的差异 / 必要补充

| 差异点 | 类型 | 说明 |
|---|---|---|
| `ExecutionRequest` 新增 `run_id: str \| None = None` | 补齐设计遗漏 | 设计文档第 4.4 节的 Hook 回调 URL `/hooks/hermes/{run_id}/{case_id}/{run_index}` 需要 `run_id`，但第 2 节定义的 `ExecutionRequest` 字段列表中遗漏了该字段。以可选字段追加，`MiniAgentBackend` 不使用该字段，不受影响 |
| `factory.py`（`build_backend`/`get_backend_for_node`） | 必要补充 | 设计文档第 2 节要求"节点通过依赖注入拿到实例，不直接 import 具体 Backend 类"，但未给出这层工厂函数的具体形态，遂新增该文件把 `routing.py` 的路由结果与 `registry.py` 的实例化逻辑接起来 |
| `HermesSandboxClient` 协议 + `UnconfiguredHermesSandboxClient` | 刻意留白（非偏差） | 见下方"已知留白" |

## 已知留白：真实 Hermes 网络调用

`HermesBackend.execute()` 的挂起-唤醒-回读全链路（`pending_hooks` 落库 →
`interrupt()` 挂起 → 等待 `resolve_suspension()` 唤醒 → `TraceRepository`
回读）已完整可跑；唯独"真的向一个真实存在的 Hermes 部署发起沙箱创建请求"
这一步，默认注入 `UnconfiguredHermesSandboxClient`，会显式抛出
`ExecutorBackendError` 而不是伪造一次"成功"。

原因：本仓库当前没有可联调的真实 Hermes 部署，伪造网络成功会让下游所有依赖
"真实执行证据"的评测维度（模块一/三/四/五）产出看似合法实则虚构的判定。

接入方式见 [`docs/dev/interfaces/03_hermes_sandbox_client.md`](../interfaces/03_hermes_sandbox_client.md)。

## 如何验证

```bash
pytest tests/skill_evaluate/test_executors.py -q
```

覆盖点：`MiniAgentBackend` happy-path/fallback、Hermes payload 映射的三种
`loaded_skill_md` 判定路径、`HermesBackend` 缺 `run_id` 时报错、
`health_check()` 在未配置真实客户端时为 `False`、路由表全量覆盖十个维度键、
未知维度报错、`registry` 默认注册 `hermes`、未知后端名报
`ConfigurationError`、以及"新增第二个异构后端"（为 `docs/dev/19` 验证扩展点）
的注册-取用流程。

## 消费方现状

- `persistence/`（04）：`suspension.py`、`repository.py::PendingHookRepository`
  被 `hermes_backend.py::HermesBackend.execute()` 延迟导入使用。
- `api/`（05）：`hooks_hermes.py` 复用 `map_hermes_payload_to_trace()` 与
  `HermesHookPayload`，是"该映射规则的唯一实现位置"（设计文档 05 第 2.2 节
  要求）。

## 待接入 / 下一步

- `docs/dev/interfaces/03_hermes_sandbox_client.md`：接入真实 Hermes 沙箱客户端。
- `docs/dev/07`：为 `MiniAgentBackend` 注入真实 `MiniLLMClient`（当前
  `StubMiniLLMClient` 仅保证 Trace 结构合法，不代表真实评审结论），见
  [`docs/dev/interfaces/05_langfuse_hook_and_agent_base.md`](../interfaces/05_langfuse_hook_and_agent_base.md) 第 2 节。
- `docs/dev/19`：新增 `llama_control` 等第二个异构后端，直接复用
  `register_backend()` 扩展点，无需改动本模块任何既有文件。
