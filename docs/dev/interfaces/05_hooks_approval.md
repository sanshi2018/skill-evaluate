# 接入文档：人工审批回调端点（hooks_approval.py）

> 由谁接入：docs/dev/22（容错机制与人工审批闭环）。
> 当前状态：`src/skill_evaluate/api/hooks_approval.py` 里的
> `POST /hooks/approval/{run_id}/{node_name}` 恒返回 `501 Not Implemented`。

## 已就绪的基础设施（22 可直接复用，不必重新设计）

- `persistence/models.py::HumanApprovalORM` 表已建（`human_approvals`），字段：
  `run_id`, `node_name`, `thread_id`, `wait_key`, `status`, `resume_payload`,
  `created_at`, `updated_at`。
- `persistence/repository.py::HumanApprovalRepository`：`create()` /
  `mark_resolved()` 已实现，用法与 `PendingHookRepository` 完全对称。
- `persistence/suspension.py::suspend_and_wait()` / `resolve_suspension()`：
  通用挂起-唤醒机制，`resolve_suspension()` 内部会自动尝试
  `PendingHookRepository` 与 `HumanApprovalRepository` 两张表，接入时无需
  改动本文件。
- `api/security.py::verify_hmac_signature()`：签名校验函数已通用化（原文档
  命名 `verify_hermes_signature` 仍保留为别名），22 只需新增一个独立的
  secret 配置项（如 `ApprovalSettings.hmac_secret`，追加到 `config.py`）。

## 接入方式

1. 在 `config.py` 新增 `ApprovalSettings`（追加式扩展，参考 `ExecutorSettings`
   的写法），挂到 `Settings.approval`。
2. 在 `hooks_approval.py::approval_hook()` 内：
   - 用 `verify_hmac_signature()` 校验请求签名。
   - 解析审查工作台（Base44 等）回传的 payload（批准/驳回、修改后的
     `SKILL.md` 差异、审批人等）。
   - 调用 `HumanApprovalRepository().mark_resolved(wait_key, resume_payload)`
     或直接使用 `resolve_suspension()`（推荐：与 `hooks_hermes.py` 保持同构）。
3. 挂起侧（`nodes/` 或 `agents/optimizer` 达到最大重试次数时）调用
   `HumanApprovalRepository().create(...)` 落库等待记录，再调用
   `suspend_and_wait(reason=..., wait_key=...)` 挂起，与
   `executors/hermes_backend.py::HermesBackend.execute()` 的写法完全对称，
   可直接参照。
4. 前置依赖：docs/dev/interfaces/04_graph_resumer.md 必须先完成。
