# 04 PostgresSaver 持久化与 Checkpoint 恢复机制

> 状态：**待确认**
> 路线图位置：第 0 层 / 第 4 份
> 依赖：`02_核心状态模型与数据契约.md`（表结构对应的 Pydantic 模型）、`03_执行引擎适配层与Hermes_Hook协议.md`（本文档需接入"外部 Hook 唤醒挂起节点"的待接入项）
> 被依赖：`05`（Hook 端点落库后调用本文档的 repository）、`09`（Optimizer 挂起触发）、`10`（Validator 校验结果落库）、`22`（人工审批闭环恢复机制）、`24`（主图装配）

---

## 1. 本文档目标

1. 接线 LangGraph 的 `PostgresSaver`，作为整条流水线唯一的 Checkpoint 后端，解决"CI Runner 被 K8s 驱逐/超时中断后如何从断点精准恢复"的问题。
2. 把文档 02 定义的重量级模型（`ExecutionTrace`、`TestSuiteVersion`、`CapabilityTree`、`JudgeVerdict`、`SecurityFinding`、`AssertionSpec/Result`）落到具体表结构，并提供统一的 Repository 层，供后续所有文档"按 ID 存取"而不是各自手写 SQL。
3. 补齐文档 03 遗留的"LangGraph 节点等待外部 Hook 唤醒"的具体机制——这是本文档对文档 03 的**接入**，同时也是本文档自身向文档 22（人工审批恢复）预留的同一套机制的复用基础。
4. 定义迁移工具与 schema 版本管理策略。

## 2. `PostgresSaver` 接线

```python
# src/skill_evaluate/persistence/checkpointer.py
from langgraph.checkpoint.postgres import PostgresSaver
from skill_evaluate.config import get_settings

def build_checkpointer() -> PostgresSaver:
    settings = get_settings()
    saver = PostgresSaver.from_conn_string(settings.db.dsn)
    saver.setup()   # 幂等：首次运行建表，之后运行跳过
    return saver
```

- `saver.setup()` 建的是 LangGraph 官方管理的 Checkpoint 表（`checkpoints`、`checkpoint_writes` 等），与本文档下方"业务表"（第 3 节）分离，不混用同一套迁移工具管理——Checkpoint 表结构由 LangGraph 库版本决定，业务表由我们自己的 Alembic 迁移管理，升级 `langgraph-checkpoint-postgres` 版本时只需重跑 `saver.setup()`，不影响业务表迁移历史。
- 编译图时统一挂载：`graph.compile(checkpointer=build_checkpointer(), interrupt_before=[...])`（`interrupt_before` 列表由文档 09/22 逐步追加，本文档只提供挂载点，见第 5 节）。
- `thread_id`（LangGraph 会话标识）约定取值 `f"{skill_id}:{run_id}"`，保证同一 Skill 的历次评测互不干扰，同时同一次 `run_id` 中断后可用相同 `thread_id` 精确恢复。

## 3. 业务表结构（Alembic 管理，独立于 Checkpoint 表）

新增依赖：`alembic>=1.13`（补入文档 01 的 `pyproject.toml` dev 组，作为本文档对脚手架文档的一处小追加，符合"新增字段不改语义"的兼容约定）。

```
src/skill_evaluate/persistence/
├── migrations/               # alembic 版本目录
│   ├── env.py
│   └── versions/
│       └── 0001_initial_schema.py
├── models.py                  # SQLAlchemy ORM 模型（与 state/ 下 Pydantic 模型一一对应，职责分离：
│                               #   Pydantic 负责跨进程/跨节点的数据契约，SQLAlchemy 负责落盘）
├── repository.py               # 统一 Repository 接口
└── checkpointer.py
```

核心表（0001 迁移，字段与文档 02 模型字段一一对应，此处只列表名与主外键关系，详细字段见 `models.py` 与对应 Pydantic 模型保持同名）：

| 表名 | 对应 Pydantic 模型 | 关键索引 |
|---|---|---|
| `skills` | `SkillDefinition` | `(skill_id, version_ref)` 唯一 |
| `test_cases` | `TestCase` | `skill_id`, `case_id` 主键 |
| `test_suite_versions` | `TestSuiteVersion` | `(skill_id, is_active)` 部分唯一索引：同 skill 只允许一条 `is_active=true` |
| `execution_traces` | `ExecutionTrace` | `(case_id, run_index)` |
| `capability_trees` | `CapabilityTree`（`nodes`/`negative_constraints` 以 JSONB 内嵌存储，量级可控，不额外拆表） | `(skill_id, skill_version_ref)` |
| `judge_verdicts` | `JudgeVerdict` | `subject_id` |
| `consensus_results` | `ConsensusResult` | `subject_id` |
| `security_findings` | `SecurityFinding` | `case_id`, `severity` |
| `assertion_specs` / `assertion_results` | `AssertionSpec` / `AssertionResult` | `case_id` |
| `pending_hooks` | 无对应 Pydantic 模型，见第 5 节 | `run_id, case_id, run_index` |
| `human_approvals` | 无对应 Pydantic 模型，文档 22 详述，此处先建表占位 | `run_id, node_name, status` |

**JSONB 使用原则**：凡是"随该记录一起读写、不需要独立跨记录检索"的嵌套结构（如 `ExecutionTrace.actions`、`CapabilityTree.nodes`）用 JSONB 内嵌，不拆多表——这是 CI 评测系统的读写模式（写多读少、按整条记录读取）决定的，拆表反而增加 join 成本且没有实际收益。

## 4. Repository 层

统一收口所有对业务表的读写，`nodes/`、`agents/` 下的代码一律通过 Repository 存取，不直接写 SQL/ORM 查询语句：

```python
# src/skill_evaluate/persistence/repository.py
class TraceRepository:
    async def save(self, trace: ExecutionTrace) -> None: ...
    async def get(self, trace_id: str) -> ExecutionTrace | None: ...
    async def list_by_case(self, case_id: str) -> list[ExecutionTrace]: ...

class TestSuiteRepository:
    async def get_active_version(self, skill_id: str, skill_version_ref: str) -> TestSuiteVersion | None:
        """返回 None 时，调用方（06 文档 Generator）判定需要首次生成；
        版本存在但 skill_version_ref 不匹配时，视为 staleness，由 06 文档决定是否提示需要 force_regenerate。"""
    async def activate_new_version(self, version: TestSuiteVersion) -> None:
        """事务内：新版本写入 + 旧 active 版本置 false，保证第 3 节唯一索引不冲突。"""

class CapabilityRepository: ...
class JudgeRepository: ...
class SecurityFindingRepository: ...
class AssertionRepository: ...
```

所有 Repository 方法为 `async`，统一走 `sqlalchemy.ext.asyncio` + `psycopg` 异步驱动，与 LangGraph 节点本身的异步执行模型保持一致，避免同步 DB 调用阻塞事件循环。

## 5. 外部事件唤醒机制（接入文档 03 的待接入项）

**问题背景**（引用文档 03 第 10 节）：Hermes 执行是异步的（可能耗时数十秒到超时上限），LangGraph 节点在等待 Hermes Hook 回调期间不能傻等阻塞图执行，需要"挂起 - 外部事件唤醒"的模式。

**机制设计**：

1. `HermesBackend.execute()` 发起沙箱请求前，先在 `pending_hooks` 表插入一条记录 `(run_id, case_id, run_index, status="waiting", created_at)`，随后调用 LangGraph 的 `interrupt()`（LangGraph ≥0.2 提供的动态中断原语，而非仅编译期的 `interrupt_before` 静态列表）挂起当前节点执行分支，将控制权交还给驱动流水线的宿主进程（CI job 或常驻 worker）。
2. 文档 05 落地的 Hook HTTP 端点收到 Hermes 回调后：
   a. 按文档 03 第 4.3 节映射规则解析为 `ExecutionTrace`，写入 `execution_traces` 表；
   b. 将 `pending_hooks` 对应记录 `status` 置为 `resolved`；
   c. 调用 `graph.invoke(Command(resume=trace_id), config={"configurable": {"thread_id": thread_id}})`，唤醒对应节点继续执行——`resume` 载荷只传 `trace_id`（引用），节点内部再通过 `TraceRepository.get()` 取完整数据，与文档 02"`PipelineState` 只存引用"的原则保持一致。
3. **超时兜底**：一个独立的定时巡检任务（`scripts/pending_hooks_reaper.py`，本文档新增，供 CI 或常驻 worker 定期调用）扫描 `pending_hooks` 中 `status="waiting"` 且 `created_at` 超过 `wall_clock_timeout_s` 的记录，主动触发文档 03 第 4.4 节的"拉取兜底"逻辑，若仍无结果则以失败态 `ExecutionTrace`（`loaded_skill_md=False`，见文档 03 保守判定原则）resume 对应节点，避免图状态永久挂起。

**同一套机制供文档 22 复用**：人工审批场景（Optimizer 达到最大重试次数）与 Hermes 异步回调在本质上是同一种"外部事件唤醒挂起图"的模式，区别只在于唤醒来源（Webhook vs. 人工在审查工作台点击"批准"后调用的内部 API）。文档 22 会复用本节的 `interrupt()` + `Command(resume=...)` 机制，而不是另起一套挂起逻辑，因此本文档在 `persistence/` 中把"挂起-恢复"封装为通用工具函数：

```python
# src/skill_evaluate/persistence/suspension.py
async def suspend_and_wait(reason: str, wait_key: str) -> Any:
    """记录挂起原因，调用 langgraph interrupt()，返回值由外部 resume 时传入"""

async def resolve_suspension(wait_key: str, resume_payload: Any, thread_id: str) -> None:
    """由 Hook 端点（05）或人工审批 API（22）调用，唤醒对应挂起"""
```

`pending_hooks` 与 `human_approvals` 两张表本质上都是 `wait_key -> resume_payload` 的具体化，未来如有第三种外部事件源，复用同一模式新增一张表即可，不改动 `suspension.py` 的核心函数签名。

## 6. Checkpoint 恢复的幂等性保证

- **同一 `thread_id` 重复 `invoke`**：LangGraph 原生保证从最后一个 Checkpoint 继续，不重跑已完成节点，本文档不需要额外处理。
- **已落库但未 resume 的 `ExecutionTrace`**：若 CI Runner 在"Hook 端点已写库"和"resume 调用"之间被中断（罕见但可能），重启后的巡检任务（第 5 节第 3 点）需要额外扫描 `execution_traces` 中存在但对应 `pending_hooks` 仍为 `waiting` 的记录，直接触发 resume 而非重新拉取——避免对同一用例重复计费/重复调用 Hermes。
- **重复 Hook 回调（Hermes 网络重试导致同一 `run_id/case_id/run_index` 收到两次回调）**：Hook 端点写入前先检查 `pending_hooks.status`，非 `waiting` 状态的回调直接忽略并记录一条 warning 日志，保证 `execution_traces` 表不产生重复/覆盖写入（`(case_id, run_index)` 唯一约束兜底）。

## 7. 迁移与 Schema 版本管理

- 使用 `alembic`，迁移脚本纳入 git 版本控制（`persistence/migrations/versions/`），CI 流水线在评测运行前先执行 `alembic upgrade head`（挂载到文档 01 CLI 骨架的 `db_init` 命令，本文档正式实现该命令）。
- **本地开发**：`docker-compose.dev.yml` 启动的容器为空库，`skill-evaluate db_init` 负责建扩展（`CREATE EXTENSION vector`，文档 01 已声明）+ 跑 Alembic 迁移 + 调用 `saver.setup()`，三步合一，保证"拉起一个空容器到可跑评测"只需一条命令。
- 每份后续文档如需新表/新字段，新增一个 Alembic revision，不修改已合并的历史 revision（数据库迁移的黄金法则，避免多人协作时迁移链分叉）。

## 8. 待接入文档（本文档留给后续模块的接口清单）

| 预留位置 | 当前状态 | 由哪份文档接入 | 接入方式 |
|---|---|---|---|
| Hook HTTP 端点调用 `resolve_suspension` | 机制已定义，端点未落地 | `05` | 在 Hook 端点处理函数末尾调用 `resolve_suspension(wait_key=f"{run_id}:{case_id}:{run_index}", ...)` |
| `human_approvals` 表的写入/查询 API | 仅建表占位 | `22` | 实现审查工作台对接的 API，复用 `suspension.py` 的通用函数 |
| `interrupt_before` 编译期列表 | 空列表 | `09`（Optimizer 最大重试挂起点）、`22`（人工审批节点） | 在各自节点定义完成后，把节点名追加进 `graph/`（文档 24）编译调用处的列表 |
| `pending_hooks_reaper.py` 的调度方式 | 脚本已定义，未接入调度器 | `24` | CI 中以独立 job 或常驻 worker 的定时任务形式接入，具体调度平台在文档 24 CI/CD 落地时确定 |
| `models.py` 中 P0/P1/P2 权重、组合覆盖对等模块八字段 | 表已建（JSONB 内嵌），细化字段随 02 模型走 | `18` | 若模块八需要独立查询能力（如按权重筛选），届时新增索引而非新表 |

---

## 下一步

待你确认本文档后，我将输出 **文档 05：可观测性骨架：双写报告与 Langfuse 集成**——落地文档 03/04 共同遗留的 Hook HTTP 端点，并接入 `benchmark.json`/HTML/Langfuse 双写。
