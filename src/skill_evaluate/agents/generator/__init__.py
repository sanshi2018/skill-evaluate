"""Generator Agent：正向/反向测试用例生成与测试集生命周期管理。

实现文档：docs/dev/06_Generator_Agent与测试集生命周期管理.md。

- `agent.py`：生成本体，只产出 `TestCase`，不落库、不判定。
- `service.py`：测试集生命周期（REUSE / FORCE_REGENERATE / INCREMENTAL_PATCH）。
- `schema.py`：`GenerationRequest` / `CapabilityFocus` 统一生成指令。

后续模块（16/17 覆盖率补盲、19 跨模型抽样、20 组合矩阵、21 种子锚点）的接入点
见 docs/dev/interfaces/06_generator_extension_points.md。
"""

from skill_evaluate.agents.generator.agent import GeneratorAgent, extract_keywords
from skill_evaluate.agents.generator.schema import (
    CapabilityFocus,
    GeneratedCase,
    GeneratedCaseBatch,
    GenerationRequest,
)
from skill_evaluate.agents.generator.service import EnsureTestSuiteResult, TestSuiteService

__all__ = [
    "CapabilityFocus",
    "EnsureTestSuiteResult",
    "GeneratedCase",
    "GeneratedCaseBatch",
    "GenerationRequest",
    "GeneratorAgent",
    "TestSuiteService",
    "extract_keywords",
]
