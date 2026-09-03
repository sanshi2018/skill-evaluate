"""模块二：上下文利用率与范围界定静态评测的节点实现（docs/dev/12）。

```
static_metrics_scan               纯代码：行数 / Token 硬性卡线
        ↓
progressive_disclosure_static_scan 纯代码：目录结构与触发条件正则初筛
        ↓
mini_agent_peer_review             并发跑 3 个 MiniReviewAgent 模板（经 Judge）
        ↓
finalize_dimension_report          聚合 + 落库
```

这是全项目第一个**全程走 `MINI` 后端、不依赖 Hermes 沙箱**的维度：不出题、不执行、
不重试，只评审 `SKILL.md` 本身的静态文本质量。因此它比模块一简单得多——没有条件
路由，四个节点一条直线。

## 两条贯穿本文件的关键决策

1. **硬性数字阻断，主观判断不阻断**（docs/dev/12 第 6 节）。架构文档原文对硬性
   指标说"抛出 Warning 或 Error"，本维度明确收窄为"超标即 Error"：500 行 /
   5,000 Token 能精确算出来、不存在歧义，没有必要给确定性指标设宽容度。而 Mini
   Agent 的主观审查（常识剥离、范围连贯性）本质上是**建议**，让它直接阻断合并
   的风险是误报过多、开发者从此不信任这条流水线，所以只记录不阻断。
   落到代码上就是 `blocking=hard_fail`，而不是维度级的常量。
2. **所有通过/失败结论都经 `JudgeAgent`**。本文件没有一处自己写的
   `if verdict == ...` 式判定：三项同行评审全部走 `judgmental_verdict()`
   （docs/dev/interfaces/08 第 0 节铁律），硬性卡线则是纯算术、不产生"判定"，
   由本维度直接聚合成维度级状态。

## 节点签名与返回值

与模块一同样的两条坑（详见 `state.py` 与
`docs/dev/interfaces/12_context_scoping_static_pipeline.md`）：签名必须写
`ContextScopingState`（否则私有键被 LangGraph 静默裁掉），返回值只带增量
（`judge_verdict_ids` 的 reducer 是 `operator.add`，回抛整个旧状态会让 id 翻倍）。
"""

from __future__ import annotations

import asyncio
from typing import cast

from pydantic import BaseModel

from skill_evaluate.agents.judge.golden_injector import is_golden_subject
from skill_evaluate.errors import PersistenceError, PipelineSuspended
from skill_evaluate.logging import get_logger
from skill_evaluate.nodes.context_scoping.deps import (
    PEER_REVIEW_CRITICALITY,
    ContextScopingDeps,
)
from skill_evaluate.nodes.context_scoping.state import (
    DIMENSION,
    KEY_DISCLOSURE_SCAN,
    KEY_PEER_REVIEW_OUTCOMES,
    KEY_STATIC_METRICS,
    ContextScopingState,
)
from skill_evaluate.nodes.context_scoping.static_scan import (
    ProgressiveDisclosureScan,
    StaticMetricsResult,
    format_reference_files_for_review,
    scan_progressive_disclosure,
    scan_static_metrics,
)
from skill_evaluate.state.enums import JudgeVerdictStatus
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.skill import SkillDefinition

logger = get_logger(component=DIMENSION)

# 节点名。docs/dev/03 的 `NODE_BACKEND_ROUTING`（键 `context_scoping`）、
# docs/dev/24 的主图装配都引用这一份，避免各处各写各的字符串。
NODE_NAMES = {
    "static_metrics_scan": f"{DIMENSION}.static_metrics_scan",
    "progressive_disclosure_static_scan": f"{DIMENSION}.progressive_disclosure_static_scan",
    "mini_agent_peer_review": f"{DIMENSION}.mini_agent_peer_review",
    "finalize_dimension_report": f"{DIMENSION}.finalize_dimension_report",
}

ENTRY_NODE = NODE_NAMES["static_metrics_scan"]
TERMINAL_NODE = NODE_NAMES["finalize_dimension_report"]

# 报告里每条同行评审结论摘录的长度。截断而不是全文入库：`JudgeVerdict` 已经带着
# 完整 reasoning 落了库（`judge_verdict_ids` 可回查），`findings` 是给人扫一眼用的
# 摘要列表，塞进整段推理只会让报告没法读。
REASONING_EXCERPT_CHARS = 200


class PeerReviewOutcome(BaseModel):
    """一次同行评审的结论摘要（进图状态用）。

    存摘要而不是整个 `JudgeVerdict`：verdict 本体已由 Judge 侧落库，状态里再放一份
    只会让每个 Checkpoint 白背几十 KB（`state.py` 的说明）。

    `skipped_reason` 不为空表示这一项**没有产生针对本 Skill 的结论**——目前唯一的
    成因是被黄金基准盲测占用（见 `mini_agent_peer_review`）。它不计入通过/失败，
    但必须出现在报告里：少做了一项审查，读报告的人有权知道。
    """

    template_key: str
    verdict_id: str | None = None
    status: JudgeVerdictStatus | None = None
    reasoning_excerpt: str = ""
    skipped_reason: str | None = None


class ContextScopingPipeline:
    """模块二的四个节点。做成类是为了让依赖注入只发生一次（构造时），
    而不是每个节点函数各自去拿一遍单例。

    用法（docs/dev/24 装配主图时）见 `graph.py::add_context_scoping_nodes()`。
    """

    def __init__(self, deps: ContextScopingDeps | None = None) -> None:
        self.deps = deps or ContextScopingDeps()
        # 装配期就核对路由表：本维度无法兑现 PLUGGABLE 声明，与其运行到一半才发现
        # 不一致，不如在建图时报错（`deps.py::assert_backend_routing` 的说明）。
        ContextScopingDeps.assert_backend_routing()

    # ------------------------------------------------------------------ #
    # 1. static_metrics_scan
    # ------------------------------------------------------------------ #

    async def static_metrics_scan(self, state: ContextScopingState) -> dict[str, object]:
        """行数 / Token 数硬性卡线（docs/dev/12 第 3 节）。

        本节点**不下判定**、只产出数字：是否阻断由 `finalize_dimension_report`
        统一决定，这样"阈值判定的口径"只存在于一个地方。
        """
        settings = self.deps.settings()
        skill = await self._load_skill(state)
        metrics = scan_static_metrics(
            skill,
            line_limit=settings.line_limit,
            token_limit=settings.token_limit,
            token_counter=self.deps.token_counter,
            estimate_uncertainty_ratio=settings.estimate_uncertainty_ratio,
        )
        logger.info(
            "context_scoping_static_metrics",
            run_id=str(state["run_id"]),
            node_name=NODE_NAMES["static_metrics_scan"],
            skill_id=skill.skill_id,
            line_count=metrics.line_count,
            token_count=metrics.token_count,
            token_count_method=metrics.token_count_method,
            line_limit_exceeded=metrics.line_limit_exceeded,
            token_limit_exceeded=metrics.token_limit_exceeded,
            needs_human_confirmation=metrics.needs_human_confirmation,
        )
        return {KEY_STATIC_METRICS: metrics.model_dump()}

    # ------------------------------------------------------------------ #
    # 2. progressive_disclosure_static_scan
    # ------------------------------------------------------------------ #

    async def progressive_disclosure_static_scan(
        self, state: ContextScopingState
    ) -> dict[str, object]:
        """目录结构审查 + 触发条件正则初筛（docs/dev/12 第 4 节）。

        产出的候选清单会在下一个节点作为 `reference_files` 变量的一部分交给
        Mini Agent 复核——正则允许误报，由 LLM 兜底分辨真问题与误报。
        """
        settings = self.deps.settings()
        skill = await self._load_skill(state)
        metrics = self._static_metrics(state)
        scan = scan_progressive_disclosure(
            skill,
            line_limit=settings.line_limit,
            token_limit=settings.token_limit,
            bulk_inline_ratio=settings.bulk_inline_ratio,
            # 用上一个节点刚算出来的 Token 数，而不是库里可能过时的字段。
            token_count=metrics.token_count if metrics else None,
        )
        logger.info(
            "context_scoping_disclosure_scanned",
            run_id=str(state["run_id"]),
            node_name=NODE_NAMES["progressive_disclosure_static_scan"],
            skill_id=skill.skill_id,
            reference_files=scan.reference_file_count,
            candidates=len(scan.candidates),
            bulk_inline_without_references=scan.bulk_inline_without_references,
        )
        return {KEY_DISCLOSURE_SCAN: scan.model_dump()}

    # ------------------------------------------------------------------ #
    # 3. mini_agent_peer_review
    # ------------------------------------------------------------------ #

    async def mini_agent_peer_review(self, state: ContextScopingState) -> dict[str, object]:
        """三项同行评审并发执行（docs/dev/12 第 5 节）。

        三个模板互不依赖，`asyncio.gather` 一次打完；本维度不设并发上限——三次
        Mini 档请求不构成任何需要节流的规模（对比模块一的"用例数 × 3"个沙箱）。

        `Criticality.ROUTINE`：见 `deps.py::PEER_REVIEW_CRITICALITY` 的说明。

        **不吞异常**：`JudgeFrozenError`（裁判因黄金基准失误率被冻结）必须逐层抛
        上去让流水线挂起——一个已被证明会误判的裁判给出的任何结论都不该进报告
        （docs/dev/interfaces/08 第 4 节）。
        """
        run_id = str(state["run_id"])
        skill = await self._load_skill(state)
        scan = self._disclosure_scan(state)
        judge = self.deps.judge()

        # 各模板需要的 content 变量见 docs/dev/interfaces/07 第 1 节的表格；缺变量
        # 会在**发请求之前**抛 ReviewTemplateError（模板环境用 StrictUndefined）。
        requests = {
            "omission_audit": {"skill_md": skill.body_markdown},
            "scoping_check": {"skill_md": skill.body_markdown},
            "progressive_disclosure_static": {
                "skill_md": skill.body_markdown,
                # 正则初筛的结论随文件清单一起给模型：docs/dev/12 第 4 节要的正是
                # "让 LLM 复核这份初筛结果里哪些是真问题、哪些是正则误报"。
                "reference_files": format_reference_files_for_review(
                    skill, scan or ProgressiveDisclosureScan(reference_file_count=0)
                ),
            },
        }

        results = await asyncio.gather(
            *(
                judge.judgmental_verdict(
                    subject_id=skill.skill_id,
                    template_key=template_key,
                    content=content,
                    criticality=PEER_REVIEW_CRITICALITY,
                )
                for template_key, content in requests.items()
            )
        )

        outcomes = [
            self._to_outcome(template_key, result)
            for template_key, result in zip(requests, results, strict=True)
        ]
        logger.info(
            "context_scoping_peer_reviewed",
            run_id=run_id,
            node_name=NODE_NAMES["mini_agent_peer_review"],
            skill_id=skill.skill_id,
            failed=[o.template_key for o in outcomes if o.status is JudgeVerdictStatus.FAIL],
            skipped=[o.template_key for o in outcomes if o.skipped_reason],
        )
        return {
            # 只回增量：`judge_verdict_ids` 的 reducer 是 `operator.add`。被黄金盲测
            # 占用的那一项没有属于本 Skill 的 verdict_id，不进这个列表。
            "judge_verdict_ids": [o.verdict_id for o in outcomes if o.verdict_id],
            KEY_PEER_REVIEW_OUTCOMES: [o.model_dump() for o in outcomes],
        }

    def _to_outcome(
        self, template_key: str, result: JudgeVerdict | ConsensusResult
    ) -> PeerReviewOutcome:
        """把 Judge 的返回值收敛成状态里存的摘要，并在此处理两种特殊返回。

        1. **黄金基准盲测**：`judgmental_verdict()` 有 2% 概率把请求整个换成一条人类
           标定过的黄金用例来考核裁判自己。这类结果的 `subject_id` 带
           `__golden__:` 前缀，**必须跳过**——把它当成本 Skill 的审查结论写进报告，
           等于用另一份文本的判决给这份 Skill 定性（docs/dev/interfaces/08 第 3 节）。
        2. **共识未达成**：本维度声明的是 ROUTINE，正常不会拿到 `ConsensusResult`。
           但 docs/dev/12 第 7 节把"是否升级为阻断项"列为可运维调优的开关，一旦有人
           把 criticality 调成 CRITICAL，这条路径就会活过来。此时
           `NEEDS_HUMAN_REVIEW` **不允许被降级**为 PASS/FAIL（docs/dev/08 的明令
           禁止项），正确处理是挂起等人工仲裁。
        """
        if isinstance(result, ConsensusResult) and not result.consensus_reached:
            raise PipelineSuspended(
                f"{NODE_NAMES['mini_agent_peer_review']}：模板 {template_key!r} 的三副本"
                f"复核未达成共识（subject_id={result.subject_id!r}），需人工仲裁。"
            )

        status = (
            result.final_status if isinstance(result, ConsensusResult) else result.status
        )
        verdict = (
            result.verdicts[0]
            if isinstance(result, ConsensusResult) and result.verdicts
            else result
        )

        if is_golden_subject(result.subject_id):
            logger.info(
                "context_scoping_review_consumed_by_golden_case",
                template_key=template_key,
                subject_id=result.subject_id,
            )
            return PeerReviewOutcome(
                template_key=template_key,
                skipped_reason="本次请求被黄金基准盲测占用，未产生针对本 Skill 的审查结论",
            )

        reasoning = getattr(verdict, "reasoning", "")
        return PeerReviewOutcome(
            template_key=template_key,
            verdict_id=getattr(verdict, "verdict_id", None),
            status=status,
            reasoning_excerpt=str(reasoning)[:REASONING_EXCERPT_CHARS],
        )

    # ------------------------------------------------------------------ #
    # 4. finalize_dimension_report
    # ------------------------------------------------------------------ #

    async def finalize_dimension_report(self, state: ContextScopingState) -> dict[str, object]:
        """聚合三路结果写进 `dimension_results`（docs/dev/12 第 6 节）。

        判定口径：

        | 情形 | status | blocking |
        |---|---|---|
        | 行数/Token 超标 | FAIL | **True**（确定性指标，阻断） |
        | Token 估算值落在限额不确定带 | NEEDS_HUMAN_REVIEW | False |
        | 仅 Mini Agent 审查有 FAIL | FAIL | False（主观建议，只告警） |
        | 三项审查全被跳过且指标正常 | NEEDS_HUMAN_REVIEW | False |
        | 其余 | PASS | False |

        `score=None`：本维度不产出连续分数，只有硬性指标 + 审查结论。硬凑一个
        "通过项数 / 总项数"的分数会让三个性质完全不同的检查被平均掉。
        """
        run_id = str(state["run_id"])
        metrics = self._static_metrics(state)
        scan = self._disclosure_scan(state)
        outcomes = self._peer_review_outcomes(state)

        findings: list[str] = []
        hard_fail = False
        needs_human = False

        if metrics is None:
            # 只可能出现在"主图状态 schema 漏了本维度私有键"或节点被跳过时。判 PASS
            # 等于把整个维度悄悄关掉，所以显式暴露给人。
            needs_human = True
            findings.append(
                "未取到静态指标扫描结果：请确认主图状态 schema 包含本维度私有键"
                f"（{KEY_STATIC_METRICS}），见 docs/dev/interfaces/12 第 2 节。"
            )
        else:
            hard_fail = metrics.hard_fail
            findings.extend(self._metrics_findings(metrics))
            needs_human = needs_human or metrics.needs_human_confirmation

        findings.extend(
            self._disclosure_findings(
                scan, tolerance=self.deps.settings().max_reference_files_without_trigger
            )
        )

        peer_failed = [o for o in outcomes if o.status is JudgeVerdictStatus.FAIL]
        findings.extend(
            f"[{o.template_key}] Mini Agent 审查未通过（非阻断，供人工参考）："
            f"{o.reasoning_excerpt}"
            for o in peer_failed
        )
        findings.extend(
            f"[{o.template_key}] 本次未产生审查结论：{o.skipped_reason}"
            for o in outcomes
            if o.skipped_reason
        )
        if outcomes and all(o.skipped_reason for o in outcomes):
            # 三项审查全被盲测占用（概率极低但不是零）：这一次评测实际上只做了硬性
            # 扫描。指标没超标时报 PASS 会掩盖"主观审查一项都没跑"这件事。
            needs_human = True

        status = self._resolve_status(
            hard_fail=hard_fail, peer_failed=bool(peer_failed), needs_human=needs_human
        )
        await self.deps.reporter().record_dimension_result(
            run_id=run_id,
            dimension=DIMENSION,
            status=status,
            score=None,
            findings=findings,
            # **关键策略**：只有确定性指标超标才阻断。Mini Agent 的主观判断即使
            # FAIL 也不阻断，理由见本文件头部第 1 条。
            blocking=hard_fail,
        )
        logger.info(
            "context_scoping_dimension_recorded",
            run_id=run_id,
            node_name=TERMINAL_NODE,
            status=status.value,
            blocking=hard_fail,
            peer_failed=len(peer_failed),
            findings=len(findings),
        )
        return {}

    @staticmethod
    def _resolve_status(
        *, hard_fail: bool, peer_failed: bool, needs_human: bool
    ) -> JudgeVerdictStatus:
        """维度级状态的优先级：FAIL > NEEDS_HUMAN_REVIEW > PASS。

        FAIL 优先于 NEEDS_HUMAN_REVIEW：已经有确凿问题时，不该因为"另有一项要人
        确认"而把结论弱化成"待定"。
        """
        if hard_fail or peer_failed:
            return JudgeVerdictStatus.FAIL
        if needs_human:
            return JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        return JudgeVerdictStatus.PASS

    @staticmethod
    def _metrics_findings(metrics: StaticMetricsResult) -> list[str]:
        """把硬性指标翻译成人能直接照着改的 findings。

        总是写一条"当前数值 + 限额 + 计数口径"，哪怕没超标：读报告的人需要知道
        这次是贴着线过的还是绰绰有余，也需要知道这个 Token 数是精算的还是估的。
        """
        findings = [
            f"静态指标：{metrics.line_count}/{metrics.line_limit} 行，"
            f"{metrics.token_count}/{metrics.token_limit} Token"
            f"（计数口径 {metrics.token_count_method}"
            f"{'' if metrics.token_count_exact else '，估算值'}）"
        ]
        if metrics.line_limit_exceeded:
            findings.append(
                f"行数超标：{metrics.line_count} > {metrics.line_limit}，"
                "请把长配置或参考资料移到 references/ 目录按需加载。"
            )
        if metrics.token_limit_exceeded and not metrics.needs_human_confirmation:
            findings.append(
                f"Token 数超标：{metrics.token_count} > {metrics.token_limit}，"
                "请精简正文或拆分到 references/。"
            )
        if metrics.needs_human_confirmation:
            findings.append(
                f"Token 数 {metrics.token_count} 超过限额 {metrics.token_limit}，但本次是"
                f"**估算值**（{metrics.token_count_method}），落在限额附近的不确定带内，"
                "因此不阻断、请人工确认。安装 tiktoken 可获得离线精确计数。"
            )
        return findings

    @staticmethod
    def _disclosure_findings(
        scan: ProgressiveDisclosureScan | None, *, tolerance: int = 0
    ) -> list[str]:
        """渐进式披露相关的 findings。

        注意这里报的是**正则初筛**的结果，措辞上必须说清它只是"疑似"——真正的
        定性由同行评审那一项（`progressive_disclosure_static`）给出，两者在报告里
        并列呈现，人才能看出"正则说 3 个、模型复核后认为其中 1 个是误报"。

        `tolerance`（`SKILLEVAL_CONTEXT_SCOPING_MAX_REFERENCE_FILES_WITHOUT_TRIGGER`）
        是**整体**容忍度，不是"逐条豁免前 N 个"：命中数不超过它就一条都不报。默认
        0 = 零容忍。做成整体阈值而不是逐条豁免，是因为"报三条里的后两条"对读报告
        的人毫无意义——他无从知道被吞掉的是哪一条。
        """
        if scan is None:
            return []
        findings: list[str] = []
        if scan.bulk_inline_without_references:
            findings.append(
                "正文体量已接近限额，但没有任何 references/ 参考文件："
                "疑似未做渐进式披露，建议把长配置/参考资料拆出去按需加载。"
            )
        if len(scan.candidates) <= tolerance:
            return findings
        for candidate in scan.candidates:
            reason = (
                "正文中完全没有提到该文件"
                if candidate.reason == "not_mentioned"
                else f"提到了但附近没有触发条件（相关行：{candidate.evidence}）"
            )
            findings.append(
                f"疑似缺少按需加载触发条件（正则初筛，以同行评审结论为准）："
                f"{candidate.path}——{reason}"
            )
        return findings

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    async def _load_skill(self, state: ContextScopingState) -> SkillDefinition:
        """取被测 Skill。

        本维度**不**支持"用 Optimizer 的工作副本"那套（模块一的 `_working_skill`）：
        它评审的就是仓库里那份 SKILL.md 的原貌，拿一份内存里改过的版本来审，报告
        就与人能看到的文件对不上了。
        """
        skill = await self.deps.skill_repository.get(
            str(state["skill_id"]), str(state["skill_version_ref"])
        )
        if skill is None:
            raise PersistenceError(
                f"未找到被测 Skill：skill_id={state['skill_id']!r} "
                f"version_ref={state['skill_version_ref']!r}。"
                "请先经 `ingestion.load_skill()` + `SkillRepository.save()` 入库。"
            )
        return skill

    @staticmethod
    def _static_metrics(state: ContextScopingState) -> StaticMetricsResult | None:
        return _model_from_state(state, KEY_STATIC_METRICS, StaticMetricsResult)

    @staticmethod
    def _disclosure_scan(state: ContextScopingState) -> ProgressiveDisclosureScan | None:
        return _model_from_state(state, KEY_DISCLOSURE_SCAN, ProgressiveDisclosureScan)

    @staticmethod
    def _peer_review_outcomes(state: ContextScopingState) -> list[PeerReviewOutcome]:
        raw = cast("list[object] | None", state.get(KEY_PEER_REVIEW_OUTCOMES))
        return [PeerReviewOutcome.model_validate(item) for item in (raw or [])]


def _model_from_state[T: BaseModel](
    state: ContextScopingState, key: str, model: type[T]
) -> T | None:
    """从图状态里取一个 Pydantic 模型，缺键时返回 None。

    统一走 `model_validate()` 而不是假设拿回来的还是模型实例：Checkpoint 反序列化
    后可能是 dict（取决于 serde 实现），两种形态 `model_validate()` 都吃。

    `state.get(key)` 用**变量**作键时，TypedDict 的静态类型会退化成 `object`
    （私有键本来也不在 `PipelineState` 的字段表里），所以在这里集中收窄一次，
    好过每个调用点各写一行 cast。
    """
    value = state.get(key)
    return None if value is None else model.model_validate(value)


__all__ = [
    "ENTRY_NODE",
    "NODE_NAMES",
    "REASONING_EXCERPT_CHARS",
    "TERMINAL_NODE",
    "ContextScopingPipeline",
    "PeerReviewOutcome",
]
