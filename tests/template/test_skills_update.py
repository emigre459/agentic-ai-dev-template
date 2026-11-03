"""Tests for the skills-update action: ``bump-pins.mjs`` and ``update-pr.sh``.

``bump-pins.mjs`` shells out to ``gh`` and ``npx`` found on ``PATH``; the tests
put fake executables first on ``PATH`` that record their argv and serve canned
JSON. ``update-pr.sh`` is run against a throwaway repo with a bare ``origin``;
its ``GH_CMD`` seam points at a fake ``gh`` that records calls and keeps the
open-PR state in a file.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess  # nosec B404 -- fixed argv, in-repo scripts, no shell
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[2]
_ACTION = _REPO / ".github" / "actions" / "skills-update"
_BUMP = _ACTION / "bump-pins.mjs"
_UPDATE_PR = _ACTION / "update-pr.sh"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)
needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is not on PATH"
)

_SOURCE = "acme/skills"


def _make_fake(bin_dir: Path, name: str, py_source: str) -> None:
    """Write executable ``bin_dir/name`` that runs ``py_source`` with this Python.

    A tiny ``sh`` wrapper execs the interpreter so the shebang stays short no
    matter how deep the virtualenv path is.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    impl = bin_dir / f"{name}_impl.py"
    impl.write_text(py_source)
    wrapper = bin_dir / name
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# --------------------------------------------------------------------------- #
# bump-pins.mjs
# --------------------------------------------------------------------------- #

_FAKE_GH_API = r"""
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps(["gh", *args]) + "\n")
cfg = json.load(open(os.environ["FAKE_CFG"]))
if cfg.get("fail_gh"):
    sys.stderr.write("gh: simulated failure\n")
    sys.exit(1)
if args[0] != "api":
    sys.exit(2)
path = args[1]
if path.endswith("/releases"):
    repo = path[len("repos/"):-len("/releases")]
    # `gh api --paginate` prints one JSON array per page, back to back.
    sys.stdout.write("".join(json.dumps(p) for p in cfg["releases"].get(repo, [[]])))
elif "/git/trees/" in path:
    repo, rest = path[len("repos/"):].split("/git/trees/")
    tag = rest.split("?")[0]
    paths = cfg["trees"][f"{repo}@{tag}"]
    tree = [{"path": p, "type": "blob"} for p in paths]
    tree.append({"path": "skills", "type": "tree"})
    sys.stdout.write(json.dumps({"tree": tree, "truncated": False}))
else:
    sys.exit(2)
"""

_FAKE_NPX = r"""
import json, os, sys

with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps(["npx", *sys.argv[1:]]) + "\n")
    fh.write(json.dumps(["env", os.environ.get("DISABLE_TELEMETRY", "")]) + "\n")
cfg = json.load(open(os.environ["FAKE_CFG"]))
sys.exit(cfg.get("npx_exit", 0))
"""


def _release(tag: str, *, draft: bool = False, prerelease: bool = False) -> dict:
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease}


def _lock_entry(source: str = _SOURCE, ref: str | None = None) -> dict[str, str]:
    entry = {"source": source}
    if ref is not None:
        entry["ref"] = ref
    entry.update({"sourceType": "github", "skillPath": "x", "computedHash": "0" * 8})
    return entry


class _Bump:
    """A consumer repo plus fake ``gh``/``npx`` for one ``bump-pins.mjs`` run."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        self.root.mkdir()
        self.bin = tmp_path / "bin"
        self.log = tmp_path / "calls.log"
        self.cfg_path = tmp_path / "cfg.json"
        self.cfg: dict[str, Any] = {"releases": {}, "trees": {}}
        _make_fake(self.bin, "gh", _FAKE_GH_API)
        _make_fake(self.bin, "npx", _FAKE_NPX)

    def install(self, name: str, skills_dir: str = ".agents/skills") -> None:
        folder = self.root / skills_dir / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"# {name}\n")

    def write_lock(self, skills: dict[str, dict[str, str]]) -> None:
        (self.root / "skills-lock.json").write_text(
            json.dumps({"version": 1, "skills": skills}, indent=2) + "\n"
        )

    def run(self) -> subprocess.CompletedProcess[str]:
        self.cfg_path.write_text(json.dumps(self.cfg))
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_CFG": str(self.cfg_path),
            "FAKE_LOG": str(self.log),
        }
        env.pop("DISABLE_TELEMETRY", None)
        return subprocess.run(  # nosec B603 B607
            ["node", str(_BUMP), "--root", str(self.root)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def calls(self, tool: str) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [r[1:] for r in rows if r[0] == tool]


@pytest.fixture
def bump(tmp_path: Path) -> _Bump:
    return _Bump(tmp_path)


@needs_node
def test_bumps_to_newest_stable_semver(bump: _Bump) -> None:
    bump.install("alpha")
    bump.write_lock({"alpha": _lock_entry(ref="v0.1.0")})
    bump.cfg["releases"][_SOURCE] = [
        [
            _release("v0.1.0"),
            _release("v0.2.0"),
            _release("v0.3.0-rc.1", prerelease=True),
            _release("v0.9.0", draft=True),
            _release("nightly"),
        ]
    ]
    bump.cfg["trees"][f"{_SOURCE}@v0.2.0"] = ["skills/alpha/SKILL.md"]
    result = bump.run()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "bumped": [
            {
                "name": "alpha",
                "source": _SOURCE,
                "from": "v0.1.0",
                "to": "v0.2.0",
                "notes_url": f"https://github.com/{_SOURCE}/releases/tag/v0.2.0",
            }
        ],
        "available": [],
        "removed": [],
        "no_releases": [],
    }
    assert bump.calls("npx") == [
        [
            "-y",
            "skills@latest",
            "add",
            f"{_SOURCE}#v0.2.0",
            "--skill",
            "alpha",
            "-a",
            "universal",
            "-y",
        ]
    ]
    assert bump.calls("env") == [["1"]]  # DISABLE_TELEMETRY=1 reaches the CLI


@needs_node
def test_releases_are_read_across_pages_and_compared_numerically(
    bump: _Bump,
) -> None:
    bump.install("alpha")
    bump.write_lock({"alpha": _lock_entry(ref="v0.9.0")})
    bump.cfg["releases"][_SOURCE] = [
        [_release("v0.9.0"), _release("v0.10.0")],
        [_release("v0.2.0")],
    ]
    bump.cfg["trees"][f"{_SOURCE}@v0.10.0"] = ["skills/alpha/SKILL.md"]
    result = bump.run()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["bumped"][0]["to"] == "v0.10.0"
    assert bump.calls("gh")[0] == ["api", f"repos/{_SOURCE}/releases", "--paginate"]


@needs_node
def test_already_on_newest_release_does_nothing(bump: _Bump) -> None:
    bump.install("alpha")
    bump.write_lock({"alpha": _lock_entry(ref="v0.2.0")})
    bump.cfg["releases"][_SOURCE] = [[_release("v0.2.0"), _release("v0.1.0")]]
    result = bump.run()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "bumped": [],
        "available": [],
        "removed": [],
        "no_releases": [],
    }
    assert bump.calls("npx") == []


@needs_node
def test_leaves_branch_tracking_entries_alone(bump: _Bump) -> None:
    bump.install("alpha")
    bump.install("beta")
    bump.write_lock(
        {"alpha": _lock_entry(), "beta": _lock_entry(source="acme/other", ref="main")}
    )
    result = bump.run()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["bumped"] == []
    assert bump.calls("npx") == []
    assert bump.calls("gh") == []  # nothing tag-pinned, nothing to look up


@needs_node
def test_source_without_releases_is_reported(bump: _Bump) -> None:
    bump.install("alpha")
    bump.write_lock({"alpha": _lock_entry(ref="v0.1.0")})
    bump.cfg["releases"][_SOURCE] = [[_release("nightly"), _release("v1.0.0-rc.1")]]
    result = bump.run()
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["no_releases"] == [_SOURCE]
    assert summary["bumped"] == []
    assert bump.calls("npx") == []


@needs_node
def test_new_upstream_skill_listed_as_available_not_installed(bump: _Bump) -> None:
    bump.install("alpha")
    bump.write_lock({"alpha": _lock_entry(ref="v0.1.0")})
    bump.cfg["releases"][_SOURCE] = [[_release("v0.1.0"), _release("v0.2.0")]]
    bump.cfg["trees"][f"{_SOURCE}@v0.2.0"] = [
        "skills/alpha/SKILL.md",
        "skills/alpha/references/notes.md",
        "skills/newcomer/SKILL.md",
    ]
    result = bump.run()
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["available"] == [{"source": _SOURCE, "name": "newcomer"}]
    (npx,) = bump.calls("npx")
    assert "newcomer" not in npx  # not installed on the user's behalf


@needs_node
def test_removed_upstream_skill_kept_and_flagged(bump: _Bump) -> None:
    bump.install("alpha")
    bump.install("gone")
    bump.write_lock(
        {"alpha": _lock_entry(ref="v0.1.0"), "gone": _lock_entry(ref="v0.1.0")}
    )
    bump.cfg["releases"][_SOURCE] = [[_release("v0.1.0"), _release("v0.2.0")]]
    bump.cfg["trees"][f"{_SOURCE}@v0.2.0"] = ["skills/alpha/SKILL.md"]
    result = bump.run()
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["removed"] == [{"name": "gone", "source": _SOURCE}]
    assert [b["name"] for b in summary["bumped"]] == ["alpha"]
    (npx,) = bump.calls("npx")
    assert npx.count("--skill") == 1 and "gone" not in npx
    assert (bump.root / ".agents/skills/gone/SKILL.md").exists()  # kept on disk


@needs_node
def test_one_cli_call_per_source_with_every_installed_skill(bump: _Bump) -> None:
    for name in ("alpha", "beta"):
        bump.install(name)
    bump.write_lock(
        {"alpha": _lock_entry(ref="v0.1.0"), "beta": _lock_entry(ref="v0.1.0")}
    )
    bump.cfg["releases"][_SOURCE] = [[_release("v0.2.0")]]
    bump.cfg["trees"][f"{_SOURCE}@v0.2.0"] = [
        "skills/alpha/SKILL.md",
        "skills/beta/SKILL.md",
    ]
    result = bump.run()
    assert result.returncode == 0, result.stderr
    (npx,) = bump.calls("npx")
    assert npx == [
        "-y",
        "skills@latest",
        "add",
        f"{_SOURCE}#v0.2.0",
        "--skill",
        "alpha",
        "--skill",
        "beta",
        "-a",
        "universal",
        "-y",
    ]
    assert [b["name"] for b in json.loads(result.stdout)["bumped"]] == ["alpha", "beta"]


@needs_node
def test_claude_code_agent_when_skills_live_under_dot_claude(bump: _Bump) -> None:
    bump.install("alpha", skills_dir=".claude/skills")
    bump.write_lock({"alpha": _lock_entry(ref="v0.1.0")})
    bump.cfg["releases"][_SOURCE] = [[_release("v0.2.0")]]
    bump.cfg["trees"][f"{_SOURCE}@v0.2.0"] = ["skills/alpha/SKILL.md"]
    result = bump.run()
    assert result.returncode == 0, result.stderr
    (npx,) = bump.calls("npx")
    assert npx[npx.index("-a") + 1] == "claude-code"


@needs_node
def test_failed_gh_call_exits_non_zero_with_message(bump: _Bump) -> None:
    bump.install("alpha")
    bump.write_lock({"alpha": _lock_entry(ref="v0.1.0")})
    bump.cfg["fail_gh"] = True
    result = bump.run()
    assert result.returncode != 0
    assert "gh" in result.stderr and "simulated failure" in result.stderr
    assert result.stdout.strip() == ""


@needs_node
def test_failed_npx_call_exits_non_zero_with_message(bump: _Bump) -> None:
    bump.install("alpha")
    bump.write_lock({"alpha": _lock_entry(ref="v0.1.0")})
    bump.cfg["releases"][_SOURCE] = [[_release("v0.2.0")]]
    bump.cfg["trees"][f"{_SOURCE}@v0.2.0"] = ["skills/alpha/SKILL.md"]
    bump.cfg["npx_exit"] = 3
    result = bump.run()
    assert result.returncode != 0
    assert "npx" in result.stderr


# --------------------------------------------------------------------------- #
# update-pr.sh
# --------------------------------------------------------------------------- #

_FAKE_GH_PR = r"""
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
state = os.environ["FAKE_PR_STATE"]  # exists <=> an open PR exists (holds its number)
body = state + ".body"


def flag(name):
    return args[args.index(name) + 1] if name in args else None


if args[:2] == ["pr", "list"]:
    if os.path.exists(state):
        sys.stdout.write(open(state).read().strip() + "\n")
elif args[:2] == ["pr", "create"]:
    open(state, "w").write("7\n")
    open(body, "w").write(flag("--body") or "")
elif args[:2] == ["pr", "edit"]:
    if "--add-label" in args and os.environ.get("FAKE_GH_FAIL_LABEL"):
        sys.stderr.write("could not add label: not found\n")
        sys.exit(1)
    if "--body" in args:
        open(body, "w").write(flag("--body") or "")
elif args[:2] == ["pr", "close"]:
    if os.path.exists(state):
        os.remove(state)
elif args[:2] == ["workflow", "run"]:
    pass
else:
    sys.stderr.write("unexpected gh call: %r\n" % (args,))
    sys.exit(2)
"""

_SUMMARY = {
    "bumped": [
        {
            "name": "alpha",
            "source": _SOURCE,
            "from": "v0.1.0",
            "to": "v0.2.0",
            "notes_url": f"https://github.com/{_SOURCE}/releases/tag/v0.2.0",
        }
    ],
    "available": [{"source": _SOURCE, "name": "newcomer"}],
    "removed": [{"name": "gone", "source": _SOURCE}],
    "no_releases": ["acme/quiet"],
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(  # nosec B603 B607
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


class _Helper:
    """A work tree with a bare ``origin`` and a fake ``gh`` for ``update-pr.sh``."""

    branch = "skills-update"

    def __init__(self, tmp_path: Path) -> None:
        self.origin = tmp_path / "origin.git"
        self.work = tmp_path / "work"
        self.log = tmp_path / "gh.log"
        self.pushes = tmp_path / "pushes.log"
        self.state = tmp_path / "pr-state"
        self.gh = tmp_path / "bin" / "gh"
        _make_fake(tmp_path / "bin", "gh", _FAKE_GH_PR)
        subprocess.run(  # nosec B603 B607
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
            check=True,
        )
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text(f'#!/bin/sh\necho push >> "{self.pushes}"\n')
        hook.chmod(0o755)
        subprocess.run(  # nosec B603 B607
            ["git", "clone", "-q", str(self.origin), str(self.work)],
            check=True,
            capture_output=True,
        )
        for key, value in (
            ("user.name", "Test"),
            ("user.email", "test@example.com"),
            ("commit.gpgsign", "false"),
        ):
            _git(self.work, "config", key, value)
        _git(self.work, "symbolic-ref", "HEAD", "refs/heads/main")
        (self.work / "skills-lock.json").write_text('{"version": 1, "skills": {}}\n')
        skill = self.work / ".agents" / "skills" / "alpha"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# alpha v1\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", "base")
        _git(self.work, "push", "-q", "origin", "main")
        self.pushes.unlink()  # count only the helper's pushes, not the seed push
        self.rebuild_branch()

    def rebuild_branch(self) -> None:
        """Do what the action does before the helper: branch off ``origin/main``."""
        _git(self.work, "fetch", "-q", "origin", "main")
        subprocess.run(  # nosec B603 B607 -- a missing remote branch is fine
            ["git", "fetch", "-q", "origin", self.branch],
            cwd=self.work,
            capture_output=True,
            check=False,
        )
        _git(self.work, "checkout", "-q", "-B", self.branch, "origin/main")

    def make_drift(self, text: str = "# alpha v2\n") -> None:
        (self.work / ".agents/skills/alpha/SKILL.md").write_text(text)

    def run(
        self,
        summary: dict[str, Any] | str | None = None,
        **env_overrides: str,
    ) -> subprocess.CompletedProcess[str]:
        if summary is None:
            summary = _SUMMARY
        env = {
            **os.environ,
            "BRANCH": self.branch,
            "BASE": "main",
            "GH_CMD": str(self.gh),
            "PR_LABELS": "dependencies",
            "DISPATCH_CI": "true",
            "CI_WORKFLOW": "ci.yml",
            "SKILLS_UPDATE_SUMMARY": (
                summary if isinstance(summary, str) else json.dumps(summary)
            ),
            "FAKE_LOG": str(self.log),
            "FAKE_PR_STATE": str(self.state),
            **env_overrides,
        }
        return subprocess.run(  # nosec B603 B607
            ["bash", str(_UPDATE_PR)],
            cwd=self.work,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def gh_calls(self, *prefix: str) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [r for r in rows if r[: len(prefix)] == list(prefix)]

    def push_count(self) -> int:
        if not self.pushes.exists():
            return 0
        return len(self.pushes.read_text().splitlines())

    def remote_branch_exists(self) -> bool:
        probe = subprocess.run(  # nosec B603 B607
            ["git", "rev-parse", "--verify", "-q", f"refs/heads/{self.branch}"],
            cwd=self.origin,
            capture_output=True,
            check=False,
        )
        return probe.returncode == 0

    def pr_body(self) -> str:
        return Path(str(self.state) + ".body").read_text()


@pytest.fixture
def helper(tmp_path: Path) -> _Helper:
    return _Helper(tmp_path)


@needs_bash
@needs_node
def test_no_drift_exits_0_without_push(helper: _Helper) -> None:
    result = helper.run()
    assert result.returncode == 0, result.stderr
    assert "No skill drift" in result.stdout + result.stderr
    assert helper.push_count() == 0
    assert not helper.remote_branch_exists()
    assert helper.gh_calls("pr", "create") == []
    assert helper.gh_calls("workflow") == []


@needs_bash
@needs_node
def test_no_drift_closes_stale_update_pr(helper: _Helper) -> None:
    helper.state.write_text("7\n")
    result = helper.run()
    assert result.returncode == 0, result.stderr
    (close,) = helper.gh_calls("pr", "close")
    assert close[2] == "7"
    assert "--comment" in close and close[close.index("--comment") + 1].strip()
    assert helper.push_count() == 0
    assert helper.gh_calls("pr", "create") == []
    assert not helper.state.exists()


@needs_bash
@needs_node
def test_first_run_pushes_creates_pr_and_dispatches(helper: _Helper) -> None:
    helper.make_drift()
    result = helper.run()
    assert result.returncode == 0, result.stderr
    assert helper.push_count() == 1
    assert helper.remote_branch_exists()
    (create,) = helper.gh_calls("pr", "create")
    assert create[create.index("--title") + 1] == "Update agent skills"
    assert create[create.index("--head") + 1] == "skills-update"
    assert create[create.index("--base") + 1] == "main"
    assert helper.gh_calls("pr", "edit", "7", "--add-label", "dependencies")
    assert helper.gh_calls("workflow", "run", "ci.yml", "--ref", "skills-update")
    # The commit holds exactly the drift, on top of origin/main.
    assert _git(helper.work, "rev-parse", "HEAD^") == _git(
        helper.work, "rev-parse", "origin/main"
    )
    assert _git(
        helper.origin, "show", f"{helper.branch}:.agents/skills/alpha/SKILL.md"
    ) == ("# alpha v2")


@needs_bash
@needs_node
def test_second_identical_run_reuses_pr_and_skips_push_and_dispatch(
    helper: _Helper,
) -> None:
    helper.make_drift()
    assert helper.run().returncode == 0
    helper.log.unlink()
    # The next night: rebuild from origin/main, redo the same update.
    helper.rebuild_branch()
    helper.make_drift()
    result = helper.run()
    assert result.returncode == 0, result.stderr
    assert helper.push_count() == 1  # still just the first run's push
    assert helper.gh_calls("pr", "create") == []
    assert helper.gh_calls("workflow") == []
    edits = helper.gh_calls("pr", "edit", "7", "--body")
    assert len(edits) == 1  # the body is refreshed on the existing PR


@needs_bash
@needs_node
def test_changed_drift_force_pushes_and_dispatches_again(helper: _Helper) -> None:
    helper.make_drift()
    assert helper.run().returncode == 0
    helper.log.unlink()
    helper.rebuild_branch()
    helper.make_drift("# alpha v3\n")
    result = helper.run()
    assert result.returncode == 0, result.stderr
    assert helper.push_count() == 2
    assert helper.gh_calls("pr", "create") == []
    assert helper.gh_calls("workflow", "run", "ci.yml", "--ref", "skills-update")


@needs_bash
@needs_node
def test_missing_label_does_not_block_pr(helper: _Helper) -> None:
    helper.make_drift()
    result = helper.run(FAKE_GH_FAIL_LABEL="1")
    assert result.returncode == 0, result.stderr
    assert "::warning::" in result.stdout + result.stderr
    assert len(helper.gh_calls("pr", "create")) == 1
    assert helper.gh_calls("workflow", "run")  # dispatch still happens


@needs_bash
@needs_node
def test_labels_are_not_passed_to_pr_create(helper: _Helper) -> None:
    helper.make_drift()
    helper.run(PR_LABELS="dependencies skills")
    (create,) = helper.gh_calls("pr", "create")
    assert "--label" not in create
    assert helper.gh_calls("pr", "edit", "7", "--add-label", "skills")


@needs_bash
@needs_node
def test_dispatch_disabled_with_token_path(helper: _Helper) -> None:
    helper.make_drift()
    result = helper.run(DISPATCH_CI="false")
    assert result.returncode == 0, result.stderr
    assert helper.push_count() == 1
    assert len(helper.gh_calls("pr", "create")) == 1
    assert helper.gh_calls("workflow") == []


@needs_bash
@needs_node
def test_body_lists_bumps_available_and_removed(helper: _Helper) -> None:
    helper.make_drift()
    result = helper.run()
    assert result.returncode == 0, result.stderr
    body = helper.pr_body()
    assert "Bumped" in body
    assert "`alpha`" in body and "v0.1.0 → v0.2.0" in body
    assert f"https://github.com/{_SOURCE}/releases/tag/v0.2.0" in body
    assert "Available, not installed" in body and "`newcomer`" in body
    assert "Removed upstream (kept)" in body and "`gone`" in body
    assert "No releases found" in body and "acme/quiet" in body
    assert "never merges itself" in body
    assert "latest `main`" in body


@needs_bash
@needs_node
@pytest.mark.parametrize("summary", ["", "{}"])
def test_body_without_summary_has_only_the_fixed_text(
    helper: _Helper, summary: str
) -> None:
    helper.make_drift()
    result = helper.run(summary=summary)
    assert result.returncode == 0, result.stderr
    body = helper.pr_body()
    assert "never merges itself" in body
    for heading in ("Bumped", "Available", "Removed", "No releases"):
        assert heading not in body


@needs_bash
@needs_node
def test_existing_pr_body_is_refreshed(helper: _Helper) -> None:
    helper.state.write_text("7\n")
    helper.make_drift()
    result = helper.run()
    assert result.returncode == 0, result.stderr
    assert helper.gh_calls("pr", "create") == []
    assert "`alpha`" in helper.pr_body()


@needs_bash
@needs_node
def test_precondition_head_must_be_the_update_branch(helper: _Helper) -> None:
    _git(helper.work, "checkout", "-q", "main")
    result = helper.run()
    assert result.returncode == 1
    assert "skills-update" in result.stderr


@needs_bash
@needs_node
def test_precondition_base_must_be_fetched(helper: _Helper) -> None:
    result = helper.run(BASE="nope")
    assert result.returncode == 1
    assert "origin/nope" in result.stderr


@needs_bash
@needs_node
def test_invalid_summary_json_is_an_error(helper: _Helper) -> None:
    helper.make_drift()
    result = helper.run(summary="not json")
    assert result.returncode == 1
    assert "SKILLS_UPDATE_SUMMARY" in result.stderr


# --------------------------------------------------------------------------- #
# action.yml wiring
# --------------------------------------------------------------------------- #


def test_action_is_a_composite_with_the_documented_inputs() -> None:
    text = (_ACTION / "action.yml").read_text()
    assert "using: composite" in text
    for name in ("token", "dispatch-ci", "branch", "base", "ci-workflow", "labels"):
        assert f"\n  {name}:" in text, name
    assert "skills@latest update -p -y" in text
    assert 'DISABLE_TELEMETRY: "1"' in text
    assert "actions/checkout" not in text  # the caller checks the repo out
