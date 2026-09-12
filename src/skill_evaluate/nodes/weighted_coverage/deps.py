"""模块八节点的依赖容器（docs/dev/18）。

## 为什么继承 `CoverageDeps`

`docs/dev/interfaces/16` 第 4 节要求模块七/八**复用同一份依赖**：三份文档读同一棵
能力树、共用同一个 `AnalyzerAgent`。各自 `CoverageDeps()` 会让两个 Agent 实例各自
持有不同的 `trace_handle`，Langfuse 上就会出现两条彼此无关的调用线，而它们本该是
同一次评测里的同一个分析基座。

继承 + `from_coverage()` 两条路同时提供（与模块七 `PruningDeps` 同一写法）：

- 单独跑模块八（本地调试、集成测试）时直接 `WeightedCoverageDeps()`；
- 装配主图时用 `WeightedCoverageDeps.from_coverage(pipeline.deps)`，把模块六/七
  已经构造好的 Agent / 仓储实例原样接过来（传 `PruningDeps` 也可以——它是
  `CoverageDeps` 的子类，多出来的建议队列仓储本维度用不上，按 `CoverageDeps` 的
  字段表搬运即可）。

## 本维度用到而模块七用不到的两样

- **`AnalyzerAgent`**：模块七刻意不用它（冗余/孤儿都是精确的集合运算）；本维度
  反过来，权重分级与负向约束抽取都是语义理解，只能交给模型。
- **`JudgeAgent` 的两个方法都用**：`quantitative_verdict()` 出加权覆盖率判定
  （纯算术），`judgmental_verdict()` 出"这条用例算不算诱导了这条约束"的裁量判定
  （`Criticality.ROUTINE`，docs/dev/18 第 4.1 节）。这是覆盖率三份文档里唯一一个
  会走裁量路径的维度。
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from skill_evaluate.nodes.coverage.deps import CoverageDeps

# 负向约束盲区反向补题时写进 `TestSuiteVersion.triggered_by` 的审计值。
#
# 与 `coverage_gap`（单项能力零覆盖）、`combinatorial_gap`（两项能力没被同一条题
# 同时用到）**分开**：排查"测试集为什么突然多了 3 条题"时，"某条禁令没有反事实
# 用例去诱导"是一个完全不同的原因，值得在审计字段上分得开。取值已同步进
# docs/dev/interfaces/06 第 2 节的取值表。
TRIGGERED_BY_NEGATIVE_CONSTRAINT_GAP = "negative_constraint_gap"


@dataclass(slots=True)
class WeightedCoverageDeps(CoverageDeps):
    """模块八全部外部依赖的注入点。

    字段全部继承自 `CoverageDeps`（Analyzer、Judge、Generator、四个仓储、报告器、
    `CoverageSettings`），本维度没有新增外部依赖——它产出的制品直接写文件系统，
    不经仓储层（见 `artifact.py` 的说明）。

    仍然单独定义一个子类而不是直接用 `CoverageDeps`，有两个理由：类型签名上
    "这个节点吃的是模块八的依赖"是显式的；将来本维度真需要加一样东西时，不必去
    动模块六的依赖容器（那会连带影响模块七）。
    """

    @classmethod
    def from_coverage(cls, deps: CoverageDeps) -> WeightedCoverageDeps:
        """把上游已经构造好的依赖原样接过来。

        逐字段搬运而不是 `copy.replace()`：`CoverageDeps` 的几个 Agent 字段是**惰性
        构造**的（`analyzer()` / `judge()` / `generator()` 首次调用时才实例化并
        回填），逐字段搬运会把"已经构造好的那个实例"和"还是 None"两种状态都如实
        带过来——共享的是实例本身，而不是"各自再造一个"。

        已经是 `WeightedCoverageDeps` 时原样返回：主图重复调用不该悄悄换掉实例。
        """
        if isinstance(deps, cls):
            return deps
        return cls(**{f.name: getattr(deps, f.name) for f in fields(CoverageDeps)})


__all__ = ["TRIGGERED_BY_NEGATIVE_CONSTRAINT_GAP", "WeightedCoverageDeps"]
