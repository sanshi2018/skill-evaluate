"""模块四的确定性探测工具（docs/dev/14 第 6、7 节）：**无 LLM、无容器调用**。

本文件只做三件事：

1. 把 `SkillDefinition.scripts` 翻译成"可以真的跑起来"的探测目标
   （`build_probe_targets()`：推断运行时、核对文件在不在盘上）；
2. 构造脏数据负载（`generate_dirty_payloads()`）；
3. 对子进程结果做确定性检查（流隔离、崩溃特征、输出体量）。

放在独立文件而不是揉进 `nodes.py`，与模块二的 `static_scan.py`、模块三的
`probe.py` 同一个理由：这些函数没有 IO、没有图状态依赖，CI 想把它们当 linter
单独调用是合理的；测试也能不搭图直接覆盖它们。
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from skill_evaluate.executors.script_sandbox import ProcessResult, infer_runtime, read_shebang
from skill_evaluate.state.skill import SkillDefinition, SkillScript

# --------------------------------------------------------------------------- #
# 1. 探测目标
# --------------------------------------------------------------------------- #


class ScriptProbeTarget(BaseModel):
    """一个脚本的探测参数（进图状态用，见 `state.py` 的说明）。

    `skip_reason` 不为空表示这个脚本**跑不起来**（文件不在盘上、或推断不出运行
    时），三条探测支路都会跳过它。它不是"通过"，而是"没测成"——收尾节点会把它
    如实写进 findings，理由与模块二对黄金盲测占用的处理一致：少做了一项检查，
    读报告的人有权知道。
    """

    path: str  # 相对 Skill 根目录，如 "scripts/parse_csv.py"
    language: str = ""
    image: str = ""
    interpreter: list[str] = Field(default_factory=list)
    # 三态，语义见 `SkillScript.is_mutating`：None = 无法判定，跳过幂等性探测。
    is_mutating: bool | None = None
    skip_reason: str | None = None

    @property
    def runnable(self) -> bool:
        return self.skip_reason is None

    def command(self, *args: str) -> list[str]:
        """容器内的完整命令。脚本路径用相对路径（工作区挂载在容器的 `/workspace`）。"""
        return [*self.interpreter, self.path, *args]


def build_probe_targets(
    skill: SkillDefinition, *, image_overrides: dict[str, str] | None = None
) -> list[ScriptProbeTarget]:
    """把 Skill 里登记的脚本翻译成探测目标。

    两处会产生 `skip_reason`：

    - **文件不在盘上**：`SkillDefinition` 可能是从库里读出来的（另一台机器上解析
      并入库），此时 `root_path` 指向的目录未必存在。判 PASS 等于用一份空清单
      "通过"了整个维度，所以必须显式暴露。
    - **推断不出运行时**：拿错解释器跑出来的失败会被误读成"脚本有缺陷"，而事实
      是我们根本没把它跑起来（见 `infer_runtime()` 的说明）。

    shebang 优先于扩展名，所以这里要读文件首行——这是本函数唯一的 IO。
    """
    root = Path(skill.root_path)
    targets: list[ScriptProbeTarget] = []
    for script in skill.scripts:
        file_path = root / script.path
        if not file_path.is_file():
            targets.append(
                ScriptProbeTarget(
                    path=script.path,
                    is_mutating=script.is_mutating,
                    skip_reason=(
                        f"脚本文件在评测机上不存在（{file_path}）："
                        "本维度是黑盒探测，必须能真的把脚本跑起来。"
                        "请确认 SkillDefinition.root_path 指向的目录已检出。"
                    ),
                )
            )
            continue

        runtime = infer_runtime(
            script.path, shebang=read_shebang(file_path), image_overrides=image_overrides
        )
        if runtime is None:
            targets.append(
                ScriptProbeTarget(
                    path=script.path,
                    is_mutating=script.is_mutating,
                    skip_reason=(
                        "无法从扩展名或 shebang 推断运行时，已跳过全部探测。"
                        "补一个 shebang（如 `#!/usr/bin/env python3`）即可让它进入探测范围。"
                    ),
                )
            )
            continue

        targets.append(
            ScriptProbeTarget(
                path=script.path,
                language=runtime.language,
                image=runtime.image,
                interpreter=list(runtime.interpreter),
                is_mutating=script.is_mutating,
            )
        )
    return targets


def is_mutating_script(script: SkillScript | ScriptProbeTarget) -> bool | None:
    """`SkillScript.is_mutating` 的读取入口（docs/dev/14 第 7 节的 `is_mutating_script()`）。

    做成函数而不是直接读字段，是为了给"标注来源"留一个唯一的收口：目前值全部
    来自 `ingestion/skill_loader.detect_mutating_script()` 的静态启发式；将来若
    docs/dev/16 的能力树能给出更准的答案，只改这一处即可。
    """
    return script.is_mutating


# --------------------------------------------------------------------------- #
# 2. 脏数据构造（docs/dev/14 第 6.1 节）
# --------------------------------------------------------------------------- #


class DirtyPayload(BaseModel):
    """一份脏数据负载。

    `arg_value` 是真正递给脚本的参数值：文件类模式给的是**工作区内的相对文件名**
    （脚本要能打开它），字面量类模式给的就是那串脏数据本身。
    """

    mode: str
    description: str  # 进报告与 LLM Prompt 的人类可读说明
    arg_value: str
    file_name: str | None = None  # 非空表示需要先把 `file_bytes` 写进工作区
    file_bytes: bytes | None = None

    model_config = {"arbitrary_types_allowed": True}


#: 预置的通用脏数据模式（docs/dev/14 第 6.1 节：确定性的组合测试，不调 Generator）。
#:
#: 为什么不为每个脚本定制生成：脏数据构造本质上是**格式层**的组合测试，与脚本的
#: 业务语义无关，用代码生成比调一次 LLM 更快、更稳、可复现。真正需要理解脚本语义
#: 才能构造的攻击性负载（命令注入等）留给 docs/dev/15 的 Attacker Agent——它会复用
#: 同一个 `ScriptSandboxRunner`，不必再造一套执行链路。
DIRTY_PAYLOAD_MODES: tuple[str, ...] = (
    "malformed_json",
    "missing_required_field",
    "non_utf8_bytes",
    "oversized_string",
)


def generate_dirty_payloads(
    target: ScriptProbeTarget,
    *,
    modes: list[str] | None = None,
    oversized_bytes: int = 64 * 1024,
) -> list[DirtyPayload]:
    """按模式表生成脏数据负载（docs/dev/14 第 6.1 节的 `generate_dirty_payload()` 复数版）。

    相对正文的一处收窄：正文的伪代码每个脚本只构造**一份**负载。实现改成"生成
    多份、全部跑一遍、只挑一条最有代表性的报错交给 LLM 复核"（挑选逻辑在
    `nodes.py::pick_review_candidate()`）。理由：单一模式打不中脚本的输入类型时
    （给一个只收数字参数的脚本喂坏 JSON 文件），拿回来的只是一句"文件不存在"，
    据此评价它的报错质量毫无意义；而多跑几次子进程的成本远低于一次 LLM 调用，
    所以"多试几种、只审一次"是这里性价比最高的组合。

    `target` 目前只用于让文件名带上脚本标识（同一工作区里多份负载不互相覆盖）。
    """
    enabled = list(modes) if modes else list(DIRTY_PAYLOAD_MODES)
    stem = Path(target.path).stem or "script"
    payloads: list[DirtyPayload] = []
    for mode in enabled:
        payload = _build_payload(mode, stem=stem, oversized_bytes=oversized_bytes)
        if payload is not None:
            payloads.append(payload)
    return payloads


def _build_payload(mode: str, *, stem: str, oversized_bytes: int) -> DirtyPayload | None:
    """构造单份负载；未知模式名返回 None（配置写错不该让整个维度崩掉）。"""
    if mode == "malformed_json":
        # 扩展名是 .csv、内容是残缺 JSON：同时覆盖"格式错乱"与"类型不匹配"两种
        # 错误——架构文档模块四点名的"把 JSON 传给需要 CSV 的接口"。
        return DirtyPayload(
            mode=mode,
            description="扩展名为 .csv、内容却是截断的 JSON（格式与扩展名双重不匹配）",
            arg_value=f"{stem}_dirty_malformed.csv",
            file_name=f"{stem}_dirty_malformed.csv",
            file_bytes=b'{"records": [{"id": 1, "name": "unterminated',
        )
    if mode == "missing_required_field":
        # 格式完全合法、只是缺了业务必填字段。它专门用来区分两类脚本：只做格式
        # 校验的会放行（然后在后面某处以一个莫名其妙的 KeyError 崩掉），做了字段
        # 校验的会给出一条能指名道姓说"缺哪个字段"的建设性报错。
        return DirtyPayload(
            mode=mode,
            description="格式合法但缺少必填字段的 CSV（只有表头、且表头缺列）",
            arg_value=f"{stem}_dirty_missing_field.csv",
            file_name=f"{stem}_dirty_missing_field.csv",
            file_bytes=b"unrelated_column\n\n",
        )
    if mode == "non_utf8_bytes":
        return DirtyPayload(
            mode=mode,
            description="非 UTF-8 字节流（解码必然失败）",
            arg_value=f"{stem}_dirty_binary.csv",
            file_name=f"{stem}_dirty_binary.csv",
            file_bytes=b"\xff\xfe\x00\x80\x81binary-garbage\x00\xfd",
        )
    if mode == "oversized_string":
        # 唯一一个不落文件、直接把脏数据放进命令行参数的模式：它测的是脚本对
        # **参数本身**的长度容忍度（有的脚本会把参数原样拼进 SQL 或日志）。
        return DirtyPayload(
            mode=mode,
            description=f"超长字面量参数（{oversized_bytes} 字节）",
            arg_value="A" * oversized_bytes,
        )
    return None


# --------------------------------------------------------------------------- #
# 3. 对子进程结果的确定性检查
# --------------------------------------------------------------------------- #

# 错误/异常堆栈的特征词。多语言混排：本维度面对的脚本可能是 Python/Node/Bash/Ruby
# 的任意一种，为每种语言分别判断的收益不足以抵消一张表变四张表的复杂度。
_ERROR_SIGNATURE_RE = re.compile(
    r"(?i)(traceback \(most recent call last\)|^\s*at [\w.$]+ \(.*:\d+:\d+\)"
    r"|\b\w*Error\b|\bException\b|\bpanic:|\bfatal\b|Segmentation fault"
    r"|command not found|No such file or directory)",
    re.MULTILINE,
)

# "未处理的崩溃"特征：比上面那张表严格得多，只认**真的没被接住**的堆栈/信号。
#
# 为什么要分成两张表：`_ERROR_SIGNATURE_RE` 用于"这段文字看起来像错误"（判流隔离
# 时用），命中一条格式良好的 `ValueError: 缺少列 id` 也算命中；而幂等性判定要回答
# 的是"第二次执行是不是直接崩了"——一条写得好的报错**恰恰是通过**的表现，用同一
# 张表会把所有能好好报错的脚本判成崩溃。
_UNHANDLED_CRASH_RE = re.compile(
    r"(?i)(traceback \(most recent call last\)"
    r"|\bunhandled (exception|rejection)\b"
    r"|^\s*at [\w.$]+ \(.*:\d+:\d+\)"
    r"|\bpanic:|Segmentation fault|\bcore dumped\b"
    r"|\bFileExistsError\b|\bEEXIST\b|already exists.*(Error|Exception)"
    r"|\bIntegrityError\b|duplicate key value violates)",
    re.MULTILINE,
)


def check_io_separation(result: ProcessResult) -> bool:
    """流隔离的启发式检查（docs/dev/14 第 6.2 节）。

    规则：`exit_code != 0` 时，stdout 里**不应**出现明显的错误/堆栈特征——那些
    属于 stderr。干净的 stdout 才能被 Agent 拿去做管道级联，混进错误信息会让
    下游解析器读到半截垃圾数据。

    这是一个**启发式补充信号，不是独立判定依据**（正文原话）：它与 Mini Agent 的
    `constructive_error` 语义审查一起出现在报告里，最终定性以后者为准。返回
    `True` 表示"没看出问题"，包括"这次执行成功了，无从判断"这种情况——本函数不
    区分"没问题"和"没测到"，那由调用方按 exit_code 自己决定怎么措辞。
    """
    if result.exit_code == 0 or not result.stdout:
        return True
    return _ERROR_SIGNATURE_RE.search(result.stdout) is None


def looks_like_unhandled_crash(stderr: str) -> bool:
    """第二次执行是否"直接崩了"（docs/dev/14 第 7 节的 `_looks_like_unhandled_crash()`）。

    只认裸堆栈与"状态已存在"类的原生异常（`FileExistsError` / `EEXIST` /
    唯一键冲突）。一条形如 `错误：输出目录已存在，请加 --force 覆盖` 的报错**不算
    崩溃**——那正是架构文档要的"安全处理了状态已存在的情况"。
    """
    return bool(stderr) and _UNHANDLED_CRASH_RE.search(stderr) is not None


def describe_invocation(target: ScriptProbeTarget, payload: DirtyPayload, *, flag: str) -> str:
    """给 `constructive_error` 模板的 `invocation` 变量：这次是怎么把脚本调起来的。

    模板要求裁判"引用报错原文里对应/缺失的片段"，而"缺了什么"取决于我们到底传了
    什么进去——只给一段报错、不给调用方式，裁判无从判断报错是否切题。
    """
    shown = payload.arg_value if len(payload.arg_value) <= 120 else f"{payload.arg_value[:120]}…"
    return (
        f"命令：{' '.join([*target.interpreter, target.path, flag, shown])}\n"
        f"脏数据模式：{payload.mode}——{payload.description}"
    )


def format_error_output(result: ProcessResult) -> str:
    """给 `constructive_error` 模板的 `error_output` 变量。

    stdout 与 stderr **都给、且标明各自来自哪个流**：模板要审的"建设性"只看内容，
    但报告读者需要能看出这条报错本来出现在哪个流里（那是流隔离那一项的证据）。
    """
    return (
        f"exit_code: {result.exit_code if not result.timed_out else '超时被杀'}\n"
        f"--- stderr ---\n{result.stderr or '(空)'}\n"
        f"--- stdout ---\n{result.stdout or '(空)'}"
    )


__all__ = [
    "DIRTY_PAYLOAD_MODES",
    "DirtyPayload",
    "ScriptProbeTarget",
    "build_probe_targets",
    "check_io_separation",
    "describe_invocation",
    "format_error_output",
    "generate_dirty_payloads",
    "is_mutating_script",
    "looks_like_unhandled_crash",
]
