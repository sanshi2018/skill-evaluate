"""Discord Webhook 通知通道：审批卡片 + 通用告警（docs/dev/22 第 4 节）。

## Discord 只做"通知 + 导流"，不做决策

卡片上只有摘要与一条指向审查工作台的深度链接，**没有**批准/拒绝按钮。结构化的补丁
diff、单跑 vs 并发的双路 Trace 并排比对，在聊天软件里既难看又难维护；决策统一在工作台
完成（`api/hooks_approval.py`），Discord 丢一条消息不会让任何审批丢失——卡片本体在
`pending_approvals` 表里。

## 两个通道、一个 Webhook

| 通道 | 协议 | 谁调用 |
|---|---|---|
| `DiscordApprovalNotifier` | `ApprovalNotifier`（本文件） | `persistence/approval_service.py`，每张新卡片一次 |
| `DiscordAlertDispatcher` | `observability/alerts.py::AlertDispatcher` | 08 冻结 / 20 深度冲突 / 21 连续坍塌 |

`configure_notification_channels()` 在进程启动时（`api/app.py` lifespan、CLI）调用一次，
按 `ApprovalSettings.discord_webhook_url` 是否配置决定注册 Discord 还是保留日志默认实现。

## 安全细节

- 卡片正文会带上 Skill 文本片段、LLM 生成的发现描述等**不受信内容**。一律
  `allowed_mentions={"parse": []}`：否则一段含 `@everyone` 的 SKILL.md 就能让评测系统替
  攻击者群发提醒。
- 按 Discord 的 embed 长度上限截断（标题 256 / 描述 4096 / 字段值 1024 / 最多 25 个字段），
  超限的请求会被 Discord 整体拒收，而不是自动截断。
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from skill_evaluate.config import Settings, get_settings
from skill_evaluate.errors import ObservabilityError
from skill_evaluate.logging import get_logger
from skill_evaluate.observability.alerts import set_alert_dispatcher
from skill_evaluate.state.approval import ApprovalDecisionType, PendingApproval

logger = get_logger(component="discord_notifier")

# ---- Discord embed 长度上限（官方文档值，超限整条消息 400） ----
_TITLE_LIMIT = 256
_DESCRIPTION_LIMIT = 4096
_FIELD_NAME_LIMIT = 256
_FIELD_VALUE_LIMIT = 1024
_MAX_FIELDS = 25

# ---- 卡片颜色（Discord 用十进制 RGB） ----
# 按"人需要多快响应"而不是按场景分色：红 = 流水线已停住在等人；橙 = 平台级可信度问题；
# 蓝 = 需要人补数据才能继续；灰 = 只是通知，流水线没在等。
_COLOR_BLOCKING_RED = 0xE74C3C
_COLOR_TRUST_ORANGE = 0xE67E22
_COLOR_DATA_BLUE = 0x3498DB
_COLOR_NOTICE_GREY = 0x95A5A6

_APPROVAL_COLORS: dict[ApprovalDecisionType, int] = {
    ApprovalDecisionType.ACCEPT_PATCH: _COLOR_BLOCKING_RED,
    ApprovalDecisionType.ABANDON_RUN: _COLOR_BLOCKING_RED,
    ApprovalDecisionType.RESOLVE_DEEP_CONFLICT: _COLOR_BLOCKING_RED,
    ApprovalDecisionType.UNFREEZE_JUDGE: _COLOR_TRUST_ORANGE,
    ApprovalDecisionType.CONFIRM_TREE_REVIEW: _COLOR_DATA_BLUE,
    ApprovalDecisionType.INJECT_NEW_SEED: _COLOR_DATA_BLUE,
    ApprovalDecisionType.CONFIRM_ORPHAN_RETIREMENT: _COLOR_NOTICE_GREY,
}

# 已知告警类型的中文标题。未知类型照样发（标题退回原始 alert_type）：新维度加了告警
# 却忘了在这里登记，结果应该是"标题不好看"，而不是"告警被吞"。
_ALERT_TITLES: dict[str, str] = {
    "judge_frozen": "Judge 配置已冻结",
    "deep_multi_skill_conflict": "深度多技能冲突",
    "generation_collapse_persistent": "Generator 连续语义坍塌",
}
_ALERT_COLORS: dict[str, int] = {
    "judge_frozen": _COLOR_TRUST_ORANGE,
    "deep_multi_skill_conflict": _COLOR_BLOCKING_RED,
    "generation_collapse_persistent": _COLOR_DATA_BLUE,
}


@runtime_checkable
class ApprovalNotifier(Protocol):
    """审批卡片通知协议。只负责"让人知道有张卡片"，发送失败由调用方吞掉（通知是旁路）。"""

    async def notify_approval(self, approval: PendingApproval) -> None: ...


class LoggingApprovalNotifier:
    """默认实现：只写结构化日志 `approval_card_dispatched`（未配置 Webhook 时使用）。

    与 `LoggingAlertDispatcher` 同一理由：事件名固定、字段完整，运维可以直接基于日志平台
    转发；接入 Discord 后它不再被调用，审计靠 `pending_approvals` 表本身。
    """

    def __init__(self) -> None:
        self.sent: list[PendingApproval] = []  # 便于本地调试 / 测试断言

    async def notify_approval(self, approval: PendingApproval) -> None:
        self.sent.append(approval)
        logger.warning(
            "approval_card_dispatched",
            approval_id=approval.approval_id,
            run_id=approval.run_id,
            decision_type=approval.decision_type.value,
            blocking=approval.blocking,
            wait_key=approval.wait_key,
            summary=approval.context_summary[:500],
        )


# --------------------------------------------------------------------------- #
# 卡片构造（纯函数，便于测试）
# --------------------------------------------------------------------------- #


def workbench_link(workbench_base_url: str, approval: PendingApproval) -> str:
    """审查工作台深度链接（docs/dev/22 第 4 节原文格式 + approval_id）。

    追加 `approval_id` 查询参数：正文只带 `wait_key`，但决策 API 以 approval_id 为路径参数，
    前端拿到链接后不必再按 wait_key 反查一次。wait_key 含冒号，需要 URL 编码。
    """
    base = workbench_base_url.rstrip("/")
    return (
        f"{base}/runs/{quote(approval.run_id, safe='')}"
        f"?wait_key={quote(approval.wait_key, safe='')}"
        f"&approval_id={quote(approval.approval_id, safe='')}"
    )


def build_approval_card(approval: PendingApproval, *, workbench_base_url: str) -> dict[str, Any]:
    """把一张审批卡片渲染成 Discord Webhook payload。

    非阻塞卡片标题用"[通知]"而不是"[待审批]"：让人一眼分得清"流水线停在那里等我"和
    "只是让我知道一下"（docs/dev/22 第 8.1 节的双模式）。
    """
    prefix = "[待审批]" if approval.blocking else "[通知]"
    color = (
        _APPROVAL_COLORS.get(approval.decision_type, _COLOR_BLOCKING_RED)
        if approval.blocking
        else _COLOR_NOTICE_GREY
    )
    fields = [
        _field("run_id", approval.run_id),
        _field("节点", approval.node_name),
        _field("审查工作台", workbench_link(workbench_base_url, approval)),
    ]
    return {
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": _truncate(f"{prefix} {approval.decision_type.value}", _TITLE_LIMIT),
                "description": _truncate(approval.context_summary, _DESCRIPTION_LIMIT),
                "fields": fields,
                "color": color,
            }
        ],
    }


def build_alert_card(
    *, alert_type: str, run_id: str, payload: dict[str, Any], workbench_base_url: str
) -> dict[str, Any]:
    """把一条通用告警渲染成 Discord Webhook payload。

    payload 的每个键渲染成一个字段（list/dict 以 JSON 形式展示）。告警 payload 由各维度
    按自己的语义组织，这里不做字段级理解——理解它们是工作台的事，卡片只负责如实转述。
    """
    title = _ALERT_TITLES.get(alert_type, alert_type)
    fields = [_field("run_id", run_id)]
    if not run_id.startswith("platform:"):
        # 平台级告警（如 Judge 冻结）没有对应的运行页面，不给一条必然 404 的链接。
        fields.append(
            _field("审查工作台", f"{workbench_base_url.rstrip('/')}/runs/{quote(run_id, safe='')}")
        )
    for key, value in payload.items():
        if len(fields) >= _MAX_FIELDS:
            break
        fields.append(_field(key, _render_value(value)))
    return {
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": _truncate(f"[告警] {title}", _TITLE_LIMIT),
                "description": _truncate(f"alert_type={alert_type}", _DESCRIPTION_LIMIT),
                "fields": fields,
                "color": _ALERT_COLORS.get(alert_type, _COLOR_BLOCKING_RED),
            }
        ],
    }


def _field(name: str, value: str) -> dict[str, Any]:
    # Discord 拒收空字段值，空串替换成占位符。
    return {
        "name": _truncate(name, _FIELD_NAME_LIMIT),
        "value": _truncate(value or "—", _FIELD_VALUE_LIMIT),
        "inline": False,
    }


def _render_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# 真实通道
# --------------------------------------------------------------------------- #


class _DiscordWebhookPoster:
    """最小的 Webhook 发送器。两个通道共用，保证超时与错误处理口径一致。"""

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout_s: float,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not webhook_url:
            raise ObservabilityError("Discord Webhook URL 为空，无法构造 Discord 通道")
        self._url = webhook_url
        self._timeout_s = timeout_s
        # 允许注入 client：测试用 httpx.MockTransport，不发真实请求。
        self._client = http_client

    async def post(self, payload: dict[str, Any]) -> None:
        try:
            if self._client is not None:
                response = await self._client.post(self._url, json=payload, timeout=self._timeout_s)
            else:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    response = await client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            # 不把 URL 写进异常：Webhook URL 本身就是凭据。
            raise ObservabilityError(f"Discord Webhook 请求失败：{type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise ObservabilityError(
                f"Discord Webhook 返回 {response.status_code}：{response.text[:200]}"
            )


class DiscordApprovalNotifier:
    """审批卡片 → Discord（实现 `ApprovalNotifier`）。"""

    def __init__(
        self,
        webhook_url: str,
        *,
        workbench_base_url: str,
        timeout_s: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._poster = _DiscordWebhookPoster(
            webhook_url, timeout_s=timeout_s, http_client=http_client
        )
        self._workbench_base_url = workbench_base_url

    async def notify_approval(self, approval: PendingApproval) -> None:
        await self._poster.post(
            build_approval_card(approval, workbench_base_url=self._workbench_base_url)
        )
        logger.info(
            "approval_card_sent_to_discord",
            approval_id=approval.approval_id,
            run_id=approval.run_id,
            decision_type=approval.decision_type.value,
        )


class DiscordAlertDispatcher:
    """通用告警 → Discord（实现 `observability/alerts.py::AlertDispatcher`）。

    这是 docs/dev/interfaces/20 第 4 节 / 21 第 5 节留下的"真实告警通道"。
    ⚠️ 本类**绝不**调用 `suspend_and_wait()`：`send()` 被 `dispatch_alert()` 包在 try/except
    里，`interrupt()` 抛出的 `GraphInterrupt` 会被当成通道故障吞掉（interfaces/20 第 4.3 节）。
    需要阻塞的场景走 `approval_service.request_human_approval()`。
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        workbench_base_url: str,
        timeout_s: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._poster = _DiscordWebhookPoster(
            webhook_url, timeout_s=timeout_s, http_client=http_client
        )
        self._workbench_base_url = workbench_base_url

    async def send(self, *, alert_type: str, run_id: str, payload: dict[str, Any]) -> None:
        await self._poster.post(
            build_alert_card(
                alert_type=alert_type,
                run_id=run_id,
                payload=payload,
                workbench_base_url=self._workbench_base_url,
            )
        )
        logger.info("alert_sent_to_discord", alert_type=alert_type, run_id=run_id)


# --------------------------------------------------------------------------- #
# 进程级注册
# --------------------------------------------------------------------------- #

_approval_notifier: ApprovalNotifier = LoggingApprovalNotifier()


def get_approval_notifier() -> ApprovalNotifier:
    """当前进程注册的审批卡片通道（未注册时是 `LoggingApprovalNotifier`）。"""
    return _approval_notifier


def set_approval_notifier(notifier: ApprovalNotifier) -> None:
    global _approval_notifier
    _approval_notifier = notifier


def configure_notification_channels(settings: Settings | None = None) -> bool:
    """按配置注册通知通道（进程启动时调用一次），返回是否启用了 Discord。

    未配置 Webhook 时**不覆盖**已注册的通道：测试或嵌入方可能先手动 `set_*` 了自定义实现，
    启动流程不该把它悄悄换回日志默认实现。
    """
    approval_settings = (settings or get_settings()).approval
    webhook_url = approval_settings.discord_webhook_url.get_secret_value()
    if not webhook_url:
        logger.warning(
            "notification_channels_logging_only",
            reason="SKILLEVAL_APPROVAL_DISCORD_WEBHOOK_URL 未配置，审批卡片与告警只写结构化日志",
        )
        return False
    common: dict[str, Any] = {
        "workbench_base_url": approval_settings.workbench_base_url,
        "timeout_s": approval_settings.notify_timeout_s,
    }
    set_approval_notifier(DiscordApprovalNotifier(webhook_url, **common))
    set_alert_dispatcher(DiscordAlertDispatcher(webhook_url, **common))
    logger.info("notification_channels_discord_enabled")
    return True


__all__ = [
    "ApprovalNotifier",
    "DiscordAlertDispatcher",
    "DiscordApprovalNotifier",
    "LoggingApprovalNotifier",
    "build_alert_card",
    "build_approval_card",
    "configure_notification_channels",
    "get_approval_notifier",
    "set_approval_notifier",
    "workbench_link",
]
