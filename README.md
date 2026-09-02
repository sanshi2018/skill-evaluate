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

第 1 层"跨维度复用的公共智能体"已实现前两份（`docs/dev/06`~`07`）：

- `06` Generator Agent 与测试集生命周期：`src/skill_evaluate/agents/generator/`，
  三态生成（默认复用 / 手动强制重生 / 定向补盲区）、60/40 数据集划分、
  `skill-evaluate generate` CLI；配套的 `SKILL.md` 解析层落在
  `src/skill_evaluate/ingestion/`（最小实现，`docs/dev/12` 完善精度）。
- `07` Mini Agent 评审框架：`src/skill_evaluate/agents/mini/`，模板注册表 +
  首批 7 个审查模板，覆盖模块二全部、模块三/四/九各一至两项。
- 两者共用的底座：`src/skill_evaluate/agents/base.py`（`BaseLLMAgent`，统一
  LLM 调用 / 计量 / Langfuse 打点 / 结构化解析重试）与 `agents/llm.py`。

`docs/dev/08` 及之后（Judge/Optimizer/Validator 与各评测维度节点）尚未实现，
相关目录已预留骨架。已知的接口留白详见 `docs/dev/interfaces/`——尤其是
`06_llm_client_and_sampling.md`，其中记录了一处会直接影响 `08` 设计的约束
（新一代 Claude 模型已移除 `temperature`，"温度扰动多副本共识"需要改用别的
扰动维度）。

## 生成测试集

```bash
skill-evaluate generate --skill-path path/to/skill      # 默认复用，不调用 LLM
skill-evaluate generate --skill-path path/to/skill --force   # 手动强制重新出题
```

CI 默认路径**不带** `--force`。这不是建议而是约束：每次运行都重新出题会让分数
失去可比性，因此重生必须由人显式触发。

## LLM 访问：统一走 OpenRouter

全项目只有一条 LLM 出口：`langchain_openai.ChatOpenAI` -> OpenRouter
（`https://openrouter.ai/api/v1`，OpenAI 兼容协议），实现在
`src/skill_evaluate/agents/llm.py::OpenRouterLLMClient`。项目不直连任何厂商 SDK。

换模型 / 换厂商只改配置，不改代码——模型 ID 用 OpenRouter 的 `<厂商>/<模型>`
写法（注意版本号是点号）：

```bash
SKILLEVAL_LLM_API_KEY=sk-or-v1-...            # 或裸 OPENROUTER_API_KEY
SKILLEVAL_LLM_JUDGE_MODEL=anthropic/claude-sonnet-5
SKILLEVAL_LLM_GENERATOR_MODEL=anthropic/claude-sonnet-5
SKILLEVAL_LLM_MINI_AGENT_MODEL=anthropic/claude-haiku-4.5
```

自建 OpenAI 兼容网关改 `SKILLEVAL_LLM_BASE_URL` 即可；`SKILLEVAL_LLM_PROVIDER`
只接受 `openrouter`，配成别的值会直接报错而不是静默降级。细节（结构化输出的
`response_format` 约束、采样参数门禁、跨模型泛化）见
`docs/dev/interfaces/06_llm_client_and_sampling.md`。

## 快速开始

```bash
pip install -e ".[dev]"
cp .env.example .env   # 按需修改：至少填 SKILLEVAL_LLM_API_KEY
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
