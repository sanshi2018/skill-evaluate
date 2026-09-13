"""真实分布对齐：种子锚点库与检索（docs/dev/21 第 3 节，升级 docs/dev/06 的简化版）。

## 仓库结构（外部独立仓库 `skill-evaluate-seed-anchors`，由运维侧初始化）

```
manifest.yaml              # 领域清单：domain_tag / description / source（脱敏审计记录）
anchors/<domain_tag>.yaml  # 该领域的脱敏真实 Prompt 记录列表：id / prompt
CHANGELOG.md
```

```yaml
# manifest.yaml
- domain_tag: data_processing
  description: 表格、CSV、数据清洗类的真实用户提问
  source: 2026-08 工单日志抽样，已脱敏（审计单 SEC-1234）

# anchors/data_processing.yaml
- id: dp-001
  prompt: 这个导出的表里好多重复行 帮我去一下 顺便把日期统一成年月日
```

同步机制照抄 docs/dev/10 断言工具箱（`agents/git_repo_cache.py`）：锁定 ref、浅克隆、记录
commit sha；**不在检索时隐式同步**（CLI `sync-seed-anchors` 在评测前单独执行）。

## 检索：混合检索优先，单一 embedding 相似度兜底（docs/dev/23 第 3.2 节）

`resolve_for_skill` 的签名即契约，调用方（`GeneratorAgent`）不改：

1. 记忆库可用且 `search_documents(collection="seed_anchors")` 里有**当前本地库 commit** 的索引 →
   Dense + BM25 + Reranker 混合检索；索引由 `sync-seed-anchors` / `memory-index` 写入。
2. 否则（记忆库关闭、索引未建或属于旧 commit、数据库/检索故障）→ docs/dev/21 的原实现：按
   `(commit_sha, embedding_model)` 在进程内缓存锚点向量、单一余弦相似度排序。

锚点不进 `case_embeddings`：该表 `case_id` 外键指向 `test_cases`，锚点不是测试用例。

## 可追溯性

`SeedAnchor.ref` = `<domain_tag>/<id>@<commit_sha>`。模型为每条用例回填它主要借鉴的锚点 id，
`GeneratorAgent` 核对后写进 `TestCase.seed_anchor_id`（docs/dev/02 早已留好该字段）——事后能
回答"这条题是哪条真实数据启发的、那条数据属于种子库的哪个版本"。

## 种子库不可用不是错误

没配仓库、没同步、manifest 缺失、embedding 通道故障，一律返回空列表并记日志：种子锚点是
"让题更像真人"的增强手段，不是出题的前置条件（与断言工具箱同一取舍）。结构非法（manifest
声明的锚点文件不存在）则显式报错——静默降级会让所有出题悄悄失去真实分布锚定。
"""

from __future__ import annotations

import asyncio
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from skill_evaluate.agents.embedding import (
    EmbeddingClient,
    OpenRouterEmbeddingClient,
    dot,
    normalize,
)
from skill_evaluate.agents.git_repo_cache import (
    RecordListParseError,
    git_head_sha,
    git_sync,
    parse_record_list,
)
from skill_evaluate.config import GeneratorTrustSettings, get_settings
from skill_evaluate.errors import AgentError
from skill_evaluate.logging import get_logger
from skill_evaluate.memory.hybrid_search import (
    HybridSearchService,
    get_default_hybrid_search,
    memory_enabled,
)
from skill_evaluate.state.memory import MemoryCollection
from skill_evaluate.state.skill import SkillDefinition

logger = get_logger(component="seed_anchors")

MANIFEST_FILENAME = "manifest.yaml"
ANCHORS_DIRNAME = "anchors"


class SeedAnchorLibraryError(AgentError):
    """种子库结构非法（manifest 解析失败、声明的锚点文件缺失、记录缺字段）。"""


class SeedAnchor(BaseModel):
    """一条脱敏真实 Prompt 锚点。"""

    anchor_id: str  # `<domain_tag>/<id>`，库内唯一；Prompt 里给模型看、模型回填的就是它
    domain_tag: str
    prompt: str
    commit_sha: str = "unknown"  # 种子库当时的 commit，保证溯源到具体版本

    @property
    def ref(self) -> str:
        """写进 `TestCase.seed_anchor_id` 的完整引用：`<anchor_id>@<commit_sha>`。"""
        return f"{self.anchor_id}@{self.commit_sha}"


class SeedAnchorLibrary:
    """本地缓存的种子锚点仓库视图。构造不做 IO，首次访问时惰性加载。"""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        repo_url: str | None = None,
        ref: str | None = None,
    ) -> None:
        settings = get_settings().generator_trust
        self._root = Path(root or settings.seed_cache_dir).expanduser()
        self._repo_url = repo_url if repo_url is not None else settings.seed_repo_url
        self._ref = ref or settings.seed_repo_ref
        self._anchors: list[SeedAnchor] | None = None
        self._commit_sha: str | None = None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def available(self) -> bool:
        return (self._root / MANIFEST_FILENAME).is_file()

    def commit_sha(self) -> str:
        if self._commit_sha is None:
            self._commit_sha = git_head_sha(self._root)
        return self._commit_sha

    def anchors(self) -> list[SeedAnchor]:
        """全部锚点（按 manifest 声明顺序）。库不可用时返回空列表。"""
        if self._anchors is None:
            self._anchors = self._load() if self.available else []
        return self._anchors

    def get_by_ids(self, anchor_ids: list[str]) -> list[SeedAnchor]:
        """按 id 精确取锚点（调用方显式指定 `GenerationRequest.seed_anchor_ids` 时用）。"""
        index = {anchor.anchor_id: anchor for anchor in self.anchors()}
        found = [index[anchor_id] for anchor_id in anchor_ids if anchor_id in index]
        unknown = [anchor_id for anchor_id in anchor_ids if anchor_id not in index]
        if unknown:
            logger.warning("seed_anchor_ids_unknown", unknown=unknown, available=self.available)
        return found

    def _load(self) -> list[SeedAnchor]:
        sha = self.commit_sha()
        try:
            domains = parse_record_list(
                (self._root / MANIFEST_FILENAME).read_text(encoding="utf-8"),
                source_name=MANIFEST_FILENAME,
            )
        except RecordListParseError as exc:
            raise SeedAnchorLibraryError(str(exc)) from exc

        anchors: list[SeedAnchor] = []
        for domain in domains:
            domain_tag = str(domain.get("domain_tag", "")).strip()
            if not domain_tag:
                raise SeedAnchorLibraryError(
                    f"{MANIFEST_FILENAME} 存在缺少 domain_tag 的记录：{domain!r}"
                )
            path = self._root / ANCHORS_DIRNAME / f"{domain_tag}.yaml"
            if not path.is_file():
                raise SeedAnchorLibraryError(f"manifest 声明的锚点文件不存在：{path}")
            try:
                records = parse_record_list(
                    path.read_text(encoding="utf-8"), source_name=str(path.name)
                )
            except RecordListParseError as exc:
                raise SeedAnchorLibraryError(str(exc)) from exc
            for record in records:
                local_id = str(record.get("id", "")).strip()
                prompt = str(record.get("prompt", "")).strip()
                if not local_id or not prompt:
                    raise SeedAnchorLibraryError(
                        f"{path.name} 存在缺少 id/prompt 的记录：{record!r}"
                    )
                anchors.append(
                    SeedAnchor(
                        anchor_id=f"{domain_tag}/{local_id}",
                        domain_tag=domain_tag,
                        prompt=prompt,
                        commit_sha=sha,
                    )
                )
        return anchors

    async def sync(self) -> bool:
        """同步种子库到本地缓存。失败降级为"不可用"，不中断评测（同 docs/dev/10）。"""
        if not self._repo_url:
            logger.info("seed_anchor_sync_skipped", reason="seed_repo_url 未配置")
            return False
        try:
            await asyncio.to_thread(git_sync, self._repo_url, self._ref, self._root)
        except (subprocess.CalledProcessError, OSError) as exc:
            logger.warning("seed_anchor_sync_failed", repo=self._repo_url, error=str(exc)[:500])
            return False
        self._anchors = None
        self._commit_sha = None
        logger.info(
            "seed_anchor_synced",
            repo=self._repo_url,
            ref=self._ref,
            commit=self.commit_sha(),
            anchors=len(self.anchors()),
        )
        return True


class SeedAnchorSource(Protocol):
    """`GeneratorAgent` 依赖的最小协议（`SeedAnchorResolver` 实现它；测试注入替身）。"""

    def resolve_ids(self, anchor_ids: list[str]) -> list[SeedAnchor]: ...

    async def resolve_for_skill(self, skill: SkillDefinition, count: int) -> list[SeedAnchor]: ...


class SeedAnchorResolver:
    """按 Skill 的 description 检索最相关的真实锚点（与反坍塌检测共用 embedding 客户端）。"""

    def __init__(
        self,
        *,
        library: SeedAnchorLibrary | None = None,
        embedding_client: EmbeddingClient | None = None,
        settings: GeneratorTrustSettings | None = None,
        search_service: HybridSearchService | None = None,
    ) -> None:
        self._settings = settings or get_settings().generator_trust
        self._library = library or SeedAnchorLibrary()
        self._embedding_client = embedding_client or OpenRouterEmbeddingClient()
        # docs/dev/23：混合检索服务。None = 按 `SKILLEVAL_MEMORY_ENABLED` 决定是否用默认实例；
        # 显式注入则总是先走混合检索。
        self._search_service = search_service
        # (commit_sha, embedding_model) -> 与 anchors() 同序的归一化向量。锚点库在一次进程
        # 生命周期里通常不变，每次出题都把几百条锚点重新 embed 一遍是纯浪费。
        self._vector_cache: dict[tuple[str, str], list[list[float]]] = {}

    @property
    def library(self) -> SeedAnchorLibrary:
        return self._library

    def resolve_ids(self, anchor_ids: list[str]) -> list[SeedAnchor]:
        return self._library.get_by_ids(anchor_ids)

    async def resolve_for_skill(self, skill: SkillDefinition, count: int) -> list[SeedAnchor]:
        """最相关的 `count` 条锚点（docs/dev/21 第 3.1 节 `_resolve_seed_anchors` 的落点）。

        docs/dev/23 已升级：先混合检索，拿不到结果再回落到进程内单一 embedding 相似度（见模块头）。
        """
        if count <= 0 or not self._library.available:
            return []
        hybrid = await self._resolve_via_hybrid_search(skill, count)
        if hybrid:
            return hybrid
        return await self._resolve_via_embedding(skill, count)

    async def _resolve_via_hybrid_search(
        self, skill: SkillDefinition, count: int
    ) -> list[SeedAnchor]:
        """混合检索路径。返回空列表表示"这条路走不通"，由调用方回落，而不是"没有相关锚点"。

        按当前本地库的 `commit_sha` 过滤：索引若还停在旧 commit，命中的锚点原文可能已被修改，
        写进用例的 `<anchor_id>@<commit>` 溯源就与注入 Prompt 的原文对不上了——宁可回落重算。
        命中结果再按 anchor_id 映射回本地库对象，保证 Prompt 里的原文一定来自当前版本。
        只要集合里有该 commit 的文档，稠密路总会返回最近邻，因此"零命中"只可能意味着没建索引。
        """
        search = self._search_service or (get_default_hybrid_search() if memory_enabled() else None)
        if search is None:
            return []
        try:
            anchors = self._library.anchors()
            hits = await search.search(
                _skill_query_text(skill),
                MemoryCollection.SEED_ANCHORS,
                top_k=count,
                metadata_filter={"commit_sha": self._library.commit_sha()},
            )
        except SeedAnchorLibraryError:
            raise
        except Exception as exc:  # noqa: BLE001 - 回落到单一 embedding 路径，不阻断出题
            logger.warning(
                "seed_anchor_hybrid_search_degraded", skill_id=skill.skill_id, error=str(exc)[:300]
            )
            return []
        by_id = {anchor.anchor_id: anchor for anchor in anchors}
        selected = [by_id[hit.doc_id] for hit in hits if hit.doc_id in by_id][:count]
        if selected:
            logger.info(
                "seed_anchors_resolved",
                skill_id=skill.skill_id,
                anchor_ids=[anchor.anchor_id for anchor in selected],
                commit=self._library.commit_sha(),
                method="hybrid_search",
            )
        return selected

    async def _resolve_via_embedding(self, skill: SkillDefinition, count: int) -> list[SeedAnchor]:
        """docs/dev/21 的原实现：进程内缓存锚点向量 + 单一余弦相似度。"""
        try:
            anchors = self._library.anchors()
            if not anchors:
                return []
            anchor_vectors = await self._anchor_vectors(anchors)
            [query] = await self._embedding_client.embed([_skill_query_text(skill)])
        except SeedAnchorLibraryError:
            raise
        except Exception as exc:  # noqa: BLE001 - 增强手段：embedding 通道故障不得阻断出题
            logger.warning(
                "seed_anchor_resolve_failed", skill_id=skill.skill_id, error=str(exc)[:500]
            )
            return []

        query_vec = normalize(query)
        ranked = sorted(
            zip(anchors, anchor_vectors, strict=True),
            key=lambda pair: (-dot(query_vec, pair[1]), pair[0].anchor_id),
        )
        selected = [anchor for anchor, _ in ranked[:count]]
        logger.info(
            "seed_anchors_resolved",
            skill_id=skill.skill_id,
            anchor_ids=[anchor.anchor_id for anchor in selected],
            commit=self._library.commit_sha(),
            method="embedding",
        )
        return selected

    async def _anchor_vectors(self, anchors: list[SeedAnchor]) -> list[list[float]]:
        key = (self._library.commit_sha(), self._settings.embedding_model)
        cached = self._vector_cache.get(key)
        if cached is None or len(cached) != len(anchors):
            raw = await self._embedding_client.embed([anchor.prompt for anchor in anchors])
            cached = [normalize(vec) for vec in raw]
            self._vector_cache[key] = cached
        return cached


def _skill_query_text(skill: SkillDefinition) -> str:
    """检索查询文本：description 为主。

    不拼正文：正文里大量的步骤与格式说明会把查询向量拉向"文档写作风格"，而锚点是用户口吻的
    短句，真正决定"用户会怎么提这类需求"的是 description 声明的业务领域。
    """
    return skill.description


@lru_cache
def get_default_seed_anchor_resolver() -> SeedAnchorResolver:
    """进程内共享的默认解析器（锚点清单与锚点向量只加载/计算一次）。"""
    return SeedAnchorResolver()


__all__ = [
    "ANCHORS_DIRNAME",
    "MANIFEST_FILENAME",
    "SeedAnchor",
    "SeedAnchorLibrary",
    "SeedAnchorLibraryError",
    "SeedAnchorResolver",
    "SeedAnchorSource",
    "get_default_seed_anchor_resolver",
]
