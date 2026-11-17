"""Tests for the update-project-memory skill's ``check_budget.py``.

The script reports the line/char budget of ``AGENTS.md`` and exits 1 past a hard
cap. Caps are CLI args, so a repo can set its own; the defaults are pinned here
against the template's own ``AGENTS.md``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = (
    _ROOT
    / ".agents"
    / "skills"
    / "update-project-memory"
    / "scripts"
    / "check_budget.py"
)


def _run(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the bundled script over ``path`` with extra CLI ``args``."""
    return subprocess.run(
        [sys.executable, str(_SCRIPT), str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _file(tmp_path: Path, lines: int, width: int = 10) -> Path:
    """Write a file of ``lines`` lines, each ``width`` chars wide."""
    path = tmp_path / "AGENTS.md"
    path.write_text("\n".join("x" * width for _ in range(lines)) + "\n")
    return path


def test_template_agents_md_within_default_budget() -> None:
    result = _run(_ROOT / "AGENTS.md")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SOFT" not in result.stdout


def test_over_hard_cap_exits_1(tmp_path: Path) -> None:
    assert _run(_file(tmp_path, 151)).returncode == 1
    wide = _file(tmp_path, 10, width=1000)  # 10 lines but ~10k chars
    assert _run(wide).returncode == 1


def test_soft_cap_warns_but_passes(tmp_path: Path) -> None:
    result = _run(_file(tmp_path, 130))
    assert result.returncode == 0
    assert "SOFT" in result.stdout
    assert "SOFT" not in _run(_file(tmp_path, 120)).stdout


def test_caps_override_from_args(tmp_path: Path) -> None:
    path = _file(tmp_path, 60)
    assert _run(path).returncode == 0
    over = _run(path, "--max-lines", "50", "--soft-lines", "40")
    assert over.returncode == 1
    soft = _run(path, "--max-lines", "100", "--soft-lines", "50")
    assert soft.returncode == 0 and "SOFT" in soft.stdout
    chars = _run(path, "--max-chars", "100")
    assert chars.returncode == 1


def test_missing_path_is_a_one_line_error_exit_2(tmp_path: Path) -> None:
    result = _run(tmp_path / "absent.md")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1
    assert "absent.md" in result.stderr
