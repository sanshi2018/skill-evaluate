# 21 模块十一（续）：Generator 可信度与沙箱环境一致性证明

> 状态：**已实现**（2026-09-13；实现与本文正文的出入以代码为准，逐条见第 9 节）
> 路线图位置：第 3 层 / 第 1 份
> 依赖：`06`（`_check_generation_collapse()` 占位、`seed_anchor_ids` 简化版实现）、`03`（`health_check()` 接口）、`04`（Repository 层）、`05`（结构化日志/告警）
> 被依赖：`24`（主图装配——本文档的沙箱指纹/金丝雀探针作为整条流水线的最前置节点）

---

## 1. 本文档目标

补齐架构文档"裁判员的裁判"一节之外的另外两个"评测系统自身可信度"子节点：**Generator 测试集的可信度**（防生成坍塌 + 真实分布对齐）与 **Hermes 沙箱环境一致性证明**（指纹校验 + 金丝雀探针）。前者补齐文档 06 的两处占位实现，后者是整条流水线执行前的**前置质量门禁**，不属于任何单一评测维度，将在文档 24 中作为主图的第一个节点。

## 2. Part A：语义信息熵与生成坍塌监控

### 2.1 最小向量基础设施（先于文档 23 完整方案的必要子集）

文档 23（长时记忆与数据飞轮）会构建完整的混合检索（向量+BM25+Reranker），但本文档需要的只是"计算新旧用例集在向量空间中的分布距离"这一单一能力，不需要等待文档 23。本文档新建最小可用的 embedding 存储，供文档 23 后续**在同一张表基础上**扩展检索能力（而非各建一套）：

```sql
-- persistence/migrations/versions/00xx_case_embeddings.py
CREATE TABLE case_embeddings (
    case_id TEXT PRIMARY KEY REFERENCES test_cases(case_id),
    skill_id TEXT NOT NULL,
    embedding vector(1536) NOT NULL,   -- 维度对齐所选embedding模型
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX ON case_embeddings USING ivfflat (embedding vector_cosine_ops);   -- ⚠️ 实现改为 hnsw，见第 9 节
```

```python
# src/skill_evaluate/agents/generator/collapse_detector.py
async def _check_generation_collapse(new_cases: list[TestCase]) -> bool:   # 替换文档06占位实现（⚠️ 实现拆为 assess/persist 两步，见第 9 节）
    embeddings = await embedding_client.embed([c.prompt for c in new_cases])
    for case, emb in zip(new_cases, embeddings):
        await case_embedding_repository.save(case.case_id, case.skill_id, emb)

    historical = await case_embedding_repository.get_recent(skill_id=new_cases[0].skill_id, exclude_case_ids=[c.case_id for c in new_cases], limit=50)
    if len(historical) < 5:
        return True   # 历史样本不足，无法判定坍塌，放行(冷启动阶段天然应该宽容)

    distances = [1 - cosine_similarity(new_emb, hist_emb) for new_emb in embeddings for hist_emb in historical]
    avg_distance = statistics.mean(distances)
    threshold = settings.generator.collapse_distance_threshold   # 见2.2弹性阈值
    if avg_distance < threshold:
        logger.warning("generation_collapse_detected", skill_id=new_cases[0].skill_id, avg_distance=avg_distance, threshold=threshold)
        return False
    return True
```

### 2.2 弹性阈值机制

```python
class GeneratorTrustSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_GENERATOR_TRUST_")
    collapse_distance_threshold_initial: float = 0.15   # 冷启动宽容阈值
    collapse_distance_threshold_mature: float = 0.35     # 数据飞轮成熟后的严格阈值
    maturity_sample_count: int = 200                      # 历史样本量超过此值视为"成熟"
```

```python
def _current_collapse_threshold(skill_id: str, historical_count: int) -> float:
    """样本量线性插值，从initial逐步收紧到mature——落实架构文档
    "在系统冷启动时设定较宽容的阈值，随着数据飞轮的转动逐步收紧标准"的建议，
    不是简单的if/else两档切换，而是连续插值，避免阈值突变造成的评测结果跳变。"""
```

**坍塌阻断后的处理**：`_check_generation_collapse()` 返回 `False` 时，`generator_service._generate_and_activate()`（文档 06）不 `activate_new_version()`，即"拒绝采纳这批废数据"，同时按架构文档"应对方案"记录一次坍塌事件（`generation_collapse_events` 表，本文档新增），若同一 `skill_id` 连续 3 次坍塌（`GeneratorTrustSettings.max_consecutive_collapses`），触发通知（复用文档 20 已建立的 `alert_dispatcher`）呼叫人类手动注入新种子——这是本文档对架构文档"当 Generator 真正陷入瓶颈时，通过发送通知...呼叫人类开发者手动注入新的种子数据来打破僵局"的具体落地。

## 3. Part B：真实分布对齐——种子锚点扩散

### 3.1 种子库版本控制

```
skill-evaluate-seed-anchors/          # 独立GitHub仓库，结构类比文档10的断言工具箱
├── anchors/
│   └── <domain_tag>.yaml              # 按领域分类的脱敏真实Prompt集合
├── manifest.yaml                        # 领域标签、来源说明(脱敏审计记录)
└── CHANGELOG.md
```

```python
# 对文档06 GenerationRequest.seed_anchor_ids 使用方式的升级（简化版→正式版）
async def _resolve_seed_anchors(skill: SkillDefinition, count: int) -> list[SeedAnchor]:
    """按skill.description的领域标签，从种子库中检索(复用2.1的向量表做语义匹配，
    与_check_generation_collapse共用同一套embedding_client，不重复建模型调用逻辑)
    最相关的count条真实Prompt锚点，返回为文档06 Prompt模板可直接使用的few-shot示例。
    种子库的具体commit ref随GenerationRequest一并记录在TestCase的seed_anchor_id字段
    (文档02已有此字段)，保证可追溯到具体是哪条真实数据启发了这条生成用例。"""
```

种子库仓库同步机制与文档 10 断言工具箱一致（`toolbox_ref` 锁定、CI 定时/按需同步），本文档不重复设计同步逻辑，直接复用文档 10 已建立的模式（新增 `GeneratorTrustSettings.seed_repo_url`/`seed_repo_ref` 配置项）。

## 4. Part C：沙箱环境指纹校验（流水线最前置节点）

```python
# src/skill_evaluate/nodes/preflight/fingerprint.py
class SandboxFingerprint(BaseModel):
    os_kernel: str
    runtime_versions: dict[str, str]     # {"python": "3.13.1", "node": "22.1.0", ...}
    key_env_vars_snapshot: dict[str, str]  # 只快照非敏感的结构性env(如PATH结构)，不含密钥
    core_package_hashes: dict[str, str]    # 关键依赖包的hash

async def probe_current_fingerprint() -> SandboxFingerprint:
    """通过HermesBackend发起一次极轻量的探测任务(不涉及被测Skill)，
    在沙箱内运行固定的探测脚本(scripts/env_fingerprint_probe.sh，本文档新增，
    随基础镜像一并维护)，收集上述字段。"""

async def sandbox_fingerprint_gate(state: PipelineState) -> PipelineState:
    current = await probe_current_fingerprint()
    golden = load_golden_fingerprint()   # 从仓库中的 golden_fingerprint.json 读取(版本控制，人工审核后更新)
    mismatches = _diff_fingerprint(current, golden)
    if mismatches:
        raise PipelineSuspended(f"基础设施环境异常: {mismatches}")   # 直接阻断执行，不进入任何评测维度
    return state
```

`golden_fingerprint.json` 纳入项目仓库版本控制（不是外部工具箱仓库，因为它与本项目的基础镜像强绑定），更新该文件本身是一个需要人工审慎操作的动作（升级基础镜像/依赖版本时同步更新），本文档不设计自动更新机制——指纹校验的价值恰恰在于"不允许静默漂移"。

## 5. Part D：金丝雀技能探针

```python
# 复用文档03 ExecutorBackend.health_check()接口(此前一直待接入)
class HermesBackend(ExecutorBackend):
    async def health_check(self) -> bool:   # ⚠️ 实现未把金丝雀放进 health_check，见第 9 节
        canary_skill = load_canary_skill()   # 固定的、极简单确定性的探针技能定义(本文档新增,
                                                # 如"读取本地文本文件并输出固定JSON结构")
        canary_case = TestCase(case_id="__canary__", skill_id="__canary__", category=TestCaseCategory.POSITIVE,
                                split=DatasetSplit.TRAIN, prompt="请读取data.txt并输出其内容的JSON包装",
                                generator_run_id="__canary__", created_at=utcnow())
        try:
            trace = await self.execute(ExecutionRequest(skill=canary_skill, case=canary_case, run_index=0, wall_clock_timeout_s=15))
            return trace.loaded_skill_md and '"content"' in trace.final_response   # 固定预期结构的确定性校验
        except Exception:
            return False
```

```python
async def canary_probe_gate(state: PipelineState) -> PipelineState:
    healthy = await executor_backend.health_check()
    if not healthy:
        logger.error("canary_probe_failed", run_id=state["run_id"])
        raise PipelineSuspended("金丝雀探针执行失败，沙箱I/O/网络/基础引擎可能已损坏，废弃当次评测")
    return state
```

## 6. 前置节点的调度策略（对齐架构文档"应对方案"）

架构文档明确指出指纹校验+金丝雀探针会增加冷启动耗时，建议差异化调度：

```python
class PreflightSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SKILLEVAL_PREFLIGHT_")
    fingerprint_check_mode: str = "hook_parallel"    # "hook_parallel"：随Hermes初始化Hook并行执行，本文档默认
    canary_check_mode: str = "nightly_or_image_change"  # "every_run" | "nightly_or_image_change"，默认后者
```

`canary_check_mode="nightly_or_image_change"` 时，`canary_probe_gate` 节点内部先检查一个"上次金丝雀探针成功时间戳+当时的镜像hash"的记录（`canary_probe_history` 表），若镜像未变且距上次成功探针未超过 24 小时，直接跳过（返回 `state` 不做实际探测）——本文档实现该跳过逻辑，供文档 24 决定默认在 CI 中采用哪种模式（开发者密集连续提交期间可切换为该模式节省时间，Nightly Build 固定跑一次）。

## 7. 前置节点在主图中的位置

```
sandbox_fingerprint_gate → canary_probe_gate → （其余全部评测维度子图 11~20 并行/串行展开）
```

本文档不属于任何 `DimensionResult`（不产出评测报告条目），而是**流水线本身能否继续运行的先决条件**——失败即整体 `PipelineSuspended`，不生成"某维度 FAIL"的报告，因为此时的失败与被测 Skill 质量无关，是评测系统自身基础设施问题（这也是文档 03 定义 `ExecutorBackendError` 与业务判定失败分离的设计初衷在这里的又一次体现）。

## 8. 待接入文档（本文档留给后续模块的接口清单）

| 预留位置 | 当前状态 | 由哪份文档接入 | 接入方式 |
|---|---|---|---|
| `case_embeddings` 表 | 本文档建表，仅供坍塌检测/种子检索最小使用 | `23` | 扩展为完整混合检索基础设施（新增 BM25 索引、Reranker 调用层），复用同一张表不重建 |
| `skill-evaluate-seed-anchors` 仓库 | 结构约定已定义 | 运维侧初始化 | 按第 3.1 节结构创建并录入首批脱敏种子 |
| `golden_fingerprint.json` | 需要人工首次生成 | 本文档实现阶段：运行一次 `probe_current_fingerprint()` 后人工确认写入仓库 | — |
| `env_fingerprint_probe.sh` / 金丝雀探针 SKILL.md | 本文档新增，随基础镜像维护 | 本文档实现阶段完成 | — |
| `PreflightSettings` 在 CI 中的具体模式选择 | 默认值已给出 | `24` | 按 CI 触发场景（PR/Nightly/镜像变更）选择对应模式 |

---

## 9. 实施记录（与正文的出入，以代码为准）

接入文档：`docs/dev/interfaces/21_generator_trust_and_preflight.md`。

| # | 正文 | 实现 | 原因 |
|---|---|---|---|
| 1 | 判定前先把新题向量写库 | `CollapseDetector.assess()` 只算不写；用例落库、激活前再 `persist()` | `case_embeddings.case_id` 外键指向 `test_cases`，判定时新题尚未落库；且被拒的废题不能进入历史分布，否则下次越比越像 |
| 2 | `_check_generation_collapse()` 替换函数体 | 删除占位函数，改为 `TestSuiteService(collapse_detector=...)` 注入 | 需要 embedding 客户端与两张表，模块级函数无法注入替身；两步时序也不是一个 bool 能表达的 |
| 3 | 只做新旧分布对比 | 追加**批内两两距离**检查（批量 ≥4，固定 initial 阈值） | 首次生成历史为空必然冷启动放行，18 条雷同题会成为第一版测试集并永久充当"历史分布" |
| 4 | `_current_collapse_threshold(skill_id, historical_count)` | `current_collapse_threshold(historical_count)` | 阈值只取决于样本量，多一个不用的参数会让人误以为有按 Skill 定制的阈值 |
| 5 | —— | 历史向量按 `embedding_model` 过滤；存量用例自动回填 | 换模型后新旧向量不在同一空间；不回填则已有项目永远停在冷启动 |
| 6 | 连续坍塌计数 | "自当前 active 版本 `created_at` 以来的坍塌事件数"，达到上限**恰好一次**告警，抛 `GenerationCollapseError(requires_human_seed=True)` | 成功激活天然清零，无需独立计数器；阻塞挂起属于 22 |
| 7 | `ivfflat` 索引 | `hnsw` 索引 | ivfflat 在建索引时按已有数据训练聚类中心，迁移时表为空，召回率极差且不会自动改善 |
| 8 | 种子检索"复用 2.1 的向量表" | 进程内按 `(commit_sha, embedding_model)` 缓存锚点向量 | 锚点不是测试用例，外键不允许入 `case_embeddings`；23 会把锚点索引进 `search_documents` |
| 9 | `seed_anchor_ids` 简化版语义（id 当文本） | `None`=自动检索 / `[]`=不要 / 非空=按 id 精确取；模型回填 `seed_anchor_id`，核对后写 `<anchor_id>@<commit_sha>` | 让每条用例能溯源到具体锚点与种子库版本 |
| 10 | `scripts/env_fingerprint_probe.sh` | `src/skill_evaluate/nodes/preflight/env_fingerprint_probe.sh` | 脚本正文由本系统下发给沙箱，必须随包分发（`scripts/` 不进 wheel） |
| 11 | 探测"通过 HermesBackend 发起一次轻量任务" | `HermesSandboxClient.run_environment_probe()`（同步请求-响应，新增协议方法） | 探测不涉及 Skill，包装成 `execute()` 会产生假 Trace，且 CLI 生成黄金指纹时没有图上下文可挂起 |
| 12 | 金丝雀写进 `HermesBackend.health_check()` | `executors/canary.py::run_canary_probe()`；`health_check()` 保持轻量可达性检查 | 模块九已把 `health_check()` 当廉价判断使用；金丝雀执行需要图上下文（挂起等 Hook） |
| 13 | 金丝雀校验 `'"content"' in final_response` | 失败态 Trace / 未加载 SKILL.md / `content` 与 `data.txt` 不逐字一致均判失败 | 文件读不到时模型最常见的行为是编一个 content |
| 14 | `raise PipelineSuspended(...)` | `InfrastructureEnvironmentError(PipelineSuspended)`，带 `gate` / `details` | 主图按挂起处理不用改；22 可区分"修环境"与"业务仲裁" |
| 15 | `return state` | 节点只返回私有键增量 | 返回整个 state 会让 add-reducer 字段翻倍 |
| 16 | 缺少黄金指纹时的行为未定义 | 门禁失败并提示 `skill-evaluate preflight-fingerprint --output ...` | 没有基线就证明不了"没有漂移" |
| 17 | 两种模式 | 指纹 `hook_parallel`/`off`，金丝雀 `every_run`/`nightly_or_image_change`/`off`；镜像标识取 `SANDBOX_IMAGE_REF`，否则回落为指纹摘要 | `off` 仅限无真实沙箱的本地开发，且会写进状态、不静默 |
| 18 | 节点名 `preflight.fingerprint` / `preflight.canary`（docs/dev/24） | `preflight.sandbox_fingerprint_gate` / `preflight.canary_probe_gate`（`ENTRY_NODE` / `TERMINAL_NODE`） | 与各维度"前缀.函数名"命名一致 |

顺带修正的前序缺陷：`HermesBackend.execute()` 与 `LlamaControlBackend` callback 模式的 `except Exception`
吞掉了 LangGraph 的 `GraphInterrupt`，挂起信号被记成失败态 Trace（docs/dev/03 / 19 遗留），已修正并加回归测试。

## 下一步

待你确认本文档后，我将输出 **文档 22：容错机制与人工审批闭环**——正式落地贯穿本项目多处（08 冻结告警、09 最大重试挂起、17 孤儿用例建议、20 深度冲突告警）的人工协作机制。
