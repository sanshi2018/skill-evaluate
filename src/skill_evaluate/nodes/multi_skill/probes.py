"""模块十的纯函数工具（不碰库、不发请求、不起沙箱，便于单测与复现）。

- 工具命名空间冲突扫描（docs/dev/20 第 5 节）；
- 高频交替报错检测（第 7 节"死锁判定用确定性规则"）；
- 业务步骤顺序打乱（第 9 节 `_shuffle_step_order()`）；
- 角色/风格预设句抽取与干扰包渲染（第 9 节静态审查的输入）。
"""

from __future__ import annotations

import random
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.trace import ActionStep

# --------------------------------------------------------------------------- #
# 1. 工具命名空间
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ToolCollision:
    """一个被多个 Skill 同时暴露的工具名。"""

    tool_name: str
    skill_ids: tuple[str, ...]  # 按 skill_id 排序，便于报告稳定

    def involves(self, skill_id: str) -> bool:
        return skill_id in self.skill_ids


def find_tool_name_collisions(skills: Sequence[SkillDefinition]) -> list[ToolCollision]:
    """扫描一组 Skill 的 `scripts[*].exposed_tool_name`，返回被 ≥2 个 Skill 暴露的工具名。

    按**小写**比较：多数 Agent 运行时的工具注册表大小写不敏感，`Parse_Data` 与
    `parse_data` 在那里就是同一个名字。同一个 Skill 内部重复暴露不算跨技能冲突
    （那是模块四的问题），因此按 skill_id 去重。
    """
    owners: dict[str, set[str]] = defaultdict(set)
    display: dict[str, str] = {}
    for skill in skills:
        for script in skill.scripts:
            if not script.exposed_tool_name:
                continue
            key = script.exposed_tool_name.lower()
            owners[key].add(skill.skill_id)
            display.setdefault(key, script.exposed_tool_name)
    return [
        ToolCollision(tool_name=display[key], skill_ids=tuple(sorted(ids)))
        for key, ids in sorted(owners.items())
        if len(ids) > 1
    ]


def namespace_prefixes(skill: SkillDefinition) -> tuple[str, ...]:
    """一个 Skill 的工具名"合格前缀"：skill_id 规范化后的全名，以及它的第一个词。

    `csv-cleaner` → (`csv_cleaner`, `csv`)。架构文档举的例子正是 `csv_parse_data`。
    """
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", skill.skill_id).strip("_").lower()
    head = normalized.split("_", 1)[0]
    return tuple(dict.fromkeys(p for p in (normalized, head) if p))


def unprefixed_tools(skill: SkillDefinition) -> list[str]:
    """没有带本 Skill 命名空间前缀的工具名（**建议项**，不作为冲突发现）。

    今天没撞名不代表明天不撞：干扰包会换、技能库会长。这里只提示"建议前缀化"，
    不判 FAIL——真正的冲突由 `find_tool_name_collisions()` 判。
    """
    prefixes = namespace_prefixes(skill)
    result: list[str] = []
    for script in skill.scripts:
        name = script.exposed_tool_name
        if name and not any(name.lower().startswith(f"{p}_") for p in prefixes):
            result.append(name)
    return sorted(set(result))


# --------------------------------------------------------------------------- #
# 2. 高频交替报错
# --------------------------------------------------------------------------- #

_COMMAND_KEYS: tuple[str, ...] = ("command", "cmd", "script", "code", "path", "file_path")
_SCRIPT_LIKE = re.compile(r"[/\\]|\.(py|sh|js|ts|rb|go|sql|json|csv|md)$", re.IGNORECASE)


def error_signature(step: ActionStep) -> str:
    """一次报错动作的"是谁在报错"签名：`action_type:<脚本或命令名>`。

    只用 `action_type` 不够：校验循环里两边的报错通常都是 `bash`，区别在于调的是
    `validate_json.py` 还是 `lint_markdown.py`。取命令里第一个"像脚本/文件"的片段，
    没有就取第一个词（`jq`、`pytest`）。
    """
    raw = ""
    for key in _COMMAND_KEYS:
        value = step.action_input.get(key)
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            break
    tokens = raw.split()
    head = next((t for t in tokens if _SCRIPT_LIKE.search(t)), tokens[0] if tokens else "")
    return f"{step.action_type}:{PurePosixPath(head).name if head else ''}"


def count_error_ping_pong(actions: Sequence[ActionStep]) -> int:
    """报错动作序列里 "A→B→A" 式往返的次数。

    1. 只取报错的动作（`exit_code` 非空且非 0），按执行顺序排列；
    2. 折叠相邻的相同签名（A A B B A 视为 A B A）：对同一个校验器连续重试是"反复
       试错"（模块三的效率问题），不是两套指令在互相拉扯；
    3. 数 `s[i] == s[i-2] != s[i-1]` 出现的次数。

    A B A B A → 3 次。只在两个（或以上）不同的报错来源之间来回切换才计数，这正是
    架构文档"为了满足 A 的指令而打破了 B 的约束"的轨迹形状。
    """
    signatures = [error_signature(a) for a in actions if a.exit_code not in (None, 0)]
    collapsed: list[str] = []
    for signature in signatures:
        if not collapsed or collapsed[-1] != signature:
            collapsed.append(signature)
    return sum(
        1
        for i in range(2, len(collapsed))
        if collapsed[i] == collapsed[i - 2] and collapsed[i] != collapsed[i - 1]
    )


# --------------------------------------------------------------------------- #
# 3. 业务步骤顺序打乱
# --------------------------------------------------------------------------- #

# 英文句号只在后跟空白时切分：`data.csv`、`v1.2` 里的点不是句末。
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；;!?\n])|(?<=\.)\s+")
# 句内按"，然后 / ，再 / ，最后"这类顺序连接词切分（中文提问里常把几步写在一句话里）。
_CLAUSE_SPLIT = re.compile(
    r"[，,]\s*(?=(?:然后|再|接着|之后|随后|最后|and then\b|then\b|finally\b|after that\b))",
    re.IGNORECASE,
)
# 段首的顺序标记词：打乱位置后若还留着"先/最后"，Prompt 仍在用词语暗示原顺序，
# 起不到"逆序描述"的压力作用。
_ORDER_MARKERS = re.compile(
    r"^(?:首先|先|然后|再|接着|之后|随后|最后|第[一二三四五六七八九十\d]+步[，,:：]?|"
    r"\d+[.)、]|(?:first(?:ly)?|and then|then|next|finally|after that)\b,?)\s*",
    re.IGNORECASE,
)
_CJK = re.compile(r"[一-鿿]")


def _split_steps(prompt: str) -> list[str]:
    segments = [s for s in _SENTENCE_SPLIT.split(prompt) if s.strip()]
    if len(segments) < 2:
        segments = [s for s in _CLAUSE_SPLIT.split(prompt) if s.strip()]
    cleaned: list[str] = []
    for segment in segments:
        text = segment.strip()
        previous = None
        while previous != text:  # "然后再" 这类叠用的标记词逐个剥掉
            previous = text
            text = _ORDER_MARKERS.sub("", text).strip()
        text = text.strip("，,；;。.!！?？ \n")
        if text:
            cleaned.append(text)
    return cleaned


def shuffle_step_order(prompt: str, seed: str) -> str | None:
    """把 Prompt 里的业务步骤打乱顺序后重新拼接；拆不出 ≥2 步时返回 None。

    是**启发式**文本重排（docs/dev/20 第 9 节），不追求语义完美，只为制造"逆序调用"的
    压力场景。确定性：同一个 seed 永远得到同一个排列（字符串种子，跨进程稳定）；
    随机排列恰好等于原顺序时整体轮转一位，保证真的被打乱了。
    """
    steps = _split_steps(prompt)
    if len(steps) < 2:
        return None
    order = list(range(len(steps)))
    random.Random(seed).shuffle(order)
    if order == sorted(order):
        order = order[1:] + order[:1]
    separator = "；" if _CJK.search(prompt) else "; "
    terminator = "。" if _CJK.search(prompt) else "."
    return separator.join(steps[i] for i in order) + terminator


# --------------------------------------------------------------------------- #
# 4. 角色/风格预设与干扰包渲染
# --------------------------------------------------------------------------- #

_PERSONA_PATTERN = re.compile(
    r"(你是|你将扮演|扮演|作为一名|作为一个|你的角色|始终以|语气|风格|"
    r"\byou are\b|\bact as\b|\byour role\b|\bpersona\b|\balways respond\b|\bonly output\b|"
    r"只输出|仅输出|一律使用|必须使用\s*(?:JSON|Markdown))",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^\s*(```|~~~)")
PERSONA_LINE_MAX_CHARS = 200


def extract_persona_lines(body_markdown: str, *, limit: int = 5) -> list[str]:
    """从 SKILL.md 正文里抽出"角色设定 / 输出风格预设"的句子（代码块内除外）。

    给 `role_persona_conflict` 模板用：docs/dev/20 正文只给模型看干扰包的 description，
    但角色设定几乎总是写在**正文**里（"你是一个严谨的 DBA"），description 里看不到。
    整篇正文塞进去又会让 Prompt 随干扰包规模线性膨胀——抽句子是两者的折中。
    刻意偏向召回：多给几句无关的句子，模型会判"不冲突"；漏给关键句，模型无从判起。
    """
    lines: list[str] = []
    in_fence = False
    for raw in body_markdown.splitlines():
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        text = raw.strip().lstrip("#>-* ").strip()
        if text and _PERSONA_PATTERN.search(text):
            lines.append(text[:PERSONA_LINE_MAX_CHARS])
        if len(lines) >= limit:
            break
    return lines


def format_noise_pack_for_review(skills: Sequence[SkillDefinition]) -> str:
    """干扰包渲染成 `role_persona_conflict` 模板的 `noise_pack_descriptions` 变量。"""
    if not skills:
        return "（没有同时装载的其他 Skill）"
    blocks: list[str] = []
    for skill in skills:
        persona = extract_persona_lines(skill.body_markdown)
        persona_text = (
            "\n".join(f"  - {line}" for line in persona) or "  - （正文中未发现角色/风格预设句）"
        )
        blocks.append(
            f"### {skill.skill_id}\n描述：{skill.description}\n角色/风格预设：\n{persona_text}"
        )
    return "\n\n".join(blocks)


def format_background_skills(skills: Sequence[SkillDefinition]) -> str:
    """干扰包渲染成 `semantic_flow_friction` 模板的 `background_skills` 变量（只要描述）。"""
    if not skills:
        return "（没有同时装载的其他 Skill）"
    return "\n".join(f"- {skill.skill_id}：{skill.description}" for skill in skills)


__all__ = [
    "PERSONA_LINE_MAX_CHARS",
    "ToolCollision",
    "count_error_ping_pong",
    "error_signature",
    "extract_persona_lines",
    "find_tool_name_collisions",
    "format_background_skills",
    "format_noise_pack_for_review",
    "namespace_prefixes",
    "shuffle_step_order",
    "unprefixed_tools",
]
