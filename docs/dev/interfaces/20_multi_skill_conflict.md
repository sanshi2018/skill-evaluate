# 接入文档：模块十子图、加载归因、告警分发器（docs/dev/20 留给后续模块的接口）

> 由谁接入：`22`（**注册真实告警通道**、按 `blocking` 分流阻塞/非阻塞人工介入、工作台双路 Trace 比对）、
> `24`（主图装配、状态 schema 合并、排序）、`21`（沙箱环境一致性：跨技能状态突变侦测的遗留项），
> 以及**运维侧**（维护基准干扰包与基石 Skill 清单）、**真实 Hermes 接入方**（多技能挂载契约）。
> 当前状态：八个节点、七条量化规则、三个评审模板、MULTI_SKILL 生成模板、多技能加载归因、
> 告警分发器接口全部落地，有测试覆盖（`tests/skill_evaluate/test_multi_skill.py` 44 条 +
> `test_generator.py` 追加 1 条，不碰库、不发真实请求、不起沙箱）。

---

## 0. 三十秒上手

```python
from skill_evaluate.nodes.multi_skill import (
    ENTRY_NODE, TERMINAL_NODE, NODE_NAMES, MultiSkillDeps, MultiSkillState,
    add_multi_skill_nodes, build_multi_skill_subgraph,
)

# A. 装进主图（docs/dev/24）：只加维度内部的边（含三条动态探测的扇出与汇合）
pipeline = add_multi_skill_nodes(builder)
builder.add_edge("coverage.finalize_weighted_coverage_report", ENTRY_NODE)   # 需要模块八的负向约束映射
builder.add_edge(TERMINAL_NODE, "finalize.report")

# B. 单独跑一遍
graph = build_multi_skill_subgraph().compile(checkpointer=...)
await graph.ainvoke({"run_id": ..., "skill_id": ..., "skill_version_ref": ...,
                     "active_suite_version_id": ...})   # 可省略：prepare 节点会调 ensure_test_suite
```

**导入即注册**：`import skill_evaluate.nodes.multi_skill` 注册七条量化规则；
`import skill_evaluate.agents.mini` 注册三个评审模板；`import skill_evaluate.agents.generator`
注册 MULTI_SKILL 生成模板。

**装配期硬校验**：`NODE_BACKEND_ROUTING["multi_skill_conflict"]` 必须是 `PLUGGABLE`，否则
`MultiSkillPipeline.__init__` 抛 `ConfigurationError`（Mini 后端忽略 `background_skills` 且
`loaded_skill_md` 恒为 True）。

---

## 1. 三个名字与节点

| 常量 | 取值 | 用途 |
|---|---|---|
| `DIMENSION` / `ROUTING_KEY` | `multi_skill_conflict` | `dimension_results.dimension`、路由表键 |
| `NODE_PREFIX` | `multi_skill` | 节点名前缀 |

```
multi_skill.prepare_multi_skill_context                  （ENTRY_NODE）
multi_skill.namespace_pollution_static_scan
multi_skill.cross_trigger_interference_probe             ┐
multi_skill.instruction_antagonism_and_semantic_flow_probe├ PROBE_NODES，并行
multi_skill.context_exhaustion_attention_decay_probe      ┘
multi_skill.role_collision_and_temporal_static_scan      （等三条支路全部完成）
multi_skill.core_skill_regression_gate
multi_skill.finalize_dimension_report                    （TERMINAL_NODE）
```

⚠️ docs/dev/24 正文里的 `"multi_skill.entry"` 是占位名，请用 `ENTRY_NODE` / `TERMINAL_NODE` 常量；
24 正文把 `"multi_skill.entry"` 当成终节点连到 `finalize.report` 也是错的——应连 `TERMINAL_NODE`。

`INTERRUPT_BEFORE_NODES = []`：本维度不产生补丁、不进闭环；基石熔断通过 `blocking=True` 表达。
`recursion_limit`：无环，固定 6 个超步（三条支路同一超步）。

---

## 2. ⚠️ 主图的状态 schema 必须包含本维度私有键

`MultiSkillState` 声明的 13 个键必须并进主图 schema（与 `interfaces/11` 第 2 节同一条坑）：

| 键 | 含义 | 谁写 | 谁读 |
|---|---|---|---|
| `_multi_skill_noise_pack_refs` | 干扰包 `[{skill_id, version_ref}]` | prepare | 全部探测、finalize（告警 payload） |
| `_multi_skill_core_skill_refs` | 基石 Skill 引用 | prepare | core gate |
| `_multi_skill_context_notes` | 准备阶段说明 | prepare | finalize |
| `_multi_skill_case_ids` | MULTI_SKILL 复合用例 | prepare | antagonism |
| `_multi_skill_suite_staleness_warning` | 用例集版本漂移告警 | prepare | finalize / `24` 透传 |
| `_multi_skill_namespace_outcome` | `ProbeOutcome` | namespace | finalize |
| `_multi_skill_hijack_outcome` | `ProbeOutcome` | hijack | finalize |
| `_multi_skill_antagonism_outcome` | `ProbeOutcome`（含 `healthy_case_ids`） | antagonism | temporal、finalize |
| `_multi_skill_attention_outcome` | `ProbeOutcome` | attention | finalize |
| `_multi_skill_role_outcome` | `ProbeOutcome` | role/temporal | finalize |
| `_multi_skill_temporal_outcome` | `ProbeOutcome` | role/temporal | finalize |
| `_multi_skill_core_regression_outcome` | `ProbeOutcome` | core gate | finalize |
| `_multi_skill_alert_dispatched` | 本次是否发出深度冲突告警 | finalize | `22` / `24` |

漏键的症状：finalize 报 `NEEDS_HUMAN_REVIEW` 并点名缺的键；时序扰动因读不到拮抗结果而 `skipped`。
三条并行支路各写自己的键，`executed_trace_ids` / `judge_verdict_ids` 是 add reducer，并行安全。

---

## 3. `24`：排序与前置依赖

- **排在模块八（`coverage.finalize_weighted_coverage_report`）之后**：注意力衰减读
  `CapabilityTree.negative_constraints[*].covering_case_ids`。顺序反了**不报错**——能力树缺失 →
  `skipped`；约束尚未映射 → "没有探针用例" `skipped`，维度被迫 `NEEDS_HUMAN_REVIEW`。
- **需要 `active_suite_version_id`**：prepare 节点会调 `ensure_test_suite(extra_categories=[MULTI_SKILL])`
  并回写该键（REUSE 语义，只在 MULTI_SKILL 一条都没有时出题）。
- **成本**：每次运行最多 `2×max_hijack(5) + max_antagonism(6) + 2 + max_temporal(3) + 2×Σ核心样本(5×5)`
  = 71 次沙箱执行，外加 ≤ 6 + 2 + 1 = 9 次裁判调用。并发上限共用
  `SKILLEVAL_EXECUTOR_MAX_CONCURRENT_SANDBOXES`（三条支路共享一个信号量）。
- **报告透传**：`BenchmarkReport` 汇总时，`dimension_results.blocking=True` 的本维度结果应阻断合并
  （与其他维度的 blocking 同一口径）。

---

## 4. `22`：告警通道（本维度最主要的待接入项）

### 4.1 接口

```python
# src/skill_evaluate/observability/alerts.py
class AlertDispatcher(Protocol):
    async def send(self, *, alert_type: str, run_id: str, payload: dict[str, Any]) -> None: ...

set_alert_dispatcher(dispatcher)     # 应用启动时注册一次（进程级）
get_alert_dispatcher()               # 未注册时返回 LoggingAlertDispatcher（只写日志 alert_dispatched）
await dispatch_alert(dispatcher, alert_type=..., run_id=..., payload=...)   # 吞异常，返回是否成功
```

`MultiSkillDeps.alerts()` 在**每次调用时**回落到当前注册的分发器——主图可以先装配、后注册通道。
这是全项目的**通用**告警入口：`08` 冻结告警、`09` 挂起通知接入时建议也改走它。`21` 的生成坍塌告警已经走它（`alert_type="generation_collapse_persistent"`，见 `interfaces/21` 第 1.3 节）。

### 4.2 本维度发出的告警

- `alert_type = "deep_multi_skill_conflict"`（`ALERT_TYPE_DEEP_CONFLICT`）
- 触发：存在基石熔断，或软性冲突发现 ≥ `SKILLEVAL_MULTISKILL_DEEP_CONFLICT_ALERT_THRESHOLD`（默认 3）
- payload（全部 JSON 可序列化）：

```json
{
  "dimension": "multi_skill_conflict",
  "skill_id": "csv-cleaner", "skill_version_ref": "abc123",
  "blocking": true,
  "hard_findings": ["[基石熔断] 引入本 Skill 导致核心 Skill 'sql-runner' 触发率由 100% 跌至 40%（低于安全下限 80%）"],
  "soft_findings": ["[劫持] case-1 单测能触发目标 Skill，但挂载基准干扰包后未能触发", "..."],
  "noise_pack": ["excel-helper", "report-writer", "json-only"]
}
```

### 4.3 `22` 的分流（docs/dev/22 第 8.1 节原文口径）

- `payload.blocking == True` → `request_human_approval(RESOLVE_DEEP_CONFLICT)` + `suspend_and_wait()`。
  ⚠️ 挂起点应放在 `24` 的汇总节点（或 22 自己的节点）里读 `_multi_skill_alert_dispatched` /
  `dimension_results.blocking` 决定，**不要**在 `send()` 里调 `suspend_and_wait()`——`send()` 被
  `dispatch_alert()` 包在 try/except 里，`interrupt()` 抛出的 `GraphInterrupt` 会被当成通道故障吞掉。
- `payload.blocking == False` → 只发 Discord 通知 + 写 `PendingApproval(status="pending")`，不挂起。

### 4.4 工作台"单跑 vs 并发"双路比对

架构文档"分支差异比对"要的两条 Trace 都已落库，按 `(case_id, run_index)` 取：

| 场景 | 单跑 | 并发 |
|---|---|---|
| 触发劫持 / 背景过触发 | `RUN_INDEX_MULTI_SKILL_HIJACK_SOLO`=230 | `..._HIJACK_CROWDED`=231 |
| 注意力衰减 | `..._ATTENTION_SOLO`=233 | `..._ATTENTION_CROWDED`=234 |
| 基石熔断（case 属于核心 Skill） | `..._CORE_BASELINE`=236 | `..._CORE_CROWDED`=237 |
| 指令拮抗 / 语义断层（仅并发） | — | `..._ANTAGONISM`=232 |
| 时序扰动（打乱后） | 原顺序参照 = 232 | `..._TEMPORAL`=235 |

对应判定按 `subject_id` 前缀回查 `judge_verdicts`（量化判定只归档 FAIL）：
`multi_skill_namespace:` / `multi_skill_hijack:` / `multi_skill_overtrigger:` / `multi_skill_deadlock:` /
`multi_skill_flow:` / `multi_skill_attention_solo:` / `multi_skill_attention_crowded:` / `multi_skill_attention:` /
`multi_skill_role:` / `multi_skill_temporal:` / `multi_skill_core:<core_id>:<target_id>`。

---

## 5. 真实 Hermes 接入方：多技能挂载契约

已追加到 `interfaces/03_hermes_sandbox_client.md`「追加契约：多技能挂载」一节，要点：

1. `request.background_skills` 非空时，目标与每个背景 Skill **分别挂载到以 `skill_id` 命名的独立目录**
   （`.../skills/<skill_id>/SKILL.md`），全部注入 Agent 可见的技能列表；
2. **显式上报 `skill_md_loaded`（指目标 Skill）**；
3. 轨迹里保留 `read_file` 动作的真实路径。

归因规则（`executors/skill_attribution.py::attribute_skill_loads`）：SKILL.md 的**直接父目录名**命中
某个 Skill 的 `skill_id` 或 `root_path` 末级目录名即归给它；有归到目标的读取 → 已加载；`loaded_skill_md=True`
但读取全部归到背景技能 → `contradictory`（证据不足，交人工）；没有任何读取动作 → 采信 `loaded_skill_md`
（运行时预加载）。**不满足挂载约定的部署**会让劫持/过触发/熔断大量落入"证据不足"——不会给出错误结论，
但维度会长期 `NEEDS_HUMAN_REVIEW`。

`llama_control`（模块九）的请求体已带 `background_skills`，运行时同样应遵守本契约。

---

## 6. 运维侧：干扰包与基石清单

```bash
SKILLEVAL_MULTISKILL_NOISE_PACK_SKILL_IDS='["excel-helper","report-writer","json-only-api"]'
SKILLEVAL_MULTISKILL_CORE_SKILL_IDS='["sql-runner","git-helper","doc-writer","csv-cleaner","http-fetch"]'
```

- 清单里的技能必须**先入库**（`skill-evaluate` 的 skill 加载流程会 `SkillRepository.save()`）；未入库的被忽略并写进报告。
- 基石 Skill 需要各自有 active 用例集（对它们跑过一次评测即可），否则熔断对它"证据不足"。
- 被测 Skill 自己出现在清单里会被自动剔除（基石 Skill 发 PR 时不会拿自己当背景）。
- 干扰包建议 3~5 个，覆盖功能混淆 / 角色冲突 / 输出格式冲突；**固定**是 O(1) 成本的前提，不要每次换。

其余配置见 `config.py::MultiSkillSettings`（均为成本/证据强度旋钮）。阻断策略不是配置项
（`nodes/multi_skill/nodes.py::BLOCKING_OUTCOME_KEYS`）。

---

## 7. 判定与报告口径

| 事项 | 做法 | 为什么 |
|---|---|---|
| 判定入口 | 7 条量化规则 + 3 个 `judgmental_verdict()` 模板，全部 `ROUTINE` | `interfaces/08` 铁律；软发现不值 3 倍 Token，唯一阻断项是纯量化 |
| 参照臂自己就不对 | PASS，写进说明行 | 单测不触发是模块一的问题、单测就违反约束是模块三/八的问题 |
| 失败态 Trace | 证据不足 → `NEEDS_HUMAN_REVIEW`（时序扰动例外，见 docs/dev/20 第 9 节） | 超时的 `loaded=False` 会凭空制造劫持/熔断 |
| 基石熔断 | 并发触发率 < 下限 **且** < 独立执行触发率 | 不把核心 Skill 既有弱点算到本次提交头上 |
| 黄金盲测占用 | 该条无结论（证据不足） | `interfaces/08` 第 3 节 |
| 共识未达成 | 抛 `PipelineSuspended` | `NEEDS_HUMAN_REVIEW` 不允许降级 |
| `score` | `None` | 七类性质不同的检测硬凑分数没有含义 |
| `blocking` | 只有基石熔断为 True | docs/dev/20 第 11 节 |
| 维度状态 | FAIL（任一发现）> NEEDS_HUMAN_REVIEW（skipped / 证据不足 / 缺键）> PASS | 同其余维度 |

---

## 8. 本次对前序模块的修改（追加式，均向后兼容）

| 位置 | 修改 |
|---|---|
| `config.py` | 新增 `MultiSkillSettings`，挂 `Settings.multi_skill` |
| `state/trace.py` | 申领 run_index 230~237；后续维度从 **240** 起申领 |
| `persistence/repository.py` | `SkillRepository.get_latest(skill_id)` |
| `agents/generator/schema.py` / `agent.py` / `service.py` | `GenerationRequest.background_skills`；`ensure_test_suite(background_skills=...)` 透传；模板渲染变量 `background_skills` |
| `agents/generator/prompts/registry.py` | 内置注册 `MULTI_SKILL → multi_skill.jinja` |
| `agents/mini/templates/` | `multi_skill.py` + 三个 `.jinja` + 三个输出 Schema |
| `executors/skill_attribution.py` | 新增：多技能加载归因 |
| `executors/mini_backend.py` | `background_skills` 非空时告警并忽略 |
| `observability/alerts.py` | 新增：通用告警分发器接口 |

无数据库迁移。`triggered_by` 新增取值 `multi_skill_bootstrap`（`interfaces/06` 第 2 节）。

---

## 9. 留给后续文档的接入点

| 预留位置 | 当前状态 | 由哪份文档接入 | 接入方式 |
|---|---|---|---|
| 真实告警通道 | 日志默认实现 | `22` | 第 4 节 |
| 阻塞式人工介入（基石熔断） | `blocking=True` + 告警 payload | `22` / `24` | 第 4.3 节 |
| 工作台双路 Trace 比对 | 数据已落库 | `22` | 第 4.4 节 |
| 主图装配 | 平铺入口就位 | `24` | 第 0~3 节 |
| Hermes 多技能挂载 | 契约已登记 | 真实 Hermes 接入 | 第 5 节 |
| 跨技能状态突变侦测（环境变量 / 共用中间文件被覆写） | **未实现**（`21` 已落地但未覆盖：沙箱指纹只证明起跑时环境一致，见 `interfaces/21` 第 5 节） | 后续 | 需要沙箱在步骤间隙上报文件 Hash 与环境变量快照（`HermesHookPayload` 追加字段），现有 Trace 只有最终 `fs_diff`，无从判定"是谁在何时改的" |
| 注意力衰减 → Optimizer 精简 / 强制渐进式披露 | **未实现**（只告警） | 视团队策略 | 可复用模块三的渐进式披露建议；本维度不产生补丁 |
| MULTI_SKILL 多种组合手法的第二层注册表 | 未需要 | — | 若演化出多种手法，照 `agents/attacker/playbook.py` 抄（`interfaces/06` 第 3 节） |
