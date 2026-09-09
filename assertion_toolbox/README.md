# 断言工具箱：模块五交付的两个 SAST 模板

`docs/dev/interfaces/10_validator_toolbox_and_assertion_evidence.md` 第 4 节把两件
事留给了 docs/dev/15（模块五）：

1. `sql_no_injection_validator.py` / `html_no_xss_validator.py` 从清单占位变成可用
   实现，接入 Semgrep 或等价 SAST 工具，并把它的退出码**翻译**成本项目的约定；
2. 这两个模板是纯 `.py`（非 `.jinja`），`ToolboxClient.render()` 原样返回，参数化
   靠脚本自己的 CLI 参数——`params` 留空，这样它们能走零 LLM 成本的
   `template_lookup` 路径。

两个脚本都在 `templates/` 下，已经写完可以直接用。

## ⚠️ 这里不是工具箱仓库本身

`skill-evaluate-assertion-toolbox` 是**外部独立仓库**（docs/dev/10 第 3.1 节），
本项目只消费它。这个目录存放的是模块五**交付给运维侧的两份参考实现**——运维在按
docs/dev/10 第 3.1 节创建那个仓库时，把这两个文件复制进它的 `templates/`，并往
`manifest.yaml` 里追加下面两条记录即可。

放在本仓库里而不是只在文档里描述，是因为"接入 Semgrep 并翻译退出码"是一段有具体
判定口径的代码（什么算发现、扫描器崩了怎么办、转义过的负载算不算），写成散文
交给下一个人重新实现，两边的口径必然对不上。

## manifest.yaml 追加内容

```yaml
- template: sql_no_injection_validator.py
  description: "扫描生成物（SQL 语句 / 查询脚本）中的 SQL 注入痕迹"
  keywords: [sql, 注入, injection, 数据库, 查询, query, database, 安全]
  params: []
  language: python

- template: html_no_xss_validator.py
  description: "扫描生成物（HTML / Markdown 报告）中未转义的 XSS 载荷"
  keywords: [html, xss, 报告, report, 网页, 转义, escape, 安全]
  params: []
  language: python
```

`params: []` 是刻意的：空参数列表意味着 `ValidatorAgent.decide_strategy()` 认为
"必需参数齐备"，于是走 `template_lookup`——零 LLM 调用（docs/dev/10 第 5 节）。
扫描范围由脚本自己按后缀在工作目录里发现，不需要调用方告诉它扫哪个文件。

## 退出码约定（两个脚本一致）

| exit_code | 含义 | `AssertionResult.passed` |
|---|---|---|
| 0 | 未发现问题 | `True` |
| 1 | 发现高危 | `False` |
| 2 | **扫描器本身出错**（没有可扫的产物、读不了文件） | `False` |

1 与 2 都算失败（`passed = exit_code == 0`，docs/dev/10 第 6 节的唯一口径），
分开只是为了让人一眼看出"发现了问题"和"没扫成"不是一回事。

**没扫成绝不算通过**是这两个脚本最重要的一条设计：一个因为"扫描器崩了"而被判成
安全的产物，比一个明确被判失败的产物危险得多——后者会被人看到，前者不会。

## Semgrep 是可选增强

两个脚本都是"内置正则永远跑 + Semgrep 装了才跑"。Semgrep 不在 PATH、或规则包拉
不下来（模块五强制无出站网络的沙箱里这是常态）时，**不**判扫描失败，只在 stderr
记一条 info 并继续用内置规则。

想指定别的规则包：`SKILLEVAL_SEMGREP_SQL_CONFIG` / `SKILLEVAL_SEMGREP_XSS_CONFIG`。
离线环境请指向一份本地规则文件路径。

## 本地自测

```bash
python assertion_toolbox/templates/sql_no_injection_validator.py <某个产物目录>
python assertion_toolbox/templates/html_no_xss_validator.py <某个产物目录>
```
