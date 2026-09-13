"""前置门禁节点：沙箱指纹校验 → 金丝雀探针（docs/dev/21 第 4~7 节）。

```
preflight.sandbox_fingerprint_gate（ENTRY_NODE）
      ↓
preflight.canary_probe_gate（TERMINAL_NODE）
      ↓
（docs/dev/24：其余全部评测维度子图 11~20 展开）
```

两道门禁都**不属于任何评测维度**：失败即抛 `InfrastructureEnvironmentError`
（`PipelineSuspended` 子类）让整条流水线挂起，不写 `dimension_results`、不生成"某维度 FAIL"——
此时的失败与被测 Skill 质量无关，是评测系统自身的基础设施问题（docs/dev/03 把
`ExecutorBackendError` 与业务判定失败分离的设计初衷在这里的又一次体现）。

## 相对 docs/dev/21 正文的实现修正

1. **节点只返回增量**：正文 `return state` 会让主图里 `executed_trace_ids` 等 add-reducer 字段翻倍。
2. **金丝雀不写进 `HermesBackend.health_check()`**：理由见 `executors/canary.py` 与
   `HermesBackend.health_check()` 的 docstring；节点调 `run_canary_probe()`。
3. **缺少黄金指纹 = 门禁失败**，而不是跳过：没有基线就证明不了"没有漂移"。
4. **`off` 模式**：两道门禁都允许显式关闭（仅限没有真实沙箱的本地开发），节点打 warning 并把
   `status="off"` 写进状态，报告能看到本次跳过了哪道证明——不是悄悄放行。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import NoReturn

from skill_evaluate.errors import (
    ConfigurationError,
    ExecutorBackendError,
    InfrastructureEnvironmentError,
)
from skill_evaluate.executors.canary import run_canary_probe
from skill_evaluate.logging import get_logger
from skill_evaluate.nodes.preflight.deps import PreflightDeps, assert_backend_routing
from skill_evaluate.nodes.preflight.fingerprint import (
    diff_fingerprint,
    load_golden_fingerprint,
    probe_current_fingerprint,
)
from skill_evaluate.nodes.preflight.state import (
    KEY_CANARY_OUTCOME,
    KEY_FINGERPRINT_OUTCOME,
    NODE_PREFIX,
    CanaryOutcome,
    FingerprintOutcome,
    PreflightState,
)
from skill_evaluate.state.generator_trust import CanaryProbeRecord

logger = get_logger(component="preflight")

GATE_FINGERPRINT = "sandbox_fingerprint"
GATE_CANARY = "canary_probe"

NODE_NAMES: dict[str, str] = {
    "sandbox_fingerprint_gate": f"{NODE_PREFIX}.sandbox_fingerprint_gate",
    "canary_probe_gate": f"{NODE_PREFIX}.canary_probe_gate",
}
ENTRY_NODE = NODE_NAMES["sandbox_fingerprint_gate"]
TERMINAL_NODE = NODE_NAMES["canary_probe_gate"]


class PreflightPipeline:
    """两道前置门禁的实现。依赖全部经 `PreflightDeps` 注入。"""

    def __init__(self, deps: PreflightDeps | None = None) -> None:
        assert_backend_routing()
        self.deps = deps or PreflightDeps()

    # ------------------------------------------------------------------ #
    # Part C：沙箱环境指纹校验
    # ------------------------------------------------------------------ #

    async def sandbox_fingerprint_gate(self, state: PreflightState) -> dict[str, object]:
        settings = self.deps.settings()
        run_id = state.get("run_id")
        if settings.fingerprint_check_mode == "off":
            logger.warning(
                "preflight_fingerprint_check_disabled",
                run_id=run_id,
                hint="本次评测未做沙箱环境一致性证明，仅限无真实沙箱的本地开发使用",
            )
            outcome: FingerprintOutcome = {
                "status": "off",
                "digest": None,
                "golden_path": settings.golden_fingerprint_path,
            }
            return {KEY_FINGERPRINT_OUTCOME: outcome}

        try:
            golden = load_golden_fingerprint(settings.golden_fingerprint_path)
        except ValueError as exc:
            self._fail(GATE_FINGERPRINT, run_id, "黄金指纹文件内容非法", [str(exc)])
        if golden is None:
            self._fail(
                GATE_FINGERPRINT,
                run_id,
                "缺少黄金指纹，无法证明沙箱环境未发生漂移",
                [
                    (
                        f"未找到 {settings.golden_fingerprint_path}。请在目标沙箱环境执行 "
                        "`skill-evaluate preflight-fingerprint --output "
                        f"{settings.golden_fingerprint_path}`，人工审核后提交到仓库。"
                    )
                ],
            )

        try:
            current = await probe_current_fingerprint(
                self.deps.probe_runner(), timeout_s=settings.fingerprint_probe_timeout_s
            )
        except (ExecutorBackendError, ConfigurationError) as exc:
            # 探测通道本身不可用同样是基础设施异常：连指纹都拿不到的沙箱更不可信。
            self._fail(GATE_FINGERPRINT, run_id, "沙箱环境指纹探测失败", [str(exc)])

        mismatches = diff_fingerprint(current, golden)
        if mismatches:
            self._fail(
                GATE_FINGERPRINT, run_id, "基础设施环境异常：沙箱指纹与黄金指纹不一致", mismatches
            )

        digest = current.digest()
        logger.info("preflight_fingerprint_passed", run_id=run_id, digest=digest)
        passed: FingerprintOutcome = {
            "status": "passed",
            "digest": digest,
            "golden_path": settings.golden_fingerprint_path,
        }
        return {KEY_FINGERPRINT_OUTCOME: passed}

    # ------------------------------------------------------------------ #
    # Part D：金丝雀技能探针（含第 6 节差异化调度）
    # ------------------------------------------------------------------ #

    async def canary_probe_gate(self, state: PreflightState) -> dict[str, object]:
        settings = self.deps.settings()
        run_id = state.get("run_id")
        if not run_id:
            raise ConfigurationError(
                "canary_probe_gate 需要 run_id（拼装 Hook 回调与探针 case_id）"
            )

        if settings.canary_check_mode == "off":
            logger.warning("preflight_canary_check_disabled", run_id=run_id)
            off: CanaryOutcome = {
                "status": "off",
                "image_ref": None,
                "reasons": [],
                "trace_id": None,
            }
            return {KEY_CANARY_OUTCOME: off}

        image_ref = self._image_ref(state)
        if settings.canary_check_mode == "nightly_or_image_change" and image_ref is not None:
            latest = await self.deps.canary_history_repository.latest_success(image_ref=image_ref)
            now = self.deps.clock()
            if latest is not None and now - latest.probed_at <= timedelta(
                hours=settings.canary_max_age_hours
            ):
                reason = (
                    f"镜像 {image_ref} 未变更，且 {latest.probed_at.isoformat()} 已成功探测"
                    f"（{settings.canary_max_age_hours}h 内，run_id={latest.run_id}）"
                )
                logger.info("preflight_canary_skipped", run_id=run_id, reason=reason)
                skipped: CanaryOutcome = {
                    "status": "skipped",
                    "image_ref": image_ref,
                    "reasons": [reason],
                    "trace_id": None,
                }
                return {KEY_CANARY_OUTCOME: skipped}

        # GraphBubbleUp（挂起等 Hook）不在这里拦：run_canary_probe 原样上抛。
        result = await run_canary_probe(
            self.deps.backend(), run_id=run_id, timeout_s=settings.canary_timeout_s
        )
        await self.deps.canary_history_repository.record(
            CanaryProbeRecord(
                probe_id=str(uuid.uuid4()),
                run_id=run_id,
                image_ref=image_ref,
                passed=result.passed,
                reasons=result.reasons,
                probed_at=self.deps.clock(),
            )
        )
        if not result.passed:
            self._fail(
                GATE_CANARY,
                run_id,
                "金丝雀探针执行失败，沙箱 I/O / 网络 / 基础引擎可能已损坏，废弃当次评测",
                result.reasons,
            )

        passed: CanaryOutcome = {
            "status": "passed",
            "image_ref": image_ref,
            "reasons": [],
            "trace_id": result.trace_id,
        }
        return {KEY_CANARY_OUTCOME: passed}

    def _image_ref(self, state: PreflightState) -> str | None:
        """镜像标识：显式配置优先，否则回落为本次指纹摘要（指纹变了镜像必然变了）。"""
        configured = self.deps.settings().sandbox_image_ref
        if configured:
            return configured
        outcome = state.get(KEY_FINGERPRINT_OUTCOME) or {}
        digest = outcome.get("digest") if isinstance(outcome, dict) else None
        return f"fingerprint:{digest}" if digest else None

    @staticmethod
    def _fail(gate: str, run_id: str | None, message: str, details: list[str]) -> NoReturn:
        logger.error(f"preflight_{gate}_failed", run_id=run_id, message=message, details=details)
        summary = "；".join(details[:10])
        more = f"（另有 {len(details) - 10} 项）" if len(details) > 10 else ""
        raise InfrastructureEnvironmentError(
            f"{message}：{summary}{more}", gate=gate, details=details
        )


__all__ = [
    "ENTRY_NODE",
    "GATE_CANARY",
    "GATE_FINGERPRINT",
    "NODE_NAMES",
    "TERMINAL_NODE",
    "PreflightPipeline",
]
