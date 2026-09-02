"""`PipelineState` 本体（docs/dev/02 第 11 节）。

设计取舍说明：
- 用 `TypedDict` 而非 Pydantic `BaseModel`，是 LangGraph 官方推荐的做法（原生支持
  reducer 语义如 `Annotated[list, add]`），重量级校验交给写入前的 Pydantic 模型
  （如 `TestCase`、`ExecutionTrace`）各自负责。
- `PipelineState` 里一律存 ID/引用，真实内容通过 `persistence/`（docs/dev/04）按 ID
  读写 Postgres——这直接决定了 `PostgresSaver` 的 Checkpoint 体积可控，也让"从
  断点恢复"（docs/dev/04、22）不需要反序列化整棵 Trace Tree。
"""

from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict


class PipelineState(TypedDict, total=False):
    # ---- 运行身份 ----
    run_id: str
    skill_id: str
    skill_version_ref: str

    # ---- 引用型字段：只存主键/版本号，不存整块大对象 ----
    active_suite_version_id: str | None
    capability_tree_id: str | None

    # ---- 累加型字段：LangGraph reducer 用 operator.add 做追加合并 ----
    executed_trace_ids: Annotated[list[str], add]
    judge_verdict_ids: Annotated[list[str], add]
    security_finding_ids: Annotated[list[str], add]

    # ---- 控制流字段 ----
    generation_mode: str  # GenerationMode 枚举值，运行入参
    node_status: dict[str, str]  # {node_name: NodeExecutionStatus}
    suspended_reason: str | None
    retry_counts: dict[str, int]  # {node_name: count}，Optimizer 闭环重试计数

    # ---- 报告聚合（05 文档消费） ----
    final_report_ref: str | None
