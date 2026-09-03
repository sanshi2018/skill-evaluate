"""Skill 静态解析层（磁盘目录 -> `SkillDefinition`）。

由 docs/dev/06 先落最小实现、docs/dev/12 完善精度，见 `skill_loader.py` 顶部
的归属说明。所有需要"读取一份 SKILL.md"的模块统一
`from skill_evaluate.ingestion import load_skill`，不重复实现解析逻辑。

计数口径独立在 `token_counter.py`：`estimate_token_count()` 只给整数（够
Generator 做 Prompt 预算），要拿数字去卡线阻断的场景用 `count_tokens()`，它会
一并告诉你这个数字精不精确（docs/dev/12 第 3 节）。
"""

from skill_evaluate.ingestion.skill_loader import load_skill
from skill_evaluate.ingestion.token_counter import (
    TokenCount,
    TokenCounter,
    count_tokens,
    estimate_token_count,
    heuristic_token_count,
)

__all__ = [
    "TokenCount",
    "TokenCounter",
    "count_tokens",
    "estimate_token_count",
    "heuristic_token_count",
    "load_skill",
]
