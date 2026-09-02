"""Optimizer 补丁契约（docs/dev/09 第 2 节，对文档 02 的一次追加）。

`PatchType` 的实体定义在 `state/enums.py`（项目约定：全局枚举只有那一个落点），
这里原样再导出，docs/dev/09 里写的 `from skill_evaluate.state.patch import PatchType`
照常可用。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from skill_evaluate.state.enums import PatchType


class Patch(BaseModel):
    """一份候选补丁。

    **只存在于评测流水线的临时工作副本中**：`Patch` 不是 git commit，也不是 PR。
    把通过回归的补丁转成真实提交是 docs/dev/24 的职责，本层不碰代码仓库。
    """

    patch_id: str
    skill_id: str
    base_skill_version_ref: str  # 补丁基于哪个版本生成，防止对已过期版本打补丁
    patch_type: PatchType
    target_path: str  # "SKILL.md" 或具体脚本相对路径
    diff: str  # 统一 diff（unified diff）格式，供人工审查与自动应用
    rationale: str  # Optimizer 生成该补丁的理由说明
    triggered_by_finding_id: str | None = None  # 关联 SecurityFinding.finding_id（模块五场景）
    created_at: datetime


class PatchApplicationResult(BaseModel):
    patch_id: str
    applied: bool
    regression_passed: bool | None = None  # None = 尚未跑回归
    working_skill_version_ref: str | None = None  # 应用补丁后的临时工作版本引用
    detail: str = ""  # 回归失败原因/应用失败原因，供人工审批卡片直接展示


__all__ = ["Patch", "PatchApplicationResult", "PatchType"]
