---
name: skill-evaluate-canary
description: 评测系统自身的金丝雀探针技能。当用户要求读取工作目录下的 data.txt 并以 JSON 包装输出其内容时使用。
---

# Canary Probe

本技能只用于验证评测沙箱的基础 I/O 是否正常（docs/dev/21 第 5 节），逻辑必须保持极简、确定。

## 步骤

1. 读取与本 SKILL.md 同目录下的 `data.txt`。
2. 只输出一个 JSON 对象，不输出任何解释性文字：

```json
{"content": "<data.txt 的完整内容，去掉首尾空白>"}
```

## 约束

- 不要修改、创建或删除任何文件。
- 不要访问网络。
- `content` 必须与文件内容逐字一致，不要翻译、改写或摘要。
