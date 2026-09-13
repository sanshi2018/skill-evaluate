"""审批卡片的证据展开（`GET /api/approvals/{approval_id}/context`，docs/dev/22 第 5 节）。

卡片本身只存"引用"（`PendingApproval.context_ref`：patch_id / model / skill_id / case_ids ...），
这里按 `decision_type` 分派，把引用展开成工作台要展示的具体证据。docs/dev/22 第 9 节
遗留的"`context_ref` 展开逻辑的完整分派表"在本文件落地：

| decision_type | context_ref 关键字段 | 展开内容 |
|---|---|---|
| ACCEPT_PATCH | patch_id | `patch`（diff + rationale）、`patch_application_result`、`retry_counts` |
| UNFREEZE_JUDGE | model, temperature | `judge_health`、`recent_golden_misses`（新→旧的 is_miss 序列） |
| CONFIRM_TREE_REVIEW | skill_id, skill_version_ref | `capability_tree`（能力条目；evidence_quote 在日志事件里） |
| RESOLVE_DEEP_CONFLICT | case_ids, hard/soft_findings | `dimension_results`、`trace_comparisons`（单跑 vs 并发双路 Trace） |
| INJECT_NEW_SEED | skill_id | `collapse_events`（最近坍塌事件） |
| ABANDON_RUN | error / gate, details | `dimension_results` |

## 两条约定

1. **单项展开失败不拖垮整个视图**：某个仓储查询失败时记进 `expansion_errors`，其余证据照常
   返回——人在凌晨处理审批时，"补丁 diff 看得到、重试计数没加载出来"远好过一个 500。
2. **只读**：本文件不做任何状态迁移，决策在 `hooks_approval.py`。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from skill_evaluate.agents.judge.health import temperature_bucket
from skill_evaluate.logging import get_logger
from skill_evaluate.persistence.repository import (
    ApprovalDecisionRepository,
    CapabilityRepository,
    DimensionResultRepository,
    GenerationCollapseEventRepository,
    JudgeHealthRepository,
    JudgeMissRepository,
    PatchRepository,
    PipelineStateRepository,
    RunRepository,
    TraceRepository,
)
from skill_evaluate.state.approval import ApprovalDecisionType, PendingApproval, allowed_outcomes
from skill_evaluate.state.trace import (
    RUN_INDEX_MULTI_SKILL_ANTAGONISM,
    RUN_INDEX_MULTI_SKILL_ATTENTION_CROWDED,
    RUN_INDEX_MULTI_SKILL_ATTENTION_SOLO,
    RUN_INDEX_MULTI_SKILL_CORE_BASELINE,
    RUN_INDEX_MULTI_SKILL_CORE_CROWDED,
    RUN_INDEX_MULTI_SKILL_HIJACK_CROWDED,
    RUN_INDEX_MULTI_SKILL_HIJACK_SOLO,
    RUN_INDEX_MULTI_SKILL_TEMPORAL,
)

logger = get_logger(component="approval_context")

# docs/dev/interfaces/20 第 4.4 节的双路比对号段表：(场景, 单跑号段, 并发号段)。
# 单跑为 None 的场景只有并发一臂；时序扰动以原顺序并发（232）为参照。
MULTI_SKILL_TRACE_PAIRS: tuple[tuple[str, int | None, int], ...] = (
    ("hijack", RUN_INDEX_MULTI_SKILL_HIJACK_SOLO, RUN_INDEX_MULTI_SKILL_HIJACK_CROWDED),
    (
        "attention_decay",
        RUN_INDEX_MULTI_SKILL_ATTENTION_SOLO,
        RUN_INDEX_MULTI_SKILL_ATTENTION_CROWDED,
    ),
    ("core_regression", RUN_INDEX_MULTI_SKILL_CORE_BASELINE, RUN_INDEX_MULTI_SKILL_CORE_CROWDED),
    ("antagonism", None, RUN_INDEX_MULTI_SKILL_ANTAGONISM),
    ("temporal", RUN_INDEX_MULTI_SKILL_ANTAGONISM, RUN_INDEX_MULTI_SKILL_TEMPORAL),
)

# 双路比对最多展开的用例数：每条用例最多 8 条完整 Trace（含 actions），不设上限时一次
# 深度冲突卡片的响应体可以到几十 MB。
MAX_COMPARED_CASES = 20
JUDGE_MISS_WINDOW = 50


class ApprovalContextBuilder:
    """按 decision_type 展开证据。全部仓储可注入（测试不碰库）。"""

    def __init__(
        self,
        *,
        run_repository: Any = None,
        decision_repository: Any = None,
        dimension_repository: Any = None,
        patch_repository: Any = None,
        pipeline_state_repository: Any = None,
        judge_health_repository: Any = None,
        judge_miss_repository: Any = None,
        capability_repository: Any = None,
        trace_repository: Any = None,
        collapse_event_repository: Any = None,
    ) -> None:
        self._runs = run_repository or RunRepository()
        self._decisions = decision_repository or ApprovalDecisionRepository()
        self._dimensions = dimension_repository or DimensionResultRepository()
        self._patches = patch_repository or PatchRepository()
        self._pipeline_state = pipeline_state_repository or PipelineStateRepository()
        self._judge_health = judge_health_repository or JudgeHealthRepository()
        self._judge_miss = judge_miss_repository or JudgeMissRepository()
        self._capabilities = capability_repository or CapabilityRepository()
        self._traces = trace_repository or TraceRepository()
        self._collapse_events = collapse_event_repository or GenerationCollapseEventRepository()

    async def build(self, approval: PendingApproval) -> dict[str, Any]:
        context: dict[str, Any] = {
            "approval": approval.model_dump(mode="json"),
            # 工作台据此渲染操作按钮，不必自己维护一份 outcome 表（会漂移）。
            "allowed_outcomes": sorted(
                allowed_outcomes(approval.decision_type, blocking=approval.blocking)
            ),
        }
        errors: list[str] = []

        async def expand(key: str, loader: Callable[[], Awaitable[Any]]) -> None:
            try:
                context[key] = await loader()
            except Exception as exc:  # noqa: BLE001 - 单项展开失败不拖垮整个视图（模块头约定 1）
                errors.append(f"{key}: {type(exc).__name__}: {str(exc)[:200]}")
                logger.warning(
                    "approval_context_expansion_failed",
                    approval_id=approval.approval_id,
                    key=key,
                    error=str(exc)[:200],
                )

        await expand("run", lambda: self._runs.get(approval.run_id))
        await expand("decision", lambda: self._decision_of(approval.approval_id))

        ref = approval.context_ref
        match approval.decision_type:
            case ApprovalDecisionType.ACCEPT_PATCH:
                patch_id = ref.get("patch_id")
                if patch_id:
                    await expand("patch", lambda: self._dump(self._patches.get(patch_id)))
                    await expand(
                        "patch_application_result",
                        lambda: self._dump(self._patches.get_application_result(patch_id)),
                    )
                await expand(
                    "retry_counts", lambda: self._pipeline_state.get_retry_counts(approval.run_id)
                )
            case ApprovalDecisionType.UNFREEZE_JUDGE:
                model = ref.get("model")
                if model:
                    bucket = temperature_bucket(str(model), float(ref.get("temperature") or 0.0))
                    await expand(
                        "judge_health",
                        lambda: self._judge_health.get(model=model, temperature_bucket=bucket),
                    )
                    await expand(
                        "recent_golden_misses",
                        lambda: self._judge_miss.recent_window(
                            model=model, temperature_bucket=bucket, window_size=JUDGE_MISS_WINDOW
                        ),
                    )
            case ApprovalDecisionType.CONFIRM_TREE_REVIEW:
                skill_id, version_ref = ref.get("skill_id"), ref.get("skill_version_ref")
                if skill_id and version_ref:
                    await expand(
                        "capability_tree",
                        lambda: self._dump(self._capabilities.get(str(skill_id), str(version_ref))),
                    )
            case ApprovalDecisionType.RESOLVE_DEEP_CONFLICT:
                await expand(
                    "dimension_results", lambda: self._dimensions.list_by_run(approval.run_id)
                )
                case_ids = [str(c) for c in (ref.get("case_ids") or [])][:MAX_COMPARED_CASES]
                await expand("trace_comparisons", lambda: self._trace_comparisons(case_ids))
            case ApprovalDecisionType.INJECT_NEW_SEED:
                skill_id = ref.get("skill_id")
                if skill_id:
                    await expand(
                        "collapse_events",
                        lambda: self._collapse_events.list_recent(skill_id=skill_id, limit=10),
                    )
            case ApprovalDecisionType.ABANDON_RUN:
                await expand(
                    "dimension_results", lambda: self._dimensions.list_by_run(approval.run_id)
                )
            case ApprovalDecisionType.CONFIRM_ORPHAN_RETIREMENT:
                # 孤儿用例走 /api/suggestions，不应出现在 pending_approvals 里；出现了就如实标注。
                errors.append("CONFIRM_ORPHAN_RETIREMENT 应通过 /api/suggestions 处理")

        context["expansion_errors"] = errors
        return context

    async def _decision_of(self, approval_id: str) -> dict[str, Any] | None:
        decision = await self._decisions.get_by_approval(approval_id)
        return decision.model_dump(mode="json") if decision else None

    @staticmethod
    async def _dump(awaitable: Awaitable[Any]) -> Any:
        value = await awaitable
        return value.model_dump(mode="json") if value is not None else None

    async def _trace_comparisons(self, case_ids: list[str]) -> list[dict[str, Any]]:
        """按 `(case_id, run_index)` 取双路 Trace，组织成"场景 → 单跑 / 并发"的并排结构。

        `execution_traces` 以 `(case_id, run_index)` 唯一，不带 run_id：取到的是该用例在该号段上
        **最近一次**的 Trace。工作台展示时应以 Trace 的 `started_at` 提示时间，避免把上一次
        运行的结果当成本次证据。
        """
        comparisons: list[dict[str, Any]] = []
        for case_id in case_ids:
            by_index = {t.run_index: t for t in await self._traces.list_by_case(case_id)}
            for scenario, solo_index, crowded_index in MULTI_SKILL_TRACE_PAIRS:
                crowded = by_index.get(crowded_index)
                if crowded is None:
                    continue
                solo = by_index.get(solo_index) if solo_index is not None else None
                comparisons.append(
                    {
                        "case_id": case_id,
                        "scenario": scenario,
                        "solo": solo.model_dump(mode="json") if solo else None,
                        "crowded": crowded.model_dump(mode="json"),
                    }
                )
        return comparisons


__all__ = ["MULTI_SKILL_TRACE_PAIRS", "ApprovalContextBuilder"]
