"""The review-skill validator: cross-skill script calls, shebang-aware ``+x``, JSON.

``.agents/skills/review-skill/scripts/validate.py`` is stdlib-only and run as a
script, so these tests drive its real command line (``--json <path>``) and read
the report back. ``test_every_shipped_skill_passes`` is the mechanical guard for
the rule that no skill tells the agent to run another skill's scripts: it runs
the validator over every directory under ``.agents/skills/``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess  # nosec B404 -- runs the validator with a fixed argv
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / ".agents" / "skills"
VALIDATOR = SKILLS_DIR / "review-skill" / "scripts" / "validate.py"

EXTERNAL_CHECK = "No cross-skill script invocations"

SHIPPED_SKILLS = sorted(
    p.name for p in SKILLS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")
)


def make_skill(
    root: Path, name: str, body: str, files: dict[str, str] | None = None
) -> Path:
    """Create ``root/name`` with a minimal valid ``SKILL.md`` and extra files.

    Parameters
    ----------
    root : Path
        Directory to create the skill under.
    name : str
        Skill folder name (also its frontmatter ``name``).
    body : str
        Markdown body for ``SKILL.md``.
    files : dict[str, str] | None
        Extra files, keyed by path relative to the skill directory.

    Returns
    -------
    Path
        The new skill directory.
    """
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A fixture skill used by the validator "
        f"tests. Use when testing.\n---\n\n{body}\n",
        encoding="utf-8",
    )
    for rel, content in (files or {}).items():
        target = skill / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return skill


def run_validator(skill: Path, tmp_path: Path) -> tuple[int, list[dict[str, str]]]:
    """Run the validator on ``skill`` and return its exit code and JSON results.

    Parameters
    ----------
    skill : Path
        Skill directory to validate.
    tmp_path : Path
        Directory for the JSON report (never inside the skill).

    Returns
    -------
    tuple[int, list[dict[str, str]]]
        The process exit code and the ``results`` list from the report.
    """
    report = tmp_path / "report.json"
    proc = subprocess.run(  # nosec B603 -- fixed argv, no shell
        [sys.executable, str(VALIDATOR), str(skill), "--json", str(report)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert report.exists(), proc.stdout + proc.stderr
    results: list[dict[str, str]] = json.loads(report.read_text())["results"]
    return proc.returncode, results


def levels(results: list[dict[str, str]], check: str) -> list[str]:
    """Return the levels of every result whose check name is ``check``."""
    return [r["level"] for r in results if r["check"] == check]


def test_a_cross_skill_script_invocation_fails(tmp_path: Path) -> None:
    other = make_skill(tmp_path, "other-skill", "Body.", {"scripts/tool.py": "x = 1\n"})
    assert other.is_dir()
    skill = make_skill(
        tmp_path,
        "my-skill",
        "Run it:\n\n```\npython3 ../other-skill/scripts/tool.py --flag\n```\n",
    )
    code, results = run_validator(skill, tmp_path)
    assert code == 1
    assert levels(results, EXTERNAL_CHECK) == ["FAIL"]
    message = next(r["message"] for r in results if r["check"] == EXTERNAL_CHECK)
    assert "../other-skill/scripts/tool.py" in message


@pytest.mark.parametrize(
    "command",
    [
        "bash ../other-skill/scripts/tool.sh",
        "sh ../other-skill/scripts/tool.sh",
        "python ../other-skill/scripts/tool.py",
        "uv run python ../other-skill/scripts/tool.py",
        "uv run ../other-skill/scripts/tool.py",
        "python3 /opt/elsewhere/tool.py",
        "python3 -I ../other-skill/scripts/tool.py",
        "../other-skill/scripts/tool.py --flag",
        "cat in.json | python3 ../other-skill/scripts/tool.py",
        "scripts/not_bundled_here.py --flag",
    ],
)
def test_every_run_form_that_leaves_the_skill_fails(
    tmp_path: Path, command: str
) -> None:
    skill = make_skill(tmp_path, "my-skill", f"```\n{command}\n```\n")
    code, results = run_validator(skill, tmp_path)
    assert code == 1, command
    assert levels(results, EXTERNAL_CHECK) == ["FAIL"], command


def test_an_inline_instruction_in_prose_fails(tmp_path: Path) -> None:
    skill = make_skill(
        tmp_path,
        "my-skill",
        "Then run `python3 ../other-skill/scripts/tool.py` to finish.",
    )
    _, results = run_validator(skill, tmp_path)
    assert levels(results, EXTERNAL_CHECK) == ["FAIL"]


def test_a_reference_file_is_scanned_too(tmp_path: Path) -> None:
    skill = make_skill(
        tmp_path,
        "my-skill",
        "See `references/notes.md` for details.",
        {"references/notes.md": "```\nbash ../other-skill/scripts/tool.sh\n```\n"},
    )
    _, results = run_validator(skill, tmp_path)
    assert levels(results, EXTERNAL_CHECK) == ["FAIL"]
    message = next(r["message"] for r in results if r["check"] == EXTERNAL_CHECK)
    assert "references/notes.md" in message


def test_an_in_skill_script_passes(tmp_path: Path) -> None:
    skill = make_skill(
        tmp_path,
        "my-skill",
        "```\npython3 scripts/tool.py --flag\nuv run python scripts/tool.py\n"
        "bash scripts/tool.sh\ncat x | python3 ./scripts/tool.py\n```\n"
        "Pass `scripts/tool.py` the issue number.",
        {
            "scripts/tool.py": "#!/usr/bin/env python3\n",
            "scripts/tool.sh": "#!/bin/sh\n",
        },
    )
    for script in ("tool.py", "tool.sh"):
        (skill / "scripts" / script).chmod(0o755)
    code, results = run_validator(skill, tmp_path)
    assert code == 0, results
    assert levels(results, EXTERNAL_CHECK) == ["PASS"]


def test_a_sibling_skill_md_pointer_is_allowed(tmp_path: Path) -> None:
    skill = make_skill(
        tmp_path,
        "my-skill",
        "For the merge-readiness report, follow `../resolve-pr-concerns/SKILL.md`.\n"
        "Details live in ../plan-issues/SKILL.md (read it; do not run it).\n"
        "Ordinary repo commands are fine: `make pr_check`, `gh pr view 1`,\n"
        "`git status`, `uv run pytest tests/ -k thing`, `python3 -m pytest tests/`.",
    )
    code, results = run_validator(skill, tmp_path)
    assert code == 0, results
    assert levels(results, EXTERNAL_CHECK) == ["PASS"]


def test_placeholder_paths_are_exempt(tmp_path: Path) -> None:
    skill = make_skill(
        tmp_path,
        "my-skill",
        "```\npython3 <skill-dir>/scripts/tool.py --repo <owner>/<repo>\n"
        "bash ${CLAUDE_SKILL_DIR}/scripts/tool.sh\n"
        "uv run python $SKILL_DIR/scripts/tool.py\n```\n",
    )
    code, results = run_validator(skill, tmp_path)
    assert code == 0, results
    assert levels(results, EXTERNAL_CHECK) == ["PASS"]


def test_importable_modules_need_no_exec_bit(tmp_path: Path) -> None:
    skill = make_skill(
        tmp_path,
        "my-skill",
        "Run `python3 scripts/tool.py`; it imports `scripts/helper.py`.",
        {
            "scripts/tool.py": "#!/usr/bin/env python3\nimport helper\n",
            "scripts/helper.py": "VALUE = 1\n",
        },
    )
    (skill / "scripts" / "tool.py").chmod(0o755)
    (skill / "scripts" / "helper.py").chmod(0o644)
    code, results = run_validator(skill, tmp_path)
    assert code == 0, results
    assert [r for r in results if r["level"] == "WARN"] == []
    assert levels(results, "'helper.py' is executable") == []
    assert levels(results, "'tool.py' is executable") == ["PASS"]


@pytest.mark.skipif(os.name == "nt", reason="no exec bit on Windows")
def test_a_shebang_script_without_exec_bit_warns(tmp_path: Path) -> None:
    skill = make_skill(
        tmp_path,
        "my-skill",
        "Run `python3 scripts/tool.py`.",
        {"scripts/tool.py": "#!/usr/bin/env python3\n"},
    )
    (skill / "scripts" / "tool.py").chmod(0o644)
    mode = (skill / "scripts" / "tool.py").stat().st_mode
    assert not mode & stat.S_IXUSR
    code, results = run_validator(skill, tmp_path)
    assert code == 0
    assert levels(results, "'tool.py' is executable") == ["WARN"]


def test_json_report_is_written(tmp_path: Path) -> None:
    skill = make_skill(tmp_path, "my-skill", "Hello.")
    code, results = run_validator(skill, tmp_path)
    assert code == 0
    assert results
    for entry in results:
        assert set(entry) == {"level", "check", "message"}
        assert entry["level"] in {"PASS", "FAIL", "WARN", "SKIP"}
    # No report is left inside the skill unless the caller asked for one.
    assert not (skill / ".validation-result.json").exists()


def test_no_json_flag_writes_nothing(tmp_path: Path) -> None:
    skill = make_skill(tmp_path, "my-skill", "Hello.")
    proc = subprocess.run(  # nosec B603 -- fixed argv, no shell
        [sys.executable, str(VALIDATOR), str(skill)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout
    assert sorted(p.name for p in skill.iterdir()) == ["SKILL.md"]


def test_a_missing_target_exits_2(tmp_path: Path) -> None:
    proc = subprocess.run(  # nosec B603 -- fixed argv, no shell
        [sys.executable, str(VALIDATOR), str(tmp_path / "nope")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2


@pytest.mark.parametrize("skill_name", SHIPPED_SKILLS)
def test_every_shipped_skill_passes(skill_name: str, tmp_path: Path) -> None:
    code, results = run_validator(SKILLS_DIR / skill_name, tmp_path)
    failures = [r for r in results if r["level"] == "FAIL"]
    assert failures == [], failures
    assert code == 0
