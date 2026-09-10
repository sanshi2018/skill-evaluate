"""模块七节点的依赖容器（docs/dev/17）。

## 为什么继承 `CoverageDeps` 而不是另起一个

`docs/dev/interfaces/16` 第 4 节要求模块七/八**复用同一份依赖**：三份文档读同一棵
能力树、共用同一个 `AnalyzerAgent`。各自 `CoverageDeps()` 会让两个 Agent 实例各自
持有不同的 `trace_handle`，Langfuse 上就会出现两条彼此无关的调用线，而它们本该是
同一次评测里的同一个分析基座。

继承 + `from_coverage()` 两条路同时提供：

- 单独跑模块七（本地调试、集成测试）时直接 `PruningDeps()`；
- 装配主图时用 `PruningDeps.from_coverage(coverage_pipeline.deps)`，把模块六已经
  构造好的 Agent / 仓储实例原样接过来。

模块七比模块六少用两样东西，这是它的职责边界：

- **不用 `AnalyzerAgent`**（虽然继承下来了）：冗余度是"能力集合完全相等"这一精确
  的集合关系，孤儿是"绑定 id 不在树上"这一精确的差集，都不需要语义理解。
  docs/dev/17 第 4.1 节把这条写得很直白——为这种低风险决策消耗 LLM 成本，换来的
  是一个不可复现的瘦身结果。
- **不用 `JudgeAgent`**：本维度不产出通过/失败结论（`status` 恒为 PASS，见
  `nodes.py::finalize_pruning_report`），因此 docs/dev/interfaces/08 第 0 节
  "凡是通过/失败的结论一律经过 JudgeAgent"这条铁律在这里没有适用对象。硬造一条
  "测试集健康度阈值"规则只会凭空多出一道谁也说不清该定在多少的门禁。

多出来的一样是 `suggestion_repository`：孤儿用例的非阻塞待办队列。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields

from skill_evaluate.nodes.coverage.deps import CoverageDeps
from skill_evaluate.persistence.repository import TestCaseSuggestionRepository

# 组合矩阵缺口反向补题时写进 `TestSuiteVersion.triggered_by` 的审计值。
#
# 与模块六的 `coverage_gap` **分开**：两者都是模块七/六触发的定向补题，但排查
# "测试集为什么突然多了 5 条题"时，"单项能力零覆盖"和"两项能力没有被同一条题
# 同时用到"是两个完全不同的原因，值得在审计字段上分得开。
# `combinatorial_gap` 这个取值在 docs/dev/interfaces/06 第 2 节的表里已经存在
# （原先只登记给模块十），本文档是它的首个真实生产者，接口文档已同步。
TRIGGERED_BY_COMBINATORIAL_GAP = "combinatorial_gap"


@dataclass(slots=True)
class PruningDeps(CoverageDeps):
    """模块七全部外部依赖的注入点。

    继承 `CoverageDeps` 的全部字段（Skill/用例/能力树仓储、Generator、报告器、
    `CoverageSettings`），追加一个建议队列仓储。
    """

    suggestion_repository: TestCaseSuggestionRepository = field(
        default_factory=TestCaseSuggestionRepository
    )

    @classmethod
    def from_coverage(cls, deps: CoverageDeps) -> PruningDeps:
        """把模块六已经构造好的依赖原样接过来，只补上本维度多出的那一个。

        逐字段搬运而不是 `copy.replace()`：`CoverageDeps` 的几个 Agent 字段是**惰性
        构造**的（`analyzer()` / `generator()` 首次调用时才实例化并回填），逐字段搬
        运会把"已经构造好的那个实例"和"还是 None"两种状态都如实带过来——共享的是
        实例本身，而不是"各自再造一个"。

        已经是 `PruningDeps` 时原样返回：主图重复调用不该悄悄换掉建议队列仓储。
        """
        if isinstance(deps, cls):
            return deps
        return cls(**{f.name: getattr(deps, f.name) for f in fields(CoverageDeps)})


__all__ = ["TRIGGERED_BY_COMBINATORIAL_GAP", "PruningDeps"]
