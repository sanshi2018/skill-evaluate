"""Analyzer Agent：SKILL.md 能力声明解析与原子能力树提取。

实现文档：docs/dev/16_模块六_能力覆盖率与测试完备性评测.md 第 2 节。

- `identity.py`：`capability_id` / `capability_tree_id` 的确定性生成与解析。
  **这是本包最要紧的一段**——id 一旦不稳定，`TestCase.target_capability_ids`
  里全部历史绑定会在下一次抽取时集体失效，而失效的表现是"覆盖率莫名其妙掉了"，
  没有任何报错。
- `schema.py`：两个任务的 LLM 结构化输出契约。
- `prompts/`：两份 Jinja 模板。
- `service.py`：`AnalyzerAgent` 本体。

**本包只做结构化抽取，不做判定**：覆盖率是否达标由
`nodes/coverage/rules.py::capability_coverage_threshold` 经 `JudgeAgent` 产出
（docs/dev/interfaces/08 第 0 节铁律在那一步兑现）。

留给后续模块的扩展点见 docs/dev/interfaces/16_capability_coverage.md：文档 18
的权重分级与反事实约束抽取、文档 17 的组合矩阵，都在这里加方法，不另起 Agent。
"""

from skill_evaluate.agents.analyzer.identity import (
    CAPABILITY_ID_INFIX,
    build_capability_id,
    build_capability_tree_id,
    normalize_capability_text,
    parse_capability_tree_id,
)
from skill_evaluate.agents.analyzer.schema import (
    CapabilityExtraction,
    CaseCapabilityMapping,
    ExtractedCapability,
)
from skill_evaluate.agents.analyzer.service import PLACEHOLDER_TIER, AnalyzerAgent

__all__ = [
    "CAPABILITY_ID_INFIX",
    "PLACEHOLDER_TIER",
    "AnalyzerAgent",
    "CapabilityExtraction",
    "CaseCapabilityMapping",
    "ExtractedCapability",
    "build_capability_id",
    "build_capability_tree_id",
    "normalize_capability_text",
    "parse_capability_tree_id",
]
