# 接入文档：模块二子图、静态扫描器与 Token 计数口径（docs/dev/12 留给后续模块的接口）

> 由谁接入：`24`（主图装配、状态 schema 合并）、`13`~`20`（复用 `load_skill()` 与
> 静态扫描器、照抄本维度的"轻量维度"骨架）、`14`（给 `SkillScript` 补
> `is_mutating`）、`16`~`18`（覆盖率维度可直接调用静态指标）。
> 当前状态：四个节点、两个纯代码扫描器、三项同行评审、报告写入、CI linter 命令
> 全部落地，有测试覆盖（`tests/skill_evaluate/test_context_scoping.py`，40 条，
> 不碰库、不发真实请求）。

---

## 0. 三十秒上手

```python
from skill_evaluate.nodes.context_scoping import (
    ENTRY_NODE, TERMINAL_NODE, NODE_NAMES, ContextScopingDeps,
    add_context_scoping_nodes, build_context_scoping_subgraph,
)

# A. 装进主图（docs/dev/24 的用法）：只加维度内部的边，外部连线由主图决定
pipeline = add_context_scoping_nodes(builder)
builder.add_edge("preflight.canary", ENTRY_NODE)
builder.add_edge(TERMINAL_NODE, "finalize.report")

# B. 单独跑一遍（本地调试 / 集成测试）
graph = build_context_scoping_subgraph().compile(checkpointer=...)
await graph.ainvoke({"run_id": ..., "skill_id": ..., "skill_version_ref": ...})
```

**本维度没有导入副作用**：不注册量化规则（判定全走 `judgmental_verdict()`），也不
注册新模板（`omission_audit` / `scoping_check` / `progressive_disclosure_static`
由 docs/dev/07 的 `templates/builtin.py` 在 `import skill_evaluate.agents.mini` 时
注册好了）。所以不存在模块一那种"忘了导入就炸"的情况。

---

## 1. 节点名与图结构

```
context_scoping.static_metrics_scan
        ↓
context_scoping.progressive_disclosure_static_scan
        ↓
context_scoping.mini_agent_peer_review
        ↓
context_scoping.finalize_dimension_report
```

一条直线，**没有条件路由、没有优化闭环**：架构文档模块二本身就没有失败重试机制
——正文质量问题涉及作者意图，不是 description 那种能由模型闭环收敛的局部改动，
直接报告给人改。

节点名一律从 `NODE_NAMES` 取，不要写字面量。`ENTRY_NODE` / `TERMINAL_NODE` 是主图
连线用的两端。`INTERRUPT_BEFORE_NODES` 是**空列表**（本维度不产生需要人工审批才
能继续的动作），仍然导出是为了让 `24` 能无差别地 `[*A, *B, ...]` 汇总。

⚠️ 前两个节点是**串行**的，`24` 不要为了省时间把它们改成并行：第二个节点要用第
一个刚算出来的 Token 数判断"体量接近限额却没有 references/"，并行会让它读到空状态
而悄悄退化成用库里的旧字段。两个节点都是纯计算，串行的代价可以忽略。

---

## 2. ⚠️ 主图的状态 schema 必须包含本维度私有键

与 `docs/dev/interfaces/11` 第 2 节同一条坑，这里再点一次名，因为**本维度的失败
表现更隐蔽**：

LangGraph 按节点函数第一个参数的类型注解裁剪图状态。本维度节点签名写的是
`ContextScopingState`（= `PipelineState` + 私有键）。主图若用裸 `PipelineState`
做 schema，本维度写进状态的扫描结果会被**静默丢弃**——收尾节点拿到的是"什么都
没有"。

本维度对这种情况**不再报 PASS**：`finalize_dimension_report` 检测到取不到静态指标
时，会写一条 `NEEDS_HUMAN_REVIEW` 并在 findings 里点名是哪个私有键没传到。这是
刻意的兜底，但**别指望它替你把 schema 配对**——那条记录只说明"这次评测没做成"。

`24` 的做法：

```python
# src/skill_evaluate/graph/main.py
from skill_evaluate.nodes.trigger_accuracy import TriggerAccuracyState
from skill_evaluate.nodes.context_scoping import ContextScopingState
# ...

class MainGraphState(TriggerAccuracyState, ContextScopingState, ..., total=False):
    """主图状态 = PipelineState 公共字段 + 各维度私有键的并集。"""

builder = StateGraph(MainGraphState)
```

本维度导出的私有键（键名常量在 `nodes/context_scoping/state.py`）：

| 键 | 含义 | 谁写 | 谁读 |
|---|---|---|---|
| `_ctx_static_metrics` | 行数/Token 扫描结果（`StaticMetricsResult` 的 dump） | static_metrics_scan | 后两个节点 |
| `_ctx_disclosure_scan` | 渐进式披露初筛结果（`ProgressiveDisclosureScan` 的 dump） | progressive_disclosure_static_scan | peer_review / finalize |
| `_ctx_peer_review_outcomes` | 三项审查的结论摘要（`PeerReviewOutcome` 列表） | mini_agent_peer_review | finalize |

其余维度**不得**读写以上键。

---

## 3. `24` 的接入点（只有三条）

1. **前置条件**：进入 `ENTRY_NODE` 之前，被测 Skill 必须已经
   `ingestion.load_skill()` + `SkillRepository.save()` 入库，否则本维度抛
   `PersistenceError`。本维度**不**依赖测试集，因此可以与模块一的
   `prepare_test_suite` 并行，不必等 Phase A。
2. **不需要 staleness 透传**：本维度不用测试集，用例集版本漂移与它无关。
3. **`interrupt_before` 贡献为空**，见上。

本维度**不读** `_working_skill`（模块一优化闭环的内存工作副本）：它评审的就是仓库
里那份 SKILL.md 的原貌，拿一份内存里改过的版本来审，报告就与人能看到的文件对不上
了。`24` 不要为了"让模块二也看到补丁后的版本"去接线。

---

## 4. `13`~`20` 可以直接复用的三样东西

### 4.1 `load_skill()`（一切读 SKILL.md 的入口）

```python
from skill_evaluate.ingestion import load_skill
skill = load_skill("path/to/skill-dir")   # 或直接传 SKILL.md 路径
```

不要再实现一份解析逻辑。`14` 要给 `SkillScript` 加 `is_mutating` 时，按追加式扩展
改 `state/skill.py` + `_scan_scripts()`，见
`docs/dev/interfaces/06_skill_loader_minimal.md` 第 4 节（那条待接入项仍然有效）。

### 4.2 两个纯代码扫描器（无 LLM、无 IO，可当 linter 用）

```python
from skill_evaluate.nodes.context_scoping import (
    scan_static_metrics, scan_progressive_disclosure,
)

metrics = scan_static_metrics(skill, line_limit=500, token_limit=5000)
scan = scan_progressive_disclosure(skill, token_count=metrics.token_count)
```

`16`~`18`（覆盖率与瘦身）想知道"这份 Skill 有多大"时直接调 `scan_static_metrics()`，
不要自己 `len(body.splitlines())`——口径漂移会让两份报告里的同一个数字对不上。

CI 里跑纯代码那一半（不需要 Postgres、不需要 LLM）：

```bash
skill-evaluate lint --skill-path path/to/skill-dir
# 硬性指标超标 -> 退出码 1；初筛告警只打印，不影响退出码
```

### 4.3 "轻量维度"骨架

本维度是第一个不需要沙箱、不需要测试集的维度。`16`~`18` 这类以"分析已有文本/
Trace"为主的维度可以照抄这套骨架：`state.py`（私有键）+ `deps.py`（惰性注入）+
纯函数扫描器 + `nodes.py`（薄节点）+ `graph.py`（两种装配入口）。薄节点的好处是
扫描逻辑的单测不需要任何替身。

---

## 5. 判定与报告口径（**与模块一不同，注意区分**）

| 事项 | 本维度的做法 | 为什么 |
|---|---|---|
| 判定入口 | 三项同行评审全走 `judge.judgmental_verdict()` | docs/dev/interfaces/08 第 0 节铁律 |
| `Criticality` | 全部 `ROUTINE` | 架构文档自己承认静态审查有"纸上谈兵"风险，应对方式是**降低结论的强制力**（非阻断），而不是花三倍 Token 给一个只作参考的建议投票 |
| 分数 | `score=None` | 本维度只有硬性指标 + 审查结论，硬凑"通过项/总项数"会把三个性质完全不同的检查平均掉 |
| `blocking` | **随检查项而变**：`blocking=hard_fail` | 500 行/5000 Token 能精确算、无歧义 → Error（阻断）；Mini Agent 的主观判断本质是建议，直接阻断合并会因误报让开发者不再信任流水线 → Warning（不阻断） |
| 状态优先级 | FAIL > NEEDS_HUMAN_REVIEW > PASS | 已有确凿问题时，不该因为"另有一项待人确认"把结论弱化成待定 |
| 黄金盲测 | `is_golden_subject()` 跳过，并在 findings 里写明"这一项本次没跑成" | 少做了一项审查，读报告的人有权知道（docs/dev/08 第 3 节只要求跳过，"写明"是本维度的加码） |

**`blocking` 是各维度自行声明的**，本维度是全项目第一个把它做成"运行期计算值"
而不是模块级常量的维度。后续维度若也存在"硬性指标 + 主观判断"混合的情况，照此
处理，不要为了统一而把整个维度设成一个固定的 blocking。

---

## 6. Token 计数口径（**这是会影响别人分数的全局约定**）

`ingestion/token_counter.py`：

```python
from skill_evaluate.ingestion import count_tokens, estimate_token_count

count = count_tokens(text)   # -> TokenCount(value, method, exact)
n = estimate_token_count(text)  # 只要整数时用这个（Generator 的 Prompt 预算等）
```

- 装了 `tiktoken`（**可选依赖**）→ 离线精确计数，`exact=True`，`method` 形如
  `tiktoken:o200k_base`。**这是正常路径**；
- 没装 → 兜底估算 `字符数 × 3/4`，`exact=False`，`method` 为
  `heuristic:chars-x0.75`。刻意不按中英文分档取系数：多分几档并不能把误差压到
  可以用来卡线的程度，既然结论都是"标注不精确、限额附近不阻断"，就用一个所有人
  一眼能算的系数。

**为什么不用 Anthropic 官方计数**：`client.messages.count_tokens` 是网络请求而不是
离线分词器，且本项目 LLM 出口统一走 OpenRouter、并不持有 Anthropic 原生凭证。把
一次静态扫描做成需要联网 + 额外凭证 + 计费的操作，代价远大于换来的精度。要接官方
计数时，实现 `TokenCounter` 协议并注入 `ContextScopingDeps.token_counter` 即可，
不必改任何节点代码。

**不精确时的阻断策略**（本维度独有，其他维度要卡线时建议照抄）：估算值超标但落在
`token_limit × (1 ± estimate_uncertainty_ratio)`（默认 ±15%）的不确定带内时，
判 `NEEDS_HUMAN_REVIEW` 而**不阻断**。拿一个 ±15% 的估算值去阻断别人的合并请求，
是 `docs/dev/interfaces/06` 明确警告过的误判来源。远超限额时估算值仍然阻断——偏差
解释不了那么大的差距。行数永远精确（数换行符），不受此影响。

---

## 7. 配置

新增一组（追加式扩展，无迁移、无破坏性变更）：

```bash
SKILLEVAL_CONTEXT_SCOPING_LINE_LIMIT=500            # 架构文档硬性上限
SKILLEVAL_CONTEXT_SCOPING_TOKEN_LIMIT=5000
SKILLEVAL_CONTEXT_SCOPING_ESTIMATE_UNCERTAINTY_RATIO=0.15
SKILLEVAL_CONTEXT_SCOPING_MAX_REFERENCE_FILES_WITHOUT_TRIGGER=0
SKILLEVAL_CONTEXT_SCOPING_BULK_INLINE_RATIO=0.8
```

`MAX_REFERENCE_FILES_WITHOUT_TRIGGER` 是**整体**容忍度，不是"逐条豁免前 N 个"：
正则初筛命中数不超过它就一条都不报（报三条里的后两条对读报告的人没有意义——他
无从知道被吞掉的是哪一条）。

阈值做成配置项是给团队按自身规范收紧/放宽用的，**不是**给 CI 里临时调大好让某次
合并通过用的。同行评审的温度沿用 `MiniReviewAgent` 的 `DEFAULT_REVIEW_TEMPERATURE
= 0.1`（架构文档模块二的"极低温度"要求），本维度不另设。

---

## 8. 相对 docs/dev/12 正文的五处实现修正（照抄正文会踩坑）

1. **`record_dimension_result()` 是关键字参数**（`run_id` / `dimension` / `status` /
   `score` / `findings` / `blocking`），不是正文写的 `result=DimensionResult(...)`。
   与 `docs/dev/interfaces/11` 第 5 节一致。
2. **节点只返回增量**，不要 `{**state, ...}`：`judge_verdict_ids` 的 reducer 是
   `operator.add`，回抛整个旧状态会让已有 id 再追加一遍。
3. **`progressive_disclosure_static` 模板的必需变量是 `reference_files`**（docs/dev/07
   已定稿），不是正文里写的 `candidate_missing_triggers`。正则初筛的结论由
   `format_reference_files_for_review()` 渲染进那一个变量——**连合格项一起给**，
   模型才有对照组；只喂可疑项会诱导它把每一项都判成问题。
4. **`judgmental_verdict()` 的返回值要处理黄金盲测与共识两种形态**。正文的
   `r.verdict_id` 写法在被盲测注入时会把黄金用例的判决当成这个 Skill 的结论写进
   报告。
5. **正则初筛限定在"同一语义单元"内搜条件词**（列表项 = 该项，否则 = 自然段）。
   全文搜索的结果必然是"全部通过"——任何一篇 SKILL.md 都会在别处出现"如果""当…
   时"，那样这个扫描就等于没做。

---

## 9. 留给运维/后续调优的开关（非新文档职责）

docs/dev/12 第 7 节列的"Mini Agent 主观审查是否应升级为阻断项"仍然开着：报告数据
积累后若发现误报率可接受、漏报风险大，改 `nodes.py` 里 `blocking=hard_fail` 这一
处即可。

若同时把 `PEER_REVIEW_CRITICALITY` 调成 `CRITICAL`，注意 `_to_outcome()` 已经备好
了共识路径：共识未达成时抛 `PipelineSuspended` 走人工仲裁，**不会**把
`NEEDS_HUMAN_REVIEW` 悄悄降级成 PASS/FAIL（docs/dev/08 的明令禁止项）。届时
`INTERRUPT_BEFORE_NODES` 应当加上 `context_scoping.mini_agent_peer_review`。
