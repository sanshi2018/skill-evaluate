# 接入文档：`ingestion/skill_loader.py`（最小实现，待 docs/dev/12 完善）

> 由谁接入：`12`（模块二，该文档已规划本模块为"一切需要读取 SKILL.md 的文档
> 共用的解析层"）—— 已接入；`14`（追加 `SkillScript.is_mutating`）—— 已接入。
> 当前状态：`docs/dev/06` 的 CLI 子命令 `skill-evaluate generate --skill-path`
> 必须先有一个"路径 -> `SkillDefinition`"的入口，因此按 `12` 指定的路径与函数名
> 先落了一个最小可用实现。

## 现状

```python
from skill_evaluate.ingestion import load_skill, estimate_token_count

skill = load_skill("path/to/skill-dir")   # 或直接传 SKILL.md 文件路径
```

已做实（`12` 可直接沿用，不必重写）：

- YAML frontmatter 的标量解析（`name` / `description`），不引入 yaml 依赖。
- `description` 缺失直接抛 `ConfigurationError`——它是模块一触发准确度的**被测
  对象本身**，缺了不该静默继续。
- `line_count` = 去掉 frontmatter 后的正文行数（与 `12` 第 2 节口径一致）。
- `references/` 与 `scripts/` 目录扫描。
- `version_ref` 解析：优先取 git commit sha；工作区脏时追加 `+dirty:<内容哈希>`
  后缀（否则本地改了 SKILL.md 却报告"版本没变"，`06` 的 staleness 检测在本地
  永远失效）；非 git 环境退化为 `sha256:<前 12 位>`。

## 桩的现状（**保持签名，只换函数体**）

### 1. `estimate_token_count()` —— ✅ `12` 已接入

实现已迁到 `ingestion/token_counter.py`：装了 `tiktoken`（可选依赖）走离线精确
计数，否则退化为"字符数 × 3/4"的兜底估算。`estimate_token_count()` 的签名与调用点
（`load_skill()` / `_scan_reference_files()` 内部）都没变。

**要拿 Token 数做卡线判定的场景改用 `count_tokens()`**，它返回
`TokenCount(value, method, exact)`——`exact=False` 时不得无宽容度地阻断。口径与
"为什么不用 Anthropic 官方计数"的取舍见
`docs/dev/interfaces/12_context_scoping_static_pipeline.md` 第 6 节。

### 2. `_extract_trigger_condition()` —— ✅ `12` 复核后**维持原样**（不是遗留桩）

实现：在正文里找出提及该参考文件的那一行，原样作为
`SkillReferenceFile.trigger_condition`。正文完全没提到该文件时返回 `None`。

它**不判断**"这句话算不算一个明确的触发条件"。`12` 评估后决定不在这里收紧语法：
判定分两级放在模块二自己那边——`nodes/context_scoping/static_scan.py` 的条件词
正则做初筛（限定在同一语义单元内），Mini Agent 的 `progressive_disclosure_static`
模板做语义定夺。收紧到 loader 里只会得到一个更容易误伤、且被**所有**读 Skill 的
模块共享的正则。

所以这个字段的正确读法仍然是"给下游的证据行"，而不是"已确认的触发条件"。

### 3. `SkillScript.supports_help_flag` 恒为 `None` —— `14` 复核后**维持原样**

静态解析拿不到可信答案，保持 `None`（"未知"）而不是猜一个 True/False。

`14` 落地后**仍然不回写这个字段**：探测结论存在模块四自己的图状态与
`JudgeRepository` 里。回写意味着一次评测运行会改动 `skills` 表里的静态快照，而那份
快照的语义是"解析 SKILL.md 当时的样子"。需要这个信息的模块请读报告。

### 4. `SkillScript.is_mutating` —— ✅ `14` 已接入

字段已加到 `state/skill.py`（可选字段 + 默认 `None`，`skills.scripts` 是 JSON 列，
**无需迁移**），由 `detect_mutating_script(source, suffix)` 在 `_scan_scripts()` 里
填充，覆盖 Python / Shell / JS·TS / Ruby / PowerShell 五个语言族的写操作正则。

**三态语义，不要当布尔用**：`True` = 扫到写操作；`False` = 扫过了没扫到；
`None` = 无法判定（文件读不到、二进制、语言不在覆盖范围内）。`if not
script.is_mutating` 会把"没扫出来"当成"确认安全"，正是这个字段最不能出的错。

启发式**刻意偏向误报**：漏判会让模块四整个跳过该脚本的幂等性探测（真实缺陷溜走），
误判只是多跑两次容器。口径详见
`docs/dev/interfaces/14_script_usability_probing.md` 第 7 节。

## 其他注意事项

- `skill_id` 取 frontmatter `name`（缺失则取目录名）的 slug。若 `12` 决定改用
  "仓库路径的 slug"（`02` 的原始建议），注意这会改变已落库测试集的绑定键，
  需要一次数据迁移，不要无声修改。
- `_SCRIPT_SUFFIXES` 目前是 `.py/.sh/.js/.ts/.rb/.ps1`，`14` 的运行时推断表
  （`executors/script_sandbox.py::infer_runtime()`）覆盖了这全部六种加
  `.bash/.mjs/.cjs`。要探测更多类型的脚本，两处都要扩充：这里决定"扫不扫进来"，
  那里决定"用哪个镜像跑"。
