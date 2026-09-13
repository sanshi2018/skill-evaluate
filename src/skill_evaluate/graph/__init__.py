"""主 DAG 拼装、GraphResumer、运行入口与 CI/CD 落地（docs/dev/24）。

- `state.py`：主图状态 schema（全部维度私有键的并集 + 编排层私有键）；
- `main.py`：`build_main_graph()`——节点分层、边、同步屏障、`SUSPENDABLE_NODES` 汇总；
- `nodes.py`：编排层节点（入口登记、收尾报告、补丁转 PR、数据飞轮归档）；
- `cold_suite.py`：Nightly COLD 用例回归节点；
- `patch_pr.py` / `git_ops.py`：补丁合成与 PR 提交；
- `resumer.py`：`CompiledGraphResumer`（interfaces/04）；
- `runner.py`：CLI / 巡检任务的进程级装配与退出码；
- `ci_support.py`：CI 里"哪些 Skill 变了 / 镜像是否变了"的解析。

**本包不在 `__init__` 里导入子模块**：`main.py` 会导入全部十个维度（连带 LLM 客户端、规则注册等
导入副作用），而 `ci_support.py` 这类纯函数要能在只 checkout 了代码的轻量 CI 步骤里使用。
接入说明见 docs/dev/interfaces/24_main_graph_and_ci_cd.md。
"""
