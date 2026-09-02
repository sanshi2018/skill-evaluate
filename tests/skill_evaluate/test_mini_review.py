"""docs/dev/07：Mini Agent 评审框架。

覆盖：模板注册表机制、7 个首批模板的可渲染性与 Schema 契约、`review()` 产出
`JudgeVerdict` 的映射规则、`to_severity` 扩展位（留给 docs/dev/15）。
"""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from skill_evaluate.agents.llm import LLMCompletion, build_response_schema
from skill_evaluate.agents.mini import (
    MiniReviewAgent,
    ReviewRequest,
    ReviewTemplate,
    get_template,
    register_template,
    verdict_field_to_status,
)
from skill_evaluate.agents.mini.templates.builtin import BUILTIN_TEMPLATE_KEYS
from skill_evaluate.agents.mini.templates.schemas import (
    ControlCalibrationOutput,
    OmissionAuditOutput,
    ScopingCheckOutput,
)
from skill_evaluate.errors import ReviewTemplateError
from skill_evaluate.state.enums import JudgeVerdictStatus

_CONTENT_FIXTURES: dict[str, dict[str, str]] = {
    "omission_audit": {"skill_md": "# demo"},
    "scoping_check": {"skill_md": "# demo"},
    "progressive_disclosure_static": {"skill_md": "# demo", "reference_files": "- a.md"},
    "help_doc_quality": {"script_path": "scripts/x.py", "help_output": "usage: x"},
    "constructive_error": {"invocation": "x --bad", "error_output": "Traceback"},
    "linguistic_smell": {"skill_md": "# demo"},
    "control_calibration": {"skill_md": "# demo"},
}


class StaticLLMClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
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
        self.calls.append({"prompt": prompt, "temperature": temperature, "model": model})
        return LLMCompletion(
            text=json.dumps(self.payload, ensure_ascii=False),
            prompt_tokens=3,
            completion_tokens=3,
            model=model,
            temperature_applied=temperature,
        )


class NoopJudgeRepo:
    def __init__(self) -> None:
        self.saved: list[Any] = []

    async def save_verdict(self, verdict: Any) -> None:
        self.saved.append(verdict)


class RegistryTests:
    def test_first_batch_of_seven_is_registered(self) -> None:
        assert len(BUILTIN_TEMPLATE_KEYS) == 7
        assert set(BUILTIN_TEMPLATE_KEYS) == set(_CONTENT_FIXTURES)

    def test_every_builtin_template_renders_with_its_declared_variables(self) -> None:
        for key in BUILTIN_TEMPLATE_KEYS:
            template = get_template(key)
            rendered = template.render(_CONTENT_FIXTURES[key])
            # 公共前缀的三条框架级约束必须出现在每一份 Prompt 里。
            assert "只输出 JSON" in rendered
            assert "宁可漏判也不误判" in rendered
            assert "reasoning 必须引用原文片段" in rendered

    def test_missing_content_variable_is_rejected_before_calling_llm(self) -> None:
        template = get_template("progressive_disclosure_static")
        with pytest.raises(ReviewTemplateError, match="reference_files"):
            template.render({"skill_md": "# demo"})

    def test_unknown_key_lists_registered_ones(self) -> None:
        with pytest.raises(ReviewTemplateError, match="omission_audit"):
            get_template("no_such_template")

    def test_duplicate_registration_is_rejected(self) -> None:
        with pytest.raises(ReviewTemplateError, match="重复注册"):
            register_template(
                ReviewTemplate(
                    key="omission_audit",
                    prompt_path="omission_audit.jinja",
                    output_schema=OmissionAuditOutput,
                    to_status=verdict_field_to_status,
                )
            )

    def test_missing_prompt_file_is_rejected_at_registration(self) -> None:
        with pytest.raises(ReviewTemplateError, match="不存在"):
            register_template(
                ReviewTemplate(
                    key="ghost_template",
                    prompt_path="ghost.jinja",
                    output_schema=OmissionAuditOutput,
                    to_status=verdict_field_to_status,
                )
            )

    def test_to_severity_slot_is_available_for_doc15(self) -> None:
        # docs/dev/15 需要把结构化输出映射到 SeverityLevel 而不是 pass/fail；
        # 这个字段的存在保证它无需修改 ReviewTemplate 本身。
        assert "to_severity" in ReviewTemplate.model_fields


class SchemaContractTests:
    def test_verdict_only_accepts_the_agreed_literals(self) -> None:
        # 模型返回 "PASS" 这类没约定过的值必须校验失败并触发重试，
        # 而不是被 `verdict == "pass"` 悄悄判成 FAIL。
        with pytest.raises(ValidationError):
            OmissionAuditOutput(reasoning="r", verdict="PASS")  # type: ignore[arg-type]

    def test_optional_enum_field_accepts_null(self) -> None:
        output = ScopingCheckOutput(reasoning="r", verdict="pass", scope_issue=None)
        assert output.scope_issue is None

    def test_every_builtin_schema_converts_to_json_schema(self) -> None:
        for key in BUILTIN_TEMPLATE_KEYS:
            schema = build_response_schema(get_template(key).output_schema)
            assert schema["additionalProperties"] is False
            assert "reasoning" in schema["required"]
            assert "verdict" in schema["required"]


class MiniReviewAgentTests:
    @pytest.mark.asyncio
    async def test_pass_verdict_is_mapped_and_persisted(self) -> None:
        client = StaticLLMClient(
            {"reasoning": "原文写的是 X", "verdict": "pass", "common_sense_statements": []}
        )
        repo = NoopJudgeRepo()
        agent = MiniReviewAgent(
            model="claude-haiku-4-5",
            llm_client=client,
            judge_repository=repo,  # type: ignore[arg-type]
        )

        verdict = await agent.review(
            ReviewRequest(
                subject_id="skill-1", template_key="omission_audit", content={"skill_md": "# demo"}
            )
        )

        assert verdict.status is JudgeVerdictStatus.PASS
        assert verdict.subject_id == "skill-1"
        assert verdict.reasoning == "原文写的是 X"
        assert verdict.temperature == 0.1  # 架构文档要求的低温审查
        assert repo.saved == [verdict]

    @pytest.mark.asyncio
    async def test_fail_verdict_is_mapped(self) -> None:
        client = StaticLLMClient(
            {
                "reasoning": "第 3 行 '什么是 CSV' 属于常识",
                "verdict": "fail",
                "common_sense_statements": ["什么是 CSV"],
            }
        )
        agent = MiniReviewAgent(
            model="claude-haiku-4-5",
            llm_client=client,
            judge_repository=NoopJudgeRepo(),  # type: ignore[arg-type]
        )

        detailed = await agent.review_detailed(
            ReviewRequest(
                subject_id="skill-1", template_key="omission_audit", content={"skill_md": "# demo"}
            )
        )

        assert detailed.verdict.status is JudgeVerdictStatus.FAIL
        assert isinstance(detailed.output, OmissionAuditOutput)
        assert detailed.output.common_sense_statements == ["什么是 CSV"]

    @pytest.mark.asyncio
    async def test_persist_can_be_disabled_for_judge_consensus(self) -> None:
        # docs/dev/08 的共识流程会对同一 subject 连打多次，由 Judge 侧决定入库。
        client = StaticLLMClient(
            {
                "reasoning": "r",
                "verdict": "pass",
                "task_fragility": "fragile",
                "control_style_observed": "rigid",
                "has_default_recommendation": True,
                "has_checklist_or_plan_verify_loop": True,
            }
        )
        repo = NoopJudgeRepo()
        agent = MiniReviewAgent(
            model="claude-haiku-4-5",
            llm_client=client,
            judge_repository=repo,  # type: ignore[arg-type]
            persist=False,
        )

        detailed = await agent.review_detailed(
            ReviewRequest(
                subject_id="skill-1",
                template_key="control_calibration",
                content={"skill_md": "# demo"},
            )
        )

        assert repo.saved == []
        assert isinstance(detailed.output, ControlCalibrationOutput)

    @pytest.mark.asyncio
    async def test_same_request_can_be_replayed_at_different_temperatures(self) -> None:
        # docs/dev/08 的多副本共识就是这样复用 review()：投票是 Judge 的职责，
        # MiniReviewAgent 只负责执行一次评审。
        payload = {"reasoning": "r", "verdict": "pass", "common_sense_statements": []}
        request = ReviewRequest(
            subject_id="skill-1", template_key="omission_audit", content={"skill_md": "# demo"}
        )

        temperatures = [0.0, 0.3, 0.7]
        seen: list[float] = []
        for temperature in temperatures:
            client = StaticLLMClient(payload)
            agent = MiniReviewAgent(
                model="claude-haiku-4-5",
                temperature=temperature,
                llm_client=client,
                judge_repository=NoopJudgeRepo(),  # type: ignore[arg-type]
                persist=False,
            )
            verdict = await agent.review(request)
            seen.append(verdict.temperature)
            assert client.calls[0]["temperature"] == temperature

        assert seen == temperatures
