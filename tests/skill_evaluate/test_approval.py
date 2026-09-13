"""docs/dev/22：容错机制与人工审批闭环。

不碰库、不发真实 Webhook：仓储全部是内存替身，Discord 走 `httpx.MockTransport`；
端到端用例用 LangGraph `InMemorySaver` 跑真实的 `interrupt()` → 决策 API → 唤醒。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any, TypedDict

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from langgraph.types import Command

from skill_evaluate.agents.judge.health import (
    ALERT_TYPE_JUDGE_FROZEN,
    JUDGE_HEALTH_ALERT_RUN_ID,
    JudgeHealthMonitor,
)
from skill_evaluate.agents.optimizer.loop import _is_adopt
from skill_evaluate.api import hooks_approval
from skill_evaluate.api.approval_context import ApprovalContextBuilder
from skill_evaluate.api.hooks_approval import (
    APPROVAL_SIGNATURE_HEADER,
    ApprovalDecisionHandler,
    SuggestionDecisionHandler,
    build_resume_payload,
)
from skill_evaluate.config import ApprovalSettings, Settings
from skill_evaluate.errors import (
    GenerationCollapseError,
    HumanRejectedSuspension,
    InfrastructureEnvironmentError,
    JudgeFrozenError,
    PipelineSuspended,
)
from skill_evaluate.nodes.approval_guard import (
    ApprovalGuard,
    ApprovalGuardedBuilder,
    resume_decision,
)
from skill_evaluate.nodes.coverage.nodes import _is_tree_confirmed
from skill_evaluate.observability import alerts as alerts_module
from skill_evaluate.observability import discord_notifier
from skill_evaluate.observability.discord_notifier import (
    DiscordAlertDispatcher,
    DiscordApprovalNotifier,
    LoggingApprovalNotifier,
    build_alert_card,
    build_approval_card,
    configure_notification_channels,
    workbench_link,
)
from skill_evaluate.persistence.approval_service import ApprovalService
from skill_evaluate.state.approval import (
    APPROVAL_OUTCOMES,
    ApprovalDecision,
    ApprovalDecisionRequest,
    ApprovalDecisionType,
    ApprovalStatus,
    PendingApproval,
    SuggestionDecisionRequest,
    allowed_outcomes,
    approval_id_for,
)
from skill_evaluate.state.enums import SuggestionStatus, SuggestionType
from skill_evaluate.state.patch import Patch, PatchApplicationResult, PatchType
from skill_evaluate.state.suggestion import TestCaseSuggestion

RUN_ID = "run-22"
SKILL_ID = "csv-cleaner"
NOW = datetime(2026, 9, 13, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #


class FakeLedger:
    """`human_approvals` 挂起账本替身。"""

    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []

    async def create(self, *, run_id: str, node_name: str, thread_id: str, wait_key: str) -> None:
        if all(c["wait_key"] != wait_key for c in self.created):
            self.created.append(
                {
                    "run_id": run_id,
                    "node_name": node_name,
                    "thread_id": thread_id,
                    "wait_key": wait_key,
                }
            )


class FakePendingRepo:
    """`pending_approvals` 替身：按 wait_key 去重，状态迁移只允许 pending → resolved。"""

    def __init__(self) -> None:
        self.by_id: dict[str, PendingApproval] = {}

    async def save(self, approval: PendingApproval) -> bool:
        if any(a.wait_key == approval.wait_key for a in self.by_id.values()):
            return False
        self.by_id[approval.approval_id] = approval
        return True

    async def get(self, approval_id: str) -> PendingApproval | None:
        return self.by_id.get(approval_id)

    async def list_by_status(
        self, status: ApprovalStatus | None = None, *, run_id: str | None = None
    ) -> list[PendingApproval]:
        return [
            a
            for a in self.by_id.values()
            if (status is None or a.status is status) and (run_id is None or a.run_id == run_id)
        ]

    async def find_pending_by_node(self, run_id: str, node_name: str) -> PendingApproval | None:
        for a in self.by_id.values():
            if (
                a.run_id == run_id
                and a.node_name == node_name
                and a.status is ApprovalStatus.PENDING
            ):
                return a
        return None

    async def mark_resolved(self, approval_id: str) -> bool:
        a = self.by_id.get(approval_id)
        if a is None or a.status is not ApprovalStatus.PENDING:
            return False
        self.by_id[approval_id] = a.model_copy(
            update={"status": ApprovalStatus.RESOLVED, "resolved_at": NOW}
        )
        return True


class FakeDecisionRepo:
    def __init__(self) -> None:
        self.saved: dict[str, ApprovalDecision] = {}

    async def save(self, decision: ApprovalDecision) -> bool:
        if decision.approval_id in self.saved:
            return False
        self.saved[decision.approval_id] = decision
        return True

    async def get_by_approval(self, approval_id: str) -> ApprovalDecision | None:
        return self.saved.get(approval_id)


class FakeRunRepo:
    def __init__(self, runs: dict[str, dict[str, Any]] | None = None) -> None:
        self.runs = runs or {}

    async def get(self, run_id: str) -> dict[str, Any] | None:
        return self.runs.get(run_id)


class FailingNotifier:
    async def notify_approval(self, approval: PendingApproval) -> None:
        raise RuntimeError("discord down")


class ScriptedSuspend:
    """模拟 `suspend_and_wait()`：按顺序返回预设的 resume payload。"""

    def __init__(self, *decisions: Any) -> None:
        self.decisions = list(decisions)
        self.calls: list[dict[str, str]] = []

    async def __call__(self, *, reason: str, wait_key: str) -> Any:
        self.calls.append({"reason": reason, "wait_key": wait_key})
        return self.decisions.pop(0) if self.decisions else None


class FakeJudgeHealth:
    def __init__(self) -> None:
        self.unfrozen: list[dict[str, Any]] = []

    async def unfreeze(self, *, model: str, temperature: float, operator: str) -> None:
        self.unfrozen.append({"model": model, "temperature": temperature, "operator": operator})


def _service(
    *,
    suspend: ScriptedSuspend | None = None,
    notifier: Any = None,
    runs: dict[str, dict[str, Any]] | None = None,
) -> tuple[ApprovalService, FakeLedger, FakePendingRepo, Any]:
    ledger, pending = FakeLedger(), FakePendingRepo()
    notifier = notifier or LoggingApprovalNotifier()
    service = ApprovalService(
        ledger_repository=ledger,
        pending_repository=pending,
        run_repository=FakeRunRepo(runs),
        notifier=notifier,
        suspend=suspend or ScriptedSuspend(),
    )
    return service, ledger, pending, notifier


def _approval(
    decision_type: ApprovalDecisionType = ApprovalDecisionType.ACCEPT_PATCH,
    *,
    blocking: bool = True,
    context_ref: dict[str, Any] | None = None,
    wait_key: str = f"{RUN_ID}:optimizer:prompt_engineer",
) -> PendingApproval:
    return PendingApproval(
        approval_id=approval_id_for(wait_key),
        run_id=RUN_ID,
        wait_key=wait_key,
        decision_type=decision_type,
        context_summary="摘要",
        context_ref=context_ref or {},
        node_name="optimizer:prompt_engineer",
        thread_id=f"{SKILL_ID}:{RUN_ID}",
        blocking=blocking,
        created_at=NOW,
    )


def _handler(
    approval: PendingApproval | None = None,
    *,
    registered: bool = True,
) -> tuple[
    ApprovalDecisionHandler,
    FakePendingRepo,
    FakeDecisionRepo,
    list[dict[str, Any]],
    FakeJudgeHealth,
]:
    pending, decisions, resolved, judge = (
        FakePendingRepo(),
        FakeDecisionRepo(),
        [],
        FakeJudgeHealth(),
    )
    if approval is not None:
        pending.by_id[approval.approval_id] = approval

    async def resolve(**kwargs: Any) -> None:
        resolved.append(kwargs)

    handler = ApprovalDecisionHandler(
        pending_repository=pending,
        decision_repository=decisions,
        resolve=resolve,
        resumer_registered=lambda: registered,
        judge_health_monitor_factory=lambda: judge,
    )
    return handler, pending, decisions, resolved, judge


def _request(outcome: str, **kw: Any) -> ApprovalDecisionRequest:
    return ApprovalDecisionRequest(decided_by="alice@example.com", outcome=outcome, **kw)


# --------------------------------------------------------------------------- #
# 1. 数据契约
# --------------------------------------------------------------------------- #


class ApprovalContractTests:
    def test_every_decision_type_offers_a_way_to_say_no(self) -> None:
        # 人必须始终有权说"不"，否则审批退化成只能点"继续"的确认框。
        negatives = {"abandon", "reject", "rejected"}
        for decision_type in ApprovalDecisionType:
            assert APPROVAL_OUTCOMES[decision_type] & negatives, decision_type

    def test_non_blocking_cards_only_accept_acknowledge(self) -> None:
        for decision_type in ApprovalDecisionType:
            assert allowed_outcomes(decision_type, blocking=False) == {"acknowledge"}

    def test_approval_id_is_deterministic_per_wait_key(self) -> None:
        # 恢复时节点整体重跑，第二次写入必须命中同一个 id 被去重。
        assert approval_id_for("a:b") == approval_id_for("a:b") != approval_id_for("a:c")

    def test_outcome_constants_match_existing_resume_parsers(self) -> None:
        """统一 payload 形状 `{"decision": ...}` 被 09/16 既有解析函数正确识别。"""
        approval = _approval()
        decision = ApprovalDecision(
            approval_id=approval.approval_id, decided_by="a", outcome="adopt", decided_at=NOW
        )
        assert _is_adopt(build_resume_payload(approval, decision)) is True
        abandon = decision.model_copy(update={"outcome": "abandon"})
        assert _is_adopt(build_resume_payload(approval, abandon)) is False
        tree = _approval(ApprovalDecisionType.CONFIRM_TREE_REVIEW)
        confirm = decision.model_copy(update={"outcome": "confirm"})
        assert _is_tree_confirmed(build_resume_payload(tree, confirm)) is True
        reject = decision.model_copy(update={"outcome": "reject"})
        assert _is_tree_confirmed(build_resume_payload(tree, reject)) is False

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ("Retry ", "retry"),
            ({"decision": "UNFREEZE"}, "unfreeze"),
            (None, None),
            (42, None),
            ({}, None),
        ],
    )
    def test_resume_decision_parsing(self, payload: Any, expected: str | None) -> None:
        assert resume_decision(payload) == expected


# --------------------------------------------------------------------------- #
# 2. ApprovalService
# --------------------------------------------------------------------------- #


class ApprovalServiceTests:
    async def test_blocking_request_records_ledger_card_notifies_then_suspends(self) -> None:
        suspend = ScriptedSuspend({"decision": "adopt"})
        service, ledger, pending, notifier = _service(suspend=suspend)
        result = await service.request_human_approval(
            run_id=RUN_ID,
            wait_key="w-1",
            decision_type=ApprovalDecisionType.ACCEPT_PATCH,
            context_summary="s",
            context_ref={"patch_id": "p-1"},
            node_name="n",
            skill_id=SKILL_ID,
        )
        assert result == {"decision": "adopt"}
        assert ledger.created[0]["thread_id"] == f"{SKILL_ID}:{RUN_ID}"
        card = next(iter(pending.by_id.values()))
        assert card.blocking is True and card.status is ApprovalStatus.PENDING
        assert card.context_ref == {"patch_id": "p-1"}
        assert len(notifier.sent) == 1
        assert suspend.calls == [{"reason": "accept_patch", "wait_key": "w-1"}]

    async def test_rerun_after_resume_does_not_send_a_second_card(self) -> None:
        service, _, pending, notifier = _service(suspend=ScriptedSuspend("x", "x"))
        kwargs: dict[str, Any] = {
            "run_id": RUN_ID,
            "wait_key": "w-1",
            "decision_type": ApprovalDecisionType.CONFIRM_TREE_REVIEW,
            "context_summary": "s",
            "context_ref": {},
            "node_name": "n",
            "skill_id": SKILL_ID,
        }
        await service.request_human_approval(**kwargs)
        await service.request_human_approval(**kwargs)
        assert len(pending.by_id) == 1 and len(notifier.sent) == 1

    async def test_notification_failure_does_not_block_suspension(self) -> None:
        suspend = ScriptedSuspend("confirm")
        service, _, pending, _ = _service(suspend=suspend, notifier=FailingNotifier())
        result = await service.request_human_approval(
            run_id=RUN_ID,
            wait_key="w",
            decision_type=ApprovalDecisionType.CONFIRM_TREE_REVIEW,
            context_summary="s",
            context_ref={},
            node_name="n",
            skill_id=SKILL_ID,
        )
        assert result == "confirm" and len(pending.by_id) == 1

    async def test_non_blocking_notice_writes_card_but_no_ledger_and_no_suspend(self) -> None:
        suspend = ScriptedSuspend()
        service, ledger, pending, notifier = _service(suspend=suspend)
        card = await service.notify_human(
            run_id=RUN_ID,
            wait_key="w",
            decision_type=ApprovalDecisionType.RESOLVE_DEEP_CONFLICT,
            context_summary="s",
            context_ref={},
            node_name="n",
            skill_id=SKILL_ID,
        )
        assert card.blocking is False and pending.by_id[card.approval_id] == card
        assert ledger.created == [] and suspend.calls == []
        assert len(notifier.sent) == 1

    @pytest.mark.parametrize(
        ("skill_id", "thread_id", "runs", "expected"),
        [
            (None, "explicit", {}, "explicit"),
            (SKILL_ID, None, {}, f"{SKILL_ID}:{RUN_ID}"),
            (None, None, {RUN_ID: {"skill_id": "from-db"}}, f"from-db:{RUN_ID}"),
            (None, None, {}, RUN_ID),
        ],
        ids=["explicit", "skill-id", "runs-table", "fallback"],
    )
    async def test_thread_id_resolution_order(
        self, skill_id: str | None, thread_id: str | None, runs: dict[str, Any], expected: str
    ) -> None:
        service, ledger, _, _ = _service(runs=runs)
        await service.request_human_approval(
            run_id=RUN_ID,
            wait_key="w",
            decision_type=ApprovalDecisionType.ABANDON_RUN,
            context_summary="s",
            context_ref={},
            node_name="n",
            skill_id=skill_id,
            thread_id=thread_id,
        )
        assert ledger.created[0]["thread_id"] == expected


# --------------------------------------------------------------------------- #
# 3. ApprovalGuard
# --------------------------------------------------------------------------- #


def _state() -> dict[str, Any]:
    return {"run_id": RUN_ID, "skill_id": SKILL_ID}


# 必须定义在模块级：`from __future__ import annotations` 下注解是字符串，LangGraph 通过
# `get_type_hints` 在函数所在模块的全局命名空间里解析——各维度的 State 类本来就都在模块级。
class GuardBigState(TypedDict, total=False):
    run_id: str
    _private: int


class GuardSmallState(TypedDict, total=False):
    run_id: str


class FlakyNode:
    """前 `failures` 次调用抛给定异常，之后返回结果。"""

    def __init__(self, exc: Exception, failures: int = 1) -> None:
        self.exc = exc
        self.failures = failures
        self.calls = 0

    async def __call__(self, state: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return {"ok": True}


class ApprovalGuardTests:
    async def test_passes_through_normal_results(self) -> None:
        service, _, pending, _ = _service()
        node = FlakyNode(PipelineSuspended("x"), failures=0)
        assert await ApprovalGuard(service).wrap("n", node)(_state()) == {"ok": True}
        assert pending.by_id == {}

    async def test_judge_frozen_asks_to_unfreeze_and_retries(self) -> None:
        suspend = ScriptedSuspend({"decision": "unfreeze"})
        service, _, pending, _ = _service(suspend=suspend)
        node = FlakyNode(JudgeFrozenError("frozen", model="m", temperature=0.0))
        assert await ApprovalGuard(service).wrap("dim.node", node)(_state()) == {"ok": True}
        card = next(iter(pending.by_id.values()))
        assert card.decision_type is ApprovalDecisionType.UNFREEZE_JUDGE
        assert card.context_ref["model"] == "m" and card.context_ref["temperature"] == 0.0
        assert suspend.calls[0]["wait_key"] == f"{RUN_ID}:dim.node:judge_frozen:0"
        assert node.calls == 2

    async def test_collapse_below_limit_is_not_escalated(self) -> None:
        service, _, pending, _ = _service()
        exc = GenerationCollapseError(
            "c", skill_id=SKILL_ID, consecutive_collapses=1, requires_human_seed=False
        )
        with pytest.raises(GenerationCollapseError):
            await ApprovalGuard(service).wrap("n", FlakyNode(exc))(_state())
        assert pending.by_id == {}

    async def test_persistent_collapse_asks_for_new_seeds(self) -> None:
        suspend = ScriptedSuspend({"decision": "seed_injected"})
        service, _, pending, _ = _service(suspend=suspend)
        exc = GenerationCollapseError(
            "c", skill_id=SKILL_ID, consecutive_collapses=3, requires_human_seed=True
        )
        assert await ApprovalGuard(service).wrap("n", FlakyNode(exc))(_state()) == {"ok": True}
        card = next(iter(pending.by_id.values()))
        assert card.decision_type is ApprovalDecisionType.INJECT_NEW_SEED
        assert card.context_ref["consecutive_collapses"] == 3

    async def test_suspended_abandon_raises_human_rejected(self) -> None:
        service, _, _, _ = _service(suspend=ScriptedSuspend({"decision": "abandon"}))
        with pytest.raises(HumanRejectedSuspension, match="选择放弃"):
            await ApprovalGuard(service).wrap("n", FlakyNode(PipelineSuspended("共识未达成")))(
                _state()
            )

    async def test_each_retry_round_gets_its_own_card_and_rounds_are_capped(self) -> None:
        suspend = ScriptedSuspend("retry", "retry")
        service, _, pending, _ = _service(suspend=suspend)
        node = FlakyNode(PipelineSuspended("still broken"), failures=99)
        with pytest.raises(PipelineSuspended, match="still broken"):
            await ApprovalGuard(service, max_rounds=2).wrap("n", node)(_state())
        assert [c["wait_key"] for c in suspend.calls] == [
            f"{RUN_ID}:n:suspended:0",
            f"{RUN_ID}:n:suspended:1",
        ]
        assert len(pending.by_id) == 2 and node.calls == 3

    async def test_human_rejected_is_never_asked_twice(self) -> None:
        service, _, pending, _ = _service()
        with pytest.raises(HumanRejectedSuspension):
            await ApprovalGuard(service).wrap("n", FlakyNode(HumanRejectedSuspension("no")))(
                _state()
            )
        assert pending.by_id == {}

    async def test_infrastructure_failure_only_notifies_then_reraises(self) -> None:
        suspend = ScriptedSuspend()
        service, ledger, pending, _ = _service(suspend=suspend)
        exc = InfrastructureEnvironmentError("drift", gate="sandbox_fingerprint", details=["a"])
        with pytest.raises(InfrastructureEnvironmentError):
            await ApprovalGuard(service).wrap("preflight.x", FlakyNode(exc))(_state())
        card = next(iter(pending.by_id.values()))
        assert card.blocking is False and card.context_ref["gate"] == "sandbox_fingerprint"
        assert suspend.calls == [] and ledger.created == []

    async def test_missing_run_id_reraises_instead_of_suspending(self) -> None:
        service, _, pending, _ = _service()
        with pytest.raises(PipelineSuspended):
            await ApprovalGuard(service).wrap("n", FlakyNode(PipelineSuspended("x")))({})
        assert pending.by_id == {}

    async def test_guarded_builder_preserves_state_schema_and_config_injection(self) -> None:
        """LangGraph 按首参注解推断输入 schema；包装后私有键不能被裁掉，config 照常注入。"""

        async def write(state: GuardBigState) -> dict[str, Any]:
            return {"_private": 7}

        async def read(state: GuardBigState, config: RunnableConfig) -> dict[str, Any]:
            assert state.get("_private") == 7 and config is not None
            return {}

        builder: Any = StateGraph(GuardSmallState)
        guarded = ApprovalGuardedBuilder(builder, ApprovalGuard(_service()[0]))
        guarded.add_node("w", write)
        guarded.add_node("r", read)
        guarded.set_entry_point("w")
        guarded.add_edge("w", "r")
        guarded.set_finish_point("r")
        assert builder.nodes["w"].input_schema is GuardBigState
        await guarded.builder.compile().ainvoke({"run_id": RUN_ID})


# --------------------------------------------------------------------------- #
# 4. 端到端：真实 interrupt() → 决策 API → 唤醒
# --------------------------------------------------------------------------- #


class EndToEndResumeTests:
    async def test_judge_freeze_suspends_graph_and_human_unfreeze_resumes_it(self) -> None:
        pending, ledger = FakePendingRepo(), FakeLedger()
        service = ApprovalService(
            ledger_repository=ledger,
            pending_repository=pending,
            run_repository=FakeRunRepo(),
            notifier=LoggingApprovalNotifier(),
        )  # 默认 suspend = 真实 suspend_and_wait（interrupt）
        judge = FakeJudgeHealth()
        frozen = {"value": True}

        class S(TypedDict, total=False):
            run_id: str
            skill_id: str
            verdict: str

        async def judge_node(state: S) -> dict[str, Any]:
            if frozen["value"]:
                raise JudgeFrozenError("frozen", model="m", temperature=0.0)
            return {"verdict": "pass"}

        builder: Any = StateGraph(S)
        ApprovalGuardedBuilder(builder, ApprovalGuard(service)).add_node("dim.judge", judge_node)
        builder.set_entry_point("dim.judge")
        builder.set_finish_point("dim.judge")
        graph = builder.compile(checkpointer=InMemorySaver())
        thread_id = f"{SKILL_ID}:{RUN_ID}"
        config: Any = {"configurable": {"thread_id": thread_id}}

        first = await graph.ainvoke({"run_id": RUN_ID, "skill_id": SKILL_ID}, config=config)
        assert "__interrupt__" in first and "verdict" not in first
        card = next(iter(pending.by_id.values()))
        assert card.thread_id == thread_id

        final: dict[str, Any] = {}

        async def resolve(*, wait_key: str, resume_payload: Any, thread_id: str) -> None:
            # 模拟人工在 Prompt 调整完成后解冻：真实 unfreeze 改库，这里翻转内存开关。
            frozen["value"] = not judge.unfrozen
            final.update(
                await graph.ainvoke(
                    Command(resume=resume_payload),
                    config={"configurable": {"thread_id": thread_id}},
                )
            )

        handler = ApprovalDecisionHandler(
            pending_repository=pending,
            decision_repository=FakeDecisionRepo(),
            resolve=resolve,
            resumer_registered=lambda: True,
            judge_health_monitor_factory=lambda: judge,
        )
        result = await handler.decide(card.approval_id, _request("unfreeze"))
        assert result["resumed"] is True
        assert judge.unfrozen[0]["model"] == "m"
        assert final["verdict"] == "pass"
        assert pending.by_id[card.approval_id].status is ApprovalStatus.RESOLVED


# --------------------------------------------------------------------------- #
# 5. 决策处理
# --------------------------------------------------------------------------- #


class ApprovalDecisionHandlerTests:
    async def test_accept_patch_resumes_with_decision_payload(self) -> None:
        approval = _approval()
        handler, pending, decisions, resolved, _ = _handler(approval)
        result = await handler.decide(approval.approval_id, _request(" Adopt ", note="看过 diff"))
        assert result == {
            "status": "resolved",
            "approval_id": approval.approval_id,
            "resumed": True,
        }
        assert resolved[0]["wait_key"] == approval.wait_key
        assert resolved[0]["thread_id"] == approval.thread_id
        assert resolved[0]["resume_payload"]["decision"] == "adopt"
        assert decisions.saved[approval.approval_id].note == "看过 diff"
        assert pending.by_id[approval.approval_id].status is ApprovalStatus.RESOLVED

    async def test_unknown_approval_is_404(self) -> None:
        handler, *_ = _handler()
        with pytest.raises(HTTPException) as info:
            await handler.decide("nope", _request("adopt"))
        assert info.value.status_code == 404

    @pytest.mark.parametrize(
        ("approval", "outcome", "registered", "status_code"),
        [
            (_approval(), "confirm", True, 422),  # 该类型不接受这个 outcome
            (_approval(blocking=False), "adopt", True, 422),  # 非阻塞卡片只接受 acknowledge
            (_approval(ApprovalDecisionType.CONFIRM_ORPHAN_RETIREMENT), "confirmed", True, 422),
            (_approval(ApprovalDecisionType.UNFREEZE_JUDGE), "unfreeze", True, 422),  # 缺 model
            (_approval(), "adopt", False, 503),  # 主图未注册 resumer
            (
                _approval().model_copy(update={"status": ApprovalStatus.RESOLVED}),
                "adopt",
                True,
                409,
            ),
        ],
        ids=[
            "wrong-outcome",
            "non-blocking",
            "orphan",
            "unfreeze-no-model",
            "no-resumer",
            "resolved",
        ],
    )
    async def test_validation_failures_write_nothing(
        self, approval: PendingApproval, outcome: str, registered: bool, status_code: int
    ) -> None:
        handler, pending, decisions, resolved, judge = _handler(approval, registered=registered)
        with pytest.raises(HTTPException) as info:
            await handler.decide(approval.approval_id, _request(outcome))
        assert info.value.status_code == status_code
        assert decisions.saved == {} and resolved == [] and judge.unfrozen == []
        assert pending.by_id[approval.approval_id].status is approval.status  # 卡片状态未被改动

    async def test_second_decision_loses_the_race_with_409(self) -> None:
        approval = _approval()
        handler, _, decisions, resolved, _ = _handler(approval)
        decisions.saved[approval.approval_id] = ApprovalDecision(
            approval_id=approval.approval_id, decided_by="bob", outcome="abandon", decided_at=NOW
        )
        with pytest.raises(HTTPException) as info:
            await handler.decide(approval.approval_id, _request("adopt"))
        assert info.value.status_code == 409 and resolved == []

    async def test_unfreeze_runs_side_effect_before_resuming(self) -> None:
        approval = _approval(
            ApprovalDecisionType.UNFREEZE_JUDGE,
            context_ref={"model": "anthropic/claude-sonnet-5", "temperature": 0.3},
        )
        handler, _, _, resolved, judge = _handler(approval)
        await handler.decide(approval.approval_id, _request("unfreeze"))
        assert judge.unfrozen == [
            {
                "model": "anthropic/claude-sonnet-5",
                "temperature": 0.3,
                "operator": "alice@example.com",
            }
        ]
        assert resolved[0]["resume_payload"]["decision"] == "unfreeze"

    async def test_abandon_on_unfreeze_card_does_not_unfreeze(self) -> None:
        approval = _approval(ApprovalDecisionType.UNFREEZE_JUDGE, context_ref={"model": "m"})
        handler, _, _, resolved, judge = _handler(approval)
        await handler.decide(approval.approval_id, _request("abandon"))
        assert judge.unfrozen == [] and resolved[0]["resume_payload"]["decision"] == "abandon"

    async def test_non_blocking_acknowledge_records_without_resuming(self) -> None:
        approval = _approval(ApprovalDecisionType.RESOLVE_DEEP_CONFLICT, blocking=False)
        # 非阻塞卡片没有挂起点，resumer 未注册也应当可以处理。
        handler, pending, decisions, resolved, _ = _handler(approval, registered=False)
        result = await handler.decide(approval.approval_id, _request("acknowledge"))
        assert result["resumed"] is False and resolved == []
        assert approval.approval_id in decisions.saved
        assert pending.by_id[approval.approval_id].status is ApprovalStatus.RESOLVED


# --------------------------------------------------------------------------- #
# 6. 孤儿用例建议
# --------------------------------------------------------------------------- #


class FakeSuggestionRepo:
    def __init__(self, *suggestions: TestCaseSuggestion) -> None:
        self.by_id = {s.suggestion_id: s for s in suggestions}

    async def get(self, suggestion_id: str) -> TestCaseSuggestion | None:
        return self.by_id.get(suggestion_id)

    async def list_by_status(self, status: SuggestionStatus) -> list[TestCaseSuggestion]:
        return [s for s in self.by_id.values() if s.status is status]

    async def update_status(self, suggestion_id: str, status: SuggestionStatus) -> bool:
        s = self.by_id[suggestion_id]
        if s.status is not SuggestionStatus.PENDING:
            return False
        self.by_id[suggestion_id] = s.model_copy(update={"status": status})
        return True


class FakeCaseRepo:
    def __init__(self) -> None:
        self.retired: list[str] = []

    async def retire(self, case_id: str) -> bool:
        self.retired.append(case_id)
        return True


def _suggestion(status: SuggestionStatus = SuggestionStatus.PENDING) -> TestCaseSuggestion:
    return TestCaseSuggestion(
        suggestion_id="sug-1",
        case_id="case-1",
        suggestion_type=SuggestionType.ORPHAN_RETIREMENT,
        reason="能力 cap-x 已消失",
        status=status,
        created_at=NOW,
    )


class SuggestionDecisionHandlerTests:
    def _handler(
        self, suggestion: TestCaseSuggestion
    ) -> tuple[SuggestionDecisionHandler, FakeSuggestionRepo, FakeCaseRepo]:
        repo, cases = FakeSuggestionRepo(suggestion), FakeCaseRepo()
        return (
            SuggestionDecisionHandler(suggestion_repository=repo, case_repository=cases),
            repo,
            cases,
        )

    async def test_confirmed_retires_case_to_cold(self) -> None:
        handler, repo, cases = self._handler(_suggestion())
        result = await handler.decide(
            "sug-1", SuggestionDecisionRequest(decided_by="alice", outcome="confirmed")
        )
        assert result["case_retired"] is True and cases.retired == ["case-1"]
        assert repo.by_id["sug-1"].status is SuggestionStatus.CONFIRMED

    async def test_rejected_keeps_the_case(self) -> None:
        handler, repo, cases = self._handler(_suggestion())
        await handler.decide("sug-1", SuggestionDecisionRequest(decided_by="a", outcome="rejected"))
        assert cases.retired == [] and repo.by_id["sug-1"].status is SuggestionStatus.REJECTED

    async def test_already_decided_is_409_and_invalid_outcome_is_422(self) -> None:
        handler, _, cases = self._handler(_suggestion(SuggestionStatus.REJECTED))
        with pytest.raises(HTTPException) as info:
            await handler.decide(
                "sug-1", SuggestionDecisionRequest(decided_by="a", outcome="confirmed")
            )
        assert info.value.status_code == 409 and cases.retired == []
        handler, _, _ = self._handler(_suggestion())
        with pytest.raises(HTTPException) as info:
            await handler.decide(
                "sug-1", SuggestionDecisionRequest(decided_by="a", outcome="delete")
            )
        assert info.value.status_code == 422


# --------------------------------------------------------------------------- #
# 7. HTTP 层
# --------------------------------------------------------------------------- #


def _app(
    pending: FakePendingRepo, handler: ApprovalDecisionHandler, suggestions: FakeSuggestionRepo
) -> TestClient:
    app = FastAPI()
    app.include_router(hooks_approval.router)
    app.dependency_overrides[hooks_approval.get_pending_approval_repository] = lambda: pending
    app.dependency_overrides[hooks_approval.get_approval_decision_handler] = lambda: handler
    app.dependency_overrides[hooks_approval.get_suggestion_repository] = lambda: suggestions
    app.dependency_overrides[hooks_approval.get_suggestion_decision_handler] = lambda: (
        SuggestionDecisionHandler(suggestion_repository=suggestions, case_repository=FakeCaseRepo())
    )
    app.dependency_overrides[hooks_approval.get_approval_context_builder] = lambda: (
        ApprovalContextBuilder(
            run_repository=FakeRunRepo(),
            decision_repository=FakeDecisionRepo(),
            patch_repository=FakePatchRepo(),
            pipeline_state_repository=FakePipelineStateRepo(),
        )
    )
    return TestClient(app)


class FakePatchRepo:
    async def get(self, patch_id: str) -> Patch | None:
        return Patch(
            patch_id=patch_id,
            skill_id=SKILL_ID,
            base_skill_version_ref="v1",
            patch_type=PatchType.DESCRIPTION_PATCH,
            target_path="SKILL.md",
            diff="@@ -1 +1 @@",
            rationale="r",
            created_at=NOW,
        )

    async def get_application_result(self, patch_id: str) -> PatchApplicationResult | None:
        raise RuntimeError("db hiccup")


class FakePipelineStateRepo:
    async def get_retry_counts(self, run_id: str) -> dict[str, int]:
        return {"optimizer:prompt_engineer": 3}


class HttpApiTests:
    def test_list_context_and_decide_round_trip(self) -> None:
        approval = _approval(context_ref={"patch_id": "p-1"})
        handler, pending, _, resolved, _ = _handler(approval)
        client = _app(pending, handler, FakeSuggestionRepo(_suggestion()))

        listed = client.get("/api/approvals").json()
        assert [a["approval_id"] for a in listed] == [approval.approval_id]
        assert client.get("/api/approvals?status=bogus").status_code == 422

        context = client.get(f"/api/approvals/{approval.approval_id}/context").json()
        assert context["patch"]["diff"] == "@@ -1 +1 @@"
        assert context["retry_counts"] == {"optimizer:prompt_engineer": 3}
        assert context["allowed_outcomes"] == ["abandon", "adopt"]
        # 单项展开失败不拖垮整个视图。
        assert context["expansion_errors"][0].startswith("patch_application_result")

        response = client.post(
            f"/api/approvals/{approval.approval_id}/decide",
            json={"decided_by": "alice", "outcome": "adopt"},
        )
        assert response.status_code == 200 and resolved
        assert client.get("/api/approvals").json() == []
        assert len(client.get("/api/approvals?status=all").json()) == 1

    def test_suggestion_endpoints(self) -> None:
        handler, pending, *_ = _handler()
        suggestions = FakeSuggestionRepo(_suggestion())
        client = _app(pending, handler, suggestions)
        assert [s["case_id"] for s in client.get("/api/suggestions").json()] == ["case-1"]
        response = client.post(
            "/api/suggestions/sug-1/decide", json={"decided_by": "a", "outcome": "confirmed"}
        )
        assert response.json()["case_retired"] is True
        assert client.get("/api/suggestions").json() == []

    def test_hmac_hook_requires_valid_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        approval = _approval()
        handler, pending, _, resolved, _ = _handler(approval)
        client = _app(pending, handler, FakeSuggestionRepo())
        settings = Settings(approval=ApprovalSettings(hmac_secret="s3cret"))  # type: ignore[arg-type]
        monkeypatch.setattr(hooks_approval, "get_settings", lambda: settings)
        body = json.dumps({"decided_by": "bot", "outcome": "adopt"}).encode()
        url = f"/hooks/approval/{RUN_ID}/{approval.node_name}"

        bad = client.post(url, content=body, headers={APPROVAL_SIGNATURE_HEADER: "nope"})
        assert bad.status_code == 401 and resolved == []

        signature = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        ok = client.post(url, content=body, headers={APPROVAL_SIGNATURE_HEADER: signature})
        assert ok.status_code == 200 and resolved[0]["resume_payload"]["decided_by"] == "bot"
        missing = client.post(
            f"/hooks/approval/{RUN_ID}/other-node",
            content=body,
            headers={APPROVAL_SIGNATURE_HEADER: signature},
        )
        assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# 8. Discord 通道
# --------------------------------------------------------------------------- #


class DiscordNotifierTests:
    def test_approval_card_disables_mentions_and_links_to_workbench(self) -> None:
        approval = _approval().model_copy(update={"context_summary": "@everyone " + "x" * 5000})
        card = build_approval_card(approval, workbench_base_url="https://wb.example/")
        assert card["allowed_mentions"] == {"parse": []}
        embed = card["embeds"][0]
        assert embed["title"] == "[待审批] accept_patch"
        assert len(embed["description"]) == 4096
        link = workbench_link("https://wb.example/", approval)
        assert link.startswith(f"https://wb.example/runs/{RUN_ID}?wait_key=run-22%3Aoptimizer")
        assert any(f["value"] == link for f in embed["fields"])

    def test_non_blocking_card_is_labelled_as_notice(self) -> None:
        card = build_approval_card(_approval(blocking=False), workbench_base_url="https://wb")
        assert card["embeds"][0]["title"].startswith("[通知]")

    def test_alert_card_renders_payload_and_skips_link_for_platform_alerts(self) -> None:
        card = build_alert_card(
            alert_type=ALERT_TYPE_JUDGE_FROZEN,
            run_id=JUDGE_HEALTH_ALERT_RUN_ID,
            payload={"miss_rate": 0.08, "details": ["a", "b"]},
            workbench_base_url="https://wb",
        )
        fields = {f["name"]: f["value"] for f in card["embeds"][0]["fields"]}
        assert "审查工作台" not in fields
        assert fields["details"] == '["a", "b"]' and fields["miss_rate"] == "0.08"
        assert card["embeds"][0]["title"] == "[告警] Judge 配置已冻结"

    async def test_discord_channels_post_to_webhook_and_surface_http_errors(self) -> None:
        posted: list[dict[str, Any]] = []
        status = {"code": 204}

        def transport(request: httpx.Request) -> httpx.Response:
            posted.append(json.loads(request.content))
            return httpx.Response(status["code"], text="rate limited")

        client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        notifier = DiscordApprovalNotifier(
            "https://discord/webhook", workbench_base_url="https://wb", http_client=client
        )
        await notifier.notify_approval(_approval())
        dispatcher = DiscordAlertDispatcher(
            "https://discord/webhook", workbench_base_url="https://wb", http_client=client
        )
        status["code"] = 429
        # dispatch_alert 吞掉通道异常：告警是旁路。
        assert (
            await alerts_module.dispatch_alert(
                dispatcher, alert_type="deep_multi_skill_conflict", run_id=RUN_ID, payload={}
            )
            is False
        )
        assert len(posted) == 2
        await client.aclose()

    def test_configure_channels_registers_discord_only_when_webhook_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel_notifier, sentinel_alerts = (
            LoggingApprovalNotifier(),
            alerts_module.LoggingAlertDispatcher(),
        )
        monkeypatch.setattr(discord_notifier, "_approval_notifier", sentinel_notifier)
        monkeypatch.setattr(alerts_module, "_dispatcher", sentinel_alerts)

        assert configure_notification_channels(Settings()) is False
        assert discord_notifier.get_approval_notifier() is sentinel_notifier

        settings = Settings(
            approval=ApprovalSettings(discord_webhook_url="https://discord/webhook")  # type: ignore[arg-type]
        )
        assert configure_notification_channels(settings) is True
        assert isinstance(discord_notifier.get_approval_notifier(), DiscordApprovalNotifier)
        assert isinstance(alerts_module.get_alert_dispatcher(), DiscordAlertDispatcher)


# --------------------------------------------------------------------------- #
# 9. Judge 冻结告警（docs/dev/08 → 22）
# --------------------------------------------------------------------------- #


class RecordingAlerts:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, *, alert_type: str, run_id: str, payload: dict[str, Any]) -> None:
        self.sent.append({"alert_type": alert_type, "run_id": run_id, "payload": payload})


class FakeMissRepo:
    async def recent_window(
        self, *, model: str, temperature_bucket: str, window_size: int
    ) -> list[bool]:
        return [True] * 3 + [False] * 7


class FakeHealthRepo:
    def __init__(self, frozen: bool = False) -> None:
        self.frozen = frozen

    async def is_frozen(self, *, model: str, temperature_bucket: str) -> bool:
        return self.frozen

    async def upsert(self, **kwargs: Any) -> None:
        self.frozen = kwargs["frozen"]


class JudgeFreezeAlertTests:
    async def test_new_freeze_dispatches_one_alert(self) -> None:
        alerts, health = RecordingAlerts(), FakeHealthRepo()
        monitor = JudgeHealthMonitor(
            miss_repository=FakeMissRepo(),  # type: ignore[arg-type]
            health_repository=health,  # type: ignore[arg-type]
            miss_rate_threshold=0.05,
            window_size=10,
            alert_dispatcher=alerts,
        )
        await monitor.check(model="m", temperature=0.0)
        await monitor.check(model="m", temperature=0.0)  # 已冻结：不重复告警
        assert len(alerts.sent) == 1
        sent = alerts.sent[0]
        assert sent["alert_type"] == ALERT_TYPE_JUDGE_FROZEN
        assert sent["run_id"] == JUDGE_HEALTH_ALERT_RUN_ID
        assert sent["payload"]["miss_count"] == 3 and sent["payload"]["model"] == "m"
