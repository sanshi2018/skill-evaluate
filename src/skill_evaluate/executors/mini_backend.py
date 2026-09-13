"""`MiniAgentBackend`：内置轻量后端（docs/dev/03 第 3 节）。

不启动外部沙箱、不实际执行 `scripts/`。用于模块二（静态审查）、模块四（部分
子检查，如 `--help` 文本审查可复用 mini 调用）等"偏文本/逻辑判断"场景。此时
`ExecutionTrace` 中：
  - `actions` 为空或仅含一条虚拟 "static_review" 步骤
  - `modified_files_manifest` 恒为空
  - `loaded_skill_md` 恒为 True（因为整篇 SKILL.md 就是作为 prompt 输入的）

关键约束：`MiniAgentBackend` 不适用于任何需要验证"Agent 是否真的调用了工具 /
是否触发了 Skill / 是否产出了文件"的评测节点（模块一/三/四/五）——这些必须用
`PluggableAgentBackend`，路由关系见 `routing.py`。

实际的评审 Prompt 模板由 docs/dev/07（Mini Agent 评审框架）封装；本文件只负责
把"调用一次 LLM + 记录 timing"包装成合法的 `ExecutionTrace`。

**接入状态（docs/dev/07 已落地）**：`executors/factory.py::build_backend()` 现在
注入 `agents/mini/llm_client.py::RealMiniLLMClient`，`final_response` 是真实模型
输出，调用方可以据此做业务决策。下面的 `StubMiniLLMClient` 保留为**显式注入用的
测试替身**（无参构造 `MiniAgentBackend()` 时仍是它），不再是生产默认值。
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Protocol

from skill_evaluate.executors.base import ExecutionRequest, ExecutorBackend
from skill_evaluate.executors.sanitize import truncate_field
from skill_evaluate.logging import get_logger
from skill_evaluate.state.enums import ExecutorBackendType
from skill_evaluate.state.trace import ActionStep, ExecutionTrace, TimingCostMetrics

logger = get_logger(component="mini_backend")


class MiniLLMClient(Protocol):
    """docs/dev/07 需要实现并注入的最小 LLM 调用协议。"""

    async def complete(self, *, prompt: str, model: str, temperature: float) -> MiniLLMResult: ...


class MiniLLMResult:
    __slots__ = ("completion_tokens", "prompt_tokens", "text")

    def __init__(self, text: str, prompt_tokens: int, completion_tokens: int) -> None:
        self.text = text
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class StubMiniLLMClient:
    """占位实现：仅供测试/降级场景显式注入。

    生产路径已由 docs/dev/07 的 `RealMiniLLMClient` 接管（见
    docs/dev/interfaces/07_review_template_registry.md 第 6 节）。响应文本里保留
    `[stub]` 标记，任何时候在真实报告里看到它都说明后端被错误地无参构造了。
    """

    async def complete(self, *, prompt: str, model: str, temperature: float) -> MiniLLMResult:
        text = (
            "[stub] MiniAgentBackend 尚未接入真实的 Mini Agent 评审框架"
            "（docs/dev/07），本响应仅用于保证 Trace 结构合法，不代表任何评审结论。"
        )
        return MiniLLMResult(
            text=text, prompt_tokens=len(prompt) // 4, completion_tokens=len(text) // 4
        )


class MiniAgentBackend(ExecutorBackend):
    backend_type = ExecutorBackendType.MINI

    def __init__(
        self, llm_client: MiniLLMClient | None = None, model: str = "mini-agent-default"
    ) -> None:
        self._llm_client: MiniLLMClient = llm_client or StubMiniLLMClient()
        self._model = model

    async def execute(self, request: ExecutionRequest) -> ExecutionTrace:
        if request.assertion_specs:
            # docs/dev/10 第 4.4 节：Mini 后端没有真实沙箱，断言脚本无处可跑。
            # 忽略并告警，而不是报错——这属于节点路由配错的旁路情况，不该让一次
            # 静态审查因此失败；warning 会在日志里指认是哪个用例传错了。
            logger.warning(
                "mini_backend_assertion_specs_ignored",
                case_id=request.case.case_id,
                spec_count=len(request.assertion_specs),
                hint="断言脚本执行要求真实沙箱，请把该维度路由到 PLUGGABLE 后端",
            )
        if request.background_skills:
            # docs/dev/20：Mini 后端只把目标 SKILL.md 当 prompt，没有"多个 Skill 同时挂载、
            # Agent 自己决定加载谁"这回事。忽略并告警，理由同上。
            logger.warning(
                "mini_backend_background_skills_ignored",
                case_id=request.case.case_id,
                background_skill_ids=[s.skill_id for s in request.background_skills],
                hint="多技能并发要求真实沙箱，请把该维度路由到 PLUGGABLE 后端",
            )
        prompt = self._build_prompt(request)
        temperature = 0.1
        if request.sampling_overrides:
            temperature = request.sampling_overrides.get("temperature", temperature)

        start = time.monotonic()
        started_at = datetime.now(UTC)
        try:
            result = await self._llm_client.complete(
                prompt=prompt, model=self._model, temperature=temperature
            )
            final_response = truncate_field(result.text) or ""
            action = ActionStep(
                step_id=0,
                timestamp=started_at,
                thought="static_review",
                action_type="static_review",
                action_input={"skill_id": request.skill.skill_id, "case_id": request.case.case_id},
                exit_code=0,
                stdout=None,
                stderr=None,
            )
            prompt_tokens = result.prompt_tokens
            completion_tokens = result.completion_tokens
        except Exception as exc:  # noqa: BLE001 - 容错约定：execute() 内部吞掉一切异常
            final_response = f"[MiniAgentBackend internal_error] {exc}"
            action = ActionStep(
                step_id=0,
                timestamp=started_at,
                thought=None,
                action_type="internal_error",
                action_input={"exception_type": type(exc).__name__},
                exit_code=1,
                stdout=None,
                stderr=truncate_field(str(exc)),
            )
            prompt_tokens = 0
            completion_tokens = 0

        finished_at = datetime.now(UTC)
        duration_ms = int((time.monotonic() - start) * 1000)

        return ExecutionTrace(
            trace_id=str(uuid.uuid4()),
            case_id=request.case.case_id,
            run_index=request.run_index,
            backend_type=self.backend_type.value,
            loaded_skill_md=True,
            timing=TimingCostMetrics(
                total_tokens=prompt_tokens + completion_tokens,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                duration_ms=duration_ms,
            ),
            actions=[action],
            final_response=final_response,
            modified_files_manifest=[],
            started_at=started_at,
            finished_at=finished_at,
        )

    async def health_check(self) -> bool:
        return True

    @staticmethod
    def _build_prompt(request: ExecutionRequest) -> str:
        return (
            f"SKILL_ID: {request.skill.skill_id}\n"
            f"SKILL_DESCRIPTION: {request.skill.description}\n"
            f"SKILL_BODY:\n{request.skill.body_markdown}\n\n"
            f"CASE_PROMPT:\n{request.case.prompt}\n"
        )
