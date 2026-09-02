"""Skill 静态解析层（磁盘目录 -> `SkillDefinition`）。

由 docs/dev/06 先落最小实现、docs/dev/12 完善精度，见 `skill_loader.py` 顶部
的归属说明。所有需要"读取一份 SKILL.md"的模块统一
`from skill_evaluate.ingestion import load_skill`，不重复实现解析逻辑。
"""

from skill_evaluate.ingestion.skill_loader import estimate_token_count, load_skill

__all__ = ["estimate_token_count", "load_skill"]
