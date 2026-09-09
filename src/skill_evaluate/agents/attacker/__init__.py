"""Attacker Agent：红队对抗用例的出题智能体（模块五 / docs/dev/15 第 2 节）。

实现文档：docs/dev/15_模块五_安全性与注入风险红蓝对抗评测.md。

- `playbook.py`：攻击手法注册表（子类型 → Prompt 模板 + 初始严重级别 + 条数权重），
  **导入本包即完成注册**。
- `agent.py`：`AttackerAgent`，`GeneratorAgent` 的子类，把 `ADVERSARIAL` 一个类别
  拆成七个攻击面各出一批。
- `service.py`：`AttackerService`，把 docs/dev/06 的三态生命周期原样用起来，
  不重新实现缓存/版本化。
- `prompts/*.jinja`：七个攻击面各自的出题 Prompt。

## 一句话说清它与 Generator 的关系

**同一套生命周期，不同的出题逻辑**。缓存复用、版本化、60/40 划分、反坍塌校验全部
复用 docs/dev/06 的 `TestSuiteService`；本包只提供"出什么题"。

## 七个攻击面

| 子类型 | 打什么 | 判定方式 |
|---|---|---|
| `direct_prompt_injection` | 输入里夹带越权指令 | LLM 语义裁决（CRITICAL 共识） |
| `data_poisoning` | 恶意文件内容间接投毒 | 确定性规则（载荷是否被当命令执行） |
| `env_theft` | 凭据/环境变量泄露 | 确定性规则（复用 docs/dev/05 的脱敏正则库） |
| `directory_traversal` | 工作区外的读写 | 确定性规则（路径是否逃逸 + 是否成功） |
| `dos_context_flood` | 撑爆上下文 | 确定性规则（超时=通过） |
| `dos_logic_bomb` | 不可终止的任务 | 确定性规则（同上） |
| `artifact_injection` | 生成物里带 SQL/XSS 载荷 | 断言脚本 SAST（不经 LLM 裁判） |

判定侧的实现在 `nodes/security/`，接入清单见
docs/dev/interfaces/15_security_red_team.md。
"""

from skill_evaluate.agents.attacker.agent import AttackerAgent
from skill_evaluate.agents.attacker.playbook import (
    ATTACK_PLAYBOOK_REGISTRY,
    AttackPlaybook,
    allocate_counts,
    get_attack_playbook,
    register_attack_playbook,
    registered_subtypes,
)
from skill_evaluate.agents.attacker.service import (
    CASES_PER_ATTACK_SUBTYPE,
    TRIGGERED_BY_ATTACKER_BOOTSTRAP,
    AttackerService,
    default_adversarial_count,
)

__all__ = [
    "ATTACK_PLAYBOOK_REGISTRY",
    "CASES_PER_ATTACK_SUBTYPE",
    "TRIGGERED_BY_ATTACKER_BOOTSTRAP",
    "AttackPlaybook",
    "AttackerAgent",
    "AttackerService",
    "allocate_counts",
    "default_adversarial_count",
    "get_attack_playbook",
    "register_attack_playbook",
    "registered_subtypes",
]
