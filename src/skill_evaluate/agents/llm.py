"""Agent 层统一 LLM 调用客户端（docs/dev/06 第 10 节的底座）。

本模块只解决"怎么把一次 prompt 发出去、怎么把结构化 JSON 收回来"，不含任何
业务语义；业务语义在 `agents/base.py::BaseLLMAgent` 及其子类里。

设计要点：

1. **协议先行**：`AgentLLMClient` 是 Protocol，`BaseLLMAgent` 只依赖它。真实实现
   `OpenRouterLLMClient` 走 LangChain 的 `langchain_openai.ChatOpenAI`，把 base_url
   指到 OpenRouter（OpenAI 兼容协议），测试注入替身。
2. **全项目只有 OpenRouter 一条出口**：换模型（换厂商）= 改
   `LLMSettings.judge_model` / `mini_agent_model` / `generator_model` 三个字段里的
   OpenRouter 模型 ID（`anthropic/claude-sonnet-5`、`openai/gpt-5.6-terra`、
   `google/gemini-3.5-flash` ...），代码与依赖都不动。这同时让 docs/dev/19 的
   "跨模型泛化"退化成一次配置改动，不再需要为每家厂商写一个 client。
3. **结构化输出用 `response_format`（json_schema, strict）而不是"求模型别乱说话"**：
   OpenRouter 侧强约束返回合法 JSON，再叠加 Pydantic 校验兜底，对应 docs/dev/06
   第 5.3 节"response_format 强约束 + Pydantic 校验"。带 schema 的请求同时下发
   `provider.require_parameters=true`，避免被路由到不支持结构化输出、会把
   `response_format` 静默丢弃的上游。
4. **采样参数的模型能力门禁**：Claude 4.6 及以后的模型（claude-opus-5 /
   claude-sonnet-5 / claude-opus-4-8 ...）已移除 temperature/top_p/top_k。而
   docs/dev/07 要求 Mini Agent 用 Temperature=0.1、docs/dev/08 要求 Judge 做温度
   扰动多副本——这两处约束在新模型上物理不成立。处理方式：
   `model_supports_sampling()` 做一次门禁，不支持时**不发送**该参数并在
   `LLMCompletion.temperature_applied` 里如实返回 None（而不是谎报 0.1），由上层
   决定如何记录。详见 docs/dev/interfaces/06_llm_client_and_sampling.md。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from skill_evaluate.config import get_settings
from skill_evaluate.errors import ConfigurationError
from skill_evaluate.logging import get_logger

logger = get_logger(component="agent_llm")


@dataclass(slots=True)
class LLMCompletion:
    """一次 LLM 调用的归一化结果。"""

    text: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    temperature_applied: float | None  # None = 该模型不接受采样参数，请求里未发送


@runtime_checkable
class AgentLLMClient(Protocol):
    """docs/dev/06~10 全部 Agent 共用的最小 LLM 调用协议。"""

    async def complete(
        self,
        *,
        prompt: str,
        model: str,
        temperature: float,
        system: str | None = None,
        max_tokens: int | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> LLMCompletion: ...


# --------------------------------------------------------------------------- #
# 模型能力门禁
# --------------------------------------------------------------------------- #

# Claude 4.6 起移除了 temperature/top_p/top_k。这里用"模型家族前缀"判定而不是穷举
# 具体模型 ID，保证新出的同代模型不需要改代码。命中任一前缀 = 不接受采样参数。
_SAMPLING_REMOVED_PREFIXES: tuple[str, ...] = (
    "claude-fable-",
    "claude-mythos-",
    "claude-opus-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
)


def normalize_model_id(model: str) -> str:
    """把 OpenRouter 模型 ID 归一成"裸模型名"，供能力门禁做前缀匹配。

    `anthropic/claude-haiku-4.5:batch` -> `claude-haiku-4-5`：去掉 `<厂商>/` 前缀、
    去掉 `:batch` / `:free` 一类的变体后缀，再把 OpenRouter 的点号版本号换成
    Anthropic 原生写法的短横线，这样一张前缀表能同时认两种写法。
    """
    name = model.split("/")[-1]
    name = name.split(":", 1)[0]
    return name.replace(".", "-").strip()


def model_supports_sampling(model: str) -> bool:
    """该模型是否接受 `temperature` 等采样参数。

    返回 False 时调用方**不得**把 temperature 塞进请求体。经 OpenRouter 时多数
    上游会静默丢弃不支持的参数而不是报错，但"静默丢弃"同样意味着扰动没发生，
    所以门禁照旧——它的作用是让 `temperature_applied` 如实反映事实。
    """
    return not normalize_model_id(model).startswith(_SAMPLING_REMOVED_PREFIXES)


# --------------------------------------------------------------------------- #
# Pydantic -> json_schema
# --------------------------------------------------------------------------- #


def build_response_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    """把 Pydantic 模型转成 `response_format.json_schema.schema` 可用的 JSON Schema。

    两处必要的加工（Pydantic 默认不做，但结构化输出要求）：
    - 内联 `$defs`/`$ref`：嵌套模型展开成自包含 schema。
    - 每层 object 补 `additionalProperties: false`，并把全部属性列进 `required`
      （结构化输出要求 required 覆盖所有属性；可选语义用 `T | None` 表达）。
    """
    raw = model_cls.model_json_schema()
    defs = raw.pop("$defs", {})
    schema = _normalize_schema(raw, defs)
    if not isinstance(schema, dict):  # pragma: no cover - Pydantic 顶层恒为 object
        raise ConfigurationError(f"{model_cls.__name__} 的 JSON Schema 顶层不是 object")
    return schema


def _normalize_schema(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_normalize_schema(item, defs) for item in node]
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = defs.get(ref.removeprefix("#/$defs/"))
        if target is None:
            raise ConfigurationError(f"JSON Schema 中存在无法解析的 $ref: {ref!r}")
        merged = {k: v for k, v in node.items() if k != "$ref"}
        return _normalize_schema({**target, **merged}, defs)

    normalized = {key: _normalize_schema(value, defs) for key, value in node.items()}
    if normalized.get("type") == "object" and "properties" in normalized:
        normalized["additionalProperties"] = False
        normalized["required"] = list(normalized["properties"].keys())
    return normalized


def parse_structured_response(text: str, model_cls: type[BaseModel]) -> BaseModel:
    """把模型返回的 JSON 文本解析为 Pydantic 实例。

    容忍模型偶发地用代码块围栏包裹（`response_format` 生效时不会发生，但走
    非结构化通道的替身/降级路径可能出现）。解析失败一律抛 `ValueError`，由
    `BaseLLMAgent._call_llm()` 的重试逻辑接住。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0].strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 输出不是合法 JSON: {exc}") from exc
    return model_cls.model_validate(payload)


# --------------------------------------------------------------------------- #
# 真实实现：OpenRouter（经 LangChain 的 ChatOpenAI）
# --------------------------------------------------------------------------- #

# 结构化输出的 schema 名字，OpenRouter/OpenAI 要求匹配 ^[a-zA-Z0-9_-]+$。
_JSON_SCHEMA_NAME = "structured_output"


class ChatModelFactory(Protocol):
    """构造一个 LangChain chat model 的工厂，测试用它注入假模型。"""

    def __call__(self, *, model: str, temperature: float | None, max_tokens: int) -> Any: ...


def build_openrouter_chat_model(*, model: str, temperature: float | None, max_tokens: int) -> Any:
    """构造指向 OpenRouter 的 `ChatOpenAI`。

    未配置 API Key / 缺少 `langchain-openai` 时直接抛 `ConfigurationError`，不做
    "静默降级成假响应"——假响应会污染下游全部评测结论（与 docs/dev/03 对
    `UnconfiguredHermesSandboxClient` 的处理原则一致）。
    """
    settings = get_settings().llm
    api_key = settings.api_key.get_secret_value()
    if not api_key:
        raise ConfigurationError(
            "未配置 SKILLEVAL_LLM_API_KEY（或 OPENROUTER_API_KEY），无法构造 "
            "OpenRouterLLMClient。本地跑测试请注入替身 AgentLLMClient。"
        )
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise ConfigurationError(
            "缺少 langchain-openai 依赖，请安装项目依赖后重试（uv sync / pip install -e '.[dev]'）。"
        ) from exc

    # OpenRouter 的归因头是可选的，没配就不发，避免出现空串 header。
    headers: dict[str, str] = {}
    if settings.http_referer:
        headers["HTTP-Referer"] = settings.http_referer
    if settings.app_title:
        headers["X-Title"] = settings.app_title

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=settings.base_url,
        temperature=temperature,  # None = 不下发该参数（见 model_supports_sampling）
        max_tokens=max_tokens,
        timeout=settings.request_timeout_s,
        default_headers=headers or None,
        max_retries=0,  # 重试策略由上层（BaseLLMAgent / tenacity）统一决定
    )


class OpenRouterLLMClient:
    """基于 `langchain_openai.ChatOpenAI` + OpenRouter 的 `AgentLLMClient` 实现。

    `ChatOpenAI` 的 model/temperature/max_tokens 是构造期参数，而本协议是按次调用
    传入的，所以这里按 `(model, temperature, max_tokens)` 缓存 chat model 实例：
    一次流水线里通常只有 2~3 种组合，既避免每次调用重建 HTTP 客户端，也不必依赖
    "把 model 塞进 invoke kwargs 覆盖构造参数"这种内部实现细节。
    """

    def __init__(self, chat_factory: ChatModelFactory | None = None) -> None:
        self._chat_factory: ChatModelFactory = chat_factory or build_openrouter_chat_model
        self._chat_models: dict[tuple[str, float | None, int], Any] = {}

    def _chat_model(self, model: str, temperature: float | None, max_tokens: int) -> Any:
        key = (model, temperature, max_tokens)
        chat = self._chat_models.get(key)
        if chat is None:
            chat = self._chat_factory(model=model, temperature=temperature, max_tokens=max_tokens)
            self._chat_models[key] = chat
        return chat

    async def complete(
        self,
        *,
        prompt: str,
        model: str,
        temperature: float,
        system: str | None = None,
        max_tokens: int | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> LLMCompletion:
        from langchain_core.messages import HumanMessage, SystemMessage

        settings = get_settings().llm

        temperature_applied: float | None = None
        if model_supports_sampling(model):
            temperature_applied = temperature
        else:
            logger.debug("llm_sampling_unsupported", model=model, requested_temperature=temperature)

        chat = self._chat_model(
            model, temperature_applied, max_tokens or settings.max_output_tokens
        )

        messages: list[Any] = []
        if system is not None:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=prompt))

        kwargs: dict[str, Any] = {}
        if response_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": _JSON_SCHEMA_NAME,
                    "strict": True,
                    "schema": response_schema,
                },
            }
            # OpenRouter 默认会把上游不支持的参数静默丢弃；对结构化输出而言
            # "静默丢弃"= 拿回一段自由文本，只能靠 Pydantic 兜底重试。要求路由
            # 只挑支持 response_format 的上游，把这种浪费挡在请求侧。
            kwargs["extra_body"] = {"provider": {"require_parameters": True}}

        message = await chat.ainvoke(messages, **kwargs)
        prompt_tokens, completion_tokens = _extract_usage(message)
        return LLMCompletion(
            text=_extract_text(message),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=_extract_model(message, fallback=model),
            temperature_applied=temperature_applied,
        )


def _extract_text(message: Any) -> str:
    """取出 AIMessage 的纯文本。

    `.text` 在 langchain-core 1.x 是属性、0.3 是方法（1.x 的返回值仍可调用，但调用
    会打 deprecation 警告，所以先按 str 用）；`output_version="v1"` 下 `.content`
    还可能是内容块列表。三种形态都归一到 str。
    """
    text = getattr(message, "text", None)
    if isinstance(text, str):
        return text
    if callable(text):
        called = text()
        if isinstance(called, str):
            return called

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)


def _extract_usage(message: Any) -> tuple[int, int]:
    """取 token 用量：优先 LangChain 归一化的 `usage_metadata`，回退到原始 usage。"""
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict):
        return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)

    raw = getattr(message, "response_metadata", None)
    if isinstance(raw, dict):
        token_usage = raw.get("token_usage") or {}
        if isinstance(token_usage, dict):
            return (
                int(token_usage.get("prompt_tokens") or 0),
                int(token_usage.get("completion_tokens") or 0),
            )
    return 0, 0


def _extract_model(message: Any, *, fallback: str) -> str:
    """回填实际生成用的模型。

    OpenRouter 会在响应里回报最终路由到的模型（可能带 `:variant` 后缀），如实记录
    比回声请求参数更有价值——跨模型对比报告靠这个字段区分副本。
    """
    raw = getattr(message, "response_metadata", None)
    if isinstance(raw, dict):
        name = raw.get("model_name") or raw.get("model")
        if isinstance(name, str) and name:
            return name
    return fallback


def build_default_llm_client() -> AgentLLMClient:
    """按 `LLMSettings.provider` 构造默认客户端。

    全项目统一走 OpenRouter：换厂商/换模型只改 `LLMSettings.*_model` 配置，不在
    这里加分支。保留 provider 这道校验只是为了让"配了别的值"立刻失败，而不是
    悄悄按 OpenRouter 发出去。
    """
    provider = get_settings().llm.provider
    if provider != "openrouter":
        raise ConfigurationError(
            f"不支持 LLM provider={provider!r}：本项目统一经 OpenRouter 访问模型。"
            "换模型请改 SKILLEVAL_LLM_JUDGE_MODEL / _GENERATOR_MODEL / _MINI_AGENT_MODEL "
            "为 OpenRouter 模型 ID（如 anthropic/claude-sonnet-5、openai/gpt-5.6-terra）；"
            "自建 OpenAI 兼容网关请改 SKILLEVAL_LLM_BASE_URL。"
        )
    return OpenRouterLLMClient()
