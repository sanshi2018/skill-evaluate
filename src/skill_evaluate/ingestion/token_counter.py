"""Token 计数（docs/dev/12 第 3 节：替换 docs/dev/06 留下的粗估桩）。

## 为什么单独成一个模块

docs/dev/12 用 Token 数做**硬性卡线**（>5,000 即阻断合并），而
`docs/dev/interfaces/06_skill_loader_minimal.md` 明确警告过：原来那个字符粗估的
精度不足以支撑卡线判定。卡线是要阻断别人合并请求的动作，用什么口径数出来的这个
数字，必须是可查、可复现、可在报告里说清楚的一件事——所以计数口径从
`skill_loader` 里独立出来，带上"用的哪种计数器、精不精确"的元信息一起返回。

## 计数器的选取顺序

1. `tiktoken`（**可选依赖**，装了就用）：离线、确定性、无网络往返。用
   `o200k_base` 编码。**这是正常路径**。
2. 字符数 × 3/4 的兜底估算：只在拿不到 `tiktoken` 时启用。

## 为什么不用 Anthropic 官方 tokenizer

docs/dev/12 正文写的是"用与 `LLMSettings.provider` 匹配的官方 tokenizer **离线**
计算"。这两个要求在 Anthropic 这里同时满足不了：Claude 的官方计数入口
（`client.messages.count_tokens`）是一次**网络请求**，不是离线分词器，且本项目
的 LLM 出口统一走 OpenRouter（`config.LLMSettings`），并不持有 Anthropic 原生
凭证。把一次静态扫描做成需要联网、需要额外凭证、还会计费的操作，代价远大于它
换来的那点精度。

因此本模块的决策是：**离线优先，并如实标注精度**。`TokenCount.exact` 为 False
时，docs/dev/12 的卡线节点会在"限额附近的不确定带"内改判
`NEEDS_HUMAN_REVIEW` 而不是直接阻断（见 `nodes/context_scoping/static_scan.py`），
这样"计数不精确"这件事不会变成误伤开发者的阻断。

真要用官方计数：实现一个 `TokenCounter` 协议的类，注入
`ContextScopingDeps.token_counter` 即可，本模块无需修改。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Protocol

from pydantic import BaseModel

from skill_evaluate.logging import get_logger

logger = get_logger(component="token_counter")

# tiktoken 的通用编码。选 o200k_base 而不是 cl100k_base：前者是较新一代模型
# （GPT-4o 系及其后继）的编码，对中文与代码的切分密度更接近当代 Claude 模型，
# 用作跨厂商代理口径时偏差更小。
TIKTOKEN_ENCODING = "o200k_base"

# 兜底估算的字符→Token 系数：字符数的 3/4。
#
# 刻意**不**按中英文分别取系数：真实文本的密度取决于分词器怎么切，中英混排、代码
# 块、URL 各不相同，多分几档只是让公式看起来更讲究，并不能把误差压到可以用来卡线
# 的程度。既然结论都是"标注为不精确、限额附近不阻断"，就用一个所有人一眼能算的
# 系数，而不是一组需要解释的经验值。
_CHARS_PER_TOKEN_RATIO = 0.75


class TokenCount(BaseModel):
    """一次计数的结果**及其可信度**。

    `exact=False` 意味着这个数字是估算的：调用方不得拿它去做无宽容度的卡线判定
    （docs/dev/12 第 6 节的工程化决策依赖这个字段）。
    """

    value: int
    method: str  # 形如 "tiktoken:o200k_base" / "heuristic:chars-x0.75"，会写进报告
    exact: bool


class TokenCounter(Protocol):
    """计数器接口。做成 Protocol 而非基类：调用方注入一个可调用对象即可，
    不必从本模块继承——测试里替身、将来接官方 API 计数都走同一个口子。"""

    def __call__(self, text: str) -> TokenCount: ...


def count_tokens(text: str) -> TokenCount:
    """默认计数器：有 `tiktoken` 用 `tiktoken`，否则退化为"字符数 × 3/4"。"""
    encoding = _load_tiktoken_encoding()
    if encoding is not None:
        return TokenCount(
            value=len(encoding.encode(text)),
            method=f"tiktoken:{TIKTOKEN_ENCODING}",
            exact=True,
        )
    return heuristic_token_count(text)


def heuristic_token_count(text: str) -> TokenCount:
    """兜底估算：字符数 × 3/4。**不做卡线判定的唯一依据**，见模块头注释。"""
    return TokenCount(
        value=int(len(text) * _CHARS_PER_TOKEN_RATIO),
        method="heuristic:chars-x0.75",
        exact=False,
    )


def estimate_token_count(text: str) -> int:
    """只要数字、不关心精度的调用方用这个（Generator 的 Prompt 预算判断等）。

    保留这个签名是为了不惊动 docs/dev/06 已有的调用点
    （`docs/dev/interfaces/06_skill_loader_minimal.md` 的接入约定是"替换函数体、
    保持签名"）。需要判断精度的场景请改用 `count_tokens()`。
    """
    return count_tokens(text).value


@lru_cache(maxsize=1)
def _load_tiktoken_encoding() -> Any | None:
    """加载并缓存编码表。

    缓存的理由不只是速度：`tiktoken` 首次取编码可能触发一次 BPE 词表下载，
    一次静态扫描里对正文 + 每个参考文件各调一次计数，不缓存就是每份文件都付一次
    这个代价。取不到时**只告警不抛错**——计数器降级不该让整个评测跑不起来。
    """
    try:
        import tiktoken
    except ImportError:
        logger.info(
            "token_counter_fallback_to_heuristic",
            reason="tiktoken_not_installed",
            hint="pip install tiktoken 可获得离线精确计数；未装时按字符数 × 3/4 估算",
        )
        return None

    try:
        return tiktoken.get_encoding(TIKTOKEN_ENCODING)
    except Exception as exc:  # noqa: BLE001 - 第三方实现的异常类型不在我们的体系内
        # 典型成因是离线环境拉不到词表文件。降级而不是失败：这条路径上唯一的
        # 损失是精度，而精度损失已经由 TokenCount.exact 如实标注给下游了。
        logger.warning(
            "token_counter_fallback_to_heuristic",
            reason="tiktoken_encoding_unavailable",
            error=str(exc),
        )
        return None


__all__ = [
    "TIKTOKEN_ENCODING",
    "TokenCount",
    "TokenCounter",
    "count_tokens",
    "estimate_token_count",
    "heuristic_token_count",
]
