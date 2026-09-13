"""AI 话术黑名单词典与确定性消融（docs/dev/19 第 6、8 节）。

**一套词典，三处消费**：

1. `nodes/cross_model` 的**随机消融测试**：按词典把 SKILL.md 里的"咒语"随机删掉一部分，
   看任务还做不做得成（动态实验）；
2. `nodes/cross_model` 的**语言坏味道审查**：词典命中清单作为提示交给
   `linguistic_smell` 模板复核（静态审查）；
3. `agents/optimizer/consensus_gate.py` 的**模型怪癖剥离拦截器**：补丁里新增了词典
   命中项即打回（架构文档模块九"Model-Quirk Stripping"）。

三处共用同一份词典是 docs/dev/19 第 8 节的硬要求：两份词典早晚会漂移，漂移的表现
是"静态审查说没问题，消融测试却发现依赖"——报告自相矛盾，读的人不知道该信哪个。

## 维护约定

词典允许持续补充，**不需要新文档**（docs/dev/19 第 10 节）。补充时注意三点：

- 只收"用情绪/身份/音量代替信息"的措辞。"删除前必须确认路径存在"是讲道理的强硬
  约束，**不是**咒语——把 `必须` 这类通用情态词收进来，消融测试就会把 Skill 真正的
  领域步骤一起删掉，实验结论从此失真；
- 每条正则都要能在代码块之外被安全删除而不破坏句子主干；
- 改完跑 `tests/skill_evaluate/test_cross_model.py` 的词典用例。

## 代码不参与消融

围栏代码块（```）与行内代码（`...`）一律视为受保护区域：代码是 Skill 的真实专有知识
（命令、JSON 模板、API 规范），恰恰是架构文档说"指令必须回归到"的那部分。一段
示例 prompt 里写着"think step by step"不代表 Skill 本身在念咒。
"""

from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel


class LexiconKind(StrEnum):
    """词典条目的类别。消融时不同类别的处理方式不同（删除 / 降级为小写 / 折叠）。"""

    INCANTATION = "incantation"  # "仔细思考""think step by step"这类思维咒语
    PERSONA_FLATTERY = "persona_flattery"  # "你是世界级专家"这类身份抬举
    MODEL_ADDRESS = "model_address"  # 点名某个模型："like a Hermes model"
    EMOTIONAL_PRESSURE = "emotional_pressure"  # "务必""这对我的职业生涯很重要"、许诺奖励/惩罚
    CAPS_EMPHASIS = "caps_emphasis"  # 全大写音量式强调：ALWAYS / NEVER / MUST
    PUNCTUATION_EMPHASIS = "punctuation_emphasis"  # 连串感叹号


@dataclass(frozen=True, slots=True)
class LexiconEntry:
    kind: LexiconKind
    pattern: re.Pattern[str]


def _entry(kind: LexiconKind, pattern: str, *, ignore_case: bool = True) -> LexiconEntry:
    flags = re.IGNORECASE if ignore_case else 0
    return LexiconEntry(kind=kind, pattern=re.compile(pattern, flags))


# 模型名清单，供"点名模型"类条目使用。只放模型家族名，不放版本号：版本号靠 `[\w.-]*` 吞掉。
_MODEL_NAMES = r"(?:hermes|gpt|chatgpt|claude|llama|gemini|mistral|qwen|deepseek|o\d)"

LEXICON: tuple[LexiconEntry, ...] = (
    # ---- 思维咒语 ----
    _entry(LexiconKind.INCANTATION, r"(?:请)?(?:认真)?仔细(?:地)?思考(?:一下)?"),
    _entry(LexiconKind.INCANTATION, r"一步一步(?:地)?(?:思考|推理|来)"),
    _entry(LexiconKind.INCANTATION, r"深呼吸(?:一下)?"),
    _entry(LexiconKind.INCANTATION, r"think\s+(?:about\s+it\s+)?step[\s-]+by[\s-]+step"),
    _entry(LexiconKind.INCANTATION, r"let'?s\s+think\s+carefully"),
    _entry(LexiconKind.INCANTATION, r"take\s+a\s+deep\s+breath"),
    # ---- 身份抬举 ----
    _entry(
        LexiconKind.PERSONA_FLATTERY,
        r"你是(?:一(?:个|位|名))?(?:世界上最(?:好|强|优秀)的|世界级的?|顶级的?|资深的?)"
        r"[^。！!\n，,]{0,16}(?:专家|大师|工程师|顾问|分析师|助手)",
    ),
    _entry(
        LexiconKind.PERSONA_FLATTERY,
        r"you\s+are\s+(?:an?\s+|the\s+)?(?:world[\s-]class|world'?s\s+best|genius|"
        r"top[\s-]tier|brilliant)\s+[\w\s-]{0,24}?(?:expert|engineer|assistant|analyst)",
    ),
    # ---- 点名模型 ----
    _entry(
        LexiconKind.MODEL_ADDRESS,
        # 尾部 model / would 都可选但至少要吞掉：留下半截 "would." 会让消融后的句子
        # 语义残缺，测的就不只是"去掉咒语"了。
        rf"(?:like|as)\s+an?\s+{_MODEL_NAMES}[\w.-]*\s+(?:model(?:\s+(?:would|does))?|would)",
    ),
    _entry(LexiconKind.MODEL_ADDRESS, rf"像\s*{_MODEL_NAMES}[\w.-]*\s*(?:模型)?(?:一样|那样)"),
    # ---- 情绪施压 / 许诺奖惩 ----
    _entry(LexiconKind.EMOTIONAL_PRESSURE, r"务必"),
    _entry(LexiconKind.EMOTIONAL_PRESSURE, r"一定要"),
    _entry(LexiconKind.EMOTIONAL_PRESSURE, r"千万(?:不要|别)"),
    _entry(LexiconKind.EMOTIONAL_PRESSURE, r"(?:这|此事)对我(?:的职业生涯)?(?:非常|十分|很)重要"),
    _entry(LexiconKind.EMOTIONAL_PRESSURE, r"(?:我会|将)(?:给你)?(?:\d+\s*(?:美元|元)的?)?小费"),
    _entry(LexiconKind.EMOTIONAL_PRESSURE, r"否则(?:你)?(?:会|将)(?:被)?(?:惩罚|扣分|解雇)"),
    _entry(
        LexiconKind.EMOTIONAL_PRESSURE,
        r"this\s+is\s+(?:very|extremely|really)\s+important\s+(?:to|for)\s+my\s+career",
    ),
    _entry(LexiconKind.EMOTIONAL_PRESSURE, r"i\s+will\s+tip\s+you(?:\s+\$?\d+)?"),
    _entry(
        LexiconKind.EMOTIONAL_PRESSURE, r"(?:or\s+)?you\s+will\s+be\s+(?:penalized|punished|fired)"
    ),
    # ---- 音量式强调（大小写敏感：只抓全大写） ----
    _entry(
        LexiconKind.CAPS_EMPHASIS,
        r"\b(?:ALWAYS|NEVER|MUST|IMPORTANT|CRITICAL|CRUCIAL|ABSOLUTELY|DO NOT|DON'T)\b",
        ignore_case=False,
    ),
    _entry(LexiconKind.PUNCTUATION_EMPHASIS, r"[!！]{2,}"),
)

# 这些类别在消融时整段删除；其余类别做"降级"而不是删除（见 `_replacement()`）。
_REMOVABLE_KINDS = frozenset(
    {
        LexiconKind.INCANTATION,
        LexiconKind.PERSONA_FLATTERY,
        LexiconKind.MODEL_ADDRESS,
        LexiconKind.EMOTIONAL_PRESSURE,
    }
)

_FENCED_CODE = re.compile(r"^(```|~~~)[^\n]*\n.*?^\1[ \t]*$", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]+`")


class LexiconHit(BaseModel):
    """一处词典命中。`start/end` 是在原文里的字符偏移，消融时按它做替换。"""

    kind: LexiconKind
    text: str
    line_no: int  # 1-based，报告与审查提示里用
    start: int
    end: int


class AblationResult(BaseModel):
    """一次消融的产物。`dropped` 为空说明文本里没有可剥离的措辞（或概率抽样全没抽中）。"""

    original: str
    ablated: str
    hits: list[LexiconHit]
    dropped: list[LexiconHit]

    @property
    def changed(self) -> bool:
        return self.ablated != self.original


def _protected_spans(text: str) -> list[tuple[int, int]]:
    spans = [(m.start(), m.end()) for m in _FENCED_CODE.finditer(text)]
    for match in _INLINE_CODE.finditer(text):
        if not any(start <= match.start() < end for start, end in spans):
            spans.append((match.start(), match.end()))
    return spans


def scan_lexicon(text: str) -> list[LexiconHit]:
    """扫描文本中的词典命中（代码区域除外），按出现顺序返回，**互不重叠**。

    两条条目命中重叠区域时保留更长的那条（"请认真仔细地思考"不该被拆成两处命中），
    否则消融替换时偏移会互相踩踏。
    """
    protected = _protected_spans(text)
    candidates: list[LexiconHit] = []
    for entry in LEXICON:
        for match in entry.pattern.finditer(text):
            if match.start() == match.end():
                continue
            if any(start < match.end() and match.start() < end for start, end in protected):
                continue
            candidates.append(
                LexiconHit(
                    kind=entry.kind,
                    text=match.group(0),
                    line_no=text.count("\n", 0, match.start()) + 1,
                    start=match.start(),
                    end=match.end(),
                )
            )

    candidates.sort(key=lambda hit: (hit.start, -(hit.end - hit.start)))
    hits: list[LexiconHit] = []
    for hit in candidates:
        if hits and hit.start < hits[-1].end:
            continue
        hits.append(hit)
    return hits


def ablation_seed(skill_id: str) -> str:
    """同一个 Skill 的消融版本可复现所用的种子。

    docs/dev/19 正文写的是 `seed=hash(state["skill_id"])`——Python 的 `hash(str)` 受
    `PYTHONHASHSEED` 随机化影响，**每个进程都不一样**，断点恢复后重跑会得到另一个
    消融版本。这里改用字符串种子（`random.Random` 对 str 种子走 SHA-512，跨进程稳定），
    与 docs/dev/06 第 6 节的确定性 shuffle 同一写法。
    """
    return f"skill-evaluate:ablation:{skill_id}"


def _replacement(hit: LexiconHit) -> str:
    """一处命中被"消融"后变成什么。

    - 咒语 / 抬举 / 点名模型 / 情绪施压：整段删除——它们不携带任务信息；
    - 全大写强调：降级为小写——`NEVER delete` 里的 never 本身是信息，删掉会把禁令
      反转成许可，那测的就不再是"去掉音量后还行不行"，而是"改写了语义后还行不行"；
    - 连串感叹号：折叠为一个句号（保持句子边界）。
    """
    if hit.kind in _REMOVABLE_KINDS:
        return ""
    if hit.kind is LexiconKind.CAPS_EMPHASIS:
        return hit.text.lower()
    return "。" if "！" in hit.text else "."


def ablate(body_markdown: str, seed: int | str, *, drop_probability: float = 0.8) -> AblationResult:
    """按词典随机屏蔽/删减部分措辞，确定性（同 seed 同结果）。

    随机性只决定"哪些命中被删"，删法固定（`_replacement()`）。按出现顺序逐个抽签，
    因此在文本其余部分不变时，同一处措辞的去留也稳定。
    """
    if not 0.0 <= drop_probability <= 1.0:
        raise ValueError(f"drop_probability 必须在 [0, 1] 内，收到 {drop_probability}")
    hits = scan_lexicon(body_markdown)
    rng = random.Random(seed)
    dropped = [hit for hit in hits if rng.random() < drop_probability]

    pieces: list[str] = []
    cursor = 0
    for hit in dropped:
        pieces.append(body_markdown[cursor : hit.start])
        pieces.append(_replacement(hit))
        cursor = hit.end
    pieces.append(body_markdown[cursor:])
    return AblationResult(
        original=body_markdown, ablated="".join(pieces), hits=hits, dropped=dropped
    )


def ablate_skill_text(body_markdown: str, seed: int | str, *, drop_probability: float = 0.8) -> str:
    """docs/dev/19 第 6 节给出的签名：只要消融后的文本。需要命中明细请用 `ablate()`。"""
    return ablate(body_markdown, seed, drop_probability=drop_probability).ablated


def _normalized(hit: LexiconHit) -> str:
    return re.sub(r"\s+", " ", hit.text.strip().lower())


def introduced_hits(before: str, after: str) -> list[LexiconHit]:
    """`after` 相对 `before` **新增**的词典命中（按归一化文本计数做多重集差）。

    供模型怪癖剥离拦截器使用：原版 SKILL.md 里本来就有的措辞不算补丁的锅（那是模块九
    语言坏味道审查要报的问题），补丁**新写进去**的才打回。按计数而不是按集合比较：
    原文有一处"务必"、补丁又加了三处，同样是在往里灌咒语。
    """
    remaining = Counter(_normalized(hit) for hit in scan_lexicon(before))
    introduced: list[LexiconHit] = []
    for hit in scan_lexicon(after):
        key = _normalized(hit)
        if remaining[key] > 0:
            remaining[key] -= 1
            continue
        introduced.append(hit)
    return introduced


def format_hits_for_review(hits: list[LexiconHit], *, limit: int = 30) -> str:
    """把命中清单渲染成交给 `linguistic_smell` 模板的提示文本（每行一条）。

    设上限：一份满篇 ALWAYS 的 Skill 可能有上百处命中，全塞进 Prompt 只会淹没正文本身。
    超出部分写明被省略了多少条，不假装清单是完整的。
    """
    if not hits:
        return "（词典未命中任何条目）"
    lines = [f"- 第 {hit.line_no} 行 [{hit.kind.value}] {hit.text!r}" for hit in hits[:limit]]
    if len(hits) > limit:
        lines.append(f"- ……另有 {len(hits) - limit} 处命中未列出")
    return "\n".join(lines)


__all__ = [
    "LEXICON",
    "AblationResult",
    "LexiconEntry",
    "LexiconHit",
    "LexiconKind",
    "ablate",
    "ablate_skill_text",
    "ablation_seed",
    "format_hits_for_review",
    "introduced_hits",
    "scan_lexicon",
]
