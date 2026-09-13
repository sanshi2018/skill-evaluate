"""审查工作台 API 与人工审批回调端点（docs/dev/22 第 5~7 节；替换 docs/dev/05 的 501 桩）。

| 端点 | 用途 |
|---|---|
| `GET  /api/approvals?status=pending` | 卡片列表（`status=all` 不过滤；可选 `run_id`） |
| `GET  /api/approvals/{approval_id}/context` | 按 decision_type 展开证据（`approval_context.py`） |
| `POST /api/approvals/{approval_id}/decide` | 人工决策 → 翻译 resume payload → `resolve_suspension()` |
| `GET  /api/suggestions?status=pending` | 孤儿用例建议队列（docs/dev/17，非阻塞） |
| `POST /api/suggestions/{suggestion_id}/decide` | 确认 / 拒绝建议；确认时执行真正的归档动作 |
| `POST /hooks/approval/{run_id}/{node_name}` | 外部系统的 HMAC 签名回调（与 `hooks_hermes.py` 同构） |

**鉴权边界**（docs/dev/22 第 5 节）：`/api/*` 不做用户鉴权，假定部署在内网或团队已有的
SSO/网关之后；`decided_by` 如实记录调用方声明的身份。需要机器对机器调用又拿不到网关的
场景，走带 HMAC 签名的 `/hooks/approval/...`。

## 决策处理顺序（与正文第 6 节伪码的偏差与理由）

正文顺序是"存决定 → resolve_suspension → mark_resolved"。实现改为：

1. **前置校验**（不写任何状态）：卡片存在、仍是 pending、outcome 合法、阻塞卡片要求
   GraphResumer 已注册——`resolve_suspension()` 会先把账本迁到 resolved 再调 resumer，
   未注册时报错那一刻账本已改，这张卡片就再也唤醒不了；
2. **副作用**（如 UNFREEZE_JUDGE 的解冻）：幂等操作放在"抢占决策权"之前，失败时卡片仍
   是干净的 pending，人可以重试；
3. **抢占决策权**：`approval_decisions` 唯一约束，先到者赢，后到者 409；
4. **mark_resolved 先于唤醒**：`GraphResumer.resume()` 会在请求内继续跑图直到下一个挂起点，
   可能很久；先标记，工作台才不会在图跑的这段时间里一直显示这张卡片待处理；
5. **唤醒**（仅阻塞卡片）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from skill_evaluate.agents.judge.health import JudgeHealthMonitor
from skill_evaluate.api.approval_context import ApprovalContextBuilder
from skill_evaluate.api.security import verify_hmac_signature
from skill_evaluate.config import get_settings
from skill_evaluate.logging import get_logger
from skill_evaluate.persistence import suspension as _suspension
from skill_evaluate.persistence.repository import (
    ApprovalDecisionRepository,
    PendingApprovalRepository,
    TestCaseRepository,
    TestCaseSuggestionRepository,
)
from skill_evaluate.state.approval import (
    OUTCOME_CONFIRMED,
    OUTCOME_REJECTED,
    OUTCOME_UNFREEZE,
    ApprovalDecision,
    ApprovalDecisionRequest,
    ApprovalDecisionType,
    ApprovalStatus,
    PendingApproval,
    SuggestionDecisionRequest,
    allowed_outcomes,
)
from skill_evaluate.state.enums import SuggestionStatus, SuggestionType
from skill_evaluate.state.suggestion import TestCaseSuggestion

router = APIRouter()
logger = get_logger(node_name="hooks_approval")

APPROVAL_SIGNATURE_HEADER = "X-Approval-Signature"


def build_resume_payload(approval: PendingApproval, decision: ApprovalDecision) -> dict[str, Any]:
    """把人的 outcome 翻译成挂起节点期望的 resume payload（docs/dev/22 第 6 节 `_build_resume_payload`）。

    所有 decision_type 统一成 `{"decision": <outcome>, ...}` 一种形状，这是既有挂起点**早已**
    兼容的写法：

    - ACCEPT_PATCH：`optimizer/loop.py::_is_adopt()` 认 `{"decision": "adopt"}`，其余一律放弃
      （docs/dev/09 第 8 节待接入的两种走向，在这里正式落地）；
    - CONFIRM_TREE_REVIEW：`coverage/nodes.py::_is_tree_confirmed()` 认 `{"decision": "confirm"}`；
    - UNFREEZE_JUDGE / INJECT_NEW_SEED / ABANDON_RUN：`nodes/approval_guard.py::resume_decision()`；
    - RESOLVE_DEEP_CONFLICT：`multi_skill.deep_conflict_approval_gate` 同上。

    统一形状而不是按类型各造一种（如 `{"accepted": True}`）：挂起点只需要读一个键，
    新增 decision_type 时不必同时修改翻译表和解析函数两处。附带的审计字段节点可以忽略。
    """
    return {
        "decision": decision.outcome,
        "decision_type": approval.decision_type.value,
        "approval_id": approval.approval_id,
        "decided_by": decision.decided_by,
        "note": decision.note,
    }


class ApprovalDecisionHandler:
    """决策处理（端点逻辑与 HTTP 解耦，便于测试注入全部依赖）。"""

    def __init__(
        self,
        *,
        pending_repository: Any = None,
        decision_repository: Any = None,
        resolve: Callable[..., Awaitable[None]] | None = None,
        resumer_registered: Callable[[], bool] | None = None,
        judge_health_monitor_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._pending = pending_repository or PendingApprovalRepository()
        self._decisions = decision_repository or ApprovalDecisionRepository()
        # 运行时再取 suspension 模块里的函数：便于测试 monkeypatch，也与 hooks_hermes 同一唤醒入口。
        self._resolve = resolve or (lambda **kw: _suspension.resolve_suspension(**kw))
        self._resumer_registered = resumer_registered or _suspension.is_graph_resumer_registered
        self._judge_health = judge_health_monitor_factory or JudgeHealthMonitor

    async def decide(self, approval_id: str, request: ApprovalDecisionRequest) -> dict[str, Any]:
        approval = await self._pending.get(approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail=f"approval {approval_id!r} 不存在")
        return await self.decide_approval(approval, request)

    async def decide_approval(
        self, approval: PendingApproval, request: ApprovalDecisionRequest
    ) -> dict[str, Any]:
        outcome = request.outcome.strip().lower()
        self._validate(approval, outcome)

        # ---- 副作用（幂等，放在抢占决策权之前，见模块头第 2 步） ----
        if (
            approval.blocking
            and approval.decision_type is ApprovalDecisionType.UNFREEZE_JUDGE
            and outcome == OUTCOME_UNFREEZE
        ):
            await self._judge_health().unfreeze(
                model=str(approval.context_ref["model"]),
                temperature=float(approval.context_ref.get("temperature") or 0.0),
                operator=request.decided_by,
            )

        decision = ApprovalDecision(
            approval_id=approval.approval_id,
            decided_by=request.decided_by,
            outcome=outcome,
            note=request.note,
            decided_at=datetime.now(UTC),
        )
        if not await self._decisions.save(decision):
            raise HTTPException(status_code=409, detail="该审批已被其他人决定")
        await self._pending.mark_resolved(approval.approval_id)

        resumed = False
        if approval.blocking:
            await self._resolve(
                wait_key=approval.wait_key,
                resume_payload=build_resume_payload(approval, decision),
                thread_id=approval.thread_id,
            )
            resumed = True

        logger.warning(
            "human_approval_decided",
            approval_id=approval.approval_id,
            run_id=approval.run_id,
            decision_type=approval.decision_type.value,
            outcome=outcome,
            decided_by=request.decided_by,
            resumed=resumed,
        )
        return {"status": "resolved", "approval_id": approval.approval_id, "resumed": resumed}

    def _validate(self, approval: PendingApproval, outcome: str) -> None:
        """前置校验：任何一项不过都不写状态（模块头第 1 步）。"""
        if approval.status is not ApprovalStatus.PENDING:
            raise HTTPException(status_code=409, detail="该审批已处理")
        if approval.decision_type is ApprovalDecisionType.CONFIRM_ORPHAN_RETIREMENT:
            raise HTTPException(
                status_code=422, detail="孤儿用例建议请通过 /api/suggestions/{id}/decide 处理"
            )
        allowed = allowed_outcomes(approval.decision_type, blocking=approval.blocking)
        if outcome not in allowed:
            raise HTTPException(
                status_code=422,
                detail=f"outcome={outcome!r} 不适用于该卡片，可选：{sorted(allowed)}",
            )
        if (
            approval.decision_type is ApprovalDecisionType.UNFREEZE_JUDGE
            and outcome == OUTCOME_UNFREEZE
            and not approval.context_ref.get("model")
        ):
            raise HTTPException(
                status_code=422,
                detail="卡片缺少 model 引用，无法定位要解冻的 Judge 配置；请直接调用 "
                "JudgeHealthMonitor.unfreeze() 后选择 abandon 或等待重新发起",
            )
        if approval.blocking and not self._resumer_registered():
            raise HTTPException(
                status_code=503,
                detail="主图 GraphResumer 尚未注册，现在决策将无法唤醒流水线；请稍后重试"
                "（docs/dev/interfaces/04_graph_resumer.md）",
            )


class SuggestionDecisionHandler:
    """孤儿用例建议的决策（docs/dev/22 第 7 节）：**不**调用 resolve_suspension。"""

    def __init__(self, *, suggestion_repository: Any = None, case_repository: Any = None) -> None:
        self._suggestions = suggestion_repository or TestCaseSuggestionRepository()
        self._cases = case_repository or TestCaseRepository()

    async def decide(
        self, suggestion_id: str, request: SuggestionDecisionRequest
    ) -> dict[str, Any]:
        suggestion = await self._suggestions.get(suggestion_id)
        if suggestion is None:
            raise HTTPException(status_code=404, detail=f"suggestion {suggestion_id!r} 不存在")
        if suggestion.status is not SuggestionStatus.PENDING:
            raise HTTPException(status_code=409, detail="该建议已处理")
        outcome = request.outcome.strip().lower()
        if outcome not in (OUTCOME_CONFIRMED, OUTCOME_REJECTED):
            raise HTTPException(
                status_code=422, detail=f"outcome 只能是 {OUTCOME_CONFIRMED} / {OUTCOME_REJECTED}"
            )

        retired = False
        if outcome == OUTCOME_CONFIRMED:
            retired = await self._apply_confirmed(suggestion)

        # update_status 的 WHERE status='pending' 兜底并发：另一个人抢先决定时这里返回 False。
        # 归档动作是幂等的（split 已是 COLD 时不改），抢输的一方最多重复了一次无害归档。
        if not await self._suggestions.update_status(suggestion_id, SuggestionStatus(outcome)):
            raise HTTPException(status_code=409, detail="该建议已被其他人决定")

        logger.warning(
            "test_case_suggestion_decided",
            suggestion_id=suggestion_id,
            case_id=suggestion.case_id,
            suggestion_type=suggestion.suggestion_type.value,
            outcome=outcome,
            decided_by=request.decided_by,
            note=request.note,
            case_retired=retired,
        )
        return {"status": outcome, "suggestion_id": suggestion_id, "case_retired": retired}

    async def _apply_confirmed(self, suggestion: TestCaseSuggestion) -> bool:
        """确认后执行的真正动作，按建议类型显式分派（docs/dev/interfaces/17 第 9 节的要求）。

        新增 `SuggestionType` 却没在这里登记时走 501，而不是静默落进某个默认分支——
        "合并重复用例"与"淘汰孤儿用例"要执行的动作完全不同。
        """
        match suggestion.suggestion_type:
            case SuggestionType.ORPHAN_RETIREMENT:
                # split → COLD：永久保留归档，但不再参与任何主动评测（见 TestCaseRepository.retire）。
                return bool(await self._cases.retire(suggestion.case_id))
            case _:
                raise HTTPException(
                    status_code=501,
                    detail=f"建议类型 {suggestion.suggestion_type.value!r} 的确认动作尚未实现",
                )


# --------------------------------------------------------------------------- #
# 依赖提供者（测试用 app.dependency_overrides 替换）
# --------------------------------------------------------------------------- #


def get_pending_approval_repository() -> Any:
    return PendingApprovalRepository()


def get_approval_decision_handler() -> ApprovalDecisionHandler:
    return ApprovalDecisionHandler()


def get_approval_context_builder() -> ApprovalContextBuilder:
    return ApprovalContextBuilder()


def get_suggestion_repository() -> Any:
    return TestCaseSuggestionRepository()


def get_suggestion_decision_handler() -> SuggestionDecisionHandler:
    return SuggestionDecisionHandler()


# `Annotated` 形式的依赖声明（而不是参数默认值里调用 `Depends(...)`）：类型与注入方式写在一处，
# 测试用 `app.dependency_overrides[get_xxx]` 替换即可。
PendingRepoDep = Annotated[Any, Depends(get_pending_approval_repository)]
ContextBuilderDep = Annotated[ApprovalContextBuilder, Depends(get_approval_context_builder)]
DecisionHandlerDep = Annotated[ApprovalDecisionHandler, Depends(get_approval_decision_handler)]
SuggestionRepoDep = Annotated[Any, Depends(get_suggestion_repository)]
SuggestionHandlerDep = Annotated[
    SuggestionDecisionHandler, Depends(get_suggestion_decision_handler)
]


# --------------------------------------------------------------------------- #
# 审批卡片
# --------------------------------------------------------------------------- #


@router.get("/api/approvals")
async def list_approvals(
    repo: PendingRepoDep,
    status: str = "pending",
    run_id: str | None = None,
) -> list[PendingApproval]:
    """卡片列表。`status=pending`（默认）/ `resolved` / `all`。"""
    if status == "all":
        wanted: ApprovalStatus | None = None
    else:
        try:
            wanted = ApprovalStatus(status)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="status 只能是 pending/resolved/all"
            ) from exc
    approvals: list[PendingApproval] = await repo.list_by_status(wanted, run_id=run_id)
    return approvals


@router.get("/api/approvals/{approval_id}/context")
async def approval_context(
    approval_id: str,
    repo: PendingRepoDep,
    builder: ContextBuilderDep,
) -> dict[str, Any]:
    approval = await repo.get(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=f"approval {approval_id!r} 不存在")
    return await builder.build(approval)


@router.post("/api/approvals/{approval_id}/decide")
async def decide_approval(
    approval_id: str,
    decision: ApprovalDecisionRequest,
    handler: DecisionHandlerDep,
) -> dict[str, Any]:
    return await handler.decide(approval_id, decision)


# --------------------------------------------------------------------------- #
# 孤儿用例建议（非阻塞队列）
# --------------------------------------------------------------------------- #


@router.get("/api/suggestions")
async def list_suggestions(
    repo: SuggestionRepoDep,
    status: str = "pending",
) -> list[TestCaseSuggestion]:
    try:
        wanted = SuggestionStatus(status)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="status 只能是 pending/confirmed/rejected"
        ) from exc
    suggestions: list[TestCaseSuggestion] = await repo.list_by_status(wanted)
    return suggestions


@router.post("/api/suggestions/{suggestion_id}/decide")
async def decide_suggestion(
    suggestion_id: str,
    decision: SuggestionDecisionRequest,
    handler: SuggestionHandlerDep,
) -> dict[str, Any]:
    return await handler.decide(suggestion_id, decision)


# --------------------------------------------------------------------------- #
# HMAC 签名回调（docs/dev/interfaces/05_hooks_approval.md）
# --------------------------------------------------------------------------- #


@router.post("/hooks/approval/{run_id}/{node_name}")
async def approval_hook(
    run_id: str,
    node_name: str,
    request: Request,
    repo: PendingRepoDep,
    handler: DecisionHandlerDep,
) -> dict[str, Any]:
    """外部系统（工作台自动化等）的签名回调：定位该节点最新的 pending 卡片后走同一套决策逻辑。

    body 与 `/api/approvals/{id}/decide` 相同（`ApprovalDecisionRequest`）。签名是对原始 body
    的 HMAC-SHA256 十六进制摘要，放在 `X-Approval-Signature` 头；密钥
    `SKILLEVAL_APPROVAL_HMAC_SECRET` 未配置时一律拒绝（`verify_hmac_signature` 对空密钥返回 False）。
    """
    raw = await request.body()
    secret = get_settings().approval.hmac_secret.get_secret_value()
    if not verify_hmac_signature(raw, request.headers.get(APPROVAL_SIGNATURE_HEADER, ""), secret):
        logger.warning("approval_hook_signature_rejected", run_id=run_id, node_name=node_name)
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        decision = ApprovalDecisionRequest.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    approval = await repo.find_pending_by_node(run_id, node_name)
    if approval is None:
        raise HTTPException(
            status_code=404, detail=f"run_id={run_id!r} 节点 {node_name!r} 上没有待处理的审批"
        )
    return await handler.decide_approval(approval, decision)


__all__ = [
    "APPROVAL_SIGNATURE_HEADER",
    "ApprovalDecisionHandler",
    "SuggestionDecisionHandler",
    "build_resume_payload",
    "router",
]
