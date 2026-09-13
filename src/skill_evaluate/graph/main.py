"""主 DAG 装配（docs/dev/24 第 2、3 节）。

## 最终拓扑（以本文件为准；docs/dev/24 正文已按此修订）

```
pipeline.bootstrap_run                                  入口：Skill 入库 + runs 记录（+ 强制出题）
        ↓
preflight.sandbox_fingerprint_gate → preflight.canary_probe_gate        Phase 0（文档 21）
        ↓ route_after_preflight
        ├─ cold_suite 模式 → nightly.cold_suite_regression ───────────────────────────┐
        └─ full 模式（并行扇出）                                                      │
           ├─ context_scoping.*          （12，纯静态）                               │
           ├─ script_usability.*         （14，脚本黑盒）                             │
           └─ trigger_accuracy.prepare_test_suite  （11，产出测试集）                  │
                 ├─ trigger_accuracy.*（执行/判定/闭环）→ …finalize → cross_model.*（19）│
                 └─ security.prepare_adversarial_suite（15）                          │
                       ├─ security.*（五条探测 / AppSec 闭环）                         │
                       └─ [trigger_accuracy.judge_train_cases 与之汇合]                │
                             → instruction_control.prepare_cases（13）                │
                                   ├─ instruction_control.*                           │
                                   └─ coverage.*（16，带环）→ 17 → 18 → multi_skill.*（20）│
        各维度终节点全部完成（LangGraph 多起点边 = 同步屏障）                            │
        ↓                                                                             │
finalize.report ←─────────────────────────────────────────────────────────────────────┘
        ↓
finalize.patch_pr → finalize.rag_archive
```

## 与 docs/dev/24 正文的四处出入（都是正文伪码在真实实现上跑不通的地方）

1. **"改用例集"的准备节点被串行化**（模块一 → 模块五 → 模块三 → 模块六）。正文把模块一与模块五
   放在同一个并行阶段、模块三/六/九放在下一个并行阶段。但这些节点都调 `ensure_test_suite()`：
   从没出过题时两边会**同时出题**，各自激活一个继承旧版本的新版本，后激活的把先激活那批用例从
   active 版本里丢掉（数据库层的 lost update，不报错）。串行化只作用于各维度的**准备节点**：模块五
   的五条探测支路、模块一的执行与判定照常与其余支路并行，墙钟代价只是几次 REUSE 查询。
2. **汇合用多起点边**（`add_edge([...], "finalize.report")`）。正文说"LangGraph 原生支持汇聚、不需要
   手写同步屏障"，但这句只对多起点边成立：逐条 `add_edge(terminal, "finalize.report")` 会让收尾节点
   **每个前驱完成时各跑一次**，第一个维度跑完就写出一份残缺报告。
3. **模块九排在模块一终节点之后、模块三排在模块一训练集判定之后**（interfaces/19 第 0 节、13 第 3.1
   节的实现期约定，比正文的"prepare_test_suite 之后"更稳妥：前者要读验证集，后者与模块一共用
   Trace 表）。
4. **编译期 `interrupt_before` 为空**，见 `INTERRUPT_BEFORE_NODES` 的注释。

## 装配方式

全部节点经 `ApprovalGuardedBuilder` 平铺装配（interfaces/22 第 2 节第 1 条：子图作为单个 Runnable
加入时 guard 不生效），各维度的 `add_*_nodes()` 一行未改。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import StateGraph

from skill_evaluate.graph.cold_suite import NODE_NAME as COLD_SUITE_NODE
from skill_evaluate.graph.cold_suite import ColdSuiteRegression
from skill_evaluate.graph.nodes import NODE_NAMES, PipelineDeps, PipelineNodes
from skill_evaluate.graph.state import KEY_MODE, MODE_COLD_SUITE, MainGraphState
from skill_evaluate.nodes import (
    context_scoping,
    coverage,
    cross_model,
    instruction_control,
    multi_skill,
    preflight,
    pruning,
    script_usability,
    security,
    trigger_accuracy,
    weighted_coverage,
)
from skill_evaluate.nodes.approval_guard import ApprovalGuard, ApprovalGuardedBuilder
from skill_evaluate.nodes.context_scoping import ContextScopingDeps
from skill_evaluate.nodes.coverage import CoverageDeps
from skill_evaluate.nodes.cross_model import CrossModelDeps
from skill_evaluate.nodes.instruction_control import InstructionControlDeps
from skill_evaluate.nodes.multi_skill import MultiSkillDeps
from skill_evaluate.nodes.preflight import PreflightDeps
from skill_evaluate.nodes.script_usability import ScriptUsabilityDeps
from skill_evaluate.nodes.security import SecurityDeps
from skill_evaluate.nodes.trigger_accuracy import TriggerAccuracyDeps

# ---- Phase 0 之后直接扇出的维度入口（互不依赖、不改用例集）----
PHASE_A_ENTRY_NODES: tuple[str, ...] = (
    trigger_accuracy.ENTRY_NODE,
    context_scoping.ENTRY_NODE,
    script_usability.ENTRY_NODE,
)

# ---- `finalize.report` 的汇合前驱：每个维度的终节点 ----
# 模块六/七/八的终节点不在其中：它们串在模块十之前，模块十的终节点完成即意味着三者已完成。
# 模块十用 `TERMINAL_NODE` 常量（现为 `multi_skill.deep_conflict_approval_gate`，interfaces/22 第 2 节
# 第 4 条），不写死 `finalize_dimension_report`。
DIMENSION_TERMINAL_NODES: tuple[str, ...] = (
    trigger_accuracy.TERMINAL_NODE,
    context_scoping.TERMINAL_NODE,
    script_usability.TERMINAL_NODE,
    security.TERMINAL_NODE,
    instruction_control.TERMINAL_NODE,
    cross_model.TERMINAL_NODE,
    multi_skill.TERMINAL_NODE,
)

# ---- 可能停在人工审批上的节点（docs/dev/24 第 3 节的汇总表）----
# 各维度导出的 `INTERRUPT_BEFORE_NODES` 的并集：模块一/三的优化闭环、模块五的 AppSec 闭环、模块六
# 的能力树规模审核。它们全部通过**动态** `interrupt()` 挂起（超出重试次数 / 树太大时才停）。
SUSPENDABLE_NODES: tuple[str, ...] = tuple(
    dict.fromkeys(
        [
            *preflight.INTERRUPT_BEFORE_NODES,
            *trigger_accuracy.INTERRUPT_BEFORE_NODES,
            *context_scoping.INTERRUPT_BEFORE_NODES,
            *instruction_control.INTERRUPT_BEFORE_NODES,
            *script_usability.INTERRUPT_BEFORE_NODES,
            *security.INTERRUPT_BEFORE_NODES,
            *coverage.INTERRUPT_BEFORE_NODES,
            *pruning.INTERRUPT_BEFORE_NODES,
            *weighted_coverage.INTERRUPT_BEFORE_NODES,
            *cross_model.INTERRUPT_BEFORE_NODES,
            *multi_skill.INTERRUPT_BEFORE_NODES,
        ]
    )
)

# ---- 实际传给 `compile(interrupt_before=...)` 的列表：刻意为空 ----
# docs/dev/24 正文第 3 节把上面那张表直接传给 `interrupt_before`。那样做的真实效果是：**每次运行**
# 在进入这些节点之前都无条件停下（静态断点不看"是否超出重试次数"），而这类停顿不写审批卡片、
# 工作台上看不到，流水线就永远停在模块六门口。interfaces/22 第 2 节第 8 条已经对审批闸门指出过
# 同一个问题；各维度接入文档里"列进静态列表只是为了编译期显式"的说法与 LangGraph 语义不符，
# 已随本文档修订。编译期显式性由 `SUSPENDABLE_NODES` 承担。
INTERRUPT_BEFORE_NODES: list[str] = []


@dataclass(slots=True)
class MainGraphDeps:
    """主图全部依赖的注入点。字段为 None 时各维度按自己的生产默认值惰性构造。

    模块七/八的依赖不单列：它们必须由模块六的依赖派生（`PruningDeps.from_coverage()`），共享同一个
    `AnalyzerAgent` 实例（interfaces/16 第 4 节）。
    """

    # 字段名与维度模块同名（调用方读起来自然），因此注解必须用显式导入的类名：写成
    # `preflight.PreflightDeps` 的话，`get_type_hints()` 会在类命名空间里把 `preflight` 解析成字段默认值。
    preflight: PreflightDeps | None = None
    trigger_accuracy: TriggerAccuracyDeps | None = None
    context_scoping: ContextScopingDeps | None = None
    instruction_control: InstructionControlDeps | None = None
    script_usability: ScriptUsabilityDeps | None = None
    security: SecurityDeps | None = None
    coverage: CoverageDeps | None = None
    cross_model: CrossModelDeps | None = None
    multi_skill: MultiSkillDeps | None = None
    pipeline: PipelineDeps | None = None
    approval_guard: ApprovalGuard | None = None


def route_after_preflight(state: MainGraphState) -> list[str]:
    """前置门禁通过后的分流：Nightly 只跑 COLD 回归，其余一律完整评测。

    返回列表 = 并行扇出（LangGraph 条件边支持返回多个目标）。
    """
    if state.get(KEY_MODE) == MODE_COLD_SUITE:
        return [COLD_SUITE_NODE]
    return list(PHASE_A_ENTRY_NODES)


def build_main_graph_builder(deps: MainGraphDeps | None = None) -> StateGraph[Any, Any, Any, Any]:
    """装配（未编译的）主图。单测直接用它检查拓扑，不需要 checkpointer。"""
    deps = deps or MainGraphDeps()
    builder: StateGraph[Any, Any, Any, Any] = StateGraph(MainGraphState)
    # 代理对象按鸭子类型充当 StateGraph（只拦截 add_node），各维度装配函数的签名写的是 StateGraph，
    # 这里显式标成 Any，而不是为了类型检查去改十个维度的函数签名。
    graph: Any = ApprovalGuardedBuilder(builder, deps.approval_guard)

    # ---- 平铺各维度节点（只加维度内部的边）----
    preflight.add_preflight_nodes(graph, deps.preflight)
    trigger_pipeline = trigger_accuracy.add_trigger_accuracy_nodes(graph, deps.trigger_accuracy)
    context_scoping.add_context_scoping_nodes(graph, deps.context_scoping)
    script_usability.add_script_usability_nodes(graph, deps.script_usability)
    security.add_security_nodes(graph, deps.security)
    instruction_control.add_instruction_control_nodes(graph, deps.instruction_control)
    coverage_pipeline = coverage.add_coverage_nodes(graph, deps.coverage)
    pruning_pipeline = pruning.add_pruning_nodes(
        graph, pruning.PruningDeps.from_coverage(coverage_pipeline.deps)
    )
    weighted_coverage.add_weighted_coverage_nodes(
        graph, weighted_coverage.WeightedCoverageDeps.from_coverage(pruning_pipeline.deps)
    )
    cross_model.add_cross_model_nodes(graph, deps.cross_model)
    multi_skill.add_multi_skill_nodes(graph, deps.multi_skill)

    # ---- 编排层节点 ----
    # COLD 回归复用模块一的 pipeline 实例：同一套执行后端、Judge 单例与信号量上限。
    pipeline_nodes = PipelineNodes(
        deps.pipeline, cold_suite=ColdSuiteRegression(trigger_pipeline=trigger_pipeline)
    )
    graph.add_node(NODE_NAMES["bootstrap_run"], pipeline_nodes.bootstrap_run)
    graph.add_node(COLD_SUITE_NODE, pipeline_nodes.cold_suite_regression)
    graph.add_node(NODE_NAMES["report"], pipeline_nodes.finalize_report)
    graph.add_node(NODE_NAMES["patch_pr"], pipeline_nodes.patch_to_pr)
    graph.add_node(NODE_NAMES["rag_archive"], pipeline_nodes.rag_archive)

    # ---- 入口 → Phase 0 ----
    builder.set_entry_point(NODE_NAMES["bootstrap_run"])
    builder.add_edge(NODE_NAMES["bootstrap_run"], preflight.ENTRY_NODE)
    builder.add_conditional_edges(
        preflight.TERMINAL_NODE,
        route_after_preflight,
        [*PHASE_A_ENTRY_NODES, COLD_SUITE_NODE],
    )

    # ---- 用例集准备节点串行化：模块一 → 模块五 → 模块三 → 模块六（理由见模块头第 1 条）----
    builder.add_edge(trigger_accuracy.ENTRY_NODE, security.ENTRY_NODE)
    # 模块三：等模块五准备完（用例集串行）且模块一训练集判定完（interfaces/13 第 3.1 节）。
    builder.add_edge(
        [security.ENTRY_NODE, trigger_accuracy.NODE_NAMES["judge_train_cases"]],
        instruction_control.ENTRY_NODE,
    )
    builder.add_edge(instruction_control.ENTRY_NODE, coverage.ENTRY_NODE)

    # ---- 模块九：读验证集，排在模块一终节点之后（interfaces/19 第 0 节）----
    builder.add_edge(trigger_accuracy.TERMINAL_NODE, cross_model.ENTRY_NODE)

    # ---- 覆盖率分区 16 → 17 → 18 → 模块十（硬顺序，interfaces/17、18、20 第 3 节）----
    builder.add_edge(coverage.TERMINAL_NODE, pruning.ENTRY_NODE)
    builder.add_edge(pruning.TERMINAL_NODE, weighted_coverage.ENTRY_NODE)
    builder.add_edge(weighted_coverage.TERMINAL_NODE, multi_skill.ENTRY_NODE)

    # ---- Phase E：同步屏障汇合 → 报告 → PR → 归档 ----
    builder.add_edge(list(DIMENSION_TERMINAL_NODES), NODE_NAMES["report"])
    builder.add_edge(COLD_SUITE_NODE, NODE_NAMES["report"])
    builder.add_edge(NODE_NAMES["report"], NODE_NAMES["patch_pr"])
    builder.add_edge(NODE_NAMES["patch_pr"], NODE_NAMES["rag_archive"])
    builder.set_finish_point(NODE_NAMES["rag_archive"])
    return builder


def build_main_graph(
    checkpointer: BaseCheckpointSaver[Any] | None,
    deps: MainGraphDeps | None = None,
) -> Any:
    """装配并编译主图。

    `checkpointer` 必填语义（允许传 None 只为本地冒烟）：Hermes Hook 挂起、优化闭环与全部人工审批
    都依赖 checkpoint 才能被唤醒。生产入口一律传 `build_async_checkpointer()` 的产物。
    编译后由调用方 `register_graph_resumer(CompiledGraphResumer(graph))`（见 `graph/runner.py`）。
    """
    return build_main_graph_builder(deps).compile(
        checkpointer=checkpointer,
        interrupt_before=INTERRUPT_BEFORE_NODES,
    )


__all__ = [
    "DIMENSION_TERMINAL_NODES",
    "INTERRUPT_BEFORE_NODES",
    "PHASE_A_ENTRY_NODES",
    "SUSPENDABLE_NODES",
    "MainGraphDeps",
    "build_main_graph",
    "build_main_graph_builder",
    "route_after_preflight",
]
