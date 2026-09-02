"""`ingestion/skill_loader.py`：Generator CLI 的输入解析（docs/dev/06 落地，
docs/dev/12 后续完善精度）。
"""

from pathlib import Path

import pytest

from skill_evaluate.errors import ConfigurationError
from skill_evaluate.ingestion import load_skill

_SKILL_MD = """---
name: CSV Cleaner
description: 清洗并校验 CSV 导出文件
---

# CSV Cleaner

遇到编码问题时，读 references/encodings.md 查对照表。
用 scripts/clean.py 执行清洗。
"""


def _write_skill(root: Path) -> Path:
    (root / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    (root / "references").mkdir()
    (root / "references" / "encodings.md").write_text("# 编码对照", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "clean.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "scripts" / "notes.txt").write_text("not a script", encoding="utf-8")
    return root


class SkillLoaderTests:
    def test_parses_frontmatter_and_directory_layout(self, tmp_path: Path) -> None:
        skill = load_skill(_write_skill(tmp_path))

        assert skill.skill_id == "csv-cleaner"
        assert skill.description == "清洗并校验 CSV 导出文件"
        assert "# CSV Cleaner" in skill.body_markdown
        # frontmatter 不计入正文行数/token 数。
        assert "description:" not in skill.body_markdown
        assert [f.path for f in skill.reference_files] == ["references/encodings.md"]
        assert [s.path for s in skill.scripts] == ["scripts/clean.py"]  # .txt 不算脚本

    def test_accepts_the_skill_md_path_directly(self, tmp_path: Path) -> None:
        _write_skill(tmp_path)
        skill = load_skill(tmp_path / "SKILL.md")
        assert skill.root_path == str(tmp_path.resolve())

    def test_reference_trigger_condition_captures_the_mentioning_line(self, tmp_path: Path) -> None:
        skill = load_skill(_write_skill(tmp_path))
        condition = skill.reference_files[0].trigger_condition
        assert condition is not None
        assert "遇到编码问题时" in condition

    def test_version_ref_is_stable_across_loads(self, tmp_path: Path) -> None:
        _write_skill(tmp_path)
        # staleness 检测依赖 version_ref 的稳定性：内容没变就不能变，
        # 否则每次运行都会误报"SKILL.md 改过了"。
        assert load_skill(tmp_path).version_ref == load_skill(tmp_path).version_ref

    def test_version_ref_changes_when_content_changes(self, tmp_path: Path) -> None:
        _write_skill(tmp_path)
        before = load_skill(tmp_path).version_ref
        (tmp_path / "SKILL.md").write_text(_SKILL_MD + "\n新增一行\n", encoding="utf-8")
        assert load_skill(tmp_path).version_ref != before

    def test_missing_description_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "SKILL.md").write_text("---\nname: x\n---\n\n# body\n", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="description"):
            load_skill(tmp_path)

    def test_missing_skill_md_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="未找到 SKILL.md"):
            load_skill(tmp_path)
