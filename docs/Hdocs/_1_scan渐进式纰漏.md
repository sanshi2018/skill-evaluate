def scan_progressive_disclosure

1.当前skill声明了reference_files
2.正则扫描正文中提及的ref的文件名的行号（优先前序路径匹配）
3.如果没有扫到，标记下来
4.扫描条件ref，正则扫描指定段落（如果，若，xxx）.如果扫到 应当把这一行保存下来，作为人工/llm的evidence
    QA：def _semantic_unit 如何避免，把上一行的条件词当作本行的，邻居有条件词这一情况。
5.检测是否做渐进式纰漏
    1.正文已经接近限额的80%缺没有拆分任何ref

6.结论，所谓渐进式纰漏，就是条件纰漏，各个引用文档应当有条件的去加载，而不是必须load进。

情况一：reference 位于列表项首行
例如：
- 当导出失败时，读取 export.md
- 详见 errors.md
处理第二项时，函数从第二项开始，只向后收集它自己的缩进续行，不向上读取第一项。
所以第一项里的“当……时”不会算到第二项头上。
这个边界已有专门测试，见[test_context_scoping.py](../../tests/skill_evaluate/test_context_scoping.py)。我也实际运行了相关的三个边界测试，全部通过。

情况二：reference 位于普通自然段
普通正文以空行作为段落边界。
例如：
如果文件包含 BOM，
读取 references/encodings.md。
虽然条件在上一行，但两行属于同一自然段，因此应当一起判断。现有测试明确要求这种写法通过

相反：
如果磁盘满了就报错。

详见 references/errors.md。
中间有空行，所以属于两个自然段。上一段的“如果”不会传给下一段。对应测试在

