"""断言规格（docs/dev/02 第 10 节，对应架构文档模块三第 5 节 Validator Agent）。

docs/dev/10 在此追加了三个可选字段（`script_content` / `failure_reason` /
`created_at`），默认值保证 docs/dev/02 时期的构造方式仍然合法：

- `script_content`：真正要下发到沙箱执行的脚本正文。`script_path` 只说明"落到
  沙箱里的哪个路径"，不含内容；而 `HermesBackend` 必须把内容一起交给沙箱，
  否则沙箱侧无从得知要跑什么（docs/dev/10 第 4.2 节）。
- `failure_reason`：`strategy=NONE` 时说明原因（未要求断言 / 生成失败降级），
  报告侧据此区分"这条用例本来就不需要断言"与"断言生成失败了"。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from skill_evaluate.state.enums import AssertionStrategy


class AssertionSpec(BaseModel):
    assertion_id: str
    case_id: str
    strategy: AssertionStrategy
    template_ref: str | None = None  # 命中的 Git 断言库模板路径（形如 `templates/x.jinja@<sha>`）
    script_path: str | None = None  # 落盘的校验脚本路径（沙箱内）
    language: str = "python"

    # ---- docs/dev/10 追加字段（向后兼容，均有默认值）----
    script_content: str | None = None  # 下发沙箱执行的脚本正文；NONE 策略下为 None
    failure_reason: str | None = None  # strategy=NONE 的原因
    created_at: datetime | None = None

    @property
    def is_executable(self) -> bool:
        """是否真的有脚本可以下发到沙箱执行。

        `strategy != NONE` 不等于"有脚本"：模板渲染/脚本生成成功才有内容。执行侧
        统一按本属性过滤，避免把一个空 spec 交给沙箱后拿回一个无意义的失败断言。
        """
        return self.strategy is not AssertionStrategy.NONE and bool(self.script_content)


class AssertionResult(BaseModel):
    assertion_id: str
    exit_code: int
    stdout: str
    stderr: str
    passed: bool

    @classmethod
    def from_exit_code(
        cls, *, assertion_id: str, exit_code: int, stdout: str = "", stderr: str = ""
    ) -> AssertionResult:
        """`passed = (exit_code == 0)` 的唯一构造入口（docs/dev/10 第 4.3、6 节）。

        判定逻辑只写一次：Hook 端点、拉取兜底、单测三处都走这里，避免某一处写成
        `exit_code != 1` 之类的变体，让 `passed` 的口径在全局不一致。
        """
        return cls(
            assertion_id=assertion_id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            passed=exit_code == 0,
        )
