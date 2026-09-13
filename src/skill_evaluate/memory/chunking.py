"""SKILL.md 正文分块：按标题层级切，超长章节再按段落二级切分（docs/dev/23 第 3.3.1 节）。

## 为什么不用固定 Token 窗口

固定窗口滑动切分极易把一条完整指令的"前提条件"和"具体步骤"切进两个 chunk：检索命中
"步骤"那一半时，模型看到的是一段丢了前提的操作清单，照着做反而会出错。按 `##` / `###`
标题切，每个 chunk 都是作者自己划定的一个自洽指令单元（"错误处理"整节、"参数说明"整节）。

## 两条细节

- **标题路径随 chunk 一起入库**（`heading_path`，如 `["数据清洗", "错误处理"]`）：三级标题
  "错误处理"脱离了它的二级标题，读者无从知道这是哪个功能的错误处理。
- **代码块内的 `#` 不是标题**：Bash 注释、Python 注释在 fenced code 里以 `#` 开头，当成标题切
  会把一段示例脚本拦腰截断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from skill_evaluate.ingestion.token_counter import estimate_token_count

# 只按 ## 与 ### 切（文档 23 原文）。`#` 一级标题在 SKILL.md 里通常就是文档名，按它切只会得到
# 一个几乎等于全文的 chunk；四级及以下标题粒度太碎，并入所属的三级章节。
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SPLIT_LEVELS = (2, 3)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True, slots=True)
class MarkdownChunk:
    """一个自洽的指令单元。"""

    heading_path: tuple[str, ...]  # 从二级标题到本节标题的路径；标题前的导言为空元组
    text: str  # 含本节标题行在内的原文
    index: int  # 在全文中的顺序（从 0 开始）
    part: int = 0  # 超长章节二级切分后的分片序号；未切分为 0
    token_estimate: int = field(default=0)

    @property
    def title(self) -> str:
        return " / ".join(self.heading_path) if self.heading_path else "(导言)"


def chunk_markdown(markdown: str, *, max_chunk_tokens: int = 800) -> list[MarkdownChunk]:
    """把 Markdown 正文切成 chunk 列表（保持原文顺序，空白章节丢弃）。"""
    sections = _split_by_headings(markdown)
    chunks: list[MarkdownChunk] = []
    for heading_path, text in sections:
        body = text.strip()
        if not body:
            continue
        pieces = _split_oversized(body, max_chunk_tokens)
        for part, piece in enumerate(pieces):
            chunks.append(
                MarkdownChunk(
                    heading_path=heading_path,
                    text=piece,
                    index=len(chunks),
                    part=part if len(pieces) > 1 else 0,
                    token_estimate=estimate_token_count(piece),
                )
            )
    return chunks


def _split_by_headings(markdown: str) -> list[tuple[tuple[str, ...], str]]:
    """按 `##`/`###` 切出 `(标题路径, 章节原文)`，跳过 fenced code 内部的 `#` 行。"""
    sections: list[tuple[tuple[str, ...], str]] = []
    path: list[str] = []  # 当前二级、三级标题栈
    buffer: list[str] = []
    current_path: tuple[str, ...] = ()
    in_fence = False

    for line in markdown.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            buffer.append(line)
            continue
        match = None if in_fence else _HEADING_RE.match(line)
        if match and len(match.group(1)) in _SPLIT_LEVELS:
            sections.append((current_path, "\n".join(buffer)))
            buffer = [line]
            level = len(match.group(1))
            # level 2 → 栈深 0 后压入；level 3 → 保留二级标题后压入。
            del path[level - 2 :]
            path.append(match.group(2).strip())
            current_path = tuple(path)
            continue
        buffer.append(line)
    sections.append((current_path, "\n".join(buffer)))
    return sections


def _split_oversized(text: str, max_tokens: int) -> list[str]:
    """超过 `max_tokens` 的章节按段落（空行）贪心合并切分；单段仍超长则按行再切。

    第二片起**重复带上本节标题行**：没有标题的分片被单独检索出来时，读者看不出它属于哪一节。
    fenced code 不在段落边界上拆开——一段被截成两半的示例脚本比一个稍大的 chunk 糟糕得多。
    """
    if estimate_token_count(text) <= max_tokens:
        return [text]

    lines = text.splitlines()
    heading = lines[0] if lines and _HEADING_RE.match(lines[0]) else ""
    paragraphs = _paragraphs(lines[1:] if heading else lines)

    pieces: list[str] = []
    current: list[str] = []
    for paragraph in paragraphs:
        for unit in _split_long_paragraph(paragraph, max_tokens):
            candidate = "\n\n".join([*current, unit])
            if current and estimate_token_count(candidate) > max_tokens:
                pieces.append("\n\n".join(current))
                current = [unit]
            else:
                current.append(unit)
    if current:
        pieces.append("\n\n".join(current))

    if not heading:
        return pieces
    return [f"{heading}\n\n{piece}" if piece else heading for piece in pieces]


def _paragraphs(lines: list[str]) -> list[str]:
    """按空行切段落，fenced code 内的空行不作为段落边界。"""
    paragraphs: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        if not line.strip() and not in_fence:
            if current:
                paragraphs.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        paragraphs.append("\n".join(current))
    return paragraphs


def _split_long_paragraph(paragraph: str, max_tokens: int) -> list[str]:
    """单个段落本身就超长（如一张巨大的参数表）时按行切，保证每片不超过上限。"""
    if estimate_token_count(paragraph) <= max_tokens:
        return [paragraph]
    units: list[str] = []
    current: list[str] = []
    for line in paragraph.splitlines():
        candidate = "\n".join([*current, line])
        if current and estimate_token_count(candidate) > max_tokens:
            units.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        units.append("\n".join(current))
    return units


__all__ = ["MarkdownChunk", "chunk_markdown"]
