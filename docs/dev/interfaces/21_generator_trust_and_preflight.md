# 接入文档：Generator 可信度（反坍塌 + 种子锚点）与前置门禁（指纹 + 金丝雀）

> 由谁接入：`22`（坍塌告警 → `INJECT_NEW_SEED` 阻塞审批；前置门禁挂起的工作台展示）、
> `23`（`case_embeddings` 近邻检索、种子锚点混合检索）、`24`（主图 Phase 0 装配、状态 schema 合并、
> CI 模式选择、黄金指纹发布检查）、**运维侧**（种子库仓库、黄金指纹首次生成、镜像标识注入）、
> **真实 Hermes 接入方**（`run_environment_probe()` 契约）。
> 当前状态：Part A~D 全部落地，有测试覆盖（`tests/skill_evaluate/test_generator_trust.py` 26 条、
> `tests/skill_evaluate/test_preflight.py` 29 条；不碰库、不发真实 embedding 请求、不起沙箱）。
> 迁移：`0009_generator_trust_and_preflight`（`case_embeddings` / `generation_collapse_events` /
> `canary_probe_history` 三张新表）。

---

## 0. 三十秒上手

```python
# A. 前置门禁装进主图（docs/dev/24 Phase 0）
from skill_evaluate.nodes.preflight import (
    ENTRY_NODE, TERMINAL_NODE, PreflightDeps, PreflightState, add_preflight_nodes,
)
add_preflight_nodes(builder)                       # 只加两道门禁之间的边
builder.set_entry_point(ENTRY_NODE)                # "preflight.sandbox_fingerprint_gate"
for node in PHASE_A_ENTRY_NODES:
    builder.add_edge(TERMINAL_NODE, node)          # "preflight.canary_probe_gate" → 各维度入口

# B. 反坍塌与种子锚点：调用方什么都不用做
await TestSuiteService().ensure_test_suite(skill)  # 默认构造即接入了检测器与种子解析器
```

```bash
skill-evaluate db-init                                          # 跑到迁移 0009
skill-evaluate sync-seed-anchors                                # 评测前同步种子库（可选）
skill-evaluate preflight-fingerprint --output golden_fingerprint.json   # 首次生成黄金指纹（人工审核后提交）
```

---

## 1. Part A：反坍塌门禁（`agents/generator/collapse_detector.py`）

### 1.1 已替换的占位

`agents/generator/service.py::_check_generation_collapse()` **已删除**，改为
`TestSuiteService(collapse_detector=...)` 注入的 `CollapseDetector` 协议：

```python
class CollapseDetector(Protocol):
    async def assess(self, new_cases, *, inherited_case_ids=None) -> CollapseAssessment: ...
    async def persist(self, assessment) -> None: ...
```

`_generate_and_activate()` 的时序：`generate → assess → (未通过: 记事件/告警/抛异常) → split →
save_many(test_cases) → persist(case_embeddings) → activate`。**向量在用例落库之后写**（外键），
**被拒的批次不写向量**（废题不进历史分布）。

### 1.2 判定口径

| 检查 | 条件 | 阈值 |
|---|---|---|
| 批内雷同（实现阶段追加） | 批量 ≥ `min_batch_size_for_intra_check`(4) | 固定 `collapse_distance_threshold_initial`(0.15) |
| 新旧分布（正文口径） | 历史样本 ≥ `min_historical_samples`(5) | `initial → mature` 按历史总数线性插值（200 条成熟） |

放行/阻断原因见 `state.enums.CollapseReason`（`cold_start` / `diverse` / `disabled` / `empty_batch` /
`collapsed_vs_history` / `collapsed_intra_batch`），`test_suite_activated` 日志带 `collapse_check` 字段。

存量用例回填：`assess()` 会为 `inherited_case_ids` 里还没有向量的用例（最近 `historical_window` 条）
补算向量，已有项目不会永远停在冷启动。

### 1.3 失败语义：`GenerationCollapseError`

```python
class GenerationCollapseError(GenerationError):
    skill_id: str
    consecutive_collapses: int      # 自当前 active 版本 created_at 以来的坍塌事件数
    requires_human_seed: bool       # >= GeneratorTrustSettings.max_consecutive_collapses(3)
```

- 父类仍是 `GenerationError`：模块六/十等"接住 GenerationError 标记补盲耗尽"的调用方**无需改动**。
- 连续次数恰好达到上限时发**一次**告警（后续继续坍塌不重复发）：
  - `alert_type = "generation_collapse_persistent"`（`service.ALERT_TYPE_GENERATION_COLLAPSE`）
  - `run_id` = 本次 `generator_run_id`（出题不隶属于某一次流水线运行，CLI 也会触发）
  - payload：`skill_id / skill_version_ref / consecutive_collapses / max_consecutive_collapses /
    last_reason / avg_distance_to_history / intra_batch_distance / threshold / triggered_by / action_required`

### 1.4 配置（`SKILLEVAL_GENERATOR_TRUST_*`）

`collapse_check_enabled`、`collapse_distance_threshold_initial/mature`、`maturity_sample_count`、
`min_historical_samples`、`historical_window`、`min_batch_size_for_intra_check`、
`max_consecutive_collapses`、`embedding_model`（默认 `openai/text-embedding-3-small`）、
`embedding_dimensions`（**必须 = 1536**，与迁移列定长一致）、`embedding_batch_size`、
`embedding_max_input_chars`（默认 8000，超长的对抗题截断后再 embed，避免超出 embedding 模型上下文）。

⚠️ 默认开启且**没有 embedding 通道就无法出题**（`ConfigurationError`/`EmbeddingError` 原样上抛，
不伪造"通过"）。确实离线的开发机设 `SKILLEVAL_GENERATOR_TRUST_COLLAPSE_CHECK_ENABLED=false`，会留 warning。

---

## 2. Part B：种子锚点（`agents/generator/seed_anchors.py`）

### 2.1 仓库结构（运维侧初始化 `skill-evaluate-seed-anchors`）

```yaml
# manifest.yaml
- domain_tag: data_processing
  description: 表格、CSV、数据清洗类真实提问
  source: 2026-08 工单日志抽样，已脱敏（审计单 SEC-1234）
# anchors/data_processing.yaml
- id: dp-001
  prompt: 这个导出的表里好多重复行 帮我去一下
```

配置：`SKILLEVAL_GENERATOR_TRUST_SEED_REPO_URL / _SEED_REPO_REF / _SEED_CACHE_DIR / _SEED_ANCHOR_COUNT`。
同步：`skill-evaluate sync-seed-anchors`（git 同步逻辑与断言工具箱共用 `agents/git_repo_cache.py`）。

### 2.2 注入与溯源

- `GenerationRequest.seed_anchor_ids`：`None`（默认）= 按 `skill.description` 自动检索；`[]` = 显式不要；
  非空 = 按 `<domain_tag>/<id>` 精确取（查不到的告警忽略——docs/dev/06 简化版"把 id 当文本"的语义已废弃）。
- `GenerationRequest.seed_anchors`（新增）：`GeneratorAgent.generate()` 一次性解析填好，同一请求各类别共用。
- Prompt 里渲染为 `[<anchor_id>] <prompt>`；模型在 `GeneratedCase.seed_anchor_id`（新增）回填借鉴的 id，
  核对后写 `TestCase.seed_anchor_id = "<anchor_id>@<commit_sha>"`；编造的 id 记 `None`。
- 库不可用 / embedding 故障 → 空列表（增强手段，不阻断出题）；库结构非法 → `SeedAnchorLibraryError`。

### 2.3 `23` 的升级点

> ✅ **`23` 已落地**：`resolve_for_skill` 先走混合检索（按当前本地库 commit 过滤），零命中或故障回落下述原实现；
> `sync-seed-anchors` 同步后自动把锚点索引进 `search_documents(collection="seed_anchors")`。

`SeedAnchorResolver.resolve_for_skill(skill, count) -> list[SeedAnchor]` 就是正文的
`_resolve_seed_anchors`，**签名即契约**：`23` 只替换函数体为 `hybrid_search.search(..., collection="seed_anchors")`。
注意：锚点**不在** `case_embeddings` 里（该表 `case_id` 外键指向 `test_cases`），当前实现是进程内按
`(commit_sha, embedding_model)` 缓存锚点向量；`23` 需要在 `sync-seed-anchors` 时把锚点索引进 `search_documents`。

---

## 3. Part C/D：前置门禁（`nodes/preflight/`、`executors/canary.py`）

### 3.1 节点与状态

| 常量 | 取值 |
|---|---|
| `ENTRY_NODE` | `preflight.sandbox_fingerprint_gate` |
| `TERMINAL_NODE` | `preflight.canary_probe_gate` |
| `ROUTING_KEY` | `preflight`（`NODE_BACKEND_ROUTING` 已登记为 PLUGGABLE，装配期硬校验） |
| `INTERRUPT_BEFORE_NODES` | `[]` |

⚠️ docs/dev/24 正文里的 `"preflight.fingerprint"` / `"preflight.canary"` 与裸函数
`sandbox_fingerprint_gate` 是占位写法，请用上表常量与 `add_preflight_nodes()`。

主图 schema 必须并入 `PreflightState` 的两个私有键（否则被静默裁掉，金丝雀拿不到指纹摘要只能每次实跑）：

| 键 | 内容 |
|---|---|
| `_preflight_fingerprint_outcome` | `{status: passed|off, digest, golden_path}` |
| `_preflight_canary_outcome` | `{status: passed|skipped|off, image_ref, reasons, trace_id}` |

建议 `24` 把这两项摘要写进报告头部："本次评测在一个被证明可信的环境里运行 / 跳过了哪道证明"。

### 3.2 失败语义：`InfrastructureEnvironmentError(PipelineSuspended)`

- 字段：`gate`（`sandbox_fingerprint` | `canary_probe`）、`details`（差异/原因清单）。
- 触发：指纹不一致、**缺少黄金指纹**、黄金指纹非法、探测通道故障；金丝雀不可达/执行报错/校验不过。
- **不写 `dimension_results`**，不生成任何维度 FAIL。`24` 在主图入口按 `PipelineSuspended` 处理即可；
  `22` 可按子类区分"找运维修环境"与"找评测负责人仲裁"，建议走**通知**而不是 `INJECT_*` 类审批
  （修好环境后重跑整条流水线即可，没有"批准继续"的语义）。✅ `22` 已按此实现：guard 写一张非阻塞
  `ABANDON_RUN` 卡片 + 通知后原样上抛。

### 3.3 调度模式（`SKILLEVAL_PREFLIGHT_*`）

| 配置 | 取值 | 说明 |
|---|---|---|
| `FINGERPRINT_CHECK_MODE` | `hook_parallel`(默认) / `off` | `off` 仅限无真实沙箱的本地开发 |
| `CANARY_CHECK_MODE` | `nightly_or_image_change`(默认) / `every_run` / `off` | 默认：同镜像 24h 内成功过则跳过 |
| `SANDBOX_IMAGE_REF` | 镜像 digest | **CI 应注入**；未配置时回落为 `fingerprint:<指纹摘要>` |
| `CANARY_MAX_AGE_HOURS` / `CANARY_TIMEOUT_S` / `FINGERPRINT_PROBE_TIMEOUT_S` | 24 / 15 / 30 | |
| `GOLDEN_FINGERPRINT_PATH` | `golden_fingerprint.json` | 相对 CWD（CI 在仓库根执行） |

`24` 的 CI 建议：PR 流水线用默认模式并注入 `SANDBOX_IMAGE_REF`；Nightly 与 Dockerfile 变更时设
`CANARY_CHECK_MODE=every_run`（docs/dev/24 第 6 节那一步保留即可）。

### 3.4 金丝雀细节

- 探针技能随包分发：`executors/canary_skill/{SKILL.md,data.txt}`，`version_ref = canary-<内容哈希>`。
- `case_id = "__canary__:<run_id>"`：执行走普通 `execute()` → Hook 回调 → `execution_traces`，因此
  **`runs` 表里必须已有该 run_id**（Hook 端点据此算 thread_id），`24` 在进入 Phase 0 之前创建 run 记录。
- 判定比正文严：失败态 Trace / 未加载 SKILL.md / `content` 与 `data.txt` 不逐字一致，任一即失败。
- `HermesBackend.health_check()` **仍是轻量可达性检查**（模块九把它当廉价判断用）；金丝雀在
  `run_canary_probe(backend, run_id=..., timeout_s=...)`，它先调 `health_check()` 再执行任务。

### 3.5 黄金指纹（运维侧，一次性 + 每次镜像升级）

```bash
SKILLEVAL_EXECUTOR_BACKEND=hermes skill-evaluate preflight-fingerprint --output golden_fingerprint.json
git diff golden_fingerprint.json   # 人工审核后提交；本仓库当前**没有**该文件，门禁会如实失败
```

不带 `--output` 时打印并与现有黄金指纹比对（退出码 1 = 不一致），可用于镜像升级前预检。

---

## 4. 本次顺带修正的前序缺陷（请知悉）

1. **`HermesBackend.execute()` / `LlamaControlBackend` callback 模式吞掉了 `GraphInterrupt`**：
   `except Exception` 会把 LangGraph 的挂起信号记成失败态 Trace，所有真实沙箱执行都走不到"等 Hook 唤醒"。
   已改为 `except (ExecutorBackendError, GraphBubbleUp): raise`，有回归测试。
2. `agents/validator/toolbox.py` 的 git 同步与 YAML 记录列表解析提到 `agents/git_repo_cache.py`，
   toolbox 保留原私有名作别名，行为不变。

---

## 5. 待接入 / 未实现清单

| 预留位置 | 当前状态 | 由谁接入 | 接入方式 |
|---|---|---|---|
| 连续坍塌 → 阻塞审批 | ✅ `22` 已实现：节点级 guard 接成 `INJECT_NEW_SEED` 审批（仅冒出节点的坍塌；四处自行接住 `GenerationError` 的补题路径不升级，见 `interfaces/22` 第 1.1 节） | `22` | 在调用 `TestSuiteService` 的节点里按子类捕获，`request_human_approval(decision_type=INJECT_NEW_SEED)`；人工注入种子后 `sync-seed-anchors` 再恢复 |
| 真实告警通道 | ✅ `22` 已实现（Discord，CLI `generate` / `generate-attacks` 入口已注册） | `22` | `set_alert_dispatcher(...)`，按 `alert_type="generation_collapse_persistent"` 选卡片 |
| `case_embeddings` 近邻检索 | HNSW 索引已建，**仍未使用**（`23` 评估后未接：当前没有跨 Skill 近邻的调用方，记忆检索走独立的 `search_documents`） | 按需 | 同表 `embedding <=> :q` |
| 种子锚点混合检索 | ✅ `23` 已实现（混合检索优先，回落单一 embedding） | `23` | 见 `interfaces/23` 第 2 节 |
| 主图 Phase 0 | 平铺入口就位 | `24` | 第 0、3.1 节 |
| `run_environment_probe()` 真实实现 | 协议 + Unconfigured 显式报错 | 真实 Hermes 接入 | `interfaces/03` 「追加契约：环境指纹探测」 |
| 黄金指纹文件 | 未生成 | 运维侧 | 第 3.5 节 |
| 种子库仓库 | 结构约定已定义 | 运维侧 | 第 2.1 节 |
| 跨技能状态突变侦测（`interfaces/20` 第 9 节遗留） | **仍未实现** | 后续 | 需要 Hermes 在步骤间隙上报文件 Hash / env 快照（`HermesHookPayload` 追加字段）；本次的指纹探测只证明"起跑时环境一致"，证明不了"运行中谁改了什么" |
