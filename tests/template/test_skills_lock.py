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
    _git(repo, "init", "-q")  # template mode lists files with git
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


# --- template mode: `write` and `check --template` -------------------------------


def _git(repo: Path, *args: str) -> None:
    """Run git in ``repo`` (fixed argv, no shell), failing the test on error."""
    subprocess.run(  # nosec B603 B607
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


def _make_template(root: Path) -> Path:
    """Build a git repo with two skills and one non-skill directory."""
    for name in ("beta", "alpha"):
        skill = root / ".agents" / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n")
        (skill / "extra.md").write_text(f"{name} extra\n")
    # A directory without SKILL.md is not a skill.
    (root / ".agents" / "skills" / "not-a-skill").mkdir()
    (root / ".agents" / "skills" / "not-a-skill" / "notes.txt").write_text("x\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _read_lock(root: Path) -> dict:
    return json.loads((root / "skills-lock.json").read_text())


def _hash_of(path: Path) -> str:
    return _node("hash", path).stdout.strip()


def test_write_without_tag(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    result = _node("write", "--root", repo)
    assert result.returncode == 0, result.stderr
    text = (repo / "skills-lock.json").read_text()
    assert text.endswith("}\n")
    assert text.startswith('{\n  "version": 1,\n  "skills": {\n    "alpha": {')
    lock = json.loads(text)
    assert lock["version"] == 1
    assert list(lock["skills"]) == ["alpha", "beta"]
    for name, entry in lock["skills"].items():
        assert list(entry) == ["source", "sourceType", "skillPath", "computedHash"]
        assert entry["source"] == "emigre459/agentic-ai-dev-template"
        assert entry["sourceType"] == "github"
        assert entry["skillPath"] == f".agents/skills/{name}/SKILL.md"
        assert entry["computedHash"] == _hash_of(repo / ".agents/skills" / name)


def test_write_with_tag(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    tagged_alpha = _hash_of(repo / ".agents/skills/alpha")
    tagged_beta = _hash_of(repo / ".agents/skills/beta")
    _git(repo, "tag", "v0.1.0")
    (repo / ".agents/skills/alpha/SKILL.md").write_text("# alpha, edited\n")
    result = _node("write", "--root", repo, "--tag", "v0.1.0")
    assert result.returncode == 0, result.stderr
    skills = _read_lock(repo)["skills"]
    assert list(skills["alpha"]) == [
        "source",
        "ref",
        "sourceType",
        "skillPath",
        "computedHash",
    ]
    assert skills["alpha"]["ref"] == skills["beta"]["ref"] == "v0.1.0"
    assert skills["alpha"]["computedHash"] == tagged_alpha
    assert skills["alpha"]["computedHash"] != _hash_of(repo / ".agents/skills/alpha")
    assert skills["beta"]["computedHash"] == tagged_beta


def test_write_with_unknown_tag_is_a_usage_error(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    result = _node("write", "--root", repo, "--tag", "v9.9.9")
    assert result.returncode == 2
    assert "v9.9.9" in result.stderr
    assert not (repo / "skills-lock.json").exists()


def test_write_cleans_up_temp_dirs(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    _git(repo, "tag", "v0.1.0")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    result = subprocess.run(  # nosec B603 B607
        ["node", str(_SCRIPT), "write", "--root", str(repo), "--tag", "v0.1.0"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "TMPDIR": str(tmp)},
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp.iterdir()) == []


def test_template_check_ignores_unreleased_edits(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    _git(repo, "tag", "v0.1.0")
    (repo / ".agents/skills/alpha/SKILL.md").write_text("# alpha, edited\n")
    assert _node("write", "--root", repo, "--tag", "v0.1.0").returncode == 0
    template = _node("check", "--root", repo, "--template")
    assert template.returncode == 0, template.stdout + template.stderr
    plain = _node("check", "--root", repo)
    assert plain.returncode == 1
    assert "DRIFT alpha" in plain.stdout


def test_template_working_tree_hash_skips_gitignored_files(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    (repo / ".gitignore").write_text("__pycache__/\n.DS_Store\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    clean_alpha = _hash_of(repo / ".agents/skills/alpha")
    skill = repo / ".agents/skills/alpha"
    (skill / "__pycache__").mkdir()
    (skill / "__pycache__" / "x.pyc").write_bytes(b"\x00bytecode")
    (skill / ".DS_Store").write_bytes(b"finder")
    # Unignored untracked files still count: they are part of the working tree.
    (repo / ".agents/skills/beta/new.md").write_text("new\n")
    assert _hash_of(skill) != clean_alpha

    assert _node("write", "--root", repo).returncode == 0
    skills = _read_lock(repo)["skills"]
    assert skills["alpha"]["computedHash"] == clean_alpha
    assert skills["beta"]["computedHash"] != _hash_of(repo / ".agents/skills/alpha")

    template = _node("check", "--root", repo, "--template")
    assert template.returncode == 0, template.stdout + template.stderr
    # The plain consumer check keeps exact CLI parity: it sees every file on disk.
    plain = _node("check", "--root", repo)
    assert plain.returncode == 1
    assert "DRIFT alpha" in plain.stdout


def test_template_check_flags_a_tracked_file_edit_despite_ignores(
    tmp_path: Path,
) -> None:
    repo = _make_template(tmp_path / "repo")
    (repo / ".gitignore").write_text("__pycache__/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    assert _node("write", "--root", repo).returncode == 0
    (repo / ".agents/skills/alpha/extra.md").write_text("edited\n")
    result = _node("check", "--root", repo, "--template")
    assert result.returncode == 1
    assert "DRIFT alpha" in result.stdout


def test_template_check_flags_a_deleted_file(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    assert _node("write", "--root", repo).returncode == 0
    (repo / ".agents/skills/alpha/extra.md").unlink()
    result = _node("check", "--root", repo, "--template")
    assert result.returncode == 1
    assert "DRIFT alpha" in result.stdout


def test_template_check_flags_a_stale_tag_hash(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    _git(repo, "tag", "v0.1.0")
    assert _node("write", "--root", repo, "--tag", "v0.1.0").returncode == 0
    (repo / ".agents/skills/alpha/extra.md").write_text("changed after the lock\n")
    _git(repo, "commit", "-q", "-am", "edit")
    _git(repo, "tag", "-f", "v0.1.0")
    result = _node("check", "--root", repo, "--template")
    assert result.returncode == 1
    assert "DRIFT alpha" in result.stdout
    assert "DRIFT beta" not in result.stdout
    assert "make skills_lock" in result.stdout


def test_template_check_without_ref_compares_the_working_tree(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    assert _node("write", "--root", repo).returncode == 0
    assert _node("check", "--root", repo, "--template").returncode == 0
    (repo / ".agents/skills/beta/SKILL.md").write_text("# beta, edited\n")
    result = _node("check", "--root", repo, "--template")
    assert result.returncode == 1
    assert "DRIFT beta" in result.stdout


def test_template_check_needs_the_tag_locally(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    _git(repo, "tag", "v0.1.0")
    assert _node("write", "--root", repo, "--tag", "v0.1.0").returncode == 0
    _git(repo, "tag", "-d", "v0.1.0")
    result = _node("check", "--root", repo, "--template")
    assert result.returncode == 2
    assert "v0.1.0" in result.stderr
    assert "fetch" in result.stderr


def test_shipped_lock_is_current() -> None:
    result = _node("check", "--root", _REPO, "--template")
    assert result.returncode == 0, result.stdout + result.stderr
    lock = json.loads((_REPO / "skills-lock.json").read_text())
    on_disk = sorted(
        path.parent.name for path in (_REPO / ".agents/skills").glob("*/SKILL.md")
    )
    assert list(lock["skills"]) == on_disk
    for name, entry in lock["skills"].items():
        assert entry["source"] == "emigre459/agentic-ai-dev-template"
        assert entry["skillPath"] == f".agents/skills/{name}/SKILL.md"


def test_make_skills_lock_target_forwards_tag() -> None:
    tagged = subprocess.run(  # nosec B603 B607
        ["make", "-n", "skills_lock", "TAG=v1.2.3"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tagged.returncode == 0, tagged.stderr
    assert "skills-lock.mjs write --root . --tag v1.2.3" in tagged.stdout
    plain = subprocess.run(  # nosec B603 B607
        ["make", "-n", "skills_lock"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert plain.returncode == 0, plain.stderr
    assert "--tag" not in plain.stdout
