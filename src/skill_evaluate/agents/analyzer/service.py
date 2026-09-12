"""Analyzer Agent 本体（docs/dev/16 第 2 节）。

**职责边界**：只做结构化抽取——把 SKILL.md 拆成 `CapabilityTree`、给能力定权重
档位、抽出负向约束、把一条用例映射到若干 `capability_id`。它不落库、不计算覆盖
率、不决定要不要补题，那些是 `nodes/coverage/`（模块六）与
`nodes/weighted_coverage/`（模块八）的事。这个边界让三份覆盖率文档复用同一个
Agent，而不需要各自造一个分析器。

四个方法分属两份文档：`extract_capability_tree()` / `map_case_to_capabilities()`
由 docs/dev/16 引入，`classify_tiers()` / `extract_negative_constraints()` 由
docs/dev/18 补齐——后两者正是前者留下的两处占位（见 `PLACEHOLDER_TIER`）。

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

from skill_evaluate.agents.analyzer.identity import build_capability_id, build_constraint_id
from skill_evaluate.agents.analyzer.schema import (
    CapabilityExtraction,
    CaseCapabilityMapping,
    ExtractedCapability,
    ExtractedNegativeConstraint,
    NegativeConstraintExtraction,
    TierClassification,
)
from skill_evaluate.agents.base import BaseLLMAgent
from skill_evaluate.agents.llm import AgentLLMClient
from skill_evaluate.agents.templating import build_prompt_env
from skill_evaluate.config import get_settings
from skill_evaluate.logging import get_logger
from skill_evaluate.observability.langfuse_adapter import LangfuseAdapter, LangfuseTraceHandle
from skill_evaluate.state.capability import CapabilityNode, CapabilityTree, NegativeConstraint
from skill_evaluate.state.enums import CapabilityTier
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase

_PROMPT_DIR = Path(__file__).parent / "prompts"

_SYSTEM_PROMPT = (
    "你是一位资深的 Agent Skill 分析者。你的产出是覆盖率评测的分析基座，会被"
    "多个下游模块长期引用，因此质量标准是：宁可漏掉一项拿不准的，也不写一项"
    "在原文里找不到出处的。你只输出 JSON，不输出任何解释性文字。"
)

# 能力抽取阶段 `tier` 的统一占位值（docs/dev/16 第 2 节 / 第 9 节）。
#
# 它**不代表**这些能力真的都是"条件性能力"——只是为了让 `CapabilityTree` 在结构
# 上合法可用（`CapabilityNode.tier` 是必填字段，`TIER_WEIGHTS` 要求它是三档之一）。
# 真实分级由本文件的 `classify_tiers()`（docs/dev/18 第 3 节）原地更新本字段，
# `capability_id` 不变，因此历史绑定不受影响。它跑在模块八的子图里，也就是模块
# 六/七全部跑完之后——在那之前读到的 tier 都还是这个占位值。
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

        本方法**只负责抽取**，两项模块八的内容不在这里产出（它们各有独立的调用，
        跑在模块八的子图里，见 `classify_tiers()` / `extract_negative_constraints()`）：

        - `tier` 统一填 `PLACEHOLDER_TIER`，此处只保证 `CapabilityTree` 结构上
          合法可用；
        - `negative_constraints` 留空列表。

        ⚠️ 这也意味着**每一次重新抽取都会把真实分级重置回占位值**（落库是按
        `(skill_id, skill_version_ref)` 的整树 upsert）。这不是 bug：分级依据的是
        SKILL.md 正文，正文重新抽过一遍，分级就该跟着重来一遍。模块八的子图排在
        模块六之后，每次运行都会重新分级，因此稳态下不会出现"树上永远是占位值"。

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
    # 2. 权重分级（docs/dev/18 第 3 节）
    # ------------------------------------------------------------------ #

    async def classify_tiers(self, tree: CapabilityTree, skill: SkillDefinition) -> CapabilityTree:
        """给树上每项能力定 P0/P1/P2 档位，**原地更新** `tier` 后返回同一棵树。

        三条落地口径：

        1. **`capability_id` 不变、节点不增不减**。分级是"给已有节点打标签"，不是
           重新抽一棵树。模型回填了清单外的 id（编的，或引用了上一版能力树）一律
           丢弃并记日志——接受它等于让一次分级调用悄悄改写能力树的结构，而
           `TestCase.target_capability_ids` 里的历史绑定会因此集体失效
           （见 `identity.py` 模块头）。
        2. **模型没给出档位的节点保留原值**（通常是 `PLACEHOLDER_TIER`）。不默认
           填 P0：漏判一项的代价应当是"这项的权重仍是中间档"，而不是"这项被当成
           了最核心的能力"，后者会让加权覆盖率朝着最激进的口径偏。
        3. **空树直接返回，不发请求**：没有候选 id 时这次调用产不出任何信息。

        Prompt 里用 few-shot 判定范本固化标准（docs/dev/08 第 4 节权衡分析建议的
        "标准能力与权重拆解范本"），而不是只给三句定义——分级是本项目里少数几个
        "同一份输入、不同措辞会给出不同答案"的任务，范本是压住这种漂移最有效的
        手段。
        """
        if not tree.nodes:
            return tree

        prompt = self._env.get_template("classify_tiers.jinja").render(
            skill=skill,
            nodes=tree.nodes,
            # 与 `map_case.jinja` 同一条经验：输出示例里用真实存在的 id，比写
            # "cap-1" 更能压住"自己编一个 id"的倾向。
            example_capability_id=tree.nodes[0].capability_id,
        )
        classification = await self._call_llm(prompt, TierClassification, system=_SYSTEM_PROMPT)

        by_id = {node.capability_id: node for node in tree.nodes}
        assigned: set[str] = set()
        for item in classification.assignments:
            node = by_id.get(item.capability_id)
            if node is None:
                logger.warning(
                    "analyzer_tier_unknown_capability_id",
                    skill_id=tree.skill_id,
                    capability_id=item.capability_id,
                    tier=item.tier,
                )
                continue
            if item.capability_id in assigned:
                # 同一项被打了两次标签。保留先出现的那一次并记日志——静默按后者
                # 覆盖，会让"这项到底几档"取决于模型的输出顺序。
                logger.warning(
                    "analyzer_tier_duplicate_assignment",
                    skill_id=tree.skill_id,
                    capability_id=item.capability_id,
                    kept_tier=node.tier.value,
                    dropped_tier=item.tier,
                )
                continue
            assigned.add(item.capability_id)
            node.tier = CapabilityTier(item.tier)
            logger.info(
                "analyzer_capability_tier_assigned",
                skill_id=tree.skill_id,
                capability_id=item.capability_id,
                tier=item.tier,
                reason=item.reason[:300],
            )

        missing = [cid for cid in by_id if cid not in assigned]
        if missing:
            logger.warning(
                "analyzer_tier_missing_assignments",
                skill_id=tree.skill_id,
                capability_ids=missing,
                fallback_tier=PLACEHOLDER_TIER.value,
            )
        logger.info(
            "analyzer_tiers_classified",
            skill_id=tree.skill_id,
            skill_version_ref=tree.skill_version_ref,
            capability_count=len(tree.nodes),
            assigned_count=len(assigned),
            tier_distribution={
                tier.value: sum(1 for n in tree.nodes if n.tier is tier) for tier in CapabilityTier
            },
        )
        return tree

    # ------------------------------------------------------------------ #
    # 3. 负向约束抽取（docs/dev/18 第 3 节）
    # ------------------------------------------------------------------ #

    async def extract_negative_constraints(
        self, skill: SkillDefinition
    ) -> list[NegativeConstraint]:
        """从 SKILL.md 里抽出"必须避免/禁止"型规则，作为反事实追踪对象。

        ## 与常识剥离度审计（docs/dev/07 模板 5.1 `omission_audit`）的关系

        两者的判定逻辑有相似之处但**目标相反**，因此刻意不合并成一个模板：
        `omission_audit` 找的是"该删的常识"（产出是删减建议），这里找的是"该保留、
        且必须被测试覆盖的禁止性规则"（产出是覆盖率追踪对象）。合并之后，一条被
        判为"常识、建议删除"的句子会同时成为一条"必须被用例覆盖"的约束，两个结论
        自相矛盾。

        ## 为什么定位段落靠模型而不是靠标题关键词

        "Gotchas"/"注意"/"避免"/"Common Mistakes" 这类标题只是**辅助线索**写进了
        Prompt，代码侧不做任何标题匹配。禁止性规则经常散落在操作步骤中间的一句
        "注意不要……"里，按标题切段会把它们整批漏掉；而漏掉的表现是"这份 Skill
        没有负向约束"——一个看起来非常健康的结论。

        返回值按 `constraint_id` 去重（理由同 `_build_nodes()`：书写差异会被归一
        掉，两条只差一个逗号的描述必然撞同一个 id，不去重会让约束覆盖率的分母
        被虚增，而其中一条永远标不上 covered）。
        """
        prompt = self._env.get_template("extract_negative_constraints.jinja").render(skill=skill)
        extraction = await self._call_llm(
            prompt, NegativeConstraintExtraction, system=_SYSTEM_PROMPT
        )
        constraints = self._build_constraints(skill.skill_id, extraction.constraints)

        logger.info(
            "analyzer_negative_constraints_extracted",
            skill_id=skill.skill_id,
            skill_version_ref=skill.version_ref,
            constraint_count=len(constraints),
            raw_count=len(extraction.constraints),
        )
        return constraints

    @staticmethod
    def _build_constraints(
        skill_id: str, extracted: list[ExtractedNegativeConstraint]
    ) -> list[NegativeConstraint]:
        """把模型抽出的条目转成 `NegativeConstraint`，并按 `constraint_id` 去重。

        `covered` / `covering_case_ids` 一律是初值：抽取阶段对"有没有用例诱导过这
        个坑"一无所知，那是 `nodes/weighted_coverage` 映射节点的结论。
        """
        constraints: list[NegativeConstraint] = []
        seen: set[str] = set()
        for item in extracted:
            description = item.description.strip()
            if not description:
                # 空描述既生不成有意义的 id，也没法作为补题指令喂给 Generator。
                logger.warning("analyzer_constraint_skipped_empty", skill_id=skill_id)
                continue
            constraint_id = build_constraint_id(skill_id, description)
            if constraint_id in seen:
                logger.info(
                    "analyzer_constraint_deduplicated",
                    skill_id=skill_id,
                    constraint_id=constraint_id,
                    dropped_description=description,
                )
                continue
            seen.add(constraint_id)
            # evidence_quote 同样不进 `NegativeConstraint`（字段表由 docs/dev/02
            # 定），但它是人工复核"这条规则是不是模型脑补的"的主要线索。
            logger.info(
                "analyzer_negative_constraint_extracted",
                skill_id=skill_id,
                constraint_id=constraint_id,
                description=description,
                evidence_quote=item.evidence_quote.strip()[:500],
            )
            constraints.append(
                NegativeConstraint(constraint_id=constraint_id, description=description)
            )
        return constraints

    # ------------------------------------------------------------------ #
    # 4. 用例-能力映射
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
