# 实现说明：04 PostgresSaver 持久化与 Checkpoint 恢复机制

> 对应设计文档：`docs/dev/04_PostgresSaver持久化与Checkpoint恢复机制.md`
> 状态：已实现（图唤醒的最后一跳留桩，待 `docs/dev/24` 接入）

## 交付了什么

`src/skill_evaluate/persistence/`：

| 文件 | 内容 | 对应设计文档章节 |
|---|---|---|
| `checkpointer.py` | `build_checkpointer()`（`PostgresSaver` 接线）、`thread_id_for()` | 第 2 节 |
| `models.py` | 全部业务表的 SQLAlchemy ORM 模型 | 第 3 节 |
| `db.py` | 异步 engine/session 工厂（设计文档未单独拆文件，见下方说明） | 第 4 节前置依赖 |
| `repository.py` | 统一 Repository 层 | 第 4 节 |
| `suspension.py` | `suspend_and_wait()` / `resolve_suspension()` 挂起-唤醒通用机制 | 第 5 节 |
| `migrations/` | Alembic 环境 + 两个 revision | 第 3/7 节 |

`cli.py::db_init`（脚手架阶段建的文件，本模块正式实现其函数体）：建
`vector` 扩展（内嵌在 `0001_initial_schema` 迁移里）→ `alembic upgrade head`
→ `PostgresSaver.setup()`，三步合一，对应设计文档第 7 节"拉起一个空容器到
可跑评测只需一条命令"。

## 业务表落地情况

设计文档第 3 节列出的 11 张表全部建了（`skills`、`test_cases`、
`test_suite_versions`、`execution_traces`、`capability_trees`、
`judge_verdicts`、`consensus_results`、`security_findings`、
`assertion_specs`/`assertion_results`、`pending_hooks`、`human_approvals`），
索引/唯一约束与设计文档描述一致（含 `test_suite_versions` 的"同 skill 仅一条
`is_active=true`"部分唯一索引）。JSONB 内嵌原则（第 3 节末段）严格执行：
`ExecutionTrace.actions`、`CapabilityTree.nodes` 等嵌套结构均未拆多表。

## 与设计文档的差异 / 必要补充

| 差异点 | 类型 | 说明 |
|---|---|---|
| 新增 `runs` 表（`RunORM`/`RunRepository`） | 设计遗漏补齐 | `PipelineState.run_id`、`pending_hooks.run_id` 都以 run_id 为核心标识，但设计文档全篇没有一张"run 身份信息表"。`api/hooks_hermes.py` 需要按 `run_id` 反查 `skill_id` 才能拼出 `thread_id`（第 2 节 `thread_id_for(skill_id, run_id)`），05 文档的 `ReportGenerator.build(run_id)` 也需要同样的反查。以新增表方式补齐，符合第 7 节"新增表新增 revision，不改历史 revision"的约定，落在独立的 `0002_reporting_tables.py` 里 |
| 新增 `dimension_results` 表（`DimensionResultORM`/`DimensionResultRepository`） | 设计遗漏补齐 | 05 文档第 6 节明确要求"`ReportGenerator` 暴露 `record_dimension_result()` 写入方法"，但落盘表结构未在 04 文档列出。同样新增在 `0002_reporting_tables.py` | 
| `db.py` 独立成文件 | 结构性补充 | 设计文档把"异步 SQLAlchemy engine/session"归入第 4 节 Repository 层的前置说明，未单独列文件；实现时拆成独立 `db.py` 便于 `repository.py` 与未来的迁移脚本共用同一套 engine 工厂，不改变任何字段/接口语义 |
| `build_checkpointer()` 返回 context manager 而非裸对象 | 实现细节调整 | `PostgresSaver.from_conn_string()` 本身就是 context manager，设计文档代码片段里的 `return saver` 若直接照抄会导致连接未被正确关闭；改为 `@contextmanager` 包装，调用方写法从 `saver = build_checkpointer()` 变为 `with build_checkpointer() as saver:`，语义（"拿到一个已 `setup()` 过的 `PostgresSaver`"）不变 |

## 外部事件唤醒机制的落地程度

设计文档第 5 节的机制**全部落地**，但刻意在最后一跳留了一个协议化的缺口：

- `suspend_and_wait(reason, wait_key)`：调用 LangGraph `interrupt()`，已实现。
- `resolve_suspension(wait_key, resume_payload, thread_id)`：
  - 重复回调忽略逻辑（第 6 节）：内部依次尝试
    `PendingHookRepository.mark_resolved()` / `HumanApprovalRepository.mark_resolved()`，
    两者都返回 `False`（记录不存在或非 `waiting` 状态）时直接安全返回，
    **已实现**。
  - 唤醒对应挂起节点这一步：设计文档写的是"调用
    `graph.invoke(Command(resume=trace_id), config=...)`"，但"已编译的主图"
    要到 `docs/dev/24` 才存在。实现时抽象为 `GraphResumer` 协议 +
    `register_graph_resumer()` 注册点：**未注册时 `resolve_suspension()` 会
    显式抛 `ConfigurationError`**，不会静默吞掉唤醒失败。

  接入方式见 [`docs/dev/interfaces/04_graph_resumer.md`](../interfaces/04_graph_resumer.md)。

`scripts/pending_hooks_reaper.py`（第 5 节第 3 点"超时兜底"）逻辑已实现
（扫描 `pending_hooks` 中超时的 `waiting` 记录 → 构造保守失败态
`ExecutionTrace` → `resolve_suspension()` 唤醒），可手动运行，尚未接入任何
定时调度器，接入方式见
[`docs/dev/interfaces/04_pending_hooks_reaper_scheduling.md`](../interfaces/04_pending_hooks_reaper_scheduling.md)。

## 幂等性保证（第 6 节）落地情况

- 同一 `thread_id` 重复 `invoke`：由 LangGraph 原生保证，无需本模块处理。
- 重复 Hook 回调：`PendingHookRepository.mark_resolved()` 内部 `UPDATE ...
  WHERE status='waiting'`，非 `waiting` 状态的记录不会被二次改写，
  `execution_traces` 的 `(case_id, run_index)` 唯一约束兜底。
- "已落库但未 resume"的中断恢复：依赖第 5 节第 3 点的巡检任务，逻辑已在
  `pending_hooks_reaper.py` 中实现，尚未独立测试该具体分支（需要真实
  Postgres，见下方"验证"）。

## 如何验证

无法在当前沙箱环境验证的部分（无 Docker，无法起 Postgres）：

- `PostgresSaver.setup()` 真实建表
- Alembic 迁移的实际执行（`alembic upgrade head`）
- Repository 层的读写往返

已验证的部分：

```bash
python -c "import skill_evaluate.persistence.models, skill_evaluate.persistence.repository, skill_evaluate.persistence.checkpointer, skill_evaluate.persistence.suspension"
mypy src   # 严格类型检查，ORM 字段类型、Repository 方法签名全部通过
```

`0001_initial_schema.py`/`0002_reporting_tables.py` 与 `models.py` 的字段
逐一人工比对一致；生产环境接入前建议先在真实 Postgres 上跑一次
`skill-evaluate db_init` 做集成验证。

## 待接入 / 下一步

- [`docs/dev/interfaces/04_graph_resumer.md`](../interfaces/04_graph_resumer.md)：`docs/dev/24` 主图编译完成后注册 `GraphResumer`。
- [`docs/dev/interfaces/04_pending_hooks_reaper_scheduling.md`](../interfaces/04_pending_hooks_reaper_scheduling.md)：`docs/dev/24` 接入定时调度。
- `docs/dev/22`：`human_approvals` 表已建，`HumanApprovalRepository` 已实现，等待人工审批 API 接入（见 05 模块 README）。
