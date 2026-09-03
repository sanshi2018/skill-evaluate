"""Hermes Hook 端点（docs/dev/05 第 2.2 节，落地 docs/dev/03 第 4.4 节协议）。

端点本身不做业务判定（不算触发率、不做裁判），只做"接收-映射-落库-唤醒"，
业务判定留给被唤醒后继续跑的 LangGraph 节点——保持"薄 I/O 层，厚业务层"的
分层。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from skill_evaluate.api.security import verify_hmac_signature
from skill_evaluate.config import get_settings
from skill_evaluate.errors import ConfigurationError
from skill_evaluate.executors.hermes_backend import (
    HermesHookPayload,
    map_assertion_executions,
    map_hermes_payload_to_trace,
)
from skill_evaluate.logging import get_logger
from skill_evaluate.persistence.checkpointer import thread_id_for
from skill_evaluate.persistence.repository import (
    AssertionRepository,
    RunRepository,
    TraceRepository,
)
from skill_evaluate.persistence.suspension import resolve_suspension

router = APIRouter()
logger = get_logger(node_name="hooks_hermes")


@router.post("/hooks/hermes/{run_id}/{case_id}/{run_index}")
async def hermes_hook(
    run_id: str, case_id: str, run_index: int, request: Request
) -> dict[str, str]:
    raw = await request.body()
    settings = get_settings()
    signature = request.headers.get("X-Hermes-Signature", "")
    secret = settings.executor.hermes_hook_secret.get_secret_value()

    if not verify_hmac_signature(raw, signature, secret):
        logger.warning(
            "hermes_hook_signature_rejected", run_id=run_id, case_id=case_id, run_index=run_index
        )
        raise HTTPException(status_code=401, detail="invalid signature")

    payload = HermesHookPayload.model_validate_json(raw)
    trace = map_hermes_payload_to_trace(payload, case_id=case_id, run_index=run_index)

    await TraceRepository().save(trace)
    await _save_assertion_results(payload, run_id=run_id, case_id=case_id)

    run = await RunRepository().get(run_id)
    if run is None:
        # run 元数据缺失通常意味着 CLI/图入口未先调用 RunRepository.create()——
        # 这是调用方（docs/dev/24 主图装配）需要保证的前置条件，此处显式报错而非
        # 静默丢弃已落库的 trace。
        raise ConfigurationError(f"未找到 run_id={run_id!r} 的运行记录，无法确定 thread_id")

    thread_id = thread_id_for(skill_id=run["skill_id"], run_id=run_id)
    await resolve_suspension(
        wait_key=f"{run_id}:{case_id}:{run_index}",
        resume_payload=trace.trace_id,
        thread_id=thread_id,
    )
    return {"status": "accepted"}


async def _save_assertion_results(payload: HermesHookPayload, *, run_id: str, case_id: str) -> None:
    """docs/dev/10 第 4.3 节：payload 含 `assertion_executions` 时逐条落
    `assertion_results` 表（`passed = exit_code == 0`）。

    落库前先确认对应的 `AssertionSpec` 存在：`assertion_results.assertion_id` 是指向
    `assertion_specs` 的外键，spec 不存在时直接插入会以一个外键违例把整个 Hook 变成
    500，连**已经落库的 trace 都白落了**。这种情况说明沙箱回传了一个评测系统没规划过
    的 assertion_id（客户端实现有 bug 或 spec 未持久化），记 warning 跳过即可——
    Trace 是主线数据，不能被一条来路不明的断言拖垮。
    """
    results = map_assertion_executions(payload)
    if not results:
        return

    repo = AssertionRepository()
    for result in results:
        if await repo.get_spec(result.assertion_id) is None:
            logger.warning(
                "hermes_hook_unknown_assertion_id",
                run_id=run_id,
                case_id=case_id,
                assertion_id=result.assertion_id,
            )
            continue
        await repo.save_result(result)

    logger.info(
        "hermes_hook_assertions_saved",
        run_id=run_id,
        case_id=case_id,
        total=len(results),
        passed=sum(1 for r in results if r.passed),
    )
