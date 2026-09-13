# 接入文档：模块四子图、`ScriptSandboxRunner` 与脚本探测工具（docs/dev/14 留给后续模块的接口）

> 由谁接入：`24`（主图装配、状态 schema 合并、`interrupt_before` 汇总）、
> `15`（**重点**：红队复用 `ScriptSandboxRunner` 与脏数据构造点做注入测试）、
> `16`（能力覆盖率若要统计 `scripts/` 的可用性结论）、`20`（多技能并发的脚本命名
> 冲突检测可复用 `build_probe_targets()`）。
> 当前状态：六个节点、并行分叉 + 汇合、`ScriptSandboxRunner`（Docker 实现）、五条
> 量化规则、`SkillScript.is_mutating` 的静态启发式标注全部落地，有测试覆盖
> （`tests/skill_evaluate/test_script_usability.py` 57 条，不起容器、不碰库、不发
> 真实请求）。

---

## 0. 三十秒上手

```python
from skill_evaluate.nodes.script_usability import (
    ENTRY_NODE, TERMINAL_NODE, NODE_NAMES, ScriptUsabilityDeps,
    add_script_usability_nodes, build_script_usability_subgraph,
)

# A. 装进主图（docs/dev/24 的用法）：只加维度内部的边，外部连线由主图决定
pipeline = add_script_usability_nodes(builder)
builder.add_edge("<某个前置节点>", ENTRY_NODE)      # 见第 3.1 节：本维度没有前置依赖
builder.add_edge(TERMINAL_NODE, "finalize.report")

# B. 单独跑一遍（本地调试 / 集成测试）
graph = build_script_usability_subgraph().compile(checkpointer=...)
await graph.ainvoke({"run_id": ..., "skill_id": ..., "skill_version_ref": ...})
```

**导入即注册**：`import skill_evaluate.nodes.script_usability` 会把五条量化规则
（见第 6 节）注册进 docs/dev/08 的规则表。两个评审模板（`help_doc_quality` /
`constructive_error`）在 docs/dev/07 落地时就已注册，本维度直接复用，不需要额外操心。

---

## 1. 节点名与图结构

```
script_usability.prepare_scripts
   ├────────────────────────┬──────────────────────────┬──────────────────────────────┐
   ↓                        ↓                          ↓
.hard_failure_probing   .self_learning_doc_test   .constructive_error_and_io_separation_test
   └────────────────────────┴──────────────────────────┴──────────────────────────────┘
                              ↓
                .idempotency_and_safety_guards
                              ↓
                .finalize_dimension_report
```

节点名一律从 `NODE_NAMES` 取，不要写字面量。`ENTRY_NODE` / `TERMINAL_NODE` 是主图
连线用的两端。逐个脚本的并发发生在**节点内部**（`asyncio.gather` + 信号量），
与模块三同一取舍：图结构在 `get_graph().draw()` 里看得见，`interrupt_before` 也能
按节点名精确挂载。

`INTERRUPT_BEFORE_NODES` 为空——本维度没有优化闭环（脚本缺陷的修复涉及脚本作者的
实现意图，不是模型能闭环收敛的局部改写），因此没有人工审批挂起点。仍然导出这个
常量，方便 `24` 无差别地 `[*A, *B, ...]`。

---

## 2. ⚠️ 主图的状态 schema 必须包含本维度私有键

与 `docs/dev/interfaces/11` 第 2 节、`12` 第 2 节、`13` 第 2 节同一条坑。LangGraph
按节点函数第一个参数的类型注解裁剪图状态；本维度节点签名写的是
`ScriptUsabilityState`（= `PipelineState` + 私有键）。主图若用裸 `PipelineState`
做 schema，本维度写进状态的结果会被**静默丢弃**，表现为"三条探测支路都跑了、收尾
节点一条发现都看不到"，然后给出一份看起来通过了的报告。

```python
from skill_evaluate.nodes.script_usability import ScriptUsabilityState

class MainGraphState(
    TriggerAccuracyState, ContextScopingState, InstructionControlState,
    ScriptUsabilityState, ..., total=False
):
    ...
```

本维度导出的私有键（键名常量在 `nodes/script_usability/state.py`，全部以 `_su_` 开头）：

| 键 | 含义 | 谁写 | 谁读 |
|---|---|---|---|
| `_su_targets` | 探测目标清单（`ScriptProbeTarget` 的 dump：路径/镜像/解释器/是否突变/跳过原因） | prepare | 三条支路 + 幂等性 + finalize |
| `_su_prepare_findings` | 准备阶段就有结论的问题（文件缺失 / 运行时未知） | prepare | finalize |
| `_su_hard_failure_findings` | 非交互性挂起（**致命，阻断**） | hard_failure | finalize |
| `_su_help_outcomes` | `--help` 文档质量判定摘要 | self_learning | finalize |
| `_su_error_outcomes` | 建设性报错 + 流隔离判定摘要 | constructive_error | finalize |
| `_su_idempotency_findings` | 幂等性与输出体量发现 | idempotency | finalize |
| `_su_sandbox_unavailable` | 沙箱运行时不可用的原因（整维度降级） | prepare | 三条支路 + finalize |

其余维度**不得**读写以上键。

---

## 3. `24` 的接入点

> ✅ **docs/dev/24 已接入**：前置门禁之后直接扇出，见 `docs/dev/interfaces/24_main_graph_and_ci_cd.md` 第 1 节。（本维度三条探测同属一个超步，逐条汇合边不会重复触发幂等性节点，未改动。）

### 3.1 没有前置依赖（可以排在最前面）

本维度**不依赖测试集**：探测对象是 `scripts/` 目录下的真实文件，"用例"由
`probes.py` 按预置模式确定性地构造，不经 Generator，也不读模块一的用例集。它唯一
的前置条件是 `SkillRepository` 里有这份 Skill（`ingestion.load_skill()` +
`SkillRepository.save()`），因此可以与模块一/二/三**完全并行**。

### 3.2 不申领 `run_index` 号段

docs/dev/interfaces/13 第 4 节的全局分配表对本维度**无适用性**：它不产出
`ExecutionTrace`，一条都不落 `execution_traces` 表（理由见第 4 节）。`15`~`20` 若
按 `list_by_case()` 聚合 Trace，不需要为本维度做任何过滤。

### 3.3 报告里的 `blocking` 语义

只有两类会阻断合并：**非交互性挂起**、**连续两次执行未处理崩溃**。`--help` 文档
质量、建设性报错、输出体量超限一律 `blocking=False`（维度 `status` 仍会是 FAIL，
让问题在报告里看得见）。延续模块二/三的一贯原则：主观审查不阻断流水线。

### 3.4 CI 里没有容器运行时的情形

`prepare_scripts` 会先探一次沙箱可用性；不可用时整个维度降级为
`NEEDS_HUMAN_REVIEW` + `blocking=False`，findings 里写明"已跳过全部探测，注意这不
等于通过"。**它不会让主图崩掉**，但也绝不会因为"没测"而给出 PASS。若 CI 明确不打算
跑本维度，正确做法是在 `24` 的装配层不挂这几个节点，而不是让它每次都产出一条
NEEDS_HUMAN_REVIEW。

---

## 4. `ScriptSandboxRunner`：与 `ExecutorBackend` 平行的第二个执行组件

`src/skill_evaluate/executors/script_sandbox.py`。

```python
from skill_evaluate.executors.script_sandbox import (
    DockerScriptSandboxRunner, ProcessResult, ScriptSandboxRunner,
    infer_runtime, infer_runtime_image, interpreter_for, prepare_workspace,
)

result: ProcessResult = await runner.run(
    image="python:3.13-slim",
    command=["python", "scripts/parse.py", "--input", "dirty.csv"],
    stdin_data=b"",          # None = 不喂；b"" = 给一个立刻 EOF 的空 stdin
    cwd=str(workspace),      # 挂载到容器的 /workspace 并作为工作目录
    timeout_s=10,
)
```

### 为什么不是 `ExecutorBackend`

`ExecutorBackend.execute()` 的语义是"Agent 执行一个任务，产出完整 Trace"（thought、
工具调用轨迹、Hook 回调、挂起唤醒）。本维度只有一次确定性的子进程调用，硬塞进去
只能**捏造**一条假 Trace，而 Trace 是模块一触发率、模块三效率诊断的统计输入——
往里灌假数据的代价远大于多一个类。

### 沿用的约定（与 docs/dev/03 第 7、8 节逐条对应）

| 约定 | 落地方式 |
|---|---|
| Ephemeral 容器 | `docker run --rm` + 每次调用一个新容器；超时后额外 `docker rm -f` 确保容器真的死掉 |
| 默认无出站网络 | `--network=none`（**没有**白名单开关：被测脚本不该在探测过程中把数据发出去） |
| Wall-clock 超时墙 | `asyncio.wait_for` + SIGKILL，超时如实标记 `timed_out=True` / `exit_code=None`，**不抛异常**（超时是要被判定的观测事实，不是故障） |
| 输出截断 | 共用 `executors/sanitize.truncate_field()`；但**截断前**的原始字节数记在 `ProcessResult.stdout_bytes/stderr_bytes` 里 |
| 资源墙 | `--memory` / `--cpus` / `--pids-limit`，均可配 |

### 三条给实现方/复用方的硬约束

1. **不要加"退化到宿主机执行"的分支**。被测脚本是外部输入，探测还会故意喂脏数据；
   宿主机执行 = 让未经审查的脚本以评测进程的权限跑任意代码。容器不可用时抛
   `ExecutorBackendError`（与 `UnconfiguredHermesSandboxClient` 同一种处理：报错，
   不伪造成功）。
2. **无 TTY**。只给 `-i`，绝不给 `-t`——非交互性测试成立的前提就是"脚本没有终端
   可以问问题"。
3. **每次探测一个独立工作区**（`prepare_workspace()` 复制整份 Skill 目录到临时目录，
   用完即删）。复制而不是直接挂载源目录：容器里的写操作会真的落到宿主机上。
   唯一刻意共享工作区的场景是幂等性探测自己的两次连续执行。

### 运行时推断表

`infer_runtime(path, shebang=..., image_overrides=...)`，**shebang 优先于扩展名**
（`.sh` 里写 `#!/usr/bin/env python3` 也能被正确识别）：

| 扩展名 | 语言 | 默认镜像 | 解释器 |
|---|---|---|---|
| `.py` | python | `python:3.13-slim` | `python` |
| `.sh` / `.bash` | bash | `bash:5` | `bash` |
| `.js` / `.mjs` / `.cjs` | node | `node:22-slim` | `node` |
| `.ts` | node | `node:22-slim` | `node --experimental-strip-types` |
| `.rb` | ruby | `ruby:3.3-slim` | `ruby` |
| `.ps1` | powershell | `mcr.microsoft.com/powershell:latest` | `pwsh -NonInteractive -File` |

识别不出来时返回 `None`（**不猜**：拿错解释器跑出来的失败会被误读成"脚本有缺陷"）。
内网/离线环境用 `SKILLEVAL_SCRIPT_USABILITY_RUNTIME_IMAGE_OVERRIDES` 按**语言名**
替换镜像源。

---

## 5. `15`（模块五红队）的接入点 —— 本文档最重要的一节

### 5.1 直接复用执行链路，不要另造一套

```python
from skill_evaluate.executors.script_sandbox import DockerScriptSandboxRunner
from skill_evaluate.nodes.script_usability import build_probe_targets

targets = build_probe_targets(skill)                  # 已经带好镜像/解释器/可运行标记
result = await runner.run(image=t.image, command=t.command("--input", payload), ...)
```

`build_probe_targets()` 已经处理了"文件不在盘上""推断不出运行时"两种情况并给出
`skip_reason`，红队不需要重复这套判断。

### 5.2 攻击性脏数据的接入方式（docs/dev/14 第 9 节留的口子）

本维度的 `probes.generate_dirty_payloads()` 只覆盖**格式层**的通用错误（残缺 JSON、
缺必填字段、非 UTF-8 字节、超长参数），刻意不做命令注入这类需要理解脚本语义的构造
——那是 Attacker Agent 的活。

`15` 的正确接入方式是**在自己的维度里**构造 `DirtyPayload` 并调用同一个 runner，
**不要**改本维度的 `generate_dirty_payloads()`：本维度的脏数据必须保持确定性、
可复现（它的结论会进合并门禁），把一个 LLM 生成的负载塞进来会让"同一份 Skill 两次
评测结论不同"。这与 docs/dev/interfaces/11 第 4.2 节"请求的构造是各维度语义的
一部分"是同一条约定。

### 5.3 可以直接拿去用的确定性检查

```python
from skill_evaluate.nodes.script_usability import check_io_separation, looks_like_unhandled_crash
```

- `check_io_separation(result)`：失败时 stdout 有没有混进错误/堆栈特征；
- `looks_like_unhandled_crash(stderr)`：**只认没被接住的**堆栈/信号/`FileExistsError`
  类原生异常。一条写得好的报错（"输出文件已存在，请加 --force"）**不算**崩溃——
  红队判"注入是否导致进程崩溃"时请注意这条口径差异，需要更宽的判定就自己写一条。

**`15` 的实际接入结果**：它复用了 `looks_like_unhandled_crash()`（在
`nodes/security/detectors.trace_crashed()` 里，与"末尾动作是 `internal_error`"
一起构成"沙箱崩了"的判定）。那条"一条写得好的报错不算崩溃"的口径差异**正好是它
要的**——DoS 判定里"崩了但给出了建设性报错"与"崩了且什么都没说"是两个不同的结论
（架构文档模块五第 2 节）。

它**没有**接 `ScriptSandboxRunner` / `build_probe_targets()`：模块五的七个攻击面都
是"把载荷发给 Agent，看它怎么处理"，走的是 `ExecutorBackend`（真实沙箱 + Trace），
而不是裸调脚本子进程。脚本层面的注入由生成物 SAST 那条支路间接覆盖（看 Agent 产出的
文件里有没有带上载荷）。将来若要加一条"直接对 `scripts/` 做注入模糊测试"的支路，
按本节第 5.1/5.2 条接即可——那时**在模块五自己的维度里**构造 `DirtyPayload`，
不要改本维度的 `generate_dirty_payloads()`。

---

## 6. 判定与报告口径

| 事项 | 本维度的做法 | 为什么 |
|---|---|---|
| 判定入口 | 确定性事实走 `quantitative_verdict()`（五条规则）；`--help` 质量与建设性报错走 `judgmental_verdict()` | docs/dev/interfaces/08 第 0 节铁律；让 LLM 去数 exit_code 既贵又不准 |
| `Criticality` | 两项 LLM 审查都是 **ROUTINE** | 它们本来就不阻断合并，给只作参考的建议投三次票是纯浪费 |
| 分数 | `score=None` | 四项性质完全不同的检查，硬凑"通过项/总项数"会平均出一个没含义的数字 |
| `blocking` | **运行期计算**：`挂起 or 幂等性崩溃` | 见第 3.3 节 |
| 维度状态 | FAIL（阻断项或任一检查未通过）> NEEDS_HUMAN_REVIEW（有事情要人看一眼）> PASS | 与 docs/dev/interfaces/13 第 6 节同一口径 |
| Skill 没有 `scripts/` | **PASS** + 一条 `[提示]` finding | 与模块三"用例集为空判 NEEDS_HUMAN_REVIEW"不同：那是"出题失败"的信号，而"这份 Skill 不带脚本"是文件系统上可确定的事实，不是流水线出了问题 |
| 沙箱不可用 / 脚本跑不起来 / 无法判定是否突变 | NEEDS_HUMAN_REVIEW | "没测到"绝不等于"通过" |
| 黄金盲测 | `is_golden_subject()` 跳过，并在 findings 里写明"这一项本次没跑成" | 与模块二/三同一口径 |
| 共识未达成 | 抛 `PipelineSuspended` 等人工仲裁 | `NEEDS_HUMAN_REVIEW` 不允许被降级（docs/dev/08 明令禁止） |

### 6.1 五条量化规则（`nodes/script_usability/rules.py`）

| 规则名 | inputs 键 | PASS 条件 |
|---|---|---|
| `script_non_interactive` | `timed_out` / `timeout_s` / `exit_code` | 没挂起（**退出码不参与判定**：缺参数时非 0 退出正是期望行为） |
| `script_help_responsive` | `help_output_chars` / `exit_code` / `timed_out` | 拿到了非空输出（stdout 或 stderr 都算） |
| `script_rejects_dirty_input` | `dirty_mode_count` / `rejected_mode_count` | 至少一种脏数据被拒；一份负载都没构造出来 → NEEDS_HUMAN_REVIEW |
| `script_idempotent` | `second_run_crashed` / `exit_code` / `timed_out` | 第二次没有未处理崩溃 |
| `script_output_bounded` | `output_bytes` / `warn_bytes` / `already_truncated` | 原始输出字节数不超上限 |

### 6.2 `subject_id` 前缀约定

同一个脚本会产生五种判定，一律带前缀（约定同 docs/dev/interfaces/13 第 6.1 节）：

| 前缀 | 用途 |
|---|---|
| `script_hang:<path>` | 挂起探测 |
| `script_help:<path>` | `--help` 闸门 + 文档质量审查（两者共用，一次运行里只会有一条） |
| `script_dirty:<path>` | 脏数据闸门 + 建设性报错审查 |
| `script_idempotency:<path>` | 幂等性 |
| `script_output:<path>#<n>` | 第 n 次执行的输出体量 |

---

## 7. 数据契约变更（其他模块可能受影响）

**`SkillScript.is_mutating: bool | None`**（新增可选字段，`skills.scripts` 是 JSON 列，
**无需迁移**）。三态语义：

- `True` = 静态启发式在脚本正文里看到了写操作（落盘/删除/网络写/DB 写）；
- `False` = 扫过了，没看到；
- `None` = **无法判定**（文件读不到、二进制、语言不在覆盖范围内）。

值由 `ingestion/skill_loader.detect_mutating_script(source, suffix)` 填充，覆盖
Python / Shell / JS·TS / Ruby / PowerShell 五个语言族。该启发式**刻意偏向误报**：
漏判会让幂等性探测整个跳过（真实缺陷溜走），误判只是多跑两次容器。

任何按 `is_mutating` 做 `if` 的地方请**显式区分 `None` 与 `False`**——`if not
script.is_mutating` 会把"没扫出来"当成"确认安全"，正好是这个字段最不能出的错。

`SkillScript.supports_help_flag` **仍然恒为 `None`**：它是黑盒探测结论，静态解析给
不出可信答案。本维度的探测结果存在 `_su_help_outcomes` 里，没有回写这个字段——
回写意味着一次评测运行会改动 `skills` 表里的静态快照，而那份快照的语义是"解析
SKILL.md 当时的样子"。需要这个信息的模块请读报告或 `JudgeRepository`。

---

## 8. 配置

新增一组（追加式扩展，无迁移、无破坏性变更）：

```bash
SKILLEVAL_SCRIPT_USABILITY_HANG_PROBE_TIMEOUT_S=10          # 挂起探测专用短超时
SKILLEVAL_SCRIPT_USABILITY_HELP_PROBE_TIMEOUT_S=10
SKILLEVAL_SCRIPT_USABILITY_DIRTY_INPUT_TIMEOUT_S=15
SKILLEVAL_SCRIPT_USABILITY_IDEMPOTENCY_TIMEOUT_S=15
SKILLEVAL_SCRIPT_USABILITY_OUTPUT_TRUNCATION_WARN_BYTES=32768   # 与 docs/dev/03 截断阈值对齐
SKILLEVAL_SCRIPT_USABILITY_DIRTY_INPUT_FLAG=--input          # 脏数据/幂等性探测的传参约定
SKILLEVAL_SCRIPT_USABILITY_DIRTY_PAYLOAD_MODES=              # 留空 = 全开
SKILLEVAL_SCRIPT_USABILITY_OVERSIZED_PAYLOAD_BYTES=65536
SKILLEVAL_SCRIPT_USABILITY_DOCKER_BINARY=docker
SKILLEVAL_SCRIPT_USABILITY_CONTAINER_MEMORY_LIMIT=512m
SKILLEVAL_SCRIPT_USABILITY_CONTAINER_CPU_LIMIT=1.0
SKILLEVAL_SCRIPT_USABILITY_CONTAINER_PIDS_LIMIT=256
SKILLEVAL_SCRIPT_USABILITY_RUNTIME_IMAGE_OVERRIDES=          # {"python": "内网镜像"}，JSON
SKILLEVAL_SCRIPT_USABILITY_MAX_CONCURRENT_SCRIPTS=           # 留空 = 复用 EXECUTOR_MAX_CONCURRENT_SANDBOXES
```

`DIRTY_INPUT_FLAG` 是本维度**最大的不确定来源**：我们并不知道任意一个脚本的参数
长什么样，只能按最通行的约定试一次。探测不成功不等于脚本有缺陷，报告里会如实
标注（见第 9 节第 2 条）。

---

## 9. 相对 docs/dev/14 正文的实现决策（正文已同步修订）

1. **脏数据"多跑几种、只审一次"**。正文每个脚本只构造一份负载；实现改为跑完全部
   预置模式，再挑一条最有代表性的报错交给 LLM。单一模式打不中脚本的输入类型时
   （给只收数字的脚本喂坏 JSON 文件），拿回来的只是"文件不存在"，据此评价报错质量
   毫无意义；多跑几次子进程的成本远低于一次 LLM 调用。
2. **幂等性只在基准调用成功时才判**。第一次就失败说明我们没能把脚本正常调起来
   （参数约定和我们猜的不一样），此时第二次的任何异常都不构成幂等性缺陷的证据——
   拿它去阻断合并就是误报，而这是本维度最不能出的错。这种情况记一条 `[提示]`。
3. **输出体量对所有可运行脚本都查**。正文把这项检查放在只对突变脚本执行的循环里，
   非突变脚本因此永远测不到防刷屏。实现改为"每个可运行脚本都用良性样本跑一次基准
   调用"，突变脚本在此基础上再跑第二次。
4. **输出体量用截断前的原始字节数**。正文的 `len(r.stdout) + len(r.stderr)` 读到的
   是我们**自己**截断后的字符串，等于用评测系统的防护掩盖了脚本没做防护这件事。
5. **确定性结论也走 `JudgeAgent.quantitative_verdict()`**。正文写的是直接构造
   结论/追加 finding。改走 Judge 是为了让阻断合并的结论在裁判记录里查得到证据
   （docs/dev/interfaces/08 第 0 节铁律）。
6. **模板变量名以注册表为准**。正文伪代码传的是 `{"help_output": ...}` /
   `{"stderr", "stdout", "exit_code"}`，而 docs/dev/07 注册的模板要的是
   `(script_path, help_output)` 与 `(invocation, error_output)`。模板环境用
   `StrictUndefined`，传错会在**发请求之前**抛 `ReviewTemplateError`。
7. **`record_dimension_result()` 是关键字参数**（`run_id` / `dimension` / `status` /
   `score` / `findings` / `blocking`），不是正文写的 `result=DimensionResult(...)`。

---

## 10. 留给后续文档的接入点

| 预留位置 | 当前状态 | 由哪份文档接入 | 接入方式 |
|---|---|---|---|
| 攻击性脏数据构造 | 只有格式层的通用模式 | `15` | 在模块五自己的节点里构造负载 + 复用 `ScriptSandboxRunner`，**不改**本维度的 `generate_dirty_payloads()`（见第 5.2 节） |
| `ScriptSandboxRunner` 的更严沙箱策略（seccomp/AppArmor/只读根） | 当前只有 network/memory/cpu/pids | `15` | 注入自己的 `ScriptSandboxRunner` 实现，或给 `DockerScriptSandboxRunner._docker_argv()` 加配置项 |
| `SkillScript.is_mutating` 的更准判定 | 静态正则启发式 | `16`（能力树）如果能给出更准的答案 | 只改 `probes.is_mutating_script()` 一处——它就是为这个留的收口 |
| `--help` 探测结果回写 `supports_help_flag` | 未回写（理由见第 7 节） | 无计划 | 若将来确实需要，应新建一张"探测结论"表，而不是改 `skills` 里的静态快照 |
| 把 Help 质量/建设性报错升级为阻断项 | 当前非阻断 | 运维调优，非新文档职责 | 改 `finalize_dimension_report()` 里 `blocking` 的计算式一处；同时应把对应 `Criticality` 升到 CRITICAL（`_to_outcome()` 已备好共识路径） |
| 无容器环境下的替代探测（如纯静态 AST 检查） | 无 | 待定 | 架构文档模块四第 5 节已论证黑盒探测优于静态 AST；若某天要补一个降级路径，应作为**独立维度**，不要让本维度在没测到的情况下给 PASS |
