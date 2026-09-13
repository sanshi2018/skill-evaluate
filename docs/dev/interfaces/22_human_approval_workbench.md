# 接入文档：统一人工审批闭环、审查工作台 API 与通知通道（docs/dev/22 留给后续模块的接口）

> 由谁接入：`24`（主图装配套 guard、注册 GraphResumer、状态 schema 合并、进程启动注册通知通道、
> CI Secrets）、**前端/运维侧**（审查工作台消费第 4 节 API、Discord Webhook、黄金用例维护）。
> 当前状态：统一决策模型、两张新表、挂起入口、节点级 guard、深度冲突闸门、Discord 卡片/告警通道、
> 工作台 API 与 HMAC 回调全部落地，有测试覆盖（`tests/skill_evaluate/test_approval.py` 52 条，
> 含一条真实 LangGraph `interrupt()` → 决策 API → 唤醒的端到端用例；不碰库、不发真实 Webhook）。
> 迁移：`0010_approval_workbench`（`pending_approvals` / `approval_decisions`）。

---

## 0. 三十秒上手

```python
# A. 主图装配（docs/dev/24）：用代理 builder 给所有维度节点套上审批 guard
from skill_evaluate.nodes.approval_guard import ApprovalGuardedBuilder
from skill_evaluate.persistence.suspension import register_graph_resumer

guarded = ApprovalGuardedBuilder(builder)
add_preflight_nodes(guarded)
add_trigger_accuracy_nodes(guarded)          # 各维度 add_*_nodes 一行不改
...
graph = guarded.builder.compile(checkpointer=...)
register_graph_resumer(CompiledGraphResumer(graph))   # 见 interfaces/04_graph_resumer.md

# B. 任何新的阻塞式挂起点
from skill_evaluate.persistence.approval_service import request_human_approval
decision = await request_human_approval(
    run_id, wait_key, ApprovalDecisionType.XXX, "给人看的摘要", {"证据引用": "..."},
    node_name="dim.node", skill_id=skill_id,
)
```

```bash
skill-evaluate db-init                                    # 跑到迁移 0010
SKILLEVAL_APPROVAL_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
SKILLEVAL_APPROVAL_WORKBENCH_BASE_URL=https://workbench.internal
SKILLEVAL_APPROVAL_HMAC_SECRET=...                        # 仅外部系统走 /hooks/approval 时需要
uvicorn skill_evaluate.api.app:app                        # lifespan 自动注册 Discord 通道
```

---

## 1. 六处人工介入场景的最终落点

| 场景 | 触发位置 | decision_type | 阻塞 | wait_key | 继续的 outcome |
|---|---|---|---|---|---|
| Optimizer 最大重试（09） | `OptimizationLoop._suspend` | `ACCEPT_PATCH` | ✅ | `{run_id}:optimizer:{role}` | `adopt` |
| 能力树规模（16） | `coverage.extract_capability_tree` | `CONFIRM_TREE_REVIEW` | ✅ | `{run_id}:coverage:tree_review` | `confirm` |
| Judge 冻结（08） | guard 捕获 `JudgeFrozenError` | `UNFREEZE_JUDGE` | ✅ | `{run_id}:{node}:judge_frozen:{round}` | `unfreeze`（API 先解冻再唤醒） |
| 连续坍塌（21） | guard 捕获 `GenerationCollapseError(requires_human_seed=True)` | `INJECT_NEW_SEED` | ✅ | `{run_id}:{node}:generation_collapse:{round}` | `seed_injected` |
| 共识未达成等（11~20） | guard 捕获其余 `PipelineSuspended` | `ABANDON_RUN` | ✅ | `{run_id}:{node}:suspended:{round}` | `retry` |
| 深度冲突-基石熔断（20） | `multi_skill.deep_conflict_approval_gate` | `RESOLVE_DEEP_CONFLICT` | ✅ | `{run_id}:multi_skill:deep_conflict` | `acknowledge` |
| 深度冲突-仅软性（20） | 同上 | `RESOLVE_DEEP_CONFLICT` | ❌ 通知 | 同上 | `acknowledge`（仅记录） |
| 前置门禁故障（21） | guard 捕获 `InfrastructureEnvironmentError` | `ABANDON_RUN` | ❌ 通知 | `{run_id}:{node}:infrastructure:{gate}` | `acknowledge`（仅记录，异常照常上抛） |
| 孤儿用例（17） | `pruning.orphan_case_detection` | —（`test_case_suggestions`） | ❌ 队列 | — | `confirmed` → 用例 `split=COLD` |

每张阻塞卡片**都有否定选项**（`abandon` / `reject`），选了之后节点抛 `HumanRejectedSuspension`
（`PipelineSuspended` 子类），guard 原样放行，流水线停下。

### 1.1 ⚠️ 不会升级为审批的 `GenerationCollapseError`

`coverage.feedback_driven_generation`、`pruning` 组合补题、`weighted_coverage` 约束补题、
`multi_skill.prepare_multi_skill_context` 这四处**自己接住** `GenerationError`（补盲/补题耗尽，
维度仍能出结论）。按项目级判据（"不等人能否继续产出有意义的结果"），它们不挂起；人仍会收到
docs/dev/21 的 `generation_collapse_persistent` 告警卡片。只有**冒出节点**的坍塌（典型是各维度
`prepare_test_suite` 调 `ensure_test_suite()`）才进入 `INJECT_NEW_SEED` 审批。

---

## 2. `24`：主图装配清单

1. **套 guard**：所有维度节点经 `ApprovalGuardedBuilder(builder)` 添加（第 0 节）。不套的后果：
   `JudgeFrozenError` / 共识未达成直接让 `ainvoke` 抛异常结束，工作台上**没有卡片**，人不知道要处理什么。
   子图作为单个 Runnable 加入主图时 guard 不生效（异常发生在子图内部节点），请平铺装配。
2. **注册 GraphResumer**：未注册时 `POST /api/approvals/{id}/decide` 对阻塞卡片返回 **503**，
   且**不写任何状态**（人可以等主图就绪后重新点）。
3. **状态 schema**：并入 `MultiSkillState` 的两个新私有键
   `_multi_skill_deep_conflict_alert`（finalize 写）/ `_multi_skill_deep_conflict_resolution`（闸门写）。
   漏了 → 闸门读不到 payload → 永远放行，基石熔断不会挂起。
4. **模块十的终点变了**：`nodes.multi_skill.TERMINAL_NODE` 现为
   `multi_skill.deep_conflict_approval_gate`。docs/dev/24 正文里的 `MULTI_SKILL_TERMINAL` 请用这个常量，
   不要写死 `multi_skill.finalize_dimension_report`。
5. **thread_id 口径**：全部挂起点统一为 `f"{skill_id}:{run_id}"`（`persistence/checkpointer.py::thread_id_for`）。
   主图 `ainvoke(config={"configurable": {"thread_id": thread_id_for(...)}})` 必须用同一口径。
6. **`runs` 记录**：挂起点未提供 skill_id 时服务会查 `runs` 表解析 thread_id；查不到回落 run_id 并记
   `approval_thread_id_fallback_to_run_id` 警告——主图入口先 `RunRepository.create()`（与 Hermes 同一前提）。
7. **进程启动注册通知通道**：API 进程已在 lifespan 里、CLI `generate` / `generate-attacks` 已在命令入口调用 `configure_notification_channels()`；
   **CLI `run` 入口（图执行进程）也要调用一次**，否则挂起时的 Discord 卡片只写日志。
8. **`interrupt_before`**：本文档新增的挂起点全部是动态 `interrupt()`，**不要**把闸门/guard 加进静态列表
   （静态中断会让每次运行都停在闸门前）。
9. **唤醒在请求内同步执行**：`decide` → `resolve_suspension()` → `GraphResumer.resume()` 会在 HTTP 请求里继续
   跑图直到下一个挂起点或结束，与 `hooks_hermes.py` 一致。若评测耗时超过网关超时，请在
   `CompiledGraphResumer.resume()` 里改为投递到后台任务/队列（接口不变）。卡片在唤醒前已标记 resolved。

### 2.1 CI Secrets（补充 docs/dev/24 第 7 节检查清单）

`SKILLEVAL_APPROVAL_DISCORD_WEBHOOK_URL`（凭据，SecretStr）、`SKILLEVAL_APPROVAL_HMAC_SECRET`、
`SKILLEVAL_APPROVAL_WORKBENCH_BASE_URL`（非密）。`MAX_APPROVAL_ROUNDS_PER_NODE` 默认 3。

---

## 3. guard 的重试语义（写新节点时要知道的）

- 恢复时节点**整体重跑**：人批准 `retry`/`unfreeze`/`seed_injected` 后，节点内层逻辑会重新执行；若问题依旧，
  前面各轮 `interrupt()` 直接返回历史决定，本轮发起新卡片（wait_key 的 round 递增）。
- **每次唤醒都会重放此前每一轮的内层逻辑**（LLM/沙箱调用）。节点应当幂等（按 upsert 落库），
  这是全项目动态挂起的既有前提。
- 轮数达到 `max_approval_rounds_per_node` 后原异常直接上抛。
- guard 需要 `state["run_id"]`；缺失时不挂起、原样抛出（记 `approval_guard_giving_up`）。
- 新节点里若"人已明确拒绝"后要停下，请抛 `HumanRejectedSuspension` 而不是裸 `PipelineSuspended`，
  否则 guard 会再发一张"要不要放弃"的卡片。

---

## 4. 审查工作台 API（前端消费的契约）

所有端点**无用户鉴权**（部署在内网/SSO 网关后，docs/dev/22 第 5 节）。

### 4.1 `GET /api/approvals?status=pending&run_id=`

`status`：`pending`（默认）/ `resolved` / `all`。返回 `PendingApproval[]`，按创建时间倒序：

```jsonc
{
  "approval_id": "uuid5(wait_key)", "run_id": "...", "wait_key": "...",
  "decision_type": "accept_patch", "context_summary": "给人看的摘要",
  "context_ref": {"patch_id": "..."}, "node_name": "optimizer:prompt_engineer",
  "thread_id": "skill:run", "blocking": true, "status": "pending",
  "created_at": "ISO-8601", "resolved_at": null
}
```

`blocking=false` 的卡片是**通知**：流水线没有在等，按钮只显示"已知悉"。

### 4.2 `GET /api/approvals/{approval_id}/context`

公共字段：`approval`、`allowed_outcomes`（**按它渲染按钮**，不要在前端另存一份）、`run`、
`decision`（已处理时）、`expansion_errors`（单项证据加载失败的说明，视图其余部分照常展示）。
按类型追加：

| decision_type | 追加字段 |
|---|---|
| `accept_patch` | `patch`（diff/rationale/target_path）、`patch_application_result`、`retry_counts` |
| `unfreeze_judge` | `judge_health`、`recent_golden_misses`（新→旧 bool 序列） |
| `confirm_tree_review` | `capability_tree`（逐条 evidence_quote 在日志 `analyzer_capability_extracted`） |
| `resolve_deep_conflict` | `dimension_results`、`trace_comparisons[]`：`{case_id, scenario, solo, crowded}` |
| `inject_new_seed` | `collapse_events`（最近 10 次） |
| `abandon_run` | `dimension_results`；`approval.context_ref` 里有 `error` / `gate` / `details` |

⚠️ `trace_comparisons` 按 `(case_id, run_index)` 取**最近一次** Trace（该表不带 run_id），展示时请带上
`started_at`。场景名与号段见 `api/approval_context.py::MULTI_SKILL_TRACE_PAIRS`
（与 `interfaces/20` 第 4.4 节同表）。基石熔断的核心 Skill 用例不在 `case_ids` 里，需按
`judge_verdicts.subject_id` 前缀 `multi_skill_core:` 回查。

### 4.3 `POST /api/approvals/{approval_id}/decide`

```json
{"decided_by": "alice@example.com", "outcome": "adopt", "note": "看过 diff"}
```

| 状态码 | 含义 | 是否写入状态 |
|---|---|---|
| 200 | `{"status": "resolved", "approval_id", "resumed": bool}` | 是 |
| 404 | 卡片不存在 | 否 |
| 409 | 已处理 / 并发决策输了 | 否（例外：解冻卡片的并发输家可能已执行过幂等的解冻） |
| 422 | outcome 不在 `allowed_outcomes`；孤儿建议走错端点；解冻卡片缺 model | 否 |
| 503 | 阻塞卡片但主图 GraphResumer 未注册 | 否 |
| 500 | 唤醒过程中图执行报错（卡片与决定**已**落库，见下） | 是 |

⚠️ 500 的情形：`resolve_suspension()` 已把账本迁到 resolved 后图在继续执行中报错——这是图本身的故障，
不是审批故障，重复点击不会再次唤醒；按运行记录排查（`thread_id` 在卡片上）。

### 4.4 `GET /api/suggestions?status=pending` / `POST /api/suggestions/{id}/decide`

body `{"decided_by", "outcome": "confirmed"|"rejected", "note"}`。`confirmed` 时对
`ORPHAN_RETIREMENT` 执行 `TestCaseRepository.retire(case_id)`：`split → COLD`（**归档不删除**，
历史 Trace/判定仍可回查，按 TRAIN/VALIDATION 取题的维度不再看到它）。返回 `case_retired`。
新增 `SuggestionType` 未登记确认动作时返回 **501**（`SuggestionDecisionHandler._apply_confirmed`）。

### 4.5 `POST /hooks/approval/{run_id}/{node_name}`（机器对机器）

body 同 4.3；`X-Approval-Signature: hex(HMAC-SHA256(secret, raw_body))`。定位该节点**最新**一张 pending 卡片后
走同一套决策逻辑。未配置 `SKILLEVAL_APPROVAL_HMAC_SECRET` 时一律 401。

---

## 5. 通知通道

| 通道 | 默认（未配置 Webhook） | 配置后 |
|---|---|---|
| 审批卡片 `get_approval_notifier()` | `LoggingApprovalNotifier`（日志 `approval_card_dispatched`） | `DiscordApprovalNotifier` |
| 告警 `get_alert_dispatcher()` | `LoggingAlertDispatcher`（日志 `alert_dispatched`） | `DiscordAlertDispatcher` |

已接入的告警类型：`judge_frozen`（08，`run_id="platform:judge_health"`，卡片不带工作台链接）、
`deep_multi_skill_conflict`（20）、`generation_collapse_persistent`（21）。新增告警类型只需调用
`dispatch_alert()`；想要中文标题/颜色，在 `discord_notifier._ALERT_TITLES/_ALERT_COLORS` 登记（不登记也照发）。

卡片只在卡片**首次**落库时发送（节点重跑不重复轰炸）；通知失败只记 `approval_notification_failed`，
不影响挂起。所有 Discord payload 带 `allowed_mentions={"parse": []}`（摘要里可能有不受信文本）。

---

## 6. 运维侧：黄金用例维护指引（docs/dev/08 第 9 节遗留）

- **补录入口**：`GoldenCaseRepository().save(GoldenCase(...))`（`interfaces/08` 第 3 节有完整示例）。
  工作台当前**没有**黄金用例录入界面——那是一个独立的数据标注需求，本项目只定义消费契约。
- **何时补**：收到 `judge_frozen` 告警后，先看 `recent_golden_misses` 判断是 Judge Prompt 退化还是黄金用例
  本身标注过时；后者应修正/停用（`active=false`）对应黄金用例，而不是直接解冻。
- **解冻不清历史**：`unfreeze` 后窗口里的旧失误仍计入下一次 `check()`；若补录/修正黄金用例后仍很快再次冻结，
  说明问题没解决，不要反复解冻。

---

## 7. 数据库

`0010_approval_workbench`：`pending_approvals`（`wait_key` 唯一、`status` 索引）、`approval_decisions`
（`approval_id` 唯一 + 外键）。**`human_approvals` 保留**作挂起账本（分层见 `state/approval.py` 模块头），
阻塞卡片两表都写、非阻塞卡片只写 `pending_approvals`。

---

## 8. 本次对前序模块的修改（追加式，均向后兼容）

| 位置 | 修改 |
|---|---|
| `errors.py` | 新增 `HumanRejectedSuspension(PipelineSuspended)`；`JudgeFrozenError` 追加可选 `model` / `temperature` |
| `config.py` | 新增 `ApprovalSettings`，挂 `Settings.approval` |
| `agents/judge/health.py` | 冻结时经 `dispatch_alert` 发 `judge_frozen`；`ensure_not_frozen` 抛错带 model/temperature；构造参数 `alert_dispatcher` |
| `agents/optimizer/loop.py` | 挂起改走 `ApprovalService`（`ACCEPT_PATCH` 卡片）；新增构造参数 `approval_service`；**thread_id 默认改为 `skill_id:run_id`** |
| `nodes/coverage/{deps,nodes}.py` | 同上（`CONFIRM_TREE_REVIEW`）；`CoverageDeps.approval_service`；未确认改抛 `HumanRejectedSuspension` |
| `nodes/{trigger_accuracy,instruction_control,security}/nodes.py` | 人工放弃补丁后改抛 `HumanRejectedSuspension` |
| `nodes/multi_skill/*` | finalize 追加写 `_multi_skill_deep_conflict_alert`；新增节点 `deep_conflict_approval_gate` 并成为 `TERMINAL_NODE`；`MultiSkillDeps.approval_service` |
| `persistence/repository.py` | 新增 `PendingApprovalRepository` / `ApprovalDecisionRepository`；`TestCaseRepository.retire`、`TestCaseSuggestionRepository.get`、`PatchRepository.get_application_result`、`GenerationCollapseEventRepository.list_recent` |
| `persistence/suspension.py` | 新增 `is_graph_resumer_registered()` |
| `api/hooks_approval.py` / `api/app.py` | 501 桩替换为完整实现；lifespan 注册通知通道 |
| `cli.py` | `generate` / `generate-attacks` 入口注册通知通道（`run` 仍由 `24` 接入） |

---

## 9. 留给后续的接入点

| 预留位置 | 当前状态 | 由谁接入 | 接入方式 |
|---|---|---|---|
| 主图套 guard / 注册 resumer / 合并 schema | 构件就位 | `24` | 第 2 节 |
| 图执行进程注册通知通道 | API 进程已注册 | `24` | CLI `run` 入口调用 `configure_notification_channels()` |
| 长耗时唤醒改后台执行 | 请求内同步 | `24` | 替换 `CompiledGraphResumer.resume()` 实现 |
| 审查工作台前端 | 明确排除在本仓库之外 | 前端/运维 | 第 4 节 |
| 用户鉴权 / 权限 | 明确排除 | 运维（SSO/网关） | — |
| Discord 按钮式快捷审批 | 未实现（判定决策应在工作台完成） | 视产品需求 | 扩展 Discord Interaction 回调后调用 `ApprovalDecisionHandler.decide_approval()`，不改第 4.3 节契约 |
| 共识未达成的"人工直接给出判决" | 只支持 retry/abandon | 后续迭代 | 需要节点侧能读取人工注入的判决并归档为 `JudgeVerdict`；当前重试依赖 Judge 再次投票 |
| 门控打回原因回灌 Optimizer（`interfaces/19` 第 4 节已知限制 1） | 未做 | docs/dev/09 修订 | 与审批无关，保持原状 |
| 黄金用例录入界面 | 无 | 数据标注工具 | 第 6 节 |
