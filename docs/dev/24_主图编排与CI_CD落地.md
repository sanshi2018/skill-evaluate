# 24 主图编排与 CI/CD 落地

> 状态：**待确认**
> 路线图位置：第 3 层 / 第 4 份（全项目收官文档）
> 依赖：**00~23 全部文档**
> 被依赖：无——本文档是全部设计的最终装配点

---

## 1. 本文档目标

把 00~23 累积的全部节点、接口、待接入项收拢为一张可执行的 LangGraph 主 DAG，并落地 GitHub Actions 接入方案、补丁转 PR 流程、CI 制品配置、Nightly Build 调度。本文档完成后，整个开发文档系列形成闭环——不再有"待接入文档"标记指向一份不存在的未来文档。

## 2. 主图节点分层与依赖顺序

综合 11~23 各文档声明的依赖关系（触发准确度产出的 `TestSuiteVersion` 被多个维度复用；能力覆盖率 16→17→18 强制顺序；多技能并发依赖覆盖率树），主图按五个阶段组织：

```
Phase 0（前置门禁，串行，文档21）
  sandbox_fingerprint_gate → canary_probe_gate

Phase A（并行，互不依赖）
  trigger_accuracy（文档11，产出 active_suite_version_id）
  context_scoping（文档12，纯静态）
  script_usability（文档14，纯脚本黑盒）
  security（文档15，自带Attacker Agent独立测试集）

Phase B（并行，依赖Phase A中trigger_accuracy产出的测试集）
  instruction_control（文档13）
  capability_coverage（文档16）
  cross_model_generalization（文档19）

Phase C（串行，依赖Phase B的capability_coverage）
  test_suite_health（文档17）
      ↓
  weighted_coverage（文档18）

Phase D（依赖Phase C的能力树 + Phase A的测试集）
  multi_skill_conflict（文档20）

Phase E（收尾，串行）
  finalize_benchmark_report（文档05 ReportGenerator.build()）
      ↓
  patch_to_pr_conversion（本文档，见第5节）
      ↓
  rag_archive_if_passed（文档23 archive_successful_run，条件调用）
```

> ⚠️ 文档 21 实现后的修正：Phase 0 请用 `skill_evaluate.nodes.preflight.add_preflight_nodes(builder)` 装配，
> 节点名以 `ENTRY_NODE`（`preflight.sandbox_fingerprint_gate`）/ `TERMINAL_NODE`（`preflight.canary_probe_gate`）
> 常量为准，下面代码里的 `"preflight.fingerprint"` / `"preflight.canary"` 与裸函数是占位写法；主图 schema 需并入
> `PreflightState`，进入 Phase 0 前须已创建 `runs` 记录（金丝雀走 Hook 回调）。详见
> `docs/dev/interfaces/21_generator_trust_and_preflight.md` 第 3 节。

```python
# src/skill_evaluate/graph/main.py
def build_main_graph() -> CompiledGraph:
    builder = StateGraph(PipelineState)

    builder.add_node("preflight.fingerprint", sandbox_fingerprint_gate)
    builder.add_node("preflight.canary", canary_probe_gate)
    builder.add_edge("preflight.fingerprint", "preflight.canary")

    for node_name, fn in PHASE_A_NODES.items():
        builder.add_node(node_name, fn)
        builder.add_edge("preflight.canary", node_name)

    for node_name, fn in PHASE_B_NODES.items():
        builder.add_node(node_name, fn)
        builder.add_edge("trigger_accuracy.prepare_test_suite", node_name)   # 显式声明对Phase A特定产出的依赖

    builder.add_node("coverage.pruning", test_suite_health_entry)          # 17
    builder.add_node("coverage.weighting", weighted_coverage_entry)         # 18
    builder.add_edge("capability_coverage.blind_spot_detection", "coverage.pruning")
    builder.add_edge("coverage.pruning", "coverage.weighting")

    multi_skill = add_multi_skill_nodes(builder)                            # 20（平铺八个节点，见 interfaces/20）
    builder.add_edge("coverage.weighting", MULTI_SKILL_ENTRY)               # 排在模块八之后：读负向约束映射
    builder.add_edge("trigger_accuracy.prepare_test_suite", MULTI_SKILL_ENTRY)

    builder.add_node("finalize.report", finalize_benchmark_report)
    for terminal_node in ["instruction_control.finalize_dimension_report", "context_scoping.finalize_dimension_report",
                            "script_usability.finalize_dimension_report", "security.finalize_dimension_report",
                            "cross_model.finalize_dimension_report", MULTI_SKILL_TERMINAL]:
        builder.add_edge(terminal_node, "finalize.report")

    builder.add_node("finalize.patch_pr", patch_to_pr_conversion)
    builder.add_node("finalize.rag_archive", rag_archive_conditional)
    builder.add_edge("finalize.report", "finalize.patch_pr")
    builder.add_edge("finalize.patch_pr", "finalize.rag_archive")

    builder.set_entry_point("preflight.fingerprint")
    builder.set_finish_point("finalize.rag_archive")

    return builder.compile(
        checkpointer=build_checkpointer(),   # 文档04
        interrupt_before=INTERRUPT_BEFORE_NODES,   # 见第3节
    )
```

**并行汇聚（LangGraph fan-in）说明**：Phase A 四个节点、Phase B 三个节点分别通过 LangGraph 的多前驱边自然并行执行，`finalize.report` 作为汇聚点等待其全部前驱（各维度的 `finalize_dimension_report` 终节点）完成——LangGraph 原生支持这种 DAG 汇聚语义，本文档不需要手写额外的同步屏障。

## 3. `interrupt_before` 编译期列表汇总

```python
INTERRUPT_BEFORE_NODES = [
    # 文档09：Optimizer最大重试挂起（trigger_accuracy.optimizer_loop / security.appsec_optimizer_loop）
    "trigger_accuracy.optimizer_loop", "security.appsec_optimizer_loop",
    # 文档16：能力树规模人工确认
    "capability_coverage.extract_capability_tree",
]
```

**注意**：这份列表只覆盖**编译期静态已知**会挂起的节点入口；文档 04 第 5 节强调的"外部事件唤醒"（Hermes Hook 回调、审批 API 回调）走的是**动态** `interrupt()`（运行时按需触发,不需要节点名预先出现在这份静态列表里）。两种机制在文档 04 已并存设计，本文档只需正确汇总静态列表，动态挂起点（如每次 `ExecutorBackend.execute()` 内部等待 Hermes 回调）不在此处重复声明。

## 4. 定时/巡检任务调度

```yaml
# .github/workflows/scheduled_maintenance.yml
name: scheduled-maintenance
on:
  schedule:
    - cron: "*/5 * * * *"    # pending_hooks巡检，文档04第5节
jobs:
  reap-pending-hooks:
    runs-on: ubuntu-latest
    steps:
      - run: skill-evaluate internal reap-pending-hooks   # CLI新增子命令，本文档实现

  judge-health-check:
    runs-on: ubuntu-latest
    if: github.event.schedule == '0 */6 * * *'   # 独立频率，通过schedule字符串区分job触发条件的简化写法，
                                                     # 实际实现可拆分为独立workflow文件，此处为示意
    steps:
      - run: skill-evaluate internal judge-health-check   # 文档08 check_judge_health()调度
```

`pending_hooks_reaper`（文档 04）与 `check_judge_health`（文档 08）都在本文档中正式接入调度——两者都不依赖某次具体评测运行的图状态，是独立于主图之外的运维巡检任务，因此不建模为主图节点，而是独立的定时 CI Job，通过 CLI 子命令触发（`internal reap-pending-hooks` / `internal judge-health-check`，本文档新增，补齐文档 01 CLI 骨架）。

## 5. 补丁转 PR 流程

```python
# src/skill_evaluate/graph/patch_pr.py
async def patch_to_pr_conversion(state: PipelineState) -> PipelineState:
    accepted_patches = await patch_repository.list_accepted_for_run(state["run_id"])   # 回归验证通过且未被人工放弃的补丁
    if not accepted_patches:
        return state

    branch_name = f"skill-evaluate/auto-fix/{state['skill_id']}/{state['run_id'][:8]}"
    await git_ops.create_branch(branch_name, base=state["skill_version_ref"])
    for patch in accepted_patches:
        await git_ops.apply_diff(branch_name, patch.target_path, patch.diff)
    pr_url = await git_ops.create_pull_request(
        branch=branch_name, title=f"[skill-evaluate] 自动修复: {state['skill_id']}",
        body=_render_pr_body(accepted_patches, state["run_id"]),   # 包含每个补丁的rationale、回归验证摘要、benchmark报告链接
    )
    await pipeline_state_repo.record_pr_url(state["run_id"], pr_url)
    return state
```

`git_ops` 封装 `gh` CLI 调用（`gh pr create` 等），这是本文档对文档 09 第 7 节"真正把补丁转换为 git commit / Pull Request 的动作，属于文档 24 的职责"的正式落地。**PR 创建后不自动合并**——即使补丁通过了所有自动化回归验证，最终合并决定仍由人类通过常规 Code Review 流程完成，评测系统的自动化边界止于"提出一个证据充分的候选修复"，不延伸到"replace 人类的合并决策"。

## 6. GitHub Actions 主流程

```yaml
# .github/workflows/skill_evaluate.yml
name: skill-evaluate
on:
  pull_request:
    paths: ["skills/**/SKILL.md", "skills/**/scripts/**", "skills/**/references/**"]
  workflow_dispatch:
    inputs:
      force_regenerate: {type: boolean, default: false}

jobs:
  evaluate:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env: {POSTGRES_PASSWORD: ci, POSTGRES_DB: skill_evaluate}
        ports: ["5432:5432"]
    steps:
      - uses: actions/checkout@v4
      - run: pip install -e ".[dev]"
      - run: skill-evaluate db_init
      - name: Generate test suite (if forced)
        if: inputs.force_regenerate
        run: skill-evaluate generate --skill-path ${{ env.CHANGED_SKILL_PATH }} --force
      - name: Run evaluation pipeline
        run: skill-evaluate run --skill-path ${{ env.CHANGED_SKILL_PATH }}
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: benchmark-report
          path: |
            benchmark.json
            report.html
            artifacts/**/traceability_matrix.*
          retention-days: 90
```

```yaml
# .github/workflows/nightly_cold_suite.yml（文档17 COLD用例调度）
name: nightly-cold-suite
on:
  schedule:
    - cron: "0 2 * * 0"   # 每周日跑一次，架构文档原话"周末的Nightly Build"
jobs:
  run-cold-cases:
    runs-on: ubuntu-latest
    steps:
      - run: skill-evaluate internal run-cold-suite --skill-path ${{ ... }}
```

```yaml
# 金丝雀探针的独立触发条件（文档21第6节canary_check_mode="nightly_or_image_change"）
# 在 skill_evaluate.yml 中额外增加一步，仅当基础镜像Dockerfile发生变更或到达每日首次运行时强制探针：
      - name: Force canary probe if base image changed
        if: contains(github.event.pull_request.changed_files, 'Dockerfile')
        run: echo "SKILLEVAL_PREFLIGHT_CANARY_CHECK_MODE=every_run" >> $GITHUB_ENV
# 文档 21 实现补充：同时注入 SKILLEVAL_PREFLIGHT_SANDBOX_IMAGE_REF=<基础镜像 digest>，
# 未注入时"镜像是否变更"回落为按沙箱指纹摘要判断。
```

`CHANGED_SKILL_PATH` 的解析（从 PR diff 中提取具体哪个 Skill 目录变更）用标准 `git diff --name-only` 结合路径前缀匹配即可，属于 CI 脚本细节，本文档不展开逐行实现。

## 7. 发布/回滚检查清单

本项目自身（评测流水线代码，区别于被评测的 Skill）的发布检查清单：

- [ ] `alembic upgrade head` 在预发布环境验证通过，且新增迁移不包含破坏性变更（无 `DROP COLUMN`/`DROP TABLE` 针对仍在使用的表；若必须删除字段，先标记废弃一个发布周期）
- [ ] `golden_fingerprint.json`（文档21）若本次发布更新了基础镜像/依赖版本，已同步更新并经人工确认
- [ ] `judge_health_status`（文档08）在预发布环境的黄金基准通过率符合预期，避免带着已冻结的 Judge 配置上线
- [ ] `LlamaControlBackend`（文档19）等外部依赖服务的可用性已确认（`health_check()` 通过）
- [ ] CI Secrets（Hermes Hook Secret、Langfuse Key、Discord Webhook、LLM API Key）已在目标环境正确配置，且旧 Secret 的轮换不影响正在进行中的评测运行（`pending_hooks` 中 `waiting` 状态的记录使用发起请求时的 Secret 版本校验，需确认签名校验逻辑对新旧 Secret 轮换窗口的处理——若无重叠容忍期，建议发布窗口选在无进行中评测运行时）
- [ ] 回滚方案：数据库迁移保持向后兼容（新版本代码可以跑在旧一版本 schema 上，至少保证紧邻的一次回滚不需要额外的降级迁移脚本）

## 8. 全项目文档系列回顾

至此，25 份开发文档（00~24）覆盖了架构文档全部十一大模块及其全部子节点/深度补充，形成如下最终结构：

- **第 0 层（01~05）**：工程地基——脚手架、State Schema、执行引擎适配、持久化、可观测性。
- **第 1 层（06~10）**：五个跨维度公共智能体——Generator、Mini Review、Judge、Optimizer、Validator。
- **第 2 层（11~20）**：十大评测维度的完整实现。
- **第 3 层（21~24）**：评测系统自身可信度、人工协作闭环、长时记忆、主图装配与 CI 落地。

全部此前标记的"待接入文档"项目均已在对应后续文档中收口；本文档作为最终装配点，未再产生新的"待接入"清单——项目设计阶段至此完整闭环，可以转入具体编码实现阶段。

---

## 全系列完成

25 份开发文档已全部产出并经你逐份确认。如需对某份文档做修订，或希望针对某个模块进一步细化（如具体 Prompt 措辞、更详细的测试计划），请指出对应文档编号，我可以在其基础上继续迭代。
