"""Tests for the skills-drift action's ``skills-lock.mjs`` (hash + drift check).

The hash must equal what the pinned ``skills`` CLI writes as ``computedHash``
(lock version 2). ``fixtures/skills_lock/golden.json`` holds the CLI's own values:
for ``fixtures/skills_lock/demo-skill`` and for the edge-case folder that
``build_edge_skill`` (``conftest.py``) writes at test time (CRLF text, binary bytes, excluded names,
symlinks). They were produced by calling the CLI's ``computeSkillFolderHash``
(``dist/_chunks/remove.mjs`` in the npm package) at the version recorded as
``cli_version``, which must equal the pin in ``.github/actions/skills-cli``:
bumping the pin means regenerating them from that version, so a change in the
CLI's algorithm fails here, on the bump. The script is run as a real ``node``
process.

Outside template mode, ``check`` asks the pinned CLI itself (``npx skills check``);
those tests put a fake ``npx`` first on PATH, so they never touch the network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404 -- fixed argv, in-repo script, no shell
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

_REPO = Path(__file__).resolve().parents[2]
_ACTIONS = _REPO / ".github" / "actions"
_SCRIPT = _ACTIONS / "skills-drift" / "skills-lock.mjs"
_PIN_FILE = _ACTIONS / "skills-cli" / "package.json"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "skills_lock"
_DEMO = _FIXTURES / "demo-skill"
_GOLDEN_FILE: dict[str, str] = json.loads((_FIXTURES / "golden.json").read_text())
_GOLDEN = _GOLDEN_FILE["demo-skill"]
_PINNED_CLI: str = json.loads(_PIN_FILE.read_text())["dependencies"]["skills"]


def _node(
    *args: str | Path,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``skills-lock.mjs`` with ``args`` and capture its text output.

    A plain ``check`` (no ``--template``) runs the real CLI through ``npx``, so it
    must come with the env of ``_fake_npx``: no test may reach the network.
    """
    plain_check = args[:1] == ("check",) and "--template" not in args
    assert not plain_check or env is not None, "plain check needs _fake_npx's env"
    return subprocess.run(  # nosec B603 B607
        ["node", str(_SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=cwd,
    )


def _lock(hash_: str = _GOLDEN, version: int = 2) -> dict:
    return {
        "version": version,
        "skills": {
            "demo-skill": {
                "source": "example/skills",
                "ref": "v1.0.0",
                "sourceType": "github",
                "skillPath": "skills/demo-skill/SKILL.md",
                "computedHash": hash_,
            }
        },
    }


def _make_repo(
    root: Path,
    skills_dir: str | None = ".agents/skills",
    hash_: str = _GOLDEN,
    version: int = 2,
) -> Path:
    """Build a consumer repo holding ``demo-skill`` and a lock for it."""
    root.mkdir(parents=True, exist_ok=True)
    if skills_dir is not None:
        shutil.copytree(_DEMO, root / skills_dir / "demo-skill")
    (root / "skills-lock.json").write_text(
        json.dumps(_lock(hash_, version), indent=2) + "\n"
    )
    return root


# --- the hash ----------------------------------------------------------------


def test_golden_comes_from_the_pinned_cli() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", _PINNED_CLI), _PINNED_CLI
    assert _GOLDEN_FILE["cli_version"] == _PINNED_CLI, (
        "golden.json was produced by another CLI version than the pin: regenerate "
        "it with the pinned CLI (see this module's docstring) before bumping."
    )


def test_hash_matches_cli_golden() -> None:
    result = _node("hash", _DEMO)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _GOLDEN


def test_hash_matches_cli_golden_on_edge_cases(edge_skill: Path) -> None:
    result = _node("hash", edge_skill)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _GOLDEN_FILE["edge-skill"]


def test_hash_skips_excluded_names_but_not_node_modules(tmp_path: Path) -> None:
    copy = tmp_path / "demo-skill"
    shutil.copytree(_DEMO, copy)
    for excluded in (".git", "__pycache__", "sub/__pypackages__"):
        (copy / excluded).mkdir(parents=True)
        (copy / excluded / "x").write_text("excluded\n")
    (copy / "sub" / "metadata.json").write_text("{}\n")
    assert _node("hash", copy).stdout.strip() == _GOLDEN
    (copy / "node_modules").mkdir()
    (copy / "node_modules" / "y").write_text("a dependency\n")
    assert _node("hash", copy).stdout.strip() != _GOLDEN


def test_hash_normalizes_crlf_only_in_text_files(tmp_path: Path) -> None:
    lf, crlf = tmp_path / "lf", tmp_path / "crlf"
    for skill, text in ((lf, b"a\nb\n"), (crlf, b"a\r\nb\r\n")):
        skill.mkdir()
        (skill / "SKILL.md").write_bytes(text)
    assert _node("hash", lf).stdout == _node("hash", crlf).stdout
    (lf / "data.bin").write_bytes(b"\x00\n")
    (crlf / "data.bin").write_bytes(b"\x00\r\n")
    assert _node("hash", lf).stdout != _node("hash", crlf).stdout


def test_hash_of_missing_dir_is_a_usage_error(tmp_path: Path) -> None:
    result = _node("hash", tmp_path / "nope")
    assert result.returncode == 2
    assert result.stdout == ""


# --- check, outside template mode: the pinned CLI decides ---------------------


def _fake_npx(tmp_path: Path, output: str = "", code: int = 0) -> dict[str, str]:
    """Put a fake ``npx`` first on PATH; return the env that runs it.

    It records its argv, working directory and DISABLE_TELEMETRY in
    ``npx-call.json``, prints ``output`` and exits with ``code``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "npx"
    record = tmp_path / "npx-call.json"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], "
        "'cwd': os.getcwd(), 'telemetry': os.environ.get('DISABLE_TELEMETRY'), "
        "'color': os.environ.get('NO_COLOR')}))\n"
        f"sys.stdout.write({output!r})\n"
        f"sys.exit({code})\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def _npx_call(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "npx-call.json").read_text())


def test_check_runs_the_pinned_cli_in_the_repo(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    env = _fake_npx(tmp_path, "✓ All project skills are up to date\n")
    result = _node("check", "--root", repo, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    call = _npx_call(tmp_path)
    assert call["argv"] == ["-y", f"skills@{_PINNED_CLI}", "check"]
    assert Path(call["cwd"]).resolve() == repo.resolve()
    assert call["telemetry"] == "1"
    assert call["color"] == "1"
    assert "All project skills are up to date" in result.stdout


def test_check_defaults_root_to_cwd(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    env = _fake_npx(tmp_path)
    result = _node("check", env=env, cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(_npx_call(tmp_path)["cwd"]).resolve() == repo.resolve()


def test_check_reports_local_changes_as_drift(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    output = (
        "\x1b[38;5;145m✎ demo-skill has local changes\x1b[0m\n"
        "1 skill(s) differ from skills-lock.json; run npx skills install --force\n"
    )
    result = _node("check", "--root", repo, env=_fake_npx(tmp_path, output, 1))
    assert result.returncode == 1
    assert "DRIFT demo-skill" in result.stdout
    assert ".agents/skill-config/" in result.stdout
    assert f"npx -y skills@{_PINNED_CLI} experimental_install --force" in result.stdout


def test_check_lets_an_upstream_update_through(tmp_path: Path) -> None:
    """A newer upstream version is the update PR's job, not drift."""
    repo = _make_repo(tmp_path / "repo")
    output = "↑ demo-skill has an update\n1 skill(s) can be updated\n"
    result = _node("check", "--root", repo, env=_fake_npx(tmp_path, output, 1))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "UPDATE demo-skill" in result.stdout


@pytest.mark.parametrize("skills_dir", [None, ".agents/skills/other"])
def test_check_reports_missing_before_asking_the_cli(
    tmp_path: Path, skills_dir: str | None
) -> None:
    repo = _make_repo(tmp_path / "repo", skills_dir=skills_dir)
    # The CLI reads a deleted skill as "has an update"; the pre-check names it.
    output = "↑ demo-skill has an update\n"
    result = _node("check", "--root", repo, env=_fake_npx(tmp_path, output, 1))
    assert result.returncode == 1
    assert "MISSING demo-skill" in result.stdout
    assert "UPDATE demo-skill" not in result.stdout


def test_check_finds_the_harness_dir(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo", skills_dir=".claude/skills")
    result = _node("check", "--root", repo, env=_fake_npx(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_accepts_a_version_1_lock(tmp_path: Path) -> None:
    """The CLI migrates a version-1 lock itself, so consumers need not rewrite it."""
    repo = _make_repo(tmp_path / "repo", version=1)
    result = _node("check", "--root", repo, env=_fake_npx(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "output",
    ["Failed to update 1 skill(s)\n", "Error: fetch failed\n", ""],
    ids=["failed-update", "error", "silent"],
)
def test_check_never_passes_an_unexplained_cli_failure(
    tmp_path: Path, output: str
) -> None:
    repo = _make_repo(tmp_path / "repo")
    result = _node("check", "--root", repo, env=_fake_npx(tmp_path, output, 1))
    assert result.returncode == 2
    assert "skills check" in result.stderr


def test_check_fails_when_npx_is_missing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    env = {**os.environ, "PATH": str(tmp_path / "empty")}
    result = subprocess.run(  # nosec B603
        [shutil.which("node") or "node", str(_SCRIPT), "check", "--root", str(repo)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 2
    assert "npx" in result.stderr


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"version": 2}),
        json.dumps([1, 2]),
        json.dumps(_lock(version=3)),
        None,
    ],
    ids=["invalid-json", "no-skills-key", "not-an-object", "version-3", "no-lock-file"],
)
def test_check_rejects_bad_lock(tmp_path: Path, content: str | None) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    if content is not None:
        (repo / "skills-lock.json").write_text(content)
    result = _node("check", "--root", repo, env=_fake_npx(tmp_path))
    assert result.returncode == 2
    assert result.stderr.strip()
    assert not (tmp_path / "npx-call.json").exists()


def test_unknown_command_is_a_usage_error() -> None:
    result = _node("frobnicate")
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


# --- the action and the pin ----------------------------------------------------


def test_action_yml_wires_root_template_mode_and_the_cli_env() -> None:
    import yaml

    action = yaml.safe_load((_SCRIPT.parent / "action.yml").read_text(encoding="utf-8"))
    assert action["runs"]["using"] == "composite"
    assert action["inputs"]["root"]["default"] == "."
    (step,) = action["runs"]["steps"]
    assert 'node "$GITHUB_ACTION_PATH/skills-lock.mjs" check' in step["run"]
    assert "scripts/init_template.py" in step["run"]
    assert "--template" in step["run"]
    # The CLI fetches each source: a token keeps it under the API rate limit.
    assert step["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
    assert step["env"]["DISABLE_TELEMETRY"] == "1"


def test_the_cli_pin_is_exact() -> None:
    pin = json.loads(_PIN_FILE.read_text())
    assert pin["private"] is True
    assert list(pin["dependencies"]) == ["skills"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin["dependencies"]["skills"])


def test_no_skills_cli_call_is_unpinned() -> None:
    """Every place that runs the CLI goes through the pin, never `skills@latest`."""
    hits = [
        f"{path.relative_to(_REPO)}"
        for path in sorted(_ACTIONS.rglob("*"))
        if path.is_file() and "skills@latest" in path.read_text(encoding="utf-8")
    ]
    assert not hits, hits


def test_dependabot_bumps_the_cli_pin_in_the_template_only() -> None:
    text = (_REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    block = text.split("# >>> STACK:template", 1)[1].split("# <<< STACK:template", 1)[0]
    assert 'package-ecosystem: "npm"' in block
    assert 'directory: "/.github/actions/skills-cli"' in block


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
    assert text.startswith('{\n  "version": 2,\n  "skills": {\n    "alpha": {')
    lock = json.loads(text)
    assert lock["version"] == 2
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


def test_write_with_tag_lists_the_skills_at_the_tag(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    _git(repo, "tag", "v0.1.0")
    # After the release, main gains a skill and loses one; neither is the tag's.
    gamma = repo / ".agents/skills/gamma"
    gamma.mkdir()
    (gamma / "SKILL.md").write_text("# gamma\n")
    shutil.rmtree(repo / ".agents/skills/beta")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "after the release")

    result = _node("write", "--root", repo, "--tag", "v0.1.0")

    assert result.returncode == 0, result.stderr
    assert list(_read_lock(repo)["skills"]) == ["alpha", "beta"]


def test_write_with_a_tag_that_has_no_skills_is_a_usage_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("no skills yet\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "tag", "v0.1.0")
    result = _node("write", "--root", repo, "--tag", "v0.1.0")
    assert result.returncode == 2
    assert "v0.1.0" in result.stderr
    assert not (repo / "skills-lock.json").exists()


def test_write_without_tag_refuses_to_unpin_a_pinned_lock(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    _git(repo, "tag", "v0.1.0")
    assert _node("write", "--root", repo, "--tag", "v0.1.0").returncode == 0
    pinned = (repo / "skills-lock.json").read_text()

    result = _node("write", "--root", repo)

    assert result.returncode == 2
    assert "--tag" in result.stderr
    assert "delete" in result.stderr
    assert (repo / "skills-lock.json").read_text() == pinned


def test_write_without_tag_refuses_a_conflicted_pinned_lock(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    conflicted = '<<<<<<< ours\n{"skills": {"alpha": {"ref": "v0.1.0"}}}\n=======\n'
    (repo / "skills-lock.json").write_text(conflicted)
    result = _node("write", "--root", repo)
    assert result.returncode == 2
    assert (repo / "skills-lock.json").read_text() == conflicted


def test_write_without_tag_regenerates_an_unpinned_lock(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    assert _node("write", "--root", repo).returncode == 0
    (repo / ".agents/skills/alpha/SKILL.md").write_text("# alpha, edited\n")
    assert _node("write", "--root", repo).returncode == 0
    assert _read_lock(repo)["skills"]["alpha"]["computedHash"] == _hash_of(
        repo / ".agents/skills/alpha"
    )


def test_write_without_tag_regenerates_a_conflicted_unpinned_lock(
    tmp_path: Path,
) -> None:
    repo = _make_template(tmp_path / "repo")
    (repo / "skills-lock.json").write_text("<<<<<<< ours\n{}\n=======\n{}\n>>>>>>>\n")
    assert _node("write", "--root", repo).returncode == 0
    assert list(_read_lock(repo)["skills"]) == ["alpha", "beta"]


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
    # A pinned lock is re-pinned with TAG; a bare `make skills_lock` would un-pin it.
    assert "make skills_lock TAG=v0.1.0" in result.stdout


def test_template_check_without_ref_compares_the_working_tree(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    assert _node("write", "--root", repo).returncode == 0
    assert _node("check", "--root", repo, "--template").returncode == 0
    (repo / ".agents/skills/beta/SKILL.md").write_text("# beta, edited\n")
    result = _node("check", "--root", repo, "--template")
    assert result.returncode == 1
    assert "DRIFT beta" in result.stdout
    assert "make skills_lock" in result.stdout
    assert "TAG=" not in result.stdout


def test_template_check_rejects_a_version_1_lock(tmp_path: Path) -> None:
    """The template writes its own lock, so it must be the CLI's current version."""
    repo = _make_template(tmp_path / "repo")
    assert _node("write", "--root", repo).returncode == 0
    lock = _read_lock(repo)
    lock["version"] = 1
    (repo / "skills-lock.json").write_text(json.dumps(lock))
    result = _node("check", "--root", repo, "--template")
    assert result.returncode == 2
    assert "make skills_lock" in result.stderr


def test_template_check_needs_the_tag_locally(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    _git(repo, "tag", "v0.1.0")
    assert _node("write", "--root", repo, "--tag", "v0.1.0").returncode == 0
    _git(repo, "tag", "-d", "v0.1.0")
    result = _node("check", "--root", repo, "--template")
    assert result.returncode == 2
    assert "v0.1.0" in result.stderr
    assert "fetch" in result.stderr


def _expected_lock_names(repo: Path, lock: dict) -> list[str]:
    """Skill names a current lock must hold.

    A pinned lock (entries with a ``ref``) lists the skills present at that tag
    (folders under ``.agents/skills/`` holding a ``SKILL.md``); skills added on
    main since are unreleased and stay out of it. An unpinned lock lists the
    working tree's skills.
    """
    refs = {entry["ref"] for entry in lock["skills"].values() if "ref" in entry}
    if not refs:
        return sorted(
            path.parent.name for path in (repo / ".agents/skills").glob("*/SKILL.md")
        )
    assert len(refs) == 1, f"lock entries disagree on ref: {sorted(refs)}"
    listing = subprocess.run(  # nosec B603 B607
        ["git", "ls-tree", "-r", "--name-only", f"refs/tags/{refs.pop()}"]
        + ["--", ".agents/skills/"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return sorted(
        parts[2]
        for parts in (line.split("/") for line in listing)
        if len(parts) == 4 and parts[3] == "SKILL.md"
    )


def test_expected_lock_names_follow_the_tag_when_pinned(tmp_path: Path) -> None:
    repo = _make_template(tmp_path / "repo")
    _git(repo, "tag", "v0.1.0")
    gamma = repo / ".agents/skills/gamma"
    gamma.mkdir()
    (gamma / "SKILL.md").write_text("# gamma\n")
    pinned = {"skills": {"alpha": {"ref": "v0.1.0"}}}
    assert _expected_lock_names(repo, pinned) == ["alpha", "beta"]
    assert _expected_lock_names(repo, {"skills": {"alpha": {}}}) == [
        "alpha",
        "beta",
        "gamma",
    ]


def test_shipped_lock_is_current() -> None:
    result = _node("check", "--root", _REPO, "--template")
    assert result.returncode == 0, result.stdout + result.stderr
    lock = json.loads((_REPO / "skills-lock.json").read_text())
    assert lock["version"] == 2
    assert list(lock["skills"]) == _expected_lock_names(_REPO, lock)
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
