"""自我一致性扰动测试：3 副本背靠背复核与共识判定（docs/dev/08 第 4 节）。

## 关于"温度扰动"这条路的定稿

架构文档要求"派生 3 个具有微小参数扰动（Temperature=0.1/0.3/0.5）的 Judge 副本"。
`docs/dev/interfaces/06_llm_client_and_sampling.md` 第 2 节指出：新一代 Claude 模型
（含默认的 `judge_model`）已移除 `temperature`，这条路在默认配置下**物理不成立**，
并把定稿权交给了本文档。

本文档的定稿：**默认走"Prompt 视角扰动"（`consensus_strategy="perspective"`），
另外两条方案保留为配置项。**

- 选它的理由不只是"温度用不了"。扰动的目的是检验"这个结论稳不稳"，而温度扰动
  检验的其实是"同一个裁判掷三次骰子会不会掷出不同结果"；视角扰动是让三个裁判
  分别从**证据充分性 / 反例存在性 / 判定一致性**切入同一份材料——三个人从三个
  角度看完都得出同一结论，比一个人抖三次手更接近"高置信度"的本意。
- `temperature`（字面温度扰动）与 `model`（跨模型共识，与 docs/dev/19 共享基础
  设施）都保留在 `JudgeSettings` 里，换一个仍支持采样的 `judge_model` 或想做跨模型
  复核时改配置即可，不改代码。

**`JudgeVerdict.temperature` 的口径**（docs/dev/interfaces/06 要求写明）：记录的是
**请求值**，即本副本要求下发的温度。模型是否真的接受了这个参数，由
`agents/llm.py::model_supports_sampling()` 决定并在
`LLMCompletion.temperature_applied` 里如实反映；`perspective` 策略下三副本的
`temperature` 字段会是同一个值——这正是"本次扰动不来自温度"的诚实体现，报告
读者不会误以为做了一次实际没发生的温度扰动。副本之间真正的差异记录在
`ConsensusResult.verdicts` 的顺序与 `dissenting_node` 摘要里。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from skill_evaluate.state.enums import JudgeVerdictStatus
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict

# docs/dev/08 第 7 节对 docs/dev/07 模板体系的追加约定：CRITICAL 场景要求 reasoning
# 用 `[step:N]` 标注它依据的 Trace 步骤。作为 system 后缀统一下发，而不是要求每个
# 模板各自在 .jinja 里重复写一遍——约定是框架级的，落实点就该只有一个。
STEP_CITATION_RULE = (
    "本次判定会进入高置信度复核流程。若你的依据来自执行 Trace 的某个步骤，"
    "请在 reasoning 中用 `[step:N]` 格式标注（N 为该步骤的 step_id），"
    "可标注多个。判定依据不涉及任何 Trace 步骤时（例如纯静态文本审查），"
    "不要编造 step_id。"
)

# 三个审查视角。措辞刻意保持"切入角度"而非"立场"——让副本站队（一个唱红脸一个
# 唱白脸）会制造出人为的分歧，共识率会被压到毫无意义的低位。
PERSPECTIVE_SUFFIXES: tuple[tuple[str, str], ...] = (
    (
        "evidence_sufficiency",
        (
            "复核视角：**证据充分性**。先问「支持这个结论的原文/步骤够不够硬」，"
            "证据不足以支撑一个 fail 时就给 pass。"
        ),
    ),
    (
        "counterexample_search",
        "复核视角：**反例存在性**。先主动为「相反结论」找一遍证据，找不到反例再下结论。",
    ),
    (
        "internal_consistency",
        (
            "复核视角：**判定一致性**。检查材料内部是否自相矛盾，"
            "并确认你的结论对材料中每一处相关内容都成立，而不只是对最显眼的那一处。"
        ),
    ),
)

_STEP_MARKER_RE = re.compile(r"\[step:\s*(\d+)\s*\]")


@dataclass(frozen=True, slots=True)
class ReplicaSpec:
    """一个复核副本的配置。三份 spec 之间的差异就是这次扰动的全部来源。"""

    label: str
    model: str | None  # None = 用调用方的默认模型
    temperature: float
    system_suffix: str


def build_replica_specs(
    *,
    strategy: str,
    base_temperature: float,
    temperatures: Sequence[float],
    models: Sequence[str],
) -> list[ReplicaSpec]:
    """按策略产出 3 份副本配置。

    未知策略名不静默回落到默认值——配置写错了却"看起来在跑共识"，比直接报错
    危险得多。
    """
    citation = STEP_CITATION_RULE
    if strategy == "perspective":
        return [
            ReplicaSpec(
                label=label,
                model=None,
                temperature=base_temperature,
                system_suffix=f"{citation}\n\n{suffix}",
            )
            for label, suffix in PERSPECTIVE_SUFFIXES
        ]
    if strategy == "temperature":
        return [
            ReplicaSpec(
                label=f"temperature_{temperature}",
                model=None,
                temperature=temperature,
                system_suffix=citation,
            )
            for temperature in temperatures
        ]
    if strategy == "model":
        if not models:
            raise ValueError(
                "consensus_strategy='model' 需要配置 SKILLEVAL_JUDGE_CONSENSUS_MODELS，"
                "否则三个副本会是同一个模型，扰动等于没做。"
            )
        return [
            ReplicaSpec(
                label=f"model_{model}",
                model=model,
                temperature=base_temperature,
                system_suffix=citation,
            )
            for model in models
        ]
    raise ValueError(
        f"未知的 consensus_strategy={strategy!r}；可选：perspective / temperature / model"
    )


def extract_cited_step_ids(reasoning: str) -> set[int]:
    """从 reasoning 中解析 `[step:N]` 标记。"""
    return {int(m) for m in _STEP_MARKER_RE.findall(reasoning)}


def reasoning_points_to_same_trace_node(verdicts: Sequence[JudgeVerdict]) -> bool:
    """架构文档要求的"三份 reasoning 指向 Trace 树的同一个行为节点"。

    实现为"引用的 step_id 集合有交集"。两处边界的处理都写在这里，避免各维度各
    猜一套：

    - **三份都没引用任何步骤**：视为该维度不适用（纯静态文本审查根本没有 Trace
      可引），本条件放行，共识只看结论是否一致。把"无法引用"判成"没有共识"会
      让模块二这类静态审查永远拿不到 CRITICAL 判定。
    - **部分引用、部分没引用**：只在**确实引用了**的那些副本之间求交集。没引用
      的那份不构成"指向了别的节点"，不该拖累共识。
    """
    cited = [ids for ids in (extract_cited_step_ids(v.reasoning) for v in verdicts) if ids]
    if not cited:
        return True
    return bool(set.intersection(*cited))


def summarize_dissent(verdicts: Sequence[JudgeVerdict]) -> str:
    """未达成共识时，给人工仲裁者一句能直接读懂的分歧摘要。

    落到 `ConsensusResult.dissenting_node`。字段名叫 node 是因为架构文档关注的是
    "指向哪个行为节点"，但结论本身不一致时，先说清楚"谁判了什么"更有用。
    """
    by_status: dict[str, int] = {}
    for verdict in verdicts:
        by_status[verdict.status.value] = by_status.get(verdict.status.value, 0) + 1
    status_summary = "、".join(f"{status}×{count}" for status, count in sorted(by_status.items()))

    step_sets = [sorted(extract_cited_step_ids(v.reasoning)) for v in verdicts]
    if len(by_status) > 1:
        return f"结论不一致（{status_summary}）；各副本引用的 Trace 步骤：{step_sets}"
    return (
        f"结论一致（{status_summary}）但 reasoning 指向不同的 Trace 步骤：{step_sets}；"
        "同一个结论建立在互不相干的依据上，不构成高置信度判决。"
    )


def evaluate_consensus(subject_id: str, verdicts: Sequence[JudgeVerdict]) -> ConsensusResult:
    """

    未达成共识时 `final_status=NEEDS_HUMAN_REVIEW`。**这是给调用方的强约束**
    （docs/dev/08 第 4.4 节）：调用方应路由到人工挂起，既不放行也不判失败，不允许
    自行把它降级成 PASS 或 FAIL。
    """
    if not verdicts:
        raise ValueError("共识判定至少需要一份 verdict")

    same_status = len({v.status for v in verdicts}) == 1
    same_node = reasoning_points_to_same_trace_node(verdicts)
    consensus_reached = same_status and same_node

    return ConsensusResult(
        subject_id=subject_id,
        verdicts=list(verdicts),
        consensus_reached=consensus_reached,
        final_status=(
            verdicts[0].status if consensus_reached else JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        ),
        dissenting_node=None if consensus_reached else summarize_dissent(verdicts),
    )


__all__ = [
    "PERSPECTIVE_SUFFIXES",
    "STEP_CITATION_RULE",
    "ReplicaSpec",
    "build_replica_specs",
    "evaluate_consensus",
    "extract_cited_step_ids",
    "reasoning_points_to_same_trace_node",
    "summarize_dissent",
]
