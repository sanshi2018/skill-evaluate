"""把外部仓库（断言工具箱、种子锚点库）索引进记忆库（docs/dev/23 第 3.1、3.2 节）。

两个集合都以**本地缓存的外部仓库为唯一真相**，因此走 `HybridSearchService.sync_collection()`
（全量同步：新增/变更 upsert，仓库里删掉的条目同步删除）。

调用时机：`skill-evaluate sync-toolbox` / `sync-seed-anchors` 在 git 同步成功后自动调用；
`skill-evaluate memory-index` 不拉仓库、只按本地缓存重建索引（数据库重建后、或无网络环境）。
**不在检索时隐式索引**：与"不在评测中途拉仓库"同一条理由——评测用的是哪一版记忆必须确定。

仓库不可用（没同步过）时**不清空**已有索引，返回 None：某台机器上缓存目录不存在，不代表
仓库被删空了，把共享库里的索引清掉会影响所有其他评测。
"""

from __future__ import annotations

from skill_evaluate.agents.generator.seed_anchors import SeedAnchorLibrary
from skill_evaluate.agents.validator.toolbox import AssertionToolbox
from skill_evaluate.logging import get_logger
from skill_evaluate.memory.hybrid_search import HybridSearchService
from skill_evaluate.state.memory import IndexStats, MemoryCollection, SearchDocument

logger = get_logger(component="memory_indexers")


def build_assertion_template_documents(toolbox: AssertionToolbox) -> list[SearchDocument]:
    """工具箱 manifest 每条模板 → 一条文档，检索文本 = `description + keywords`（文档 23 第 3.1 节）。

    doc_id 用模板文件名（manifest 内唯一），检索命中后 `AssertionToolbox._semantic_lookup()` 按它
    映射回**当前** manifest 里的 `TemplateMetadata`——索引里残留的旧模板对不上号会被丢弃。
    """
    commit = toolbox.commit_sha()
    documents = []
    for meta in toolbox.manifest():
        keywords = "、".join(meta.keywords)
        text = meta.description if not keywords else f"{meta.description}\n关键词：{keywords}"
        documents.append(
            SearchDocument(
                doc_id=meta.template,
                collection=MemoryCollection.ASSERTION_TEMPLATES.value,
                text=text,
                metadata={
                    "template": meta.template,
                    "params": list(meta.params),
                    "language": meta.language,
                    "commit_sha": commit,
                },
            )
        )
    return documents


def build_seed_anchor_documents(library: SeedAnchorLibrary) -> list[SearchDocument]:
    """种子库每条锚点 → 一条文档，检索文本 = 真实用户 Prompt 原文。

    `commit_sha` 进元数据：`SeedAnchorResolver` 检索时按当前本地库的 commit 过滤，保证写进
    `TestCase.seed_anchor_id` 的 `<anchor_id>@<commit_sha>` 引用与注入 Prompt 的原文是同一版。
    """
    return [
        SearchDocument(
            doc_id=anchor.anchor_id,
            collection=MemoryCollection.SEED_ANCHORS.value,
            text=anchor.prompt,
            metadata={
                "anchor_id": anchor.anchor_id,
                "domain_tag": anchor.domain_tag,
                "prompt": anchor.prompt,
                "commit_sha": anchor.commit_sha,
            },
        )
        for anchor in library.anchors()
    ]


async def sync_assertion_template_index(
    toolbox: AssertionToolbox, search: HybridSearchService
) -> IndexStats | None:
    if not toolbox.available:
        logger.info(
            "memory_index_skipped", collection="assertion_templates", reason="toolbox_unavailable"
        )
        return None
    return await search.sync_collection(
        MemoryCollection.ASSERTION_TEMPLATES, build_assertion_template_documents(toolbox)
    )


async def sync_seed_anchor_index(
    library: SeedAnchorLibrary, search: HybridSearchService
) -> IndexStats | None:
    if not library.available:
        logger.info("memory_index_skipped", collection="seed_anchors", reason="library_unavailable")
        return None
    return await search.sync_collection(
        MemoryCollection.SEED_ANCHORS, build_seed_anchor_documents(library)
    )


__all__ = [
    "build_assertion_template_documents",
    "build_seed_anchor_documents",
    "sync_assertion_template_index",
    "sync_seed_anchor_index",
]
