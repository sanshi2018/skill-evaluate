"""docs/dev/06 第 10 节：`BaseLLMAgent` 与共享 LLM 层。

覆盖：结构化解析成功/重试/耗尽、Langfuse 打点挂载、采样参数的模型能力门禁、
Pydantic -> json_schema 的转换规则。
"""

from typing import Any

import pytest
from pydantic import BaseModel

from skill_evaluate.agents.base import BaseLLMAgent
from skill_evaluate.agents.llm import (
    LLMCompletion,
    build_response_schema,
    model_supports_sampling,
    parse_structured_response,
)
from skill_evaluate.errors import AgentResponseFormatError
from skill_evaluate.observability.langfuse_adapter import LangfuseTraceHandle
from skill_evaluate.state.trace import TimingCostMetrics


class Answer(BaseModel):
    verdict: str
    score: int


class Nested(BaseModel):
    answers: list[Answer]


class ScriptedLLMClient:
    """按脚本依次返回预设文本，并记录每次收到的参数。"""

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "temperature": temperature,
                "system": system,
                "response_schema": response_schema,
            }
        )
        text = self._texts.pop(0) if self._texts else "{}"
        return LLMCompletion(
            text=text,
            prompt_tokens=11,
            completion_tokens=7,
            model=model,
            temperature_applied=temperature,
        )


class _Agent(BaseLLMAgent):
    name = "test_agent"

    async def ask(self) -> Answer:
        return await self._call_llm("请判定", Answer, system="sys")


class RecordingLangfuse:
    def __init__(self) -> None:
        self.agent_calls: list[dict[str, Any]] = []

    def log_agent_call(
        self,
        trace_handle: LangfuseTraceHandle,
        agent_name: str,
        prompt: str,
        response: str,
        model: str,
        usage: TimingCostMetrics,
    ) -> None:
        self.agent_calls.append(
            {"agent_name": agent_name, "model": model, "total_tokens": usage.total_tokens}
        )


class SamplingGateTests:
    def test_new_generation_models_reject_sampling(self) -> None:
        assert model_supports_sampling("claude-sonnet-5") is False
        assert model_supports_sampling("claude-opus-5") is False
        assert model_supports_sampling("claude-opus-4-8") is False

    def test_older_models_still_accept_sampling(self) -> None:
        # Mini Agent 默认模型必须仍支持 temperature=0.1，否则 docs/dev/07 的
        # "低温审查"约束在默认配置下就失效了。
        assert model_supports_sampling("claude-haiku-4-5") is True


class ResponseSchemaTests:
    def test_flat_model_gets_additional_properties_false(self) -> None:
        schema = build_response_schema(Answer)
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {"verdict", "score"}

    def test_nested_refs_are_inlined(self) -> None:
        schema = build_response_schema(Nested)
        assert "$defs" not in schema
        item = schema["properties"]["answers"]["items"]
        assert item["additionalProperties"] is False
        assert set(item["properties"]) == {"verdict", "score"}

    def test_fenced_json_is_tolerated(self) -> None:
        parsed = parse_structured_response('```json\n{"verdict": "pass", "score": 1}\n```', Answer)
        assert isinstance(parsed, Answer)
        assert parsed.verdict == "pass"

    def test_non_json_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="不是合法 JSON"):
            parse_structured_response("大概是 pass 吧", Answer)


class CallLlmTests:
    @pytest.mark.asyncio
    async def test_happy_path_parses_and_records_usage(self) -> None:
        client = ScriptedLLMClient(['{"verdict": "pass", "score": 3}'])
        agent = _Agent(model="claude-haiku-4-5", temperature=0.1, llm_client=client)

        answer = await agent.ask()

        assert answer.score == 3
        assert len(client.calls) == 1
        assert client.calls[0]["system"] == "sys"
        assert client.calls[0]["response_schema"]["additionalProperties"] is False
        assert agent.last_usage is not None
        assert agent.last_usage.total_tokens == 18

    @pytest.mark.asyncio
    async def test_invalid_output_is_retried_with_error_fed_back(self) -> None:
        client = ScriptedLLMClient(["不是 JSON", '{"verdict": "fail", "score": 0}'])
        agent = _Agent(model="claude-haiku-4-5", temperature=0.1, llm_client=client)

        answer = await agent.ask()

        assert answer.verdict == "fail"
        assert len(client.calls) == 2
        # 第二次调用必须带上第一次的错误，否则重试只是"再赌一次"。
        assert "无法通过结构化校验" in client.calls[1]["prompt"]

    @pytest.mark.asyncio
    async def test_exhausted_retries_raise_instead_of_returning_partial(self) -> None:
        client = ScriptedLLMClient(["坏", "还是坏", "依旧坏", "第四次"])
        agent = _Agent(model="claude-haiku-4-5", temperature=0.1, llm_client=client)

        with pytest.raises(AgentResponseFormatError):
            await agent.ask()
        # 默认 max_structured_retries=2 => 首次 + 2 次重试 = 3 次，不多不少。
        assert len(client.calls) == 3

    @pytest.mark.asyncio
    async def test_langfuse_hook_is_invoked_when_trace_handle_given(self) -> None:
        client = ScriptedLLMClient(['{"verdict": "pass", "score": 1}'])
        langfuse = RecordingLangfuse()
        agent = _Agent(
            model="claude-haiku-4-5",
            temperature=0.1,
            llm_client=client,
            langfuse_adapter=langfuse,  # type: ignore[arg-type]
            trace_handle=LangfuseTraceHandle(run_id="run-1"),
        )

        await agent.ask()

        assert langfuse.agent_calls == [
            {"agent_name": "test_agent", "model": "claude-haiku-4-5", "total_tokens": 18}
        ]

    @pytest.mark.asyncio
    async def test_no_trace_handle_means_no_langfuse_call(self) -> None:
        client = ScriptedLLMClient(['{"verdict": "pass", "score": 1}'])
        langfuse = RecordingLangfuse()
        agent = _Agent(
            model="claude-haiku-4-5",
            temperature=0.1,
            llm_client=client,
            langfuse_adapter=langfuse,  # type: ignore[arg-type]
        )

        await agent.ask()

        assert langfuse.agent_calls == []
