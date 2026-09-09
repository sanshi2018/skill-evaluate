"""Analyzer Agent 本体（docs/dev/16 第 2 节）。

**职责边界**：只做两件结构化抽取——把 SKILL.md 拆成 `CapabilityTree`、把一条
用例映射到若干 `capability_id`。它不落库、不计算覆盖率、不决定要不要补题，那些
是 `nodes/coverage/` 的事。这个边界让文档 17（冗余折叠/组合矩阵）与文档 18
（权重分级/反事实约束）可以各自复用同一个 Agent，而不需要各自造一个分析器。

## 为什么映射不走 Judge Agent

`docs/dev/interfaces/08` 第 0 节的铁律是"凡是**通过/失败**的结论一律经过
JudgeAgent"。用例-能力映射不是通过/失败的结论——它是一次结构化抽取，产出的是
一组 id，既没有 PASS/FAIL 语义，也不适用黄金基准盲测（黄金用例的标定值是
`human_labeled_status`，一个判决状态，没法拿来标定"该映射到哪几项能力"）。

真正的判定发生在下游：覆盖率算出来之后，由 `capability_coverage_threshold` 这条
量化规则经 `JudgeAgent.quantitative_verdict()` 给出 PASS/FAIL。铁律在那一步兑现。

## 为什么两个任务共用一个模型与温度

温度取 0.0：两件事都是抽取而不是创作，同一份输入每次给出同一份结果，是覆盖率
这个数字能被人信任的前提（出题才需要发散，见 `GeneratorAgent` 的 1.0）。
注意新一代 Claude 模型已移除采样参数，此时该值不会被发送（`agents/llm.py` 的
能力门禁），确定性由"抽取任务本身没有发挥空间"和 Prompt 的硬约束保证。
"""

from __future__ import annotations

from pathlib import Path

from skill_evaluate.agents.analyzer.identity import build_capability_id
from skill_evaluate.agents.analyzer.schema import (
    CapabilityExtraction,
    CaseCapabilityMapping,
    ExtractedCapability,
)
from skill_evaluate.agents.base import BaseLLMAgent
from skill_evaluate.agents.llm import AgentLLMClient
from skill_evaluate.agents.templating import build_prompt_env
from skill_evaluate.config import get_settings
from skill_evaluate.logging import get_logger
from skill_evaluate.observability.langfuse_adapter import LangfuseAdapter, LangfuseTraceHandle
from skill_evaluate.state.capability import CapabilityNode, CapabilityTree
from skill_evaluate.state.enums import CapabilityTier
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase

_PROMPT_DIR = Path(__file__).parent / "prompts"

_SYSTEM_PROMPT = (
    "你是一位资深的 Agent Skill 分析者。你的产出是覆盖率评测的分析基座，会被"
    "多个下游模块长期引用，因此质量标准是：宁可漏掉一项拿不准的，也不写一项"
    "在原文里找不到出处的。你只输出 JSON，不输出任何解释性文字。"
)

# 本文档阶段 `tier` 的统一占位值（docs/dev/16 第 2 节 / 第 9 节）。
#
# 它**不代表**这些能力真的都是"条件性能力"——只是为了让 `CapabilityTree` 在结构
# 上合法可用（`CapabilityNode.tier` 是必填字段，`TIER_WEIGHTS` 要求它是三档之一）。
# 真实分级是文档 18 的职责：它会重跑一次分级子任务原地更新本字段，`capability_id`
# 不变，因此历史绑定不受影响。
#
# 为什么选 P1 而不是 P0：占位值会被 `weighted_coverage()` 当真。全填 P0 会让
# 文档 18 接入前的任何一次加权计算都得出"全部是核心能力"这一最激进的口径；
# 全填 P2 则相反。取中间档，错得最不离谱。
PLACEHOLDER_TIER = CapabilityTier.P1_CONDITIONAL

logger = get_logger(component="analyzer")


class AnalyzerAgent(BaseLLMAgent):
    """SKILL.md 声明能力拆解 + 用例-能力双向追溯映射。"""

    name = "analyzer_agent"

    def __init__(
        self,
        *,
        model: str | None = None,
        temperature: float = 0.0,
        llm_client: AgentLLMClient | None = None,
        langfuse_adapter: LangfuseAdapter | None = None,
        trace_handle: LangfuseTraceHandle | None = None,
    ) -> None:
        super().__init__(
            model=model or get_settings().llm.analyzer_model,
            temperature=temperature,
            llm_client=llm_client,
            langfuse_adapter=langfuse_adapter,
            trace_handle=trace_handle,
        )
        self._env = build_prompt_env(_PROMPT_DIR)

    # ------------------------------------------------------------------ #
    # 1. 能力树抽取
    # ------------------------------------------------------------------ #

    async def extract_capability_tree(self, skill: SkillDefinition) -> CapabilityTree:
        """通读 SKILL.md 的 description 与正文，拆解为原子能力清单。

        本文档阶段的两个占位（docs/dev/16 第 9 节的接口清单）：

        - `tier` 统一填 `PLACEHOLDER_TIER`，**不做真实分级**——分级是文档 18 的
          职责，此处只保证 `CapabilityTree` 结构上合法可用；
        - `negative_constraints` 留空列表——反事实约束抽取同样属于文档 18。

        `covered` / `covering_case_ids` 一律是初值：覆盖情况由
        `nodes/coverage/nodes.py::map_case_coverage` 在映射阶段填，抽取阶段
        对"有没有用例测过它"一无所知。
        """
        prompt = self._env.get_template("extract_capabilities.jinja").render(
            skill=skill,
            review_threshold=get_settings().coverage.capability_count_review_threshold,
        )
        extraction = await self._call_llm(prompt, CapabilityExtraction, system=_SYSTEM_PROMPT)
        nodes = self._build_nodes(skill.skill_id, extraction.capabilities)

        logger.info(
            "analyzer_capability_tree_extracted",
            skill_id=skill.skill_id,
            skill_version_ref=skill.version_ref,
            capability_count=len(nodes),
            raw_count=len(extraction.capabilities),
        )
        return CapabilityTree(
            skill_id=skill.skill_id,
            skill_version_ref=skill.version_ref,
            nodes=nodes,
        )

    @staticmethod
    def _build_nodes(
        skill_id: str, capabilities: list[ExtractedCapability]
    ) -> list[CapabilityNode]:
        """把模型抽出的条目转成 `CapabilityNode`，并按 `capability_id` 去重。

        去重是必要的而不是防御性冗余：Prompt 明确要求"不要把同一件事换个说法写
        两遍"，但书写差异（标点、全角半角、大小写）本来就会被
        `build_capability_id()` 归一掉，因此"两条描述只差一个逗号"必然撞同一个
        id。若不去重，`CapabilityTree.nodes` 里会出现两个 id 相同的节点：覆盖率
        分母被虚增，而映射阶段按 id 建索引时其中一个永远标不上 covered——表现为
        一项永远补不满的盲区。

        保留先出现的那一条（模型通常把更主要的能力写在前面），并留下日志。
        """
        nodes: list[CapabilityNode] = []
        seen: set[str] = set()
        for item in capabilities:
            description = item.description.strip()
            if not description:
                # 空描述无法生成有意义的 id，也无法作为补盲指令喂给 Generator。
                logger.warning("analyzer_capability_skipped_empty", skill_id=skill_id)
                continue
            capability_id = build_capability_id(skill_id, description)
            if capability_id in seen:
                logger.info(
                    "analyzer_capability_deduplicated",
                    skill_id=skill_id,
                    capability_id=capability_id,
                    dropped_description=description,
                )
                continue
            seen.add(capability_id)
            # evidence_quote 不进 `CapabilityNode`（字段表由 docs/dev/02 定，本
            # 文档不擅自扩展），但它是人工审核卡片与事后回查的主要线索，因此
            # 逐条落结构化日志。
            logger.info(
                "analyzer_capability_extracted",
                skill_id=skill_id,
                capability_id=capability_id,
                description=description,
                evidence_quote=item.evidence_quote.strip()[:500],
            )
            nodes.append(
                CapabilityNode(
                    capability_id=capability_id,
                    skill_id=skill_id,
                    description=description,
                    tier=PLACEHOLDER_TIER,
                )
            )
        return nodes

    # ------------------------------------------------------------------ #
    # 2. 用例-能力映射
    # ------------------------------------------------------------------ #

    async def map_case_to_capabilities(self, case: TestCase, tree: CapabilityTree) -> list[str]:
        """判断一条用例实际激活了树中哪些 `capability_id`。

        返回值**已按能力树过滤**：模型编造的、或引用了上一版能力树的 id 会被丢弃
        并记日志。不过滤的后果是覆盖率被一个根本不存在的节点抬高——而覆盖率是本
        维度唯一的分数，抬高它等于让整个维度失效。

        空能力树直接返回空列表，不发请求：没有候选 id 时这次调用不可能产出任何
        有用信息，只会烧一次 token。
        """
        if not tree.nodes:
            return []

        prompt = self._env.get_template("map_case.jinja").render(
            skill_id=tree.skill_id,
            nodes=tree.nodes,
            case=case,
            # 输出示例里用真实存在的 id，比写 "cap-1" 更能压住"自己编一个 id"的倾向。
            example_capability_id=tree.nodes[0].capability_id,
        )
        mapping = await self._call_llm(prompt, CaseCapabilityMapping, system=_SYSTEM_PROMPT)

        known = {node.capability_id for node in tree.nodes}
        accepted = [cid for cid in dict.fromkeys(mapping.capability_ids) if cid in known]
        rejected = [cid for cid in mapping.capability_ids if cid not in known]
        if rejected:
            logger.warning(
                "analyzer_case_mapping_unknown_ids",
                case_id=case.case_id,
                skill_id=tree.skill_id,
                unknown_capability_ids=rejected,
            )
        logger.info(
            "analyzer_case_mapped",
            case_id=case.case_id,
            skill_id=tree.skill_id,
            capability_ids=accepted,
            reasoning=mapping.reasoning[:500],
        )
        return accepted


__all__ = ["PLACEHOLDER_TIER", "AnalyzerAgent"]
