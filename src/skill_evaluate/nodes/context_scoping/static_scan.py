"""模块二的纯代码扫描（docs/dev/12 第 3、4 节）。

本文件里**没有任何 LLM 调用**，也没有任何 IO：输入一个 `SkillDefinition`，输出
结构化的扫描结果。这样做的直接好处是这两个扫描器可以被 CI 当成 linter 单独调用
（架构文档模块二："类似传统的 Linter 运行模式"），也让它们的单测不需要任何替身。

两个扫描器的定位截然不同，注意区分：

- `scan_static_metrics()`：**确定性指标**。500 行 / 5,000 Token 是能精确算出来、
  不存在歧义的数字，因此它的结论直接用于阻断（docs/dev/12 第 6 节）。唯一的例外
  是计数器不精确时的不确定带，见 `StaticMetricsResult.needs_human_confirmation`。
- `scan_progressive_disclosure()`：**正则初筛**。它只负责把"看起来缺触发条件"的
  文件挑出来喂给 Mini Agent 复核，允许误报。把它做成精确解析器是没有意义的——
  "这句话算不算一个明确的触发条件"本来就是语义问题（架构文档对这一步的要求也
  只是"正则或语法树分析"）。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from skill_evaluate.ingestion.token_counter import TokenCounter, count_tokens
from skill_evaluate.logging import get_logger
from skill_evaluate.state.skill import SkillDefinition

logger = get_logger(component="context_scoping")

# 条件性表述的触发词。命中其中之一即认为"这句话尝试给出了加载条件"。
#
# 为什么是这一组词：它们都能引出一个**可判断的前提**（"什么情况下"），而
# "详见 X""更多信息见 X"这类只是指路、没有前提。中英并列是因为 SKILL.md 常见
# 中英混写。刻意不收 "参见/详见/见" ——那正是本扫描要抓的反面写法。
_CONDITION_PATTERNS = (
    r"当[^。；\n]{0,40}[时候]",  # 当……时 / 当……的时候
    r"如果",
    r"若",
    r"一旦",
    r"遇到",
    r"需要",
    r"仅当",
    r"只有",
    r"除非",
    r"在[^。；\n]{0,20}情况下",
    r"才(?:去|需|要|读|查|加载)",
    r"\bif\b",
    r"\bwhen(?:ever)?\b",
    r"\bonly\b",
    r"\bin case\b",
)
_CONDITION_RE = re.compile("|".join(_CONDITION_PATTERNS), re.IGNORECASE)

# Markdown 列表项 / 有序列表项的行首。列表项是一个独立的语义单元：上一个列表项
# 里的"当……时"不该被算作下一个列表项的触发条件，否则一份写成清单的 SKILL.md
# 会因为"邻居有条件词"而整体蒙混过关。
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


class StaticMetricsResult(BaseModel):
    """Token / 行数硬性扫描的结果（docs/dev/12 第 3 节）。

    比 docs/dev/12 正文多带了四个字段，都是为了让"这条阻断到底怎么来的"在报告里
    自证：限额本身（可配置，读报告的人不该去翻当时的环境变量）、计数器口径与
    精度、以及"数字落在不确定带里"这一情形。
    """

    line_count: int
    token_count: int
    line_limit: int
    token_limit: int
    line_limit_exceeded: bool
    token_limit_exceeded: bool
    token_count_method: str  # 形如 "tiktoken:o200k_base"，写进 findings 供复现
    token_count_exact: bool
    # 计数器不精确、且 token 数落在限额附近的不确定带内。此时**不阻断**，改为请人
    # 确认——一个 ±15% 的估算值不足以支撑"阻断别人的合并请求"这个动作
    # （docs/dev/interfaces/06 明确警告过的误判来源）。
    needs_human_confirmation: bool = False

    @property
    def hard_fail(self) -> bool:
        """是否构成硬性超标。

        行数永远是精确的（数换行符），所以行数超标一律成立；Token 超标在不确定带
        内时让位给 `needs_human_confirmation`。
        """
        if self.line_limit_exceeded:
            return True
        return self.token_limit_exceeded and not self.needs_human_confirmation


class MissingTriggerCandidate(BaseModel):
    """一个"疑似缺少按需加载触发条件"的参考文件。

    `reason` 区分两种成因，因为它们对作者的含义完全不同：正文压根没提这个文件
    （多半是文件被遗忘了），和提了但没说什么时候看（是渐进式披露没写完）。
    """

    path: str
    reason: Literal["not_mentioned", "no_condition"]
    evidence: str | None = None  # 正文中提及该文件的那一行原文，供 Mini Agent 复核


class ProgressiveDisclosureScan(BaseModel):
    """目录结构与触发条件的正则初筛结果（docs/dev/12 第 4 节）。"""

    reference_file_count: int
    candidates: list[MissingTriggerCandidate] = Field(default_factory=list)
    # 正文体量已接近限额、却一个参考文件都没有：该做渐进式披露而没做
    # （架构文档模块二第 1 节"确认大型 Skill 是否按规范将长配置或参考资料移至
    # references/ 目录"）。这一项与触发条件无关，但同属"目录结构审查"。
    bulk_inline_without_references: bool = False

    @property
    def candidate_paths(self) -> list[str]:
        return [c.path for c in self.candidates]


def scan_static_metrics(
    skill: SkillDefinition,
    *,
    line_limit: int = 500,
    token_limit: int = 5000,
    token_counter: TokenCounter = count_tokens,
    estimate_uncertainty_ratio: float = 0.15,
) -> StaticMetricsResult:
    """行数 / Token 数硬性扫描。

    **重新计算而不是直接读 `skill.line_count` / `skill.token_count`**，有两个理由：

    1. 库里那两个字段可能是很久以前、用当时那版计数器算出来的（docs/dev/06 的
       粗估桩），拿它去卡线正是 docs/dev/interfaces/06 警告过的场景；
    2. 计数器的精度元信息（`TokenCount.exact`）只有当场算才拿得到，而本维度的
       阻断策略依赖它。

    口径（docs/dev/12 第 3 节）：行数 = `body_markdown.splitlines()` 长度，即
    **不含 YAML frontmatter**——frontmatter 不是给模型读的正文。
    """
    line_count = len(skill.body_markdown.splitlines())
    tokens = token_counter(skill.body_markdown)

    if line_count != skill.line_count or tokens.value != skill.token_count:
        # 不算错误：换过计数器、或 SkillDefinition 由别处构造时都会不一致。但要留痕，
        # 否则"报告里的数字和库里的字段对不上"会变成一桩无从查起的悬案。
        logger.info(
            "context_scoping_metrics_recomputed",
            skill_id=skill.skill_id,
            stored_line_count=skill.line_count,
            recomputed_line_count=line_count,
            stored_token_count=skill.token_count,
            recomputed_token_count=tokens.value,
            token_count_method=tokens.method,
        )

    token_exceeded = tokens.value > token_limit
    # 不确定带：|token - 限额| <= 限额 × ratio。只在计数器不精确时才成立。
    uncertain = (
        not tokens.exact
        and abs(tokens.value - token_limit) <= token_limit * estimate_uncertainty_ratio
    )

    return StaticMetricsResult(
        line_count=line_count,
        token_count=tokens.value,
        line_limit=line_limit,
        token_limit=token_limit,
        line_limit_exceeded=line_count > line_limit,
        token_limit_exceeded=token_exceeded,
        token_count_method=tokens.method,
        token_count_exact=tokens.exact,
        # 只有"估算值判超标"才需要人来确认。估算值判**没**超标但落在带内时不打扰
        # 人：本维度的阻断只发生在超标方向上，没超标就没有要复核的动作。
        needs_human_confirmation=token_exceeded and uncertain,
    )


def scan_progressive_disclosure(
    skill: SkillDefinition,
    *,
    line_limit: int = 500,
    token_limit: int = 5000,
    bulk_inline_ratio: float = 0.8,
    token_count: int | None = None,
) -> ProgressiveDisclosureScan:
    """扫出缺少"按需加载触发条件说明"的参考文件（docs/dev/12 第 4 节）。

    判定规则：对每个 `skill.reference_files`，在正文里找到提及该路径（或其文件名）
    的位置，取**该位置所在的语义单元**（列表项就是那一项，否则是整个自然段），
    检查单元内是否出现条件性表述（`_CONDITION_PATTERNS`）。

    为什么限定在"同一语义单元"而不是整篇文档里搜条件词：几乎任何一篇 SKILL.md
    都会在别处出现"如果""当……时"，全文搜索的结果必然是"全部通过"，这个扫描也就
    等于没做。

    这是**正则启发式，允许误报**——命中清单会交给 Mini Agent 的
    `progressive_disclosure_static` 模板复核哪些是真问题。两阶段结合是为了兼顾
    成本与准确度：正则免费但笨，LLM 准确但要钱，让正则先把范围缩小。

    `token_count` 传 `scan_static_metrics()` 刚算出来的值，避免用 `skill.token_count`
    ——那可能是很久以前用另一版计数器算的（同 `scan_static_metrics()` 的说明）。
    不传则退化为库里的字段，单独当 linter 用时够用。
    """
    lines = skill.body_markdown.splitlines()
    candidates: list[MissingTriggerCandidate] = []

    for reference in skill.reference_files:
        index = _find_mention(lines, reference.path)
        if index is None:
            candidates.append(
                MissingTriggerCandidate(path=reference.path, reason="not_mentioned")
            )
            continue
        unit = _semantic_unit(lines, index)
        if _CONDITION_RE.search(unit) is None:
            candidates.append(
                MissingTriggerCandidate(
                    path=reference.path, reason="no_condition", evidence=lines[index].strip()
                )
            )

    return ProgressiveDisclosureScan(
        reference_file_count=len(skill.reference_files),
        candidates=candidates,
        bulk_inline_without_references=_is_bulk_inline(
            skill,
            line_limit=line_limit,
            token_limit=token_limit,
            bulk_inline_ratio=bulk_inline_ratio,
            token_count=skill.token_count if token_count is None else token_count,
        ),
    )


def format_reference_files_for_review(
    skill: SkillDefinition, scan: ProgressiveDisclosureScan
) -> str:
    """把参考文件清单 + 正则初筛结论渲染成 `progressive_disclosure_static` 模板的
    `reference_files` 变量（模板里的说明：含检索到的相关行，未检索到则标注"正文
    未提及"）。

    **把初筛结论一并交给模型**，而不是只给文件清单：docs/dev/12 第 4 节要的正是
    "让 LLM 复核这份正则初筛结果里哪些是真问题、哪些是正则误报"。同时保留全部
    文件（含初筛认为合格的），模型才有对照组——只喂可疑项会诱导它把每一项都判成
    问题。

    每行的形状：`路径 | 初筛结论 | 证据行`。用一行一条而不是塞 JSON，省 token 也
    更好读（与 docs/dev/interfaces/07 第 3 节对 Trace 序列化的建议同一思路）。
    """
    if not skill.reference_files:
        return "（这份 Skill 没有 references/ 目录，或目录为空。）"

    by_path = {c.path: c for c in scan.candidates}
    rows: list[str] = []
    for reference in skill.reference_files:
        candidate = by_path.get(reference.path)
        if candidate is None:
            verdict = "正则初筛：正文中该文件附近存在条件性表述"
        elif candidate.reason == "not_mentioned":
            verdict = "正则初筛：正文未提及该文件"
        else:
            verdict = "正则初筛：正文提及了该文件，但附近没有条件性表述"
        evidence = (candidate.evidence if candidate else None) or reference.trigger_condition
        rows.append(f"- {reference.path} | {verdict} | 相关行：{evidence or '（无）'}")

    return "\n".join(rows)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _find_mention(lines: list[str], relative_path: str) -> int | None:
    """返回首次提及该参考文件的行号。

    整路径优先于文件名：`references/errors.md` 与 `errors.md` 都算提及，但整路径
    的匹配更可信，先扫一遍整路径能避免"目录下有同名文件"时定位到错误的那一处。
    """
    for index, line in enumerate(lines):
        if relative_path in line:
            return index
    name = relative_path.rsplit("/", 1)[-1]
    for index, line in enumerate(lines):
        if name in line:
            return index
    return None


def _semantic_unit(lines: list[str], index: int) -> str:
    """取第 `index` 行所在的语义单元：列表项 = 该项（含其缩进续行），否则 = 自然段。

    段落用空行分隔；列表项用行首标记识别。这一层是整个初筛准确度的关键：范围取
    大了会漏（隔壁的条件词被算进来），取小了会误报（条件写在同一段的上一句里）。
    """
    if _LIST_ITEM_RE.match(lines[index]):
        unit = [lines[index]]
        # 列表项的续行：比该项缩进更深、且自身不是新列表项的行。
        for line in lines[index + 1 :]:
            if not line.strip() or _LIST_ITEM_RE.match(line) or not line.startswith((" ", "\t")):
                break
            unit.append(line)
        return "\n".join(unit)

    start = index
    while start > 0 and lines[start - 1].strip():
        start -= 1
    end = index
    while end + 1 < len(lines) and lines[end + 1].strip():
        end += 1
    return "\n".join(lines[start : end + 1])


def _is_bulk_inline(
    skill: SkillDefinition,
    *,
    line_limit: int,
    token_limit: int,
    bulk_inline_ratio: float,
    token_count: int,
) -> bool:
    """正文已接近限额、却没有任何参考文件 = 该拆分而没拆。

    用"接近限额"（默认 80%）而不是"已超限额"作阈值：等超标了再提示，作者面对的
    是一次返工；在 400 行左右提醒，他还只需要挪一节内容。这一项是**非阻断**的
    建议（docs/dev/12 第 6 节：主观判断走 Warning），所以早提示不会误伤合并。
    """
    if skill.reference_files:
        return False
    lines = len(skill.body_markdown.splitlines())
    return lines >= line_limit * bulk_inline_ratio or token_count >= token_limit * bulk_inline_ratio


__all__ = [
    "MissingTriggerCandidate",
    "ProgressiveDisclosureScan",
    "StaticMetricsResult",
    "format_reference_files_for_review",
    "scan_progressive_disclosure",
    "scan_static_metrics",
]
