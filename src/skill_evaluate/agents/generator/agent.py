"""Generator Agent 本体（docs/dev/06 第 2、5 节）。

**职责边界**：只产出 `TestCase` 列表。不执行、不判定、不落库、不决定复用还是
重生——那些是 `service.py` 的事。这个边界让模块六/七/九/十可以各自构造不同的
`GenerationRequest` 复用同一个生成器，而不需要各自实现一个"生成器"。

**与 MiniAgentBackend 的关系**（docs/dev/06 第 5.3 节 vs docs/dev/07 第 2 节）：
docs/dev/06 说"Generator LLM 调用走 MiniAgentBackend"，docs/dev/07 澄清了
`MiniAgentBackend` 是"用什么后端跑"（产出 `ExecutionTrace`）、业务智能体是
"跑什么逻辑"。出题不是一次"执行"，把它硬包成 `ExecutionTrace` 会往
`execution_traces` 表里塞一批 `case_id` 无处安放的假记录。因此这里按
docs/dev/06 第 10 节的最终形态实现：继承 `BaseLLMAgent`，复用同一套 LLM 通道
与 Langfuse 打点，不经过 `ExecutionTrace` 这一层。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from skill_evaluate.agents.base import BaseLLMAgent
from skill_evaluate.agents.generator.schema import (
    CapabilityFocus,
    GeneratedCaseBatch,
    GenerationRequest,
)
from skill_evaluate.agents.llm import AgentLLMClient
from skill_evaluate.agents.templating import build_prompt_env
from skill_evaluate.config import get_settings
from skill_evaluate.errors import AgentResponseFormatError, GenerationError
from skill_evaluate.observability.langfuse_adapter import LangfuseAdapter, LangfuseTraceHandle
from skill_evaluate.state.enums import DatasetSplit, TestCaseCategory
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase

_PROMPT_DIR = Path(__file__).parent / "prompts"
_TEMPLATE_BY_CATEGORY: dict[TestCaseCategory, str] = {
    TestCaseCategory.POSITIVE: "positive.jinja",
    TestCaseCategory.NEGATIVE: "negative.jinja",
}

_SYSTEM_PROMPT = (
    "你是一位资深的 Agent Skill 测试设计者。你的产出会直接作为 CI/CD 流水线的"
    "评测输入，因此质量标准是：宁可少出一条，也不出一条自己都说不清为什么属于"
    "该类别的用例。你只输出 JSON，不输出任何解释性文字。"
)

# 从 description 抽关键词时剔除的高频虚词（中英混合）。反向用例的"近似度"依赖
# 关键词质量，把 "the"/"用于" 这类词喂给模型只会稀释锚点。
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "for",
        "in",
        "on",
        "with",
        "is",
        "are",
        "be",
        "this",
        "that",
        "it",
        "as",
        "by",
        "from",
        "use",
        "used",
        "using",
        "when",
        "user",
        "skill",
        "should",
        "can",
        "will",
        "you",
        "your",
        "的",
        "了",
        "和",
        "与",
        "或",
        "在",
        "是",
        "为",
        "用于",
        "使用",
        "这个",
        "一个",
        "可以",
        "需要",
        "进行",
        "以及",
        "当",
        "时",
        "该",
        "对",
    }
)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}|[一-鿿]{2,}")


class GeneratorAgent(BaseLLMAgent):
    """按 `GenerationRequest` 产出 `TestCase` 列表。"""

    name = "generator_agent"

    def __init__(
        self,
        *,
        model: str | None = None,
        temperature: float = 1.0,
        llm_client: AgentLLMClient | None = None,
        langfuse_adapter: LangfuseAdapter | None = None,
        trace_handle: LangfuseTraceHandle | None = None,
    ) -> None:
        # 出题需要发散，温度取高位。注意：新一代 Claude 模型已移除采样参数，
        # 此时该值不会被发送（见 agents/llm.py 的能力门禁），多样性完全由
        # Prompt 中的四类表述硬约束保证——这正是把多样性写进模板而不是
        # "靠调温度碰运气"的原因（docs/dev/06 第 5.1 节）。
        super().__init__(
            model=model or get_settings().llm.generator_model,
            temperature=temperature,
            llm_client=llm_client,
            langfuse_adapter=langfuse_adapter,
            trace_handle=trace_handle,
        )
        self._env = build_prompt_env(_PROMPT_DIR)

    async def generate(
        self, request: GenerationRequest, *, generator_run_id: str
    ) -> list[TestCase]:
        """按 request 中声明的每个 category 各调用一次 LLM，汇总为 TestCase 列表。

        任一批次失败即整体失败（抛 `GenerationError`），不返回半成品用例集——
        半成品会让下游误以为"这个 Skill 的正向用例天然就只有 3 条"。
        """
        cases: list[TestCase] = []
        for category in request.categories:
            count = request.count_for(category)
            if count <= 0:
                continue
            cases.extend(
                await self._generate_category(
                    request, category=category, count=count, generator_run_id=generator_run_id
                )
            )
        if not cases:
            raise GenerationError(
                f"Generator 未能为 skill_id={request.skill.skill_id!r} 产出任何用例"
                f"（categories={[c.value for c in request.categories]}）"
            )
        return cases

    async def _generate_category(
        self,
        request: GenerationRequest,
        *,
        category: TestCaseCategory,
        count: int,
        generator_run_id: str,
    ) -> list[TestCase]:
        template_name = _TEMPLATE_BY_CATEGORY.get(category)
        if template_name is None:
            raise GenerationError(
                f"category={category.value} 尚无对应的生成模板。"
                "ADVERSARIAL 由 docs/dev/15（Attacker Agent）、MULTI_SKILL 由 "
                "docs/dev/20 各自新增模板并在 _TEMPLATE_BY_CATEGORY 注册，"
                "接入方式见 docs/dev/interfaces/06_generator_extension_points.md。"
            )

        prompt = self._env.get_template(template_name).render(
            skill=request.skill,
            count=count,
            focus=request.capability_focus,
            seed_texts=self._resolve_seed_texts(request),
            keywords=extract_keywords(request.skill),
        )

        try:
            batch = await self._call_llm(prompt, GeneratedCaseBatch, system=_SYSTEM_PROMPT)
        except AgentResponseFormatError as exc:
            # docs/dev/06 第 5.3 节：重试用尽后判定为 GenerationFailure，整体失败。
            raise GenerationError(
                f"生成 {category.value} 用例失败（skill_id={request.skill.skill_id}）：{exc}"
            ) from exc

        now = datetime.now(UTC)
        return [
            TestCase(
                case_id=str(uuid.uuid4()),
                skill_id=request.skill.skill_id,
                category=category,
                # split 由 service._split_dataset() 统一改写，这里先占位。
                split=DatasetSplit.TRAIN,
                prompt=generated.prompt,
                expected_output=generated.expected_output,
                target_capability_ids=generated.target_capability_ids,
                negative_constraint_ids=generated.negative_constraint_ids,
                seed_anchor_id=None,
                generator_run_id=generator_run_id,
                created_at=now,
            )
            for generated in batch.cases
        ]

    @staticmethod
    def _resolve_seed_texts(request: GenerationRequest) -> list[str]:
        """把 `seed_anchor_ids` 解析成 few-shot 文本。

        当前为**简化版**（docs/dev/06 第 7 节）：没有版本化的种子库，直接把 id
        原文当作示例文本注入。docs/dev/21 接入 GitHub 托管的种子锚点配置后替换
        本方法的实现即可，调用点不变。
        """
        return list(request.seed_anchor_ids or [])


def extract_keywords(skill: SkillDefinition, limit: int = 12) -> list[str]:
    """从 description（辅以正文标题）抽取反向用例的关键词锚点。

    docs/dev/06 第 5.2 节要求"围绕这些关键词但故意跑题"，关键词的可控性直接决定
    反向用例的近似度。这里用词频而不是让模型自己去猜哪些词重要——同一份 Skill
    每次生成拿到的锚点应当稳定，否则反向用例集会在两次生成之间漂移。
    """
    headings = "\n".join(
        line for line in skill.body_markdown.splitlines() if line.lstrip().startswith("#")
    )
    # description 权重更高：它才是触发判定的实际依据，重复一次相当于加权。
    corpus = f"{skill.description}\n{skill.description}\n{headings}"

    counts: dict[str, int] = {}
    for match in _WORD_RE.finditer(corpus):
        word = match.group(0)
        normalized = word.lower() if word.isascii() else word
        if normalized in _STOPWORDS:
            continue
        counts[normalized] = counts.get(normalized, 0) + 1

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _ in ranked[:limit]]


__all__ = ["CapabilityFocus", "GenerationRequest", "GeneratorAgent", "extract_keywords"]
