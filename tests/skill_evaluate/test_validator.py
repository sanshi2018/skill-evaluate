"""docs/dev/10：Validator Agent 与动态断言 Git 工具箱。

覆盖：manifest 解析与关键词检索、三条策略路径的选路、模板渲染的零 LLM 路径、
生成脚本的静态语法检查与重试降级、Hook payload 的断言结果映射、执行侧的下发过滤
与 Mini 后端忽略行为、Judge 证据字典。全部用替身注入，不碰数据库、不发真实请求。
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from skill_evaluate.agents.llm import LLMCompletion
from skill_evaluate.agents.validator import (
    AssertionToolbox,
    StaticCheckStatus,
    ToolboxError,
    ValidatorAgent,
    build_assertion_evidence,
    check_script_syntax,
)
from skill_evaluate.agents.validator.evidence import (
    KEY_EXIT_CODE,
    KEY_PASSED,
    KEY_SUMMARY,
    all_passed,
    any_failed,
)
from skill_evaluate.agents.validator.toolbox import extract_keywords, parse_manifest, score_template
from skill_evaluate.executors.base import ExecutionRequest
from skill_evaluate.executors.hermes_backend import (
    HermesHookPayload,
    executable_assertion_specs,
    map_assertion_executions,
)
from skill_evaluate.executors.mini_backend import MiniAgentBackend, StubMiniLLMClient
from skill_evaluate.state.assertion import AssertionResult, AssertionSpec
from skill_evaluate.state.enums import AssertionStrategy, DatasetSplit, TestCaseCategory
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase

MANIFEST = """\
- template: json_schema_validator.py.jinja
  description: "校验目标文件是否为合法 JSON 且满足给定 schema"
  keywords: [json, schema, 结构化输出, 格式校验]
  params: [target_file, schema_definition]
- template: file_exists_validator.py.jinja
  description: "校验目标文件是否存在"
  keywords:
    - 文件
    - 存在
    - output
  params: []
"""

JSON_TEMPLATE = """\
import json, sys
from pathlib import Path

path = Path("{{ target_file }}")
if not path.is_file():
    print(f"缺少产物 {{ target_file }}", file=sys.stderr)
    sys.exit(1)
data = json.loads(path.read_text(encoding="utf-8"))
print(json.dumps(sorted(data)))
sys.exit(0)
"""

FILE_EXISTS_TEMPLATE = """\
import sys
from pathlib import Path

if not Path("out.json").is_file():
    print("out.json 不存在", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
"""


def _skill() -> SkillDefinition:
    body = "# CSV Cleaner\n\n把导出的表格清洗成结构化 JSON。\n"
    return SkillDefinition(
        skill_id="csv-cleaner",
        version_ref="v1",
        root_path=".",
        description="清洗 CSV 导出文件并输出 JSON",
        body_markdown=body,
        line_count=len(body.splitlines()),
        token_count=40,
    )


def _case(
    case_id: str = "case-1",
    expected_output: str | None = "生成 out.json，内容是合法 JSON 且满足给定 schema，格式校验通过",
) -> TestCase:
    return TestCase(
        case_id=case_id,
        skill_id="csv-cleaner",
        category=TestCaseCategory.POSITIVE,
        split=DatasetSplit.TRAIN,
        prompt="把 data.csv 清洗后导出成 out.json",
        expected_output=expected_output,
        generator_run_id="gen-1",
        created_at=datetime.now(UTC),
    )


def _toolbox(tmp_path: Path, *, manifest: str = MANIFEST) -> AssertionToolbox:
    (tmp_path / "templates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "manifest.yaml").write_text(manifest, encoding="utf-8")
    (tmp_path / "templates" / "json_schema_validator.py.jinja").write_text(
        JSON_TEMPLATE, encoding="utf-8"
    )
    (tmp_path / "templates" / "file_exists_validator.py.jinja").write_text(
        FILE_EXISTS_TEMPLATE, encoding="utf-8"
    )
    return AssertionToolbox(tmp_path)


class ScriptLLMClient:
    """按顺序吐出预置的 `ScriptDraft` JSON；用尽后重复最后一条。"""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = payloads
        self.prompts: list[str] = []

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
        self.prompts.append(prompt)
        payload = self._payloads[min(len(self.prompts) - 1, len(self._payloads) - 1)]
        return LLMCompletion(
            text=json.dumps(payload, ensure_ascii=False),
            prompt_tokens=5,
            completion_tokens=5,
            model=model,
            temperature_applied=temperature,
        )


class FakeAssertionRepo:
    def __init__(self) -> None:
        self.specs: list[AssertionSpec] = []
        self.results: list[AssertionResult] = []

    async def save_spec(self, spec: AssertionSpec) -> None:
        self.specs.append(spec)

    async def save_result(self, result: AssertionResult) -> None:
        self.results.append(result)


def _agent(
    toolbox: AssertionToolbox,
    payloads: list[dict[str, Any]] | None = None,
) -> tuple[ValidatorAgent, ScriptLLMClient, FakeAssertionRepo]:
    client = ScriptLLMClient(
        payloads or [{"language": "python", "script": "import sys\nsys.exit(0)\n"}]
    )
    repo = FakeAssertionRepo()
    agent = ValidatorAgent(
        llm_client=client,
        toolbox=toolbox,
        assertion_repository=repo,  # type: ignore[arg-type]
    )
    return agent, client, repo


# --------------------------------------------------------------------------- #
# 工具箱：manifest 解析与检索
# --------------------------------------------------------------------------- #


class ManifestParsingTests:
    def test_parses_inline_and_block_lists(self) -> None:
        metas = parse_manifest(MANIFEST)
        assert [m.template for m in metas] == [
            "json_schema_validator.py.jinja",
            "file_exists_validator.py.jinja",
        ]
        assert metas[0].keywords == ("json", "schema", "结构化输出", "格式校验")
        assert metas[0].params == ("target_file", "schema_definition")
        # 块列表写法的第二条记录没有被上一条吞掉
        assert metas[1].keywords == ("文件", "存在", "output")
        assert metas[1].params == ()

    def test_language_inferred_from_filename(self) -> None:
        metas = parse_manifest("- template: cleanup_validator.sh.jinja\n  keywords: [清理]\n")
        assert metas[0].language == "bash"

    def test_record_without_template_is_rejected(self) -> None:
        with pytest.raises(ToolboxError):
            parse_manifest("- description: 没有 template 字段\n")


class KeywordLookupTests:
    def test_cjk_bigrams_allow_reordered_match(self) -> None:
        tokens = extract_keywords("校验格式")
        assert "校验" in tokens
        metas = parse_manifest(MANIFEST)
        # manifest 写 "格式校验"，查询写 "校验格式"，仍然能对上
        assert score_template(metas[0], "需要做校验格式的检查") > 0

    async def test_lookup_ranks_and_filters_by_threshold(self, tmp_path: Path) -> None:
        toolbox = _toolbox(tmp_path)
        matches = await toolbox.lookup("产出合法 json，做格式校验，满足 schema", threshold=0.3)
        assert matches
        assert matches[0].template.template == "json_schema_validator.py.jinja"
        assert await toolbox.lookup("与工具箱毫无关系的内容", threshold=0.9) == []

    def test_unavailable_toolbox_is_not_an_error(self, tmp_path: Path) -> None:
        toolbox = AssertionToolbox(tmp_path / "never-synced")
        assert toolbox.available is False
        assert toolbox.manifest() == []

    def test_template_ref_carries_commit_sha(self, tmp_path: Path) -> None:
        toolbox = _toolbox(tmp_path)
        ref = toolbox.template_ref("json_schema_validator.py.jinja")
        assert ref.startswith("templates/json_schema_validator.py.jinja@")

    def test_render_requires_declared_params(self, tmp_path: Path) -> None:
        toolbox = _toolbox(tmp_path)
        meta = toolbox.manifest()[0]
        with pytest.raises(ToolboxError) as exc:
            toolbox.render(meta, {"target_file": "out.json"})
        assert "schema_definition" in str(exc.value)

    async def test_sync_without_repo_url_is_a_noop(self, tmp_path: Path) -> None:
        toolbox = AssertionToolbox(tmp_path, repo_url=None)
        assert await toolbox.sync() is False


# --------------------------------------------------------------------------- #
# 静态检查
# --------------------------------------------------------------------------- #


class StaticCheckTests:
    def test_valid_python_passes(self) -> None:
        assert check_script_syntax("import sys\nsys.exit(0)\n", "python").ok

    def test_invalid_python_fails_with_line_number(self) -> None:
        result = check_script_syntax("def broken(:\n    pass\n", "python")
        assert result.status is StaticCheckStatus.FAILED
        assert "第 1 行" in result.detail

    def test_empty_script_fails(self) -> None:
        assert check_script_syntax("   \n", "python").status is StaticCheckStatus.FAILED

    def test_unknown_language_fails(self) -> None:
        assert check_script_syntax("echo hi", "ruby").status is StaticCheckStatus.FAILED

    def test_bash_check_passes_or_skips(self) -> None:
        # 宿主可能没有 bash（Windows），SKIPPED 也算通过——无法验证 != 验证不通过。
        assert check_script_syntax("set -e\necho hi\n", "bash").ok


# --------------------------------------------------------------------------- #
# 策略选路与产出
# --------------------------------------------------------------------------- #


class PlanAssertionTests:
    async def test_no_expected_output_yields_none_strategy(self, tmp_path: Path) -> None:
        agent, client, repo = _agent(_toolbox(tmp_path))
        spec = await agent.plan_assertion(_case(expected_output=None), _skill())

        assert spec.strategy is AssertionStrategy.NONE
        assert spec.is_executable is False
        assert spec.script_content is None
        assert client.prompts == []  # 不调用 LLM
        assert repo.specs == [spec]

    async def test_template_lookup_needs_no_llm(self, tmp_path: Path) -> None:
        agent, client, _ = _agent(_toolbox(tmp_path))
        spec = await agent.plan_assertion(
            _case(),
            _skill(),
            known_params={"target_file": "out.json", "schema_definition": "{}"},
        )

        assert spec.strategy is AssertionStrategy.TEMPLATE_LOOKUP
        assert client.prompts == []
        assert spec.template_ref is not None
        assert "out.json" in (spec.script_content or "")
        assert spec.script_path is not None and spec.script_path.endswith(".py")
        assert spec.is_executable

    async def test_missing_params_degrade_to_inherit(self, tmp_path: Path) -> None:
        agent, client, _ = _agent(
            _toolbox(tmp_path),
            [
                {
                    "language": "python",
                    "script": "import sys\nsys.exit(0)\n",
                    "rationale": "检查 out.json",
                }
            ],
        )
        spec = await agent.plan_assertion(_case(), _skill())

        assert spec.strategy is AssertionStrategy.TEMPLATE_INHERIT
        assert len(client.prompts) == 1
        # 继承式生成必须把模板原文作为 few-shot 交给模型
        assert "json.loads" in client.prompts[0]
        assert spec.template_ref is not None

    async def test_no_toolbox_falls_back_to_scratch(self, tmp_path: Path) -> None:
        agent, client, _ = _agent(AssertionToolbox(tmp_path / "absent"))
        spec = await agent.plan_assertion(_case(), _skill())

        assert spec.strategy is AssertionStrategy.GENERATED_FROM_SCRATCH
        assert spec.template_ref is None
        assert len(client.prompts) == 1
        assert "exit_code" in client.prompts[0]  # 脚本输出规范进了 Prompt

    async def test_syntax_failure_retries_then_degrades_to_none(self, tmp_path: Path) -> None:
        agent, client, repo = _agent(
            AssertionToolbox(tmp_path / "absent"),
            [{"language": "python", "script": "def broken(:\n    pass\n"}],
        )
        spec = await agent.plan_assertion(_case(), _skill())

        # 默认 max_script_repair_retries=2 -> 首次 + 2 次重试 = 3 次调用
        assert len(client.prompts) == 3
        assert "静态语法检查" in client.prompts[1]  # 错误回灌
        assert spec.strategy is AssertionStrategy.NONE
        assert spec.failure_reason is not None and "断言生成失败" in spec.failure_reason
        assert repo.specs[-1].strategy is AssertionStrategy.NONE

    async def test_repair_succeeds_on_second_attempt(self, tmp_path: Path) -> None:
        agent, client, _ = _agent(
            AssertionToolbox(tmp_path / "absent"),
            [
                {"language": "python", "script": "def broken(:\n"},
                {"language": "python", "script": "import sys\nsys.exit(0)\n"},
            ],
        )
        spec = await agent.plan_assertion(_case(), _skill())

        assert len(client.prompts) == 2
        assert spec.strategy is AssertionStrategy.GENERATED_FROM_SCRATCH
        assert spec.is_executable

    async def test_batch_reports_degraded_cases_only(self, tmp_path: Path) -> None:
        agent, _, _ = _agent(
            AssertionToolbox(tmp_path / "absent"),
            [{"language": "python", "script": "def broken(:\n"}],
        )
        batch = await agent.plan_assertions(
            [_case("case-1"), _case("case-2", expected_output=None)], _skill()
        )

        assert len(batch.specs) == 2
        # 只有"生成失败"算降级；"本来就不需要断言"不算
        assert batch.degraded_case_ids == ["case-1"]
        assert batch.executable_count == 0


# --------------------------------------------------------------------------- #
# 执行侧：下发过滤 / Hook 映射 / Mini 后端
# --------------------------------------------------------------------------- #


def _spec(assertion_id: str, *, script: str | None = "import sys\nsys.exit(0)\n") -> AssertionSpec:
    return AssertionSpec(
        assertion_id=assertion_id,
        case_id="case-1",
        strategy=(
            AssertionStrategy.NONE if script is None else AssertionStrategy.GENERATED_FROM_SCRATCH
        ),
        script_path=None if script is None else f"/tmp/{assertion_id}.py",
        script_content=script,
    )


class ExecutionIntegrationTests:
    def test_only_executable_specs_are_dispatched(self) -> None:
        specs = [_spec("a-1"), _spec("a-2", script=None), _spec("a-3", script="")]
        assert [s.assertion_id for s in executable_assertion_specs(specs)] == ["a-1"]

    def test_hook_payload_maps_exit_code_to_passed(self) -> None:
        now = datetime.now(UTC)
        payload = HermesHookPayload(
            started_at=now,
            finished_at=now,
            assertion_executions=[
                {"assertion_id": "a-1", "exit_code": 0, "stdout": "ok", "stderr": ""},
                {"assertion_id": "a-2", "exit_code": 2, "stderr": "期望 3 列，实际 2 列"},
            ],
        )
        results = map_assertion_executions(payload)

        assert [r.passed for r in results] == [True, False]
        assert results[1].stderr == "期望 3 列，实际 2 列"
        assert results[1].stdout == ""  # None 归一成空串，落库列非空

    def test_payload_without_assertions_is_unchanged(self) -> None:
        now = datetime.now(UTC)
        payload = HermesHookPayload(started_at=now, finished_at=now)
        assert map_assertion_executions(payload) == []

    async def test_mini_backend_ignores_assertion_specs(self) -> None:
        backend = MiniAgentBackend(llm_client=StubMiniLLMClient())
        trace = await backend.execute(
            ExecutionRequest(
                skill=_skill(),
                case=_case(),
                run_index=0,
                assertion_specs=[_spec("a-1")],
            )
        )
        # 忽略而不是报错：这属于路由配错的旁路情况，不该让静态审查失败
        assert trace.final_response
        assert trace.actions[0].action_type == "static_review"


# --------------------------------------------------------------------------- #
# Judge 证据
# --------------------------------------------------------------------------- #


class EvidenceTests:
    def test_evidence_dict_is_all_strings(self) -> None:
        results = [
            AssertionResult.from_exit_code(assertion_id="a-1", exit_code=1, stderr="缺少 out.json")
        ]
        evidence = build_assertion_evidence(results, spec=_spec("a-1"))

        assert all(isinstance(v, str) for v in evidence.values())
        assert evidence[KEY_EXIT_CODE] == "1"
        assert evidence[KEY_PASSED] == "false"
        assert "缺少 out.json" in evidence[KEY_SUMMARY]

    def test_none_strategy_states_why_there_is_no_assertion(self) -> None:
        spec = _spec("a-1", script=None).model_copy(update={"failure_reason": "用例未声明期望产出"})
        evidence = build_assertion_evidence([], spec=spec)

        assert "用例未声明期望产出" in evidence[KEY_SUMMARY]
        assert KEY_EXIT_CODE not in evidence

    def test_planned_but_missing_results_is_explicit(self) -> None:
        evidence = build_assertion_evidence([], spec=_spec("a-1"))
        assert "未取到执行结果" in evidence[KEY_SUMMARY]

    def test_predicates(self) -> None:
        ok = AssertionResult.from_exit_code(assertion_id="a-1", exit_code=0)
        bad = AssertionResult.from_exit_code(assertion_id="a-2", exit_code=1)
        assert all_passed([ok]) and not any_failed([ok])
        assert any_failed([ok, bad]) and not all_passed([ok, bad])
        assert all_passed([])  # 没有断言不构成失败证据
