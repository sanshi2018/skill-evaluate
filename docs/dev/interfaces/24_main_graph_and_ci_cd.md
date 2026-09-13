# 接入文档：主图编排、运行入口与 CI/CD（docs/dev/24 实施后的最终形态）

> 由谁接入：**任何新增评测维度 / 新挂起点 / 新交付动作的后续开发**（本文档是装配点的操作手册）、
> **运维侧**（CI Secrets、常驻 Postgres、自托管 runner、API 进程部署）。
> 当前状态：主图装配、GraphResumer、CLI `run` 与 `internal *` 子命令、补丁转 PR、Nightly COLD 回归、
> 数据飞轮收尾节点、四个 GitHub Actions workflow 全部落地；有测试覆盖
> （`tests/skill_evaluate/test_main_graph.py` 42 条，不碰库、不起 git/gh、不发真实请求）。
> 迁移：`0012_run_pr_url`（`runs.pr_url`，纯追加 nullable）。
> 00~23 各接入文档里指向 `24` 的待接入项已全部收口（逐条对照见第 9 节）。

---

## 0. 三十秒上手

```bash
skill-evaluate db-init                                   # 迁移到 0012 + checkpoint 表
uvicorn skill_evaluate.api.app:app                       # lifespan：注册通知通道 + 装配主图 + 注册 GraphResumer
skill-evaluate run --skill-path skills/csv-cleaner       # 发起评测并等待跑完（退出码见第 4 节）
skill-evaluate run --skill-path skills/csv-cleaner --run-id <id>   # 续跑 / 重新观察
skill-evaluate internal run-cold-suite --skill-path skills/csv-cleaner   # Nightly COLD 回归
skill-evaluate internal reap-pending-hooks               # 超时回调巡检
skill-evaluate internal judge-health-check               # Judge 黄金基准巡检
skill-evaluate internal changed-skills --base <sha> --head <sha>          # CI：解析改动的 Skill
```

```python
from skill_evaluate.graph.main import MainGraphDeps, build_main_graph, build_main_graph_builder
from skill_evaluate.graph.resumer import CompiledGraphResumer
from skill_evaluate.persistence.checkpointer import build_async_checkpointer
from skill_evaluate.persistence.suspension import register_graph_resumer

async with build_async_checkpointer() as saver:          # ⚠️ 异步版，见第 5.1 节
    graph = build_main_graph(saver)                       # 或 build_main_graph(saver, MainGraphDeps(...))
    register_graph_resumer(CompiledGraphResumer(graph))
```

---

## 1. 最终拓扑

```
pipeline.bootstrap_run                     Skill 入库 + runs 记录 + 顶层 Langfuse trace（+ --force-regenerate 出题）
  → preflight.sandbox_fingerprint_gate → preflight.canary_probe_gate                     （21）
  → route_after_preflight
      ├─ _pipeline_mode=cold_suite → nightly.cold_suite_regression ─────────────┐
      └─ full（并行扇出）                                                        │
          ├─ context_scoping.*                                         （12）     │
          ├─ script_usability.*                                        （14）     │
          └─ trigger_accuracy.prepare_test_suite                       （11）     │
               ├─ trigger_accuracy.* → finalize → cross_model.*       （19）     │
               └─ security.prepare_adversarial_suite                   （15）     │
                    ├─ security.*                                                 │
                    └─ ⋈ trigger_accuracy.judge_train_cases                       │
                         → instruction_control.prepare_cases           （13）     │
                              ├─ instruction_control.*                            │
                              └─ coverage.*(16) → pruning(17) → weighted(18) → multi_skill.*(20)
  ⋈ 七个维度终节点（同步屏障）                                                    │
  → finalize.report ←────────────────────────────────────────────────────────────┘
  → finalize.patch_pr → finalize.rag_archive
```

常量（都在 `graph/main.py`，**不要写字面量**）：

| 常量 | 含义 |
|---|---|
| `PHASE_A_ENTRY_NODES` | 前置门禁后直接扇出的三个入口 |
| `DIMENSION_TERMINAL_NODES` | `finalize.report` 的汇合前驱（模块十用 `multi_skill.TERMINAL_NODE`） |
| `SUSPENDABLE_NODES` | 各维度 `INTERRUPT_BEFORE_NODES` 的并集（**只作编译期显式说明**） |
| `INTERRUPT_BEFORE_NODES` | 实际传给 `compile()` 的列表：**恒为空**（第 3 节） |

### 1.1 四条装配期决策（与 docs/dev/24 正文伪码的出入，正文已修订）

1. **改用例集的准备节点串行化**：模块一 → 模块五 → 模块三 → 模块六。它们都会调
   `ensure_test_suite()`，并行时从没出过题的 Skill 会被两边**同时出题**，后激活的版本把先激活那批用例
   从 active 版本里丢掉（数据库 lost update，不报错）。只串行"准备节点"，各维度后续的执行/探测照常并行。
2. **汇合一律用多起点边** `builder.add_edge([...], target)`。逐条 `add_edge` 会让目标节点**每个前驱完成
   时各跑一次**。实施期据此修复了模块三的一处真实缺陷（第 8 节）。
3. **模块九排在模块一终节点之后、模块三同时等模块一训练集判定**（interfaces/19 第 0 节、13 第 3.1 节）。
4. **`active_suite_version_id` 在主图 schema 里换成"非 None 后写者胜"的 reducer**
   （`graph/state.py::keep_latest_suite_version`）：同一超步两个节点回写同一个版本号时，默认的
   `LastValue` 通道会直接抛 `InvalidUpdateError`。

---

## 2. 新增一个评测维度要做的事（后续开发照此办理）

1. 维度包照 12~20 的骨架导出 `ENTRY_NODE` / `TERMINAL_NODE` / `NODE_NAMES` / `INTERRUPT_BEFORE_NODES` /
   `XxxState` / `add_xxx_nodes(builder, deps)`；私有键带维度前缀。
2. `graph/state.py`：`MainGraphState` 多继承加上 `XxxState`，并把它追加进 `DIMENSION_STATE_TYPES`
   （测试 `test_main_schema_contains_every_dimension_private_key` / `..._never_collide` 会守住）。
3. `graph/main.py`：
   - `MainGraphDeps` 加一个字段；`build_main_graph_builder()` 里 `add_xxx_nodes(graph, deps.xxx)`；
   - 连入口边：**会改用例集的准备节点必须串进第 1.1 节第 1 条的链**；只读用例集的挂在对应前驱之后；
   - 终节点加进 `DIMENSION_TERMINAL_NODES`（或串在某个已在列表里的终节点之前）；
   - `SUSPENDABLE_NODES` 追加 `*xxx.INTERRUPT_BEFORE_NODES`。
4. 多前驱汇合用多起点边；跑一遍 `test_full_mode_runs_each_node_once_and_report_waits_for_all_terminals`，
   它用真实边 + 替身节点执行整张图，任何节点跑两次都会失败。
5. 维度要产出补丁并希望转 PR：在 `graph/patch_pr.py::PATCH_SOURCES` 登记 `(维度, patch_id 键, 工作副本键)`，
   顺序即冲突优先级。
6. 维度结论是覆盖率类分数：在 `observability/report_generator.py::COVERAGE_DIMENSIONS` 登记。

**不要**把维度编成子图作为单个 Runnable 加入主图：`ApprovalGuardedBuilder` 只在平铺 `add_node` 时生效
（interfaces/22 第 2 节第 1 条）。

---

## 3. 挂起：只有动态 `interrupt()`，编译期列表恒为空

docs/dev/24 正文第 3 节与 interfaces/11、13、15、16 曾写"把优化闭环 / 能力树审核节点列进
`interrupt_before`，只是为了编译期显式、不影响挂起"。**这与 LangGraph 语义不符**：静态
`interrupt_before` 会让**每次运行**在进入这些节点前无条件停下，且这种停顿不写审批卡片——流水线会
永远停在模块六门口。因此：

- `compile(interrupt_before=INTERRUPT_BEFORE_NODES)`，`INTERRUPT_BEFORE_NODES == []`；
- "哪些节点可能停在人工审批上"由 `SUSPENDABLE_NODES` 表达（测试断言它包含四个节点）；
- 各维度的 `INTERRUPT_BEFORE_NODES` 常量保留（它们现在是 `SUSPENDABLE_NODES` 的数据源），语义按本节理解。

所有挂起点（Hermes/Llama Hook、Optimizer 耗尽、能力树规模、guard 接住的 Judge 冻结/坍塌/共识未达成、
深度冲突闸门）都经 `suspend_and_wait()`，中断载荷形如 `{"reason": ..., "wait_key": ...}`。
**新增挂起点必须带 `wait_key`**：`CompiledGraphResumer` 靠它在多个并行中断里定位要唤醒的那一个。

---

## 4. 运行入口与退出码（`graph/runner.py`）

跨进程模型：CLI 是"发起者 + 观察者"，Hook 回调与审批决策打到 API 进程，由那里的主图实例继续跑；
CLI 轮询同一 thread 的 checkpoint，结束后**在自己的工作目录**从库重写报告。

| 码 | 含义 | CI 处理 |
|---|---|---|
| 0 | 结束，无阻断项 | 通过 |
| 1 | 结束，存在 `blocking=True ∧ status=FAIL` | 阻断合并 |
| 2 | 评测系统自身错误（`SkillEvaluateError`） | 查运维 |
| 3 | 等待超时，仍挂起（`pending_wait_keys` 列出卡在哪） | 去工作台处理后 `--run-id` 重新观察 |
| 4 | 被停下：`HumanRejectedSuspension` / `InfrastructureEnvironmentError`，或别的进程里节点报错 | 看 `stop_reason` |

`--run-id` 语义：线程已结束 → 不重跑直接返回；线程停在断点且无待处理中断 → `ainvoke(None)` 续跑；
线程停在中断上 → 只观察；线程不存在 → 以该 id 新开。

`bootstrap_run` 会核对磁盘上的 Skill 身份与运行入参一致（`skill_id` / `version_ref`），评测期间改动
被测 Skill 会以 `ConfigurationError` 失败——请以新 run 重跑。

---

## 5. 进程级接线

### 5.1 checkpointer：必须用异步版

`persistence/checkpointer.py::build_async_checkpointer()`（`AsyncPostgresSaver`）。同步 `PostgresSaver`
不实现 `aget_tuple/aput`，挂在 `ainvoke()` 上首次写 checkpoint 就会 `NotImplementedError`。两者写同一套
表，`db-init` 仍用同步版建表。

### 5.2 GraphResumer（`graph/resumer.py::CompiledGraphResumer`）

| 进程 | 注册位置 |
|---|---|
| API（uvicorn） | `api/app.py` lifespan，开关 `SKILLEVAL_PIPELINE_REGISTER_RESUMER_IN_API`（默认 true；数据库不可达时**启动即失败**） |
| CLI `run` / `internal run-cold-suite` | `graph/runner.py::run_pipeline()` |
| `internal reap-pending-hooks` / `scripts/pending_hooks_reaper.py` | `graph/runner.py::reap_pending_hooks_with_graph()` |

- `GraphResumer.resume()` 追加了可选关键字参数 `wait_key`（`resolve_suspension()` 已传入）；自定义实现请
  接受该参数。
- 同一 thread 的唤醒在进程内串行（`asyncio.Lock`）；API 多副本部署时跨进程并发仍存在，届时把 `resume()`
  改成投递队列（接口不变）。
- `SKILLEVAL_PIPELINE_RESUME_IN_BACKGROUND=true`：唤醒投递为后台任务立即返回（网关超时短于维度耗时的
  部署用），代价是图报错只进日志 `graph_resume_failed`，决策 API 不再返回 500。

### 5.3 通知通道

API lifespan、CLI `run` / `run-cold-suite` / `generate*` / 两个巡检子命令都调用
`configure_notification_channels()`（interfaces/22 第 2 节第 7 条）。

---

## 6. 收尾三节点

| 节点 | 失败语义 | 写入状态 |
|---|---|---|
| `finalize.report` | **会**让流水线失败（报告就是产品） | `_pipeline_report_paths` / `_overall_status` / `_blocking`、`final_report_ref` |
| `finalize.patch_pr` | 从不失败，结果进报告尾部 | `_pipeline_pull_request`（`PullRequestOutcome`） |
| `finalize.rag_archive` | 从不失败；归档后重写一次报告补上 PR 与归档结果 | `_pipeline_archive_outcome` |

报告新增字段（`BenchmarkReport`，全部可选）：`preflight_summary`（头部：指纹 / 金丝雀摘要）、
`pull_request`、`archive_outcome`；`coverage_summary` 从 `capability_coverage` / `test_suite_health` /
`weighted_coverage` 三个维度的 `score` 聚合（`score=None` 不写）。staleness 告警按
模块一 → 五 → 三 → 十 的顺序取第一条非空。

### 6.1 补丁转 PR（`graph/patch_pr.py`）

- **来源**：状态里的 `_sec_applied_patch_id` / `_applied_patch_id` / `_ic_applied_patch_id`（按此顺序合入，
  即冲突优先级），到 `patch_application_results` 核对 `applied=True`。`regression_passed` 不为 True 的只可能
  是人工在 ACCEPT_PATCH 卡片上 adopt 的，照常进 PR 但正文醒目标注。
- **合成**：不 `git apply patch.diff`（description/正文补丁 diff 的不是文件，且闭环逐轮叠加）。从状态里的
  **工作副本**还原：description 直接取、正文相对原始正文算 diff 逐份叠加（冲突进"未合入"清单）、脚本从
  工作副本目录读回；工作副本不可得时退化为重放最终补丁 diff 并写明。SKILL.md 走
  `patch_applier.render_skill_md()`（与回归时写工作副本同一口径，未改的部分逐字保留）。
- **交付**：`git worktree add -B <prefix>/<skill_id>/<run_id[:8]> <tmp> <skill_version_ref>` → 写文件 →
  `commit` → `push --force-with-lease` → 已有同分支 PR 则复用，否则 `gh pr create`（正文走 `--body-file`）→
  删除 worktree → `runs.pr_url`。**不自动合并**。
- **跳过条件**（`status=skipped`，原因写明）：无采纳补丁、`PATCH_PR_ENABLED=false`（仍列出候选补丁）、
  不在 git 仓库、`skill_version_ref` 不是提交（`+dirty:` / `sha256:`）、合成后无改动。
- 依赖可注入：`PatchToPullRequest(git_ops=..., patch_repository=..., ...)`；换 GitLab 实现 `GitOps` 协议即可。

### 6.2 Nightly COLD 回归（`graph/cold_suite.py`）

`_pipeline_mode=cold_suite` 时前置门禁之后只跑 `nightly.cold_suite_regression`：取 active 用例集中
`split=COLD` 的正/反向用例，复用模块一 `run_cases()`（号段 `RUN_INDEX_COLD_SUITE=240`）与触发率规则；
维度 `cold_suite_regression`，`blocking=False`，`subject_id` 前缀 `cold_suite:`，只归档 FAIL。
无 active 用例集 → `NEEDS_HUMAN_REVIEW`；无 COLD 用例 → `PASS` + 提示。

---

## 7. CI / 运维

### 7.1 workflow 一览（`.github/workflows/`）

| 文件 | 触发 | 作用 |
|---|---|---|
| `skill_evaluate.yml` | PR 改动 `skills/**` / Dockerfile / 黄金指纹；手动（可带 `skill_path` / `force_regenerate` / `run_id`） | `changed-skills` 解析 → 每个 Skill 一个矩阵任务：db-init → sync-toolbox / sync-seed-anchors → 起 API → `run` → 归档报告与 `api.log` |
| `scheduled_maintenance.yml` | `*/5 * * * *` | `internal reap-pending-hooks`（未配置常驻库时跳过） |
| `judge_health_check.yml` | `0 */6 * * *` | `internal judge-health-check`（正文建议的"拆成独立 workflow"） |
| `nightly_cold_suite.yml` | `0 2 * * 0` | 全部 Skill 的 `internal run-cold-suite`，金丝雀 `every_run` |

- 基础镜像变更检测由 `internal changed-skills` 输出的 `base_image_changed` 决定（正文示例里的
  `github.event.pull_request.changed_files` 是个整数，不能 `contains`）。
- 临时服务容器 Postgres 只适合冒烟：用例集复用、Judge 健康、数据飞轮、跨作业唤醒都需要常驻库
  （`secrets.SKILLEVAL_DB_*`）；三个定时 workflow 在没有常驻库时跳过或报错。
- 真实 Hermes 回调要能访问作业里的 API 进程：建议自托管 runner（`vars.SKILL_EVALUATE_RUNNER`）并配置
  `vars.SKILLEVAL_API_INTERNAL_BASE_URL`。

### 7.2 Secrets / Variables 清单

Secrets：`SKILLEVAL_DB_HOST/PORT/USER/PASSWORD/DATABASE`、`SKILLEVAL_LLM_API_KEY`、
`SKILLEVAL_EXECUTOR_HERMES_HOOK_SECRET`、`SKILLEVAL_EXECUTOR_LLAMA_CONTROL_API_KEY` / `_HOOK_SECRET`、
`SKILLEVAL_LANGFUSE_PUBLIC_KEY` / `_SECRET_KEY`、`SKILLEVAL_APPROVAL_DISCORD_WEBHOOK_URL`、
`SKILLEVAL_APPROVAL_HMAC_SECRET`。`GH_TOKEN` 用 `github.token`（workflow 已声明 `contents` /
`pull-requests: write`）。

Variables：`SKILL_EVALUATE_RUNNER`、`SKILLEVAL_EXECUTOR_BACKEND`、`SKILLEVAL_EXECUTOR_HERMES_ENDPOINT`、
`SKILLEVAL_EXECUTOR_LLAMA_CONTROL_ENDPOINT`、`SKILLEVAL_API_INTERNAL_BASE_URL`、`SKILLEVAL_LANGFUSE_ENABLED`、
`SKILLEVAL_APPROVAL_WORKBENCH_BASE_URL`、`SKILLEVAL_VALIDATOR_TOOLBOX_REPO_URL`、
`SKILLEVAL_GENERATOR_TRUST_SEED_REPO_URL`、`SKILLEVAL_PREFLIGHT_SANDBOX_IMAGE_REF`。

### 7.3 配置（`config.py::PipelineSettings`，前缀 `SKILLEVAL_PIPELINE_`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `report_dir` | `.` | 报告输出目录（CLI `--report-dir` 覆盖） |
| `recursion_limit` | 250 | 主图超步上限（interfaces/16 第 6 节第 3 条） |
| `wait_timeout_s` / `poll_interval_s` | 21600 / 15 | CLI 等待挂起线程跑完 |
| `register_resumer_in_api` / `resume_in_background` | true / false | 第 5.2 节 |
| `patch_pr_enabled` | **false** | 本地默认不推分支；CI 设 true |
| `patch_pr_base_branch` / `_remote` / `_branch_prefix` | 空 / origin / `skill-evaluate/auto-fix` | |
| `patch_pr_commit_author_name/email` | skill-evaluate-bot | |
| `report_url` | 空 | PR 正文里的报告链接（CI 注入 Actions 运行页） |
| `git_binary` / `gh_binary` / `git_command_timeout_s` | git / gh / 120 | |

---

## 8. 本次对前序模块的修改（追加式或缺陷修复）

| 位置 | 修改 | 性质 |
|---|---|---|
| `nodes/instruction_control/graph.py` | 三条支路汇入 `collect_findings` 改为多起点边 | **缺陷修复**：A/B 支路多一跳，原写法让汇总/路由/收尾各跑两遍，第一遍写进 `dimension_results` 的是缺效率诊断的结论 |
| `persistence/suspension.py` | `GraphResumer.resume(..., wait_key=None)`；`resolve_suspension()` 传入 | 追加 |
| `persistence/checkpointer.py` | `build_async_checkpointer()` | 追加 |
| `persistence/reaper.py` | 从 `scripts/pending_hooks_reaper.py` 移入包内，仓储可注入；脚本改为薄包装 | 重构（行为不变） |
| `persistence/models.py` / `repository.py` / 迁移 `0012` | `runs.pr_url`、`RunRepository.record_pr_url()`、`RunInfo.pr_url`（NotRequired） | 追加 |
| `agents/optimizer/patch_applier.py` | 提取纯函数 `render_skill_md()`（`None` = 该部分逐字保留） | 重构（`_sync_skill_md` 行为不变） |
| `observability/report_schema.py` / `report_generator.py` / 模板 | 三个可选字段 + `coverage_summary` 聚合 | 追加 |
| `state/trace.py` | 申领 `RUN_INDEX_COLD_SUITE=240`（240~249）；后续维度从 **250** 起 | 追加 |
| `errors.py` | `DeliveryError` | 追加 |
| `config.py` | `PipelineSettings` → `Settings.pipeline` | 追加 |
| `api/app.py` | lifespan 装配主图并注册 GraphResumer | 接入 |
| `cli.py` | `run` 实现（`--skill-path` 改为选项，与其余子命令一致）；新增 `internal` 子命令组 | 接入 |

---

## 9. 00~23 待接入项收口对照

| 来源 | 待接入项 | 结果 |
|---|---|---|
| interfaces/04_graph_resumer | 编译后注册 GraphResumer | ✅ 第 5.2 节（并增强为按 wait_key 唤醒） |
| interfaces/04_pending_hooks_reaper_scheduling | 调度接入 | ✅ `scheduled_maintenance.yml` + `internal reap-pending-hooks` |
| interfaces/05 第 1 节 | 流水线入口产出 Langfuse trace | ✅ `bootstrap_run` 调 `start_run_trace()`；各 Agent 仍各自打点（trace_handle 未注入 Agent，见第 10 节） |
| interfaces/08 `check_judge_health()` 调度 | | ✅ `judge_health_check.yml` |
| interfaces/09 第 7 节 | 补丁转 commit / PR | ✅ 第 6.1 节 |
| interfaces/11 第 3 节 | interrupt 汇总、staleness 透传、runs 先创建、补丁转 PR | ✅（interrupt 见第 3 节修正） |
| interfaces/12~15、19、20 第 2/3 节 | schema 合并、排序、PR | ✅ |
| interfaces/16 第 6 节 | schema、interrupt、recursion_limit、coverage_summary、排序 | ✅ |
| interfaces/17 第 7 节 | schema、排序、COLD Nightly 调度 | ✅ 第 6.2 节 + `nightly_cold_suite.yml` |
| interfaces/18 第 7.1、8 节 | 可追溯性制品归档、coverage_summary | ✅ `upload-artifact` 含 `artifacts/**/traceability_matrix.*` |
| interfaces/21 第 3 节 | Phase 0 装配、runs 先于 Phase 0、报告头部摘要、CI 镜像注入与 every_run | ✅ |
| interfaces/22 第 2 节 | guard、resumer、schema 新键、终点常量、thread_id 口径、CLI run 注册通知、不加静态中断、后台唤醒 | ✅ |
| interfaces/23 第 3 节 | `rag_archive_if_passed` 收尾节点、CI 迁移与索引 | ✅ 节点名 `finalize.rag_archive` |

---

## 10. 仍未实现 / 留给后续

| 事项 | 现状 | 建议接入方式 |
|---|---|---|
| `trace_handle` 注入各 Agent | 顶层 trace 已建，Agent 调用未挂到同一 trace 下 | 给各 `*Deps` 追加 `trace_handle` 并在 `bootstrap_run` 后按 run 构造；需要改动十个维度的 Deps，属于可观测性增强 |
| 跨进程并发唤醒同一 thread | 进程内加锁 | API 多副本时把 `CompiledGraphResumer.resume()` 改为队列投递 |
| `pending_hooks` 拉取兜底（Hermes/Llama `task_id`） | 仍为保守失败态 | 见 interfaces/03 第 3 点、19 第 9 节 |
| GitLab / Jenkins | 只有 GitHub Actions 参考实现 | 实现 `GitOps` 协议；CI 步骤照 `skill_evaluate.yml` 的命令序列 |
| COLD 回归失败自动恢复为 TRAIN | 只报告 + 建议 | 需要先解决"恢复后下一轮又被折叠"（interfaces/17 第 5.4 节），应走审查工作台 |
