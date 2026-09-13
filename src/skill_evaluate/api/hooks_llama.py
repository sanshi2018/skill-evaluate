"""`llama_control` 回调端点（docs/dev/19 第 3 节，callback 等待模式的唤醒入口）。

与 `hooks_hermes.py` 同一种"薄 I/O 层"：验签 → 映射 → 落库 → 唤醒，不做任何业务
判定。只在 `SKILLEVAL_EXECUTOR_LLAMA_CONTROL_WAIT_MODE=callback` 时会被调用；poll
模式下后端自己轮询，这个端点闲置无害。

签名头用 `X-Llama-Signature`、密钥用 `llama_control_hook_secret`，与 Hermes 分开：
两个外部运行时的信任边界不同，一边的密钥泄露不应让另一边的回调也能被伪造。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from skill_evaluate.api.security import verify_hmac_signature
from skill_evaluate.config import get_settings
from skill_evaluate.errors import ConfigurationError
from skill_evaluate.executors.hermes_backend import map_hermes_payload_to_trace
from skill_evaluate.executors.llama_backend import BACKEND_NAME, LlamaRunPayload, llama_wait_key
from skill_evaluate.logging import get_logger
from skill_evaluate.persistence.checkpointer import thread_id_for
from skill_evaluate.persistence.repository import RunRepository, TraceRepository
from skill_evaluate.persistence.suspension import resolve_suspension

router = APIRouter()
logger = get_logger(node_name="hooks_llama_control")

SIGNATURE_HEADER = "X-Llama-Signature"


@router.post(f"/hooks/{BACKEND_NAME}/{{run_id}}/{{case_id}}/{{run_index}}")
async def llama_control_hook(
    run_id: str, case_id: str, run_index: int, request: Request
) -> dict[str, str]:
    raw = await request.body()
    secret = get_settings().executor.llama_control_hook_secret.get_secret_value()
    if not verify_hmac_signature(raw, request.headers.get(SIGNATURE_HEADER, ""), secret):
        logger.warning(
            "llama_control_hook_signature_rejected",
            run_id=run_id,
            case_id=case_id,
            run_index=run_index,
        )
        raise HTTPException(status_code=401, detail="invalid signature")

    # 结果体与 Hermes 同构，走同一个映射函数：对照实验的两条臂必须用同一套口径
    # 生成 Trace（见 `executors/llama_backend.py` 模块头）。
    payload = LlamaRunPayload.model_validate_json(raw)
    trace = map_hermes_payload_to_trace(payload, case_id=case_id, run_index=run_index)
    await TraceRepository().save(trace)

    run = await RunRepository().get(run_id)
    if run is None:
        raise ConfigurationError(f"未找到 run_id={run_id!r} 的运行记录，无法确定 thread_id")

    await resolve_suspension(
        wait_key=llama_wait_key(run_id, case_id, run_index),
        resume_payload=trace.trace_id,
        thread_id=thread_id_for(skill_id=run["skill_id"], run_id=run_id),
    )
    return {"status": "accepted"}
