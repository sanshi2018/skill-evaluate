# security 子图：红蓝对抗安全评测

> 对应开发文档：`docs/dev/15`；接入文档：`docs/dev/interfaces/15_security_red_team.md`
> 代码位置：`src/skill_evaluate/nodes/security/`（判定侧）、`src/skill_evaluate/agents/attacker/`（出题侧）

---

## 一句话说清它是干什么的

**给一份即将合入的 Agent Skill 做一次上线前的"授权渗透测试"**：自动造一批攻击用例，
在断网的一次性沙箱里真的打一遍，看这份 Skill 挡不挡得住；挡不住的，判它有多严重，
能自动修的尝试自动修，并且**证明修补没有把正常功能改坏**，最后把结论写进评测报告、把
修复方案交给 PR。

---

## 1. 为什么需要这个子图（设计目的）

### 1.1 Skill 一旦被加载，就是 Agent 的攻击面

一份 Skill 本质是"一段会被注入到 Agent 上下文里的指令 + 一组它会去调用的脚本"。
Agent 加载它之后，用户的每一句输入、Agent 读到