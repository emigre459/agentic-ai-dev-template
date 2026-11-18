"""Every SKILL.md's frontmatter must parse with a strict YAML parser.

The open ``skills`` CLI skips a skill whose frontmatter is not valid YAML (an
unquoted ``: `` inside a description is enough), so ``npx skills add`` would not
find it. Claude Code's own parser is more lenient and would not notice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_SKILL_FILES = sorted(
    [*(_REPO / ".agents" / "skills").glob("*/SKILL.md"), *_FIXTURES.rglob("SKILL.md")]
)


def _frontmatter(path: Path) -> dict[str, object]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", f"{path}: no opening ---"
    end = lines.index("---", 1)
    loaded = yaml.safe_load("\n".join(lines[1:end]))
    assert isinstance(loaded, dict), f"{path}: frontmatter is not a mapping"
    return loaded


def test_skill_files_are_found() -> None:
    assert any(p.parent.parent.name == "skills" for p in _SKILL_FILES)
    assert any(_FIXTURES in p.parents for p in _SKILL_FILES)


@pytest.mark.parametrize("path", _SKILL_FILES, ids=lambda p: str(p.relative_to(_REPO)))
def test_frontmatter_parses_strictly(path: Path) -> None:
    meta = _frontmatter(path)

    assert isinstance(meta.get("name"), str) and meta["name"]
    assert isinstance(meta.get("description"), str) and meta["description"]
    assert meta["name"] == path.parent.name
