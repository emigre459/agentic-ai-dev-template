"""Tests for the upgrade-deps skill's sweep script, ``update_prs.py``.

Sweep mode gives every open update PR (Dependabot bumps and the nightly skills
update PR) exactly one action: ``FOLD``, ``CLOSE_SUPERSEDED``, ``CLOSE_ROUTINE`` or
``REVIEW``. ``classify`` is pure; ``collect`` and the CLI go through an injected
runner, stubbed here with a fake ``gh``/``git``, and the recheck wait runs on a fake
clock. Nothing here touches the network.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / ".agents" / "skills" / "upgrade-deps" / "scripts" / "update_prs.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("update_prs", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["update_prs"] = module
    spec.loader.exec_module(module)
    return module


update_prs = _load()

PY_LOCK = "stacks/python/uv.lock"
BUN_LOCK = "stacks/react/bun.lock"


def _pr(
    number: int,
    title: str,
    *,
    ecosystem: str = "uv",
    lockfile: str | None = PY_LOCK,
    body: str = "",
    files: list[str] | None = None,
    changes_main: bool | None = None,
    kind: str = "dependabot",
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "kind": kind,
        "ecosystem": ecosystem,
        "lockfile": lockfile,
        "files": files if files is not None else ([lockfile] if lockfile else []),
        "changes_main": changes_main,
    }


def _alert(
    package: str,
    patched: str | None,
    *,
    ecosystem: str = "pip",
    lockfiles: tuple[str, ...] = (PY_LOCK,),
    severity: str = "high",
    ghsa: str = "GHSA-aaaa-bbbb-cccc",
) -> Any:
    return update_prs.Alert(
        ecosystem=ecosystem,
        package=package,
        severity=severity,
        ghsa=ghsa,
        patched=patched,
        lockfiles=lockfiles,
    )


def _alerts(*alerts: Any) -> dict[tuple[str, str], Any]:
    return {(a.ecosystem, a.package): a for a in alerts}


def _grouped_body(*members: tuple[str, str, str]) -> str:
    lines = [
        f"Bumps the python-dev group with {len(members)} updates.",
        "",
    ]
    lines += [f"Updates `{pkg}` from {frm} to {to}" for pkg, frm, to in members]
    return "\n".join(lines)


GROUP_TITLE = "deps: bump the python-dev group in /stacks/python with 2 updates"
DIRECT = {
    PY_LOCK: {"requests": update_prs.Floor("stacks/python/pyproject.toml", ">=", "2.0")}
}


# --- classification ---------------------------------------------------------------


def test_security_bump_is_fold() -> None:
    pr = _pr(5, "deps: bump requests from 2.31.0 to 2.32.0 in /stacks/python")
    action, reason = update_prs.classify(
        pr, _alerts(_alert("requests", "2.32.0")), {(PY_LOCK, "requests"): "2.31.0"}
    )
    assert action == "FOLD"
    assert "GHSA-aaaa-bbbb-cccc" in reason


def test_superseded_is_close_superseded() -> None:
    pr = _pr(5, "deps: bump requests from 2.31.0 to 2.32.0")
    action, _ = update_prs.classify(pr, {}, {(PY_LOCK, "requests"): "2.32.1"})
    assert action == "CLOSE_SUPERSEDED"


def test_a_stale_alert_main_already_fixed_does_not_fold() -> None:
    """Dependabot closes alerts late; main locking the patched version wins."""
    pr = _pr(5, "deps: bump requests from 2.31.0 to 2.33.0")
    action, _ = update_prs.classify(
        pr,
        _alerts(_alert("requests", "2.32.0")),
        {(PY_LOCK, "requests"): "2.32.0"},
    )
    assert action == "CLOSE_ROUTINE"


def test_routine_minor_major_only_policy_is_close_routine() -> None:
    """Under the major-only policy a minor bump with no advisory is a leftover."""
    pr = _pr(6, "deps: bump requests from 2.31.0 to 2.32.0")
    action, reason = update_prs.classify(pr, {}, {(PY_LOCK, "requests"): "2.31.0"})
    assert action == "CLOSE_ROUTINE"
    assert "advisory" in reason


def test_indirect_major_without_advisory_is_close_routine() -> None:
    """Majors come for direct dependencies only; an indirect one is a leftover."""
    pr = _pr(6, "deps: bump idna from 2.10 to 3.7")
    action, reason = update_prs.classify(
        pr, {}, {(PY_LOCK, "idna"): "2.10"}, direct=DIRECT
    )
    assert action == "CLOSE_ROUTINE"
    assert "indirect" in reason


def test_grouped_minor_patch_without_advisory_is_close_routine() -> None:
    body = _grouped_body(("requests", "2.31.0", "2.32.0"), ("ruff", "0.6.1", "0.6.4"))
    pr = _pr(7, GROUP_TITLE, body=body)
    versions = {(PY_LOCK, "requests"): "2.31.0", (PY_LOCK, "ruff"): "0.6.1"}
    action, _ = update_prs.classify(pr, {}, versions)
    assert action == "CLOSE_ROUTINE"


def test_grouped_with_one_advisory_member_folds_that_member_alone() -> None:
    """Review Focus 5: never close an advisory fix as routine with its group."""
    body = _grouped_body(("requests", "2.31.0", "2.32.0"), ("ruff", "0.6.1", "0.6.4"))
    pr = _pr(7, GROUP_TITLE, body=body)
    versions = {(PY_LOCK, "requests"): "2.31.0", (PY_LOCK, "ruff"): "0.6.1"}
    alerts = _alerts(_alert("requests", "2.32.0"))

    action, reason = update_prs.classify(pr, alerts, versions)
    assert action == "FOLD"
    assert "requests" in reason

    plan = update_prs.build_plan(
        [pr], alerts, versions, alerts_available=True, direct=DIRECT
    )
    [decision] = [d for d in plan["decisions"] if d["pr"] == 7]
    assert decision["fold_commands"] == [
        "(cd stacks/python && uv lock --upgrade-package requests==2.32.0)"
    ]
    assert not any("ruff" in c for c in decision["fold_commands"])
    assert 7 not in plan["close_now"]
    assert plan["close_after_merge"] == [7]
    # The member's alert is covered by the grouped PR: no second, PR-less fold.
    assert [d["pr"] for d in plan["decisions"] if d["action"] == "FOLD"] == [7]


def test_grouped_with_major_is_review() -> None:
    body = _grouped_body(("requests", "2.31.0", "3.0.0"), ("ruff", "0.6.1", "0.6.4"))
    pr = _pr(8, GROUP_TITLE, body=body)
    versions = {(PY_LOCK, "requests"): "2.31.0", (PY_LOCK, "ruff"): "0.6.1"}
    action, reason = update_prs.classify(pr, {}, versions)
    assert action == "REVIEW"
    assert "major" in reason


def test_major_without_advisory_is_review() -> None:
    pr = _pr(9, "deps: bump requests from 2.31.0 to 3.0.0")
    action, reason = update_prs.classify(
        pr, {}, {(PY_LOCK, "requests"): "2.31.0"}, direct=DIRECT
    )
    assert action == "REVIEW"
    assert "major" in reason


def test_unreadable_alerts_never_close_as_routine() -> None:
    pr = _pr(6, "deps: bump requests from 2.31.0 to 2.32.0")
    action, _ = update_prs.classify(
        pr, {}, {(PY_LOCK, "requests"): "2.31.0"}, alerts_available=False
    )
    assert action == "REVIEW"


def test_bun_branch_maps_to_npm_alerts_and_bun_update_command() -> None:
    assert update_prs.ALERT_ECOSYSTEM["bun"] == "npm"
    assert update_prs.ALERT_ECOSYSTEM["npm_and_yarn"] == "npm"
    pr = _pr(
        11,
        "deps: bump undici from 7.18.0 to 7.30.1 in /stacks/react",
        ecosystem="bun",
        lockfile=BUN_LOCK,
    )
    alerts = _alerts(_alert("undici", "7.30.1", ecosystem="npm", lockfiles=(BUN_LOCK,)))
    versions = {(BUN_LOCK, "undici"): "7.18.0"}
    assert update_prs.classify(pr, alerts, versions)[0] == "FOLD"
    plan = update_prs.build_plan(
        [pr], alerts, versions, alerts_available=True, direct={}
    )
    [decision] = plan["decisions"]
    assert decision["fold_commands"] == [
        "(cd stacks/react && bun update undici@7.30.1)"
    ]


def test_an_alert_with_no_pr_is_folded_in_its_lockfile() -> None:
    alerts = _alerts(_alert("idna", "3.7"))
    plan = update_prs.build_plan(
        [], alerts, {(PY_LOCK, "idna"): "3.6"}, alerts_available=True, direct=DIRECT
    )
    [decision] = plan["decisions"]
    assert (decision["kind"], decision["action"], decision["pr"]) == (
        "alert",
        "FOLD",
        None,
    )
    assert decision["fold_commands"] == [
        "(cd stacks/python && uv lock --upgrade-package idna==3.7)"
    ]


def test_a_direct_dependency_fold_carries_the_floor_note() -> None:
    pr = _pr(5, "deps: bump requests from 2.31.0 to 2.32.0")
    alerts = _alerts(_alert("requests", "2.32.0"))
    plan = update_prs.build_plan(
        [pr],
        alerts,
        {(PY_LOCK, "requests"): "2.31.0"},
        alerts_available=True,
        direct=DIRECT,
    )
    [note] = plan["decisions"][0]["floor_notes"]
    assert "stacks/python/pyproject.toml" in note and ">=2.32.0" in note


def test_skills_pr_outside_the_skills_tree_is_review() -> None:
    pr = _pr(
        12,
        "Update agent skills",
        kind="skills",
        ecosystem="",
        lockfile=None,
        files=["skills-lock.json", ".github/workflows/ci.yml"],
        changes_main=True,
    )
    action, reason = update_prs.classify(pr, {}, {})
    assert action == "REVIEW"
    assert ".github/workflows/ci.yml" in reason


def test_skills_pr_confined_to_the_skills_tree_is_fold() -> None:
    pr = _pr(
        12,
        "Update agent skills",
        kind="skills",
        ecosystem="",
        lockfile=None,
        files=["skills-lock.json", ".agents/skills/x/SKILL.md"],
        changes_main=True,
    )
    assert update_prs.classify(pr, {}, {})[0] == "FOLD"


# --- version parsing --------------------------------------------------------------


def test_version_parser_orders_prereleases_and_post() -> None:
    parse = update_prs.parse_version
    ordered = [
        "1.0a1",
        "1.0b2",
        "1.0rc1",
        "1.0",
        "1.0.post1",
        "1.1",
        "2.0.0-beta.2",
        "2.0.0-beta.10",
        "2.0.0",
    ]
    versions = [parse(v) for v in ordered]
    assert all(v is not None for v in versions)
    assert versions == sorted(versions)
    assert parse("1.0") == parse("1.0.0")
    assert parse("v4.2.1") == parse("4.2.1")
    assert parse("not-a-version") is None
    assert parse("3.1.0").release == (3, 1, 0)


@pytest.mark.parametrize(
    ("frm", "to", "major"),
    [
        ("2.31.0", "3.0.0", True),
        ("0.6.1", "0.7.0", True),
        ("0.6.1", "0.6.4", False),
        ("4", "5", True),
        ("2.1", "2.9", False),
    ],
)
def test_is_major(frm: str, to: str, major: bool) -> None:
    assert update_prs.is_major(frm, to) is major


def test_pypi_names_normalize_per_pep_503() -> None:
    assert update_prs.normalize_name("pip", "Typing_Extensions") == "typing-extensions"
    assert update_prs.normalize_name("npm", "lodash.merge") == "lodash.merge"


# --- repo config ------------------------------------------------------------------

DEPENDABOT_BUN_ONLY = """\
version: 2
updates:
  # bun-managed stack
  - package-ecosystem: "bun"
    directory: "/stacks/react"
    schedule:
      interval: "weekly"
    groups:
      react-dev:
        update-types: ["minor", "patch"]
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
"""


def test_projects_from_dependabot_directories_not_root_pyproject(
    tmp_path: Path,
) -> None:
    assert update_prs.dependabot_projects(DEPENDABOT_BUN_ONLY) == [
        update_prs.Project("bun", "stacks/react"),
        update_prs.Project("github-actions", "."),
    ]
    assert update_prs.lockfiles_of(
        update_prs.dependabot_projects(DEPENDABOT_BUN_ONLY)
    ) == [BUN_LOCK]

    undici = _row(
        40,
        "deps: bump undici from 7.18.0 to 7.30.1 in /stacks/react",
        "dependabot/bun/stacks/react/undici-7.30.1",
        [BUN_LOCK, "stacks/react/package.json"],
    )
    gh = FakeGh(
        files={".github/dependabot.yml": DEPENDABOT_BUN_ONLY, BUN_LOCK: BUN_LOCK_TEXT},
        prs=[undici],
    )
    snapshot = update_prs.collect(gh, tmp_path)
    assert snapshot["lockfiles"] == [BUN_LOCK]
    assert snapshot["prs"][0]["lockfile"] == BUN_LOCK
    shown = [c[2].split(":", 1)[1] for c in gh.calls if c[:2] == ["git", "show"]]
    assert BUN_LOCK in shown
    assert "pyproject.toml" not in shown and "uv.lock" not in shown
    assert snapshot["main_versions"][BUN_LOCK]["undici"] == "7.18.0"


def test_the_shipped_dependabot_config_yields_both_stacks() -> None:
    text = (_REPO / ".github" / "dependabot.yml").read_text()
    lockfiles = update_prs.lockfiles_of(update_prs.dependabot_projects(text))
    assert {"stacks/python/uv.lock", "stacks/react/bun.lock"} <= set(lockfiles)


def test_skills_update_branch_read_from_action(tmp_path: Path) -> None:
    assert update_prs.skills_branch(_REPO) == "skills-update"

    action = tmp_path / ".github" / "actions" / "skills-update" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text(
        (_REPO / ".github/actions/skills-update/action.yml")
        .read_text()
        .replace("default: skills-update", "default: bots/skills")
    )
    assert update_prs.skills_branch(tmp_path) == "bots/skills"


def test_skills_branch_from_a_workflow_override(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "skills-update.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  update:\n    steps:\n"
        "      - uses: ./.github/actions/skills-update\n"
        "        with:\n          token: x\n          branch: chore/skills\n"
    )
    assert update_prs.skills_branch(tmp_path) == "chore/skills"


def test_skills_branch_from_the_released_action(tmp_path: Path) -> None:
    """A consumer repo has no local action: read the pinned release's default."""
    workflow = tmp_path / ".github" / "workflows" / "skills-update.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  update:\n    steps:\n"
        "      - uses: owner/template/.github/actions/skills-update@v1.2.3\n"
        "        with:\n          token: x\n"
    )
    calls: list[Sequence[str]] = []

    def runner(argv: Sequence[str]) -> str:
        calls.append(list(argv))
        return (_REPO / ".github/actions/skills-update/action.yml").read_text()

    assert update_prs.skills_branch(tmp_path, runner) == "skills-update"
    [argv] = calls
    assert (
        "repos/owner/template/contents/.github/actions/skills-update/action.yml?ref=v1.2.3"
        in argv
    )
    with pytest.raises(LookupError):
        update_prs.skills_branch(tmp_path)


# --- CLI: close and recheck (fake gh/git) -----------------------------------------

UV_LOCK_TEXT = """\
version = 1

[[package]]
name = "requests"
version = "{requests}"

[[package]]
name = "ruff"
version = "0.6.1"
"""

BUN_LOCK_TEXT = """\
{
  "lockfileVersion": 1,
  "packages": {
    "undici": ["undici@7.18.0", "", {}, "sha512-c"],
  }
}
"""

DEPENDABOT_PY = """\
version: 2
updates:
  - package-ecosystem: "uv"
    directory: "/stacks/python"
"""

PYPROJECT = '[project]\nname = "x"\ndependencies = ["requests>=2.0"]\n'


def _row(number: int, title: str, head: str, files: list[str]) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": "",
        "headRefName": head,
        "isCrossRepository": False,
        "files": [{"path": f} for f in files],
    }


def _raw_alert(
    package: str, patched: str, manifest: str = "stacks/python/uv.lock"
) -> dict[str, Any]:
    return {
        "number": 1,
        "dependency": {
            "manifest_path": manifest,
            "package": {"ecosystem": "pip", "name": package},
        },
        "security_advisory": {"severity": "high", "ghsa_id": "GHSA-x", "summary": "s"},
        "security_vulnerability": {
            "vulnerable_version_range": f"< {patched}",
            "first_patched_version": {"identifier": patched},
        },
    }


class FakeGh:
    """A stand-in for ``gh`` and ``git``: canned answers, every call recorded."""

    def __init__(
        self,
        *,
        files: dict[str, str] | None = None,
        prs: list[dict[str, Any]] | None = None,
        alerts: list[dict[str, Any]] | None = None,
        fold_state: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.files = files or {}
        self.prs = prs or []
        self.alerts = alerts or []
        self.fold_state = fold_state
        self.calls: list[list[str]] = []
        self.closed: list[int] = []

    def __call__(self, argv: Sequence[str]) -> str:
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["git", "fetch"]:
            return ""
        if argv[:2] == ["git", "rev-parse"]:
            return "tree0\n" if argv[2].endswith("^{tree}") else "0123456789abcdef\n"
        if argv[:2] == ["git", "show"]:
            path = argv[2].split(":", 1)[1]
            if path not in self.files:
                raise update_prs.GhError(f"fatal: path '{path}' does not exist")
            return self.files[path]
        if argv[:2] == ["git", "merge-tree"]:
            return "tree1\n"
        if argv[:3] == ["git", "update-ref", "-d"]:
            return ""
        if argv[:3] == ["gh", "repo", "view"]:
            return json.dumps({"nameWithOwner": "o/r"})
        if argv[:3] == ["gh", "pr", "list"]:
            return json.dumps([p for p in self.prs if p["number"] not in self.closed])
        if argv[:2] == ["gh", "api"]:
            return "\n".join(json.dumps(a) for a in self.alerts)
        if argv[:3] == ["gh", "pr", "view"]:
            assert self.fold_state is not None
            return json.dumps(self.fold_state())
        if argv[:3] == ["gh", "pr", "close"]:
            assert "--comment" in argv, "close with a comment, never silently"
            self.closed.append(int(argv[3]))
            return ""
        raise AssertionError(f"unexpected command {argv}")


def _py_repo(
    requests_on_main: str,
    prs: list[dict[str, Any]],
    alerts: list[dict[str, Any]] | None = None,
    **kw: Any,
) -> FakeGh:
    return FakeGh(
        files={
            ".github/dependabot.yml": DEPENDABOT_PY,
            PY_LOCK: UV_LOCK_TEXT.format(requests=requests_on_main),
            "stacks/python/pyproject.toml": PYPROJECT,
        },
        prs=prs,
        alerts=alerts,
        **kw,
    )


ROUTINE = _row(
    20,
    "deps: bump ruff from 0.6.1 to 0.6.4",
    "dependabot/uv/stacks/python/ruff-0.6.4",
    [PY_LOCK],
)
SECURITY = _row(
    21,
    "deps: bump requests from 2.31.0 to 2.32.0",
    "dependabot/uv/stacks/python/requests-2.32.0",
    [PY_LOCK],
)


def test_plan_prints_the_action_table(tmp_path: Path, capsys: Any) -> None:
    gh = _py_repo("2.31.0", [ROUTINE, SECURITY], [_raw_alert("requests", "2.32.0")])
    assert update_prs.main(["--root", str(tmp_path), "plan"], runner=gh) == 0
    out = capsys.readouterr().out
    assert "| #21 |" in out and "FOLD" in out
    assert "| #20 |" in out and "CLOSE_ROUTINE" in out
    assert not [c for c in gh.calls if c[:3] == ["gh", "pr", "close"]]


def test_close_refuses_pr_whose_state_changed(tmp_path: Path, capsys: Any) -> None:
    """#21 gained an advisory since the plan, #22 is no longer open: both refused."""
    gh = _py_repo("2.31.0", [ROUTINE, SECURITY], [_raw_alert("requests", "2.32.0")])
    code = update_prs.main(
        ["--root", str(tmp_path), "close", "--prs", "20", "21", "22", "--execute"],
        runner=gh,
    )
    assert code == 1
    assert gh.closed == [20]
    err = capsys.readouterr().err
    assert "refusing #21" in err and "refusing #22" in err


def test_close_without_execute_is_a_dry_run(tmp_path: Path, capsys: Any) -> None:
    gh = _py_repo("2.31.0", [ROUTINE])
    assert (
        update_prs.main(["--root", str(tmp_path), "close", "--prs", "20"], runner=gh)
        == 0
    )
    assert gh.closed == []
    assert "[dry run] would close #20" in capsys.readouterr().out


MERGED_AT = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now_at = start
        self.paused: list[float] = []

    def now(self) -> datetime:
        return self.now_at

    def pause(self, seconds: float) -> None:
        self.paused.append(seconds)
        self.now_at += timedelta(seconds=seconds)


@pytest.mark.parametrize(
    ("fold", "requests_on_main", "expected_code", "expected_closed"),
    [
        ({"state": "MERGED", "mergedAt": "2026-11-17T10:00:00Z"}, "2.32.0", 0, [21]),
        ({"state": "OPEN", "mergedAt": None}, "2.31.0", 2, []),
        ({"state": "CLOSED", "mergedAt": None}, "2.31.0", 3, []),
        ({"state": "MERGED", "mergedAt": "2026-11-17T10:00:00Z"}, "2.31.0", 4, []),
    ],
    ids=[
        "done",
        "not-merged-before-timeout",
        "closed-unmerged",
        "main-does-not-satisfy",
    ],
)
def test_recheck_exit_codes(
    tmp_path: Path,
    fold: dict[str, Any],
    requests_on_main: str,
    expected_code: int,
    expected_closed: list[int],
) -> None:
    clock = FakeClock(MERGED_AT)
    gh = _py_repo(requests_on_main, [SECURITY], fold_state=lambda: fold)
    code = update_prs.main(
        [
            "--root",
            str(tmp_path),
            "recheck",
            "--fold-pr",
            "30",
            "--prs",
            "21",
            "--execute",
            "--timeout",
            "120",
            "--grace",
            "60",
        ],
        runner=gh,
        now=clock.now,
        pause=clock.pause,
    )
    assert code == expected_code
    assert gh.closed == expected_closed
    # The only raw times are the timeout and grace: every pause is a slice of them.
    assert clock.now_at - MERGED_AT <= timedelta(seconds=120 + 60)


def test_recheck_stops_early_once_github_closes_everything(tmp_path: Path) -> None:
    clock = FakeClock(MERGED_AT)
    gh = _py_repo(
        "2.32.0",
        [],
        fold_state=lambda: {"state": "MERGED", "mergedAt": "2026-11-17T10:00:00Z"},
    )
    code = update_prs.main(
        [
            "--root",
            str(tmp_path),
            "recheck",
            "--fold-pr",
            "30",
            "--prs",
            "21",
            "--execute",
        ],
        runner=gh,
        now=clock.now,
        pause=clock.pause,
    )
    assert code == 0
    assert clock.paused == []


def test_default_waits_fit_a_two_hour_background_call() -> None:
    assert update_prs.DEFAULT_TIMEOUT_S + update_prs.DEFAULT_GRACE_S < 7200


# --- fail closed: unmapped ecosystems and partly parsed groups --------------------


def test_unmapped_ecosystem_is_review_not_routine() -> None:
    """Cargo alerts say ``rust``: an unmapped segment can't see its advisories."""
    pr = _pr(
        50,
        "deps: bump serde from 1.0.0 to 1.0.5",
        ecosystem="cargo",
        lockfile=None,
        files=["Cargo.lock"],
        changes_main=True,
    )
    alerts = _alerts(_alert("serde", "1.0.5", ecosystem="rust", lockfiles=()))
    action, reason = update_prs.classify(pr, alerts, {})
    assert action == "REVIEW"
    assert "cargo" in reason


def test_grouped_body_with_fewer_members_than_the_title_is_review() -> None:
    """A member in another form (or a truncated body) must not close as routine."""
    body = _grouped_body(("requests", "2.31.0", "2.32.0")) + (
        "\nUpdates the requirements on [ruff](https://example.invalid) "
        "to permit the latest version."
    )
    pr = _pr(51, GROUP_TITLE, body=body)
    action, reason = update_prs.classify(pr, {}, {(PY_LOCK, "requests"): "2.31.0"})
    assert action == "REVIEW"
    assert "2" in reason and "1" in reason


def test_grouped_title_without_a_count_is_review() -> None:
    body = _grouped_body(("requests", "2.31.0", "2.32.0"))
    pr = _pr(52, "deps: bump the python-dev group in /stacks/python", body=body)
    action, _ = update_prs.classify(pr, {}, {(PY_LOCK, "requests"): "2.31.0"})
    assert action == "REVIEW"


# Before 3.11 there is no ``datetime.UTC``; the script must still import there so its
# own interpreter check can say "needs Python 3.11+" instead of a traceback.
_PRE_311 = """
import datetime, runpy, sys
del datetime.UTC
sys.modules["tomllib"] = None  # import tomllib fails, as before 3.11
sys.argv = [sys.argv[1], "plan"]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def test_an_older_interpreter_gets_the_version_message_not_a_traceback() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PRE_311, str(_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 64, result.stderr
    assert "needs Python 3.11+" in result.stderr
    assert "Traceback" not in result.stderr


# --- _changes_main: the PR head goes into a private ref that is deleted after ---


class RefRunner:
    """Answers only what ``_changes_main`` runs; records every call."""

    def __init__(self, *, conflict: bool = False, delete_fails: bool = False) -> None:
        self.conflict = conflict
        self.delete_fails = delete_fails
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> str:
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["git", "fetch"]:
            return ""
        if argv[:2] == ["git", "merge-tree"]:
            if self.conflict:
                raise update_prs.GhError("merge conflict")
            return "tree1\n"
        if argv[:2] == ["git", "rev-parse"]:
            return "tree0\n"
        if argv[:3] == ["git", "update-ref", "-d"]:
            if self.delete_fails:
                raise update_prs.GhError("cannot lock ref")
            return ""
        raise AssertionError(f"unexpected command {argv}")


_PRIVATE_REF = "refs/upgrade-deps/pr-7"
_DELETE = ["git", "update-ref", "-d", _PRIVATE_REF]


def test_changes_main_fetches_into_a_private_ref_and_deletes_it() -> None:
    runner = RefRunner()
    assert update_prs._changes_main(runner, "refs/remotes/origin/main", 7) is True
    assert runner.calls[0][-1] == f"+refs/pull/7/head:{_PRIVATE_REF}"
    assert runner.calls[-1] == _DELETE
    assert not any("refs/remotes/origin/pr-" in " ".join(c) for c in runner.calls)


def test_changes_main_deletes_the_ref_when_merge_tree_conflicts() -> None:
    runner = RefRunner(conflict=True)
    assert update_prs._changes_main(runner, "refs/remotes/origin/main", 7) is None
    assert runner.calls[-1] == _DELETE


def test_changes_main_tolerates_a_failed_ref_delete() -> None:
    runner = RefRunner(delete_fails=True)
    assert update_prs._changes_main(runner, "refs/remotes/origin/main", 7) is True
    assert runner.calls[-1] == _DELETE
