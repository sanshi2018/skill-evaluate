# skill-evaluate

基于 LangGraph 的 Skill 评测 CI/CD 流水线。输入一份 `SKILL.md`（及其
`scripts/`、`references/`），输出可阻断合并请求的量化评测报告。

设计文档见 `docs/_5_关键架构.md`（架构原文）与 `docs/dev/`（按开发顺序拆分的
实施文档，`docs/dev/00_项目总览与路线图.md` 是入口）。

## 当前实现进度

第 0 层"工程地基"（`docs/dev/00`~`05`）已实现：

- `01` 项目脚手架：目录结构、依赖管理、配置系统、本地 Postgres+pgvector。
- `02` 核心状态模型：`src/skill_evaluate/state/`，全项目数据契约唯一来源。
- `03` 执行引擎适配层：`src/skill_evaluate/executors/`，`MiniAgentBackend` +
  `HermesBackend` + Hook 协议。
- `04` 持久化：`src/skill_evaluate/persistence/`，`PostgresSaver` 接线、业务表、
  挂起-唤醒机制。
- `05` 可观测性骨架：`src/skill_evaluate/observability/` + `src/skill_evaluate/api/`，
  `benchmark.json`/HTML 报告、Langfuse 双写、Hook HTTP 端点。

第 1 层起（`docs/dev/06` 及之后，Generator/Mini/Judge/Optimizer/Validator
Agent 与各评测维度节点）尚未实现，相关目录已预留骨架（见各 `__init__.py`
中的占位说明）。已知的接口留白详见 `docs/dev/interfaces/`。

## 快速开始

```bash
pip install -e ".[dev]"
cp .env.example .env   # 按需修改
docker compose -f docker-compose.dev.yml up -d
skill-evaluate db_init   # 建扩展 + 跑 Alembic 迁移 + 初始化 PostgresSaver
pytest
```

## 代码质量基线

```bash
ruff check src tests scripts
ruff format src tests scripts
mypy src
pytest
```
