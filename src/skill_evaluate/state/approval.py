"""统一的人工决策模型（docs/dev/22 第 2 节）。

## 为什么需要"统一"

截至 docs/dev/21，全项目散落着六处"需要人类介入"的场景（Optimizer 最大重试、Judge
冻结、能力树规模确认、深度多技能冲突、生成坍塌连续失败、孤儿用例建议），各自用
`human_approvals` 占位表 + 各不相同的 resume payload 形状。审查工作台若要逐个适配，
每新增一种场景就要改一次前端。本文件把它们收口成**一个**数据契约：

- `PendingApproval`：一张"待人处理的卡片"——工作台只需按 `decision_type` 选渲染模板；
- `ApprovalDecision`：人的决定——`outcome` 必须落在 `APPROVAL_OUTCOMES` 声明的集合里；
- 决定如何翻译成挂起节点能读懂的 resume payload，见 `api/hooks_approval.py`。

## 与 `human_approvals` 表的分工（与 docs/dev/22 正文的偏差）

正文写"新表替换其占位的 `human_approvals` 表定义"。实现阶段**保留** `human_approvals`：
它已经是 `persistence/suspension.py::resolve_suspension()` 的挂起账本（`wait_key` 的
waiting → resolved 状态迁移 + 重复回调幂等），Hermes/Llama 回调与之同构，替换它会让
已验证的唤醒路径整体返工。因此两张表分层：

| 表 | 层次 | 内容 |
|---|---|---|
| `human_approvals` | 挂起账本（docs/dev/04） | wait_key / thread_id / waiting→resolved |
| `pending_approvals` | 业务卡片（本文档） | decision_type / 摘要 / 证据引用 / 是否阻塞 |
| `approval_decisions` | 审计 | 谁、何时、选了什么、备注 |

阻塞式审批两张都写；非阻塞通知（docs/dev/22 第 8.1 节）只写 `pending_approvals`——它
没有挂起点，也就不需要账本。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ApprovalDecisionType(StrEnum):
    """一张审批卡片"要人决定什么"（docs/dev/22 第 2 节）。工作台按它选渲染模板与操作按钮。"""

    ACCEPT_PATCH = "accept_patch"  # 09: 闭环耗尽重试，是否采纳最后一个候选补丁
    # 09/15/19 等: 流水线因共识未达成等原因即将放弃本次评测——人决定"重试该节点"还是"确认放弃"；
    # 同时用作基础设施故障（21 前置门禁）的**非阻塞**通知卡片类型
    ABANDON_RUN = "abandon_run"
    UNFREEZE_JUDGE = "unfreeze_judge"  # 08: 解冻 Judge Agent 配置
    CONFIRM_TREE_REVIEW = "confirm_tree_review"  # 16: 确认能力树规模，继续映射计算
    RESOLVE_DEEP_CONFLICT = "resolve_deep_conflict"  # 20: 记录多技能冲突的人工裁定说明
    INJECT_NEW_SEED = "inject_new_seed"  # 21: 人工补充种子数据后恢复 Generator
    # 17: 非阻塞队列，走独立 API（/api/suggestions）而非 suspend 流程（docs/dev/22 第 7 节）。
    # 保留这个枚举值是为了让"六处场景收口"在类型层面是完整的；它**不会**出现在 pending_approvals 里。
    CONFIRM_ORPHAN_RETIREMENT = "confirm_orphan_retirement"


class ApprovalStatus(StrEnum):
    """卡片状态。只有两态：人处理过没有。处理结果本身在 `approval_decisions` 里。"""

    PENDING = "pending"
    RESOLVED = "resolved"


# ---- outcome 取值 ----
# 做成常量而不是散落的字符串字面量：挂起节点（解析 resume payload）与 API（校验人的输入）
# 必须用同一套词，写错一个字母的后果是"人点了批准，节点却按放弃处理"。
OUTCOME_ADOPT = "adopt"  # 与 optimizer/loop.py::RESUME_ADOPT 同值（09 已实现的解析口径）
OUTCOME_CONFIRM = "confirm"  # 与 nodes/coverage/nodes.py::RESUME_CONFIRM 同值
OUTCOME_REJECT = "reject"
OUTCOME_RETRY = "retry"
OUTCOME_ABANDON = "abandon"
OUTCOME_UNFREEZE = "unfreeze"
OUTCOME_ACKNOWLEDGE = "acknowledge"
OUTCOME_SEED_INJECTED = "seed_injected"
OUTCOME_CONFIRMED = "confirmed"  # 孤儿用例建议，与 SuggestionStatus.CONFIRMED 同值
OUTCOME_REJECTED = "rejected"  # 孤儿用例建议，与 SuggestionStatus.REJECTED 同值

# 阻塞式审批：每种 decision_type 允许的 outcome。
# **每一项都含一个"否定"选项**（abandon / reject）：人必须始终有权说"不"，否则审批就
# 退化成了一个只能点"继续"的确认框。
APPROVAL_OUTCOMES: dict[ApprovalDecisionType, frozenset[str]] = {
    ApprovalDecisionType.ACCEPT_PATCH: frozenset({OUTCOME_ADOPT, OUTCOME_ABANDON}),
    ApprovalDecisionType.ABANDON_RUN: frozenset({OUTCOME_RETRY, OUTCOME_ABANDON}),
    ApprovalDecisionType.UNFREEZE_JUDGE: frozenset({OUTCOME_UNFREEZE, OUTCOME_ABANDON}),
    ApprovalDecisionType.CONFIRM_TREE_REVIEW: frozenset({OUTCOME_CONFIRM, OUTCOME_REJECT}),
    ApprovalDecisionType.RESOLVE_DEEP_CONFLICT: frozenset({OUTCOME_ACKNOWLEDGE, OUTCOME_ABANDON}),
    ApprovalDecisionType.INJECT_NEW_SEED: frozenset({OUTCOME_SEED_INJECTED, OUTCOME_ABANDON}),
    ApprovalDecisionType.CONFIRM_ORPHAN_RETIREMENT: frozenset(
        {OUTCOME_CONFIRMED, OUTCOME_REJECTED}
    ),
}

# 非阻塞通知卡片只接受"已知悉"：流水线并没有在等这个决定，此时提供"放弃"按钮会让人
# 误以为点了就能叫停一次早已跑完的评测。
NON_BLOCKING_OUTCOMES: frozenset[str] = frozenset({OUTCOME_ACKNOWLEDGE})


def allowed_outcomes(decision_type: ApprovalDecisionType, *, blocking: bool) -> frozenset[str]:
    """某张卡片允许的 outcome 集合（API 校验与工作台渲染按钮共用）。"""
    return APPROVAL_OUTCOMES[decision_type] if blocking else NON_BLOCKING_OUTCOMES


def approval_id_for(wait_key: str) -> str:
    """由 `wait_key` 派生**确定性**的 approval_id。

    为什么不用随机 uuid4：LangGraph 动态 `interrupt()` 恢复时会**整体重跑**挂起的节点
    （docs/dev/interfaces/16 第 5 节），`request_human_approval()` 因此会被再调一次。
    id 确定 → 第二次写入命中唯一约束被忽略，工作台上不会出现同一个挂起点的两张卡片。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"skill-evaluate:approval:{wait_key}"))


class PendingApproval(BaseModel):
    """一张待人处理的审批卡片（`pending_approvals` 表的契约）。

    相对 docs/dev/22 正文追加三个字段（追加式扩展，不改既有字段语义）：
    `node_name` / `thread_id`（决策 API 唤醒时要用，正文按 run_id 现算，但各挂起点
    传入的 thread_id 口径并不总一致，记下挂起时的实际值才可靠）、`blocking`
    （第 8.1 节的双模式：False 表示只是通知，决策时不调 `resolve_suspension`）。
    """

    approval_id: str
    run_id: str
    wait_key: str  # 对应 docs/dev/04 suspend_and_wait 的 wait_key
    decision_type: ApprovalDecisionType
    context_summary: str  # 人在 Discord 卡片/工作台看到的一段摘要
    # 指向具体证据的引用（patch_id / model / skill_id / case_ids ...），只放 JSON 可序列化值。
    # 展开逻辑按 decision_type 分派，见 api/approval_context.py。
    context_ref: dict[str, Any] = Field(default_factory=dict)
    node_name: str
    thread_id: str
    blocking: bool = True
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime
    resolved_at: datetime | None = None


class ApprovalDecision(BaseModel):
    """人对一张卡片的决定（`approval_decisions` 表的契约，审计用，一张卡片只有一条）。"""

    approval_id: str
    # 人类操作者标识（邮箱/用户名）。本项目不负责鉴权，只如实记录调用方声明的身份
    # （docs/dev/22 第 5 节鉴权边界声明）。
    decided_by: str
    outcome: str  # 必须落在 allowed_outcomes(decision_type, blocking=...) 里
    note: str | None = None
    decided_at: datetime


class ApprovalDecisionRequest(BaseModel):
    """`POST /api/approvals/{approval_id}/decide` 的请求体。

    与正文"body: ApprovalDecision"的偏差：`approval_id` 取自路径、`decided_at` 取服务端
    时钟——让客户端自报这两项只会制造"路径与 body 不一致"与"时间被回填"两类歧义。
    """

    decided_by: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    note: str | None = None


class SuggestionDecisionRequest(BaseModel):
    """`POST /api/suggestions/{suggestion_id}/decide` 的请求体（docs/dev/22 第 7 节）。"""

    decided_by: str = Field(min_length=1)
    outcome: str = Field(min_length=1)  # confirmed | rejected
    note: str | None = None


__all__ = [
    "APPROVAL_OUTCOMES",
    "NON_BLOCKING_OUTCOMES",
    "OUTCOME_ABANDON",
    "OUTCOME_ACKNOWLEDGE",
    "OUTCOME_ADOPT",
    "OUTCOME_CONFIRM",
    "OUTCOME_CONFIRMED",
    "OUTCOME_REJECT",
    "OUTCOME_REJECTED",
    "OUTCOME_RETRY",
    "OUTCOME_SEED_INJECTED",
    "OUTCOME_UNFREEZE",
    "ApprovalDecision",
    "ApprovalDecisionRequest",
    "ApprovalDecisionType",
    "ApprovalStatus",
    "PendingApproval",
    "SuggestionDecisionRequest",
    "allowed_outcomes",
    "approval_id_for",
]
