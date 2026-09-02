"""人工审批回调端点（桩，docs/dev/22 详述并实现）。

复用 `security.py` 的签名校验模式（届时换一套独立的 secret 配置项），以及
`persistence.suspension` 的 `resolve_suspension()` 通用唤醒机制——与
`hooks_hermes.py` 是同一种"外部事件唤醒挂起图"模式的两个具体化实例。

见 docs/dev/interfaces/05_hooks_approval.md。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.post("/hooks/approval/{run_id}/{node_name}")
async def approval_hook(run_id: str, node_name: str) -> dict[str, str]:
    """docs/dev/22 需要实现：

    1. 新增 `ApprovalSettings`（挂到 `config.py`）承载独立的 HMAC secret / 审查
       工作台鉴权方式。
    2. 解析审批工作台回传的 payload（批准/驳回 + 修改后的 SKILL.md 差异等）。
    3. 调用 `persistence.repository.HumanApprovalRepository` 落库 + 调用
       `persistence.suspension.resolve_suspension()` 唤醒对应挂起节点。
    """
    raise HTTPException(
        status_code=501,
        detail=f"人工审批回调端点尚未实现（docs/dev/22 接入，run_id={run_id}, node_name={node_name}）",
    )
