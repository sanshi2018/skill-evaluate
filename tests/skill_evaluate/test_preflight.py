"""docs/dev/21 Part C/D：沙箱环境指纹门禁与金丝雀探针。

不起沙箱、不碰库：执行后端、探测通道、探针历史仓储全部用替身。探测脚本本身在本机 `sh`
下真跑一次，验证它产出的是可解析的指纹 JSON。
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from langgraph.errors import GraphInterrupt

from skill_evaluate.config import PreflightSettings
from skill_evaluate.errors import (
    ExecutorBackendError,
    InfrastructureEnvironmentError,
    PipelineSuspended,
)
from skill_evaluate.executors.base import ExecutionRequest, ExecutorBackend
from skill_evaluate.executors.canary import (
    build_canary_request,
    expected_canary_content,
    load_canary_skill,
    run_canary_probe,
    verify_canary_trace,
)
from skill_evaluate.executors.hermes_backend import (
    EnvironmentProbeResult,
    HermesBackend,
    HermesSandboxHandle,
    UnconfiguredHermesSandboxClient,
    build_failure_trace,
)
from skill_evaluate.executors.routing import resolve_backend_type
from skill_evaluate.nodes.preflight import (
    GATE_CANARY,
    GATE_FINGERPRINT,
    KEY_CANARY_OUTCOME,
    KEY_FINGERPRINT_OUTCOME,
    PreflightDeps,
    PreflightPipeline,
    SandboxFingerprint,
    build_preflight_subgraph,
    diff_fingerprint,
    dump_fingerprint,
)
from skill_evaluate.nodes.preflight.fingerprint import PROBE_SCRIPT_PATH, parse_probe_output
from skill_evaluate.state.enums import ExecutorBackendType
from skill_evaluate.state.generator_trust import CanaryProbeRecord
from skill_evaluate.state.trace import ExecutionTrace, TimingCostMetrics

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _fingerprint(**overrides: Any) -> SandboxFingerprint:
    base: dict[str, Any] = {
        "os_kernel": "Linux 6.8.0 | debian 12",
        "runtime_versions": {"python": "Python 3.13.1", "node": "v22.1.0"},
        "key_env_vars_snapshot": {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
        "core_package_hashes": {"python_packages": "aaa"},
    }
    base.update(overrides)
    return SandboxFingerprint(**base)


def _probe_ok(fp: SandboxFingerprint) -> EnvironmentProbeResult:
    return EnvironmentProbeResult(exit_code=0, stdout=fp.model_dump_json())


class FakeProbeRunner:
    def __init__(self, result: EnvironmentProbeResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, int]] = []

    async def run_environment_probe(
        self, *, script_content: str, timeout_s: int
    ) -> EnvironmentProbeResult:
        self.calls.append((script_content, timeout_s))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _trace(final_response: str, *, loaded: bool = True) -> ExecutionTrace:
    return ExecutionTrace(
        trace_id="t-1",
        case_id="__canary__:run-1",
        run_index=0,
        backend_type=ExecutorBackendType.PLUGGABLE.value,
        loaded_skill_md=loaded,
        timing=TimingCostMetrics(
            total_tokens=1, prompt_tokens=1, completion_tokens=0, duration_ms=1
        ),
        actions=[],
        final_response=final_response,
        modified_files_manifest=[],
        started_at=NOW,
        finished_at=NOW,
    )


class FakeBackend(ExecutorBackend):
    backend_type = ExecutorBackendType.PLUGGABLE

    def __init__(
        self,
        *,
        reachable: bool = True,
        trace: ExecutionTrace | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.reachable = reachable
        self.trace = trace
        self.error = error
        self.requests: list[ExecutionRequest] = []

    async def execute(self, request: ExecutionRequest) -> ExecutionTrace:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        assert self.trace is not None
        return self.trace

    async def health_check(self) -> bool:
        return self.reachable


class FakeCanaryHistory:
    def __init__(self, latest: CanaryProbeRecord | None = None) -> None:
        self.latest = latest
        self.recorded: list[CanaryProbeRecord] = []
        self.lookups: list[str] = []

    async def record(self, record: CanaryProbeRecord) -> None:
        self.recorded.append(record)

    async def latest_success(self, *, image_ref: str) -> CanaryProbeRecord | None:
        self.lookups.append(image_ref)
        return self.latest if self.latest and self.latest.image_ref == image_ref else None


def _pipeline(
    tmp_path: Path,
    *,
    golden: SandboxFingerprint | None = None,
    runner: FakeProbeRunner | None = None,
    backend: FakeBackend | None = None,
    history: FakeCanaryHistory | None = None,
    **settings: Any,
) -> PreflightPipeline:
    golden_path = tmp_path / "golden_fingerprint.json"
    if golden is not None:
        golden_path.write_text(dump_fingerprint(golden), encoding="utf-8")
    return PreflightPipeline(
        PreflightDeps(
            executor_backend=backend or FakeBackend(),
            environment_probe_runner=runner,
            canary_history_repository=history or FakeCanaryHistory(),  # type: ignore[arg-type]
            preflight_settings=PreflightSettings(
                golden_fingerprint_path=str(golden_path), **settings
            ),
            clock=lambda: NOW,
        )
    )


# --------------------------------------------------------------------------- #
# 指纹：比对与解析
# --------------------------------------------------------------------------- #


class FingerprintDiffTests:
    def test_identical_fingerprints_have_no_mismatch(self) -> None:
        assert diff_fingerprint(_fingerprint(), _fingerprint()) == []

    def test_minor_version_bump_missing_and_extra_keys_are_all_reported(self) -> None:
        current = _fingerprint(
            runtime_versions={"python": "Python 3.13.2", "uv": "uv 0.5"},  # node 缺失、uv 多出
        )
        mismatches = diff_fingerprint(current, _fingerprint())

        assert any("runtime_versions.python" in m and "3.13.2" in m for m in mismatches)
        assert any("runtime_versions.node" in m and "缺失" in m for m in mismatches)
        assert any("runtime_versions.uv" in m and "多出" in m for m in mismatches)

    def test_dump_is_stable_for_code_review(self) -> None:
        assert dump_fingerprint(_fingerprint()) == dump_fingerprint(_fingerprint())
        assert dump_fingerprint(_fingerprint()).endswith("\n")


class ProbeOutputTests:
    def test_non_zero_exit_is_backend_error(self) -> None:
        with pytest.raises(ExecutorBackendError, match="exit_code=124"):
            parse_probe_output(EnvironmentProbeResult(exit_code=124, stderr="timeout"))

    def test_garbage_output_is_backend_error_not_a_partial_fingerprint(self) -> None:
        with pytest.raises(ExecutorBackendError, match="不是合法的指纹"):
            parse_probe_output(EnvironmentProbeResult(exit_code=0, stdout="hello"))

    def test_secret_like_env_vars_are_stripped(self) -> None:
        fp = _fingerprint(
            key_env_vars_snapshot={"PATH": "/bin", "OPENROUTER_API_KEY": "sk-or", "GH_TOKEN": "x"}
        )
        parsed = parse_probe_output(_probe_ok(fp))
        assert parsed.key_env_vars_snapshot == {"PATH": "/bin"}

    def test_probe_script_emits_parseable_fingerprint(self) -> None:
        completed = subprocess.run(
            ["sh", str(PROBE_SCRIPT_PATH)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8", "MY_SECRET": "x"},
        )
        parsed = parse_probe_output(
            EnvironmentProbeResult(
                exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
            )
        )
        assert parsed.os_kernel
        assert parsed.key_env_vars_snapshot.get("LANG") == "C.UTF-8"
        assert "MY_SECRET" not in parsed.key_env_vars_snapshot  # 白名单之外的变量根本不采集


# --------------------------------------------------------------------------- #
# 指纹门禁节点
# --------------------------------------------------------------------------- #


class FingerprintGateTests:
    @pytest.mark.asyncio
    async def test_matching_fingerprint_passes_with_digest(self, tmp_path: Path) -> None:
        runner = FakeProbeRunner(_probe_ok(_fingerprint()))
        pipeline = _pipeline(
            tmp_path, golden=_fingerprint(), runner=runner, fingerprint_probe_timeout_s=7
        )

        update = await pipeline.sandbox_fingerprint_gate({"run_id": "run-1"})

        outcome = update[KEY_FINGERPRINT_OUTCOME]
        assert outcome["status"] == "passed"  # type: ignore[index]
        assert outcome["digest"] == _fingerprint().digest()  # type: ignore[index]
        assert runner.calls[0][1] == 7
        assert "#!/bin/sh" in runner.calls[0][0]  # 下发的是随包维护的探测脚本

    @pytest.mark.asyncio
    async def test_drift_suspends_the_whole_pipeline(self, tmp_path: Path) -> None:
        drifted = _fingerprint(core_package_hashes={"python_packages": "bbb"})
        pipeline = _pipeline(
            tmp_path, golden=_fingerprint(), runner=FakeProbeRunner(_probe_ok(drifted))
        )

        with pytest.raises(InfrastructureEnvironmentError) as info:
            await pipeline.sandbox_fingerprint_gate({"run_id": "run-1"})

        assert isinstance(info.value, PipelineSuspended)  # 主图按挂起处理，不是某维度 FAIL
        assert info.value.gate == GATE_FINGERPRINT
        assert any("core_package_hashes.python_packages" in d for d in info.value.details)

    @pytest.mark.asyncio
    async def test_missing_golden_fingerprint_fails_instead_of_skipping(
        self, tmp_path: Path
    ) -> None:
        runner = FakeProbeRunner(_probe_ok(_fingerprint()))
        pipeline = _pipeline(tmp_path, golden=None, runner=runner)

        with pytest.raises(InfrastructureEnvironmentError, match="缺少黄金指纹"):
            await pipeline.sandbox_fingerprint_gate({"run_id": "run-1"})
        assert runner.calls == []

    @pytest.mark.asyncio
    async def test_probe_channel_failure_is_infrastructure_error(self, tmp_path: Path) -> None:
        pipeline = _pipeline(
            tmp_path,
            golden=_fingerprint(),
            runner=FakeProbeRunner(ExecutorBackendError("hermes down")),
        )
        with pytest.raises(InfrastructureEnvironmentError, match="指纹探测失败"):
            await pipeline.sandbox_fingerprint_gate({"run_id": "run-1"})

    @pytest.mark.asyncio
    async def test_unconfigured_hermes_cannot_fake_a_fingerprint(self, tmp_path: Path) -> None:
        backend = HermesBackend(sandbox_client=UnconfiguredHermesSandboxClient())
        pipeline = PreflightPipeline(
            PreflightDeps(
                executor_backend=backend,
                preflight_settings=PreflightSettings(
                    golden_fingerprint_path=str(tmp_path / "g.json")
                ),
            )
        )
        (tmp_path / "g.json").write_text(dump_fingerprint(_fingerprint()), encoding="utf-8")

        with pytest.raises(InfrastructureEnvironmentError, match="尚未接入真实的 Hermes"):
            await pipeline.sandbox_fingerprint_gate({"run_id": "run-1"})

    @pytest.mark.asyncio
    async def test_off_mode_is_recorded_not_silent(self, tmp_path: Path) -> None:
        pipeline = _pipeline(tmp_path, fingerprint_check_mode="off")
        update = await pipeline.sandbox_fingerprint_gate({"run_id": "run-1"})
        assert update[KEY_FINGERPRINT_OUTCOME]["status"] == "off"  # type: ignore[index]


# --------------------------------------------------------------------------- #
# 金丝雀探针
# --------------------------------------------------------------------------- #


def _good_response() -> str:
    return json.dumps({"content": expected_canary_content()}, ensure_ascii=False)


class CanaryVerificationTests:
    def test_canary_skill_is_self_contained(self) -> None:
        skill = load_canary_skill()
        assert (Path(skill.root_path) / "data.txt").is_file()
        assert skill.version_ref.startswith("canary-")

    def test_request_case_id_is_scoped_per_run(self) -> None:
        request = build_canary_request(run_id="run-9", timeout_s=15)
        assert request.case.case_id == "__canary__:run-9"
        assert request.run_id == "run-9"
        assert request.wall_clock_timeout_s == 15

    def test_exact_content_passes(self) -> None:
        wrapped = f"好的：\n```json\n{_good_response()}\n```"
        assert verify_canary_trace(_trace(wrapped)).passed is True

    def test_made_up_content_fails(self) -> None:
        result = verify_canary_trace(_trace(json.dumps({"content": "文件为空"})))
        assert result.passed is False
        assert any("不一致" in r for r in result.reasons)

    def test_skill_not_loaded_fails(self) -> None:
        assert verify_canary_trace(_trace(_good_response(), loaded=False)).passed is False

    def test_failure_trace_fails(self) -> None:
        failure = build_failure_trace(case_id="c", run_index=0, reason="boom", timed_out=True)
        result = verify_canary_trace(failure)
        assert result.passed is False
        assert any("sandbox_timeout" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_unreachable_backend_does_not_execute(self) -> None:
        backend = FakeBackend(reachable=False)
        result = await run_canary_probe(backend, run_id="r", timeout_s=15)
        assert result.passed is False
        assert backend.requests == []

    @pytest.mark.asyncio
    async def test_graph_interrupt_is_propagated_not_treated_as_failure(self) -> None:
        backend = FakeBackend(error=GraphInterrupt())
        with pytest.raises(GraphInterrupt):
            await run_canary_probe(backend, run_id="r", timeout_s=15)


class CanaryGateTests:
    @pytest.mark.asyncio
    async def test_recent_success_on_same_image_is_skipped(self, tmp_path: Path) -> None:
        history = FakeCanaryHistory(
            CanaryProbeRecord(
                probe_id="p",
                run_id="run-0",
                image_ref="img@sha256:1",
                passed=True,
                probed_at=NOW - timedelta(hours=3),
            )
        )
        backend = FakeBackend(trace=_trace(_good_response()))
        pipeline = _pipeline(
            tmp_path, backend=backend, history=history, sandbox_image_ref="img@sha256:1"
        )

        update = await pipeline.canary_probe_gate({"run_id": "run-1"})

        assert update[KEY_CANARY_OUTCOME]["status"] == "skipped"  # type: ignore[index]
        assert backend.requests == []
        assert history.recorded == []

    @pytest.mark.asyncio
    async def test_stale_success_runs_probe_and_records(self, tmp_path: Path) -> None:
        history = FakeCanaryHistory(
            CanaryProbeRecord(
                probe_id="p",
                run_id="run-0",
                image_ref="img@sha256:1",
                passed=True,
                probed_at=NOW - timedelta(hours=30),
            )
        )
        backend = FakeBackend(trace=_trace(_good_response()))
        pipeline = _pipeline(
            tmp_path, backend=backend, history=history, sandbox_image_ref="img@sha256:1"
        )

        update = await pipeline.canary_probe_gate({"run_id": "run-1"})

        assert update[KEY_CANARY_OUTCOME]["status"] == "passed"  # type: ignore[index]
        assert len(backend.requests) == 1
        assert history.recorded[0].passed is True
        assert history.recorded[0].image_ref == "img@sha256:1"

    @pytest.mark.asyncio
    async def test_image_ref_falls_back_to_fingerprint_digest(self, tmp_path: Path) -> None:
        history = FakeCanaryHistory()
        backend = FakeBackend(trace=_trace(_good_response()))
        pipeline = _pipeline(tmp_path, backend=backend, history=history)

        await pipeline.canary_probe_gate(
            {
                "run_id": "run-1",
                KEY_FINGERPRINT_OUTCOME: {"status": "passed", "digest": "abc", "golden_path": "g"},
            }
        )

        assert history.lookups == ["fingerprint:abc"]

    @pytest.mark.asyncio
    async def test_every_run_mode_never_skips(self, tmp_path: Path) -> None:
        history = FakeCanaryHistory(
            CanaryProbeRecord(
                probe_id="p", run_id="r0", image_ref="img", passed=True, probed_at=NOW
            )
        )
        backend = FakeBackend(trace=_trace(_good_response()))
        pipeline = _pipeline(
            tmp_path,
            backend=backend,
            history=history,
            sandbox_image_ref="img",
            canary_check_mode="every_run",
        )

        await pipeline.canary_probe_gate({"run_id": "run-1"})

        assert len(backend.requests) == 1
        assert history.lookups == []

    @pytest.mark.asyncio
    async def test_failed_probe_is_recorded_then_suspends(self, tmp_path: Path) -> None:
        history = FakeCanaryHistory()
        backend = FakeBackend(trace=_trace(json.dumps({"content": "?"})))
        pipeline = _pipeline(
            tmp_path, backend=backend, history=history, canary_check_mode="every_run"
        )

        with pytest.raises(InfrastructureEnvironmentError) as info:
            await pipeline.canary_probe_gate({"run_id": "run-1"})

        assert info.value.gate == GATE_CANARY
        assert history.recorded[0].passed is False  # 失败记录同样落库，供运维排查
        assert history.recorded[0].reasons


class PreflightWiringTests:
    def test_routing_requires_real_sandbox(self) -> None:
        assert resolve_backend_type("preflight") is ExecutorBackendType.PLUGGABLE

    def test_subgraph_compiles(self, tmp_path: Path) -> None:
        graph = build_preflight_subgraph(PreflightDeps(executor_backend=FakeBackend())).compile()
        assert "preflight.sandbox_fingerprint_gate" in graph.get_graph().nodes
        assert "preflight.canary_probe_gate" in graph.get_graph().nodes


class HermesInterruptPropagationTests:
    @pytest.mark.asyncio
    async def test_execute_does_not_swallow_graph_interrupt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docs/dev/03 遗留缺陷的回归测试：挂起信号曾被 `except Exception` 吞成失败态 Trace。"""

        class Client(UnconfiguredHermesSandboxClient):
            async def create_sandbox(self, **_: Any) -> HermesSandboxHandle:
                return HermesSandboxHandle(sandbox_id="sb-1")

        class PendingRepo:
            async def create(self, **_: Any) -> None:
                return None

        async def interrupting_wait(**_: Any) -> None:
            raise GraphInterrupt()

        monkeypatch.setattr(
            "skill_evaluate.persistence.repository.PendingHookRepository", PendingRepo
        )
        monkeypatch.setattr(
            "skill_evaluate.persistence.suspension.suspend_and_wait", interrupting_wait
        )

        backend = HermesBackend(sandbox_client=Client())
        with pytest.raises(GraphInterrupt):
            await backend.execute(build_canary_request(run_id="run-1", timeout_s=15))
