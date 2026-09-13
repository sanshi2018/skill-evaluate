"""Cross-Encoder 重排（docs/dev/23 第 2.2 节）。

## 为什么需要重排

稠密检索的双塔向量是"查询与文档各自独立编码"，擅长同义改写（"数据清理" ≈ "数据清洗"）
却容易把"主题相近但并不回答这个问题"的文档排在前面；BM25 只认词面。两路合并后的候选
交给 Cross-Encoder **把查询与文档拼在一起**逐对打分，才是真正读过两者再判断相关性，这是
架构文档"召回精度达到工业级天花板"的那一环。

## 为什么是可选依赖

`sentence-transformers` 会连带拉入 torch（数百 MB 起步）。只跑静态维度的 CI 作业、离线开发机
不该被迫安装。因此做成 `pyproject` 的 `reranker` 可选依赖（与 docs/dev/12 的 `tiktoken` 同一
取舍）：没装或模型加载失败时 `available=False`，`HybridSearchService` 退化为 RRF 融合排序并
**如实**在 `score_breakdown` 里不出现 `rerank_score`——不伪装成重排过。

模型本地加载、不走网络 API（首次加载时会从 Hugging Face 下载权重到本地缓存，之后离线可用）；
推理是同步 CPU/GPU 计算，由调用方经 `asyncio.to_thread` 放到线程池，避免阻塞事件循环。
"""

from __future__ import annotations

import math
import threading
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from skill_evaluate.config import MemorySettings, get_settings
from skill_evaluate.logging import get_logger
from skill_evaluate.state.memory import SearchDocument

logger = get_logger(component="memory_reranker")


@runtime_checkable
class Reranker(Protocol):
    """重排协议：对 `(query, text)` 逐对打分。测试注入确定性替身。"""

    @property
    def available(self) -> bool: ...

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        """返回与 `texts` 等长、同序、归一化到 0~1 的相关度。不可用时抛 `RerankerUnavailableError`。"""
        ...


class RerankerUnavailableError(RuntimeError):
    """Reranker 未安装 / 已禁用 / 模型加载失败。调用方据此退化为无重排排序。"""


class CrossEncoderReranker:
    """基于 `sentence_transformers.CrossEncoder` 的本地重排器。

    构造不加载模型（惰性到首次 `score()`）：流水线里绝大多数节点根本不检索，不该为此在进程
    启动时就付出几秒的模型加载时间。加载失败的结果会被**记住**，不在每次检索时反复重试加载
    （那会让每次检索都多出一次注定失败的几秒延迟）。
    """

    def __init__(
        self,
        model_name: str | None = None,
        *,
        settings: MemorySettings | None = None,
    ) -> None:
        self._settings = settings or get_settings().memory
        self._model_name = model_name or self._settings.reranker_model
        self._model: Any | None = None
        self._load_error: str | None = None if self._settings.reranker_enabled else "disabled"
        # 多个协程经 to_thread 并发首次调用时，只允许加载一次模型。
        self._lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def available(self) -> bool:
        """是否可用。会触发一次惰性加载（结果被缓存）。"""
        return self._ensure_model() is not None

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        model = self._ensure_model()
        if model is None:
            raise RerankerUnavailableError(self._load_error or "reranker unavailable")
        if not texts:
            return []
        raw = model.predict([(query, text) for text in texts], show_progress_bar=False)
        return _to_probabilities([float(value) for value in raw])

    def rerank(
        self, query: str, candidates: list[SearchDocument], top_k: int
    ) -> list[SearchDocument]:
        """文档 23 第 2.2 节的接口形态：按重排分数降序取前 `top_k`，分数写入 `score`。"""
        scores = self.score(query, [doc.text for doc in candidates])
        ranked = sorted(
            zip(candidates, scores, strict=True), key=lambda pair: (-pair[1], pair[0].doc_id)
        )
        return [
            doc.model_copy(
                update={
                    "score": value,
                    "score_breakdown": {**doc.score_breakdown, "rerank_score": value},
                }
            )
            for doc, value in ranked[:top_k]
        ]

    def _ensure_model(self) -> Any | None:
        if self._model is not None:
            return self._model
        if self._load_error is not None:
            return None
        with self._lock:
            if self._model is not None or self._load_error is not None:
                return self._model
            try:
                from sentence_transformers import CrossEncoder  # 可选依赖，延迟导入
            except ImportError:
                self._load_error = (
                    "sentence-transformers 未安装（pip install 'skill-evaluate[reranker]'）"
                )
                logger.warning(
                    "reranker_unavailable", reason=self._load_error, model=self._model_name
                )
                return None
            try:
                self._model = CrossEncoder(
                    self._model_name,
                    max_length=self._settings.reranker_max_length,
                    device=self._settings.reranker_device,
                )
            except Exception as exc:  # noqa: BLE001 - 权重下载/加载失败一律降级，不中断检索
                self._load_error = f"模型加载失败：{exc}"[:500]
                logger.warning(
                    "reranker_unavailable", reason=self._load_error, model=self._model_name
                )
                return None
            logger.info("reranker_loaded", model=self._model_name)
            return self._model


def _to_probabilities(values: list[float]) -> list[float]:
    """把 Cross-Encoder 输出统一成 0~1 概率。

    单标签 Cross-Encoder 在 sentence-transformers 3.x / 4.x 下默认已套 Sigmoid，输出本就在
    0~1；但部分模型配置为 Identity 激活，输出的是 logit。两个大版本里控制激活函数的参数名
    还不一样（`activation_fct` / `activation_fn`），因此不传参数，而是看输出：全部落在 0~1
    视为已是概率，否则整体补一次 Sigmoid。Validator 的模板命中阈值依赖这个统一口径。
    """
    if all(0.0 <= value <= 1.0 for value in values):
        return values
    return [1.0 / (1.0 + math.exp(-value)) for value in values]


__all__ = [
    "CrossEncoderReranker",
    "Reranker",
    "RerankerUnavailableError",
]
