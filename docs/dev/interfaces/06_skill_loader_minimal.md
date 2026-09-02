# 接入文档：`ingestion/skill_loader.py`（最小实现，待 docs/dev/12 完善）

> 由谁接入：`12`（模块二，该文档已规划本模块为"一切需要读取 SKILL.md 的文档
> 共用的解析层"）；`14` 需要追加 `SkillScript.is_mutating`。
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

## 三个桩，等 `12`/`14` 替换（**保持签名，只换函数体**）

### 1. `estimate_token_count()` —— 精度不足

当前是"约 4 字符 1 token（CJK 按 1.5 字符 1 token）"的粗估，够 Generator 做
Prompt 预算判断，**不足以**支撑 `12` 的 5,000 token 硬性卡线判定（会误判）。

`12` 接入方式：改用与 `LLMSettings.provider` 匹配的官方 tokenizer
（Anthropic 走 `client.messages.count_tokens`）离线精确计数。调用点
（`load_skill()` 内部、`_scan_reference_files()` 内部）不变。

### 2. `_extract_trigger_condition()` —— 只捞证据，不做判定

当前实现：在正文里找出提及该参考文件的那一行，原样作为
`SkillReferenceFile.trigger_condition`。正文完全没提到该文件时返回 `None`。

它**不判断**"这句话算不算一个明确的触发条件"——那是语义理解，由 Mini Agent 的
`progressive_disclosure_static` 模板完成（`07` 已预置，见
`docs/dev/interfaces/07_review_template_registry.md`）。所以这个字段的正确读法
是"给 Mini Agent 的证据行"，而不是"已确认的触发条件"。

`12` 若要收紧语法（例如只认"当……时，读取 X"这类句式），在此函数内实现。

### 3. `SkillScript.supports_help_flag` 恒为 `None`

静态解析拿不到可信答案，保持 `None`（"未知"）而不是猜一个 True/False。
由 `14` 的黑盒探测填充。

### 4. `14` 需要新增的字段

`14` 第 165 行要求给 `SkillScript` 追加 `is_mutating: bool | None`，由
`skill_loader` 解析脚本内容中的常见写操作 API 做启发式标注，解析失败为 `None`。
该字段**尚未添加**到 `state/skill.py`——`14` 接入时按追加式扩展补上（新增可选
字段，不改已有字段语义），并在 `_scan_scripts()` 里填充。

## 其他注意事项

- `skill_id` 取 frontmatter `name`（缺失则取目录名）的 slug。若 `12` 决定改用
  "仓库路径的 slug"（`02` 的原始建议），注意这会改变已落库测试集的绑定键，
  需要一次数据迁移，不要无声修改。
- `_SCRIPT_SUFFIXES` 目前是 `.py/.sh/.js/.ts/.rb/.ps1`。`14` 若要探测更多类型
  的脚本，在此扩充。
