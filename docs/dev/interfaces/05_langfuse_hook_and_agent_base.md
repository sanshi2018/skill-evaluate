# 接入文档：Agent 基类挂载 Langfuse 钩子 + Mini Agent LLM Client

> 由谁接入：docs/dev/06~10（Generator/Mini/Judge/Optimizer/Validator Agent 框架）。
>
> **状态：第 1、2 节已由 docs/dev/06、07 完成接入**（`agents/base.py::BaseLLMAgent`
> 统一打点；`executors/factory.py` 注入 `RealMiniLLMClient` 取代 Stub）。第 3 节
> （`DimensionResult` 写入）仍待 docs/dev/11~20。新增 Agent 的写法见
> `docs/dev/interfaces/06_llm_client_and_sampling.md`。

## 1. LangfuseAdapter 挂载点

`src/skill_evaluate/observability/langfuse_adapter.py::LangfuseAdapter` 已完整
实现（`start_run_trace` / `log_agent_call` / `log_execution_trace`），未配置
`SKILLEVAL_LANGFUSE_*` 时自动降级为 no-op。

接入方式：docs/dev/06~10 设计"Agent 基类"时，在其内部统一封装的 `_call_llm()`
方法末尾调用一次：

```python
langfuse_adapter.log_agent_call(
    trace_handle, agent_name=self.name, prompt=prompt, response=response.text,
    model=self._model, usage=TimingCostMetrics(...),
)
```

> ✅ **docs/dev/24**：主图入口节点 `pipeline.bootstrap_run` 已调用 `start_run_trace(run_id, skill_id)` 建立顶层 trace；trace_handle **尚未**注入各 Agent（需要改十个维度的 Deps，列为可选增强，见 `docs/dev/interfaces/24_main_graph_and_ci_cd.md` 第 10 节）。

`trace_handle` 由流水线入口（docs/dev/24 或更早的运行入口）通过
`LangfuseAdapter().start_run_trace(run_id, skill_id)` 产出一次，随
`PipelineState` 或依赖注入容器传递给各 Agent，不建议塞进
`state.pipeline_state.PipelineState`（会破坏"只存 ID/引用"的约定），推荐作为
节点执行时的旁路依赖注入参数。

`ExecutorBackend` 产出 `ExecutionTrace` 后，调用方（评测维度节点）应调用一次
`langfuse_adapter.log_execution_trace(trace_handle, trace)`。

## 2. MiniAgentBackend 的 MiniLLMClient 协议

`src/skill_evaluate/executors/mini_backend.py::MiniLLMClient` 是 docs/dev/07
（Mini Agent 评审框架）需要实现并注入的最小 LLM 调用协议：

```python
class MiniLLMClient(Protocol):
    async def complete(self, *, prompt: str, model: str, temperature: float) -> MiniLLMResult: ...
```

当前默认注入 `StubMiniLLMClient`：返回固定的占位文本（明确标注 `[stub]`），
保证在 07 落地前 `MiniAgentBackend.execute()` 仍能产出结构合法的
`ExecutionTrace`，但 `final_response` 不代表任何真实评审结论——依赖真实语义
判断的调用方（模块二等）在 07 接入前不应读取该字段做业务决策。

接入方式：

1. 在 `agents/mini/` 下实现真实的 `MiniLLMClient`（调用
   `LLMSettings.mini_agent_model` 对应的 API），封装 Prompt 模板注册表
   （doc07 自身职责）。
2. 在构造 `MiniAgentBackend` 时传入该实现：
   `MiniAgentBackend(llm_client=RealMiniLLMClient(...), model=settings.llm.mini_agent_model)`，
   替换 `executors/factory.py::build_backend()` 中目前直接 `MiniAgentBackend()`
   的无参构造。

## 3. 待 Judge/Optimizer/Validator 落地后的 DimensionResult 写入

`observability/report_generator.py::ReportGenerator.record_dimension_result()`
已实现，docs/dev/11~20 各维度节点在完成判定后应调用它写入
`dimension_results` 表，供 `ReportGenerator.build()` 聚合为
`BenchmarkReport`。`security_findings_summary` 目前是全局统计（未按
run_id/skill_id 精细过滤，见 `report_generator.py` 内注释），docs/dev/15 落库
`SecurityFinding` 时如需要按 run 维度精确统计，需要给 `security_findings`
表补充 `run_id` 列（新增 Alembic revision）。
