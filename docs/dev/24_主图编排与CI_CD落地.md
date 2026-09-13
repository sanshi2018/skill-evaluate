# 24 主图编排与 CI/CD 落地

> 状态：**已实现**（实施后接入文档：`docs/dev/interfaces/24_main_graph_and_ci_cd.md`，以代码与该文档为准）
> 路线图位置：第 3 层 / 第 4 份（全项目收官文档）
> 依赖：**00~23 全部文档**
> 被依赖：无——本文档是全部设计的最终装配点

---

## 1. 本文档目标

把 00~23 累积的全部节点、接口、待接入项收拢为一张可执行的 LangGraph 主 DAG，并落地 GitHub Actions 接入方案、补丁转 PR 流程、CI 制品配置、Nightly Build 调度。本文档完成后，整个开发文档系列形成闭环——不再有"待接入文档"标记指向一份不存在的未来文档。

## 2. 主图节点分层与依赖顺序

综合 11~23 各文档声明的依赖关系（触发准确度产出的 `TestSuiteVersion` 被多个维度复用；能力覆盖率 16→17→18 强制顺序；多技能并发依赖覆盖率树），主图按五个阶段组织。

> ✅ **实现后的最终拓扑**（`src/skill_evaluate/graph/main.py`）。与本节初稿的差异均为"初稿伪码在真实实现上跑不通"，
> 原因逐条见下方"实现修正"：

```
pipeline.bootstrap_run（入口：Skill 入库 + runs 记录 + 顶层 Langfuse trace；--force-regenerate 时出题）
    ↓
Phase 0（前置门禁，串行，文档21）
  preflight.sandbox_fingerprint_gate → preflight.canary_probe_gate
    ↓ route_after_preflight（_pipeline_mode=cold_suite → nightly.cold_suite_regression → finalize.report）

Phase A（并行，互不依赖、不改用例集）
  context_scoping（文档12，纯静态）
  script_usability（文档14，纯脚本黑盒）
  trigger_accuracy（文档11，prepare_test_suite 产出 active_suite_version_id）

"改用例集"的准备节点串行链（其余节点照常并行）
  trigger_accuracy.prepare_test_suite
      → security.prepare_adversarial_suite（文档15，其后五条探测并行）
      → ⋈ trigger_accuracy.judge_train_cases → instruction_control.prepare_cases（文档13）
      → coverage.extract_capability_tree（文档16）

Phase B'（依赖模块一的产出）
  cross_model_generalization（文档19）：排在 trigger_accuracy.finalize_dimension_report 之后（要读验证集）

Phase C（串行，16 → 17 → 18）
  coverage.* → coverage.redundant_case_pruning … finalize_pruning_report → coverage.extract_tier_and_negative_constraints … finalize_weighted_coverage_report

Phase D
  multi_skill_conflict（文档20）：排在 coverage.finalize_weighted_coverage_report 之后

Phase E（收尾，串行）
  ⋈ 七个维度终节点（同步屏障） → finalize.report（文档05 ReportGenerator.build() + 写 benchmark.json/report.html）
      ↓
  finalize.patch_pr（本文档，见第5节）
      ↓
  finalize.rag_archive（文档23 archive_successful_run，门槛判断在函数内部；归档后补写报告尾部）
```

**实现修正（以代码为准）**：

1. **改用例集的准备节点串行化**。初稿把模块一与模块五并列于 Phase A、模块三/六/九并列于 Phase B。这些准备节点都调
   `ensure_test_suite()`：从没出过题时两边会同时出题，各自激活一个继承旧版本的新版本，后激活者把先激活那批用例
   从 active 版本里丢掉（数据库 lost update，不报错）。
2. **汇合必须用多起点边** `builder.add_edge([...], "finalize.report")`。初稿"LangGraph 原生支持汇聚、不需要手写同步
   屏障"只对多起点边成立；逐条 `add_edge(terminal, "finalize.report")` 会让收尾节点每个前驱完成时各跑一次。
3. 模块三同时等模块一训练集判定（interfaces/13 第 3.1 节），模块九排在模块一终节点之后（interfaces/19 第 0 节）。
4. 主图 schema 是全部维度 `*State` 的并集（`graph/state.py::MainGraphState`），并把 `active_suite_version_id`
   换成"非 None 后写者胜"的 reducer——同一超步两个节点回写同一个版本号时默认通道会抛 `InvalidUpdateError`。
5. 全部节点经 `ApprovalGuardedBuilder` 平铺装配；节点名一律用各维度的 `ENTRY_NODE` / `TERMINAL_NODE` 常量
   （初稿的 `"preflight.fingerprint"`、`"multi_skill.entry"` 等是占位写法）。
6. checkpointer 用异步版 `build_async_checkpointer()`：节点全是 `async def`，同步 `PostgresSaver` 挂在 `ainvoke()`
   上会 `NotImplementedError`。
7. 实施期发现并修复了模块三子图的一处汇合缺陷（三条支路汇入 `collect_findings` 改为多起点边），见接入文档第 8 节。

```python
# src/skill_evaluate/graph/main.py（节选，完整实现见源码）
def build_main_graph_builder(deps: MainGraphDeps | None = None) -> StateGraph:
    builder = StateGraph(MainGraphState)
    graph = ApprovalGuardedBuilder(builder, deps.approval_guard)      # 文档22：所有节点套审批 guard

    preflight.add_preflight_nodes(graph, deps.preflight)
    trigger_pipeline = trigger_accuracy.add_trigger_accuracy_nodes(graph, deps.trigger_accuracy)
    ...                                                               # 其余维度的 add_*_nodes 一行未改
    graph.add_node("pipeline.bootstrap_run", pipeline_nodes.bootstrap_run)
    graph.add_node("nightly.cold_suite_regression", pipeline_nodes.cold_suite_regression)
    graph.add_node("finalize.report", pipeline_nodes.finalize_report)
    graph.add_node("finalize.patch_pr", pipeline_nodes.patch_to_pr)
    graph.add_node("finalize.rag_archive", pipeline_nodes.rag_archive)

    builder.set_entry_point("pipeline.bootstrap_run")
    builder.add_edge("pipeline.bootstrap_run", preflight.ENTRY_NODE)
    builder.add_conditional_edges(preflight.TERMINAL_NODE, route_after_preflight,
                                  [*PHASE_A_ENTRY_NODES, "nightly.cold_suite_regression"])
    builder.add_edge(trigger_accuracy.ENTRY_NODE, security.ENTRY_NODE)
    builder.add_edge([security.ENTRY_NODE, trigger_accuracy.NODE_NAMES["judge_train_cases"]],
                     instruction_control.ENTRY_NODE)
    builder.add_edge(instruction_control.ENTRY_NODE, coverage.ENTRY_NODE)
    builder.add_edge(trigger_accuracy.TERMINAL_NODE, cross_model.ENTRY_NODE)
    builder.add_edge(coverage.TERMINAL_NODE, pruning.ENTRY_NODE)
    builder.add_edge(pruning.TERMINAL_NODE, weighted_coverage.ENTRY_NODE)
    builder.add_edge(weighted_coverage.TERMINAL_NODE, multi_skill.ENTRY_NODE)
    builder.add_edge(list(DIMENSION_TERMINAL_NODES), "finalize.report")   # 同步屏障
    builder.add_edge("nightly.cold_suite_regression", "finalize.report")
    builder.add_edge("finalize.report", "finalize.patch_pr")
    builder.add_edge("finalize.patch_pr", "finalize.rag_archive")
    builder.set_finish_point("finalize.rag_archive")
    return builder

def build_main_graph(checkpointer, deps=None):
    return build_main_graph_builder(deps).compile(checkpointer=checkpointer,
                                                  interrupt_before=INTERRUPT_BEFORE_NODES)  # 恒为 []，见第3节
# 编译后：register_graph_resumer(CompiledGraphResumer(graph))（API lifespan / CLI run / 巡检入口）
```

## 3. `interrupt_before` 编译期列表汇总

```python
# src/skill_evaluate/graph/main.py
SUSPENDABLE_NODES = (            # 各维度 INTERRUPT_BEFORE_NODES 的并集：可能停在人工审批上的节点
    "trigger_accuracy.optimizer_loop", "instruction_control.optimizer_loop",   # 文档09：闭环最大重试
    "security.appsec_optimizer_loop",
    "coverage.extract_capability_tree",                                        # 文档16：能力树规模审核
)
INTERRUPT_BEFORE_NODES: list[str] = []   # 实际传给 compile() 的列表：刻意为空
```

> ⚠️ **实现修正**：初稿把上表直接传给 `compile(interrupt_before=...)`。静态 `interrupt_before` 的真实语义是**每次运行**
> 在进入这些节点前无条件停下——不管有没有超出重试次数、树大不大——且这种停顿不写审批卡片，流水线会永远停在
> 模块六门口。上述节点实际全部通过**动态** `interrupt()` 按需挂起，因此编译期列表为空，"可能挂起"由
> `SUSPENDABLE_NODES` 表达。文档 22 的审批闸门与节点级 guard 同样是动态 `interrupt()`。

**注意**：文档 04 第 5 节的"外部事件唤醒"（Hermes Hook 回调、审批 API 回调）走的是**动态** `interrupt()`。主图里常有多个
并行节点同时挂起，`CompiledGraphResumer` 按中断载荷里的 `wait_key` 定位中断 id 精确唤醒
（`GraphResumer.resume()` 追加了 `wait_key` 参数）。

## 4. 定时/巡检任务调度

```yaml
# .github/workflows/scheduled_maintenance.yml（节选）
on:
  schedule:
    - cron: "*/5 * * * *"    # pending_hooks巡检，文档04第5节
jobs:
  reap-pending-hooks:
    steps:
      - run: skill-evaluate internal reap-pending-hooks   # 装配主图 + 注册 GraphResumer 后调用 persistence/reaper.py::reap_once

# .github/workflows/judge_health_check.yml（按初稿建议拆成独立 workflow）
on:
  schedule:
    - cron: "0 */6 * * *"
jobs:
  judge-health-check:
    steps:
      - run: skill-evaluate internal judge-health-check   # 文档08 check_judge_health()；不健康时退出码 1
```

`pending_hooks_reaper`（文档 04）与 `check_judge_health`（文档 08）都在本文档中正式接入调度——两者都不依赖某次具体评测运行的图状态，是独立于主图之外的运维巡检任务，因此不建模为主图节点，而是独立的定时 CI Job，通过 CLI 子命令触发（`internal reap-pending-hooks` / `internal judge-health-check`，补齐文档 01 CLI 骨架）。

> 实现补充：两个定时任务必须连接**常驻** Postgres（`secrets.SKILLEVAL_DB_*`），未配置时跳过；`reap_once` 已从
> `scripts/pending_hooks_reaper.py` 移入 `skill_evaluate.persistence.reaper`（脚本保留为薄包装）。巡检唤醒挂起线程后会在
> 本作业内把流水线继续跑到下一个挂起点，作业超时按维度最长耗时留足。

## 5. 补丁转 PR 流程

```python
# src/skill_evaluate/graph/patch_pr.py（结构示意，完整实现见源码）
async def run(self, state, *, report_link) -> PullRequestOutcome:     # 从不抛异常，结果写进报告尾部
    accepted, excluded = await collect_accepted_patches(state, patch_repository)
    #   ↑ 从状态取 _sec_applied_patch_id / _applied_patch_id / _ic_applied_patch_id，
    #     核对 patch_application_results.applied；regression_passed≠True 的是人工 adopt，照常纳入并醒目标注
    if not accepted or not settings.patch_pr_enabled: return skipped
    worktree = await git_ops.create_worktree(repo, branch=f"skill-evaluate/auto-fix/{skill_id}/{run_id[:8]}",
                                             base_ref=state["skill_version_ref"])
    composed = compose_changes(original_skill, accepted, read_file_from_worktree)
    #   ↑ 从工作副本还原文件内容：description 直接取；正文相对原始正文算 diff 逐份叠加（冲突进"未合入"清单）；
    #     脚本从工作副本目录读回；SKILL.md 走 patch_applier.render_skill_md()
    write(composed.files); await git_ops.commit_all(...); await git_ops.push(worktree, branch)
    pr_url = existing_pr or await git_ops.create_pull_request(worktree, branch=..., title=f"[skill-evaluate] 自动修复: {skill_id}",
                                                              body=render_pr_body(...), base=settings.patch_pr_base_branch)
    await run_repository.record_pr_url(run_id, pr_url)                 # 迁移 0012：runs.pr_url
```

> ⚠️ **实现修正**：
> 1. 不存在 `patch_repository.list_accepted_for_run(run_id)`——`patches` 表没有 run_id 列，本次运行采纳了哪份补丁本来就在
>    图状态里（interfaces/11、13、15 第 3 节）。
> 2. 不能 `git_ops.apply_diff(patch.diff)`：description / 正文补丁 diff 的是字符串字段而不是文件，且优化闭环逐轮叠加，
>    最终补丁的基线是上一轮工作副本。经过回归验证的是状态里的**工作副本**，因此从它还原文件内容。
> 3. 多份补丁（模块三/五都可能改正文）先确认能叠加，冲突的写进 PR 正文而不是悄悄丢掉。
> 4. `skill_version_ref` 不是提交（`+dirty:` / `sha256:`，评测的是未提交的工作区）、不在 git 仓库、未开启
>    `SKILLEVAL_PIPELINE_PATCH_PR_ENABLED`（默认 false，CI 设 true）时跳过并写明原因。
> 5. 在独立 `git worktree` 里准备提交，不动 CI 当前检出；推送用 `--force-with-lease`，同分支已有 PR 则复用（断点恢复幂等）。

`git_ops` 封装 `git` / `gh` CLI 调用（`graph/git_ops.py::SubprocessGitOps`，实现 `GitOps` 协议，参数列表调用、无 shell），这是本文档对文档 09 第 7 节"真正把补丁转换为 git commit / Pull Request 的动作，属于文档 24 的职责"的正式落地。**PR 创建后不自动合并**——即使补丁通过了所有自动化回归验证，最终合并决定仍由人类通过常规 Code Review 流程完成，评测系统的自动化边界止于"提出一个证据充分的候选修复"，不延伸到"replace 人类的合并决策"。

## 6. GitHub Actions 主流程

> ✅ 实现见 `.github/workflows/skill_evaluate.yml` 与 `nightly_cold_suite.yml`。下列为骨架，完整的 env / Secrets 见
> 接入文档第 7 节。

```yaml
# .github/workflows/skill_evaluate.yml（骨架）
on:
  pull_request:
    paths: ["skills/**/SKILL.md", "skills/**/scripts/**", "skills/**/references/**", "**/Dockerfile", "golden_fingerprint.json"]
  workflow_dispatch:
    inputs: {skill_path: {type: string}, force_regenerate: {type: boolean, default: false}, run_id: {type: string}}

jobs:
  resolve:            # skill-evaluate internal changed-skills --base <base.sha> --head <head.sha> → {"skills": [...], "base_image_changed": bool}
  evaluate:
    needs: resolve
    strategy: {matrix: {skill: "${{ fromJSON(needs.resolve.outputs.skills) }}"}}
    services:
      postgres: {image: pgvector/pgvector:pg16}     # 仅冒烟；生产用 secrets.SKILLEVAL_DB_* 指向常驻库
    steps:
      - uses: actions/checkout@v4                     # fetch-depth: 0（自动修复分支从 skill_version_ref 拉出）
      - run: pip install -e ".[tokenizer,langfuse,reranker]"
      - run: skill-evaluate db-init                   # 迁移到 0012
      - run: skill-evaluate sync-toolbox              # 同步后自动建记忆库索引（文档23）
      - run: skill-evaluate sync-seed-anchors
      - name: Force canary probe if base image changed
        if: needs.resolve.outputs.base_image_changed == 'true'
        run: echo "SKILLEVAL_PREFLIGHT_CANARY_CHECK_MODE=every_run" >> $GITHUB_ENV
      - run: nohup uvicorn skill_evaluate.api.app:app ... &   # 承接 Hook 回调与审批决策（lifespan 注册 GraphResumer）
      - run: skill-evaluate run --skill-path ${{ matrix.skill }} [--force-regenerate] [--run-id ...]
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          path: |
            benchmark.json
            report.html
            artifacts/**/traceability_matrix.*
          retention-days: 90
```

```yaml
# .github/workflows/nightly_cold_suite.yml（文档17 COLD用例调度）
on:
  schedule:
    - cron: "0 2 * * 0"   # 每周日跑一次，架构文档原话"周末的Nightly Build"
jobs:
  run-cold-cases:
    strategy: {matrix: {skill: "<internal changed-skills --all>"}}
    env: {SKILLEVAL_PREFLIGHT_CANARY_CHECK_MODE: every_run}
    steps:
      - run: skill-evaluate internal run-cold-suite --skill-path ${{ matrix.skill }}
```

> ⚠️ **实现修正**：
> 1. 初稿的 `if: contains(github.event.pull_request.changed_files, 'Dockerfile')` 不成立——`changed_files` 是改动文件**数**。
>    改由 `internal changed-skills` 输出 `base_image_changed`（`graph/ci_support.py::touches_base_image`）。
>    同时注入 `SKILLEVAL_PREFLIGHT_SANDBOX_IMAGE_REF=<基础镜像 digest>`（文档 21），未注入时回落为按沙箱指纹摘要判断。
> 2. `run` 命令的 skill 路径是 `--skill-path` 选项；数据库初始化命令是 `db-init`（typer 自动转连字符）。
> 3. `run` 退出码：0 通过 / 1 阻断 / 2 评测系统故障 / 3 仍挂起在审批或回调上 / 4 被人工放弃或前置门禁拒绝。
> 4. Nightly COLD 回归与完整评测共用同一张主图（`_pipeline_mode=cold_suite`），因为 API 进程只持有一个已编译主图，
>    另编一张图会让挂起后的唤醒用错图恢复 checkpoint。维度 `cold_suite_regression` 不阻断合并。

`CHANGED_SKILL_PATH` 的解析（从 PR diff 中提取具体哪个 Skill 目录变更）由 `internal changed-skills` 完成：`git diff --name-only base...head`，每个改动文件归属离它最近的、含 SKILL.md 的祖先目录（`skills/` 之下），SKILL.md 已删除的目录不产生评测目标。

## 7. 发布/回滚检查清单

本项目自身（评测流水线代码，区别于被评测的 Skill）的发布检查清单：

- [ ] `alembic upgrade head` 在预发布环境验证通过，且新增迁移不包含破坏性变更（无 `DROP COLUMN`/`DROP TABLE` 针对仍在使用的表；若必须删除字段，先标记废弃一个发布周期）
- [ ] `golden_fingerprint.json`（文档21）若本次发布更新了基础镜像/依赖版本，已同步更新并经人工确认
- [ ] `judge_health_status`（文档08）在预发布环境的黄金基准通过率符合预期，避免带着已冻结的 Judge 配置上线
- [ ] `LlamaControlBackend`（文档19）等外部依赖服务的可用性已确认（`health_check()` 通过）
- [ ] CI Secrets（Hermes Hook Secret、Langfuse Key、Discord Webhook（`SKILLEVAL_APPROVAL_DISCORD_WEBHOOK_URL`，文档 22）、审批 HMAC Secret（`SKILLEVAL_APPROVAL_HMAC_SECRET`）、LLM API Key）已在目标环境正确配置，且旧 Secret 的轮换不影响正在进行中的评测运行（`pending_hooks` 中 `waiting` 状态的记录使用发起请求时的 Secret 版本校验，需确认签名校验逻辑对新旧 Secret 轮换窗口的处理——若无重叠容忍期，建议发布窗口选在无进行中评测运行时）
- [ ] 本次发布的迁移 `0012_run_pr_url` 为纯追加 nullable 列，旧版本代码不读该列，可直接回滚代码（文档 24 实现补充）
- [ ] API 进程启动日志出现 `api_graph_resumer_registered`（未出现时审批决策 API 对阻塞卡片返回 503、Hook 返回 500）
- [ ] 新旧版本主图拓扑变化时（节点改名/增删），确认没有进行中的挂起线程：checkpoint 里记着旧节点名，新图无法恢复它们；有则先处理完或放弃
- [ ] 回滚方案：数据库迁移保持向后兼容（新版本代码可以跑在旧一版本 schema 上，至少保证紧邻的一次回滚不需要额外的降级迁移脚本）

## 8. 全项目文档系列回顾

至此，25 份开发文档（00~24）覆盖了架构文档全部十一大模块及其全部子节点/深度补充，形成如下最终结构：

- **第 0 层（01~05）**：工程地基——脚手架、State Schema、执行引擎适配、持久化、可观测性。
- **第 1 层（06~10）**：五个跨维度公共智能体——Generator、Mini Review、Judge、Optimizer、Validator。
- **第 2 层（11~20）**：十大评测维度的完整实现。
- **第 3 层（21~24）**：评测系统自身可信度、人工协作闭环、长时记忆、主图装配与 CI 落地。

全部此前标记的"待接入文档"项目均已在对应后续文档中收口（逐条对照见 `docs/dev/interfaces/24_main_graph_and_ci_cd.md` 第 9 节）；本文档作为最终装配点，未再产生新的强制"待接入"清单。实施期识别出的可选增强（Agent 级 trace_handle 注入、多副本 API 的跨进程唤醒队列、GitLab/Jenkins 适配等）列在该接入文档第 10 节。

---

## 全系列完成

25 份开发文档已全部产出并经你逐份确认。如需对某份文档做修订，或希望针对某个模块进一步细化（如具体 Prompt 措辞、更详细的测试计划），请指出对应文档编号，我可以在其基础上继续迭代。
