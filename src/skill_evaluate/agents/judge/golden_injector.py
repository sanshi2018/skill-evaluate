"""黄金基准盲测的注入点（docs/dev/08 第 3.2 节）。

盲测的关键是 **Judge Agent 自己不知道正在被考核**：注入发生在
`JudgeAgent.judgmental_verdict()` 内部，被替换后的请求与真实请求同构、走完全相同
的模板与模型通道，Prompt 里没有任何"这是一道考题"的痕迹。

对**外**（各评测维度节点）也保持透明：节点拿回的仍是一个合法 `JudgeVerdict` /
`ConsensusResult`，只是 `subject_id` 带上了 `__golden__:` 前缀。节点按约定跳过带
前缀的结果，不计入 `BenchmarkReport`——黄金用例的判决属于"考核裁判"，不属于
"评测这个 Skill"，混进报告会污染真实分数。

一处刻意的实现取舍：本函数返回的是 `GoldenInjection`（请求 + 命中的黄金用例）而
不是裸 `ReviewRequest`。开发文档写的是"返回值与真实请求同构，调用方无法区分"，
指的是**外部调用方**；`judgmental_verdict()` 内部必须知道这次是不是黄金注入，
否则无从比对人类标定、也无从记账。把这个事实塞进返回值而不是靠 `subject_id` 前缀
反解，是为了让"谁能看见这个事实"在类型上一目了然。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from skill_evaluate.agents.mini.service import ReviewRequest
from skill_evaluate.config import get_settings
from skill_evaluate.logging import get_logger
from skill_evaluate.persistence.repository import GoldenCaseRepository
from skill_evaluate.state.golden import GoldenCase

logger = get_logger(component="judge_golden")

GOLDEN_SUBJECT_PREFIX = "__golden__:"


def golden_subject_id(golden_id: str) -> str:
    return f"{GOLDEN_SUBJECT_PREFIX}{golden_id}"


def is_golden_subject(subject_id: str) -> bool:
    """各评测维度节点用它判断"这条结果要不要计入报告"。

    约定见 docs/dev/08 第 3.2 节：返回 True 时**跳过**，既不计 PASS 也不计 FAIL，
    更不要把它当成一次真实用例的判定写进 `dimension_results`。
    """
    return subject_id.startswith(GOLDEN_SUBJECT_PREFIX)


@dataclass(slots=True)
class GoldenInjection:
    """一次注入判定的结果。`golden is None` 表示本次是真实请求，原样放行。"""

    request: ReviewRequest
    golden: GoldenCase | None = None

    @property
    def injected(self) -> bool:
        return self.golden is not None


async def maybe_inject_golden_case(
    real_request: ReviewRequest,
    *,
    rate: float | None = None,
    repository: GoldenCaseRepository | None = None,
    rng: random.Random | None = None,
) -> GoldenInjection:
    """以 `SKILLEVAL_JUDGE_GOLDEN_INJECT_RATE`（默认 0.02）的概率把本次真实请求替换成黄金用例。

    只会挑选**同一 `template_key`** 的黄金用例：换了模板就换了 `content` 的字段
    形状，渲染会因 `StrictUndefined` 直接报错，伪装也就无从谈起。库里没有该模板
    的黄金用例时打一条日志后原样放行——没有考题不该阻断真实评测。
    """
    inject_rate = get_settings().judge.golden_inject_rate if rate is None else rate
    if inject_rate <= 0:
        return GoldenInjection(request=real_request)

    roll = (rng or random).random()
    if roll >= inject_rate:
        return GoldenInjection(request=real_request)

    candidates = await (repository or GoldenCaseRepository()).list_active(
        template_key=real_request.template_key
    )
    if not candidates:
        logger.info(
            "judge_golden_injection_skipped",
            template_key=real_request.template_key,
            reason="no_active_golden_case",
        )
        return GoldenInjection(request=real_request)

    golden = (rng or random).choice(candidates)
    logger.info(
        "judge_golden_injected",
        template_key=real_request.template_key,
        golden_id=golden.golden_id,
        # 故意不打印被顶替掉的真实 subject_id 之外的任何内容：日志本身也不该
        # 成为"这次是不是考题"的旁路泄漏渠道（对人有用，对 Agent 不可见）。
        replaced_subject_id=real_request.subject_id,
    )
    return GoldenInjection(
        request=ReviewRequest(
            subject_id=golden_subject_id(golden.golden_id),
            template_key=golden.template_key,
            content=dict(golden.content),
        ),
        golden=golden,
    )


__all__ = [
    "GOLDEN_SUBJECT_PREFIX",
    "GoldenInjection",
    "golden_subject_id",
    "is_golden_subject",
    "maybe_inject_golden_case",
]
