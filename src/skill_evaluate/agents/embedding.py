"""文本 embedding 客户端（docs/dev/21 第 2.1 节"最小向量基础设施"）。

与 `agents/llm.py` 同一套设计：**协议先行**，业务代码（反坍塌检测、种子锚点检索）只依赖
`EmbeddingClient` 协议，真实实现走 OpenRouter 的 OpenAI 兼容 `/embeddings` 端点，测试注入替身。

为什么不经 LangChain 的 `OpenAIEmbeddings`：它默认会按 tiktoken 对输入做切块与重新拼接
（`check_embedding_ctx_length`），对非 OpenAI 上游的模型反而会得到错位的向量；这里只需要
"一段文本 → 一个向量"，直接一次 httpx POST 更可控，也不多引入依赖（httpx 已是运行期依赖）。

全项目仍然只有 OpenRouter 一条出口：`api_key` / `base_url` / 归因头全部复用 `LLMSettings`，
模型与维度在 `GeneratorTrustSettings`。docs/dev/23 的混合检索复用同一个客户端。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

import httpx

from skill_evaluate.config import get_settings
from skill_evaluate.errors import ConfigurationError, SkillEvaluateError
from skill_evaluate.logging import get_logger

logger = get_logger(component="embedding")


class EmbeddingError(SkillEvaluateError):
    """embedding 调用失败（网络/上游报错/返回形状不符）。

    不并入 `AgentResponseFormatError`：那个的语义是"LLM 输出解析失败、可带错误回灌重试"，
    embedding 没有"让模型改正"这回事，失败就是通道故障。
    """


@runtime_checkable
class EmbeddingClient(Protocol):
    """最小 embedding 协议：一批文本 → 等长、同序的一批向量。"""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class OpenRouterEmbeddingClient:
    """OpenRouter（OpenAI 兼容协议）`POST {base_url}/embeddings` 的实现。

    构造不读 API Key（惰性到首次 `embed()`）：与 `RealMiniLLMClient` 同一取舍——没配 Key
    的环境仍然可以构造依赖容器、跑不涉及 embedding 的路径。
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        dimensions: int | None = None,
        batch_size: int | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        trust = get_settings().generator_trust
        self._model = model or trust.embedding_model
        self._dimensions = dimensions or trust.embedding_dimensions
        self._batch_size = batch_size or trust.embedding_batch_size
        self._max_input_chars = trust.embedding_max_input_chars
        # 可注入：单测用 `httpx.MockTransport` 验证请求体与维度校验，不发真实请求。
        self._http_client = http_client

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        # 截断超长输入（见 GeneratorTrustSettings.embedding_max_input_chars）；空串替换为单个空格，
        # 部分上游对空输入直接 400。
        prepared = [(text[: self._max_input_chars] or " ") for text in texts]
        vectors: list[list[float]] = []
        # 分批：上游对单次 input 条数有上限，一次把几百条历史用例打过去会 413。
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(await self._embed_batch(prepared[start : start + self._batch_size]))
        return vectors

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        llm = get_settings().llm
        api_key = llm.api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError(
                "未配置 SKILLEVAL_LLM_API_KEY（或 OPENROUTER_API_KEY），无法计算 embedding。"
                "确实没有 embedding 通道的环境请显式设置 "
                "SKILLEVAL_GENERATOR_TRUST_COLLAPSE_CHECK_ENABLED=false（会留下告警日志）。"
            )
        headers = {"Authorization": f"Bearer {api_key}"}
        if llm.http_referer:
            headers["HTTP-Referer"] = llm.http_referer
        if llm.app_title:
            headers["X-Title"] = llm.app_title

        body: dict[str, Any] = {"model": self._model, "input": batch}
        # `dimensions` 只对支持 Matryoshka 截断的模型有意义（text-embedding-3-*）；显式下发
        # 是为了让"配置的维度"与"表结构的维度"绑定，而不是依赖模型的默认输出维度。
        body["dimensions"] = self._dimensions

        url = f"{llm.base_url.rstrip('/')}/embeddings"
        try:
            if self._http_client is not None:
                response = await self._http_client.post(url, json=body, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=llm.request_timeout_s) as client:
                    response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingError(f"embedding 请求失败（model={self._model}）：{exc}") from exc

        return self._parse(payload, expected=len(batch))

    def _parse(self, payload: Any, *, expected: int) -> list[list[float]]:
        """按 `index` 排序后取向量，并做两道硬校验。

        - 条数必须对上：少一条会让 `zip(cases, vectors)` 静默错位，把 A 用例的向量存到 B 名下；
        - 维度必须对上：pgvector 列是定长 `vector(1536)`，维度不符在落库时才炸会让已经算完的
          距离判定白算，这里提前失败并点名配置项。
        """
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != expected:
            raise EmbeddingError(
                f"embedding 返回条数不符：期望 {expected}，实际 "
                f"{len(data) if isinstance(data, list) else 'N/A'}"
            )
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        vectors: list[list[float]] = []
        for item in ordered:
            vector = [float(v) for v in item.get("embedding") or []]
            if len(vector) != self._dimensions:
                raise EmbeddingError(
                    f"embedding 维度不符：期望 {self._dimensions}，实际 {len(vector)}。"
                    "请检查 SKILLEVAL_GENERATOR_TRUST_EMBEDDING_MODEL / _EMBEDDING_DIMENSIONS "
                    "与迁移 0009 的 vector 维度是否一致。"
                )
            vectors.append(vector)
        return vectors


# --------------------------------------------------------------------------- #
# 纯 Python 向量工具（项目不依赖 numpy，见 pyproject：只为几次点积引入 numpy 不划算）
# --------------------------------------------------------------------------- #


def normalize(vector: Sequence[float]) -> list[float]:
    """L2 归一化。归一化后余弦相似度 = 点积，批量两两计算时省掉重复开方。

    零向量原样返回（全 0）：它与任何向量的"相似度"都记为 0，不除零报错——上游偶发返回
    零向量不该让整次判定崩掉。
    """
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return [0.0 for _ in vector]
    return [v / norm for v in vector]


def dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    return dot(normalize(left), normalize(right))


__all__ = [
    "EmbeddingClient",
    "EmbeddingError",
    "OpenRouterEmbeddingClient",
    "cosine_similarity",
    "dot",
    "normalize",
]
