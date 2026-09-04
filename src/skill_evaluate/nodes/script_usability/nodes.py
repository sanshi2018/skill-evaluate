"""模块四：脚本接口的智能体易用性黑盒探测的节点实现（docs/dev/14）。

```
prepare_scripts                        推断运行时 / 核对文件 / 探沙箱可用性
      ↓
   ┌──────────────────────┬──────────────────────┬────────────────────────────┐
   ↓                      ↓                      ↓
hard_failure_probing   self_learning_doc_test   constructive_error_and_io_separation_test
（非交互挂起，致命）      （--help 文档质量）        （脏数据报错 + 流隔离）
   └──────────────────────┴──────────────────────┴────────────────────────────┘
                                   ↓
                     idempotency_and_safety_guards（连续执行 + 输出体量）
                                   ↓
                        finalize_dimension_report
```

## 三条贯穿本文件的关键决策

1. **确定性事实与主观审查分开处理，阻断口径也不同**（docs/dev/14 第 8 节）。
   挂起、连续执行崩溃是二元事实，判失败即阻断合并；`--help` 写得好不好、报错够
   不够建设性是 LLM 的主观审查，只告警。这延续模块二/三的一贯原则：避免主观审查
   的误报直接拖垮流水线的可信度。
2. **所有 pass/fail 结论都经 `JudgeAgent`**，包括确定性的那些——走
   `quantitative_verdict()` + `rules.py` 里注册的规则（理由见 `rules.py` 的模块头）。
   本文件没有一处自己写的 `if ...: findings.append("[致命] ...")` 式判定。
3. **每次探测一个独立工作区**（docs/dev/14 第 3 节的硬性要求）。脚本 A 的副作用
   不得污染脚本 B 的幂等性判定；唯一刻意共享工作区的是幂等性探测自己的两次连续
   执行——那正是它要观测的东西。

## 节点签名与返回值

与模块一/二/三同样的两条坑：签名必须写 `ScriptUsabilityState`（否则私有键被
LangGraph 静默裁掉，表现为"探测跑了、收尾节点什么也看不到"），返回值只带增量
（`judge_verdict_ids` 的 reducer 是 `operator.add`，回抛整个旧状态会让 id 翻倍）。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from pydantic import BaseModel

from skill_evaluate.agents.judge.golden_injector import is_golden_subject
from skill_evaluate.errors import ExecutorBackendError, PersistenceError, PipelineSuspended
from skill_evaluate.executors.script_sandbox import ProcessResult, prepare_workspace
from skill_evaluate.logging import get_logger
from skill_evaluate.nodes.script_usability import rules
from skill_evaluate.nodes.script_usability.deps import (
    ERROR_REVIEW_CRITICALITY,
    HELP_REVIEW_CRITICALITY,
    TEMPLATE_CONSTRUCTIVE_ERROR,
    TEMPLATE_HELP_DOC_QUALITY,
    ScriptUsabilityDeps,
)
from skill_evaluate.nodes.script_usability.probes import (
    DirtyPayload,
    ScriptProbeTarget,
    build_probe_targets,
    check_io_separation,
    describe_invocation,
    format_error_output,
    generate_dirty_payloads,
    is_mutating_script,
    looks_like_unhandled_crash,
)
from skill_evaluate.nodes.script_usability.state import (
    DIMENSION,
    KEY_ERROR_OUTCOMES,
    KEY_HARD_FAILURE_FINDINGS,
    KEY_HELP_OUTCOMES,
    KEY_IDEMPOTENCY_FINDINGS,
    KEY_PREPARE_FINDINGS,
    KEY_SANDBOX_UNAVAILABLE,
    KEY_TARGETS,
    ScriptUsabilityState,
)
from skill_evaluate.state.enums import JudgeVerdictStatus
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.skill import SkillDefinition

logger = get_logger(component=DIMENSION)

# 节点名。docs/dev/03 的 `NODE_BACKEND_ROUTING`（键 `script_usability`）、
# docs/dev/24 的主图装配都引用这一份，避免各处各写各的字符串。
NODE_NAMES = {
    "prepare_scripts": f"{DIMENSION}.prepare_scripts",
    "hard_failure_probing": f"{DIMENSION}.hard_failure_probing",
    "self_learning_doc_test": f"{DIMENSION}.self_learning_doc_test",
    "constructive_error_and_io_separation_test": (
        f"{DIMENSION}.constructive_error_and_io_separation_test"
    ),
    "idempotency_and_safety_guards": f"{DIMENSION}.idempotency_and_safety_guards",
    "finalize_dimension_report": f"{DIMENSION}.finalize_dimension_report",
}

ENTRY_NODE = NODE_NAMES["prepare_scripts"]
TERMINAL_NODE = NODE_NAMES["finalize_dimension_report"]

# 判定 `subject_id` 的前缀（约定见 docs/dev/interfaces/13 第 6.1 节）。
# 同一个脚本在本维度会产生五种判定，不加前缀会在
# `JudgeRepository.list_verdicts(subject_id)` 里混成一堆分不出彼此。
SUBJECT_PREFIX_HANG = "script_hang:"
SUBJECT_PREFIX_HELP = "script_help:"
SUBJECT_PREFIX_DIRTY = "script_dirty:"
SUBJECT_PREFIX_IDEMPOTENCY = "script_idempotency:"
SUBJECT_PREFIX_OUTPUT = "script_output:"

# 报告 findings 里每条判定摘录的长度（与模块二/三同一口径）：`JudgeVerdict` 已经
# 带着完整 reasoning 落了库，findings 是给人扫一眼用的摘要列表。
REASONING_EXCERPT_CHARS = 200

# findings 的严重级前缀。收敛成常量是因为 `finalize_dimension_report()` 要按它们
# 分类计算 `blocking`——字面量散在各节点里，改一个措辞就会让阻断逻辑悄悄失效。
FATAL_PREFIX = "[致命]"
BLOCKING_PREFIX = "[阻断]"
WARNING_PREFIX = "[警告]"
INFO_PREFIX = "[提示]"

#: `_run_per_script()` 里"这次探测该怎么把脚本调起来"的小工厂签名：
#: 拿到目标与工作区路径，返回 (容器内命令, 要喂给 stdin 的字节；None = 不喂)。
type CommandBuilder = Callable[[ScriptProbeTarget, Path], tuple[list[str], bytes | None]]

# 幂等性/输出体量探测用的良性样本文件：一份格式完全合法的最小 CSV。
# 它的用途不是"测业务逻辑对不对"，而是给脚本一个**能走进主流程**的输入，好观测
# 它连续执行两次的行为与输出体量——用脏数据跑幂等性会在第一步就被拒，什么也测不到。
BENIGN_SAMPLE_NAME = "skilleval_benign_sample.csv"
BENIGN_SAMPLE_BYTES = b"id,name,amount\n1,alpha,10\n2,beta,20\n"


class ScriptCheckOutcome(BaseModel):
    """一个脚本在某一项检查上的结论摘要（进图状态用）。

    存摘要而不是整个 `JudgeVerdict`：verdict 本体已由 Judge 侧落库，状态里再放一份
    只会让每个 Checkpoint 白背几十 KB（`state.py` 的说明）。

    `skipped_reason` 不为空表示这一项**没有产生结论**——两种成因：脚本本身跑不起来
    （`ScriptProbeTarget.skip_reason`），或本次请求被黄金基准盲测占用。两者都不计入
    通过/失败，但都必须出现在报告里：少做了一项检查，读报告的人有权知道。

    `detail` 放的是不参与判定的补充信号（当前只有流隔离检查的结论）。它与
    `status` 分开是有意的：docs/dev/14 第 6.2 节明确要求流隔离只作**补充信号**、
    不作独立判定依据，把它并进 status 就等于让一条启发式规则决定了成败。
    """

    script_path: str
    check: str  # 模板 key 或规则名，报告里据此分组
    verdict_id: str | None = None
    status: JudgeVerdictStatus | None = None
    reasoning_excerpt: str = ""
    skipped_reason: str | None = None
    detail: str = ""


class ScriptUsabilityPipeline:
    """模块四的六个节点。做成类是为了让依赖注入只发生一次（构造时），
    而不是每个节点函数各自去拿一遍单例。

    用法（docs/dev/24 装配主图时）见 `graph.py::add_script_usability_nodes()`。
    """

    def __init__(self, deps: ScriptUsabilityDeps | None = None) -> None:
        self.deps = deps or ScriptUsabilityDeps()
        # 装配期就核对路由表：本维度无法在 MINI 后端下兑现任何一项检查，与其运行
        # 到一半才发现不一致，不如在建图时报错（`deps.py::assert_backend_routing`）。
        ScriptUsabilityDeps.assert_backend_routing()

    # ------------------------------------------------------------------ #
    # 1. prepare_scripts
    # ------------------------------------------------------------------ #

    async def prepare_scripts(self, state: ScriptUsabilityState) -> dict[str, object]:
        """列出待探测脚本、推断运行时、确认沙箱可用（docs/dev/14 第 3 节）。

        三种"还没开始就已经有结论"的情况在这里一次性收口，后面三条支路只需要处理
        `runnable` 的目标：

        - 沙箱运行时不可用 → 整个维度降级（写 `_su_sandbox_unavailable`），
          **不**判 PASS：我们什么都没测到，说"通过"就是撒谎；
        - 脚本文件不在盘上 / 推断不出运行时 → 该脚本单独跳过并记 finding；
        - Skill 根本没有 `scripts/` 目录 → 一条提示，维度判 PASS（见收尾节点）。
        """
        skill = await self._load_skill(state)
        settings = self.deps.settings()
        targets = build_probe_targets(
            skill, image_overrides=settings.runtime_image_overrides or None
        )
        findings = [f"{WARNING_PREFIX} {t.path}：{t.skip_reason}" for t in targets if t.skip_reason]

        unavailable: str | None = None
        if targets and not await self._sandbox_available():
            unavailable = (
                "脚本沙箱运行时不可用（容器引擎未就绪）：本维度的全部结论都来自真实"
                "子进程的执行结果，没有沙箱就一项也测不了。已跳过全部探测——注意这"
                "不等于通过。"
            )

        logger.info(
            "script_usability_prepared",
            run_id=str(state["run_id"]),
            node_name=ENTRY_NODE,
            skill_id=skill.skill_id,
            scripts=len(targets),
            runnable=sum(1 for t in targets if t.runnable),
            sandbox_unavailable=bool(unavailable),
        )
        return {
            KEY_TARGETS: [t.model_dump() for t in targets],
            KEY_PREPARE_FINDINGS: findings,
            KEY_SANDBOX_UNAVAILABLE: unavailable,
        }

    async def _sandbox_available(self) -> bool:
        """探一次沙箱可用性；探测本身出错也算不可用（而不是让整条流水线崩掉）。"""
        try:
            return await self.deps.sandbox().is_available()
        except ExecutorBackendError:
            return False

    # ------------------------------------------------------------------ #
    # 2. hard_failure_probing
    # ------------------------------------------------------------------ #

    async def hard_failure_probing(self, state: ScriptUsabilityState) -> dict[str, object]:
        """非交互性挂起测试（docs/dev/14 第 4 节）。

        **故意不传任何参数**、空 stdin、无 TTY 地把脚本调起来，看它会不会等下去。
        合格的脚本应当立刻以一条用法说明退出；挂起意味着它在等一个永远不会来的
        输入（密码提示、Y/N 确认），而 Agent 调用它时同样没有终端可以回答。

        `timeout_s` 用本维度专用的短超时（默认 10 秒，见 `ScriptUsabilitySettings`）：
        挂起测试的本意就是"脚本应该立刻拒绝"，用常规的 60 秒只会让每个不合格脚本
        白等 50 秒。

        命中挂起 = **致命、阻断合并**，且不经 Mini Agent 复核——"是否挂起"是二元
        确定性事实，不需要语义裁决（走的是 `quantitative_verdict()`，见 `rules.py`）。
        """
        targets = self._runnable_targets(state)
        if not targets:
            return {KEY_HARD_FAILURE_FINDINGS: []}

        settings = self.deps.settings()
        judge = self.deps.judge()
        results = await self._run_per_script(
            await self._load_skill(state),
            targets,
            # 空参数 + 空 stdin：模拟"没有交互输入可用"的真实 Agent 调用环境。
            lambda target, _workspace: (target.command(), b""),
            timeout_s=settings.hang_probe_timeout_s,
        )

        findings: list[str] = []
        verdict_ids: list[str] = []
        for target, result in results:
            verdict = judge.quantitative_verdict(
                subject_id=f"{SUBJECT_PREFIX_HANG}{target.path}",
                rule_name=rules.RULE_NON_INTERACTIVE,
                inputs={
                    rules.KEY_TIMED_OUT: result.timed_out,
                    rules.KEY_TIMEOUT_S: settings.hang_probe_timeout_s,
                    rules.KEY_EXIT_CODE: result.exit_code,
                },
            )
            verdict_ids.append(verdict.verdict_id)
            if verdict.status is JudgeVerdictStatus.FAIL:
                await self.deps.judge_repository.save_verdict(verdict)
                findings.append(
                    f"{FATAL_PREFIX} {target.path} 在缺失参数时挂起超过 "
                    f"{settings.hang_probe_timeout_s} 秒未退出，判定为非交互性失败："
                    "脚本疑似在等待交互输入（密码/确认），而调用它的智能体没有终端可以回答。"
                )

        logger.info(
            "script_usability_hard_failure_probed",
            run_id=str(state["run_id"]),
            node_name=NODE_NAMES["hard_failure_probing"],
            probed=len(results),
            hung=len(findings),
        )
        return {"judge_verdict_ids": verdict_ids, KEY_HARD_FAILURE_FINDINGS: findings}

    # ------------------------------------------------------------------ #
    # 3. self_learning_doc_test
    # ------------------------------------------------------------------ #

    async def self_learning_doc_test(self, state: ScriptUsabilityState) -> dict[str, object]:
        """`--help` 文档质量审查（docs/dev/14 第 5 节，模板 5.4）。

        两级判定：

        1. **确定性闸门**（`script_help_responsive`）：`--help` 有没有拿到任何可读
           输出。stdout 与 stderr 都接受——不少手写脚本把用法打到 stderr 再以 2
           退出，那仍然是一份能读的文档。两者皆空则直接判 FAIL，**不调 LLM**：把
           空字符串交给裁判没有任何审查价值，只是白烧一次 Token。
        2. **语义审查**（模板 `help_doc_quality`）：参数是否列全、环境变量是否写明、
           有没有可直接复制的调用示例。
        """
        targets = self._runnable_targets(state)
        if not targets:
            return {KEY_HELP_OUTCOMES: []}

        settings = self.deps.settings()
        results = await self._run_per_script(
            await self._load_skill(state),
            targets,
            lambda target, _workspace: (target.command("--help"), None),
            timeout_s=settings.help_probe_timeout_s,
        )

        outcomes: list[ScriptCheckOutcome] = []
        verdict_ids: list[str] = []
        for target, result in results:
            # 有的脚本习惯把 help 输出到 stderr，两者都接受（docs/dev/14 第 5 节）。
            help_text = result.stdout.strip() or result.stderr.strip()
            gate = self.deps.judge().quantitative_verdict(
                subject_id=f"{SUBJECT_PREFIX_HELP}{target.path}",
                rule_name=rules.RULE_HELP_RESPONSIVE,
                inputs={
                    rules.KEY_HELP_OUTPUT_CHARS: len(help_text),
                    rules.KEY_EXIT_CODE: result.exit_code,
                    rules.KEY_TIMED_OUT: result.timed_out,
                },
            )
            verdict_ids.append(gate.verdict_id)
            if gate.status is JudgeVerdictStatus.FAIL:
                await self.deps.judge_repository.save_verdict(gate)
                outcomes.append(
                    ScriptCheckOutcome(
                        script_path=target.path,
                        check=rules.RULE_HELP_RESPONSIVE,
                        verdict_id=gate.verdict_id,
                        status=JudgeVerdictStatus.FAIL,
                        reasoning_excerpt=(
                            "脚本未响应 --help 标志（stdout/stderr 均为空"
                            f"{'，且执行超时' if result.timed_out else ''}）："
                            "智能体无法自学如何调用它。"
                        ),
                    )
                )
                continue

            review = await self.deps.judge().judgmental_verdict(
                subject_id=f"{SUBJECT_PREFIX_HELP}{target.path}",
                template_key=TEMPLATE_HELP_DOC_QUALITY,
                # 变量名以模板注册表的 `required_variables` 为准（docs/dev/07 模板
                # 5.4 要的是 script_path + help_output），缺变量会在**发请求之前**
                # 抛 ReviewTemplateError（模板环境用 StrictUndefined）。
                content={"script_path": target.path, "help_output": help_text},
                criticality=HELP_REVIEW_CRITICALITY,
            )
            outcome = self._to_outcome(
                target.path,
                TEMPLATE_HELP_DOC_QUALITY,
                review,
                node=NODE_NAMES["self_learning_doc_test"],
            )
            outcomes.append(outcome)
            if outcome.verdict_id:
                verdict_ids.append(outcome.verdict_id)

        logger.info(
            "script_usability_help_reviewed",
            run_id=str(state["run_id"]),
            node_name=NODE_NAMES["self_learning_doc_test"],
            reviewed=len(outcomes),
            failed=[o.script_path for o in outcomes if o.status is JudgeVerdictStatus.FAIL],
        )
        return {
            "judge_verdict_ids": verdict_ids,
            KEY_HELP_OUTCOMES: [o.model_dump() for o in outcomes],
        }

    # ------------------------------------------------------------------ #
    # 4. constructive_error_and_io_separation_test
    # ------------------------------------------------------------------ #

    async def constructive_error_and_io_separation_test(
        self, state: ScriptUsabilityState
    ) -> dict[str, object]:
        """脏数据容错与流隔离（docs/dev/14 第 6 节，模板 5.5）。

        每个脚本跑**多种**预置脏数据模式（见 `probes.generate_dirty_payloads()` 的
        说明：多试几种、只审一次，是这里性价比最高的组合），然后：

        1. 确定性判定 `script_rejects_dirty_input`：至少有一种模式被脚本识别出来
           （非 0 退出）。全部照单全收 = 智能体拿不到任何"输入有问题"的信号；
        2. 挑一条最有代表性的报错交给模板 5.5 审查"建设性"；
        3. 对同一条结果跑 `check_io_separation()`——**补充信号，不参与判定**
           （docs/dev/14 第 6.2 节），结论写进 `ScriptCheckOutcome.detail`。
        """
        targets = self._runnable_targets(state)
        if not targets:
            return {KEY_ERROR_OUTCOMES: []}

        settings = self.deps.settings()
        semaphore = asyncio.Semaphore(self.deps.concurrency_limit())
        outcomes: list[ScriptCheckOutcome] = []
        verdict_ids: list[str] = []

        skill = await self._load_skill(state)
        gathered = await asyncio.gather(
            *(self._probe_dirty_inputs(skill, target, semaphore) for target in targets)
        )
        for target, payloads, results in gathered:
            rejected = [r for _, r in results if _is_rejection(r)]
            gate = self.deps.judge().quantitative_verdict(
                subject_id=f"{SUBJECT_PREFIX_DIRTY}{target.path}",
                rule_name=rules.RULE_REJECTS_DIRTY_INPUT,
                inputs={
                    rules.KEY_MODE_COUNT: len(payloads),
                    rules.KEY_REJECTED_COUNT: len(rejected),
                },
            )
            verdict_ids.append(gate.verdict_id)

            candidate = _pick_review_candidate(results)
            if candidate is None:
                # 没有任何一次调用产生可审查的报错。这本身就是结论：要么脚本把脏
                # 数据全收了（gate 已判 FAIL），要么它连报错都没打印。不调 LLM。
                if gate.status is not JudgeVerdictStatus.PASS:
                    await self.deps.judge_repository.save_verdict(gate)
                outcomes.append(
                    ScriptCheckOutcome(
                        script_path=target.path,
                        check=rules.RULE_REJECTS_DIRTY_INPUT,
                        verdict_id=gate.verdict_id,
                        status=gate.status,
                        reasoning_excerpt=(
                            f"{len(payloads)} 种脏数据模式全部以 exit_code=0 结束且未产生"
                            "任何报错输出：智能体无法察觉自己传了非法输入。"
                        ),
                    )
                )
                continue

            payload, result = candidate
            review = await self.deps.judge().judgmental_verdict(
                subject_id=f"{SUBJECT_PREFIX_DIRTY}{target.path}",
                template_key=TEMPLATE_CONSTRUCTIVE_ERROR,
                # 变量名以模板注册表的 `required_variables` 为准（模板 5.5 要的是
                # invocation + error_output）。
                content={
                    "invocation": describe_invocation(
                        target, payload, flag=settings.dirty_input_flag
                    ),
                    "error_output": format_error_output(result),
                },
                criticality=ERROR_REVIEW_CRITICALITY,
            )
            outcome = self._to_outcome(
                target.path,
                TEMPLATE_CONSTRUCTIVE_ERROR,
                review,
                node=NODE_NAMES["constructive_error_and_io_separation_test"],
            )
            outcome.detail = _io_separation_detail(result)
            outcomes.append(outcome)
            if outcome.verdict_id:
                verdict_ids.append(outcome.verdict_id)

        logger.info(
            "script_usability_error_handling_reviewed",
            run_id=str(state["run_id"]),
            node_name=NODE_NAMES["constructive_error_and_io_separation_test"],
            reviewed=len(outcomes),
            failed=[o.script_path for o in outcomes if o.status is JudgeVerdictStatus.FAIL],
        )
        return {
            "judge_verdict_ids": verdict_ids,
            KEY_ERROR_OUTCOMES: [o.model_dump() for o in outcomes],
        }

    async def _probe_dirty_inputs(
        self,
        skill: SkillDefinition,
        target: ScriptProbeTarget,
        semaphore: asyncio.Semaphore,
    ) -> tuple[ScriptProbeTarget, list[DirtyPayload], list[tuple[DirtyPayload, ProcessResult]]]:
        """对一个脚本依次跑完各脏数据模式，全部在**同一个**独立工作区里。

        同一个脚本的几种模式共用工作区是安全的：脏数据探测期望脚本在校验阶段就
        拒绝，不该产生副作用；而不同脚本之间必须隔离（各自一个工作区），否则脚本
        A 留下的文件会让脚本 B 的行为不可复现。

        模式之间**串行**：它们共用一个工作区，并行写同名文件会互相打架；跨脚本
        的并发由外层 `asyncio.gather` + 信号量承担。
        """
        settings = self.deps.settings()
        payloads = generate_dirty_payloads(
            target,
            modes=settings.dirty_payload_modes or None,
            oversized_bytes=settings.oversized_payload_bytes,
        )
        results: list[tuple[DirtyPayload, ProcessResult]] = []
        async with self._workspace(skill) as workspace:
            for payload in payloads:
                if payload.file_name and payload.file_bytes is not None:
                    await asyncio.to_thread(
                        (workspace / payload.file_name).write_bytes, payload.file_bytes
                    )
                async with semaphore:
                    results.append(
                        (
                            payload,
                            await self.deps.sandbox().run(
                                image=target.image,
                                command=target.command(
                                    settings.dirty_input_flag, payload.arg_value
                                ),
                                cwd=str(workspace),
                                timeout_s=settings.dirty_input_timeout_s,
                            ),
                        )
                    )
        return target, payloads, results

    # ------------------------------------------------------------------ #
    # 5. idempotency_and_safety_guards
    # ------------------------------------------------------------------ #

    async def idempotency_and_safety_guards(self, state: ScriptUsabilityState) -> dict[str, object]:
        """幂等性与输出截断（docs/dev/14 第 7 节）。

        每个可运行脚本先用一份**良性样本**跑一次基准调用，然后：

        - `is_mutating is True` → 在**同一个工作区**再跑一次，检查第二次是否以
          未处理崩溃收场（"状态已存在"场景没被妥善处理）。这是阻断项；
        - `is_mutating is None` → 跳过幂等性判定并记一条"无法自动判定是否为突变
          脚本，建议人工确认"（docs/dev/14 第 7 节的明确要求）；
        - 无论是否突变，都对每次执行做**输出体量**检查（防刷屏），只告警。

        ### 相对正文的两处实现决策

        1. 正文只对突变脚本跑，因而非突变脚本永远不会被做输出体量检查。这里改成
           "所有可运行脚本都跑一次基准调用"：防刷屏是**所有**脚本都该有的能力，
           而多一次容器调用的成本远低于漏掉一整类脚本。
        2. 只有第一次执行成功（exit_code == 0）时才判幂等性。第一次就失败说明我们
           **没能把脚本正常调起来**（它的参数约定和我们猜的不一样），此时第二次的
           任何异常都不构成"幂等性缺陷"的证据——拿它去阻断合并就是误报，而这是本
           维度最不能出的错。这种情况如实记一条"无法构造有效调用"的提示。
        """
        targets = self._runnable_targets(state)
        if not targets:
            return {KEY_IDEMPOTENCY_FINDINGS: []}

        semaphore = asyncio.Semaphore(self.deps.concurrency_limit())
        skill = await self._load_skill(state)
        gathered = await asyncio.gather(
            *(self._probe_idempotency(skill, target, semaphore) for target in targets)
        )

        findings: list[str] = []
        verdict_ids: list[str] = []
        for target, runs in gathered:
            findings_for_script, ids = await self._judge_idempotency(target, runs)
            findings.extend(findings_for_script)
            verdict_ids.extend(ids)

        logger.info(
            "script_usability_idempotency_probed",
            run_id=str(state["run_id"]),
            node_name=NODE_NAMES["idempotency_and_safety_guards"],
            probed=len(gathered),
            blocking=sum(1 for f in findings if f.startswith(BLOCKING_PREFIX)),
            warnings=sum(1 for f in findings if f.startswith(WARNING_PREFIX)),
        )
        return {"judge_verdict_ids": verdict_ids, KEY_IDEMPOTENCY_FINDINGS: findings}

    async def _probe_idempotency(
        self,
        skill: SkillDefinition,
        target: ScriptProbeTarget,
        semaphore: asyncio.Semaphore,
    ) -> tuple[ScriptProbeTarget, list[ProcessResult]]:
        """基准调用（+ 突变脚本的第二次调用），两次共用同一个工作区。

        共用工作区是这项检查成立的前提：第二次执行必须能看见第一次留下的文件/
        数据，否则"状态已存在"的场景根本不会发生，测了等于没测。
        """
        settings = self.deps.settings()
        command = target.command(settings.dirty_input_flag, BENIGN_SAMPLE_NAME)
        runs: list[ProcessResult] = []
        async with self._workspace(skill) as workspace:
            await asyncio.to_thread(
                (workspace / BENIGN_SAMPLE_NAME).write_bytes, BENIGN_SAMPLE_BYTES
            )
            async with semaphore:
                runs.append(
                    await self.deps.sandbox().run(
                        image=target.image,
                        command=command,
                        cwd=str(workspace),
                        timeout_s=settings.idempotency_timeout_s,
                    )
                )
            # 第二次只对"会写外部状态"的脚本跑：只读脚本连续跑两次必然一样，
            # 白烧一个容器。
            if is_mutating_script(target) is True and runs[0].exit_code == 0:
                async with semaphore:
                    runs.append(
                        await self.deps.sandbox().run(
                            image=target.image,
                            command=command,
                            cwd=str(workspace),
                            timeout_s=settings.idempotency_timeout_s,
                        )
                    )
        return target, runs

    async def _judge_idempotency(
        self, target: ScriptProbeTarget, runs: list[ProcessResult]
    ) -> tuple[list[str], list[str]]:
        """把一个脚本的 1~2 次执行结果翻译成 findings + verdict_id。"""
        settings = self.deps.settings()
        judge = self.deps.judge()
        findings: list[str] = []
        verdict_ids: list[str] = []

        # ---- 输出体量（防刷屏），对每一次执行都查 ----
        for index, result in enumerate(runs):
            verdict = judge.quantitative_verdict(
                subject_id=f"{SUBJECT_PREFIX_OUTPUT}{target.path}#{index}",
                rule_name=rules.RULE_OUTPUT_BOUNDED,
                inputs={
                    rules.KEY_OUTPUT_BYTES: result.total_output_bytes,
                    rules.KEY_WARN_BYTES: settings.output_truncation_warn_bytes,
                    rules.KEY_ALREADY_TRUNCATED: result.truncated,
                },
            )
            verdict_ids.append(verdict.verdict_id)
            if verdict.status is JudgeVerdictStatus.FAIL:
                await self.deps.judge_repository.save_verdict(verdict)
                findings.append(
                    f"{WARNING_PREFIX} {target.path} 第 {index + 1} 次执行输出 "
                    f"{result.total_output_bytes} 字节，超过防刷屏建议上限 "
                    f"{settings.output_truncation_warn_bytes} 字节，且未见脚本自身的截断标记："
                    "过长输出会挤占智能体的上下文窗口。"
                )

        # ---- 幂等性 ----
        mutating = is_mutating_script(target)
        if mutating is None:
            findings.append(
                f"{INFO_PREFIX} {target.path}：无法自动判定是否为突变脚本，已跳过幂等性测试，"
                "建议人工确认。"
            )
            return findings, verdict_ids
        if mutating is False:
            return findings, verdict_ids
        if runs[0].exit_code != 0:
            findings.append(
                f"{INFO_PREFIX} {target.path}：基准调用未能成功执行"
                f"（exit_code={runs[0].exit_code}{'，超时' if runs[0].timed_out else ''}），"
                f"无法构造有效的连续执行场景，已跳过幂等性判定。"
                f"若该脚本的参数约定不是 `{settings.dirty_input_flag} <文件>`，"
                "请调整 SKILLEVAL_SCRIPT_USABILITY_DIRTY_INPUT_FLAG 后重跑。"
            )
            return findings, verdict_ids

        second = runs[1]
        crashed = looks_like_unhandled_crash(second.stderr) or second.timed_out
        verdict = judge.quantitative_verdict(
            subject_id=f"{SUBJECT_PREFIX_IDEMPOTENCY}{target.path}",
            rule_name=rules.RULE_IDEMPOTENT,
            inputs={
                rules.KEY_CRASHED: crashed,
                rules.KEY_EXIT_CODE: second.exit_code,
                rules.KEY_TIMED_OUT: second.timed_out,
            },
        )
        verdict_ids.append(verdict.verdict_id)
        if verdict.status is JudgeVerdictStatus.FAIL:
            await self.deps.judge_repository.save_verdict(verdict)
            findings.append(
                f"{BLOCKING_PREFIX} {target.path} 连续两次执行导致未处理崩溃"
                f"（第二次 exit_code={second.exit_code}"
                f"{'、超时未退出' if second.timed_out else ''}）："
                "'状态已存在'的场景没有被妥善处理，智能体重试一次就会把任务打断。"
            )
        return findings, verdict_ids

    # ------------------------------------------------------------------ #
    # 6. finalize_dimension_report
    # ------------------------------------------------------------------ #

    async def finalize_dimension_report(self, state: ScriptUsabilityState) -> dict[str, object]:
        """聚合四路结果写进 `dimension_results`（docs/dev/14 第 8 节）。

        判定口径：

        | 情形 | status | blocking |
        |---|---|---|
        | 非交互性挂起（致命） | FAIL | **True** |
        | 连续执行未处理崩溃 | FAIL | **True** |
        | Help 文档质量 / 建设性报错未通过 | FAIL | False（主观审查，只告警） |
        | 输出超过防刷屏上限 | FAIL | False |
        | 沙箱不可用 / 脚本跑不起来 / 无法判定是否突变 | NEEDS_HUMAN_REVIEW | False |
        | Skill 没有 `scripts/` 目录 | PASS | False |
        | 其余 | PASS | False |

        **阻断策略延续模块二/三的一贯原则**：能被确定性验证的可用性硬伤（挂起、
        崩溃）阻断合并；依赖 LLM 主观审查的"文档写得好不好""报错够不够建设性"
        只告警——避免主观审查的误报直接拖垮流水线的可信度。

        `score=None`：四项性质完全不同的检查，硬凑"通过项/总项数"会把它们平均成
        一个没有含义的数字（与模块三同一口径）。
        """
        run_id = str(state["run_id"])
        targets = self._targets(state)
        unavailable = cast("str | None", state.get(KEY_SANDBOX_UNAVAILABLE))
        prepare_findings = _str_list(state, KEY_PREPARE_FINDINGS)
        fatal = _str_list(state, KEY_HARD_FAILURE_FINDINGS)
        idempotency = _str_list(state, KEY_IDEMPOTENCY_FINDINGS)
        outcomes = [
            *self._outcomes(state, KEY_HELP_OUTCOMES),
            *self._outcomes(state, KEY_ERROR_OUTCOMES),
        ]

        blocking_findings = [f for f in idempotency if f.startswith(BLOCKING_PREFIX)]
        soft_findings = [f for f in idempotency if not f.startswith(BLOCKING_PREFIX)]
        failed_outcomes = [o for o in outcomes if o.status is JudgeVerdictStatus.FAIL]

        findings: list[str] = []
        if unavailable:
            findings.append(f"{WARNING_PREFIX} {unavailable}")
        if not targets:
            findings.append(
                f"{INFO_PREFIX} 本 Skill 未附带任何 scripts/ 脚本，本维度没有适用的检查项。"
            )
        findings.extend(prepare_findings)
        findings.extend(fatal)
        findings.extend(blocking_findings)
        findings.extend(
            f"{WARNING_PREFIX} [{o.check}] {o.script_path} 未通过（非阻断，供人工参考）："
            f"{o.reasoning_excerpt}{(' | ' + o.detail) if o.detail else ''}"
            for o in failed_outcomes
        )
        findings.extend(
            f"{INFO_PREFIX} [{o.check}] {o.script_path} 本次未产生结论：{o.skipped_reason}"
            for o in outcomes
            if o.skipped_reason
        )
        findings.extend(soft_findings)
        # 流隔离是补充信号：即使这一项的语义审查通过了，流隔离的异常也要出现在
        # 报告里（docs/dev/14 第 6.2 节要求两者并列呈现，由人自行取舍）。
        findings.extend(
            f"{INFO_PREFIX} [{o.check}] {o.script_path} 流隔离补充检查：{o.detail}"
            for o in outcomes
            if o.detail and o.status is not JudgeVerdictStatus.FAIL
        )

        blocking = bool(fatal or blocking_findings)
        needs_human = bool(
            unavailable
            or any(t.skip_reason for t in targets)
            or any(f.startswith(INFO_PREFIX) for f in idempotency)
            or (outcomes and all(o.skipped_reason for o in outcomes))
        )
        status = self._resolve_status(
            blocking=blocking,
            soft_failed=bool(failed_outcomes or soft_findings),
            needs_human=needs_human,
        )

        await self.deps.reporter().record_dimension_result(
            run_id=run_id,
            dimension=DIMENSION,
            status=status,
            score=None,
            findings=findings,
            # **关键策略**：只有挂起与幂等性崩溃阻断合并。见本方法的口径表。
            blocking=blocking,
        )
        logger.info(
            "script_usability_dimension_recorded",
            run_id=run_id,
            node_name=TERMINAL_NODE,
            status=status.value,
            blocking=blocking,
            scripts=len(targets),
            findings=len(findings),
        )
        return {}

    @staticmethod
    def _resolve_status(
        *, blocking: bool, soft_failed: bool, needs_human: bool
    ) -> JudgeVerdictStatus:
        """维度级状态的优先级：FAIL > NEEDS_HUMAN_REVIEW > PASS。

        与 docs/dev/interfaces/13 第 6 节同一口径：非阻断问题也判 FAIL（`blocking`
        才是"能不能合并"的唯一依据），而"有事情要人看一眼但还谈不上失败"用
        NEEDS_HUMAN_REVIEW——报告里出现"结论通过、正文列着一串问题"是自相矛盾的。
        """
        if blocking or soft_failed:
            return JudgeVerdictStatus.FAIL
        if needs_human:
            return JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        return JudgeVerdictStatus.PASS

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    def _to_outcome(
        self,
        script_path: str,
        template_key: str,
        result: JudgeVerdict | ConsensusResult,
        *,
        node: str,
    ) -> ScriptCheckOutcome:
        """把 Judge 的返回值收敛成状态里存的摘要，并在此处理两种特殊返回。

        1. **黄金基准盲测**：`judgmental_verdict()` 有 2% 概率把请求整个换成一条
           人类标定过的黄金用例来考核裁判自己。这类结果的 `subject_id` 带
           `__golden__:` 前缀，**必须跳过**——把它当成本脚本的审查结论写进报告，
           等于用另一段文本的判决给这个脚本定性（docs/dev/interfaces/08 第 3 节）。
        2. **共识未达成**：本维度两项审查都声明 ROUTINE，正常拿不到 `ConsensusResult`。
           但 criticality 是可运维调整的，一旦有人调成 CRITICAL 这条路径就会活过来。
           此时 `NEEDS_HUMAN_REVIEW` **不允许被降级**为 PASS/FAIL（docs/dev/08 的
           明令禁止项），正确处理是挂起等人工仲裁。
        """
        if isinstance(result, ConsensusResult) and not result.consensus_reached:
            raise PipelineSuspended(
                f"{node}：模板 {template_key!r} 对脚本 {script_path!r} 的三副本复核未达成共识"
                f"（subject_id={result.subject_id!r}），需人工仲裁。"
            )

        status = result.final_status if isinstance(result, ConsensusResult) else result.status
        verdict = (
            result.verdicts[0]
            if isinstance(result, ConsensusResult) and result.verdicts
            else result
        )

        if is_golden_subject(result.subject_id):
            logger.info(
                "script_usability_review_consumed_by_golden_case",
                template_key=template_key,
                subject_id=result.subject_id,
            )
            return ScriptCheckOutcome(
                script_path=script_path,
                check=template_key,
                skipped_reason="本次请求被黄金基准盲测占用，未产生针对本脚本的审查结论",
            )

        return ScriptCheckOutcome(
            script_path=script_path,
            check=template_key,
            verdict_id=getattr(verdict, "verdict_id", None),
            status=status,
            reasoning_excerpt=str(getattr(verdict, "reasoning", ""))[:REASONING_EXCERPT_CHARS],
        )

    async def _run_per_script(
        self,
        skill: SkillDefinition,
        targets: Sequence[ScriptProbeTarget],
        build: CommandBuilder,
        *,
        timeout_s: int,
    ) -> list[tuple[ScriptProbeTarget, ProcessResult]]:
        """对每个脚本各起一个独立工作区跑一次命令，跨脚本并发（有上限）。

        `build(target, workspace) -> (command, stdin_data)` 由调用方给出：挂起探测
        要"不带参数 + 空 stdin"，help 探测要"带 --help + 不喂 stdin"，两者的差别
        只在这一处，没必要为此写两个近乎相同的循环。
        """
        semaphore = asyncio.Semaphore(self.deps.concurrency_limit())

        async def run_one(target: ScriptProbeTarget) -> tuple[ScriptProbeTarget, ProcessResult]:
            async with self._workspace(skill) as workspace:
                command, stdin_data = build(target, workspace)
                async with semaphore:
                    result = await self.deps.sandbox().run(
                        image=target.image,
                        command=command,
                        stdin_data=stdin_data,
                        cwd=str(workspace),
                        timeout_s=timeout_s,
                    )
            return target, result

        return list(await asyncio.gather(*(run_one(target) for target in targets)))

    @asynccontextmanager
    async def _workspace(self, skill: SkillDefinition) -> AsyncIterator[Path]:
        """一次探测的独立工作区：整份 Skill 目录的临时副本，用完即删。

        复制而不是直接挂载源目录：容器里的写操作会真的落到宿主机上，直接挂载等于
        让一次评测改动开发者的工作区（架构文档模块四第 5 节点名的那条权衡）。
        `copytree`/`rmtree` 走 `to_thread`，避免在事件循环里做同步 IO 把并发拖垮。
        """
        temp_dir = await asyncio.to_thread(tempfile.mkdtemp, prefix="skilleval-script-")
        try:
            await asyncio.to_thread(prepare_workspace, skill.root_path, temp_dir)
            yield Path(temp_dir)
        finally:
            await asyncio.to_thread(shutil.rmtree, temp_dir, True)

    async def _load_skill(self, state: ScriptUsabilityState) -> SkillDefinition:
        """取被测 Skill。

        本维度**不**支持"用 Optimizer 的工作副本"（模块一/三的 `_working_skill`）：
        它探测的是仓库里那份 `scripts/` 的原貌，拿一份内存里改过的版本来测，报告
        就与人能看到的文件对不上了。
        """
        skill = await self.deps.skill_repository.get(
            str(state["skill_id"]), str(state["skill_version_ref"])
        )
        if skill is None:
            raise PersistenceError(
                f"未找到被测 Skill：skill_id={state['skill_id']!r} "
                f"version_ref={state['skill_version_ref']!r}。"
                "请先经 `ingestion.load_skill()` + `SkillRepository.save()` 入库。"
            )
        return skill

    @staticmethod
    def _targets(state: ScriptUsabilityState) -> list[ScriptProbeTarget]:
        raw = cast("list[object] | None", state.get(KEY_TARGETS))
        return [ScriptProbeTarget.model_validate(item) for item in (raw or [])]

    def _runnable_targets(self, state: ScriptUsabilityState) -> list[ScriptProbeTarget]:
        """可以真的跑起来的脚本。

        沙箱不可用时返回空列表：三条探测支路因此全部空转，收尾节点按
        `_su_sandbox_unavailable` 判 NEEDS_HUMAN_REVIEW。这比让每条支路各自抛异常
        要好——那样报告里只剩一个栈回溯，看不出"到底测了什么、没测什么"。
        """
        if state.get(KEY_SANDBOX_UNAVAILABLE):
            return []
        return [t for t in self._targets(state) if t.runnable]

    @staticmethod
    def _outcomes(state: ScriptUsabilityState, key: str) -> list[ScriptCheckOutcome]:
        raw = cast("list[object] | None", state.get(key))
        return [ScriptCheckOutcome.model_validate(item) for item in (raw or [])]


# --------------------------------------------------------------------------- #
# 模块级纯函数
# --------------------------------------------------------------------------- #


def _is_rejection(result: ProcessResult) -> bool:
    """这次脏数据调用算不算"脚本识别出了非法输入"。

    超时**不算**识别：那是另一种失败（脚本在脏数据上卡住了），会由 finalize 里的
    输出/挂起相关项各自反映，把它算成"成功拒绝"会掩盖一个更严重的问题。
    """
    return not result.timed_out and result.exit_code not in (0, None)


def _pick_review_candidate(
    results: list[tuple[DirtyPayload, ProcessResult]],
) -> tuple[DirtyPayload, ProcessResult] | None:
    """从多次脏数据调用里挑一条最值得交给 LLM 审查的报错。

    优先级：**被拒绝且有 stderr** > 被拒绝（哪怕报错打在 stdout 上，那本身是流隔离
    问题，正该被审查） > 无（返回 None，调用方据此跳过 LLM）。

    只审一条而不是逐条审：模板 5.5 审的是"这个脚本的报错风格建不建设性"，同一个
    脚本的几条报错通常出自同一套错误处理代码，逐条审只是把同一个结论买四遍。
    """
    rejected = [(payload, result) for payload, result in results if _is_rejection(result)]
    if not rejected:
        return None
    with_stderr = [item for item in rejected if item[1].stderr.strip()]
    return (with_stderr or rejected)[0]


def _io_separation_detail(result: ProcessResult) -> str:
    """流隔离检查的人类可读结论（补充信号，不参与判定）。"""
    if check_io_separation(result):
        return "流隔离检查通过：失败时 stdout 未混入错误/堆栈特征"
    return (
        "流隔离检查未通过：脚本以非 0 退出，但错误/堆栈特征出现在 stdout 里。"
        "干净数据应走 stdout（供智能体做管道级联），诊断信息应走 stderr"
        "（启发式规则，最终定性以建设性报错的语义审查为准）"
    )


def _str_list(state: ScriptUsabilityState, key: str) -> list[str]:
    raw = cast("list[str] | None", state.get(key))
    return list(raw or [])


__all__ = [
    "BENIGN_SAMPLE_BYTES",
    "BENIGN_SAMPLE_NAME",
    "BLOCKING_PREFIX",
    "ENTRY_NODE",
    "FATAL_PREFIX",
    "INFO_PREFIX",
    "NODE_NAMES",
    "REASONING_EXCERPT_CHARS",
    "TERMINAL_NODE",
    "WARNING_PREFIX",
    "ScriptCheckOutcome",
    "ScriptUsabilityPipeline",
]
