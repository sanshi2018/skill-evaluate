"""沙箱环境指纹：探测、黄金基线读写与严格比对（docs/dev/21 第 4 节）。

架构文档的要求是"哪怕某个次要依赖升了一个小版本，也直接阻断"。因此比对是**逐键严格相等**：
不做语义版本号的"兼容范围"判断，不忽略多出来的键——"不允许静默漂移"正是这道门禁的价值，
任何宽容规则都会变成下一次事故复盘里"当时为什么没拦住"的答案。

黄金指纹 `golden_fingerprint.json` 纳入**本项目仓库**版本控制（它与本项目的基础镜像强绑定，
不属于外部工具箱仓库），首次生成与每次更新都是人工动作：

```bash
skill-evaluate preflight-fingerprint --output golden_fingerprint.json   # 在目标沙箱环境里探测
git diff golden_fingerprint.json                                        # 人工审核差异后提交
```
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field, ValidationError

from skill_evaluate.errors import ExecutorBackendError
from skill_evaluate.executors.hermes_backend import EnvironmentProbeResult

PROBE_SCRIPT_PATH = Path(__file__).parent / "env_fingerprint_probe.sh"

# 服务端二次过滤：即使探测脚本被人改坏、把密钥类变量也打了出来，也不让它进入指纹文件
# （指纹文件会被提交进仓库、写进日志与挂起说明）。
_SENSITIVE_ENV_NAME_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE)", re.IGNORECASE
)


class SandboxFingerprint(BaseModel):
    """一次沙箱环境探测的结构化结果。"""

    os_kernel: str
    # {"python": "Python 3.13.1", "node": "v22.1.0", ...}；工具不存在就没有这个键
    runtime_versions: dict[str, str] = Field(default_factory=dict)
    # 只快照非敏感的结构性 env（PATH 结构、locale 等），不含任何密钥。
    key_env_vars_snapshot: dict[str, str] = Field(default_factory=dict)
    core_package_hashes: dict[str, str] = Field(default_factory=dict)  # 关键依赖包清单的 sha256

    def digest(self) -> str:
        """规范化 JSON 的 sha256。

        用途：未配置 `PreflightSettings.sandbox_image_ref` 时充当"镜像标识"——指纹变了镜像必然
        变了，金丝雀的跳过判定据此知道该重新实跑。
        """
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@runtime_checkable
class EnvironmentProbeRunner(Protocol):
    """能在沙箱里跑探测脚本的执行后端（`HermesBackend` 实现了它）。"""

    async def run_environment_probe(
        self, *, script_content: str, timeout_s: int
    ) -> EnvironmentProbeResult: ...


def load_probe_script() -> str:
    return PROBE_SCRIPT_PATH.read_text(encoding="utf-8")


def sanitize_env_snapshot(env: dict[str, str]) -> dict[str, str]:
    return {name: value for name, value in env.items() if not _SENSITIVE_ENV_NAME_RE.search(name)}


def parse_probe_output(result: EnvironmentProbeResult) -> SandboxFingerprint:
    """把探测脚本的输出解析为指纹；脚本失败或输出不合法一律抛 `ExecutorBackendError`。

    归到"执行后端错误"而不是返回一个残缺指纹：残缺指纹与黄金指纹比对必然"不一致"，报告里
    会写成"环境漂移"，把排查方向引到镜像上，而真正坏的是探测通道本身。
    """
    if result.exit_code != 0:
        raise ExecutorBackendError(
            f"环境指纹探测脚本失败（exit_code={result.exit_code}）：{result.stderr[:500]}"
        )
    try:
        payload = json.loads(
            result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        )
        fingerprint = SandboxFingerprint.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, IndexError) as exc:
        raise ExecutorBackendError(
            f"环境指纹探测输出不是合法的指纹 JSON：{exc}；stdout={result.stdout[:500]!r}"
        ) from exc
    fingerprint.key_env_vars_snapshot = sanitize_env_snapshot(fingerprint.key_env_vars_snapshot)
    return fingerprint


async def probe_current_fingerprint(
    runner: EnvironmentProbeRunner, *, timeout_s: int
) -> SandboxFingerprint:
    """通过执行后端在沙箱内运行固定探测脚本，收集当前环境指纹（不涉及被测 Skill）。"""
    result = await runner.run_environment_probe(
        script_content=load_probe_script(), timeout_s=timeout_s
    )
    return parse_probe_output(result)


def load_golden_fingerprint(path: Path | str) -> SandboxFingerprint | None:
    """读取仓库里的黄金指纹；文件不存在返回 None（由调用方决定这是否算门禁失败）。

    文件存在但内容非法时抛 `ValueError`：一份被改坏的基线不能被当成"没有基线"悄悄跳过。
    """
    file = Path(path)
    if not file.is_file():
        return None
    try:
        return SandboxFingerprint.model_validate_json(file.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise ValueError(f"黄金指纹文件 {file} 内容非法：{exc}") from exc


def dump_fingerprint(fingerprint: SandboxFingerprint) -> str:
    """稳定格式（键排序、缩进、末尾换行）：黄金指纹要进代码评审，diff 必须只反映真实变化。"""
    return (
        json.dumps(
            fingerprint.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False
        )
        + "\n"
    )


def diff_fingerprint(current: SandboxFingerprint, golden: SandboxFingerprint) -> list[str]:
    """逐键严格比对，返回人类可读的差异清单（空列表 = 一致）。"""
    mismatches: list[str] = []
    if current.os_kernel != golden.os_kernel:
        mismatches.append(f"os_kernel: 期望 {golden.os_kernel!r}，实际 {current.os_kernel!r}")
    for field_name in ("runtime_versions", "key_env_vars_snapshot", "core_package_hashes"):
        mismatches.extend(
            _diff_mapping(field_name, getattr(current, field_name), getattr(golden, field_name))
        )
    return mismatches


def _diff_mapping(name: str, current: dict[str, str], golden: dict[str, str]) -> list[str]:
    out: list[str] = []
    for key in sorted(golden.keys() | current.keys()):
        if key not in current:
            out.append(f"{name}.{key}: 黄金指纹中存在，当前环境缺失（期望 {golden[key]!r}）")
        elif key not in golden:
            out.append(f"{name}.{key}: 当前环境多出未登记的项 {current[key]!r}")
        elif current[key] != golden[key]:
            out.append(f"{name}.{key}: 期望 {golden[key]!r}，实际 {current[key]!r}")
    return out


__all__ = [
    "PROBE_SCRIPT_PATH",
    "EnvironmentProbeRunner",
    "SandboxFingerprint",
    "diff_fingerprint",
    "dump_fingerprint",
    "load_golden_fingerprint",
    "load_probe_script",
    "parse_probe_output",
    "probe_current_fingerprint",
    "sanitize_env_snapshot",
]
