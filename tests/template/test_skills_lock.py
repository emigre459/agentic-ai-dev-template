"""Tests for the skills-drift action's ``skills-lock.mjs`` (hash + drift check).

The hash must equal what the real ``skills`` CLI writes as ``computedHash``;
``fixtures/skills_lock/golden.json`` holds that value, produced by running
``npx -y skills@latest add`` on ``fixtures/skills_lock/demo-skill`` (the CLI
version is recorded alongside it). The script is run as a real ``node`` process.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 -- fixed argv, in-repo script, no shell
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / ".github" / "actions" / "skills-drift" / "skills-lock.mjs"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "skills_lock"
_DEMO = _FIXTURES / "demo-skill"
_GOLDEN: str = json.loads((_FIXTURES / "golden.json").read_text())["demo-skill"]


def _node(*args: str | Path) -> subprocess.CompletedProcess[str]:
    """Run ``skills-lock.mjs`` with ``args`` and capture its text output."""
    return subprocess.run(  # nosec B603 B607
        ["node", str(_SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


def _make_repo(
    root: Path, skills_dir: str | None = ".agents/skills", hash_: str = _GOLDEN
) -> Path:
    """Build a consumer repo holding ``demo-skill`` and a lock for it."""
    root.mkdir(parents=True, exist_ok=True)
    if skills_dir is not None:
        shutil.copytree(_DEMO, root / skills_dir / "demo-skill")
    lock = {
        "version": 1,
        "skills": {
            "demo-skill": {
                "source": "example/skills",
                "sourceType": "github",
                "skillPath": "skills/demo-skill/SKILL.md",
                "computedHash": hash_,
            }
        },
    }
    (root / "skills-lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    return root


def test_hash_matches_cli_golden() -> None:
    result = _node("hash", _DEMO)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _GOLDEN


def test_hash_ignores_git_node_modules_and_symlinks(tmp_path: Path) -> None:
    copy = tmp_path / "demo-skill"
    shutil.copytree(_DEMO, copy)
    (copy / ".git").mkdir()
    (copy / ".git" / "x").write_text("git internals")
    (copy / "sub" / "node_modules").mkdir()
    (copy / "sub" / "node_modules" / "y").write_text("a dependency")
    os.symlink(copy / "alpha.md", copy / "link.md")
    os.symlink(copy / "sub", copy / "linked-dir")
    result = _node("hash", copy)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _GOLDEN


def test_hash_of_missing_dir_is_a_usage_error(tmp_path: Path) -> None:
    result = _node("hash", tmp_path / "nope")
    assert result.returncode == 2
    assert result.stdout == ""


def test_check_passes_on_clean_install(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    result = _node("check", "--root", repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_defaults_root_to_cwd(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    result = subprocess.run(  # nosec B603 B607
        ["node", str(_SCRIPT), "check"],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_template_flag_is_accepted(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    result = _node("check", "--root", repo, "--template")
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_reports_drift(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    with (repo / ".agents/skills/demo-skill/alpha.md").open("a") as handle:
        handle.write("!")
    result = _node("check", "--root", repo)
    assert result.returncode == 1
    assert "DRIFT demo-skill" in result.stdout
    assert ".agents/skill-config/" in result.stdout


def test_check_finds_harness_dir(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", skills_dir=".claude/skills")
    result = _node("check", "--root", repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_prefers_the_agents_dir(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    shutil.copytree(_DEMO, repo / ".claude/skills/demo-skill")
    (repo / ".claude/skills/demo-skill/alpha.md").write_text("edited\n")
    result = _node("check", "--root", repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_reports_missing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", skills_dir=None)
    result = _node("check", "--root", repo)
    assert result.returncode == 1
    assert "MISSING demo-skill" in result.stdout


@pytest.mark.parametrize(
    "content",
    ["{not json", json.dumps({"version": 1}), json.dumps([1, 2]), None],
    ids=["invalid-json", "no-skills-key", "not-an-object", "no-lock-file"],
)
def test_check_rejects_bad_lock(tmp_path: Path, content: str | None) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    if content is not None:
        (repo / "skills-lock.json").write_text(content)
    result = _node("check", "--root", repo)
    assert result.returncode == 2
    assert result.stderr.strip()


def test_unknown_command_is_a_usage_error() -> None:
    result = _node("frobnicate")
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


def test_action_yml_wires_root_and_template_mode() -> None:
    import yaml

    action = yaml.safe_load((_SCRIPT.parent / "action.yml").read_text(encoding="utf-8"))
    assert action["runs"]["using"] == "composite"
    assert action["inputs"]["root"]["default"] == "."
    script = "\n".join(step["run"] for step in action["runs"]["steps"])
    assert 'node "$GITHUB_ACTION_PATH/skills-lock.mjs" check' in script
    assert "scripts/init_template.py" in script
    assert "--template" in script
