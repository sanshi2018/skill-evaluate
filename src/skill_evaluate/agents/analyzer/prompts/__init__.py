"""Analyzer Agent 的 Prompt 模板目录（docs/dev/16）。

与 Generator 的模板目录同一套约定：模板用 Jinja、环境由
`agents/templating.py::build_prompt_env()` 统一构造（`StrictUndefined`，变量拼错
在渲染期就报错）。这里只有两份模板，且没有注册表——两个任务是 Analyzer 固定的
两步，不存在"后续文档追加一种抽取模板"的扩展场景（文档 18 追加的是**分级**子
任务，它会新增自己的模板文件并在 `service.py` 里加一个方法，不需要注册表）。
"""
