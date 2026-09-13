"""后端路由表（docs/dev/03 第 5 节）：哪个评测维度用哪种后端，固定映射，写入配置
而非散落各节点。

各评测维度对应文档（11~20）在实现节点时，从此表读取默认后端，不再自行判断；
若某维度需要"部分子检查用 mini、部分用 pluggable"，由该维度文档在内部拆分子
节点各自声明，而不是让整个维度用单一后端牵就最严格的子检查。
"""

from __future__ import annotations

from skill_evaluate.state.enums import ExecutorBackendType

NODE_BACKEND_ROUTING: dict[str, ExecutorBackendType] = {
    "trigger_accuracy": ExecutorBackendType.PLUGGABLE,  # 模块一：必须真实观测是否加载 SKILL.md
    "context_scoping": ExecutorBackendType.MINI,  # 模块二：纯静态文本审查
    "instruction_control": ExecutorBackendType.PLUGGABLE,  # 模块三：A/B 对比、Trace 深度审查
    "script_usability": ExecutorBackendType.PLUGGABLE,  # 模块四：真实黑盒调用脚本子进程
    "security": ExecutorBackendType.PLUGGABLE,  # 模块五：红队攻击必须真实执行
    "coverage_analysis": ExecutorBackendType.MINI,  # 模块六/七/八：主要是对已有 Trace/文本的分析
    "cross_model_generalization": ExecutorBackendType.PLUGGABLE,  # 模块九：异构矩阵
    "multi_skill_conflict": ExecutorBackendType.PLUGGABLE,  # 模块十：并发加载必须真实沙箱
    # docs/dev/21：前置门禁证明的是"评测维度将要使用的那个真实沙箱"是否可信，必须与
    # 模块一/三/四/五/九/十同一后端；拿 Mini 后端跑金丝雀永远健康，证明不了任何事。
    "preflight": ExecutorBackendType.PLUGGABLE,
}


def resolve_backend_type(node_name: str) -> ExecutorBackendType:
    if node_name not in NODE_BACKEND_ROUTING:
        raise KeyError(
            f"节点 {node_name!r} 未登记后端路由，请在 NODE_BACKEND_ROUTING 中显式声明"
            "（docs/dev/03 第 5 节：不允许节点自行判断使用哪种后端）。"
        )
    return NODE_BACKEND_ROUTING[node_name]
