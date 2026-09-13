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
from skill_evaluate.agents.generator.prompts.registry import (
    GenerationTemplate,
    get_generation_template,
)
from skill_evaluate.agents.generator.schema import (
    CapabilityFocus,
    GeneratedCase,
    GeneratedCaseBatch,
    GenerationRequest,
)
from skill_evaluate.agents.generator.seed_anchors import (
    SeedAnchorSource,
    get_default_seed_anchor_resolver,
)
from skill_evaluate.agents.llm import AgentLLMClient
from skill_evaluate.agents.templating import build_prompt_env
from skill_evaluate.config import get_settings
from skill_evaluate.errors import AgentResponseFormatError, GenerationError
from skill_evaluate.logging import get_logger
from skill_evaluate.observability.langfuse_adapter import LangfuseAdapter, LangfuseTraceHandle
from skill_evaluate.observability.log_sanitize import sanitize_for_log
from skill_evaluate.state.enums import DatasetSplit, TestCaseCategory
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase

_PROMPT_DIR = Path(__file__).parent / "prompts"

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

logger = get_logger(component="generator")


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
        seed_anchor_resolver: SeedAnchorSource | None = None,
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
        # docs/dev/21 第 3 节：种子锚点解析器。None = 进程级默认实例（种子库未同步时它直接返回
        # 空列表、不发任何 embedding 请求，所以单测/离线环境无需关心）。
        self._seed_resolver = seed_anchor_resolver

    async def generate(
        self, request: GenerationRequest, *, generator_run_id: str
    ) -> list[TestCase]:
        """按 request 中声明的每个 category 各调用一次 LLM，汇总为 TestCase 列表。

        任一批次失败即整体失败（抛 `GenerationError`），不返回半成品用例集——
        半成品会让下游误以为"这个 Skill 的正向用例天然就只有 3 条"。
        """
        request = await self._attach_seed_anchors(request)
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
        # 类别 -> 模板从注册表取（docs/dev/13 第 3.1 节对 docs/dev/06 的修订）：
        # 新增类别不再需要改本文件。未注册的类别在这里报错并点名该由哪份文档补齐。
        template = get_generation_template(category)

        prompt = self._env.get_template(template.prompt_path).render(
            skill=request.skill,
            count=count,
            focus=request.capability_focus,
            seed_texts=self._resolve_seed_texts(request),
            keywords=extract_keywords(request.skill),
            # 渐进式披露探查模板（docs/dev/13）需要逐条参考文件的加载条件原文；
            # 其余模板不渲染这个变量，多传无害（StrictUndefined 只在**用到**未定义
            # 变量时报错，多给几个不会）。统一传比在这里按类别分支更省心。
            reference_files=request.skill.reference_files,
            # 多技能复合用例模板（docs/dev/20）需要干扰包里各 Skill 的描述；理由同上，统一传。
            background_skills=request.background_skills,
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
                seed_anchor_id=self._resolve_seed_anchor_ref(generated, request),
                probe_target_reference=self._resolve_probe_target(
                    generated, template=template, skill=request.skill
                ),
                generator_run_id=generator_run_id,
                created_at=now,
            )
            for generated in batch.cases
        ]

    @staticmethod
    def _resolve_probe_target(
        generated: GeneratedCase, *, template: GenerationTemplate, skill: SkillDefinition
    ) -> str | None:
        """核对模型回填的"探查目标参考文件"，返回规范化后的路径（docs/dev/13）。

        只有声明了 `requires_probe_target` 的类别才有这个字段的语义；其余类别一律
        返回 None，免得某个模板里的自由发挥污染了别的类别的用例。

        核对分两级：先按原文精确匹配 `skill.reference_files` 的 path，再退一步按
        **文件名**匹配（模型很容易把 `references/errors.md` 写成 `errors.md`，或反
        过来带上 skill 根目录前缀）。两级都不中就返回 None 并告警——
        **不抛异常**：一条对不上号的探查用例只是这一条测不出东西，把整批用例连坐
        作废反而更糟。下游（`nodes/instruction_control/probe.py`）会把"触发探查用例
        没有探查目标"如实记成一条非严重发现，让人能看见这次少测了什么。
        """
        if not template.requires_probe_target:
            return None
        raw = (generated.probe_target_reference or "").strip()
        if not raw:
            logger.warning(
                "generator_probe_target_missing",
                skill_id=skill.skill_id,
                category=template.category.value,
                prompt=sanitize_for_log(generated.prompt),
            )
            return None

        known = {ref.path for ref in skill.reference_files}
        if raw in known:
            return raw
        by_basename = {ref.path.rsplit("/", 1)[-1]: ref.path for ref in skill.reference_files}
        matched = by_basename.get(raw.rsplit("/", 1)[-1])
        if matched is None:
            logger.warning(
                "generator_probe_target_unknown",
                skill_id=skill.skill_id,
                category=template.category.value,
                probe_target_reference=raw,
                known_reference_files=sorted(known),
            )
        return matched

    async def _attach_seed_anchors(self, request: GenerationRequest) -> GenerationRequest:
        """出题前一次性解析种子锚点（docs/dev/21 第 3 节），返回填好 `seed_anchors` 的请求副本。

        三种输入：调用方已传 `seed_anchors` → 原样使用；显式 `seed_anchor_ids` → 按 id 精确取；
        都没有 → 按 description 自动检索 `GeneratorTrustSettings.seed_anchor_count` 条。
        每个请求只解析一次而不是每个类别各检索一次：同一批题共用同一组锚点，溯源才一致。
        """
        if request.seed_anchors:
            return request
        resolver: SeedAnchorSource = self._seed_resolver or get_default_seed_anchor_resolver()
        if request.seed_anchor_ids is not None:
            anchors = resolver.resolve_ids(request.seed_anchor_ids)
        else:
            anchors = await resolver.resolve_for_skill(
                request.skill, get_settings().generator_trust.seed_anchor_count
            )
        if not anchors:
            return request
        return request.model_copy(update={"seed_anchors": anchors})

    @staticmethod
    def _resolve_seed_texts(request: GenerationRequest) -> list[str]:
        """把已解析的锚点渲染成 few-shot 文本：`[<anchor_id>] <prompt>`。

        方括号里的 id 是给模型回填 `seed_anchor_id` 用的（模板 `seed_block` 宏里有说明）。
        """
        return [f"[{anchor.anchor_id}] {anchor.prompt}" for anchor in request.seed_anchors]

    @staticmethod
    def _resolve_seed_anchor_ref(
        generated: GeneratedCase, request: GenerationRequest
    ) -> str | None:
        """核对模型回填的锚点 id，返回带 commit 的完整引用写进 `TestCase.seed_anchor_id`。

        只认本次**真的注入过**的锚点：模型编造一个看起来合理的 id，写进库就成了一条伪造的
        溯源记录。对不上时返回 None 并告警，不抛异常（一条题溯源缺失不值得整批作废）。
        容忍模型把方括号一起抄回来。
        """
        raw = (generated.seed_anchor_id or "").strip().strip("[]").strip()
        if not raw or not request.seed_anchors:
            return None
        by_id = {anchor.anchor_id: anchor for anchor in request.seed_anchors}
        anchor = by_id.get(raw)
        if anchor is None:
            logger.warning(
                "generator_seed_anchor_unknown",
                skill_id=request.skill.skill_id,
                seed_anchor_id=raw,
                injected=sorted(by_id),
            )
            return None
        return anchor.ref


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
