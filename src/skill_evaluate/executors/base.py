"""`ExecutorBackend` 抽象接口（docs/dev/03 第 2 节）。

所有 `nodes/` 下的评测节点，只依赖本模块的 `ExecutorBackend` 接口和
`ExecutionRequest`/`ExecutionTrace`，通过依赖注入拿到具体实例，不直接 import
`MiniAgentBackend` 或 `PluggableAgentBackend`。后端选择由 `config.py` 中
`ExecutorSettings.backend` 驱动，并支持按节点覆盖（见 `routing.py`）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from skill_evaluate.state.enums import ExecutorBackendType
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase
from skill_evaluate.state.trace import ExecutionTrace


class ExecutionRequest(BaseModel):
    skill: SkillDefinition
    case: TestCase
    run_index: int  # 第几次冗余执行（0-based）
    load_skill: bool = True  # False 时用于模块三"不加载 Skill 的基线对照"
    background_skills: list[SkillDefinition] = Field(default_factory=list)  # 模块十并发干扰包
    sampling_overrides: dict[str, float] | None = None  # 模块九：temperature/top_p 扰动
    wall_clock_timeout_s: int = 60  # 模块五：单次执行硬超时
    # 补充字段（相对 docs/dev/03 原文的向后兼容追加，非破坏性修改）：
    # PluggableAgentBackend（HermesBackend）需要 run_id 拼装 Hook 回调 URL
    # （docs/dev/03 第 4.4 节）与 pending_hooks 唯一键（docs/dev/04 第 5 节），
    # 原文档遗漏了该字段，这里以可选字段形式补齐，默认 None 不影响 MiniAgentBackend。
    run_id: str | None = None


class ExecutorBackend(ABC):
    """执行后端抽象基类。子类须固定声明 `backend_type`。"""

    backend_type: ExecutorBackendType

    @abstractmethod
    async def execute(self, request: ExecutionRequest) -> ExecutionTrace:
        """同步/异步均可，但对外统一 async 接口；实现内部自行处理沙箱生命周期。

        必须保证：无论成功/超时/异常，都返回一个合法的 `ExecutionTrace`（失败态也要
        建模，不允许抛出未捕获异常让调用方处理裸 Exception——见 docs/dev/03 第 6 节
        容错约定）。沙箱创建失败等"评测系统自身故障"应抛出
        `skill_evaluate.errors.ExecutorBackendError`，由节点层决定重试或挂起。
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """供 docs/dev/21 金丝雀探针复用：验证后端当前是否可用。"""
