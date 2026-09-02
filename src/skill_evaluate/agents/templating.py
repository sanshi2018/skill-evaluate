"""Agent Prompt 模板的 Jinja 环境构造（docs/dev/06、07 共用）。

统一在此构造是为了让所有 Agent 的模板行为一致：关闭 HTML 自动转义（Prompt 是
纯文本，转义会把 `<` `&` 写坏）、保留换行语义（`keep_trailing_newline`）、
未定义变量直接报错而不是静默渲染成空字符串——模板变量拼错时应当在开发期立刻
暴露，而不是把一个缺了半截的 Prompt 发给模型。
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined


def build_prompt_env(template_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=False,  # Prompt 是纯文本，HTML 转义会把 < & 写坏
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
