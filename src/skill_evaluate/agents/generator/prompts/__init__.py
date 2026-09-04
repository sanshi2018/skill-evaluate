"""Generator 的 Prompt 模板目录。

除了 `.jinja` 文件本身，这里还放**类别 → 模板**的注册表（`registry.py`）。做成
包（而不是一个纯资源目录）是 docs/dev/13 第 3.1 节对 docs/dev/06 的正式修订：
原先 `agent.py` 里硬编码了 `positive` / `negative` 两套模板的字典，每新增一个
用例类别都要改 `agent.py`；改成注册表后，新增类别 = 新增一个 `.jinja` + 一次
`register_generation_template()`，与 docs/dev/07 的评审模板注册表同构。
"""
