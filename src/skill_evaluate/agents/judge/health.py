"""裁判失误率监控与冻结（docs/dev/08 第 3.3 节）。

三件事：

1. 每一次黄金用例判决都记一笔（命中与失误**都记**，否则失误率没有分母）；
2. 滑动窗口统计最近 N 次的失误率，超过阈值（默认 5%）触发告警并**冻结**该
   Judge 配置；
3. 冻结后，`judgmental_verdict()` 对该配置的调用直接抛 `JudgeFrozenError`。

**冻结粒度是 `(model, temperature_bucket)`**：换一个模型或换一档温度就是另一个
裁判，不该因为别的配置误判而被连坐；反过来，一个被证明会误判的配置也不该靠"换
个 subject 再试一次"绕过。

**为什么是冻结而不是自动降级**：一个已被统计证明会误判的裁判，它给出的任何结论
都不该进报告。此时正确的行为是让流水线整体挂起等人工介入（调 Prompt / 换模型 /
补黄金用例），而不是"那就当它 PASS 吧"继续跑完——后者会产出一份看起来正常、
实际毫无公信力的报告，比直接失败危险得多。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from skill_evaluate.agents.llm import model_supports_sampling
from skill_evaluate.config import get_settings
from skill_evaluate.errors import JudgeFrozenError
from skill_evaluate.logging import get_logger
from skill_evaluate.persistence.repository import JudgeHealthRepository, JudgeMissRepository
from skill_evaluate.state.enums import JudgeVerdictStatus
from skill_evaluate.state.golden import GoldenCase, JudgeMissRecord

logger = get_logger(component="judge_health")


def temperature_bucket(model: str, temperature: float) -> str:
    """把温度归到冻结粒度用的分桶。

    模型不接受采样参数时返回 `"n/a"`——此时所有请求温度在物理上是同一档，硬按
    请求值分桶会把同一个裁判拆成三个互不相干的统计口径，每个口径的样本数都不够
    触发阈值，冻结机制就形同虚设了。
    """
    if not model_supports_sampling(model):
        return "n/a"
    if temperature < 0.2:
        return "low"
    if temperature < 0.6:
        return "mid"
    return "high"


@dataclass(slots=True)
class JudgeHealth:
    """一次健康检查的结果。"""

    model: str
    temperature_bucket: str
    window_size: int  # 实际参与统计的样本数（不足窗口时就是实际条数）
    miss_count: int
    miss_rate: float
    frozen: bool
    healthy: bool


class JudgeHealthMonitor:
    """黄金用例判决的记账 + 失误率检查 + 冻结/解冻。"""

    def __init__(
        self,
        *,
        miss_repository: JudgeMissRepository | None = None,
        health_repository: JudgeHealthRepository | None = None,
        miss_rate_threshold: float | None = None,
        window_size: int | None = None,
    ) -> None:
        settings = get_settings().judge
        self._miss_repo = miss_repository or JudgeMissRepository()
        self._health_repo = health_repository or JudgeHealthRepository()
        self._threshold = (
            settings.miss_rate_threshold if miss_rate_threshold is None else miss_rate_threshold
        )
        self._window_size = settings.health_window_size if window_size is None else window_size

    # ------------------------------------------------------------------ #
    # 记账
    # ------------------------------------------------------------------ #

    async def record_outcome(
        self,
        *,
        golden: GoldenCase,
        judge_status: JudgeVerdictStatus,
        model: str,
        temperature: float,
    ) -> JudgeMissRecord:
        """比对判决与人类标定，落一条记录并返回它（`is_miss` 表示是否失误）。"""
        is_miss = judge_status != golden.human_labeled_status
        record = JudgeMissRecord(
            miss_id=str(uuid.uuid4()),
            golden_id=golden.golden_id,
            judge_output_status=judge_status,
            occurred_at=datetime.now(UTC),
            model=model,
            temperature=temperature,
            is_miss=is_miss,
        )
        bucket = temperature_bucket(model, temperature)
        await self._miss_repo.record(record, temperature_bucket=bucket)

        log = logger.warning if is_miss else logger.info
        log(
            "judge_golden_outcome",
            golden_id=golden.golden_id,
            model=model,
            temperature_bucket=bucket,
            judge_status=judge_status.value,
            human_labeled_status=golden.human_labeled_status.value,
            is_miss=is_miss,
        )
        return record

    # ------------------------------------------------------------------ #
    # 检查与冻结
    # ------------------------------------------------------------------ #

    async def check(
        self, *, model: str, temperature: float, window_size: int | None = None
    ) -> JudgeHealth:
        """统计滑动窗口失误率；超阈值则冻结该配置并告警。

        样本数为 0 时视为健康——还没考过试不等于考砸了，那样会让一个全新部署的
        环境在第一次评测前就被冻死。
        """
        bucket = temperature_bucket(model, temperature)
        size = self._window_size if window_size is None else window_size
        window = await self._miss_repo.recent_window(
            model=model, temperature_bucket=bucket, window_size=size
        )
        miss_count = sum(1 for is_miss in window if is_miss)
        miss_rate = (miss_count / len(window)) if window else 0.0
        should_freeze = bool(window) and miss_rate > self._threshold

        already_frozen = await self._health_repo.is_frozen(model=model, temperature_bucket=bucket)
        frozen = should_freeze or already_frozen

        reason = (
            f"黄金基准失误率 {miss_rate:.1%} 超过阈值 {self._threshold:.1%}"
            f"（最近 {len(window)} 次判决中失误 {miss_count} 次）"
            if should_freeze
            else None
        )
        await self._health_repo.upsert(
            model=model,
            temperature_bucket=bucket,
            frozen=frozen,
            miss_rate=miss_rate,
            window_size=len(window),
            reason=reason,
        )

        if should_freeze and not already_frozen:
            # 平台级告警。当前只有结构化日志这一条渠道；实际推送（Discord Webhook）
            # 由 docs/dev/22 接入，接入点就是这条日志事件名。
            logger.error(
                "judge_frozen",
                model=model,
                temperature_bucket=bucket,
                miss_rate=miss_rate,
                miss_count=miss_count,
                window_size=len(window),
                threshold=self._threshold,
                action="该 Judge 配置的评测权限已冻结，需人工调整 Prompt/更换模型后解冻",
            )

        return JudgeHealth(
            model=model,
            temperature_bucket=bucket,
            window_size=len(window),
            miss_count=miss_count,
            miss_rate=miss_rate,
            frozen=frozen,
            healthy=not frozen,
        )

    async def ensure_not_frozen(self, *, model: str, temperature: float) -> None:
        """在真正发起裁量判定前调用。已冻结的配置直接抛错，不发请求。"""
        bucket = temperature_bucket(model, temperature)
        status = await self._health_repo.get(model=model, temperature_bucket=bucket)
        if status and status["frozen"]:
            raise JudgeFrozenError(
                f"Judge 配置已冻结（model={model!r}, temperature_bucket={bucket!r}）："
                f"{status['reason'] or '失误率超阈值'}。"
                "请人工调整 Prompt 或更换模型后，经审批工作台（docs/dev/22）解冻。"
            )

    async def unfreeze(self, *, model: str, temperature: float, operator: str) -> None:
        """人工解冻（docs/dev/22 审批工作台调用）。

        解冻不清空历史失误记录：窗口里的旧失误仍然会被下一次 `check()` 统计到。
        这是有意的——想让统计口径立刻变干净，只能靠新的黄金判决把旧记录挤出窗口，
        也就是"用新表现证明自己"，而不是"点一下按钮把历史抹掉"。
        """
        bucket = temperature_bucket(model, temperature)
        await self._health_repo.upsert(
            model=model,
            temperature_bucket=bucket,
            frozen=False,
            miss_rate=0.0,
            window_size=0,
            reason=f"manually unfrozen by {operator}",
        )
        logger.warning("judge_unfrozen", model=model, temperature_bucket=bucket, operator=operator)


async def check_judge_health(window_size: int = 50) -> bool:
    """docs/dev/08 第 3.3 节的模块级入口：检查**默认 Judge 配置**是否健康。

    调度时机由 docs/dev/24 决定（每次评测运行后检查一次，还是独立定时任务），
    本文档不预设——它只保证"随时可以被调用一次"。需要检查非默认配置的调用方，
    直接用 `JudgeHealthMonitor.check(model=..., temperature=...)`。
    """
    settings = get_settings()
    health = await JudgeHealthMonitor().check(
        model=settings.llm.judge_model,
        temperature=0.0,
        window_size=window_size,
    )
    return health.healthy


__all__ = [
    "JudgeHealth",
    "JudgeHealthMonitor",
    "check_judge_health",
    "temperature_bucket",
]
