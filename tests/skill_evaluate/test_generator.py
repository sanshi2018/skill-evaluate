"""docs/dev/06：Generator Agent 与测试集生命周期。

重点覆盖项目的第一条关键约束——**默认复用、只能手动强制重生**——以及版本漂移
时"只告警不自动重生"这条容易被后续模块误解的语义。
"""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from skill_evaluate.agents.generator import (
    CapabilityFocus,
    GenerationRequest,
    GeneratorAgent,
    TestSuiteService,
    extract_keywords,
)
from skill_evaluate.agents.generator.service import _split_dataset
from skill_evaluate.agents.llm import LLMCompletion
from skill_evaluate.errors import GenerationError
from skill_evaluate.state.enums import DatasetSplit, GenerationMode, TestCaseCategory
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion


def _skill(version_ref: str = "v1") -> SkillDefinition:
    return SkillDefinition(
        skill_id="csv-cleaner",
        version_ref=version_ref,
        root_path=".",
        description="清洗并校验 CSV 导出文件，输出规范化的数据表",
        body_markdown="# CSV Cleaner\n\n## 校验规则\n\n## 导出格式",
        line_count=5,
        token_count=40,
    )


class CountingLLMClient:
    """按调用次数返回固定数量的用例，并统计调用次数（用于验证"没调用 LLM"）。"""

    def __init__(self, cases_per_call: int = 2) -> None:
        self.call_count = 0
        self.prompts: list[str] = []
        self._cases_per_call = cases_per_call

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
        self.call_count += 1
        self.prompts.append(prompt)
        payload = {
            "cases": [
                {
                    "prompt": f"用例 {self.call_count}-{i}",
                    "rationale": "理由",
                    "diversity_tag": "colloquial",
                    "target_capability_ids": [],
                    "negative_constraint_ids": [],
                    "expected_output": None,
                }
                for i in range(self._cases_per_call)
            ]
        }
        return LLMCompletion(
            text=json.dumps(payload, ensure_ascii=False),
            prompt_tokens=5,
            completion_tokens=5,
            model=model,
            temperature_applied=None,
        )


class FakeSuiteRepo:
    def __init__(self, active: TestSuiteVersion | None = None) -> None:
        self.active = active
        self.activated: list[TestSuiteVersion] = []

    async def get_active_version(
        self, skill_id: str, skill_version_ref: str | None = None
    ) -> TestSuiteVersion | None:
        if self.active is None:
            return None
        if skill_version_ref is not None and self.active.skill_version_ref != skill_version_ref:
            return None
        return self.active

    async def activate_new_version(self, version: TestSuiteVersion) -> None:
        self.activated.append(version)
        self.active = version


class FakeCaseRepo:
    def __init__(self) -> None:
        self.saved: list[TestCase] = []

    async def save_many(self, cases: list[TestCase]) -> None:
        self.saved.extend(cases)


def _service(
    llm: CountingLLMClient, active: TestSuiteVersion | None = None
) -> tuple[TestSuiteService, FakeSuiteRepo, FakeCaseRepo]:
    suite_repo = FakeSuiteRepo(active)
    case_repo = FakeCaseRepo()
    service = TestSuiteService(
        generator=GeneratorAgent(model="claude-haiku-4-5", llm_client=llm),
        test_suite_repo=suite_repo,  # type: ignore[arg-type]
        test_case_repo=case_repo,  # type: ignore[arg-type]
    )
    return service, suite_repo, case_repo


def _existing_version(version_ref: str = "v1") -> TestSuiteVersion:
    return TestSuiteVersion(
        suite_version_id="suite-1",
        skill_id="csv-cleaner",
        skill_version_ref=version_ref,
        generation_mode=GenerationMode.REUSE.value,
        case_ids=["c1", "c2"],
        created_at=datetime.now(UTC),
        is_active=True,
    )


class KeywordExtractionTests:
    def test_description_terms_outrank_body(self) -> None:
        keywords = extract_keywords(_skill())
        assert "csv" in keywords
        # 停用词不应污染反向用例的关键词锚点。
        assert "的" not in keywords
        assert "使用" not in keywords


class GeneratorAgentTests:
    @pytest.mark.asyncio
    async def test_generates_both_categories_with_one_call_each(self) -> None:
        llm = CountingLLMClient(cases_per_call=3)
        agent = GeneratorAgent(model="claude-haiku-4-5", llm_client=llm)

        cases = await agent.generate(
            GenerationRequest(skill=_skill(), mode=GenerationMode.REUSE, triggered_by="t"),
            generator_run_id="gen-1",
        )

        assert llm.call_count == 2  # positive + negative 各一次
        assert len(cases) == 6
        assert {c.category for c in cases} == {
            TestCaseCategory.POSITIVE,
            TestCaseCategory.NEGATIVE,
        }
        assert all(c.generator_run_id == "gen-1" for c in cases)

    @pytest.mark.asyncio
    async def test_focus_constraints_are_injected_into_prompt(self) -> None:
        llm = CountingLLMClient()
        agent = GeneratorAgent(model="claude-haiku-4-5", llm_client=llm)
        focus = CapabilityFocus(
            capability_ids=["cap-7"], descriptions={"cap-7": "处理带 BOM 头的文件"}
        )

        await agent.generate(
            GenerationRequest(
                skill=_skill(),
                mode=GenerationMode.INCREMENTAL_PATCH,
                capability_focus=focus,
                triggered_by="coverage_gap",
            ),
            generator_run_id="gen-2",
        )

        # 必须把人类可读描述喂给模型，光给 id 对模型没有信息量。
        assert "处理带 BOM 头的文件" in llm.prompts[0]

    @pytest.mark.asyncio
    async def test_unsupported_category_names_the_owning_document(self) -> None:
        agent = GeneratorAgent(model="claude-haiku-4-5", llm_client=CountingLLMClient())
        request = GenerationRequest(
            skill=_skill(),
            mode=GenerationMode.REUSE,
            categories=[TestCaseCategory.ADVERSARIAL],
            triggered_by="t",
        )

        with pytest.raises(GenerationError, match="docs/dev/15"):
            await agent.generate(request, generator_run_id="gen-3")


class DatasetSplitTests:
    def test_sixty_forty_split_per_category(self) -> None:
        cases = [
            TestCase(
                case_id=f"c{i}",
                skill_id="s",
                category=TestCaseCategory.POSITIVE,
                split=DatasetSplit.TRAIN,
                prompt="p",
                generator_run_id="g",
                created_at=datetime.now(UTC),
            )
            for i in range(10)
        ]

        _split_dataset(cases, skill_id="s")

        assert sum(c.split == DatasetSplit.TRAIN for c in cases) == 6
        assert sum(c.split == DatasetSplit.VALIDATION for c in cases) == 4

    def test_split_is_deterministic_for_the_same_skill(self) -> None:
        def build() -> list[TestCase]:
            return [
                TestCase(
                    case_id=f"c{i}",
                    skill_id="s",
                    category=TestCaseCategory.POSITIVE,
                    split=DatasetSplit.TRAIN,
                    prompt="p",
                    generator_run_id="g",
                    created_at=datetime.now(UTC),
                )
                for i in range(10)
            ]

        first = build()
        second = build()
        _split_dataset(first, skill_id="csv-cleaner")
        _split_dataset(second, skill_id="csv-cleaner")

        assert [c.split for c in first] == [c.split for c in second]


class TestSuiteLifecycleTests:
    @pytest.mark.asyncio
    async def test_reuse_hits_cache_without_calling_llm(self) -> None:
        llm = CountingLLMClient()
        service, _, _ = _service(llm, active=_existing_version("v1"))

        result = await service.ensure_test_suite(_skill("v1"))

        assert llm.call_count == 0  # 关键约束：复用路径一次 LLM 都不调
        assert result.generated is False
        assert result.staleness_warning is None
        assert result.suite_version.suite_version_id == "suite-1"

    @pytest.mark.asyncio
    async def test_version_drift_warns_but_does_not_regenerate(self) -> None:
        llm = CountingLLMClient()
        service, _, _ = _service(llm, active=_existing_version("v1"))

        result = await service.ensure_test_suite(_skill("v2"))

        # 版本漂移**不构成**自动重新生成的理由，只告警（docs/dev/06 第 4.1 节）。
        assert llm.call_count == 0
        assert result.generated is False
        assert result.staleness_warning is not None
        assert "--force" in result.staleness_warning
        assert result.suite_version.suite_version_id == "suite-1"

    @pytest.mark.asyncio
    async def test_first_run_bootstraps_and_splits(self) -> None:
        llm = CountingLLMClient(cases_per_call=5)
        service, suite_repo, case_repo = _service(llm, active=None)

        result = await service.ensure_test_suite(_skill())

        assert result.generated is True
        assert llm.call_count == 2
        assert len(case_repo.saved) == 10
        assert len(suite_repo.activated) == 1
        assert suite_repo.activated[0].generation_mode == GenerationMode.REUSE.value
        assert {c.split for c in case_repo.saved} == {DatasetSplit.TRAIN, DatasetSplit.VALIDATION}

    @pytest.mark.asyncio
    async def test_force_regenerate_always_calls_llm(self) -> None:
        llm = CountingLLMClient()
        service, _, _ = _service(llm, active=_existing_version("v1"))

        version = await service.force_regenerate(_skill("v1"))

        assert llm.call_count == 2
        assert version.generation_mode == GenerationMode.FORCE_REGENERATE.value
        # 全量重生：不继承旧 case_ids。
        assert "c1" not in version.case_ids

    @pytest.mark.asyncio
    async def test_incremental_patch_merges_old_and_new_cases(self) -> None:
        llm = CountingLLMClient(cases_per_call=1)
        service, _, _ = _service(llm, active=_existing_version("v1"))
        focus = CapabilityFocus(capability_ids=["cap-1"], negative_constraint_ids=["neg-1"])

        version = await service.incremental_patch(_skill("v1"), focus, triggered_by="coverage_gap")

        assert version.generation_mode == GenerationMode.INCREMENTAL_PATCH.value
        # 老用例保留（补盲区不该丢题），新用例追加。
        assert version.case_ids[:2] == ["c1", "c2"]
        assert len(version.case_ids) == 4

    @pytest.mark.asyncio
    async def test_incremental_patch_requires_non_empty_focus(self) -> None:
        service, _, _ = _service(CountingLLMClient(), active=_existing_version("v1"))

        with pytest.raises(GenerationError, match="非空的 CapabilityFocus"):
            await service.incremental_patch(_skill("v1"), CapabilityFocus(), triggered_by="x")

    @pytest.mark.asyncio
    async def test_incremental_patch_without_existing_suite_is_rejected(self) -> None:
        service, _, _ = _service(CountingLLMClient(), active=None)

        with pytest.raises(GenerationError, match="ensure_test_suite"):
            await service.incremental_patch(
                _skill(), CapabilityFocus(capability_ids=["cap-1"]), triggered_by="x"
            )


class GenerateCliTests:
    """`skill-evaluate generate` 的接线（docs/dev/06 第 4.2 节）。

    重点：**不带 `--force` 时永远走复用路径**——这是防止"CI 每跑一次就重新出一
    套题"的物理保障，接线接错了，上面所有 service 层的约束都白设。
    """

    @staticmethod
    def _skill_dir(tmp_path):  # type: ignore[no-untyped-def]
        (tmp_path / "SKILL.md").write_text(
            "---\nname: CSV Cleaner\ndescription: 清洗 CSV\n---\n\n# body\n", encoding="utf-8"
        )
        return tmp_path

    def _invoke(self, monkeypatch, tmp_path, args):  # type: ignore[no-untyped-def]
        import typer.testing

        from skill_evaluate import cli
        from skill_evaluate.agents import generator as generator_pkg

        calls: list[str] = []

        class SpyService:
            def __init__(self, **kwargs: object) -> None:
                pass

            async def ensure_test_suite(self, skill):  # type: ignore[no-untyped-def]
                calls.append("ensure")
                return generator_pkg.EnsureTestSuiteResult(
                    suite_version=_existing_version(skill.version_ref)
                )

            async def force_regenerate(self, skill, triggered_by="manual_cli", **kwargs):  # type: ignore[no-untyped-def]
                calls.append(f"force:{kwargs.get('positive_count')}")
                return _existing_version(skill.version_ref)

        monkeypatch.setattr(generator_pkg, "TestSuiteService", SpyService)
        monkeypatch.setattr(generator_pkg, "GeneratorAgent", lambda *a, **k: None)

        result = typer.testing.CliRunner().invoke(
            cli.app, ["generate", "--skill-path", str(self._skill_dir(tmp_path)), *args]
        )
        assert result.exit_code == 0, result.output
        return calls

    def test_default_path_reuses_without_forcing(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        assert self._invoke(monkeypatch, tmp_path, []) == ["ensure"]

    def test_force_flag_routes_to_force_regenerate_with_counts(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        calls = self._invoke(monkeypatch, tmp_path, ["--force", "--positive-count", "4"])
        assert calls == ["force:4"]


# --------------------------------------------------------------------------- #
# docs/dev/13 对本文档的两处扩展：类别-模板注册表 + extra_categories 的 REUSE 语义
# --------------------------------------------------------------------------- #


class GenerationTemplateRegistryTests:
    """类别 -> 模板从硬编码字典改成注册表（docs/dev/13 第 3.1 节的正式修订）。"""

    def test_builtin_categories_are_registered(self) -> None:
        from skill_evaluate.agents.generator.prompts.registry import registered_categories

        assert set(registered_categories()) == {
            TestCaseCategory.POSITIVE,
            TestCaseCategory.NEGATIVE,
            TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
            TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR,
        }

    def test_unregistered_category_names_the_owning_document(self) -> None:
        """未注册的类别必须显式报错，且指明该由哪份文档补齐——不能静默跳过。"""
        from skill_evaluate.agents.generator.prompts.registry import get_generation_template

        with pytest.raises(GenerationError, match="docs/dev/15"):
            get_generation_template(TestCaseCategory.ADVERSARIAL)

    def test_duplicate_registration_is_rejected(self) -> None:
        from skill_evaluate.agents.generator.prompts.registry import (
            register_generation_template,
        )

        with pytest.raises(GenerationError, match="重复注册"):
            register_generation_template(TestCaseCategory.POSITIVE, "positive.jinja")

    def test_missing_template_file_fails_at_registration_time(self) -> None:
        from skill_evaluate.agents.generator.prompts.registry import (
            register_generation_template,
        )

        with pytest.raises(GenerationError, match="不存在"):
            register_generation_template(TestCaseCategory.ADVERSARIAL, "nope.jinja")


class ProbeTargetResolutionTests:
    """渐进式披露触发探查用例的 `probe_target_reference` 回填（docs/dev/13 第 3.1 节）。"""

    def _skill_with_refs(self) -> SkillDefinition:
        from skill_evaluate.state.skill import SkillReferenceFile

        return _skill().model_copy(
            update={
                "reference_files": [
                    SkillReferenceFile(
                        path="references/errors.md", trigger_condition="执行报错时查阅"
                    )
                ]
            }
        )

    async def _generate(self, probe_target: str | None) -> list[TestCase]:
        class PdLLM(CountingLLMClient):
            async def complete(self, **kwargs: Any) -> LLMCompletion:  # type: ignore[override]
                payload = {
                    "cases": [
                        {
                            "prompt": "我跑的时候报了 KeyError，帮我看看",
                            "rationale": "命中报错时查阅的条件",
                            "diversity_tag": "pd_trigger",
                            "probe_target_reference": probe_target,
                            "target_capability_ids": [],
                            "negative_constraint_ids": [],
                            "expected_output": None,
                        }
                    ]
                }
                return LLMCompletion(
                    text=json.dumps(payload, ensure_ascii=False),
                    prompt_tokens=5,
                    completion_tokens=5,
                    model=kwargs["model"],
                    temperature_applied=None,
                )

        agent = GeneratorAgent(model="claude-haiku-4-5", llm_client=PdLLM())
        request = GenerationRequest(
            skill=self._skill_with_refs(),
            mode=GenerationMode.INCREMENTAL_PATCH,
            categories=[TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER],
            positive_count=0,
            negative_count=0,
            category_counts={TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER: 1},
            triggered_by="dimension_extra_categories",
        )
        return await agent.generate(request, generator_run_id="gen-pd")

    @pytest.mark.asyncio
    async def test_exact_path_is_kept(self) -> None:
        cases = await self._generate("references/errors.md")
        assert cases[0].probe_target_reference == "references/errors.md"

    @pytest.mark.asyncio
    async def test_basename_is_normalized_to_the_known_path(self) -> None:
        """模型常把路径写成裸文件名，按文件名兜底匹配比丢掉这条题划算。"""
        cases = await self._generate("errors.md")
        assert cases[0].probe_target_reference == "references/errors.md"

    @pytest.mark.asyncio
    async def test_unknown_target_degrades_to_none_without_failing_the_batch(self) -> None:
        """对不上号只让这条题测不出东西，不该把整批用例连坐作废。"""
        cases = await self._generate("references/whatever.md")
        assert cases[0].probe_target_reference is None
        assert len(cases) == 1

    @pytest.mark.asyncio
    async def test_other_categories_never_carry_a_probe_target(self) -> None:
        llm = CountingLLMClient(cases_per_call=1)
        agent = GeneratorAgent(model="claude-haiku-4-5", llm_client=llm)
        request = GenerationRequest(
            skill=self._skill_with_refs(),
            mode=GenerationMode.REUSE,
            categories=[TestCaseCategory.POSITIVE],
            negative_count=0,
            triggered_by="auto_bootstrap",
        )
        cases = await agent.generate(request, generator_run_id="gen-1")
        assert all(c.probe_target_reference is None for c in cases)


class ExtraCategoriesTests:
    """`ensure_test_suite(extra_categories=...)` 仍然是 REUSE 语义（docs/dev/13 第 3 节）。"""

    class _CaseRepo(FakeCaseRepo):
        def __init__(self, existing: list[TestCase] | None = None) -> None:
            super().__init__()
            self.existing = existing or []
            self.queries: list[list[TestCaseCategory]] = []

        async def list_by_categories(
            self, suite_version_id: str, categories: list[TestCaseCategory]
        ) -> list[TestCase]:
            self.queries.append(list(categories))
            return [c for c in self.existing if c.category in categories]

    def _service_with(
        self, llm: CountingLLMClient, case_repo: "ExtraCategoriesTests._CaseRepo"
    ) -> tuple[TestSuiteService, FakeSuiteRepo]:
        suite_repo = FakeSuiteRepo(_existing_version("v1"))
        service = TestSuiteService(
            generator=GeneratorAgent(model="claude-haiku-4-5", llm_client=llm),
            test_suite_repo=suite_repo,  # type: ignore[arg-type]
            test_case_repo=case_repo,  # type: ignore[arg-type]
        )
        return service, suite_repo

    def _pd_case(self, category: TestCaseCategory) -> TestCase:
        return TestCase(
            case_id=f"pd-{category.value}",
            skill_id="csv-cleaner",
            category=category,
            split=DatasetSplit.TRAIN,
            prompt="题面",
            generator_run_id="gen-0",
            created_at=datetime.now(UTC),
        )

    @pytest.mark.asyncio
    async def test_existing_extra_categories_are_reused_without_calling_llm(self) -> None:
        case_repo = self._CaseRepo(
            [
                self._pd_case(TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER),
                self._pd_case(TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR),
            ]
        )
        llm = CountingLLMClient()
        service, _ = self._service_with(llm, case_repo)

        result = await service.ensure_test_suite(
            _skill("v1"),
            extra_categories=[
                TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER,
                TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR,
            ],
            category_counts={
                TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER: 2,
                TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR: 3,
            },
        )
        assert llm.call_count == 0
        assert result.generated is False
        assert result.suite_version.suite_version_id == "suite-1"

    @pytest.mark.asyncio
    async def test_missing_extra_categories_generate_only_themselves(self) -> None:
        """补的是缺的那两个类别，正/反向用例原样继承——否则就是变相的强制重生。"""
        case_repo = self._CaseRepo([])
        llm = CountingLLMClient(cases_per_call=2)
        service, suite_repo = self._service_with(llm, case_repo)

        result = await service.ensure_test_suite(
            _skill("v1"),
            extra_categories=[TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER],
            category_counts={TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER: 2},
        )
        assert llm.call_count == 1  # 只为缺的那一个类别调了一次
        assert result.generated is True
        new_version = suite_repo.activated[-1]
        assert new_version.generation_mode == GenerationMode.INCREMENTAL_PATCH.value
        # 旧用例继承，新用例追加。
        assert new_version.case_ids[:2] == ["c1", "c2"]
        assert len(new_version.case_ids) == 4
        assert {c.category for c in case_repo.saved} == {
            TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER
        }

    @pytest.mark.asyncio
    async def test_zero_requested_count_skips_the_llm_entirely(self) -> None:
        """没有 references/ 的 Skill 一条探查题都出不了，不该为此发一次注定出 0 条的请求。"""
        case_repo = self._CaseRepo([])
        llm = CountingLLMClient()
        service, _ = self._service_with(llm, case_repo)

        result = await service.ensure_test_suite(
            _skill("v1"),
            extra_categories=[TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER],
            category_counts={TestCaseCategory.PROGRESSIVE_DISCLOSURE_TRIGGER: 0},
        )
        assert llm.call_count == 0
        assert result.generated is False

    @pytest.mark.asyncio
    async def test_stale_suite_still_gets_missing_categories_topped_up(self) -> None:
        """版本漂移不等于这个类别出过题：漂移告警照发，缺的类别照补。"""
        case_repo = self._CaseRepo([])
        llm = CountingLLMClient(cases_per_call=1)
        service, _ = self._service_with(llm, case_repo)

        result = await service.ensure_test_suite(
            _skill("v2"),
            extra_categories=[TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR],
            category_counts={TestCaseCategory.PROGRESSIVE_DISCLOSURE_REGULAR: 3},
        )
        assert result.staleness_warning is not None
        assert result.generated is True
        assert llm.call_count == 1
