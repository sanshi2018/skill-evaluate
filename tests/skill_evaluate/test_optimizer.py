"""docs/dev/09：Optimizer Agent 与闭环重试策略。

覆盖：训练集约束的强制执行、角色与补丁类型的边界、unified diff 的应用与容错、
临时工作副本的隔离、闭环重试与超限挂起。全部用替身注入，不碰数据库、不发真实
请求。
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from skill_evaluate.agents.llm import LLMCompletion
from skill_evaluate.agents.optimizer import (
    ROLE_APPSEC_EXPERT,
    FailureContext,
    LoopResult,
    OptimizationLoop,
    OptimizerAgent,
    apply_patch,
    apply_unified_diff,
    build_failure_context,
    cleanup_working_copy,
)
from skill_evaluate.agents.optimizer import loop as loop_module
from skill_evaluate.agents.optimizer.patch_applier import WORKING_COPY_MARKER
from skill_evaluate.errors import AgentError, PatchApplyError
from skill_evaluate.state.enums import (
    DatasetSplit,
    JudgeVerdictStatus,
    PatchType,
    TestCaseCategory,
)
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.patch import Patch
from skill_evaluate.state.skill import SkillDefinition
from skill_evaluate.state.test_case import TestCase

NL = "\n"


def _skill(
    description: str = "清洗 CSV 导出文件", body: str = "# CSV Cleaner\n\n## 规则\n"
) -> SkillDefinition:
    return SkillDefinition(
        skill_id="csv-cleaner",
        version_ref="v1",
        root_path=".",
        description=description,
        body_markdown=body,
        line_count=len(body.splitlines()),
        token_count=40,
    )


def _case(case_id: str, split: DatasetSplit = DatasetSplit.TRAIN) -> TestCase:
    return TestCase(
        case_id=case_id,
        skill_id="csv-cleaner",
        category=TestCaseCategory.POSITIVE,
        split=split,
        prompt=f"帮我处理一下导出的表格（{case_id}）",
        generator_run_id="gen-1",
        created_at=datetime.now(UTC),
    )


def _verdict(reasoning: str = "没有命中 description") -> JudgeVerdict:
    return JudgeVerdict(
        verdict_id="v-1",
        subject_id="case-1",
        status=JudgeVerdictStatus.FAIL,
        reasoning=reasoning,
        temperature=0.1,
        model="anthropic/claude-haiku-4.5",
        created_at=datetime.now(UTC),
    )


def _patch(
    diff: str,
    *,
    patch_type: PatchType = PatchType.DESCRIPTION_PATCH,
    target_path: str = "SKILL.md",
    base_ref: str = "v1",
    patch_id: str = "p-1",
) -> Patch:
    return Patch(
        patch_id=patch_id,
        skill_id="csv-cleaner",
        base_skill_version_ref=base_ref,
        patch_type=patch_type,
        target_path=target_path,
        diff=diff,
        rationale="因为失败用例里都在说「表格」",
        created_at=datetime.now(UTC),
    )


class StaticLLMClient:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = payloads
        self.prompts: list[str] = []

    async def complete(
        self,
        *,
        prompt: str,
        model: str,
        temperature: float,
        system: str | None = None,
        max_tokens: int | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> LLMCompletion:
        self.prompts.append(prompt)
        payload = self._payloads[min(len(self.prompts) - 1, len(self._payloads) - 1)]
        return LLMCompletion(
            text=json.dumps(payload, ensure_ascii=False),
            prompt_tokens=5,
            completion_tokens=5,
            model=model,
            temperature_applied=temperature,
        )


class FakePatchRepo:
    def __init__(self) -> None:
        self.patches: list[Patch] = []
        self.results: list[Any] = []

    async def save(self, patch: Patch) -> None:
        self.patches.append(patch)

    async def save_application_result(self, result: Any) -> None:
        self.results.append(result)


class FakeStateRepo:
    def __init__(self) -> None:
        self.increments: list[tuple[str, str]] = []

    async def increment_retry(self, run_id: str, node_name: str) -> int:
        self.increments.append((run_id, node_name))
        return len(self.increments)


class FakeApprovalRepo:
    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []

    async def create(self, *, run_id: str, node_name: str, thread_id: str, wait_key: str) -> None:
        self.created.append(
            {"run_id": run_id, "node_name": node_name, "thread_id": thread_id, "wait_key": wait_key}
        )


# --------------------------------------------------------------------------- #
# 训练集约束（docs/dev/09 第 4 节）
# --------------------------------------------------------------------------- #


class FailureContextTests:
    def test_validation_cases_are_rejected(self) -> None:
        # 防过拟合的约束落在类型层面，而不是靠调用方自觉——真正想拿验证集去优化
        # 的那一次，恰恰是最想走捷径的那一次。
        with pytest.raises(ValueError, match="不得接收非训练集用例"):
            build_failure_context(
                _skill(),
                [_case("c-1"), _case("c-2", DatasetSplit.VALIDATION)],
                [_verdict()],
            )

    def test_train_cases_carry_their_prompts_into_the_context(self) -> None:
        ctx = build_failure_context(_skill(), [_case("c-1")], [_verdict()])
        assert ctx.failed_case_ids == ["c-1"]
        # 只给 id 的 Prompt 对模型毫无信息量。
        assert ctx.failed_case_prompts == ["帮我处理一下导出的表格（c-1）"]

    def test_consensus_results_are_flattened(self) -> None:
        consensus = ConsensusResult(
            subject_id="case-1",
            verdicts=[_verdict("理由一"), _verdict("理由二")],
            consensus_reached=True,
            final_status=JudgeVerdictStatus.FAIL,
        )
        ctx = build_failure_context(_skill(), [_case("c-1")], [consensus])
        assert [v.reasoning for v in ctx.verdicts] == ["理由一", "理由二"]

    def test_unknown_role_fails_fast(self) -> None:
        with pytest.raises(AgentError, match="未注册的 Optimizer 角色"):
            build_failure_context(_skill(), [_case("c-1")], [_verdict()], role="astrologer")


# --------------------------------------------------------------------------- #
# unified diff
# --------------------------------------------------------------------------- #


class UnifiedDiffTests:
    def test_replace_and_insert(self) -> None:
        original = NL.join(["line1", "line2", "line3"]) + NL
        diff = NL.join(
            [
                "@@ -1,3 +1,4 @@",
                " line1",
                "-line2",
                "+line2 modified",
                "+line2 extra",
                " line3",
            ]
        )
        assert apply_unified_diff(original, diff) == (
            NL.join(["line1", "line2 modified", "line2 extra", "line3"]) + NL
        )

    def test_wrong_line_numbers_still_apply(self) -> None:
        # 模型写的行号常常是错的，但只要上下文对得上就应该能打上去。
        original = NL.join(["a", "b", "target", "c"])
        diff = NL.join(["@@ -99,1 +99,1 @@", "-target", "+patched"])
        assert apply_unified_diff(original, diff) == NL.join(["a", "b", "patched", "c"])

    def test_context_mismatch_is_rejected(self) -> None:
        # 定位容错 ≠ 放宽匹配：旧内容块必须逐字符一致。
        with pytest.raises(PatchApplyError, match="与当前内容不匹配"):
            apply_unified_diff("a\nb\n", NL.join(["@@ -1,1 +1,1 @@", "-not-there", "+x"]))

    def test_whole_file_rewrite_without_hunks_is_rejected(self) -> None:
        with pytest.raises(PatchApplyError, match="没有任何 @@ hunk"):
            apply_unified_diff("a\n", "这是我重写后的完整文件内容")

    def test_file_headers_are_ignored(self) -> None:
        diff = NL.join(["--- a/SKILL.md", "+++ b/SKILL.md", "@@ -1,1 +1,1 @@", "-a", "+b"])
        assert apply_unified_diff("a", diff) == "b"


# --------------------------------------------------------------------------- #
# 补丁应用
# --------------------------------------------------------------------------- #


class ApplyPatchTests:
    def test_description_patch_updates_description_only(self) -> None:
        skill = _skill()
        patch = _patch(
            NL.join(
                ["@@ -1,1 +1,1 @@", "-清洗 CSV 导出文件", "+清洗、去重、校验 CSV 或表格导出文件"]
            )
        )
        patched = apply_patch(skill, patch)
        assert patched.description == "清洗、去重、校验 CSV 或表格导出文件"
        assert patched.body_markdown == skill.body_markdown
        assert patched.version_ref == "v1+patch:p-1"
        assert skill.description == "清洗 CSV 导出文件"  # 原对象不被就地修改

    def test_rigid_constraint_patch_updates_body_and_recounts(self) -> None:
        skill = _skill()
        patch = _patch(
            NL.join(["@@ -3,1 +3,3 @@", " ## 规则", "+", "+- 不得读写 data/ 之外的路径"]),
            patch_type=PatchType.RIGID_CONSTRAINT,
        )
        patched = apply_patch(skill, patch)
        assert "不得读写 data/ 之外的路径" in patched.body_markdown
        assert patched.line_count == len(patched.body_markdown.splitlines())

    def test_empty_description_after_patch_is_rejected(self) -> None:
        # description 是模块一的被测对象本身，删空了等于把这个维度废掉。
        patch = _patch(NL.join(["@@ -1,1 +1,0 @@", "-清洗 CSV 导出文件"]))
        with pytest.raises(PatchApplyError, match="内容为空"):
            apply_patch(_skill(), patch)

    def test_stale_base_version_is_rejected(self) -> None:
        patch = _patch("@@ -1,1 +1,1 @@\n-x\n+y", base_ref="v0")
        with pytest.raises(PatchApplyError, match="基线版本已过期"):
            apply_patch(_skill(), patch)

    def test_code_patch_works_on_a_temp_copy_and_leaves_the_repo_untouched(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "csv-cleaner"
        (root / "scripts").mkdir(parents=True)
        (root / "SKILL.md").write_text(
            NL.join(["---", "name: csv-cleaner", "description: 旧的描述", "---", "", "# 正文"]),
            encoding="utf-8",
        )
        script = root / "scripts" / "run.py"
        script.write_text(
            NL.join(["import subprocess", 'subprocess.run(f"convert {p}", shell=True)']),
            encoding="utf-8",
        )

        skill = _skill().model_copy(update={"root_path": str(root), "description": "新的描述"})
        patch = _patch(
            NL.join(
                [
                    "@@ -2,1 +2,1 @@",
                    '-subprocess.run(f"convert {p}", shell=True)',
                    '+subprocess.run(["convert", p], check=True)',
                ]
            ),
            patch_type=PatchType.CODE_PATCH,
            target_path="scripts/run.py",
        )

        patched = apply_patch(skill, patch)
        working_root = Path(patched.root_path)

        assert working_root != root
        assert (working_root / WORKING_COPY_MARKER).is_file()
        assert '["convert", p]' in (working_root / "scripts" / "run.py").read_text(encoding="utf-8")
        # 被测仓库绝对不能被改。
        assert "shell=True" in script.read_text(encoding="utf-8")
        # 工作副本的 SKILL.md 带上内存里已经改过的 description，否则回归跑的是旧的那份。
        assert "description: 新的描述" in (working_root / "SKILL.md").read_text(encoding="utf-8")

        cleanup_working_copy(patched)
        assert not working_root.exists()

    def test_cleanup_never_touches_a_real_repo(self, tmp_path: Path) -> None:
        root = tmp_path / "real-repo"
        root.mkdir()
        (root / "SKILL.md").write_text("# x", encoding="utf-8")
        cleanup_working_copy(_skill().model_copy(update={"root_path": str(root)}))
        assert root.exists()

    def test_path_traversal_in_target_path_is_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "csv-cleaner"
        root.mkdir()
        (root / "SKILL.md").write_text("# x", encoding="utf-8")
        skill = _skill().model_copy(update={"root_path": str(root)})
        patch = _patch(
            "@@ -1,1 +1,1 @@\n-x\n+y",
            patch_type=PatchType.CODE_PATCH,
            target_path="../../etc/passwd",
        )
        with pytest.raises(PatchApplyError, match="越出 skill 根目录"):
            apply_patch(skill, patch)


# --------------------------------------------------------------------------- #
# OptimizerAgent
# --------------------------------------------------------------------------- #


class OptimizerAgentTests:
    def _agent(
        self, payloads: list[dict[str, Any]]
    ) -> tuple[OptimizerAgent, FakePatchRepo, StaticLLMClient]:
        repo = FakePatchRepo()
        client = StaticLLMClient(payloads)
        agent = OptimizerAgent(
            llm_client=client,
            patch_repository=repo,  # type: ignore[arg-type]
        )
        return agent, repo, client

    async def test_prompt_engineer_produces_a_description_patch(self) -> None:
        agent, repo, _client = self._agent(
            [
                {
                    "patch_type": "description_patch",
                    "target_path": "SKILL.md",
                    "diff": "@@ -1,1 +1,1 @@\n-旧\n+新",
                    "rationale": "失败用例都在说「表格」",
                    "functional_risk": "收窄后 TSV 请求可能不再触发",
                }
            ]
        )
        ctx = build_failure_context(_skill(), [_case("c-1")], [_verdict()])
        patch = await agent.propose_patch(ctx)

        assert patch.patch_type is PatchType.DESCRIPTION_PATCH
        assert patch.base_skill_version_ref == "v1"
        # 功能误伤评估必须跟着补丁走到人工审查，不能只留在模型的中间输出里。
        assert "【功能误伤评估】" in patch.rationale
        assert repo.patches == [patch]

    async def test_prompt_includes_failure_evidence_and_diff_rules(self) -> None:
        agent, _repo, client = self._agent(
            [
                {
                    "patch_type": "description_patch",
                    "target_path": "SKILL.md",
                    "diff": "@@ -1,1 +1,1 @@\n-旧\n+新",
                    "rationale": "r",
                    "functional_risk": "",
                }
            ]
        )
        ctx = build_failure_context(_skill(), [_case("c-1")], [_verdict("没有命中 description")])
        await agent.propose_patch(ctx)
        prompt = client.prompts[0]

        assert "帮我处理一下导出的表格（c-1）" in prompt
        assert "没有命中 description" in prompt
        assert "unified diff" in prompt
        assert "最小化改动" in prompt

    async def test_role_boundary_is_enforced(self) -> None:
        # prompt_engineer 交回 code_patch 时，模块五的安全回归根本不会被触发——
        # 这不是"模型有创意"，是这次产出没法被下游正确处理。
        agent, _repo, _client = self._agent(
            [
                {
                    "patch_type": "code_patch",
                    "target_path": "scripts/run.py",
                    "diff": "@@ -1,1 +1,1 @@\n-a\n+b",
                    "rationale": "r",
                    "functional_risk": "",
                }
            ]
        )
        ctx = build_failure_context(_skill(), [_case("c-1")], [_verdict()])
        with pytest.raises(AgentError, match="不允许产出"):
            await agent.propose_patch(ctx)

    async def test_appsec_role_uses_its_own_template(self) -> None:
        agent, _repo, client = self._agent(
            [
                {
                    "patch_type": "code_patch",
                    "target_path": "scripts/run.py",
                    "diff": "@@ -1,1 +1,1 @@\n-a\n+b",
                    "rationale": "命令拼接",
                    "functional_risk": "无：参数语义不变",
                }
            ]
        )
        ctx = build_failure_context(
            _skill(),
            [_case("c-1")],
            [_verdict()],
            role=ROLE_APPSEC_EXPERT,
            triggered_by_finding_id="finding-7",
        )
        patch = await agent.propose_patch(ctx)

        assert patch.patch_type is PatchType.CODE_PATCH
        assert patch.triggered_by_finding_id == "finding-7"
        prompt = client.prompts[0]
        assert "shlex.quote" in prompt  # 安全编码规范的 few-shot
        assert "误伤正常功能路径" in prompt  # 过度杀伤力风险的缓解要求


# --------------------------------------------------------------------------- #
# 闭环
# --------------------------------------------------------------------------- #


class ScriptedOptimizer:
    """替身 Optimizer：按脚本吐补丁，不调 LLM。"""

    def __init__(self, patches: list[Patch]) -> None:
        self._patches = patches
        self.contexts: list[FailureContext] = []

    async def propose_patch(self, ctx: FailureContext) -> Patch:
        self.contexts.append(ctx)
        return self._patches[min(len(self.contexts) - 1, len(self._patches) - 1)]


def _loop() -> tuple[OptimizationLoop, FakePatchRepo, FakeStateRepo, FakeApprovalRepo]:
    patch_repo, state_repo, approval_repo = FakePatchRepo(), FakeStateRepo(), FakeApprovalRepo()
    loop = OptimizationLoop(
        max_retries=3,
        patch_repository=patch_repo,  # type: ignore[arg-type]
        pipeline_state_repository=state_repo,  # type: ignore[arg-type]
        approval_repository=approval_repo,  # type: ignore[arg-type]
    )
    return loop, patch_repo, state_repo, approval_repo


def _desc_patch(patch_id: str, new_text: str, base_ref: str = "v1") -> Patch:
    return _patch(
        NL.join(["@@ -1,1 +1,1 @@", "-清洗 CSV 导出文件", f"+{new_text}"]),
        base_ref=base_ref,
        patch_id=patch_id,
    )


class OptimizationLoopTests:
    async def test_returns_the_patch_that_passes_retest(self) -> None:
        loop, patch_repo, state_repo, _ = _loop()
        optimizer = ScriptedOptimizer([_desc_patch("p-1", "清洗并校验表格导出文件")])
        ctx = build_failure_context(_skill(), [_case("c-1")], [_verdict()])

        async def retest(skill: SkillDefinition) -> LoopResult:
            return LoopResult(passed=True, detail="训练集全通过")

        patch = await loop.run("run-1", ctx, retest, optimizer)  # type: ignore[arg-type]

        assert patch is not None and patch.patch_id == "p-1"
        assert patch_repo.results[0].regression_passed is True
        assert state_repo.increments == []  # 一次过，不该记重试

    async def test_second_attempt_builds_on_the_first(self) -> None:
        loop, patch_repo, state_repo, _ = _loop()
        # 第 2 轮的补丁基线是第 1 轮的产物（v1+patch:p-1），逐轮迭代而不是每轮重开。
        optimizer = ScriptedOptimizer(
            [
                _desc_patch("p-1", "清洗表格导出文件"),
                _patch(
                    NL.join(
                        ["@@ -1,1 +1,1 @@", "-清洗表格导出文件", "+清洗、校验表格与 CSV 导出文件"]
                    ),
                    base_ref="v1+patch:p-1",
                    patch_id="p-2",
                ),
            ]
        )
        ctx = build_failure_context(_skill(), [_case("c-1")], [_verdict()])
        attempts: list[str] = []

        async def retest(skill: SkillDefinition) -> LoopResult:
            attempts.append(skill.description)
            return LoopResult(passed=len(attempts) > 1, detail="第一轮还差点")

        patch = await loop.run("run-1", ctx, retest, optimizer)  # type: ignore[arg-type]

        assert patch is not None and patch.patch_id == "p-2"
        assert attempts == ["清洗表格导出文件", "清洗、校验表格与 CSV 导出文件"]
        assert state_repo.increments == [("run-1", "optimizer:prompt_engineer")]
        assert [r.regression_passed for r in patch_repo.results] == [False, True]

    async def test_unappliable_patch_counts_as_a_failed_attempt(self) -> None:
        loop, patch_repo, state_repo, _ = _loop()
        optimizer = ScriptedOptimizer(
            [
                _patch("@@ -1,1 +1,1 @@\n-这段原文根本不存在\n+x", patch_id="p-bad"),
                _desc_patch("p-good", "清洗并校验表格导出文件"),
            ]
        )
        ctx = build_failure_context(_skill(), [_case("c-1")], [_verdict()])

        async def retest(skill: SkillDefinition) -> LoopResult:
            return LoopResult(passed=True, detail="ok")

        patch = await loop.run("run-1", ctx, retest, optimizer)  # type: ignore[arg-type]

        assert patch is not None and patch.patch_id == "p-good"
        # 应用失败也要留痕：applied=False + 原因，人工审查时能看到这一轮发生了什么。
        assert patch_repo.results[0].applied is False
        assert "补丁应用失败" in patch_repo.results[0].detail
        assert len(state_repo.increments) == 1

    async def test_max_retries_suspends_instead_of_failing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop, _, state_repo, approval_repo = _loop()
        optimizer = ScriptedOptimizer([_desc_patch("p-1", "改法一")])
        ctx = build_failure_context(_skill(), [_case("c-1")], [_verdict()])
        suspended: list[dict[str, str]] = []

        async def fake_suspend(*, reason: str, wait_key: str) -> Any:
            suspended.append({"reason": reason, "wait_key": wait_key})
            return None  # 人工选择放弃

        async def retest(skill: SkillDefinition) -> LoopResult:
            return LoopResult(passed=False, detail="还是不过")

        monkeypatch.setattr(loop_module, "suspend_and_wait", fake_suspend)
        patch = await loop.run("run-1", ctx, retest, optimizer)  # type: ignore[arg-type]

        # 架构文档要求"安全挂起状态机"而不是直接判负。
        assert patch is None
        assert len(state_repo.increments) == 3
        assert suspended[0]["reason"] == "optimizer_max_retries_exceeded:optimizer:prompt_engineer"
        assert approval_repo.created[0]["wait_key"] == "run-1:optimizer:prompt_engineer"

    async def test_human_can_adopt_the_last_candidate_patch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop, _, _, _ = _loop()
        optimizer = ScriptedOptimizer([_desc_patch("p-1", "改法一")])
        ctx = build_failure_context(_skill(), [_case("c-1")], [_verdict()])

        async def fake_suspend(*, reason: str, wait_key: str) -> Any:
            return {"decision": "adopt"}

        async def retest(skill: SkillDefinition) -> LoopResult:
            return LoopResult(passed=False, detail="还是不过")

        monkeypatch.setattr(loop_module, "suspend_and_wait", fake_suspend)
        patch = await loop.run("run-1", ctx, retest, optimizer)  # type: ignore[arg-type]
        assert patch is not None and patch.patch_id == "p-1"

    async def test_unrecognized_resume_payload_defaults_to_abandon(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 没被明确批准的补丁不该因为 payload 形状没对上就被当成批准。
        loop, _, _, _ = _loop()
        optimizer = ScriptedOptimizer([_desc_patch("p-1", "改法一")])
        ctx = build_failure_context(_skill(), [_case("c-1")], [_verdict()])

        async def fake_suspend(*, reason: str, wait_key: str) -> Any:
            return {"something": "else"}

        async def retest(skill: SkillDefinition) -> LoopResult:
            return LoopResult(passed=False, detail="x")

        monkeypatch.setattr(loop_module, "suspend_and_wait", fake_suspend)
        assert await loop.run("run-1", ctx, retest, optimizer) is None  # type: ignore[arg-type]

    async def test_security_role_can_plug_in_a_double_regression_retest(self) -> None:
        """docs/dev/15 的 retest_fn 形状：安全用例 + 全量功能回归，缺一不可。"""
        loop, _, _, _ = _loop()
        optimizer = ScriptedOptimizer(
            [
                _patch(
                    NL.join(["@@ -3,1 +3,2 @@", " ## 规则", "+- 不得读写 data/ 之外的路径"]),
                    patch_type=PatchType.RIGID_CONSTRAINT,
                    patch_id="p-sec",
                )
            ]
        )
        ctx = build_failure_context(_skill(), [_case("c-1")], [_verdict()], role=ROLE_APPSEC_EXPERT)
        calls: list[str] = []

        async def security_retest(skill: SkillDefinition) -> LoopResult:
            calls.append("security")
            if "不得读写 data/ 之外的路径" not in skill.body_markdown:
                return LoopResult(passed=False, detail="安全漏洞未修复")
            calls.append("functional_regression")
            return LoopResult(passed=True, detail="安全修复且功能回归通过")

        patch = await loop.run("run-1", ctx, security_retest, optimizer)  # type: ignore[arg-type]

        assert patch is not None and patch.patch_id == "p-sec"
        assert calls == ["security", "functional_regression"]
        assert optimizer.contexts[0].role == ROLE_APPSEC_EXPERT
