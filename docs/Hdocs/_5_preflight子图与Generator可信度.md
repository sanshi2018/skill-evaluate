# preflight 子图与 Generator 可信度：评测系统先证明自己靠得住（模块十一续）

> 代码位置：
> - 前置门禁子图：`src/skill_evaluate/nodes/preflight/`（`fingerprint.py`、`nodes.py`、`graph.py`、`deps.py`、`state.py`、`env_fingerprint_probe.sh`）
> - 金丝雀探针本体：`src/skill_evaluate/executors/canary.py` + `executors/canary_skill/`
> - 反坍塌门禁：`src/skill_evaluate/agents/generator/collapse_detector.py`（挂在 `service.py::_generate_and_activate()`）
> - 种子锚点：`src/skill_evaluate/agents/generator/seed_anchors.py`（挂在 `agent.py::generate()`）
> - embedding 客户端：`src/skill_evaluate/agents/embedding.py`
>
> 设计文档：`docs/dev/21_模块十一续_Generator可信度与沙箱环境一致性证明.md`（第 9 节是实现与正文的出入）
> 接入文档：`docs/dev/interfaces/21_generator_trust_and_preflight.md`

---

## 1. 一句话说清楚

前面十个模块都在给**被测 Skill** 打分。文档 21 要回答的是另一个问题：**打分的这套系统本身可信吗？**

分数不可信有两种来源，文档 21 各设了一道关：

| 不可信的来源 | 后果 | 对应的关 |
|---|---|---|
| **考卷有问题**：Generator 出了一批彼此雷同、或跟历史题雷同的题 | 通过率虚高，看起来"覆盖很全"，其实只考了一道题 | **反坍塌门禁** + **种子锚点**（嵌在出题流程里，不是图节点） |
| **考场有问题**：沙箱镜像悄悄升了个依赖、文件权限坏了、网络被墙了 | 大面积"失败"，全算到 Skill 头上，其实是环境坏了 | **preflight 子图**：指纹门禁 → 金丝雀门禁（主图最前面的两个节点） |

两道关的共同原则：**出了问题就拦下来、说清楚为什么，绝不"先放行再说"**。一次错误的放行，比一次多余的拦截代价大得多——前者会产出一份看起来正常、实则毫无意义的报告。

---

## 2. 设计目的

| 目的 | 具体做法 |
|---|---|
| **考卷不能坍塌** | 新题算 embedding，和历史题比、彼此之间比，平均距离太近就拒绝激活 |
| **考卷要像真人问的** | 从 Git 托管的脱敏真实 Prompt 库里检索最相关的几条作 few-shot，并记录每道题借鉴了哪条 |
| **防止死锁在"阈值太严"上** | 阈值随历史样本量从宽松（0.15）线性收紧到严格（0.35）；连续坍塌 3 次就叫人补种子，不让机器无限重试 |
| **考场不能静默漂移** | 沙箱环境指纹与仓库里人工确认过的"黄金指纹"逐项严格比对，差一个小版本也拦 |
| **考场的物理 I/O 必须真的能用** | 跑一个"永远不该失败"的金丝雀技能，读文件、输出固定 JSON，内容逐字比对 |
| **环境故障不能冒充 Skill 失败** | 门禁失败抛 `InfrastructureEnvironmentError`，整条流水线挂起，**不写任何维度结果** |
| **控制冷启动成本** | 指纹探测契约要求在 Hermes 初始化 Hook 里并行跑；金丝雀默认"同一镜像 24 小时内成功过就跳过" |

---

## 3. 在整个评测流程中的位置

```
                         ┌────────────────────────────────────────────┐
                         │  Phase 0：preflight 子图（本文档，串行）     │
  评测运行开始  ───────▶ │  sandbox_fingerprint_gate                  │
  （runs 记录已创建）     │           ↓                                │
                         │  canary_probe_gate                         │
                         └───────────────────┬────────────────────────┘
                                             │ 两道都过了才往下走
                                             │ 任一失败 → 整条流水线挂起
                                             ▼
  Phase A  trigger_accuracy / context_scoping / script_usability / security   ← 各维度 prepare 节点
                  │                                                              调 TestSuiteService
                  │   ┌──────────────────────────────────────────────────┐        出题/复用题
                  └──▶│ 出题时（仅在真的调用 LLM 生成时）：                 │
                      │   种子锚点注入 → 生成 → 反坍塌门禁 → 入库/激活       │ ← 本文档的另一半
                      └──────────────────────────────────────────────────┘
  Phase B  instruction_control / coverage / cross_model
  Phase C  pruning → weighted_coverage
  Phase D  multi_skill
  Phase E  报告 → 补丁转 PR → RAG 归档
```

要点：

- **preflight 是图节点**，位于主图入口，只跑一次，决定"这次评测还要不要跑下去"。
- **反坍塌与种子锚点不是图节点**，它们嵌在 `TestSuiteService` / `GeneratorAgent` 里。任何维度（模块一首次生成、模块三/五/十补类别、模块六/七/八补盲区）以及 CLI `generate --force` 只要真的出题，都会自动经过这两关，调用方一行代码不用改。
- 两者都**不属于任何评测维度**：不写 `dimension_results`，不出现在"十个维度得分"里。

---

## 4. preflight 子图的节点

子图只有两个节点，一条直线，没有条件边，没有 `interrupt_before`：

```
preflight.sandbox_fingerprint_gate   （ENTRY_NODE）
            ↓
preflight.canary_probe_gate          （TERMINAL_NODE）
```

为什么串行而不并行：
1. 金丝雀的"跳过判定"要用指纹摘要充当镜像标识；
2. 指纹都对不上了，环境已被证明不可信，再在上面跑任务只会多一份噪声。

### 4.1 `preflight.sandbox_fingerprint_gate`——沙箱环境指纹校验

**回答：这次跑评测的沙箱，和我们认可的那个沙箱，是不是一模一样？**

```
读配置 fingerprint_check_mode
   │
   ├─ off ───────────▶ 打 warning，状态写 {status: "off"}，放行（仅限无真实沙箱的本地开发）
   │
   ▼ hook_parallel（默认）
读仓库里的 golden_fingerprint.json
   │
   ├─ 文件不存在 ────▶ 失败："缺少黄金指纹，无法证明没有漂移"（附生成命令）
   ├─ 文件内容非法 ──▶ 失败
   ▼
下发 env_fingerprint_probe.sh 到沙箱执行（HermesSandboxClient.run_environment_probe）
   │
   ├─ 探测通道故障 / 脚本非零退出 / 输出不是合法 JSON ──▶ 失败："指纹探测失败"
   ▼
过滤密钥类环境变量名（KEY/TOKEN/SECRET/PASSWORD/...）
   ▼
与黄金指纹逐键严格比对（多了、少了、不同，都算差异）
   │
   ├─ 有差异 ────────▶ 失败："基础设施环境异常"，details 列出每一项差异
   ▼
状态写 {status: "passed", digest: <指纹 sha256>}，放行
```

**指纹里有什么**（`SandboxFingerprint`）：

| 字段 | 内容 | 例子 |
|---|---|---|
| `os_kernel` | 内核 + 发行版 | `Linux 6.8.0 \| debian 12` |
| `runtime_versions` | 各运行时版本（工具不存在就没有这个键） | `python / pip / node / npm / git / bash / uv / java` |
| `key_env_vars_snapshot` | 白名单里的结构性环境变量 | `PATH / LANG / LC_ALL / TZ / HOME / SHELL / USER / PYTHONPATH / NODE_PATH / VIRTUAL_ENV` |
| `core_package_hashes` | 包清单排序后的 sha256 | `pip list`、`npm ls -g`、`dpkg-query` 或 `apk info` |

**几个关键设计：**

- **严格到"小版本"**：不做语义版本兼容判断。"不允许静默漂移"就是这道门的全部价值，任何宽容规则都会变成下次事故复盘里"当时为什么没拦住"的答案。
- **没有基线就算失败**，而不是跳过——没有参照就证明不了"没变"。
- **探测脚本是纯 POSIX sh**：基础镜像里不一定有 Python/jq，探测脚本不能自己成为新的环境依赖。环境变量要在脚本把 `LC_ALL` 固定为 C **之前**采集，否则快照到的是脚本自己设的值。
- **黄金指纹只能人更新**：`skill-evaluate preflight-fingerprint --output golden_fingerprint.json` 生成 → `git diff` 人工审核 → 提交。流水线里没有任何自动更新它的路径。
- **探测走同步请求**，不走 Hook 挂起：CLI 生成黄金指纹时没有图上下文可挂起。

### 4.2 `preflight.canary_probe_gate`——金丝雀技能探针

**回答：沙箱的读文件、执行引擎、Hook 回调这条链路，此刻真的是通的吗？**

指纹一致只能说明"装的东西没变"，不能说明"东西能用"——磁盘权限、网络策略、Hermes 引擎本身都可能坏。金丝雀就是真跑一次最简单的任务来验证。

```
没有 run_id ──────▶ ConfigurationError（探针的 case_id 和 Hook 回调都要用它）

读配置 canary_check_mode
   │
   ├─ off ──────────────────▶ warning，状态写 {status: "off"}，放行
   │
   ▼
算镜像标识 image_ref = SANDBOX_IMAGE_REF 配置（CI 注入镜像 digest）
                    或  "fingerprint:<上一个节点的指纹摘要>"
                    或  None（拿不到就无法判断"镜像变没变"）
   │
   ├─ 模式 = nightly_or_image_change（默认）且 image_ref 不为 None
   │     查 canary_probe_history：同一 image_ref 最近一次**成功**的探针
   │     └─ 存在且在 24 小时内 ─▶ 状态写 {status: "skipped", reasons: [跳过依据]}，放行
   │
   ▼ （every_run 模式 / 镜像变了 / 超过 24h / 从没成功过）
run_canary_probe()
   ├─ backend.health_check()   —— 轻量可达性检查，不可达直接判失败，不起沙箱
   ├─ backend.execute(金丝雀请求) —— 真实起沙箱；PLUGGABLE 后端在这里挂起等 Hook 回调
   └─ verify_canary_trace()
   │
写 canary_probe_history（成功、失败都写）
   │
   ├─ 失败 ─▶ 抛 InfrastructureEnvironmentError(gate="canary_probe", details=原因清单)
   ▼
状态写 {status: "passed", image_ref, trace_id}，放行
```

**金丝雀技能**（随包分发，`executors/canary_skill/`）：

- `SKILL.md`：读同目录下的 `data.txt`，只输出 `{"content": "<文件内容>"}`；不写文件、不联网。
- `data.txt`：`skill-evaluate canary probe: sandbox io ok`
- `version_ref = canary-<两份文件内容的哈希>`（装成 wheel 后没有 git，内容哈希正好回答"探针本身被改过没"）
- `case_id = __canary__:<run_id>`：不同运行的探针 Trace 不会互相覆盖。

**判定比设计文档原文更严，三条任一不满足即失败：**

1. Trace 不是失败态（最后一个动作不是 `internal_error` / `sandbox_timeout`）；
2. 金丝雀的 `SKILL.md` 确实被加载了；
3. 从最终回复里能解析出 JSON（允许包在代码块里），且 `content` 与 `data.txt` **逐字相等**。

原文只检查 `'"content"' in final_response`。问题在于：文件读不到时，模型最常见的反应恰恰是**编一个 content**。只看字段存在会把这种情况判成健康。

**为什么金丝雀没有写进 `HermesBackend.health_check()`**：模块九已经把 `health_check()` 当作"备用代理能不能用"的廉价判断在调；而金丝雀要真起沙箱、要在图里挂起等 Hook。合二为一会让 `health_check()` 在图外调用必然失败。

### 4.3 子图的状态

两个节点都**只返回自己的私有键增量**（不 `return state`，否则 add-reducer 字段会翻倍）：

| 私有键 | 写入者 | 内容 | 用途 |
|---|---|---|---|
| `_preflight_fingerprint_outcome` | 指纹门禁 | `{status: passed\|off, digest, golden_path}` | 金丝雀用 digest 当镜像标识；报告头部展示 |
| `_preflight_canary_outcome` | 金丝雀门禁 | `{status: passed\|skipped\|off, image_ref, reasons, trace_id}` | 报告头部展示"本次跳过了哪道证明、为什么" |

⚠️ 主图 schema 必须并入 `PreflightState`，否则这两个键会被 LangGraph 静默裁掉，金丝雀拿不到指纹摘要，只能每次都实跑。

### 4.4 失败了会怎样

```
InfrastructureEnvironmentError  ⊂  PipelineSuspended  ⊂  SkillEvaluateError
    .gate    = "sandbox_fingerprint" | "canary_probe"
    .details = ["core_package_hashes.python_packages: 期望 'aaa'，实际 'bbb'", ...]
```

- 主图按 `PipelineSuspended` 处理——**整条流水线挂起**，后面十个维度一个都不跑。
- 不写 `dimension_results`，不会出现"触发准确度 FAIL"这种把环境故障算到 Skill 头上的结论。
- 单独建子类是为了让审批工作台（文档 22）能区分：这是"找运维修环境"，不是"找评测负责人仲裁"。修好环境重跑即可，没有"批准继续"的语义。

---

## 5. 考卷这一半：反坍塌门禁与种子锚点

这部分不在图里，而是一次"真的调用 LLM 出题"的内部流程。先回忆一下出题的三种模式（文档 06）：`REUSE` 只在从没出过题时才出一次、`FORCE_REGENERATE` 只能人触发、`INCREMENTAL_PATCH` 由覆盖率等维度按盲区补题。**无论哪种，只要真出题，都走下面这条流程**；纯复用已有题则完全不经过。

### 5.1 出题主流程（`TestSuiteService._generate_and_activate()`）

```
GenerationRequest（skill、类别、条数、focus、triggered_by ...）
   │
   ▼
① GeneratorAgent.generate()
   ├─ _attach_seed_anchors()        —— 种子锚点解析（见 5.3），整个请求只解析一次
   └─ 按类别逐个调 LLM 出题            —— Prompt 里渲染 "[data/dp-001] 真实用户提问..."
                                      模型回填 seed_anchor_id，核对后写 "<id>@<commit>"
   │
   ▼
② CollapseDetector.assess(new_cases, inherited_case_ids)   —— 只算不写（见 5.2）
   │
   ├─ 不通过 ─▶ _reject_collapsed_batch()
   │             ├─ 写 generation_collapse_events
   │             ├─ 连续次数 = 自当前 active 用例集版本创建以来的坍塌事件数
   │             ├─ 恰好 == 3 次 ─▶ 发告警 generation_collapse_persistent（只发一次）
   │             └─ 抛 GenerationCollapseError(consecutive_collapses, requires_human_seed)
   │                 （这批题不入库、不激活，旧版本用例集保持 active）
   ▼
③ 60/40 划分训练/验证集（只动新题）
④ test_cases 入库
⑤ CollapseDetector.persist()      —— 这时才把新题向量写进 case_embeddings
⑥ activate_new_version()          —— 新版本用例集生效，旧版本置为非 active
```

**为什么"先判定、入库后再写向量"**（与设计原文的最大出入）：

- `case_embeddings.case_id` 有外键指向 `test_cases`，判定时新题还没入库，先写必然违反外键；
- 更重要的是：**被拒的废题不能进入历史分布**。否则下一次出题会拿一批废题当参照系，越比越像，门禁会自己把自己放松掉。

**为什么连续计数不需要单独的计数器**：成功激活会产生一个 `created_at` 更新的 active 版本，"自它以来的坍塌事件数"天然就在成功时清零。

### 5.2 反坍塌判定（`GenerationCollapseDetector.assess()`）

```
new_cases 为空 ─────────────────────▶ 阻断 empty_batch
collapse_check_enabled = false ─────▶ 放行 disabled（打 warning 留痕）
   │
   ▼
存量回填：inherited_case_ids 里还没有向量的题（取最近 50 条）→ 算向量写库
   │        （文档 21 之前生成的老题没有向量，不回填的话老项目永远停在冷启动）
   ▼
新题算 embedding（超过 8000 字符截断——防上下文洪泛类对抗题撑爆 embedding 模型）
   ▼
threshold = 0.15 + (0.35 − 0.15) × min(历史向量总数 / 200, 1)
   ▼
【检查一：批内雷同】批量 ≥ 4 时
   新题两两平均余弦距离 < 0.15（固定用宽松阈值）─▶ 阻断 collapsed_intra_batch
   ▼
【检查二：贴着历史】取该 Skill 最近 50 条历史向量（同一 embedding 模型）
   历史 < 5 条 ─────────────────────────────────▶ 放行 cold_start
   新题 × 历史 平均余弦距离 < threshold ─────────▶ 阻断 collapsed_vs_history
   ▼
放行 diverse
```

| 检查 | 来源 | 为什么需要 |
|---|---|---|
| 贴着历史 | 设计原文 | 模型在反复出同一批题，只是换了几个词 |
| 批内雷同 | **实现阶段追加** | 首次生成时历史为空、必然冷启动放行。只做新旧对比的话，18 条几乎一样的题会原样成为第一版测试集，并**永久**充当之后所有对比的"历史分布" |

批内检查固定用宽松阈值：同一个 Skill、同一批次的题天然围绕同一主题，严格阈值在这里没意义，它只负责抓"几乎一模一样"。

阈值为什么是**连续插值**而不是两档切换：两档切换会让同一份 Skill 在第 199 次和第 200 次出题之间得出完全不同的结论。

历史向量按 `embedding_model` 过滤：换了 embedding 模型后新旧向量不在同一空间，混着算会得到一个毫无意义却看起来很正常的数字。

### 5.3 种子锚点（`SeedAnchorResolver`）

**回答：Generator 编的题，像不像真实用户会说的话？**

种子库是一个独立 Git 仓库 `skill-evaluate-seed-anchors`（运维维护，与断言工具箱同一套同步方式）：

```
manifest.yaml                 # 领域清单：domain_tag / description / source（脱敏审计记录）
anchors/<domain_tag>.yaml     # 该领域的脱敏真实 Prompt：id / prompt
```

解析规则（`GenerationRequest.seed_anchor_ids`）：

| 取值 | 行为 |
|---|---|
| `None`（默认） | 用 `skill.description` 的 embedding 在全部锚点里检索最相似的 5 条 |
| `[]` | 显式不要锚点 |
| `["data/dp-001", ...]` | 按 id 精确取，查不到的告警忽略 |

溯源链路：Prompt 里渲染 `[data/dp-001] 这个导出的表里好多重复行...` → 模型在 `seed_anchor_id` 回填借鉴的 id → 核对确实是本次注入过的 → 写 `TestCase.seed_anchor_id = "data/dp-001@<种子库 commit>"`。模型编造的 id 记 `None`，不写伪造的溯源。

降级原则：种子库没同步、没配置、embedding 故障 → 返回空列表，照常出题（锚点是增强手段，不是前置条件）；但**库结构非法**（manifest 声明的文件不存在）→ 显式报错，否则所有出题会悄悄失去真实分布锚定。

锚点向量在进程内按 `(commit, embedding_model)` 缓存，不进 `case_embeddings`（外键不允许），文档 23 会升级为混合检索。

---

## 6. 主要业务流程（端到端串起来）

### 6.1 一次正常的 CI 评测

```
1. CI 注入 SKILLEVAL_PREFLIGHT_SANDBOX_IMAGE_REF=<基础镜像 digest>
2. （可选）skill-evaluate sync-seed-anchors
3. 主图创建 runs 记录，进入 Phase 0
4. 指纹门禁：探测 → 与 golden_fingerprint.json 一致 → passed
5. 金丝雀门禁：该镜像 3 小时前刚成功探测过 → skipped（不起沙箱，省冷启动）
6. Phase A：trigger_accuracy 的 prepare 节点调 ensure_test_suite()
   └─ 已有 active 用例集 → 直接复用，不出题，不经过反坍塌
7. Phase B：coverage 发现盲区 → incremental_patch()
   └─ 注入种子锚点 → 出 3 道补盲题 → 反坍塌：历史 40 条、阈值 0.19、距离 0.46 → diverse
   └─ 入库 → 写向量 → 激活新版本
8. ……其余维度照常 → 报告（头部写明：指纹 passed，金丝雀 skipped + 跳过依据）
```

### 6.2 基础镜像被人偷偷升级了 pip 包

```
指纹门禁：core_package_hashes.python_packages 期望 'aaa…' 实际 'bbb…'
   → InfrastructureEnvironmentError(gate="sandbox_fingerprint")
   → 流水线挂起，十个维度一个都不跑，没有任何维度 FAIL
运维：确认是有意升级 → skill-evaluate preflight-fingerprint --output golden_fingerprint.json
   → git diff 审核 → 提交 → 重跑
```

### 6.3 沙箱文件权限坏了

```
指纹门禁：passed（装的东西没变）
金丝雀门禁：镜像标识没变但上次成功已超过 24 小时 → 实跑
   → 模型回复 {"content": "文件为空"}
   → content 与 data.txt 不一致 → 写一条失败的 canary_probe_history
   → InfrastructureEnvironmentError(gate="canary_probe")，流水线挂起
```

### 6.4 Generator 陷入坍塌

```
第 1 次 force_regenerate：批内平均距离 0.04 < 0.15 → collapsed_intra_batch
   → 事件入库，GenerationCollapseError(consecutive=1, requires_human_seed=False)
   → 旧用例集仍然 active，评测不会用到这批废题
第 2 次：collapsed_vs_history → consecutive=2
第 3 次：consecutive=3 → 发告警 generation_collapse_persistent（"请注入新种子"）
   → GenerationCollapseError(requires_human_seed=True)
   → 文档 22 接入后：据此发起 INJECT_NEW_SEED 阻塞审批
人工：往种子库补一批真实 Prompt → sync-seed-anchors → 再出题 → diverse → 激活，计数自然清零
```

注意：模块六/十等调用方原本就会接住 `GenerationError` 并"标记补盲耗尽、写进 findings"，`GenerationCollapseError` 是它的子类，这些维度的行为不变，不会因为一次坍塌掀掉整条流水线。

---

## 7. 数据落在哪

| 表（迁移 0009） | 写入时机 | 读取者 |
|---|---|---|
| `case_embeddings` | 新题入库后 / 存量回填 | 反坍塌检测的历史分布；文档 23 近邻检索（HNSW 索引已建） |
| `generation_collapse_events` | 每次坍塌被阻断 | 连续坍塌计数；人工排查"阈值是否太严" |
| `canary_probe_history` | 每次实跑金丝雀（成功、失败都写） | 金丝雀跳过判定；运维排查"沙箱从什么时候开始坏的" |

外加两个关键文件：`golden_fingerprint.json`（本项目仓库，人工维护，**当前尚未生成**）、种子库仓库（外部，运维维护）。

---

## 8. 关键配置

| 配置 | 默认 | 说明 |
|---|---|---|
| `SKILLEVAL_PREFLIGHT_FINGERPRINT_CHECK_MODE` | `hook_parallel` | `off` 仅限无真实沙箱的本地开发 |
| `SKILLEVAL_PREFLIGHT_CANARY_CHECK_MODE` | `nightly_or_image_change` | `every_run` / `off` |
| `SKILLEVAL_PREFLIGHT_SANDBOX_IMAGE_REF` | 未设置 | CI 应注入镜像 digest |
| `SKILLEVAL_PREFLIGHT_CANARY_MAX_AGE_HOURS` | 24 | |
| `SKILLEVAL_GENERATOR_TRUST_COLLAPSE_CHECK_ENABLED` | true | 关掉会留 warning；**开着但没有 embedding 通道时出题会报错** |
| `SKILLEVAL_GENERATOR_TRUST_COLLAPSE_DISTANCE_THRESHOLD_INITIAL / _MATURE` | 0.15 / 0.35 | |
| `SKILLEVAL_GENERATOR_TRUST_MAX_CONSECUTIVE_COLLAPSES` | 3 | |
| `SKILLEVAL_GENERATOR_TRUST_EMBEDDING_MODEL` | `openai/text-embedding-3-small` | 维度必须 1536 |
| `SKILLEVAL_GENERATOR_TRUST_SEED_REPO_URL` | 未设置 | 不设 = 不注入锚点（合法状态） |

---

## 9. 它不做什么

- **不评价被测 Skill**：不产出任何维度分数，不进 Optimizer 闭环。
- **不自动修环境、不自动更新黄金指纹**：只拦、只说清楚差在哪。
- **不自动降低阈值**：连续坍塌的出路是叫人补种子，不是让机器把及格线降到刚好能过。
- **不证明"运行过程中"环境没被改**：指纹只证明起跑那一刻环境一致。多技能并发时"谁在什么时候改了共用文件/环境变量"（文档 20 遗留）仍未实现。
- **不影响纯复用路径**：用例集复用时不算 embedding、不查历史，零额外成本。

---

## 10. 常见疑问

**Q：金丝雀跳过了，这次评测还算"被证明可信"吗？**
算，但证据强度较弱，所以跳过依据会写进状态、带到报告里：同一镜像、24 小时内、哪次运行成功过。要最强证据就在 Nightly 和镜像变更时设 `every_run`。

**Q：为什么指纹门禁没有黄金指纹就失败，而种子库没有就放行？**
一个是"证明"，一个是"增强"。没有基线就无法证明环境没漂移，放行等于没做；种子锚点只是让题更像真人，没有它题照样能出、照样过反坍塌检测。

**Q：本地开发没有 Hermes 怎么办？**
`SKILLEVAL_PREFLIGHT_FINGERPRINT_CHECK_MODE=off` + `SKILLEVAL_PREFLIGHT_CANARY_CHECK_MODE=off`。两道门禁会打 warning 并在状态里写 `off`，不会伪造"通过"。

**Q：实现过程中顺手修了什么？**
`HermesBackend.execute()` 和 llama 后端 callback 模式原来用 `except Exception` 兜底，把 LangGraph 的挂起信号 `GraphInterrupt` 也吞了，导致真实沙箱执行永远等不到 Hook 回调、每次都被记成失败。金丝雀要走同一条链路，这个 bug 在实现时暴露并修正了。
