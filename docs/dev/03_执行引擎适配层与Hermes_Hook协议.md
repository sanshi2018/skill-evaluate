# 03 执行引擎适配层与 Hermes Hook 协议

> 状态：**待确认**
> 路线图位置：第 0 层 / 第 3 份
> 依赖：`02_核心状态模型与数据契约.md`（本文档产出的一切最终都必须收敛为 `ExecutionTrace`）
> 被依赖：`05`（可观测性双写）、`06`（Generator，间接）、`08`（Judge）、`11~20`（全部评测维度节点）、`21`（沙箱环境指纹与金丝雀探针）

---

## 1. 本文档目标

落实你的第二条关键约束：**简单维度用内置 Mini Agent，需要完整执行 Skill 的维度用外置可插拔 Agent（默认 Hermes-agent），通过 Hook 机制回传执行信息**。本文档产出：

1. `ExecutorBackend` 抽象接口（面向 `nodes/` 下所有评测维度节点的统一编程界面）。
2. `MiniAgentBackend`：内置轻量后端，不启动外部沙箱，直接调用 LLM API 做文本级/静态分析。
3. `PluggableAgentBackend`：外置完整执行后端骨架，默认实现对接 Hermes-agent；同时定义"可插拔"的注册机制，为文档 19（跨模型泛化，需要接入第二个异构 Agent）预留扩展点。
4. Hermes Hook 回调协议：Hermes 侧如何把执行过程中的思考/工具调用/环境反馈，实时或结束时回传给本流水线，并映射为文档 02 定义的 `ExecutionTrace`。
5. 沙箱资源与安全边界的工程约定（Ephemeral Docker、无出站网络/白名单出站、Wall-clock 超时墙）。

## 2. `ExecutorBackend` 抽象接口

```python
# src/skill_evaluate/executors/base.py
from abc import ABC, abstractmethod
from skill_evaluate.state.trace import ExecutionTrace
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase


class ExecutionRequest(BaseModel):
    skill: SkillDefinition
    case: TestCase
    run_index: int                     # 第几次冗余执行（0-based）
    load_skill: bool = True             # False 时用于模块三"不加载 Skill 的基线对照"
    background_skills: list[SkillDefinition] = Field(default_factory=list)  # 模块十并发干扰包
    sampling_overrides: dict | None = None  # 模块九：temperature/top_p 扰动
    wall_clock_timeout_s: int = 60           # 模块五：单次执行硬超时


class ExecutorBackend(ABC):
    backend_type: ExecutorBackendType   # 子类固定声明

    @abstractmethod
    async def execute(self, request: ExecutionRequest) -> ExecutionTrace:
        """同步/异步均可，但对外统一 async 接口；实现内部自行处理沙箱生命周期。
        必须保证：无论成功/超时/异常，都返回一个合法的 ExecutionTrace（失败态也要建模，
        不允许抛出未捕获异常让调用方处理裸 Exception——见第 6 节容错约定。
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """供 21 文档的金丝雀探针复用：验证后端当前是否可用"""
```

所有 `nodes/` 下的评测节点，只依赖 `ExecutorBackend` 接口和 `ExecutionRequest`/`ExecutionTrace`，通过依赖注入拿到具体实例，不直接 import `MiniAgentBackend` 或 `PluggableAgentBackend`。后端选择由 `config.py` 中 `ExecutorSettings.backend` 驱动，并支持**按节点覆盖**（见第 5 节）。

## 3. `MiniAgentBackend`：内置轻量后端

```python
# src/skill_evaluate/executors/mini_backend.py
class MiniAgentBackend(ExecutorBackend):
    backend_type = ExecutorBackendType.MINI

    async def execute(self, request: ExecutionRequest) -> ExecutionTrace:
        """
        不启动外部沙箱、不实际执行 scripts/。
        用于模块二（静态审查）、模块四（部分子检查，如 --help 文本审查可复用 mini 调用）等
        "偏文本/逻辑判断"场景。此时 ExecutionTrace 中：
          - actions 为空或仅含一条虚拟 "static_review" 步骤
          - modified_files_manifest 恒为空
          - loaded_skill_md 恒为 True（因为整篇 SKILL.md 就是作为 prompt 输入的）
        实际的评审逻辑由文档 07（Mini Agent 评审框架）在此基础上封装 Prompt 模板，
        本文档只负责把"调用一次 LLM + 记录 timing"包装成合法的 ExecutionTrace。
        """
```

**关键约束**：`MiniAgentBackend` **不适用于**任何需要验证"Agent 是否真的调用了工具 / 是否触发了 Skill / 是否产出了文件"的评测节点（模块一的触发判定、模块三的执行效果评测、模块四的黑盒探测、模块五的红队攻击）——这些必须用 `PluggableAgentBackend`。哪些维度用哪种后端，由第 5 节的"后端路由表"固定，不由节点自行决定，避免后续开发时选错后端导致评测失真。

## 4. `PluggableAgentBackend`：外置可插拔完整执行后端

### 4.1 注册机制

```python
# src/skill_evaluate/executors/registry.py
_BACKEND_REGISTRY: dict[str, type[ExecutorBackend]] = {}

def register_backend(name: str):
    def _decorator(cls: type[ExecutorBackend]):
        _BACKEND_REGISTRY[name] = cls
        return cls
    return _decorator

def get_backend(name: str) -> ExecutorBackend: ...
```

默认注册 `"hermes"`；文档 19（跨模型泛化）新增第二个异构后端时，只需 `@register_backend("llama_control")` 注册一个新类，不改动本文件其余部分。

### 4.2 Hermes 适配实现骨架

```python
# src/skill_evaluate/executors/hermes_backend.py
@register_backend("hermes")
class HermesBackend(ExecutorBackend):
    backend_type = ExecutorBackendType.PLUGGABLE

    def __init__(self, endpoint: str, hook_secret: str): ...

    async def execute(self, request: ExecutionRequest) -> ExecutionTrace:
        """
        1. 向 Hermes 沙箱管理 API 发起容器创建请求，挂载 request.skill（及 background_skills）
        2. 注入 request.case.prompt 作为初始任务
        3. 等待 Hermes 通过 Hook（见第 5 节协议）回传完整执行数据，或超时
        4. 将 Hermes 原生 payload 映射为 ExecutionTrace（见 4.3 映射规则）
        5. 容器销毁（Ephemeral，一次性，见第 7 节）
        """
```

### 4.3 Hermes 原生 Payload → `ExecutionTrace` 映射规则

架构文档模块三第 2 节已经把 Hermes 应回传的字段列全了，本节把它固化为**强制字段映射表**，防止 Hermes 版本升级后字段名漂移导致静默丢数据：

| Hermes Hook Payload 字段（约定命名） | 映射到 `ExecutionTrace` |
|---|---|
| `usage.total_tokens` / `prompt_tokens` / `completion_tokens` | `timing.*_tokens` |
| `usage.duration_ms` | `timing.duration_ms` |
| `trajectory[]`（每项含 `thought`, `tool_name`, `tool_input`, `exit_code`, `stdout`, `stderr`, `ts`） | `actions[]`（`ActionStep`，`tool_name→action_type`，`tool_input→action_input`） |
| `final_message` | `final_response` |
| `fs_diff[]`（每项含 `path`, `sha256`, `op`） | `modified_files_manifest[]` |
| `skill_md_loaded: bool`（由 Hermes hook 显式上报，而非猜测） | `loaded_skill_md` |
| `started_at` / `finished_at` | 同名字段 |

**`loaded_skill_md` 的确定性要求**：这是模块一触发率判定的核心依据，不能靠"扫描 final_response 里是否提到关键词"这种模糊启发式。要求 Hermes 侧 Hook 在其工具调用层显式拦截"读取 `SKILL.md`"这一动作并上报布尔标志（Hermes 集成细节见 4.4）。如果所接入的第三方 Agent（如未来的 `llama_control`）无法提供这一显式信号，退化方案是：扫描 `trajectory[]` 中是否存在 `action_type == "read_file"` 且 `action_input.path` 命中 `SKILL.md` 路径，作为 fallback 判定逻辑，两种方式在实现中都要保留，优先取显式信号。

### 4.4 Hermes 集成方式（Hook 协议）

采用**回调 Webhook + 拉取兜底**双通道，避免长任务下纯回调因网络抖动丢失：

1. `HermesBackend.execute()` 创建沙箱时，传入 `callback_url = f"{internal_api}/hooks/hermes/{run_id}/{case_id}/{run_index}"` 和 `hook_secret`（HMAC 签名，本文档要求所有回调必须校验签名，防止伪造 Trace 注入）。
2. Hermes 执行结束（成功/失败/超时）后 POST 该 URL，body 为 4.3 表格左列结构的 JSON。
3. 本流水线暴露一个轻量 HTTP 端点（`observability/` 或独立 `hooks/` 模块，具体落位在文档 05 定，本文档只定协议不定端点宿主）接收并落库，同时唤醒等待该 `run_id` 的 LangGraph 节点（LangGraph 支持外部事件唤醒挂起节点，实现方式在文档 04 结合 Checkpointer 讨论）。
4. **拉取兜底**：如果 `wall_clock_timeout_s` 到期仍未收到回调，`HermesBackend` 主动调用 Hermes 的查询 API 拉取当前沙箱状态一次，取到什么算什么，并将 `loaded_skill_md` 等无法确定的字段标记为 `False`（保守判定，宁可漏判触发也不可误判触发——避免模块一假阳性）。

## 5. 后端路由表（哪个评测维度用哪种后端，固定映射，写入配置而非散落各节点）

```python
# src/skill_evaluate/executors/routing.py
NODE_BACKEND_ROUTING: dict[str, ExecutorBackendType] = {
    "trigger_accuracy":        ExecutorBackendType.PLUGGABLE,  # 模块一：必须真实观测是否加载 SKILL.md
    "context_scoping":         ExecutorBackendType.MINI,        # 模块二：纯静态文本审查
    "instruction_control":     ExecutorBackendType.PLUGGABLE,   # 模块三：A/B 对比、Trace 深度审查
    "script_usability":        ExecutorBackendType.PLUGGABLE,   # 模块四：真实黑盒调用脚本子进程
    "security":                 ExecutorBackendType.PLUGGABLE,  # 模块五：红队攻击必须真实执行
    "coverage_analysis":         ExecutorBackendType.MINI,       # 模块六/七/八：主要是对已有 Trace/文本的分析
    "cross_model_generalization": ExecutorBackendType.PLUGGABLE, # 模块九：异构矩阵
    "multi_skill_conflict":        ExecutorBackendType.PLUGGABLE, # 模块十：并发加载必须真实沙箱
}
```

各评测维度对应文档（11~20）在实现节点时，从此表读取默认后端，不再自行判断；若某维度需要"部分子检查用 mini、部分用 pluggable"（如模块四的 `--help` 文本审查可以复用 mini 调用去做语义判断，但脚本本身的挂起测试必须 pluggable 真跑），由该维度文档在内部拆分子节点各自声明，而不是让整个维度用单一后端牵就最严格的子检查。

## 6. 容错约定

- `execute()` 内部捕获一切异常，统一转换为"失败态 `ExecutionTrace`"而非向上抛出——`final_response` 填入错误摘要，`actions` 末尾追加一条 `action_type="internal_error"` 记录原始异常堆栈（截断，见第 8 节防刷屏）。这保证 Judge Agent（08）永远面对结构一致的输入，不需要对"执行阶段崩了"和"Skill 本身执行失败"做两套处理逻辑——两者在 Trace 层面统一表示。
  - **docs/dev/15 的一处追加约定**：**墙钟超时**这条路径记 `action_type="sandbox_timeout"` 而不是 `internal_error`（常量在 `executors/hermes_backend.py`：`ACTION_TYPE_SANDBOX_TIMEOUT` / `ACTION_TYPE_INTERNAL_ERROR`）。原因是模块五的 DoS 判定里**超时即通过**——墙钟约束成功阻断了挂起，这正是期望的结果；而沙箱崩溃且没给建设性报错是不通过。两者都记 `internal_error` 的话，这两个方向相反的结论就区分不出来，一次成功的防御会被读成一次失守。
  - 实现上是 `build_failure_trace(..., timed_out=True)`，默认 `False` 保证既有调用方行为不变；**唯一**传 `True` 的是 `scripts/pending_hooks_reaper.py`（它就是墙钟超时那条路径）。其余维度对这两个取值一视同仁（都是"这次没跑成"），因此该改动向后兼容。
- 沙箱创建失败（如 Hermes 服务不可用）单独抛出 `ExecutorBackendError`（继承自文档 01 定义的 `SkillEvaluateError`），由节点层决定是重试还是让整条流水线挂起（挂起策略在文档 09/22 细化）——这一类是"评测系统自身故障"，不应该被误判为"Skill 评测失败"。

## 7. 沙箱安全边界（工程约定，供 `HermesBackend` 及未来新后端遵守）

- **Ephemeral 容器**：每次 `execute()` 调用对应一个全新容器，执行结束（无论成败）立即销毁，不复用、不缓存文件系统状态。这是模块五"必须强制 Hermes Agent 在短暂的、无网络权限的 Ephemeral Container 中执行"的直接落地。
- **默认无出站网络**；确需网络的评测子场景（如模块一测试用例可能涉及"帮我查一下 xxx"这类需要联网的近脱靶用例）走白名单机制，白名单在 `ExecutorSettings` 中以显式列表配置，默认空。
- **Wall-clock 超时墙**：`ExecutionRequest.wall_clock_timeout_s` 默认值取自 `config.py` 的 `sandbox_wall_clock_timeout_s`（文档 01 已定义，默认 60 秒），模块五 DoS 探测场景下超时即视为"成功阻断挂起"，由调用方（模块五节点）负责把"超时"这一事实翻译为 Pass 判定，`HermesBackend` 本身只负责如实上报超时事件，不做业务语义判断。

## 8. 防刷屏 / 输出截断约定

`ActionStep.stdout` / `stderr` 及 `final_response` 在写入 `ExecutionTrace` 前统一经过截断（默认单字段上限 32KB，超出部分保留头尾各一半 + 中间省略标记），避免模块五"逻辑炸弹/Zip Bomb"场景下超长输出把 Postgres 记录或后续 LLM 裁判的上下文撑爆。截断逻辑放在 `executors/sanitize.py`，两个 Backend 实现共用，不允许各自实现一遍。

## 9. 并发与冗余执行的编排位置

架构文档要求"每个测试用例运行 3 次"。这不是 `ExecutorBackend` 自身的职责（`execute()` 只负责单次执行），而是由调用方（模块一等节点）对同一 `case_id` 以 `run_index=0,1,2` 并发发起三次 `execute()` 调用后聚合。本文档只保证：`ExecutionTrace.run_index` 字段存在，供聚合层区分。三次结果如何聚合成"触发率"，属于模块一（文档 11）的业务逻辑，不在本文档范围。

## 10. 待接入文档（本文档留给后续模块的接口清单）

| 预留位置 | 当前状态 | 由哪份文档接入 | 接入方式 |
|---|---|---|---|
| Hook 接收 HTTP 端点的具体宿主/路由实现 | 协议已定义（4.4），端点未落地 | `05` | 在可观测性/API 层实现 `POST /hooks/hermes/{run_id}/{case_id}/{run_index}`，按 4.3 表格解析并落库为 `ExecutionTrace`，同时唤醒对应 LangGraph 挂起节点 |
| LangGraph 节点等待外部 Hook 唤醒的具体机制 | 仅提及"结合 Checkpointer 讨论" | `04` | 定义基于 `interrupt`/外部事件表的等待与恢复模式 |
| `NODE_BACKEND_ROUTING` 中模块六/七/八条目 | 暂定整体为 MINI | `16~18` | 如实现中发现某子检查需要真实执行（如模块七的组合能力探测可能需要真实调用），可在对应文档内为具体子节点覆盖后端，不修改本表的维度级默认值 |
| `llama_control` 等第二个异构 Backend | 仅有注册机制，无实现 | `19` | 新建 `executors/llama_backend.py`，实现 `ExecutorBackend`，`@register_backend("llama_control")` 注册 |
| `health_check()` 的消费方 | 接口已声明，无调用方 | `21` | 金丝雀探针节点在主测试集运行前调用，失败即挂起废弃当次评测 |

---

## 下一步

待你确认本文档后，我将输出 **文档 04：PostgresSaver 持久化与 Checkpoint 恢复机制**——衔接本文档遗留的"外部 Hook 唤醒挂起节点"接入点，同时把文档 02 中 `PipelineState` 的引用字段落到具体的表结构。
