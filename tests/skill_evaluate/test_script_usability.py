"""docs/dev/14：模块四——脚本接口的智能体易用性黑盒探测。

覆盖：运行时推断（扩展名/shebang/镜像覆盖/未知类型）、突变脚本启发式标注、脏数据
模式构造、流隔离与崩溃特征检查、`ProcessResult` 的截断前字节记账、五条量化规则、
六个节点的行为（挂起致命、--help 空输出不调 LLM、脏数据全收判失败、幂等性只在基准
调用成功时才判、输出体量告警、黄金盲测跳过、共识未达成挂起）、报告口径（只有挂起与
幂等性崩溃阻断）、以及子图结构。

全部用替身注入：不起容器、不碰数据库、不发真实请求。
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from skill_evaluate.agents.judge.golden_injector import golden_subject_id
from skill_evaluate.agents.judge.rules import get_rule
from skill_evaluate.config import ScriptUsabilitySettings
from skill_evaluate.errors import (
    ConfigurationError,
    ExecutorBackendError,
    PersistenceError,
    PipelineSuspended,
)
from skill_evaluate.executors.script_sandbox import (
    DockerScriptSandboxRunner,
    ProcessResult,
    infer_runtime,
    infer_runtime_image,
    interpreter_for,
)
from skill_evaluate.ingestion.skill_loader import detect_mutating_script, load_skill
from skill_evaluate.nodes.script_usability import (
    DIMENSION,
    DIRTY_PAYLOAD_MODES,
    NODE_NAMES,
    ScriptProbeTarget,
    ScriptUsabilityDeps,
    ScriptUsabilityPipeline,
    build_probe_targets,
    build_script_usability_subgraph,
    check_io_separation,
    generate_dirty_payloads,
    looks_like_unhandled_crash,
    rules,
)
from skill_evaluate.nodes.script_usability.graph import INTERRUPT_BEFORE_NODES
from skill_evaluate.nodes.script_usability.nodes import (
    BENIGN_SAMPLE_NAME,
    BLOCKING_PREFIX,
    FATAL_PREFIX,
    INFO_PREFIX,
    WARNING_PREFIX,
)
from skill_evaluate.nodes.script_usability.state import (
    KEY_ERROR_OUTCOMES,
    KEY_HARD_FAILURE_FINDINGS,
    KEY_HELP_OUTCOMES,
    KEY_IDEMPOTENCY_FINDINGS,
    KEY_PREPARE_FINDINGS,
    KEY_SANDBOX_UNAVAILABLE,
    KEY_TARGETS,
)
from skill_evaluate.state.enums import Criticality, ExecutorBackendType, JudgeVerdictStatus
from skill_evaluate.state.judge import ConsensusResult, JudgeVerdict
from skill_evaluate.state.skill import SkillDefinition, SkillScript

SKILL_ID = "csv-cleaner"
RUN_ID = "run-1"
VERSION_REF = "v1"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def _skill(root: Path, scripts: list[SkillScript] | None = None) -> SkillDefinition:
    return SkillDefinition(
        skill_id=SKILL_ID,
        version_ref=VERSION_REF,
        root_path=str(root),
        description="清洗并校验 CSV 导出文件",
        body_markdown="# CSV Cleaner\n",
        line_count=1,
        token_count=10,
        scripts=scripts or [],
    )


def _write_script(root: Path, rel: str, content: str = "print('hi')\n") -> SkillScript:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return SkillScript(path=rel, exposed_tool_name=Path(rel).stem)


def _result(
    *,
    exit_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    stdout_bytes: int | None = None,
    stderr_bytes: int | None = None,
) -> ProcessResult:
    return ProcessResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_ms=5,
        stdout_bytes=len(stdout.encode()) if stdout_bytes is None else stdout_bytes,
        stderr_bytes=len(stderr.encode()) if stderr_bytes is None else stderr_bytes,
    )


def _verdict(
    status: JudgeVerdictStatus, *, subject_id: str = SKILL_ID, reasoning: str = "原文片段：……"
) -> JudgeVerdict:
    return JudgeVerdict(
        verdict_id=f"v-{subject_id}-{status.value}",
        subject_id=subject_id,
        status=status,
        reasoning=reasoning + "补" * 300,
        temperature=0.1,
        model="anthropic/claude-haiku-4.5",
        created_at=datetime.now(UTC),
    )


class FakeSkillRepo:
    def __init__(self, skill: SkillDefinition | None) -> None:
        self.skill = skill

    async def get(self, skill_id: str, version_ref: str) -> SkillDefinition | None:
        return self.skill


class FakeReporter:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record_dimension_result(self, **kwargs: Any) -> None:
        self.recorded.append(kwargs)


class FakeJudgeRepo:
    def __init__(self) -> None:
        self.saved: list[JudgeVerdict] = []

    async def save_verdict(self, verdict: JudgeVerdict) -> None:
        self.saved.append(verdict)


class FakeJudge:
    """替代 `JudgeAgent`：量化判定走真实规则表，裁量判定按模板 key 回放。"""

    def __init__(self, reviews: dict[str, JudgeVerdict | ConsensusResult] | None = None) -> None:
        self.reviews = reviews or {}
        self.review_calls: list[dict[str, Any]] = []
        self.quantitative_calls: list[dict[str, Any]] = []

    def quantitative_verdict(
        self, subject_id: str, rule_name: str, inputs: dict[str, Any]
    ) -> JudgeVerdict:
        self.quantitative_calls.append(
            {"subject_id": subject_id, "rule_name": rule_name, "inputs": inputs}
        )
        status = get_rule(rule_name)(inputs)
        return JudgeVerdict(
            verdict_id=f"q-{rule_name}-{subject_id}",
            subject_id=subject_id,
            status=status,
            reasoning=f"rule {rule_name}",
            temperature=0.0,
            model=f"rule:{rule_name}",
            created_at=datetime.now(UTC),
        )

    async def judgmental_verdict(
        self,
        subject_id: str,
        template_key: str,
        content: dict[str, str],
        criticality: Criticality,
    ) -> JudgeVerdict | ConsensusResult:
        self.review_calls.append(
            {
                "subject_id": subject_id,
                "template_key": template_key,
                "content": content,
                "criticality": criticality,
            }
        )
        return self.reviews.get(
            template_key, _verdict(JudgeVerdictStatus.PASS, subject_id=subject_id)
        )


class FakeSandbox:
    """替代 `ScriptSandboxRunner`：按"命令里出现的关键字"回放结果。

    按关键字而不是按调用序号匹配：三条探测支路是并行的，序号在不同运行里不稳定，
    照序号断言的测试会莫名其妙地闪断。
    """

    def __init__(
        self,
        *,
        default: ProcessResult | None = None,
        by_keyword: dict[str, ProcessResult] | None = None,
        sequence: dict[str, list[ProcessResult]] | None = None,
        available: bool = True,
    ) -> None:
        self.default = default or _result()
        self.by_keyword = by_keyword or {}
        self.sequence = sequence or {}
        self.available = available
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        *,
        image: str,
        command: list[str],
        stdin_data: bytes | None = None,
        cwd: str | None = None,
        timeout_s: int = 10,
    ) -> ProcessResult:
        self.calls.append(
            {
                "image": image,
                "command": command,
                "stdin_data": stdin_data,
                "cwd": cwd,
                "timeout_s": timeout_s,
            }
        )
        joined = " ".join(command)
        for keyword, queue in self.sequence.items():
            if keyword in joined and queue:
                return queue.pop(0)
        for keyword, result in self.by_keyword.items():
            if keyword in joined:
                return result
        return self.default

    async def is_available(self) -> bool:
        return self.available


def _deps(
    skill: SkillDefinition | None,
    *,
    sandbox: FakeSandbox | None = None,
    judge: FakeJudge | None = None,
    reporter: FakeReporter | None = None,
    settings: ScriptUsabilitySettings | None = None,
) -> ScriptUsabilityDeps:
    return ScriptUsabilityDeps(
        sandbox_runner=sandbox or FakeSandbox(),  # type: ignore[arg-type]
        judge_agent=judge or FakeJudge(),  # type: ignore[arg-type]
        report_generator=reporter or FakeReporter(),  # type: ignore[arg-type]
        skill_repository=FakeSkillRepo(skill),  # type: ignore[arg-type]
        judge_repository=FakeJudgeRepo(),  # type: ignore[arg-type]
        usability_settings=settings or ScriptUsabilitySettings(),
    )


def _state(**extra: Any) -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "skill_id": SKILL_ID,
        "skill_version_ref": VERSION_REF,
        **extra,
    }


# --------------------------------------------------------------------------- #
# 1. 运行时推断
# --------------------------------------------------------------------------- #


class 运行时推断Tests:
    def test_按扩展名推断镜像与解释器(self) -> None:
        runtime = infer_runtime("scripts/parse.py")
        assert runtime is not None
        assert runtime.language == "python"
        assert runtime.image == "python:3.13-slim"
        assert runtime.command_for("scripts/parse.py", "--help") == [
            "python",
            "scripts/parse.py",
            "--help",
        ]

    def test_shebang_优先于扩展名(self) -> None:
        # `.sh` 后缀 + python shebang：按后缀会用 bash 跑，必然语法错误，
        # 那会被误判成"脚本有缺陷"，而事实是我们没把它跑起来。
        runtime = infer_runtime("scripts/tool.sh", shebang="#!/usr/bin/env python3")
        assert runtime is not None
        assert runtime.language == "python"

    def test_typescript_带类型剥离开关(self) -> None:
        assert interpreter_for("scripts/x.ts") == ["node", "--experimental-strip-types"]

    def test_未知类型返回None而不是猜一个(self) -> None:
        assert infer_runtime("scripts/tool.exe") is None
        assert infer_runtime_image("scripts/tool.exe") is None
        assert interpreter_for("scripts/tool.exe") is None

    def test_镜像覆盖按语言生效(self) -> None:
        runtime = infer_runtime(
            "scripts/parse.py", image_overrides={"python": "registry.internal/python:3.13"}
        )
        assert runtime is not None
        assert runtime.image == "registry.internal/python:3.13"
        assert runtime.interpreter == ["python"]  # 只换镜像，不动解释器


# --------------------------------------------------------------------------- #
# 2. 突变脚本启发式（docs/dev/14 第 7 节，落在 skill_loader）
# --------------------------------------------------------------------------- #


class 突变脚本标注Tests:
    def test_python_写文件判定为突变(self) -> None:
        assert detect_mutating_script("with open(p, 'w') as f:\n    f.write(x)\n", ".py") is True

    def test_只读脚本判定为非突变(self) -> None:
        assert detect_mutating_script("import sys\nprint(sys.argv)\n", ".py") is False

    def test_不在覆盖范围内的语言返回None(self) -> None:
        # None 与 False 必须区分：None 是"没扫出来"，会跳过幂等性测试并提示人工确认；
        # 合并成布尔会让"没扫出来"被当成"确认安全"。
        assert detect_mutating_script("whatever", ".exe") is None
        assert detect_mutating_script("", ".py") is None

    def test_shell_重定向与rm判定为突变(self) -> None:
        assert detect_mutating_script("echo x > out.txt\n", ".sh") is True
        assert detect_mutating_script("rm -rf build\n", ".sh") is True
        assert detect_mutating_script("cat input.txt | grep x\n", ".sh") is False

    def test_loader_填充is_mutating字段(self, tmp_path: Path) -> None:
        (tmp_path / "SKILL.md").write_text(
            "---\nname: demo\ndescription: 演示\n---\n\n正文\n", encoding="utf-8"
        )
        _write_script(tmp_path, "scripts/writer.py", "open('o.txt', 'w').write('x')\n")
        _write_script(tmp_path, "scripts/reader.py", "print(open('i.txt').read())\n")

        skill = load_skill(tmp_path)
        by_path = {s.path: s for s in skill.scripts}
        assert by_path["scripts/writer.py"].is_mutating is True
        assert by_path["scripts/reader.py"].is_mutating is False
        # `--help` 支持与否是黑盒探测结论，静态解析保持"未知"
        assert by_path["scripts/reader.py"].supports_help_flag is None


# --------------------------------------------------------------------------- #
# 3. 探测目标与脏数据构造
# --------------------------------------------------------------------------- #


class 探测目标Tests:
    def test_文件不在盘上时标记跳过而不是静默通过(self, tmp_path: Path) -> None:
        skill = _skill(tmp_path, [SkillScript(path="scripts/missing.py")])
        targets = build_probe_targets(skill)
        assert len(targets) == 1
        assert targets[0].runnable is False
        assert "不存在" in (targets[0].skip_reason or "")

    def test_无法推断运行时的脚本标记跳过(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/tool.bin", "binary-ish")
        skill = _skill(tmp_path, [script])
        targets = build_probe_targets(skill)
        assert targets[0].runnable is False
        assert "推断运行时" in (targets[0].skip_reason or "")

    def test_可运行脚本带上镜像解释器与突变标注(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/parse.py")
        script = script.model_copy(update={"is_mutating": True})
        targets = build_probe_targets(_skill(tmp_path, [script]))
        assert targets[0].runnable
        assert targets[0].image == "python:3.13-slim"
        assert targets[0].is_mutating is True
        assert targets[0].command("--help") == ["python", "scripts/parse.py", "--help"]


class 脏数据构造Tests:
    def test_默认生成全部预置模式(self) -> None:
        payloads = generate_dirty_payloads(ScriptProbeTarget(path="scripts/parse.py"))
        assert [p.mode for p in payloads] == list(DIRTY_PAYLOAD_MODES)

    def test_文件类模式带文件名与字节_字面量模式不带(self) -> None:
        payloads = {p.mode: p for p in generate_dirty_payloads(ScriptProbeTarget(path="s/p.py"))}
        assert (
            payloads["non_utf8_bytes"].file_bytes == b"\xff\xfe\x00\x80\x81binary-garbage\x00\xfd"
        )
        assert payloads["oversized_string"].file_name is None

    def test_只启用指定模式(self) -> None:
        payloads = generate_dirty_payloads(
            ScriptProbeTarget(path="s/p.py"), modes=["malformed_json"]
        )
        assert [p.mode for p in payloads] == ["malformed_json"]

    def test_未知模式名被忽略而不是抛异常(self) -> None:
        # 配置写错不该让整个维度崩掉。
        assert generate_dirty_payloads(ScriptProbeTarget(path="s/p.py"), modes=["nope"]) == []


# --------------------------------------------------------------------------- #
# 4. 确定性检查与 ProcessResult 记账
# --------------------------------------------------------------------------- #


class 确定性检查Tests:
    def test_流隔离_失败时stdout出现堆栈判不通过(self) -> None:
        result = _result(exit_code=1, stdout="Traceback (most recent call last):\n  ...")
        assert check_io_separation(result) is False

    def test_流隔离_成功执行一律视为无从判断(self) -> None:
        assert check_io_separation(_result(exit_code=0, stdout="Error-like text")) is True

    def test_建设性报错不算未处理崩溃(self) -> None:
        # 这条是本维度最容易误报的地方：一条写得好的报错恰恰是"妥善处理"的表现。
        assert looks_like_unhandled_crash("错误：输出目录已存在，请加 --force 覆盖") is False
        assert looks_like_unhandled_crash("Traceback (most recent call last):") is True
        assert looks_like_unhandled_crash("FileExistsError: [Errno 17]") is True

    def test_截断前的原始字节数被如实记录(self) -> None:
        big = b"x" * (40 * 1024)
        result = DockerScriptSandboxRunner._build_result(
            stdout_bytes=big, stderr_bytes=b"", exit_code=0, timed_out=False, duration_ms=1
        )
        # 我们自己会把字段截到 32KB，但记账必须记脚本真实吐出的量——否则
        # "脚本没有自己截断"这件事会被我们的截断掩盖掉。
        assert result.stdout_bytes == 40 * 1024
        assert result.truncated is True
        assert len(result.stdout.encode()) <= 32 * 1024

    async def test_容器运行时缺失时报错而不是退化到宿主机执行(self) -> None:
        # 被测脚本是外部输入，探测还会故意给它喂脏数据：宿主机直接执行等于让一份
        # 未经审查的脚本以评测进程的权限跑起来。因此这里必须是报错，不是降级。
        runner = DockerScriptSandboxRunner(
            ScriptUsabilitySettings(docker_binary="skilleval-no-such-container-runtime")
        )
        assert await runner.is_available() is False
        with pytest.raises(ExecutorBackendError, match="拒绝退化到宿主机"):
            await runner.run(image="python:3.13-slim", command=["python", "-c", "print(1)"])

    def test_docker参数满足沙箱安全约定(self) -> None:
        runner = DockerScriptSandboxRunner(ScriptUsabilitySettings())
        argv = runner._docker_argv(
            image="python:3.13-slim", command=["python", "a.py"], cwd=".", container_name="c1"
        )
        assert "--rm" in argv  # Ephemeral
        assert "--network=none" in argv  # 默认无出站网络
        assert "-i" in argv and "-t" not in argv  # 有 stdin、无 TTY
        assert any(a.startswith("--memory=") for a in argv)
        assert argv[-2:] == ["python", "a.py"]


# --------------------------------------------------------------------------- #
# 5. 量化规则
# --------------------------------------------------------------------------- #


class 量化规则Tests:
    def test_挂起判失败_非零退出码不判失败(self) -> None:
        rule = get_rule(rules.RULE_NON_INTERACTIVE)
        assert rule({rules.KEY_TIMED_OUT: True}) is JudgeVerdictStatus.FAIL
        # 缺参数时以非 0 退出正是期望行为，不能算失败。
        assert rule({rules.KEY_TIMED_OUT: False, rules.KEY_EXIT_CODE: 2}) is JudgeVerdictStatus.PASS

    def test_help只要有输出就算响应(self) -> None:
        rule = get_rule(rules.RULE_HELP_RESPONSIVE)
        assert (
            rule({rules.KEY_HELP_OUTPUT_CHARS: 120, rules.KEY_EXIT_CODE: 2})
            is JudgeVerdictStatus.PASS
        )
        assert rule({rules.KEY_HELP_OUTPUT_CHARS: 0}) is JudgeVerdictStatus.FAIL

    def test_脏数据至少被拒一次即通过_一次都没有判失败(self) -> None:
        rule = get_rule(rules.RULE_REJECTS_DIRTY_INPUT)
        assert (
            rule({rules.KEY_MODE_COUNT: 4, rules.KEY_REJECTED_COUNT: 1}) is JudgeVerdictStatus.PASS
        )
        assert (
            rule({rules.KEY_MODE_COUNT: 4, rules.KEY_REJECTED_COUNT: 0}) is JudgeVerdictStatus.FAIL
        )
        # 一份负载都没构造出来 ≠ 通过。
        assert (
            rule({rules.KEY_MODE_COUNT: 0, rules.KEY_REJECTED_COUNT: 0})
            is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        )

    def test_幂等性只看崩溃特征不看裸退出码(self) -> None:
        rule = get_rule(rules.RULE_IDEMPOTENT)
        assert rule({rules.KEY_CRASHED: False, rules.KEY_EXIT_CODE: 1}) is JudgeVerdictStatus.PASS
        assert rule({rules.KEY_CRASHED: True}) is JudgeVerdictStatus.FAIL

    def test_输出体量超限判失败(self) -> None:
        rule = get_rule(rules.RULE_OUTPUT_BOUNDED)
        assert (
            rule({rules.KEY_OUTPUT_BYTES: 100, rules.KEY_WARN_BYTES: 1000})
            is JudgeVerdictStatus.PASS
        )
        assert (
            rule({rules.KEY_OUTPUT_BYTES: 5000, rules.KEY_WARN_BYTES: 1000})
            is JudgeVerdictStatus.FAIL
        )


# --------------------------------------------------------------------------- #
# 6. 节点行为
# --------------------------------------------------------------------------- #


class 准备节点Tests:
    async def test_列出脚本并推断运行时(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/parse.py")
        pipeline = ScriptUsabilityPipeline(_deps(_skill(tmp_path, [script])))
        out = await pipeline.prepare_scripts(_state())  # type: ignore[arg-type]
        assert [t["path"] for t in out[KEY_TARGETS]] == ["scripts/parse.py"]  # type: ignore[index]
        assert out[KEY_SANDBOX_UNAVAILABLE] is None

    async def test_沙箱不可用时整维度降级而不是判通过(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/parse.py")
        deps = _deps(_skill(tmp_path, [script]), sandbox=FakeSandbox(available=False))
        pipeline = ScriptUsabilityPipeline(deps)
        out = await pipeline.prepare_scripts(_state())  # type: ignore[arg-type]
        assert "不等于通过" in str(out[KEY_SANDBOX_UNAVAILABLE])

    async def test_没有脚本时不探沙箱(self, tmp_path: Path) -> None:
        sandbox = FakeSandbox(available=False)
        pipeline = ScriptUsabilityPipeline(_deps(_skill(tmp_path), sandbox=sandbox))
        out = await pipeline.prepare_scripts(_state())  # type: ignore[arg-type]
        assert out[KEY_TARGETS] == []
        assert out[KEY_SANDBOX_UNAVAILABLE] is None

    async def test_Skill缺失时报错而不是空跑(self, tmp_path: Path) -> None:
        pipeline = ScriptUsabilityPipeline(_deps(None))
        with pytest.raises(PersistenceError):
            await pipeline.prepare_scripts(_state())  # type: ignore[arg-type]


class 挂起探测Tests:
    async def test_挂起记为致命并落库判定(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/parse.py")
        sandbox = FakeSandbox(default=_result(exit_code=None, timed_out=True))
        judge = FakeJudge()
        deps = _deps(_skill(tmp_path, [script]), sandbox=sandbox, judge=judge)
        pipeline = ScriptUsabilityPipeline(deps)
        targets = (await pipeline.prepare_scripts(_state()))[KEY_TARGETS]  # type: ignore[arg-type]

        out = await pipeline.hard_failure_probing(_state(**{KEY_TARGETS: targets}))  # type: ignore[arg-type]
        findings = out[KEY_HARD_FAILURE_FINDINGS]
        assert len(findings) == 1 and str(findings[0]).startswith(FATAL_PREFIX)  # type: ignore[index]
        assert deps.judge_repository.saved  # type: ignore[union-attr]
        # 探测本身：不传参数、空 stdin、用挂起专用短超时。
        call = sandbox.calls[0]
        assert call["command"] == ["python", "scripts/parse.py"]
        assert call["stdin_data"] == b""
        assert call["timeout_s"] == ScriptUsabilitySettings().hang_probe_timeout_s

    async def test_正常退出不产生发现(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/parse.py")
        deps = _deps(
            _skill(tmp_path, [script]),
            sandbox=FakeSandbox(default=_result(exit_code=2, stderr="usage: parse.py --input F")),
        )
        pipeline = ScriptUsabilityPipeline(deps)
        targets = (await pipeline.prepare_scripts(_state()))[KEY_TARGETS]  # type: ignore[arg-type]
        out = await pipeline.hard_failure_probing(_state(**{KEY_TARGETS: targets}))  # type: ignore[arg-type]
        assert out[KEY_HARD_FAILURE_FINDINGS] == []


class Help文档探测Tests:
    async def test_有输出时才调LLM并按模板变量传参(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/parse.py")
        judge = FakeJudge()
        deps = _deps(
            _skill(tmp_path, [script]),
            sandbox=FakeSandbox(default=_result(stdout="usage: parse.py --input FILE")),
            judge=judge,
        )
        pipeline = ScriptUsabilityPipeline(deps)
        targets = (await pipeline.prepare_scripts(_state()))[KEY_TARGETS]  # type: ignore[arg-type]

        await pipeline.self_learning_doc_test(_state(**{KEY_TARGETS: targets}))  # type: ignore[arg-type]
        call = judge.review_calls[0]
        assert call["template_key"] == "help_doc_quality"
        # 变量名以模板注册表的 required_variables 为准（script_path + help_output）。
        assert set(call["content"]) == {"script_path", "help_output"}
        assert call["criticality"] is Criticality.ROUTINE

    async def test_help无输出时直接判失败不调LLM(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/parse.py")
        judge = FakeJudge()
        deps = _deps(
            _skill(tmp_path, [script]),
            sandbox=FakeSandbox(default=_result(exit_code=1)),
            judge=judge,
        )
        pipeline = ScriptUsabilityPipeline(deps)
        targets = (await pipeline.prepare_scripts(_state()))[KEY_TARGETS]  # type: ignore[arg-type]

        out = await pipeline.self_learning_doc_test(_state(**{KEY_TARGETS: targets}))  # type: ignore[arg-type]
        assert judge.review_calls == []  # 空输入没有审查价值，不烧 Token
        outcome = out[KEY_HELP_OUTCOMES][0]  # type: ignore[index]
        assert outcome["status"] == JudgeVerdictStatus.FAIL
        assert "未响应 --help" in str(outcome["reasoning_excerpt"])

    async def test_help输出到stderr也接受(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/parse.py")
        judge = FakeJudge()
        deps = _deps(
            _skill(tmp_path, [script]),
            sandbox=FakeSandbox(default=_result(exit_code=2, stderr="usage: parse.py [-h]")),
            judge=judge,
        )
        pipeline = ScriptUsabilityPipeline(deps)
        targets = (await pipeline.prepare_scripts(_state()))[KEY_TARGETS]  # type: ignore[arg-type]
        await pipeline.self_learning_doc_test(_state(**{KEY_TARGETS: targets}))  # type: ignore[arg-type]
        assert judge.review_calls[0]["content"]["help_output"] == "usage: parse.py [-h]"

    async def test_黄金盲测占用时跳过而不是当成本脚本的结论(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/parse.py")
        judge = FakeJudge(
            reviews={
                "help_doc_quality": _verdict(
                    JudgeVerdictStatus.FAIL, subject_id=golden_subject_id("golden-1")
                )
            }
        )
        deps = _deps(
            _skill(tmp_path, [script]),
            sandbox=FakeSandbox(default=_result(stdout="usage")),
            judge=judge,
        )
        pipeline = ScriptUsabilityPipeline(deps)
        targets = (await pipeline.prepare_scripts(_state()))[KEY_TARGETS]  # type: ignore[arg-type]
        out = await pipeline.self_learning_doc_test(_state(**{KEY_TARGETS: targets}))  # type: ignore[arg-type]
        outcome = out[KEY_HELP_OUTCOMES][0]  # type: ignore[index]
        assert outcome["status"] is None
        assert "黄金基准盲测" in str(outcome["skipped_reason"])

    async def test_共识未达成时挂起而不是降级(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/parse.py")
        judge = FakeJudge(
            reviews={
                "help_doc_quality": ConsensusResult(
                    subject_id="script_help:scripts/parse.py",
                    verdicts=[],
                    consensus_reached=False,
                    final_status=JudgeVerdictStatus.NEEDS_HUMAN_REVIEW,
                )
            }
        )
        deps = _deps(
            _skill(tmp_path, [script]),
            sandbox=FakeSandbox(default=_result(stdout="usage")),
            judge=judge,
        )
        pipeline = ScriptUsabilityPipeline(deps)
        targets = (await pipeline.prepare_scripts(_state()))[KEY_TARGETS]  # type: ignore[arg-type]
        with pytest.raises(PipelineSuspended):
            await pipeline.self_learning_doc_test(_state(**{KEY_TARGETS: targets}))  # type: ignore[arg-type]


class 脏数据与流隔离Tests:
    async def test_逐模式执行并只审一条报错(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/parse.py")
        judge = FakeJudge()
        sandbox = FakeSandbox(
            default=_result(exit_code=1, stderr="错误：第 1 行不是合法 CSV，期望 3 列")
        )
        deps = _deps(_skill(tmp_path, [script]), sandbox=sandbox, judge=judge)
        pipeline = ScriptUsabilityPipeline(deps)
        targets = (await pipeline.prepare_scripts(_state()))[KEY_TARGETS]  # type: ignore[arg-type]

        out = await pipeline.constructive_error_and_io_separation_test(
            _state(**{KEY_TARGETS: targets})  # type: ignore[arg-type]
        )
        # 四种模式各跑一次子进程，但只买一次 LLM 审查。
        assert len(sandbox.calls) == len(DIRTY_PAYLOAD_MODES)
        assert len(judge.review_calls) == 1
        call = judge.review_calls[0]
        assert call["template_key"] == "constructive_error"
        assert set(call["content"]) == {"invocation", "error_output"}
        outcome = out[KEY_ERROR_OUTCOMES][0]  # type: ignore[index]
        assert "流隔离检查通过" in str(outcome["detail"])

    async def test_全部脏数据被静默接受时判失败且不调LLM(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/parse.py")
        judge = FakeJudge()
        deps = _deps(
            _skill(tmp_path, [script]),
            sandbox=FakeSandbox(default=_result(exit_code=0, stdout="done")),
            judge=judge,
        )
        pipeline = ScriptUsabilityPipeline(deps)
        targets = (await pipeline.prepare_scripts(_state()))[KEY_TARGETS]  # type: ignore[arg-type]
        out = await pipeline.constructive_error_and_io_separation_test(
            _state(**{KEY_TARGETS: targets})  # type: ignore[arg-type]
        )
        assert judge.review_calls == []
        outcome = out[KEY_ERROR_OUTCOMES][0]  # type: ignore[index]
        assert outcome["status"] == JudgeVerdictStatus.FAIL
        assert "无法察觉" in str(outcome["reasoning_excerpt"])

    async def test_流隔离异常写进detail但不改判定(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/parse.py")
        judge = FakeJudge()  # 语义审查默认 PASS
        deps = _deps(
            _skill(tmp_path, [script]),
            sandbox=FakeSandbox(
                default=_result(exit_code=1, stdout="Traceback (most recent call last):")
            ),
            judge=judge,
        )
        pipeline = ScriptUsabilityPipeline(deps)
        targets = (await pipeline.prepare_scripts(_state()))[KEY_TARGETS]  # type: ignore[arg-type]
        out = await pipeline.constructive_error_and_io_separation_test(
            _state(**{KEY_TARGETS: targets})  # type: ignore[arg-type]
        )
        outcome = out[KEY_ERROR_OUTCOMES][0]  # type: ignore[index]
        # docs/dev/14 第 6.2 节：流隔离只是补充信号，最终定性以语义审查为准。
        assert outcome["status"] == JudgeVerdictStatus.PASS
        assert "流隔离检查未通过" in str(outcome["detail"])


class 幂等性与输出体量Tests:
    async def _targets(self, pipeline: ScriptUsabilityPipeline) -> Any:
        return (await pipeline.prepare_scripts(_state()))[KEY_TARGETS]  # type: ignore[arg-type]

    async def test_突变脚本连续两次崩溃判阻断(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/writer.py", "open('o','w').write('x')\n")
        script = script.model_copy(update={"is_mutating": True})
        sandbox = FakeSandbox(
            sequence={
                "scripts/writer.py": [
                    _result(exit_code=0, stdout="ok"),
                    _result(exit_code=1, stderr="FileExistsError: [Errno 17] o"),
                ]
            }
        )
        deps = _deps(_skill(tmp_path, [script]), sandbox=sandbox)
        pipeline = ScriptUsabilityPipeline(deps)
        targets = await self._targets(pipeline)

        out = await pipeline.idempotency_and_safety_guards(_state(**{KEY_TARGETS: targets}))  # type: ignore[arg-type]
        findings = [str(f) for f in out[KEY_IDEMPOTENCY_FINDINGS]]  # type: ignore[index]
        assert any(f.startswith(BLOCKING_PREFIX) for f in findings)
        # 两次执行必须共用同一个工作区，否则"状态已存在"根本不会发生。
        assert sandbox.calls[0]["cwd"] == sandbox.calls[1]["cwd"]
        assert BENIGN_SAMPLE_NAME in " ".join(sandbox.calls[0]["command"])

    async def test_第二次以建设性报错退出不算崩溃(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/writer.py", "open('o','w').write('x')\n")
        script = script.model_copy(update={"is_mutating": True})
        sandbox = FakeSandbox(
            sequence={
                "scripts/writer.py": [
                    _result(exit_code=0),
                    _result(exit_code=1, stderr="错误：输出文件已存在，请加 --force 覆盖"),
                ]
            }
        )
        pipeline = ScriptUsabilityPipeline(_deps(_skill(tmp_path, [script]), sandbox=sandbox))
        targets = await self._targets(pipeline)
        out = await pipeline.idempotency_and_safety_guards(_state(**{KEY_TARGETS: targets}))  # type: ignore[arg-type]
        assert out[KEY_IDEMPOTENCY_FINDINGS] == []

    async def test_基准调用失败时跳过幂等性判定而不是误报(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/writer.py", "open('o','w').write('x')\n")
        script = script.model_copy(update={"is_mutating": True})
        sandbox = FakeSandbox(default=_result(exit_code=2, stderr="unrecognized arguments"))
        pipeline = ScriptUsabilityPipeline(_deps(_skill(tmp_path, [script]), sandbox=sandbox))
        targets = await self._targets(pipeline)
        out = await pipeline.idempotency_and_safety_guards(_state(**{KEY_TARGETS: targets}))  # type: ignore[arg-type]
        findings = [str(f) for f in out[KEY_IDEMPOTENCY_FINDINGS]]  # type: ignore[index]
        assert len(sandbox.calls) == 1  # 没有第二次执行
        assert any(
            f.startswith(INFO_PREFIX) and "无法构造有效的连续执行场景" in f for f in findings
        )

    async def test_无法判定是否突变时提示人工确认(self, tmp_path: Path) -> None:
        script = _write_script(tmp_path, "scripts/parse.py")  # is_mutating 默认 None
        pipeline = ScriptUsabilityPipeline(_deps(_skill(tmp_path, [script])))
        targets = await self._targets(pipeline)
        out = await pipeline.idempotency_and_safety_guards(_state(**{KEY_TARGETS: targets}))  # type: ignore[arg-type]
        findings = [str(f) for f in out[KEY_IDEMPOTENCY_FINDINGS]]  # type: ignore[index]
        assert any("建议人工确认" in f for f in findings)

    async def test_非突变脚本也做输出体量检查(self, tmp_path: Path) -> None:
        # docs/dev/14 正文只对突变脚本跑这一节点，非突变脚本因此永远测不到防刷屏。
        script = _write_script(tmp_path, "scripts/parse.py")
        script = script.model_copy(update={"is_mutating": False})
        sandbox = FakeSandbox(default=_result(exit_code=0, stdout="x", stdout_bytes=100_000))
        pipeline = ScriptUsabilityPipeline(_deps(_skill(tmp_path, [script]), sandbox=sandbox))
        targets = await self._targets(pipeline)
        out = await pipeline.idempotency_and_safety_guards(_state(**{KEY_TARGETS: targets}))  # type: ignore[arg-type]
        findings = [str(f) for f in out[KEY_IDEMPOTENCY_FINDINGS]]  # type: ignore[index]
        assert len(sandbox.calls) == 1
        assert any(f.startswith(WARNING_PREFIX) and "防刷屏" in f for f in findings)


# --------------------------------------------------------------------------- #
# 7. 报告聚合
# --------------------------------------------------------------------------- #


class 报告口径Tests:
    async def _finalize(self, state: dict[str, Any], reporter: FakeReporter) -> dict[str, Any]:
        pipeline = ScriptUsabilityPipeline(_deps(_skill(Path(".")), reporter=reporter))
        await pipeline.finalize_dimension_report(_state(**state))  # type: ignore[arg-type]
        return reporter.recorded[0]

    async def test_挂起阻断合并(self) -> None:
        reporter = FakeReporter()
        recorded = await self._finalize(
            {
                KEY_TARGETS: [ScriptProbeTarget(path="s.py", image="i").model_dump()],
                KEY_HARD_FAILURE_FINDINGS: [f"{FATAL_PREFIX} s.py 挂起"],
            },
            reporter,
        )
        assert recorded["blocking"] is True
        assert recorded["status"] is JudgeVerdictStatus.FAIL
        assert recorded["dimension"] == DIMENSION
        assert recorded["score"] is None

    async def test_幂等性崩溃阻断合并(self) -> None:
        reporter = FakeReporter()
        recorded = await self._finalize(
            {
                KEY_TARGETS: [ScriptProbeTarget(path="s.py", image="i").model_dump()],
                KEY_IDEMPOTENCY_FINDINGS: [f"{BLOCKING_PREFIX} s.py 第二次崩溃"],
            },
            reporter,
        )
        assert recorded["blocking"] is True

    async def test_主观审查失败只告警不阻断(self) -> None:
        reporter = FakeReporter()
        recorded = await self._finalize(
            {
                KEY_TARGETS: [ScriptProbeTarget(path="s.py", image="i").model_dump()],
                KEY_HELP_OUTCOMES: [
                    {
                        "script_path": "s.py",
                        "check": "help_doc_quality",
                        "status": JudgeVerdictStatus.FAIL,
                        "reasoning_excerpt": "没有给出调用示例",
                    }
                ],
            },
            reporter,
        )
        # 状态判 FAIL（报告里得看见问题），但不阻断合并——这是本维度的核心策略。
        assert recorded["status"] is JudgeVerdictStatus.FAIL
        assert recorded["blocking"] is False

    async def test_没有脚本时判通过并写明原因(self) -> None:
        reporter = FakeReporter()
        recorded = await self._finalize({KEY_TARGETS: []}, reporter)
        assert recorded["status"] is JudgeVerdictStatus.PASS
        assert any("未附带任何 scripts/" in f for f in recorded["findings"])

    async def test_沙箱不可用判需要人工复核而不是通过(self) -> None:
        reporter = FakeReporter()
        recorded = await self._finalize(
            {
                KEY_TARGETS: [ScriptProbeTarget(path="s.py", image="i").model_dump()],
                KEY_SANDBOX_UNAVAILABLE: "容器引擎未就绪",
            },
            reporter,
        )
        assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        assert recorded["blocking"] is False

    async def test_跳过的脚本进findings且判需要人工复核(self) -> None:
        reporter = FakeReporter()
        recorded = await self._finalize(
            {
                KEY_TARGETS: [
                    ScriptProbeTarget(path="s.bin", skip_reason="无法推断运行时").model_dump()
                ],
                KEY_PREPARE_FINDINGS: [f"{WARNING_PREFIX} s.bin：无法推断运行时"],
            },
            reporter,
        )
        assert recorded["status"] is JudgeVerdictStatus.NEEDS_HUMAN_REVIEW
        assert any("s.bin" in f for f in recorded["findings"])

    async def test_全部通过时判通过(self) -> None:
        reporter = FakeReporter()
        recorded = await self._finalize(
            {
                KEY_TARGETS: [ScriptProbeTarget(path="s.py", image="i").model_dump()],
                KEY_HELP_OUTCOMES: [
                    {
                        "script_path": "s.py",
                        "check": "help_doc_quality",
                        "status": JudgeVerdictStatus.PASS,
                    }
                ],
            },
            reporter,
        )
        assert recorded["status"] is JudgeVerdictStatus.PASS
        assert recorded["blocking"] is False


# --------------------------------------------------------------------------- #
# 8. 子图结构与装配期断言
# --------------------------------------------------------------------------- #


class 子图结构Tests:
    def test_六个节点与并行分叉汇合(self, tmp_path: Path) -> None:
        graph = build_script_usability_subgraph(_deps(_skill(tmp_path))).compile()
        drawn = graph.get_graph()
        node_ids = set(drawn.nodes)
        for name in NODE_NAMES.values():
            assert name in node_ids

        edges = {(e.source, e.target) for e in drawn.edges}
        for key in (
            "hard_failure_probing",
            "self_learning_doc_test",
            "constructive_error_and_io_separation_test",
        ):
            assert (NODE_NAMES["prepare_scripts"], NODE_NAMES[key]) in edges
            assert (NODE_NAMES[key], NODE_NAMES["idempotency_and_safety_guards"]) in edges
        assert (
            NODE_NAMES["idempotency_and_safety_guards"],
            NODE_NAMES["finalize_dimension_report"],
        ) in edges

    def test_没有人工挂起点(self) -> None:
        assert INTERRUPT_BEFORE_NODES == []

    def test_路由表被改成MINI时装配期报错(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skill_evaluate.executors import routing

        monkeypatch.setitem(routing.NODE_BACKEND_ROUTING, DIMENSION, ExecutorBackendType.MINI)
        with pytest.raises(ConfigurationError, match="黑盒探测"):
            ScriptUsabilityPipeline(_deps(None))
