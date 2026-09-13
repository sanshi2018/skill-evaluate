"""长时记忆库的数据契约（docs/dev/23 第 2 节）。

放在 `state/` 而不是 `memory/`：`persistence/repository.py` 需要按这个形状存取，而
`memory/` 又依赖 `persistence/`——契约下沉到最底层的 `state/` 才不会形成循环导入
（与 `state/generator_trust.py` 同一种分层）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MemoryCollection(StrEnum):
    """`search_documents.collection` 的四个取值（文档 23 第 2 节）。

    用枚举而不是散落的字符串字面量：集合名一旦拼错，写入与检索会各自落在两个互不相交的
    "集合"里，检索永远返回空，而且不会报任何错。
    """

    ASSERTION_TEMPLATES = "assertion_templates"  # Validator：工具箱模板 description + keywords
    SEED_ANCHORS = "seed_anchors"  # Generator：脱敏真实用户 Prompt
    SUCCESSFUL_SKILL_ARCHIVE = "successful_skill_archive"  # Generator 冷启动：全维度通过的范本
    OPTIMIZER_PATCH_HISTORY = "optimizer_patch_history"  # Optimizer：失败摘要 → 补丁 → 成败


class SearchDocument(BaseModel):
    """记忆库中的一条文档，也是 `HybridSearchService.search()` 的返回元素。"""

    doc_id: str  # 集合内唯一；同 doc_id 重复写入即 upsert（归档与同步都依赖这一幂等性）
    collection: str  # MemoryCollection 取值
    text: str  # 用于 BM25 索引、Reranker 打分与展示的原文
    metadata: dict[str, Any] = Field(default_factory=dict)
    # 写入时可选传入；未传则由服务内部调用 embedding 客户端计算。检索结果里恒为 None——
    # 1536 维向量对调用方没有用处，回传只会让日志与 Prompt 渲染意外地变得巨大。
    embedding: list[float] | None = None

    # ---- 以下两项只在检索结果上有值（文档 23 正文把分数塞进 metadata["rerank_score"]，
    # 实现改为独立字段：metadata 是入库时的业务数据，混入检索期分数会让"再写回去"时污染库）----
    # 归一化到 0~1 的相关度。有 Reranker 时 = Cross-Encoder 概率；没有时 = 该文档在稠密路的
    # 余弦相似度（只被 BM25 召回的文档用归一化的 ts_rank）。Validator 的模板命中阈值按它过滤。
    score: float | None = None
    # 各路原始信号，供日志排查"为什么召回了它"：dense_similarity / lexical_rank /
    # rerank_score / rrf_score。
    score_breakdown: dict[str, float] = Field(default_factory=dict)


class StoredSearchDocument(BaseModel):
    """写入存储层时的完整形态（服务层计算好 embedding / 词元串 / 内容哈希后交给仓储）。"""

    document: SearchDocument
    embedding: list[float]
    embedding_model: str
    lexical_text: str
    content_hash: str


class IndexStats(BaseModel):
    """一次批量索引 / 集合同步的结果统计（CLI 打印、日志记录）。"""

    collection: str
    indexed: int = 0  # 新写入或文本变化后重新 embed 的条数
    skipped: int = 0  # 文本与模型都没变、跳过 embed 的条数
    deleted: int = 0  # 集合同步时删除的、源里已经不存在的条数


# --------------------------------------------------------------------------- #
# RAG 归档与冷启动检索（docs/dev/23 第 3.3 节）
# --------------------------------------------------------------------------- #


class ArchiveOutcome(BaseModel):
    """`archive_successful_run()` 的结果。文档 23 正文返回 None，实现返回本对象（超集）：
    主图收尾节点需要把"为什么没归档"写进日志/报告，否则数据飞轮停转了也没人知道。"""

    run_id: str
    archived: bool
    # 未归档原因：memory_disabled / run_not_found / no_blocking_dimensions /
    # blocking_dimensions_not_passed / skill_not_found / no_suite_version
    reason: str = ""
    archive_key: str | None = None  # `<skill_id>@<skill_version_ref>`
    document_count: int = 0
    failed_dimensions: list[str] = Field(default_factory=list)


class ArchivedCase(BaseModel):
    """归档范本中的一条优质用例（冷启动 few-shot 的"用例设计模式"示例）。"""

    category: str  # TestCaseCategory 取值
    split: str  # 只会是 train / validation（cold 区的降级用例不归档）
    prompt: str
    expected_output: str | None = None


class ArchivedExample(BaseModel):
    """一份历史成功范本：Skill 画像 + 该次评测验证过的用例。"""

    archive_key: str
    skill_id: str
    skill_version_ref: str
    description: str
    # 与新 Skill 最相关的那段画像原文（description 本身或正文某一节），让模型知道"像在哪里"。
    matched_section: str
    score: float | None = None
    cases: list[ArchivedCase] = Field(default_factory=list)

    def cases_for(self, category: str) -> list[ArchivedCase]:
        """模板按当前出题类别取示例：正向模板只看正向范例，反向模板只看反向范例。"""
        return [case for case in self.cases if case.category == category]


# --------------------------------------------------------------------------- #
# Optimizer 修复经验（docs/dev/23 第 3.4 节）
# --------------------------------------------------------------------------- #


class PatchExperience(BaseModel):
    """一条历史修复经验：某类失败 → 当时出的补丁 → 是否通过回归。"""

    patch_id: str
    role: str
    skill_id: str
    patch_type: str
    target_path: str
    failure_summary: str
    diff: str  # 入库时已截断
    rationale: str
    # 是否通过回归。False 同样有价值：Prompt 里作为"过往失败尝试（避免重蹈覆辙）"展示。
    passed: bool
    # regression_passed / regression_failed / apply_failed
    outcome: str
    detail: str = ""
    score: float | None = None


__all__ = [
    "ArchiveOutcome",
    "ArchivedCase",
    "ArchivedExample",
    "IndexStats",
    "MemoryCollection",
    "PatchExperience",
    "SearchDocument",
    "StoredSearchDocument",
]
