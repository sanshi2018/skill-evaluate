# 接入文档：长时记忆与数据飞轮（混合检索 + 三处 Few-shot 闭环）

> 由谁接入：`24`（主图收尾节点 `rag_archive_if_passed`、CI 里的索引步骤与迁移 0011）、
> **运维侧**（Reranker 模型选型与权重缓存、分块参数、embedding 模型更换后的重建）。
> 当前状态：检索服务、四个集合、三处集成（Validator 模板 / 种子锚点 / Generator 冷启动 / Optimizer 经验）
> 全部落地；有测试覆盖（`tests/skill_evaluate/test_memory.py` 46 条，不碰库、不发真实 embedding 请求、
> 不加载 Cross-Encoder）。**唯一未接入的是 `archive_successful_run()` 的调用点**（等 `24` 的主图收尾节点）。
> 迁移：`0011_search_documents`（新表 `search_documents`）。

---

## 0. 三十秒上手

```python
# 24：主图收尾节点（Phase E 最后一个节点，排在 finalize.report / finalize.patch_pr 之后）
from skill_evaluate.memory.rag_archive import archive_successful_run

async def rag_archive_conditional(state: PipelineState) -> dict:
    try:
        outcome = await archive_successful_run(state["run_id"])   # 门槛判断在函数内部，见第 3 节
    except Exception as exc:                                      # 基础设施故障：记日志，不让收尾失败
        logger.warning("rag_archive_failed", run_id=state["run_id"], error=str(exc)[:300])
        return {}
    logger.info("rag_archive_outcome", **outcome.model_dump())
    return {}
```

```bash
skill-evaluate db-init                 # 跑到迁移 0011
skill-evaluate sync-toolbox            # git 同步后自动索引 assertion_templates
skill-evaluate sync-seed-anchors       # git 同步后自动索引 seed_anchors
skill-evaluate memory-index            # 不拉仓库，按本地缓存重建上面两个集合（--collection 可选）
pip install 'skill-evaluate[reranker]' # 可选：本地 Cross-Encoder 重排（会拉 torch）
```

其余三处集成（Validator / Generator / Optimizer）**调用方什么都不用做**：默认依赖在
`SKILLEVAL_MEMORY_ENABLED=true`（默认）时自动接入，任何记忆库故障都退化为 `23` 之前的行为。

---

## 1. 检索服务（`memory/hybrid_search.py`）

```python
from skill_evaluate.memory import HybridSearchService, MemoryCollection, SearchDocument

search = get_default_hybrid_search()           # 进程级单例（Reranker 只加载一次）
await search.index_many([SearchDocument(doc_id=..., collection=..., text=..., metadata={...})])
await search.sync_collection(MemoryCollection.SEED_ANCHORS, docs)   # 全量同步（仅外部仓库类集合）
hits = await search.search(query, MemoryCollection.OPTIMIZER_PATCH_HISTORY, top_k=3,
                           metadata_filter={"role": "appsec_expert"})
hits[0].score             # 0~1 相关度（有 Reranker = 概率；无 = 稠密相似度 / 归一化 ts_rank）
hits[0].score_breakdown   # dense_similarity / lexical_rank / rrf_score / rerank_score
```

### 1.1 四个集合

| `MemoryCollection` | 写入方 | 读取方 | 生命周期 |
|---|---|---|---|
| `assertion_templates` | `sync-toolbox` / `memory-index` | `AssertionToolbox._semantic_lookup()` | 与工具箱仓库全量同步 |
| `seed_anchors` | `sync-seed-anchors` / `memory-index` | `SeedAnchorResolver.resolve_for_skill()` | 与种子库全量同步 |
| `successful_skill_archive` | `archive_successful_run()`（**待 24 调用**） | `GeneratorAgent` 冷启动 | 只增不删 |
| `optimizer_patch_history` | `OptimizationLoop.run()` 结束时 | `OptimizerAgent.propose_patch()` | 只增不删 |

`sync_collection()` 对两个只增不删的集合直接抛 `ValueError`，防止误删历史。

### 1.2 降级语义

| 故障 | 行为 |
|---|---|
| `sentence-transformers` 未装 / 模型加载失败 / `RERANKER_ENABLED=false` | RRF 融合排序；`score_breakdown` 里**没有** `rerank_score`（不伪装） |
| 检索时 embedding 故障 | 只走 BM25 路，warning `memory_search_dense_degraded` |
| 数据库故障 | `search()` 原样上抛；三处集成点各自回落（见第 2 节） |
| 索引时 embedding 故障 | 原样上抛（`embedding` 列 NOT NULL） |

### 1.3 表结构（迁移 0011，与文档 23 第 2.1 节的偏差）

- 全文检索列 `text_search = to_tsvector('simple', lexical_text)`，**不是** `to_tsvector('english', text)`：
  `english` 解析器把整段中文当一个词元，中文 BM25 完全失效。`lexical_text` 由
  `lexical_tokens()` 预切分（英文按词、中文 2-gram），入库与查询共用同一个函数。
- 向量索引 HNSW（同 0009 的理由）；另有 `metadata` 的 GIN(`jsonb_path_ops`) 索引支撑 `metadata_filter`。
- 追加列：`embedding_model`（换模型后旧向量退出参照系）、`content_hash`（文本未变跳过 embed）、
  `lexical_text`、`updated_at`。
- ⚠️ Postgres 文本解析器对中文词元的识别依赖库的编码与 `lc_ctype`。docker-compose 使用的
  `pgvector/pgvector:pg16` 默认 `UTF8` + `en_US.utf8`，中文 2-gram 能被正确识别为词元；自建库请同样使用
  `UTF8` 编码，上线前用 `SELECT to_tsvector('simple', '表格 格式');` 确认输出两个词元。
  （本仓库单测用内存替身，**未**在真实 Postgres 上跑过该检查——当前环境无 Docker。）

---

## 2. 三处集成的现状（已替换的占位）

| 位置 | 原实现 | 现实现 | 回落条件 |
|---|---|---|---|
| `agents/validator/toolbox.py::_semantic_lookup` | 仅关键词（docs/dev/10） | 混合检索 top_k（`MEMORY_VALIDATOR_TEMPLATE_TOP_K`=3）与关键词分数**取 max** | 开关关闭 / 任何检索异常 → 纯关键词 |
| `agents/generator/seed_anchors.py::resolve_for_skill` | 进程内单一 embedding（docs/dev/21） | 混合检索，按**当前本地库 commit** 过滤 | 开关关闭 / 零命中（= 未索引或索引是旧 commit）/ 检索异常 → 21 原实现 |
| `GeneratorAgent.generate()` | 无 | 冷启动时注入历史范本（`GenerationRequest.archived_examples`，新增字段） | 开关关闭 / 检索异常 → 不注入 |
| `OptimizerAgent.propose_patch()` | 无 | 同角色相似经验 few-shot（成功 / 失败分段） | 开关关闭 / 检索异常 → 不注入 |
| `OptimizationLoop.run()` | 无 | 成功返回前、耗尽挂起前归档本轮**每一次**尝试 | 归档异常只 warning，不影响返回值 |

各类新增构造参数（全部可选，显式注入则无视总开关）：`AssertionToolbox(search_service=)`、
`SeedAnchorResolver(search_service=)`、`GeneratorAgent(cold_start_retriever=)`、
`OptimizerAgent(patch_memory=)`、`OptimizationLoop(patch_memory=)`。

### 2.1 Generator 冷启动的触发口径

同时满足才检索：`request.archived_examples is None`、`mode != INCREMENTAL_PATCH`、
`capability_focus is None`、categories 含 POSITIVE/NEGATIVE、**该 skill_id 在归档里没有任何记录**。
只有 `positive.jinja` / `negative.jinja` 渲染（`_shared.jinja::archive_block`），按当前类别只展示同类用例。
与种子锚点是两段独立 few-shot（`seed_block` 在前、`archive_block` 在后），不合并。

> 数据隔离说明：归档里包含 VALIDATION 用例原文，但一个 Skill 只会看到**其他** Skill 的归档
> （自己有归档即不检索），不会把自己的验证集题目泄漏回自己的出题 Prompt。

### 2.2 Optimizer 经验

- 检索 query 与入库 text 都是 `summarize_failure(ctx)`（角色、目标、description、失败用例、裁判理由、
  红队证据；**不含**正文全文），按 `metadata.role` 过滤。
- 归档口径比文档 23 正文宽：一轮闭环里 `apply_failed` / `regression_failed` / `regression_passed` 每次尝试
  各一条（doc_id = patch_id），不只是"最终 Patch"。
- 其他文档自行 `register_role()` 的角色模板若也想展示经验，`{% import "_shared.jinja" as shared %}` 后
  加一行 `{{ shared.past_patch_experience(few_shot_patches) }}` 即可（变量已统一传入）。

---

## 3. `24` 要做的事

### 3.1 收尾节点 `rag_archive_if_passed`

`archive_successful_run(run_id) -> ArchiveOutcome` 已实现，**门槛判断在函数内部**，主图不必自己判：

| `reason` | 含义 |
|---|---|
| （`archived=True`） | 已归档：description + 正文分块 + TRAIN/VALIDATION 用例 |
| `memory_disabled` | 总开关关闭，未访问数据库 |
| `run_not_found` | `runs` 表无此 run |
| `no_blocking_dimensions` | `dimension_results` 里没有任何 blocking 维度——无从证明"完全通过" |
| `blocking_dimensions_not_passed` | 有 blocking 维度不是 `pass`（**含 `needs_human_review`**），`failed_dimensions` 列出 |
| `skill_not_found` | `skills` 表无该版本 |
| `no_suite_version` | run 没绑用例集且无 active 版本 |

接入要点：

1. **放在所有维度结论落库之后**（`finalize.report` 之后），否则门槛判断读到的是不完整的维度集合。
2. 主图应在入口 `RunRepository.set_suite_version(run_id, ...)`（docs/dev/05 已有），否则回落到"当前
   active 版本"——并发补题后它可能已不是本次评测跑的那一版。
3. 基础设施故障（数据库 / embedding）会**上抛**：收尾节点按第 0 节示例自行 try/except，
   不要让一次归档失败把已经出完的报告变成失败的流水线。
4. 重复调用幂等（doc_id 由 `skill_id@version_ref` 推导），断点恢复重跑收尾节点无副作用。
5. 建议把 `ArchiveOutcome` 写进报告尾部或日志：飞轮停转（长期 `blocking_dimensions_not_passed`）应该被看见。

### 3.2 CI

| 步骤 | 说明 |
|---|---|
| `skill-evaluate db-init` | 迁移到 0011 |
| `sync-toolbox` / `sync-seed-anchors` | 现在会顺带建索引，**需要** DB 与 `SKILLEVAL_LLM_API_KEY`（embedding）；索引失败退出码 1 |
| Reranker | 评测 job 想要重排：安装 `[reranker]` 并缓存 `~/.cache/huggingface`（首次会下载权重）；不装也能跑 |
| 只跑静态维度的 job（`lint`） | 设 `SKILLEVAL_MEMORY_ENABLED=false`，完全不访问记忆库 |

---

## 4. 配置（`config.py::MemorySettings`，前缀 `SKILLEVAL_MEMORY_`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关；只影响默认依赖构造，显式注入的服务照常生效。单测由 `tests/conftest.py` 关闭 |
| `dense_k` / `bm25_k` | 20 / 20 | 两路召回条数 |
| `rrf_k` | 60 | 无 Reranker 时的 RRF 常数 |
| `reranker_enabled` | `true` | |
| `reranker_model` | `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` | 多语言轻量模型（中文内容多，纯英文 ms-marco 不适用）；运维可换 `BAAI/bge-reranker-v2-m3` |
| `reranker_device` / `reranker_max_length` | 自动 / 512 | |
| `chunk_max_tokens` | 800 | 单章节超过它才按段落二级切分 |
| `validator_template_top_k` | 3 | |
| `cold_start_example_count` / `cold_start_cases_per_example` | 3 / 4 | 每份范本**每个类别**最多几条 |
| `optimizer_patch_top_k` / `optimizer_patch_diff_max_chars` | 3 / 1500 | |

embedding 模型与维度复用 `GeneratorTrustSettings.embedding_model / embedding_dimensions`（必须 1536）。

---

## 5. 运维须知

- **更换 embedding 模型**：`memory-index` 重建模板与锚点；两个归档集合**没有源可重建**，旧模型的向量
  会被 `embedding_model` 过滤掉（稠密路查不到，BM25 路仍可命中），相当于经验库从新模型重新积累。
  如需保留历史，需要一个"读出 text → 重新 embed → upsert"的脚本（**未实现**，按需补）。
- **Reranker 权重版本**：文档 23 只锁接口；模型名写进配置即可，`score` 统一为 0~1 概率（输出若是 logit
  会自动补 Sigmoid），Validator 阈值 `template_match_threshold`（0.34）无需随模型调整。
- **HNSW + 过滤**：`dense_search` 带 `collection` / `embedding_model` / `metadata` 过滤时，候选数受
  `hnsw.ef_search`（默认 40）限制。集合很大且过滤很窄时可在会话里调高 `SET hnsw.ef_search = 100`。

---

## 6. 与文档 23 正文的出入（以代码为准）

| # | 正文 | 实现 | 原因 |
|---|---|---|---|
| 1 | `to_tsvector('english', text)`、ivfflat | `to_tsvector('simple', lexical_text)`、HNSW，追加 4 列 | 中文分词；空表 ivfflat 召回差 |
| 2 | `SearchDocument` 放 `memory/hybrid_search.py`，分数塞 `metadata["rerank_score"]` | 契约下沉 `state/memory.py`（`memory` 包再导出）；分数为独立字段 `score` / `score_breakdown` | 避免 persistence ↔ memory 循环导入；检索期分数不污染业务元数据 |
| 3 | `search(query, collection, top_k, dense_k, bm25_k)` | 追加关键字参数 `metadata_filter` | Optimizer 按角色、冷启动按 kind / archive_key 过滤 |
| 4 | `TemplateMatch(template=r.metadata["template_path"], score=rerank_score)` | 按模板名映射回当前 manifest 的 `TemplateMetadata`，与关键词分数取 max | 实际 `TemplateMatch.template` 是元数据对象；升级不应减少关键词版能命中的模板 |
| 5 | `_resolve_seed_anchors` 函数体整体替换 | 混合检索优先、零命中 / 故障回落 21 原实现，按 commit 过滤 | 索引未建或过期时不能让种子锚点整体失效 |
| 6 | 新增 `sync_assertion_toolbox` 子命令 | 并入既有 `sync-toolbox`（同步后自动索引）+ 新命令 `memory-index` | 避免"同步了仓库忘了建索引"；`db_init` 不做索引（需要 embedding 通道与网络） |
| 7 | `retrieve_few_shot_examples_for_cold_start -> list[dict]` | `-> list[ArchivedExample]` | 模板与测试依赖稳定字段；`model_dump()` 可得 dict |
| 8 | `archive_successful_run -> None` | `-> ArchiveOutcome` | 收尾节点需要知道"为什么没归档" |
| 9 | 归档"最终 Patch" | 归档每次尝试 | 前几轮失败的改法是最有价值的负面经验 |
| 10 | Reranker 必装依赖 `sentence-transformers>=3.0` | 可选依赖 `[reranker]`，缺失退化 RRF | torch 体积；只跑静态维度的环境不应被迫安装 |
| 11 | "全部 blocking 维度均 PASS" | 同时要求**至少存在一个** blocking 维度；`needs_human_review` 不算 PASS | 无结论不等于通过 |

顺带修复（与本文档无关的前序缺陷）：`agents/optimizer/patch_applier.py::_apply_code_patch` 只对 target 做
`resolve()`、未对 root 做，macOS 临时目录（`/var` → `/private/var` 符号链接）下合法路径被误判越界，
`test_code_patch_works_on_a_temp_copy_and_leaves_the_repo_untouched` 在 macOS 上一直失败。

---

## 7. 待接入 / 未实现清单

| 预留位置 | 当前状态 | 由谁接入 | 接入方式 |
|---|---|---|---|
| `archive_successful_run()` 调用点 | 函数已实现，**调用点未接入** | `24` | 第 3.1 节 |
| CI 迁移 0011 与索引步骤 | 命令已就绪 | `24` | 第 3.2 节 |
| Reranker 权重版本锁定 | 默认多语言 MiniLM | 运维 | 改 `SKILLEVAL_MEMORY_RERANKER_MODEL` |
| 分块参数调优 | 默认 800 token | 运维 | `SKILLEVAL_MEMORY_CHUNK_MAX_TOKENS` |
| 归档集合换 embedding 模型后的重算脚本 | 未实现 | 按需 | 第 5 节 |
| `case_embeddings` 库内近邻检索（interfaces/21 第 5 节） | HNSW 索引仍未使用 | 按需 | 坍塌检测按 skill_id 取最近 N 条在进程内算距离已足够，当前没有需要跨 Skill 近邻的调用方；本文档的记忆检索走 `search_documents`，不复用该表 |
| Analyzer 能力拆解的 few-shot（架构文档"Analyzer 检索优秀范本"） | 未实现 | 按需 | 可复用 `successful_skill_archive`：以新 Skill description 检索 `kind=profile`，取范本的能力树（需在归档时额外写入 `CapabilityTree` 摘要） |
