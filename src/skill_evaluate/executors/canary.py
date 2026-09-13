"""金丝雀技能探针（docs/dev/21 第 5 节）。

在正式评测跑之前，让执行后端先跑一个"永远不应该失败"的极简技能：读取 `data.txt` 并输出
`{"content": ...}`。它失败只能说明沙箱底层（I/O、权限、网络配置、执行引擎）坏了——此时继续
跑评测只会产出大面积与被测 Skill 质量无关的假阴性。

## 为什么放在 executors/ 而不是 nodes/preflight/

它只依赖 `ExecutorBackend` 协议，与"图里怎么调度"无关；放在这里，`HermesBackend`、CLI 冒烟
命令与 docs/dev/24 的主图节点都能直接复用，而不会形成 executors → nodes 的反向依赖。

## 判定口径（比 docs/dev/21 正文更严）

正文只检查 `loaded_skill_md and '"content"' in final_response`。那会把"输出了一个 content 字段
但内容是模型编的"判成健康——而文件读不到时模型最常见的行为恰恰是编一个。这里要求：

1. Trace 不是失败态（末尾动作不是 `internal_error` / `sandbox_timeout`）；
2. 目标 SKILL.md 确实被加载；
3. 最终回复里能解析出 JSON 对象，且 `content` 与 `data.txt` 原文（去首尾空白）**逐字相等**。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from langgraph.errors import GraphBubbleUp

from skill_evaluate.errors import ExecutorBackendError
from skill_evaluate.executors.base import ExecutionRequest, ExecutorBackend
from skill_evaluate.executors.hermes_backend import (
    ACTION_TYPE_INTERNAL_ERROR,
    ACTION_TYPE_SANDBOX_TIMEOUT,
)
from skill_evaluate.logging import get_logger
from skill_evaluate.state.enums import DatasetSplit, TestCaseCategory
from skill_evaluate.state.skill import SkillDefinition, SkillReferenceFile
from skill_evaluate.state.test_case import TestCase
from skill_evaluate.state.trace import ExecutionTrace

logger = get_logger(component="canary_probe")

CANARY_SKILL_DIR = Path(__file__).parent / "canary_skill"
CANARY_SKILL_ID = "__canary__"
CANARY_CASE_PREFIX = "__canary__"
CANARY_PROMPT = "请读取 data.txt，并只输出一个 JSON 对象，把文件内容放在 content 字段里。"

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(slots=True)
class CanaryProbeResult:
    """一次探针的结论。`reasons` 在失败时逐条列出原因，直接进日志与挂起说明。"""

    passed: bool
    reasons: list[str] = field(default_factory=list)
    trace_id: str | None = None


def load_canary_skill() -> SkillDefinition:
    """构造金丝雀技能定义。

    `version_ref` 取 SKILL.md + data.txt 的内容哈希而不是 git sha：探针文件随本包分发，安装成
    wheel 后没有 git 目录；而内容哈希恰好回答了"探针本身有没有被改过"。
    `root_path` 指向包内目录，沙箱客户端按它挂载 `data.txt`（与普通 Skill 的 scripts/ 同一挂载契约）。
    """
    skill_md = (CANARY_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    data = (CANARY_SKILL_DIR / "data.txt").read_text(encoding="utf-8")
    digest = hashlib.sha256(f"{skill_md}\0{data}".encode()).hexdigest()[:12]
    description = _frontmatter_description(skill_md)
    return SkillDefinition(
        skill_id=CANARY_SKILL_ID,
        version_ref=f"canary-{digest}",
        root_path=str(CANARY_SKILL_DIR),
        description=description,
        body_markdown=skill_md,
        line_count=skill_md.count("\n") + 1,
        token_count=len(skill_md) // 2,
        reference_files=[SkillReferenceFile(path="data.txt")],
    )


def expected_canary_content() -> str:
    return (CANARY_SKILL_DIR / "data.txt").read_text(encoding="utf-8").strip()


def build_canary_request(*, run_id: str, timeout_s: int) -> ExecutionRequest:
    """构造探针执行请求。

    `case_id` 带上 run_id：`execution_traces` 以 `(case_id, run_index)` 唯一、`pending_hooks`
    以 `(run_id, case_id, run_index)` 唯一，固定写 `__canary__` 会让不同运行的探针 Trace 互相
    覆盖，排查"上周那次探针到底输出了什么"时就查不到了。
    """
    skill = load_canary_skill()
    case = TestCase(
        case_id=f"{CANARY_CASE_PREFIX}:{run_id}",
        skill_id=skill.skill_id,
        category=TestCaseCategory.POSITIVE,
        split=DatasetSplit.TRAIN,
        prompt=CANARY_PROMPT,
        expected_output=json.dumps({"content": expected_canary_content()}, ensure_ascii=False),
        generator_run_id=CANARY_SKILL_ID,
        created_at=datetime.now(UTC),
    )
    return ExecutionRequest(
        skill=skill,
        case=case,
        run_index=0,
        wall_clock_timeout_s=timeout_s,
        run_id=run_id,
    )


def verify_canary_trace(trace: ExecutionTrace) -> CanaryProbeResult:
    """确定性校验探针 Trace（口径见模块头）。"""
    reasons: list[str] = []
    if trace.actions and trace.actions[-1].action_type in {
        ACTION_TYPE_INTERNAL_ERROR,
        ACTION_TYPE_SANDBOX_TIMEOUT,
    }:
        reasons.append(
            f"执行失败态：{trace.actions[-1].action_type}（{(trace.actions[-1].stderr or '')[:200]}）"
        )
    if not trace.loaded_skill_md:
        reasons.append("金丝雀 SKILL.md 未被加载")

    content = _extract_content(trace.final_response)
    expected = expected_canary_content()
    if content is None:
        reasons.append("最终回复中没有可解析的 JSON 对象或缺少 content 字段")
    elif content.strip() != expected:
        reasons.append(
            f"content 与 data.txt 不一致：期望 {expected!r}，实际 {content.strip()[:200]!r}"
        )

    return CanaryProbeResult(passed=not reasons, reasons=reasons, trace_id=trace.trace_id)


async def run_canary_probe(
    backend: ExecutorBackend, *, run_id: str, timeout_s: int
) -> CanaryProbeResult:
    """先做廉价可达性检查，再真实执行一次探针任务并校验。

    必须在已挂载 checkpointer 的图节点里调用（PLUGGABLE 后端的 `execute()` 会挂起等 Hook）。
    `GraphBubbleUp` 原样上抛——那是挂起信号，不是失败。
    """
    if not await backend.health_check():
        return CanaryProbeResult(
            passed=False, reasons=["执行后端 health_check() 返回 False（不可达）"]
        )
    try:
        trace = await backend.execute(build_canary_request(run_id=run_id, timeout_s=timeout_s))
    except GraphBubbleUp:
        raise
    except ExecutorBackendError as exc:
        return CanaryProbeResult(passed=False, reasons=[f"执行后端报错：{exc}"])
    result = verify_canary_trace(trace)
    logger.info(
        "canary_probe_executed",
        run_id=run_id,
        passed=result.passed,
        reasons=result.reasons,
        trace_id=result.trace_id,
    )
    return result


def _extract_content(final_response: str) -> str | None:
    """从最终回复里取 `content`：先整段按 JSON 解析，不行再取最外层花括号（容忍代码块围栏）。"""
    candidates = [final_response.strip()]
    match = _JSON_OBJECT_RE.search(final_response)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("content"), str):
            return str(payload["content"])
    return None


def _frontmatter_description(skill_md: str) -> str:
    for line in skill_md.splitlines():
        if line.startswith("description:"):
            return line.partition(":")[2].strip()
    return "skill-evaluate canary probe"


__all__ = [
    "CANARY_CASE_PREFIX",
    "CANARY_SKILL_DIR",
    "CANARY_SKILL_ID",
    "CanaryProbeResult",
    "build_canary_request",
    "expected_canary_content",
    "load_canary_skill",
    "run_canary_probe",
    "verify_canary_trace",
]
