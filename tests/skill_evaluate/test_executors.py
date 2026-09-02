"""docs/dev/03 ExecutorBackend 抽象层测试：MiniAgentBackend happy-path + fallback，
以及 Hermes payload -> ExecutionTrace 映射规则。
"""

from datetime import UTC, datetime

import pytest

from skill_evaluate.errors import ConfigurationError, ExecutorBackendError
from skill_evaluate.executors.base import ExecutionRequest
from skill_evaluate.executors.hermes_backend import (
    HermesBackend,
    HermesHookPayload,
    UnconfiguredHermesSandboxClient,
    map_hermes_payload_to_trace,
)
from skill_evaluate.executors.mini_backend import MiniAgentBackend, MiniLLMResult
from skill_evaluate.executors.registry import (
    get_backend,
    list_registered_backends,
    register_backend,
)
from skill_evaluate.executors.routing import resolve_backend_type
from skill_evaluate.state.enums import DatasetSplit, ExecutorBackendType, TestCaseCategory
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase


def _skill() -> SkillDefinition:
    return SkillDefinition(
        skill_id="s1",
        version_ref="abc",
        root_path=".",
        description="a test skill",
        body_markdown="# body",
        line_count=1,
        token_count=1,
    )


def _case() -> TestCase:
    return TestCase(
        case_id="c1",
        skill_id="s1",
        category=TestCaseCategory.POSITIVE,
        split=DatasetSplit.TRAIN,
        prompt="hello",
        generator_run_id="g1",
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_mini_agent_backend_happy_path_produces_valid_trace() -> None:
    class FakeLLM:
        async def complete(self, *, prompt: str, model: str, temperature: float) -> MiniLLMResult:
            return MiniLLMResult(text="looks fine", prompt_tokens=10, completion_tokens=5)

    backend = MiniAgentBackend(llm_client=FakeLLM())
    trace = await backend.execute(ExecutionRequest(skill=_skill(), case=_case(), run_index=0))

    assert trace.loaded_skill_md is True
    assert trace.final_response == "looks fine"
    assert trace.timing.total_tokens == 15
    assert trace.backend_type == ExecutorBackendType.MINI.value
    assert await backend.health_check() is True


@pytest.mark.asyncio
async def test_mini_agent_backend_fallback_on_llm_error_still_valid_trace() -> None:
    class BrokenLLM:
        async def complete(self, *, prompt: str, model: str, temperature: float) -> MiniLLMResult:
            raise RuntimeError("llm exploded")

    backend = MiniAgentBackend(llm_client=BrokenLLM())
    trace = await backend.execute(ExecutionRequest(skill=_skill(), case=_case(), run_index=0))

    # 容错约定（docs/dev/03 第 6 节）：execute() 不允许向上抛异常，必须返回合法 Trace。
    assert trace.actions[0].action_type == "internal_error"
    assert trace.actions[0].exit_code == 1
    assert "internal_error" in trace.final_response


def test_hermes_payload_mapping_prefers_explicit_signal() -> None:
    now = datetime.now(UTC)
    payload = HermesHookPayload.model_validate(
        {
            "usage": {
                "total_tokens": 10,
                "prompt_tokens": 6,
                "completion_tokens": 4,
                "duration_ms": 100,
            },
            "trajectory": [],
            "final_message": "done",
            "fs_diff": [],
            "skill_md_loaded": True,
            "started_at": now,
            "finished_at": now,
        }
    )
    trace = map_hermes_payload_to_trace(payload, case_id="c1", run_index=0)
    assert trace.loaded_skill_md is True


def test_hermes_payload_mapping_fallback_scans_trajectory() -> None:
    now = datetime.now(UTC)
    payload = HermesHookPayload.model_validate(
        {
            "usage": {},
            "trajectory": [
                {
                    "thought": None,
                    "tool_name": "read_file",
                    "tool_input": {"path": "skills/foo/SKILL.md"},
                    "ts": now,
                }
            ],
            "final_message": "",
            "fs_diff": [],
            "skill_md_loaded": None,
            "started_at": now,
            "finished_at": now,
        }
    )
    trace = map_hermes_payload_to_trace(payload, case_id="c1", run_index=0)
    assert trace.loaded_skill_md is True


def test_hermes_payload_mapping_fallback_false_when_not_read() -> None:
    now = datetime.now(UTC)
    payload = HermesHookPayload.model_validate(
        {
            "usage": {},
            "trajectory": [
                {"thought": None, "tool_name": "bash", "tool_input": {"cmd": "ls"}, "ts": now}
            ],
            "final_message": "",
            "fs_diff": [],
            "skill_md_loaded": None,
            "started_at": now,
            "finished_at": now,
        }
    )
    trace = map_hermes_payload_to_trace(payload, case_id="c1", run_index=0)
    assert trace.loaded_skill_md is False


@pytest.mark.asyncio
async def test_hermes_backend_without_run_id_raises() -> None:
    backend = HermesBackend(sandbox_client=UnconfiguredHermesSandboxClient())
    with pytest.raises(ExecutorBackendError):
        await backend.execute(
            ExecutionRequest(skill=_skill(), case=_case(), run_index=0, run_id=None)
        )


@pytest.mark.asyncio
async def test_hermes_backend_health_check_false_when_unconfigured() -> None:
    backend = HermesBackend(sandbox_client=UnconfiguredHermesSandboxClient())
    assert await backend.health_check() is False


def test_routing_table_covers_all_ten_dimensions() -> None:
    for name in [
        "trigger_accuracy",
        "context_scoping",
        "instruction_control",
        "script_usability",
        "security",
        "coverage_analysis",
        "cross_model_generalization",
        "multi_skill_conflict",
    ]:
        resolve_backend_type(name)  # 不应抛异常


def test_routing_table_unknown_node_raises() -> None:
    with pytest.raises(KeyError):
        resolve_backend_type("not_a_real_dimension")


def test_registry_hermes_is_registered_by_default() -> None:
    assert "hermes" in list_registered_backends()
    assert isinstance(get_backend("hermes"), HermesBackend)


def test_registry_unknown_backend_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        get_backend("does_not_exist")


def test_registry_supports_new_backend_registration_for_module_19() -> None:
    """docs/dev/19 需要新增第二个异构后端，验证扩展点本身可用。"""

    from skill_evaluate.executors.base import ExecutorBackend

    @register_backend("llama_control_test_only")
    class _FakeControlBackend(ExecutorBackend):
        backend_type = ExecutorBackendType.PLUGGABLE

        async def execute(self, request):  # type: ignore[override]
            raise NotImplementedError

        async def health_check(self) -> bool:
            return True

    instance = get_backend("llama_control_test_only")
    assert isinstance(instance, _FakeControlBackend)
