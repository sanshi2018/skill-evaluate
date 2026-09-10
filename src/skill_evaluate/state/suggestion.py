"""用例处置建议（docs/dev/17 第 6.1 节，对应架构文档模块七"平滑淘汰"）。

## 这张表存在的理由：区分两种人机协作模式

本项目已经有一套人工介入机制——`suspend_and_wait()` + `human_approvals`
（docs/dev/04/09/16）。它是**阻塞式**的：流水线停在原地，不确认就算不下去。
能力树规模超阈值走的是它，因为一棵粒度不对的树会让后面每一个覆盖率数字都失真。

孤儿用例不属于这一类。一条绑定了已消失能力的用例**不会主动报错**：模块一/三/五
等消费方是按 `split` 取用例后各自独立判定的，没有谁会去检查"这条题绑的能力还在不
在"。也就是说，它的存在不影响本次运行任何结论的正确性，只是一条注定失去意义、
需要人找时间清理的题。为这种事把整条流水线挂起，代价与收益完全不成比例。

所以模块七把它设计成**非阻塞的建议队列**：评测照跑照出报告，建议落表等人批量处理。
两种模式的分界线是——**不确认就无法继续算下去的，用阻塞式挂起；可以先继续跑、
但需要人类找时间清理的，用本表。**

## 为什么不允许流水线自己把状态推进到 confirmed

架构文档模块七的"应对方案"原文：*瘦身节点默认只做"降级运行"或"建议剔除"，硬性的
删除操作必须在内部审核工作台上，保留人类开发者的最终 Review 确认权限。* 落到代码
上就是：`nodes/pruning/` 只写 `status=PENDING`，全项目没有任何一条把它改成
`CONFIRMED` 的自动路径，那一步属于 docs/dev/22。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from skill_evaluate.state.enums import SuggestionStatus, SuggestionType


class TestCaseSuggestion(BaseModel):
    """一条"建议人工处置某条用例"的记录。

    与 `HumanApproval`（`human_approvals` 表）的关键区别：那张表以 `wait_key` 为
    锚点、绑定一次**运行**里的一个挂起点；本表以 `case_id` 为锚点、跨运行长期存活
    ——同一条孤儿用例在连续三次评测里被检出，应该只产生一条待办，而不是三条。
    这一点由 `(case_id, suggestion_type)` 的唯一约束在库层面保证，见
    `TestCaseSuggestionRepository.save_if_absent()`。
    """

    suggestion_id: str
    case_id: str
    suggestion_type: SuggestionType
    # 给人看的判断依据。必须写清楚"哪几项能力消失了"，因为审查工作台上的人要据此
    # 决定这条题是该淘汰还是该重新绑定到改名后的能力上——只写"该用例已过期"等于
    # 把判断重新推回给人自己去查。
    reason: str
    status: SuggestionStatus = SuggestionStatus.PENDING
    created_at: datetime


__all__ = ["TestCaseSuggestion"]
