"""docs/dev/interfaces/06：LLM 出口切到 OpenRouter（经 langchain_openai.ChatOpenAI）。

覆盖：OpenRouter 模型 ID 的归一化与采样门禁、请求体的结构化输出约束、用量/模型
回填、chat model 缓存，以及"没配 Key / provider 配错"必须显式失败而不是静默降级。
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from skill_evaluate.agents.llm import (
    OpenRouterLLMClient,
    build_default_llm_client,
    build_openrouter_chat_model,
    model_supports_sampling,
    normalize_model_id,
)
from skill_evaluate.config import LLMSettings, Settings
from skill_evaluate.errors import ConfigurationError


class FakeChatModel:
    """记录 `ainvoke` 收到的参数，并返回预设 AIMessage。"""

    def __init__(self, message: AIMessage | None = None, **construct_kwargs: Any) -> None:
        self.construct_kwargs = construct_kwargs
        self.calls: list[dict[str, Any]] = []
        self._message = message or AIMessage(
            content='{"ok": true}',
            usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            response_metadata={"model_name": "anthropic/claude-haiku-4.5"},
        )

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self._message


def _client_with(
    message: AIMessage | None = None,
) -> tuple[OpenRouterLLMClient, list[FakeChatModel]]:
    built: list[FakeChatModel] = []

    def factory(*, model: str, temperature: float | None, max_tokens: int) -> FakeChatModel:
        chat = FakeChatModel(message, model=model, temperature=temperature, max_tokens=max_tokens)
        built.append(chat)
        return chat

    return OpenRouterLLMClient(chat_factory=factory), built


def _settings_patch(monkeypatch: pytest.MonkeyPatch, llm: LLMSettings) -> None:
    monkeypatch.setattr(
        "skill_evaluate.agents.llm.get_settings", lambda: Settings(llm=llm), raising=True
    )


class ModelIdTests:
    def test_openrouter_id_is_stripped_to_bare_model_name(self) -> None:
        assert normalize_model_id("anthropic/claude-haiku-4.5") == "claude-haiku-4-5"
        assert normalize_model_id("anthropic/claude-haiku-4.5:batch") == "claude-haiku-4-5"
        assert normalize_model_id("claude-haiku-4-5") == "claude-haiku-4-5"

    def test_sampling_gate_reads_through_openrouter_ids(self) -> None:
        # 点号写法必须与原生短横线写法判定一致，否则默认配置下 Mini Agent 的
        # temperature=0.1 会被误判成"不下发"。
        assert model_supports_sampling("anthropic/claude-haiku-4.5") is True
        assert model_supports_sampling("anthropic/claude-sonnet-5") is False
        assert model_supports_sampling("anthropic/claude-opus-5:batch") is False

    def test_non_anthropic_models_keep_sampling(self) -> None:
        assert model_supports_sampling("openai/gpt-5.6-terra") is True
        assert model_supports_sampling("google/gemini-3.5-flash") is True


class CompleteTests:
    @pytest.mark.asyncio
    async def test_system_and_prompt_become_langchain_messages(self) -> None:
        client, built = _client_with()

        await client.complete(
            prompt="请判定", model="anthropic/claude-haiku-4.5", temperature=0.1, system="sys"
        )

        messages = built[0].calls[0]["messages"]
        assert isinstance(messages[0], SystemMessage) and messages[0].content == "sys"
        assert isinstance(messages[1], HumanMessage) and messages[1].content == "请判定"

    @pytest.mark.asyncio
    async def test_response_schema_becomes_strict_json_schema_with_require_parameters(self) -> None:
        client, built = _client_with()
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

        await client.complete(
            prompt="p",
            model="anthropic/claude-haiku-4.5",
            temperature=0.1,
            response_schema=schema,
        )

        kwargs = built[0].calls[0]["kwargs"]
        assert kwargs["response_format"]["type"] == "json_schema"
        assert kwargs["response_format"]["json_schema"]["strict"] is True
        assert kwargs["response_format"]["json_schema"]["schema"] == schema
        # 不加这条，OpenRouter 可能路由到会静默丢弃 response_format 的上游。
        assert kwargs["extra_body"] == {"provider": {"require_parameters": True}}

    @pytest.mark.asyncio
    async def test_plain_call_sends_no_response_format(self) -> None:
        client, built = _client_with()

        await client.complete(prompt="p", model="anthropic/claude-haiku-4.5", temperature=0.1)

        assert built[0].calls[0]["kwargs"] == {}

    @pytest.mark.asyncio
    async def test_sampling_unsupported_model_gets_no_temperature(self) -> None:
        client, built = _client_with()

        completion = await client.complete(
            prompt="p", model="anthropic/claude-sonnet-5", temperature=0.7
        )

        # 既不下发给 ChatOpenAI，也不在结果里谎报做过扰动。
        assert built[0].construct_kwargs["temperature"] is None
        assert completion.temperature_applied is None

    @pytest.mark.asyncio
    async def test_sampling_supported_model_keeps_temperature(self) -> None:
        client, built = _client_with()

        completion = await client.complete(
            prompt="p", model="anthropic/claude-haiku-4.5", temperature=0.1
        )

        assert built[0].construct_kwargs["temperature"] == 0.1
        assert completion.temperature_applied == 0.1

    @pytest.mark.asyncio
    async def test_usage_and_routed_model_are_read_back(self) -> None:
        client, _ = _client_with()

        completion = await client.complete(
            prompt="p", model="~anthropic/claude-haiku-latest", temperature=0.1
        )

        assert completion.text == '{"ok": true}'
        assert (completion.prompt_tokens, completion.completion_tokens) == (11, 7)
        # OpenRouter 回报的是实际路由到的模型，比回声请求参数更有价值。
        assert completion.model == "anthropic/claude-haiku-4.5"

    @pytest.mark.asyncio
    async def test_usage_falls_back_to_raw_token_usage(self) -> None:
        message = AIMessage(
            content="hi",
            response_metadata={"token_usage": {"prompt_tokens": 3, "completion_tokens": 5}},
        )
        client, _ = _client_with(message)

        completion = await client.complete(
            prompt="p", model="anthropic/claude-haiku-4.5", temperature=0.1
        )

        assert (completion.prompt_tokens, completion.completion_tokens) == (3, 5)
        assert completion.model == "anthropic/claude-haiku-4.5"  # 无 model_name 时回落请求值

    @pytest.mark.asyncio
    async def test_chat_models_are_cached_per_model_config(self) -> None:
        client, built = _client_with()

        await client.complete(prompt="a", model="anthropic/claude-haiku-4.5", temperature=0.1)
        await client.complete(prompt="b", model="anthropic/claude-haiku-4.5", temperature=0.1)
        await client.complete(prompt="c", model="anthropic/claude-sonnet-5", temperature=0.1)

        assert len(built) == 2  # 同一组合复用，不同模型才新建


class BuildClientTests:
    def test_missing_api_key_raises_instead_of_faking_responses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _settings_patch(monkeypatch, LLMSettings(api_key=""))

        with pytest.raises(ConfigurationError, match="OPENROUTER_API_KEY"):
            build_openrouter_chat_model(
                model="anthropic/claude-haiku-4.5", temperature=0.1, max_tokens=100
            )

    def test_chat_model_points_at_openrouter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _settings_patch(
            monkeypatch,
            LLMSettings(api_key="sk-or-v1-test", http_referer="https://example.com", app_title="t"),
        )

        chat = build_openrouter_chat_model(
            model="anthropic/claude-haiku-4.5", temperature=0.1, max_tokens=123
        )

        assert chat.model_name == "anthropic/claude-haiku-4.5"
        assert chat.openai_api_base == "https://openrouter.ai/api/v1"
        assert chat.max_tokens == 123
        assert chat.default_headers == {"HTTP-Referer": "https://example.com", "X-Title": "t"}

    def test_default_client_is_openrouter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _settings_patch(monkeypatch, LLMSettings(api_key="sk-or-v1-test"))

        assert isinstance(build_default_llm_client(), OpenRouterLLMClient)

    def test_other_provider_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _settings_patch(monkeypatch, LLMSettings(provider="anthropic"))

        with pytest.raises(ConfigurationError, match="OpenRouter"):
            build_default_llm_client()
