"""Analyzer Agent 的 LLM 结构化输出契约（docs/dev/16 第 2 节）。

两个任务、两份契约，都只描述**模型该交回什么**，不承载业务状态——业务状态是
`state/capability.py` 的 `CapabilityTree`。刻意分成两层而不是让模型直接产出
`CapabilityNode`：`capability_id` 必须由代码按确定性哈希生成（见
`identity.py`），让模型自己编 id 会在第二次抽取时全部变掉；`tier`/`covered`
这些字段也不该出现在模型的视野里（前者是文档 18 的职责，后者是映射阶段的结论）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ExtractedCapability(BaseModel):
    """模型抽出的一条原子能力。"""

    description: str
    # 支撑这条能力的 SKILL.md 原文片段。要求模型逐字引用而不是复述，有两个作用：
    # 1. **抑制凭空发挥**——必须落到原文的一句话，模型就很难把"它大概也能做 X"
    #    写成一项声明能力，而模块六全部结论都建立在"声明了什么"之上；
    # 2. 人工审核卡片（第 4 节的挂起点）里，人要判断"这 20 多项是不是拆太细了"，
    #    看引文比看抽象描述快得多。
    #
    # 它**不进** `CapabilityNode`（那个模型的字段表由 docs/dev/02 定，本文档不
    # 擅自扩展），只写进结构化日志 `analyzer_capability_extracted` 供回查。
    evidence_quote: str


class CapabilityExtraction(BaseModel):
    """一次能力树抽取调用的完整输出。"""

    capabilities: list[ExtractedCapability] = Field(default_factory=list)


class TierAssignment(BaseModel):
    """模型给一项能力定的权重档位（模块八 / docs/dev/18 第 3 节）。

    `capability_id` 由调用方在 Prompt 里给定、模型原样回填，**不是**模型自己编的
    ——分级是"给已有节点打标签"，不是重新抽一棵树。回填不上号的条目会被调用方
    丢弃并记日志（与用例映射同一处理）。

    `tier` 用 Literal 而不是直接用 `CapabilityTier`：Schema 层是 LLM 的输出契约，
    模型返回 "P0"/"core" 这类没约定过的写法时，应当在 Pydantic 校验阶段就失败并
    触发重试，而不是被一个宽松的枚举构造悄悄接受或当场崩掉（与 docs/dev/15 的
    `SecuritySeverityRatingOutput.severity` 同一条理由）。
    """

    capability_id: str
    tier: Literal["p0_core", "p1_conditional", "p2_defensive"]
    # 为什么定这一档。不落库，但进结构化日志：分级会改变加权覆盖率这个最终分数，
    # 出现可疑分数时，这是唯一能回溯"模型当时怎么想的"的线索。
    reason: str = ""


class TierClassification(BaseModel):
    """一次权重分级调用的完整输出。"""

    assignments: list[TierAssignment] = Field(default_factory=list)


class ExtractedNegativeConstraint(BaseModel):
    """模型抽出的一条负向约束（"必须避免 / 不能 / 禁止"型规则）。"""

    description: str
    # 与 `ExtractedCapability.evidence_quote` 同样的作用与同样的硬要求：必须逐字
    # 摘自 SKILL.md。负向约束比能力更容易被"脑补"——模型很清楚"数据库查询一般
    # 要注意 SQL 注入"，但那是常识而不是**这份 Skill 声明过的**约束，把它当成
    # 追踪对象会逼着系统去为一条根本没写在文档里的规则出题。
    evidence_quote: str


class NegativeConstraintExtraction(BaseModel):
    """一次负向约束抽取调用的完整输出。"""

    constraints: list[ExtractedNegativeConstraint] = Field(default_factory=list)


class CaseCapabilityMapping(BaseModel):
    """一条用例激活了哪些能力（双向追溯矩阵的一格）。"""

    # 只允许出现在 Prompt 里给出的 id 清单中。模型仍可能编造，调用方会按能力树
    # 过滤一遍并记日志——不过滤的后果是覆盖率被一个不存在的 id 抬高。
    capability_ids: list[str] = Field(default_factory=list)
    # 为什么是这几项。不落库，但会进日志：映射结果决定了"哪些能力算已覆盖"，
    # 出现可疑的覆盖率时，这是唯一能回溯"模型当时怎么想的"的线索。
    reasoning: str = ""


__all__ = [
    "CapabilityExtraction",
    "CaseCapabilityMapping",
    "ExtractedCapability",
    "ExtractedNegativeConstraint",
    "NegativeConstraintExtraction",
    "TierAssignment",
    "TierClassification",
]
