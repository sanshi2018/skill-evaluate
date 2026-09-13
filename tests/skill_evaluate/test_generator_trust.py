"""docs/dev/21 Part A/B：生成坍塌监控与种子锚点。

不碰库、不发真实 embedding 请求：embedding 用"关键词 → 固定方向"的确定性替身，
仓储全部用内存替身。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from skill_evaluate.agents.embedding import EmbeddingError, OpenRouterEmbeddingClient
from skill_evaluate.agents.generator import GenerationRequest, GeneratorAgent, TestSuiteService
from skill_evaluate.agents.generator.collapse_detector import (
    CollapseAssessment,
    GenerationCollapseDetector,
    current_collapse_threshold,
)
from skill_evaluate.agents.generator.seed_anchors import (
    SeedAnchor,
    SeedAnchorLibrary,
    SeedAnchorLibraryError,
    SeedAnchorResolver,
)
from skill_evaluate.agents.generator.service import ALERT_TYPE_GENERATION_COLLAPSE
from skill_evaluate.agents.llm import LLMCompletion
from skill_evaluate.config import GeneratorTrustSettings, LLMSettings, Settings
from skill_evaluate.errors import ConfigurationError, GenerationCollapseError, GenerationError
from skill_evaluate.observability.alerts import LoggingAlertDispatcher
from skill_evaluate.state.enums import (
    CollapseReason,
    DatasetSplit,
    GenerationMode,
    TestCaseCategory,
)
from skill_evaluate.state.generator_trust import GenerationCollapseEvent
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase, TestSuiteVersion

DIM = 8


def _skill() -> SkillDefinition:
    return SkillDefinition(
        skill_id="csv-cleaner",
        version_ref="v1",
        root_path=".",
        description="清洗 CSV 导出文件",
        body_markdown="# CSV Cleaner",
        line_count=1,
        token_count=5,
    )


def _case(case_id: str, prompt: str, *, age_s: int = 0) -> TestCase:
    return TestCase(
        case_id=case_id,
        skill_id="csv-cleaner",
        category=TestCaseCategory.POSITIVE,
        split=DatasetSplit.TRAIN,
        prompt=prompt,
        generator_run_id="g",
        created_at=datetime.now(UTC) - timedelta(seconds=age_s),
    )


def _axis(index: int, noise: float = 0.0) -> list[float]:
    vec = [0.0] * DIM
    vec[index % DIM] = 1.0
    vec[(index + 1) % DIM] = noise
    return vec


class AxisEmbedding:
    """prompt 形如 `axis:<n>[:noise]` → 第 n 维为 1 的向量；便于精确构造距离。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for text in texts:
            parts = text.split(":")
            out.append(_axis(int(parts[1]), float(parts[2]) if len(parts) > 2 else 0.0))
        return out


class MemoryEmbeddingRepo:
    def __init__(self, history: dict[str, list[float]] | None = None) -> None:
        self.rows: dict[str, list[float]] = dict(history or {})
        self.saved: list[dict[str, list[float]]] = []

    async def save_many(
        self, *, skill_id: str, embedding_model: str, vectors: dict[str, list[float]]
    ) -> None:
        self.saved.append(dict(vectors))
        self.rows.update(vectors)

    async def get_recent(
        self,
        *,
        skill_id: str,
        embedding_model: str,
        exclude_case_ids: list[str] | None = None,
        limit: int = 50,
    ) -> list[list[float]]:
        excluded = set(exclude_case_ids or [])
        return [v for k, v in self.rows.items() if k not in excluded][:limit]

    async def count_by_skill(self, *, skill_id: str, embedding_model: str) -> int:
        return len(self.rows)

    async def missing_case_ids(self, case_ids: list[str], *, embedding_model: str) -> list[str]:
        return [c for c in case_ids if c not in self.rows]


class MemoryCaseRepo:
    def __init__(self, cases: list[TestCase] | None = None) -> None:
        self.cases = {c.case_id: c for c in cases or []}
        self.saved: list[TestCase] = []

    async def list_by_ids(self, case_ids: list[str]) -> list[TestCase]:
        return [self.cases[c] for c in case_ids if c in self.cases]

    async def save_many(self, cases: list[TestCase]) -> None:
        self.saved.extend(cases)


def _detector(
    history: dict[str, list[float]] | None = None,
    *,
    cases: list[TestCase] | None = None,
    **overrides: Any,
) -> tuple[GenerationCollapseDetector, AxisEmbedding, MemoryEmbeddingRepo]:
    embedding = AxisEmbedding()
    repo = MemoryEmbeddingRepo(history)
    detector = GenerationCollapseDetector(
        embedding_client=embedding,
        embedding_repository=repo,  # type: ignore[arg-type]
        test_case_repository=MemoryCaseRepo(cases),  # type: ignore[arg-type]
        settings=GeneratorTrustSettings(**overrides),
    )
    return detector, embedding, repo


# --------------------------------------------------------------------------- #
# 弹性阈值与检测器
# --------------------------------------------------------------------------- #


class CollapseThresholdTests:
    def test_linear_interpolation_between_initial_and_mature(self) -> None:
        cfg = GeneratorTrustSettings()
        assert current_collapse_threshold(0, cfg) == pytest.approx(0.15)
        assert current_collapse_threshold(100, cfg) == pytest.approx(0.25)
        assert current_collapse_threshold(200, cfg) == pytest.approx(0.35)
        # 超过成熟样本量不再继续收紧
        assert current_collapse_threshold(10_000, cfg) == pytest.approx(0.35)


class CollapseDetectorTests:
    @pytest.mark.asyncio
    async def test_cold_start_passes_diverse_batch_without_history(self) -> None:
        detector, _, repo = _detector()
        cases = [_case(f"n{i}", f"axis:{i}") for i in range(4)]

        result = await detector.assess(cases)

        assert result.passed is True
        assert result.reason is CollapseReason.COLD_START
        assert result.intra_batch_distance == pytest.approx(1.0)
        assert repo.saved == []  # assess 只算不写

    @pytest.mark.asyncio
    async def test_near_duplicate_batch_is_blocked_even_on_cold_start(self) -> None:
        # 原文只做新旧对比，首次生成必然冷启动放行——批内检查补上这个口子。
        detector, _, _ = _detector()
        cases = [_case(f"n{i}", "axis:0:0.01") for i in range(5)]

        result = await detector.assess(cases)

        assert result.passed is False
        assert result.reason is CollapseReason.COLLAPSED_INTRA_BATCH

    @pytest.mark.asyncio
    async def test_small_batch_skips_intra_check(self) -> None:
        detector, _, _ = _detector()
        cases = [_case(f"n{i}", "axis:0") for i in range(3)]  # < min_batch_size_for_intra_check

        result = await detector.assess(cases)

        assert result.passed is True
        assert result.intra_batch_distance is None

    @pytest.mark.asyncio
    async def test_batch_hugging_history_is_blocked(self) -> None:
        history = {f"h{i}": _axis(0) for i in range(10)}
        detector, _, _ = _detector(history)
        # 3 条 < 批内检查最小批量，只走新旧对比：整体贴着历史的 0 轴
        cases = [_case(f"n{i}", f"axis:0:{0.05 * (i % 2)}") for i in range(3)]

        result = await detector.assess(cases)

        assert result.passed is False
        assert result.reason is CollapseReason.COLLAPSED_VS_HISTORY
        assert result.avg_distance_to_history is not None
        assert result.avg_distance_to_history < result.threshold

    @pytest.mark.asyncio
    async def test_diverse_batch_passes_and_persist_writes_vectors(self) -> None:
        history = {f"h{i}": _axis(0) for i in range(10)}
        detector, _, repo = _detector(history)
        cases = [_case(f"n{i}", f"axis:{i + 1}") for i in range(4)]

        result = await detector.assess(cases)
        await detector.persist(result)

        assert result.passed is True
        assert result.reason is CollapseReason.DIVERSE
        assert set(repo.saved[-1]) == {"n0", "n1", "n2", "n3"}

    @pytest.mark.asyncio
    async def test_persist_never_writes_a_rejected_batch(self) -> None:
        detector, _, repo = _detector()
        cases = [_case(f"n{i}", "axis:0") for i in range(5)]

        result = await detector.assess(cases)
        await detector.persist(result)

        assert result.passed is False
        assert repo.saved == []  # 废题不进历史分布

    @pytest.mark.asyncio
    async def test_disabled_check_passes_without_embedding(self) -> None:
        detector, embedding, _ = _detector(collapse_check_enabled=False)

        result = await detector.assess([_case("n0", "axis:0")])

        assert result.passed is True
        assert result.reason is CollapseReason.DISABLED
        assert embedding.calls == []

    @pytest.mark.asyncio
    async def test_empty_batch_is_rejected(self) -> None:
        detector, _, _ = _detector()
        result = await detector.assess([])
        assert result.passed is False
        assert result.reason is CollapseReason.EMPTY_BATCH

    @pytest.mark.asyncio
    async def test_inherited_cases_without_vectors_are_backfilled(self) -> None:
        legacy = [_case(f"old{i}", f"axis:{i}", age_s=i) for i in range(6)]
        detector, _, repo = _detector(cases=legacy)

        await detector.assess(
            [_case("n0", "axis:7")], inherited_case_ids=[c.case_id for c in legacy]
        )

        assert set(repo.saved[0]) == {c.case_id for c in legacy}


# --------------------------------------------------------------------------- #
# TestSuiteService 集成：阻断激活、事件、连续坍塌告警
# --------------------------------------------------------------------------- #


class FixedLLM:
    async def complete(self, *, prompt: str, model: str, **_: Any) -> LLMCompletion:
        payload = {
            "cases": [
                {
                    "prompt": f"题 {i}",
                    "rationale": "r",
                    "diversity_tag": "colloquial",
                    "target_capability_ids": [],
                    "negative_constraint_ids": [],
                    "expected_output": None,
                    "probe_target_reference": None,
                    "seed_anchor_id": None,
                }
                for i in range(2)
            ]
        }
        return LLMCompletion(
            text=json.dumps(payload, ensure_ascii=False),
            prompt_tokens=1,
            completion_tokens=1,
            model=model,
            temperature_applied=None,
        )


class StubDetector:
    def __init__(self, passed: bool) -> None:
        self.passed = passed
        self.persisted: list[CollapseAssessment] = []
        self.order: list[str]

    async def assess(
        self, new_cases: list[TestCase], *, inherited_case_ids: list[str] | None = None
    ) -> CollapseAssessment:
        return CollapseAssessment(
            passed=self.passed,
            reason=CollapseReason.DIVERSE if self.passed else CollapseReason.COLLAPSED_VS_HISTORY,
            threshold=0.2,
            historical_count=30,
            new_case_count=len(new_cases),
            skill_id="csv-cleaner",
            avg_distance_to_history=0.05,
        )

    async def persist(self, assessment: CollapseAssessment) -> None:
        self.order.append("persist")
        self.persisted.append(assessment)


class SuiteRepo:
    def __init__(self, active: TestSuiteVersion | None) -> None:
        self.active = active
        self.activated: list[TestSuiteVersion] = []

    async def get_active_version(
        self, skill_id: str, skill_version_ref: str | None = None
    ) -> TestSuiteVersion | None:
        return self.active

    async def activate_new_version(self, version: TestSuiteVersion) -> None:
        self.activated.append(version)


class OrderedCaseRepo(MemoryCaseRepo):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self.order = order

    async def save_many(self, cases: list[TestCase]) -> None:
        self.order.append("save_cases")
        await super().save_many(cases)


class EventRepo:
    def __init__(self) -> None:
        self.events: list[GenerationCollapseEvent] = []
        self.since_calls: list[datetime | None] = []

    async def record(self, event: GenerationCollapseEvent) -> None:
        self.events.append(event)

    async def count_since(self, *, skill_id: str, since: datetime | None) -> int:
        self.since_calls.append(since)
        return len(self.events)


def _service(
    detector: StubDetector, active: TestSuiteVersion | None = None
) -> tuple[TestSuiteService, SuiteRepo, OrderedCaseRepo, EventRepo, LoggingAlertDispatcher]:
    order: list[str] = []
    detector.order = order
    suite_repo = SuiteRepo(active)
    case_repo = OrderedCaseRepo(order)
    events = EventRepo()
    alerts = LoggingAlertDispatcher()
    service = TestSuiteService(
        generator=GeneratorAgent(
            model="m", llm_client=FixedLLM(), seed_anchor_resolver=NullResolver()
        ),
        test_suite_repo=suite_repo,  # type: ignore[arg-type]
        test_case_repo=case_repo,  # type: ignore[arg-type]
        collapse_detector=detector,
        collapse_event_repo=events,  # type: ignore[arg-type]
        alert_dispatcher=alerts,
    )
    return service, suite_repo, case_repo, events, alerts


class NullResolver:
    def resolve_ids(self, anchor_ids: list[str]) -> list[SeedAnchor]:
        return []

    async def resolve_for_skill(self, skill: SkillDefinition, count: int) -> list[SeedAnchor]:
        return []


class CollapseGateServiceTests:
    @pytest.mark.asyncio
    async def test_collapsed_batch_is_not_saved_or_activated(self) -> None:
        detector = StubDetector(passed=False)
        service, suite_repo, case_repo, events, _ = _service(detector)

        with pytest.raises(GenerationCollapseError) as info:
            await service.force_regenerate(_skill())

        assert isinstance(info.value, GenerationError)  # 既有调用方按父类接住即可降级
        assert suite_repo.activated == []
        assert case_repo.saved == []
        assert detector.persisted == []
        assert len(events.events) == 1
        assert events.events[0].reason is CollapseReason.COLLAPSED_VS_HISTORY
        assert events.events[0].triggered_by == "manual_cli"
        assert info.value.consecutive_collapses == 1
        assert info.value.requires_human_seed is False

    @pytest.mark.asyncio
    async def test_consecutive_count_is_scoped_to_active_version(self) -> None:
        created = datetime(2026, 9, 1, tzinfo=UTC)
        active = TestSuiteVersion(
            suite_version_id="s1",
            skill_id="csv-cleaner",
            skill_version_ref="v1",
            generation_mode="reuse",
            case_ids=[],
            created_at=created,
        )
        service, _, _, events, _ = _service(StubDetector(passed=False), active)

        with pytest.raises(GenerationCollapseError):
            await service.force_regenerate(_skill())

        assert events.since_calls == [created]

    @pytest.mark.asyncio
    async def test_alert_fires_exactly_once_when_limit_reached(self) -> None:
        service, _, _, _, alerts = _service(StubDetector(passed=False))
        errors: list[GenerationCollapseError] = []
        for _ in range(4):
            with pytest.raises(GenerationCollapseError) as info:
                await service.force_regenerate(_skill())
            errors.append(info.value)

        assert [e.requires_human_seed for e in errors] == [False, False, True, True]
        assert len(alerts.sent) == 1  # 第 3 次叫人，第 4 次不重复轰炸
        assert alerts.sent[0]["alert_type"] == ALERT_TYPE_GENERATION_COLLAPSE
        payload = alerts.sent[0]["payload"]
        assert payload["consecutive_collapses"] == 3
        json.dumps(payload)  # 约定：payload 必须 JSON 可序列化

    @pytest.mark.asyncio
    async def test_passing_batch_persists_vectors_after_cases_are_saved(self) -> None:
        detector = StubDetector(passed=True)
        service, suite_repo, _, events, _ = _service(detector)

        await service.force_regenerate(_skill())

        assert detector.order == ["save_cases", "persist"]  # 外键：先落用例再落向量
        assert len(suite_repo.activated) == 1
        assert events.events == []


# --------------------------------------------------------------------------- #
# embedding 客户端
# --------------------------------------------------------------------------- #


def _settings_with_key() -> Settings:
    return Settings(
        llm=LLMSettings(api_key="sk-or-test", base_url="https://gateway.test/api/v1"),
        generator_trust=GeneratorTrustSettings(embedding_dimensions=3, embedding_batch_size=2),
    )


class EmbeddingClientTests:
    @pytest.mark.asyncio
    async def test_batches_and_reorders_by_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "skill_evaluate.agents.embedding.get_settings", lambda: _settings_with_key()
        )
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            bodies.append(body)
            assert request.url.path.endswith("/embeddings")
            assert request.headers["Authorization"] == "Bearer sk-or-test"
            data = [
                {"index": i, "embedding": [float(len(text)), 0.0, 1.0]}
                for i, text in enumerate(body["input"])
            ]
            return httpx.Response(200, json={"data": list(reversed(data))})

        client = OpenRouterEmbeddingClient(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        vectors = await client.embed(["a", "bb", "ccc"])

        assert [v[0] for v in vectors] == [1.0, 2.0, 3.0]
        assert [len(b["input"]) for b in bodies] == [2, 1]
        assert bodies[0]["dimensions"] == 3

    @pytest.mark.asyncio
    async def test_dimension_mismatch_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "skill_evaluate.agents.embedding.get_settings", lambda: _settings_with_key()
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

        client = OpenRouterEmbeddingClient(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        with pytest.raises(EmbeddingError, match="维度不符"):
            await client.embed(["a"])

    @pytest.mark.asyncio
    async def test_missing_api_key_is_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "skill_evaluate.agents.embedding.get_settings",
            lambda: Settings(llm=LLMSettings(api_key="")),
        )
        with pytest.raises(ConfigurationError):
            await OpenRouterEmbeddingClient().embed(["a"])


# --------------------------------------------------------------------------- #
# 种子锚点库与注入
# --------------------------------------------------------------------------- #


def _write_library(root: Path) -> None:
    (root / "anchors").mkdir(parents=True)
    (root / "manifest.yaml").write_text(
        "- domain_tag: data\n  description: 数据类\n  source: 脱敏工单\n"
        "- domain_tag: web\n  description: 前端类\n  source: 脱敏工单\n",
        encoding="utf-8",
    )
    (root / "anchors" / "data.yaml").write_text(
        "- id: d1\n  prompt: axis:1\n- id: d2\n  prompt: axis:2\n", encoding="utf-8"
    )
    (root / "anchors" / "web.yaml").write_text("- id: w1\n  prompt: axis:5\n", encoding="utf-8")


class SeedAnchorTests:
    def test_library_loads_anchors_with_namespaced_ids(self, tmp_path: Path) -> None:
        _write_library(tmp_path)
        library = SeedAnchorLibrary(tmp_path)

        anchors = library.anchors()

        assert [a.anchor_id for a in anchors] == ["data/d1", "data/d2", "web/w1"]
        assert anchors[0].ref == "data/d1@unknown"  # 非 git 目录，sha 如实记 unknown

    def test_manifest_pointing_to_missing_file_is_a_structural_error(self, tmp_path: Path) -> None:
        (tmp_path / "manifest.yaml").write_text("- domain_tag: ghost\n", encoding="utf-8")
        with pytest.raises(SeedAnchorLibraryError, match="不存在"):
            SeedAnchorLibrary(tmp_path).anchors()

    def test_unknown_ids_are_ignored(self, tmp_path: Path) -> None:
        _write_library(tmp_path)
        anchors = SeedAnchorLibrary(tmp_path).get_by_ids(["web/w1", "nope/x"])
        assert [a.anchor_id for a in anchors] == ["web/w1"]

    @pytest.mark.asyncio
    async def test_resolver_ranks_by_similarity_to_description(self, tmp_path: Path) -> None:
        _write_library(tmp_path)
        embedding = AxisEmbedding()
        resolver = SeedAnchorResolver(
            library=SeedAnchorLibrary(tmp_path),
            embedding_client=embedding,
            settings=GeneratorTrustSettings(),
        )
        skill = _skill().model_copy(update={"description": "axis:5:0.1"})

        first = await resolver.resolve_for_skill(skill, 2)
        await resolver.resolve_for_skill(skill, 2)

        assert first[0].anchor_id == "web/w1"
        # 锚点向量按 commit 缓存：第二次只 embed 查询文本
        assert [len(c) for c in embedding.calls] == [3, 1, 1]

    @pytest.mark.asyncio
    async def test_unavailable_library_returns_nothing_without_embedding(
        self, tmp_path: Path
    ) -> None:
        embedding = AxisEmbedding()
        resolver = SeedAnchorResolver(
            library=SeedAnchorLibrary(tmp_path / "missing"), embedding_client=embedding
        )
        assert await resolver.resolve_for_skill(_skill(), 5) == []
        assert embedding.calls == []

    @pytest.mark.asyncio
    async def test_embedding_failure_degrades_to_no_anchors(self, tmp_path: Path) -> None:
        _write_library(tmp_path)

        class Broken:
            async def embed(self, texts: Sequence[str]) -> list[list[float]]:
                raise EmbeddingError("down")

        resolver = SeedAnchorResolver(
            library=SeedAnchorLibrary(tmp_path), embedding_client=Broken()
        )
        assert await resolver.resolve_for_skill(_skill(), 5) == []


class RecordingLLM:
    def __init__(self, seed_anchor_id: str | None) -> None:
        self.prompts: list[str] = []
        self.seed_anchor_id = seed_anchor_id

    async def complete(self, *, prompt: str, model: str, **_: Any) -> LLMCompletion:
        self.prompts.append(prompt)
        payload = {
            "cases": [
                {
                    "prompt": "帮我去重",
                    "rationale": "r",
                    "diversity_tag": "colloquial",
                    "target_capability_ids": [],
                    "negative_constraint_ids": [],
                    "expected_output": None,
                    "probe_target_reference": None,
                    "seed_anchor_id": self.seed_anchor_id,
                }
            ]
        }
        return LLMCompletion(
            text=json.dumps(payload, ensure_ascii=False),
            prompt_tokens=1,
            completion_tokens=1,
            model=model,
            temperature_applied=None,
        )


class FixedResolver:
    def __init__(self) -> None:
        self.anchor = SeedAnchor(
            anchor_id="data/d1", domain_tag="data", prompt="表里重复行太多了", commit_sha="abc123"
        )
        self.auto_calls = 0

    def resolve_ids(self, anchor_ids: list[str]) -> list[SeedAnchor]:
        return [self.anchor] if "data/d1" in anchor_ids else []

    async def resolve_for_skill(self, skill: SkillDefinition, count: int) -> list[SeedAnchor]:
        self.auto_calls += 1
        return [self.anchor]


class SeedInjectionTests:
    def _request(self, **kwargs: Any) -> GenerationRequest:
        return GenerationRequest(
            skill=_skill(),
            mode=GenerationMode.REUSE,
            categories=[TestCaseCategory.POSITIVE],
            positive_count=1,
            triggered_by="t",
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_anchor_is_injected_and_traced_back_with_commit(self) -> None:
        llm = RecordingLLM(seed_anchor_id="[data/d1]")  # 容忍模型把方括号抄回来
        resolver = FixedResolver()
        agent = GeneratorAgent(model="m", llm_client=llm, seed_anchor_resolver=resolver)

        cases = await agent.generate(self._request(), generator_run_id="g")

        assert "[data/d1] 表里重复行太多了" in llm.prompts[0]
        assert resolver.auto_calls == 1
        assert cases[0].seed_anchor_id == "data/d1@abc123"

    @pytest.mark.asyncio
    async def test_fabricated_anchor_id_is_not_recorded(self) -> None:
        agent = GeneratorAgent(
            model="m",
            llm_client=RecordingLLM(seed_anchor_id="data/made-up"),
            seed_anchor_resolver=FixedResolver(),
        )
        cases = await agent.generate(self._request(), generator_run_id="g")
        assert cases[0].seed_anchor_id is None

    @pytest.mark.asyncio
    async def test_explicit_empty_ids_disable_auto_resolution(self) -> None:
        llm = RecordingLLM(seed_anchor_id=None)
        resolver = FixedResolver()
        agent = GeneratorAgent(model="m", llm_client=llm, seed_anchor_resolver=resolver)

        await agent.generate(self._request(seed_anchor_ids=[]), generator_run_id="g")

        assert resolver.auto_calls == 0
        assert "真实种子锚点" not in llm.prompts[0]
