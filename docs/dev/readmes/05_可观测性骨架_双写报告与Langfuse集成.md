# 实现说明：05 可观测性骨架：双写报告与 Langfuse 集成

> 对应设计文档：`docs/dev/05_可观测性骨架_双写报告与Langfuse集成.md`
> 状态：已实现（人工审批端点留桩，待 `docs/dev/22` 接入）

## 交付了什么

### API 层：`src/skill_evaluate/api/`

| 文件 | 内容 | 对应设计文档章节 |
|---|---|---|
| `security.py` | `verify_hmac_signature()`（原文档命名 `verify_hermes_signature`，保留为别名） | 第 2.1 节 |
| `hooks_hermes.py` | `POST /hooks/hermes/{run_id}/{case_id}/{run_index}` | 第 2.2 节 |
| `hooks_approval.py` | `POST /hooks/approval/{run_id}/{node_name}` **桩**（501） | 第 6 节待接入项 |
| `app.py` | FastAPI app 工厂，挂载上述两个路由 + `/healthz` | 第 2 节 |

### 报告生成器：`src/skill_evaluate/observability/`

| 文件 | 内容 | 对应设计文档章节 |
|---|---|---|
| `report_schema.py` | `DimensionResult` / `BenchmarkReport`（含 `blocking` 属性） | 第 3.1 节 |
| `report_generator.py` | `ReportGenerator`：`record_dimension_result()` / `build()` / `to_json()` / `to_html()` | 第 3.2 节 |
| `templates/report.html.jinja` | HTML 报告模板 | 第 3.2 节 |
| `langfuse_adapter.py` | `LangfuseAdapter`：`start_run_trace()` / `log_agent_call()` / `log_execution_trace()` | 第 4 节 |
| `log_sanitize.py` | `sanitize_for_log()` / `redact_secrets()` / `sanitize_mapping()` | 第 5 节 |

## 核心行为与设计文档的对应关系

- **Hook 端点分层**（第 2.2 节"薄 I/O 层，厚业务层"）：`hooks_hermes.py` 只做
  验签 → `map_hermes_payload_to_trace()` 映射 → `TraceRepository.save()` 落库
  → `resolve_suspension()` 唤醒，不做任何触发率/裁判计算，业务判定完全留给
  被唤醒后继续跑的节点。
- **CI 阻断判定规则**（第 3.3 节）：`BenchmarkReport.blocking` 属性——任一
  `dimensions[].blocking == True` 且 `status == FAIL` 即为 `True`；
  `ReportGenerator.build()` 内部用同样的逻辑推导 `overall_status`（并额外
  处理了设计文档未细化的 `NEEDS_HUMAN_REVIEW` 情形：只要没有 blocking 的
  FAIL，但存在任一维度 `NEEDS_HUMAN_REVIEW`，整体状态跟随标记为
  `NEEDS_HUMAN_REVIEW`，不会被误判为 `PASS`）。
- **Langfuse 可选降级**（第 4 节）：`LangfuseSettings.enabled=False`（默认）
  时 `LangfuseAdapter.enabled` 恒为 `False`，全部 `log_*`/`start_run_trace`
  方法直接返回，不引入任何硬依赖；未安装可选依赖组
  `skill-evaluate[langfuse]` 时 `_build_default_client()` 捕获
  `ImportError` 并降级为 no-op，而不是启动时报错。
- **脱敏工具**（第 5 节）：`sanitize_for_log()` 对超过 500 字符的字段截断，
  `redact_secrets()` 对常见密钥格式（`sk-*`、AWS Access Key、GitHub token、
  `Bearer <token>`、以及要求同时含数字+大小写字母的通用长 base64/hex 串）
  打码；`langfuse_adapter.py::log_agent_call()`/`log_execution_trace()` 在
  写入 Langfuse 前统一经过这层脱敏，避免可观测性平台成为敏感信息泄露面。

## 与设计文档的差异 / 必要补充

| 差异点 | 类型 | 说明 |
|---|---|---|
| `ReportGenerator.build()` 需要先查 `runs` 表 | 设计遗漏补齐 | 见 04 模块 README 的"新增 `runs` 表"说明；`build(run_id)` 若找不到对应 run 记录会抛 `ObservabilityError`，而不是静默返回空报告 |
| `record_dimension_result()` 落盘到新增的 `dimension_results` 表 | 设计遗漏补齐 | 同上，见 04 模块 README |
| `security_findings_summary` 目前是全局统计，未按 `run_id` 过滤 | 已知简化，非最终形态 | `SecurityFindingRepository.summarize_by_severity()` 当前对全表按 `severity` group by；`security_findings` 表目前没有 `run_id` 列。等 `docs/dev/15` 落库真实安全发现时，如需要按 run 精确统计，需要给该表补一列（新增 Alembic revision），已在代码注释与接口文档中标注 |
| `verify_hermes_signature` 重命名为通用的 `verify_hmac_signature` | 结构性补充，保留兼容别名 | 05/22 两类回调共用同一套签名校验逻辑（设计文档第 6 节明确要求 `hooks_approval.py` "复用 `security.py` 的签名校验模式"），故把函数改为通用命名，同时保留 `verify_hermes_signature = verify_hmac_signature` 别名，不破坏按原文档命名查找该函数的读者 |

## 已知留白：人工审批回调端点

`api/hooks_approval.py::approval_hook()` 恒返回 `501`。已就绪、22 文档可
直接复用而不必重新设计的基础设施：`HumanApprovalORM`/
`HumanApprovalRepository`（04 模块）、`resolve_suspension()`（自动同时尝试
`pending_hooks`/`human_approvals` 两张表）、`verify_hmac_signature()`。接入
方式见 [`docs/dev/interfaces/05_hooks_approval.md`](../interfaces/05_hooks_approval.md)。

## 已知留白：Agent 基类挂载点

`LangfuseAdapter`/`MiniLLMClient` 两个协议接口均已就绪，但"具体在哪个 Agent
基类的哪个方法里调用"要等 `docs/dev/06~10` 设计出 Agent 基类才能确定，接入
方式见
[`docs/dev/interfaces/05_langfuse_hook_and_agent_base.md`](../interfaces/05_langfuse_hook_and_agent_base.md)。

## 如何验证

```bash
pytest tests/skill_evaluate/test_report_schema.py -q
python -c "from skill_evaluate.api.app import app; print(app)"   # FastAPI app 可正常构造
```

`test_report_schema.py` 覆盖 `BenchmarkReport.blocking` 的三种情形：阻断维度
失败→True、非阻断维度失败→False、全部通过→False。API 层（真实 HTTP 请求-
响应往返、签名校验拒绝路径）与 Langfuse 双写（需要真实/伪造的 Langfuse
client）未做自动化测试，属于"需要真实外部依赖"的集成测试范畴，本次未在无
Docker/无 Langfuse 账号的沙箱环境中补全。

## 待接入 / 下一步

- [`docs/dev/interfaces/05_hooks_approval.md`](../interfaces/05_hooks_approval.md)：`docs/dev/22` 实现人工审批回调端点。
- [`docs/dev/interfaces/05_langfuse_hook_and_agent_base.md`](../interfaces/05_langfuse_hook_and_agent_base.md)：`docs/dev/06~10` 在 Agent 基类中挂载 Langfuse 钩子与 `MiniLLMClient`。
- `docs/dev/11~20`：各评测维度节点调用 `ReportGenerator.record_dimension_result()` 写入判定结果。
- `docs/dev/15`：安全发现落库后，如需按 run 精确统计，为 `security_findings` 表补 `run_id` 列。
