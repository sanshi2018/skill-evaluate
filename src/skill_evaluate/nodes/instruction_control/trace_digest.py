"""把一条 `ExecutionTrace` 压成可以塞进评审 Prompt 的文本（docs/dev/13 第 5 节）。

## 为什么不能直接 `[a.model_dump() for a in trace.actions]`

docs/dev/13 正文的伪代码就是这么写的，但真跑起来会有两个问题：

1. **体量**。一条真实执行轨迹可以有上百步，每步的 `stdout` 上限是 32KB
   （`executors/sanitize.py`）。整串 dump 进 Prompt 轻松几 MB，超上下文只是其一，
   更麻烦的是裁判会在噪音里抓不住重点——效率诊断要看的是"动作之间的关系"，
   不是每条命令的完整输出。
2. **泄密面**。Trace 里可能带着沙箱环境变量、token 之类的东西。发给 LLM 之前
   统一过一遍 `redact_secrets()`，与日志侧同一套口径（docs/dev/05 第 5 节）。

## 截断策略：留头尾，掐中间

超过步数上限时保留**开头**与**结尾**各一半，中间用一行省略标记代替。效率诊断
关心的恰恰是这两端——开头看它有没有一上来就走错方向，结尾看它有没有在收尾时
反复折腾；中间的重复劳动只要有几步样本就足够看出模式了。
"""

from __future__ import annotations

from skill_evaluate.observability.log_sanitize import redact_secrets
from skill_evaluate.state.trace import ActionStep, ExecutionTrace

_OMISSION_LINE = "... [中间 {count} 步已省略，只保留首尾以控制 Prompt 体量] ..."


def _clip(value: str | None, limit: int) -> str:
    """单字段截断 + 脱敏。空值统一渲染成短横线，让 Prompt 里的表格对齐可读。"""
    if not value:
        return "-"
    redacted = redact_secrets(value)
    if len(redacted) <= limit:
        return redacted
    return redacted[:limit] + f"...[已截断，原长 {len(redacted)} 字符]"


def _format_step(step: ActionStep, *, max_output_chars: int) -> str:
    """一步动作渲染成几行文本。

    `thought` 单独列出来是因为效率诊断的三个检查项（反复试错 / 盲目执行 / 选择
    困难）判的都是**模型的意图**，而意图只在 thought 里；只给命令和输出，裁判
    就只能靠猜。
    """
    lines = [
        f"[step:{step.step_id}] action_type={step.action_type} exit_code={step.exit_code}",
        f"  thought: {_clip(step.thought, max_output_chars)}",
        f"  input: {_clip(str(step.action_input), max_output_chars)}",
        f"  stdout: {_clip(step.stdout, max_output_chars)}",
        f"  stderr: {_clip(step.stderr, max_output_chars)}",
    ]
    return "\n".join(lines)


def format_actions_for_review(
    trace: ExecutionTrace, *, max_steps: int = 40, max_output_chars: int = 400
) -> str:
    """把动作序列渲染成 `trace_efficiency` 模板的 `actions` 变量。

    没有任何动作时返回一句明确的说明而不是空串：空串在 Prompt 里看起来像是渲染
    出了问题，而"这次执行一步工具都没调"本身是个很有信息量的事实（多半意味着
    Agent 直接凭记忆答了）。
    """
    steps = trace.actions
    if not steps:
        return "（这次执行没有任何工具调用记录：Agent 未经任何动作直接给出了答复。）"

    if len(steps) <= max_steps:
        selected = list(steps)
        omitted = 0
    else:
        head = max_steps // 2
        tail = max_steps - head
        selected = [*steps[:head], *steps[-tail:]]
        omitted = len(steps) - max_steps

    rendered = [_format_step(step, max_output_chars=max_output_chars) for step in selected]
    if omitted:
        head_count = max_steps // 2
        rendered.insert(head_count, _OMISSION_LINE.format(count=omitted))
    header = f"共 {len(steps)} 步动作" + (f"（下面展示其中 {len(selected)} 步）" if omitted else "")
    return header + "\n\n" + "\n\n".join(rendered)


def format_final_response(trace: ExecutionTrace, *, max_chars: int = 4000) -> str:
    """最终答复的脱敏 + 截断版本。

    上限比单步输出宽松得多：ROI 判定要比较两边答复的质量，砍太狠会把差异本身
    砍掉。
    """
    return _clip(trace.final_response, max_chars)


__all__ = ["format_actions_for_review", "format_final_response"]
