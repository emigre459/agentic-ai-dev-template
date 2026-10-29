"""Tests for resolve-pr-concerns' ``wait_for_pr_checks.py`` (Step 6's CI settle).

The script names no PR reviewer: its default expected bot, its soft-pending
("external reviewer") set and its post-settle review-of-HEAD wait all come from
``.github/pr-reviewer.json``. Most tests drive ``main`` in-process with
``subprocess.run`` patched onto the stand-in ``gh`` in
``fixtures/wait_for_pr_checks/fake_gh.py`` (so ``reviewer_state._gh_json``'s real
page flattening runs), a fake clock and a fake ``sleep``; a few run the script as a
real process against the same fake installed as ``gh`` on ``PATH``. Nothing sleeps
for real: in-process sleeps are recorded, and the process tests pass
``--timeout-min 0`` (one poll), ``MERGESTATE_POLL_SEC=0`` and no ``<owner>/<repo>``
(which skips the post-settle poll).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess  # nosec B404 -- fixed argv, in-repo script, no shell
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / ".agents" / "skills" / "resolve-pr-concerns" / "scripts"
_SCRIPT = _SCRIPTS / "wait_for_pr_checks.py"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "wait_for_pr_checks"
_CONFIG = _REPO / ".github" / "pr-reviewer.json"


def _load(name: str, folder: Path) -> ModuleType:
    """Load a standalone script as a module, registered by its name."""
    spec = importlib.util.spec_from_file_location(name, folder / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reviewer_state = _load("reviewer_state", _SCRIPTS)
wait = _load("wait_for_pr_checks", _SCRIPTS)
fake_gh = _load("fake_gh", _FIXTURES)

REPO = "owner/repo"
HEAD = "0123456789abcdef0123456789abcdef01234567"
OLD = "fedcba9876543210fedcba9876543210fedcba98"
REVIEWER_BOT = "coderabbitai[bot]"
LINT_PASS = {"name": "lint", "bucket": "pass", "state": "SUCCESS", "link": ""}


def _check(name: str, bucket: str) -> dict[str, str]:
    return {"name": name, "bucket": bucket, "state": bucket.upper(), "link": ""}


def _pending(name: str) -> dict[str, str]:
    return _check(name, "pending")


def _config(tmp_path: Path, active: str | None = "coderabbit", **fields: Any) -> Path:
    """Write the shipped config with ``active`` set; ``fields`` edit that adapter.

    A field set to ``...`` is deleted.
    """
    config = json.loads(_CONFIG.read_text())
    config["active"] = active
    for field, value in fields.items():
        if active is None:
            raise ValueError("fields edit the active adapter; none is active")
        if value is ...:
            del config["reviewers"][active][field]
        else:
            config["reviewers"][active][field] = value
    path = tmp_path / "pr-reviewer.json"
    path.write_text(json.dumps(config))
    return path


class Result(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


class Harness:
    """Runs ``main`` in-process against the fake ``gh``, clock and sleep."""

    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self.capsys = capsys
        self.scenario: dict[str, Any] = {"checks": [LINT_PASS], "repo": REPO}
        self.gh_state: dict[str, Any] = {}
        self.calls: list[list[str]] = []
        self.clock = 1000.0
        self.sleeps: list[float] = []
        # None: reviewer_state can't be read (the hermetic default).
        self.state: dict[str, Any] | None = None
        self.state_calls: list[tuple[str, int]] = []

    def subprocess_run(
        self, argv: Sequence[str], *_: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        if argv[0] != "gh":
            return subprocess.CompletedProcess(argv, 128, "", "not a git repository")
        self.calls.append(argv[1:])
        code, out, err = fake_gh.answer(self.scenario, argv[1:], self.gh_state)
        if kwargs.get("check") and code:
            raise subprocess.CalledProcessError(code, argv, out, err)
        return subprocess.CompletedProcess(argv, code, out, err)

    def now(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds

    def read_state(
        self, repo: str, pr: int, adapter: Any, config: Path
    ) -> dict[str, Any]:
        self.state_calls.append((repo, pr))
        if self.state is None:
            raise subprocess.CalledProcessError(1, ["gh"], "", "unreadable")
        return self.state

    def run(self, *args: str, config: Path | None = None) -> Result:
        config_args = [] if config is None else ["--config", str(config)]
        code = wait.main(["1", *config_args, "--timeout-min", "0", *args])
        out = self.capsys.readouterr()
        return Result(code, out.out, out.err)

    def gh_calls(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


@pytest.fixture
def harness(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> Harness:
    h = Harness(capsys)
    monkeypatch.setattr(subprocess, "run", h.subprocess_run)
    monkeypatch.setattr(wait, "_now", h.now)
    monkeypatch.setattr(wait, "_sleep", h.sleep)
    monkeypatch.setattr(wait, "_read_reviewer_state", h.read_state)
    monkeypatch.delenv("MERGESTATE_POLL_SEC", raising=False)
    # Not a git repo: the default config is <tmp_path>/.github/pr-reviewer.json,
    # absent unless a test writes it.
    monkeypatch.chdir(tmp_path)
    return h


def _cli(
    tmp_path: Path, scenario: dict[str, Any], *args: str
) -> subprocess.CompletedProcess[str]:
    """Run the script as a process with the fake ``gh`` first on ``PATH``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{_FIXTURES / "fake_gh.py"}" "$@"\n'
    )
    gh.chmod(0o755)
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_SCENARIO": str(scenario_path),
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "MERGESTATE_POLL_SEC": "0",
    }
    # cwd = tmp_path (not a git repo), so the default config path is
    # <tmp_path>/.github/pr-reviewer.json -- absent unless a test writes it.
    return subprocess.run(  # nosec B603 -- fixed argv, stub gh, no shell
        [sys.executable, str(_SCRIPT), "1", "--timeout-min", "0", *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )


def _logged_calls(tmp_path: Path) -> list[list[str]]:
    log = tmp_path / "gh.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


# --- the script itself --------------------------------------------------------


def test_the_script_is_an_executable_python3_script() -> None:
    assert _SCRIPT.read_text().startswith("#!/usr/bin/env python3\n")
    assert os.access(_SCRIPT, os.X_OK)
    assert not (_SCRIPTS / "wait_for_pr_checks.sh").exists()


def test_help_exits_zero(tmp_path: Path) -> None:
    result = _cli(tmp_path, {}, "--help")
    assert result.returncode == 0
    assert "Exit codes" in result.stdout


# --- reviewer config (ported from the bash script's tests) --------------------


def test_missing_explicit_config_fails_hard(tmp_path: Path) -> None:
    """An explicit --config that doesn't exist is a typo -- fail, don't guess."""
    result = _cli(tmp_path, {"checks": [LINT_PASS]}, "--config", "absent.json")
    assert result.returncode == 64
    assert "pr-reviewer" in result.stderr
    assert _logged_calls(tmp_path) == []


def test_repo_without_a_reviewer_config_still_settles(tmp_path: Path) -> None:
    """resolve-pr-concerns Step 1b supports repos with no reviewer configured.

    With no default config the script must still settle CI (no expected reviewer
    bot), and say so, rather than exit before polling anything.
    """
    result = _cli(tmp_path, {"checks": [LINT_PASS]}, "--bot-window-sec", "600")
    assert result.returncode == 0, result.stderr
    assert "no PR reviewer configured" in result.stderr
    assert "not yet registered" not in result.stderr
    assert "Traceback" not in result.stderr


def test_no_active_reviewer_waits_on_ci_only(harness: Harness, tmp_path: Path) -> None:
    """``"active": null`` settles CI with no expected reviewer bot, like no config."""
    config = _config(tmp_path, active=None)
    result = harness.run("--bot-window-sec", "600", config=config)
    assert result.returncode == 0, result.stderr
    assert "no PR reviewer active" in result.stderr
    assert "not yet registered" not in result.stderr


def test_no_active_reviewer_still_treats_adapter_checks_as_soft(
    harness: Harness, tmp_path: Path
) -> None:
    """A still-installed reviewer app's pending check must not block the wait."""
    harness.scenario["checks"] = [LINT_PASS, _pending("Cursor Bugbot")]
    config = _config(tmp_path, active=None)
    result = harness.run("--bot-window-sec", "0", config=config)
    assert result.returncode == 0, result.stderr


def test_default_config_is_used_when_present(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    config = json.loads(_CONFIG.read_text())
    config["active"] = "coderabbit"
    (tmp_path / ".github" / "pr-reviewer.json").write_text(json.dumps(config))
    result = _cli(tmp_path, {"checks": [LINT_PASS]}, "--bot-window-sec", "600")
    assert result.returncode == 2
    assert "CodeRabbit (not yet registered)" in result.stderr


def test_invalid_default_config_fails_hard(harness: Harness, tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "pr-reviewer.json").write_text("{not json")
    result = harness.run()
    assert result.returncode == 64
    assert harness.calls == []


def test_active_reviewer_without_check_name_fails_hard(
    harness: Harness, tmp_path: Path
) -> None:
    result = harness.run(config=_config(tmp_path, check_name=...))
    assert result.returncode == 64
    assert "check_name" in result.stderr
    assert harness.calls == []


@pytest.mark.parametrize("logins", [[], [""], "coderabbitai", [1], None])
def test_empty_bot_logins_is_a_usage_error(
    harness: Harness, tmp_path: Path, logins: Any
) -> None:
    """A login list that could never match a review would skip the HEAD wait."""
    result = harness.run(config=_config(tmp_path, bot_logins=logins))
    assert result.returncode == 64
    assert "bot_logins" in result.stderr
    assert harness.calls == []


def test_configured_reviewer_check_is_soft_pending(
    harness: Harness, tmp_path: Path
) -> None:
    """A pending reviewer check stops blocking once the bot window has elapsed."""
    harness.scenario["checks"] = [LINT_PASS, _pending("CodeRabbit")]
    result = harness.run("--bot-window-sec", "0", config=_config(tmp_path))
    assert result.returncode == 0, result.stderr


def test_every_configured_reviewer_check_is_soft_pending(
    harness: Harness, tmp_path: Path
) -> None:
    """An inactive adapter's check (mid-cutover, both bots installed) is soft too."""
    harness.scenario["checks"] = [LINT_PASS, _pending("Cursor Bugbot")]
    result = harness.run(
        "--bot-window-sec", "0", config=_config(tmp_path, active="coderabbit")
    )
    assert result.returncode == 0, result.stderr


def test_a_copilot_check_is_soft_pending(harness: Harness) -> None:
    harness.scenario["checks"] = [LINT_PASS, _pending("Copilot code review")]
    result = harness.run("--bot-window-sec", "0")
    assert result.returncode == 0, result.stderr


def test_non_reviewer_pending_check_is_hard(harness: Harness, tmp_path: Path) -> None:
    harness.scenario["checks"] = [LINT_PASS, _pending("deploy")]
    result = harness.run("--bot-window-sec", "0", config=_config(tmp_path))
    assert result.returncode == 2
    assert "deploy" in result.stderr


def test_default_expected_bot_is_the_active_reviewers_check(
    harness: Harness, tmp_path: Path
) -> None:
    result = harness.run("--bot-window-sec", "600", config=_config(tmp_path))
    assert result.returncode == 2
    assert "CodeRabbit (not yet registered)" in result.stderr


def test_expected_bot_flag_overrides_the_config_default(
    harness: Harness, tmp_path: Path
) -> None:
    result = harness.run(
        "--bot-window-sec",
        "600",
        "--expected-bot",
        "Some Other Bot",
        config=_config(tmp_path),
    )
    assert "Some Other Bot (not yet registered)" in result.stderr
    assert "CodeRabbit (not yet registered)" not in result.stderr


def test_the_expected_bot_flag_repeats(harness: Harness) -> None:
    result = harness.run(
        "--bot-window-sec", "600", "--expected-bot", "Bot A", "--expected-bot", "Bot B"
    )
    assert "Bot A (not yet registered), Bot B (not yet registered)" in result.stderr


def test_an_expected_bot_that_never_registered_is_noted(harness: Harness) -> None:
    result = harness.run("--bot-window-sec", "0", "--expected-bot", "Bot A")
    assert result.returncode == 0, result.stderr
    assert "expected bot 'Bot A' did not register a check within 0s" in result.stderr


def test_no_repo_and_no_reviewer_config_runs_clean(tmp_path: Path) -> None:
    """Ported from the bash-3.2 empty-array test: the bare minimum invocation works.

    No repo arg + no reviewer config left both of the bash script's arrays empty,
    which crashed bash < 4.4 under ``set -u``; the port must run the same case clean.
    """
    result = _cli(tmp_path, {"checks": [LINT_PASS]})
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr


def test_explicitly_empty_config_fails_hard(harness: Harness) -> None:
    """`--config ""` is an explicit (broken) value, not a request for the default."""
    result = harness.run("--config", "")
    assert result.returncode == 64


@pytest.mark.parametrize(
    "flag",
    ["--config", "--timeout-min", "--bot-window-sec", "--poll-sec", "--expected-bot"],
)
def test_a_value_flag_without_its_value_is_a_usage_error(
    tmp_path: Path, flag: str
) -> None:
    """A trailing value flag must exit 64 with a message, not crash."""
    result = _cli(tmp_path, {"checks": [LINT_PASS]}, flag)
    assert result.returncode == 64
    assert f"{flag} needs a value" in result.stderr


# --- usage --------------------------------------------------------------------


def test_no_pr_is_a_usage_error(harness: Harness) -> None:
    assert wait.main([]) == 64
    assert "usage:" in harness.capsys.readouterr().err


def test_a_third_positional_is_a_usage_error(harness: Harness) -> None:
    result = harness.run(REPO, "extra")
    assert result.returncode == 64
    assert "unexpected arg: extra" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("--timeout-min", "soon"),
        ("--bot-window-sec", "-1"),
        ("--poll-sec", "1.5"),
        ("--unknown-flag",),
        ("not/a/repo",),
    ],
)
def test_a_bad_value_is_a_usage_error(harness: Harness, args: tuple[str, ...]) -> None:
    assert harness.run(*args).returncode == 64
    assert harness.calls == []


def test_a_pr_that_is_not_a_number_is_a_usage_error(harness: Harness) -> None:
    assert wait.main(["topic-branch"]) == 64
    assert harness.calls == []


# --- the poll loop and the exit code ------------------------------------------


def test_the_soft_wait_ends_when_the_window_elapses(
    harness: Harness, tmp_path: Path
) -> None:
    harness.scenario["checks"] = [LINT_PASS, _pending("CodeRabbit")]
    result = harness.run(
        "--timeout-min",
        "20",
        "--bot-window-sec",
        "60",
        "--poll-sec",
        "30",
        config=_config(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert harness.sleeps == [30, 30]
    assert result.stderr.count("waiting on external bot reviews") == 2


def test_a_hard_pending_check_times_out_with_the_latest_table(
    harness: Harness,
) -> None:
    harness.scenario["checks"] = [LINT_PASS, _pending("deploy")]
    result = harness.run("--timeout-min", "1", "--poll-sec", "30")
    assert result.returncode == 2
    assert harness.sleeps == [30, 30]
    assert "still pending: deploy" in result.stderr
    assert "TIMEOUT after 1m. Latest:" in result.stderr
    assert "deploy\tpending" in result.stderr


def test_no_checks_yet_keeps_waiting(harness: Harness) -> None:
    harness.scenario["checks"] = None
    result = harness.run()
    assert result.returncode == 2
    assert "(no checks reported yet" in result.stderr


def test_the_summary_counts_checks_by_bucket(harness: Harness) -> None:
    harness.scenario["checks"] = [LINT_PASS, _check("x", "pass"), _pending("deploy")]
    result = harness.run()
    assert "pass=2 pending=1 (still pending: deploy)" in result.stderr


def test_a_failed_check_is_not_mergeable(harness: Harness) -> None:
    harness.scenario["checks"] = [LINT_PASS, _check("tests", "fail")]
    result = harness.run()
    assert result.returncode == 1
    assert "1 check(s) failed or were cancelled" in result.stderr
    assert result.stdout.startswith("FINAL CHECK RESULTS:\n")
    assert "tests\tfail" in result.stdout


def test_all_green_reports_the_final_table_on_stdout(harness: Harness) -> None:
    result = harness.run()
    assert result.returncode == 0
    assert result.stdout == "FINAL CHECK RESULTS:\nlint\tpass\n"
    assert "All required checks passed." in result.stderr


def _break_final_checks_read(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    """Make only the verdict's ``gh pr checks --json name,bucket`` read fail."""
    real = harness.subprocess_run

    def run(
        argv: Sequence[str], *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if list(argv[1:3]) == ["pr", "checks"] and list(argv[-2:]) == [
            "--json",
            "name,bucket",
        ]:
            return subprocess.CompletedProcess(list(argv), 1, "", stderr)
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)


def test_an_unreadable_final_check_list_is_not_a_pass(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_final_checks_read(harness, monkeypatch, "HTTP 502: Bad Gateway")
    result = harness.run()
    assert result.returncode == 2
    assert "could not read the PR's checks" in result.stderr
    assert "All required checks passed." not in result.stderr


def test_no_checks_reported_on_the_final_read_is_not_unreadable(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_final_checks_read(
        harness, monkeypatch, "no checks reported on the 'topic' branch"
    )
    result = harness.run()
    assert result.returncode == 0
    assert "No checks reported on the PR" in result.stderr
    assert "could not read" not in result.stderr
    assert "All required checks passed." not in result.stderr


def test_an_unreadable_check_list_while_polling_is_not_no_checks(
    harness: Harness,
) -> None:
    harness.scenario["fail"] = ["pr checks"]
    result = harness.run()
    assert result.returncode == 2
    assert "could not read the PR's checks" in result.stderr
    assert "no checks reported yet" not in result.stderr


def test_a_superseded_review_gate_run_is_not_a_failure(
    harness: Harness, tmp_path: Path
) -> None:
    """The review gate's cancelled job runs are noise.

    Its verdict is the `review-gate` commit status, so a cancelled run of the job
    that posts it must not read as a failed CI check.
    """
    harness.scenario["checks"] = [
        LINT_PASS,
        _check("CodeRabbit", "pass"),
        _check("compute review gate status", "cancel"),
        _check("review-gate", "pass"),
    ]
    result = harness.run(config=_config(tmp_path))
    assert result.returncode == 0, result.stderr


def test_any_other_cancelled_check_still_fails(
    harness: Harness, tmp_path: Path
) -> None:
    harness.scenario["checks"] = [
        LINT_PASS,
        _check("CodeRabbit", "pass"),
        _check("tests", "cancel"),
    ]
    result = harness.run(config=_config(tmp_path))
    assert result.returncode == 1


def test_a_cancelled_review_gate_run_needs_a_passing_review_gate_status(
    harness: Harness, tmp_path: Path
) -> None:
    """Cancelled gate runs are noise only if a run that finished posted a pass."""
    harness.scenario["checks"] = [
        LINT_PASS,
        _check("CodeRabbit", "pass"),
        _check("compute review gate status", "cancel"),
    ]
    result = harness.run(config=_config(tmp_path))
    assert result.returncode == 1
    assert "review-gate" in result.stderr


# --- merge-state preflight ----------------------------------------------------


def test_conflicting_exits_3(tmp_path: Path) -> None:
    """A conflicting PR runs no pull_request CI: fail fast instead of spinning."""
    result = _cli(tmp_path, {"checks": [], "merge": ["CONFLICTING/BLOCKED"]})
    assert result.returncode == 3
    assert "MERGE STATE CONFLICTING/BLOCKED" in result.stderr
    assert "Step 2f" in result.stderr
    assert not [c for c in _logged_calls(tmp_path) if c[:2] == ["pr", "checks"]]


def test_dirty_exits_3(harness: Harness) -> None:
    harness.scenario["merge"] = ["MERGEABLE/DIRTY"]
    result = harness.run()
    assert result.returncode == 3
    assert "MERGE STATE MERGEABLE/DIRTY" in result.stderr
    assert harness.gh_calls("pr", "checks") == []


def test_unknown_then_conflicting_exits_3(harness: Harness) -> None:
    """Mergeability never resolved in the preflight; the empty check list re-checks."""
    harness.scenario["checks"] = []
    harness.scenario["merge"] = ["UNKNOWN/UNKNOWN"] * 5 + ["CONFLICTING/UNKNOWN"]
    result = harness.run("--timeout-min", "5")
    assert result.returncode == 3
    assert "did not resolve mergeability within 5 tries" in result.stderr
    assert "MERGE STATE CONFLICTING/UNKNOWN" in result.stderr
    assert harness.gh_state["merge_reads"] == 6
    # Four 2s preflight pauses between the five reads; no check-poll sleep.
    assert harness.sleeps == [2, 2, 2, 2]


def test_behind_warns_and_keeps_waiting(harness: Harness) -> None:
    harness.scenario["merge"] = ["MERGEABLE/BEHIND"]
    result = harness.run()
    assert result.returncode == 0, result.stderr
    assert "NOTE: branch is BEHIND its base" in result.stderr
    assert harness.gh_calls("pr", "checks")


def test_an_unknown_merge_state_is_polled_until_it_resolves(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MERGESTATE_POLL_SEC", "0.5")
    harness.scenario["merge"] = [
        "UNKNOWN/UNKNOWN",
        "UNKNOWN/UNKNOWN",
        "MERGEABLE/CLEAN",
    ]
    result = harness.run()
    assert result.returncode == 0, result.stderr
    assert harness.sleeps == [0.5, 0.5]
    assert "did not resolve" not in result.stderr


def test_an_empty_mergestate_poll_sec_means_the_default(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MERGESTATE_POLL_SEC", "")
    harness.scenario["merge"] = ["UNKNOWN/UNKNOWN", "MERGEABLE/CLEAN"]
    assert harness.run().returncode == 0
    assert harness.sleeps == [2]


def test_an_unreadable_merge_state_reads_as_unknown(harness: Harness) -> None:
    harness.scenario["fail"] = ["pr view"]
    result = harness.run()
    assert result.returncode == 0, result.stderr
    assert "did not resolve mergeability within 5 tries" in result.stderr


@pytest.mark.parametrize("value", ["soon", "-1", "nan", "inf"])
def test_a_bad_mergestate_poll_sec_is_a_usage_error(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("MERGESTATE_POLL_SEC", value)
    result = harness.run()
    assert result.returncode == 64
    assert "MERGESTATE_POLL_SEC" in result.stderr


# --- the shipped config (Review Focus 1) --------------------------------------


def test_the_shipped_config_waits_on_ci_only(tmp_path: Path) -> None:
    assert json.loads(_CONFIG.read_text())["active"] is None
    result = _cli(
        tmp_path,
        {"checks": [LINT_PASS]},
        "--config",
        str(_CONFIG),
        "--bot-window-sec",
        "600",
    )
    assert result.returncode == 0, result.stderr
    assert "no PR reviewer active" in result.stderr
    assert "REVIEWER:" not in result.stderr
    assert "not yet registered" not in result.stderr


def test_the_shipped_config_still_lists_unresolved_findings(harness: Harness) -> None:
    harness.scenario["thread_pages"] = [[_thread("octocat", "body")]]
    result = harness.run(REPO, "--config", str(_CONFIG))
    assert result.returncode == 0, result.stderr
    assert "  - octocat src/app/main.py:3 — body" in result.stdout
    # No reviewer: nothing to wait for, so no review read and no settle note.
    assert harness.gh_calls("api", f"repos/{REPO}/pulls/1/reviews") == []
    assert "may still be settling" not in result.stderr
    assert harness.state_calls == []


# --- post-settle: the reviewer's review of HEAD and unresolved findings --------


def _thread(login: str, body: str, resolved: bool = False) -> dict[str, Any]:
    comment = {"author": {"login": login}, "path": "src/app/main.py", "line": 3}
    return {"isResolved": resolved, "comments": {"nodes": [{**comment, "body": body}]}}


def _review(login: str, commit_id: str = OLD, body: str = "") -> dict[str, Any]:
    return {"user": {"login": login}, "commit_id": commit_id, "body": body}


@pytest.fixture
def settled(harness: Harness) -> Harness:
    """Make a green PR on ``REPO`` whose head is ``HEAD``, its reviewer check passed."""
    harness.scenario["head"] = HEAD
    harness.scenario["checks"] = [LINT_PASS, _check("CodeRabbit", "pass")]
    return harness


def test_unresolved_findings_are_listed(settled: Harness) -> None:
    settled.scenario["thread_pages"] = [
        [
            _thread("octocat", "### Rename this\nbecause"),
            _thread("reviewer-a", "done", resolved=True),
        ]
    ]
    result = settled.run(REPO)
    assert result.returncode == 0, result.stderr
    assert "UNRESOLVED REVIEW FINDINGS:\n" in result.stdout
    assert "  - octocat src/app/main.py:3 — Rename this\n" in result.stdout
    assert "reviewer-a" not in result.stdout
    assert "leave resolving to a human" in result.stderr


def test_no_unresolved_threads_reads_none_unresolved(settled: Harness) -> None:
    settled.scenario["thread_pages"] = [[_thread("octocat", "x", resolved=True)]]
    result = settled.run(REPO)
    assert result.stdout.endswith("UNRESOLVED REVIEW FINDINGS:\n  none unresolved.\n")


def test_a_failed_thread_query_is_an_error_not_an_all_clear(
    settled: Harness,
) -> None:
    """A failed GraphQL read on a merge gate must never read as 'none unresolved'."""
    settled.scenario["fail"] = ["api graphql"]
    result = settled.run(REPO)
    assert "none unresolved" not in result.stdout
    assert "ERROR: could not query review threads" in result.stderr


def test_no_repo_skips_the_post_settle_poll(settled: Harness) -> None:
    result = settled.run()
    assert "UNRESOLVED REVIEW FINDINGS" not in result.stdout
    assert settled.gh_calls("api") == []


def test_a_review_of_head_ends_the_review_wait(
    settled: Harness, tmp_path: Path
) -> None:
    settled.scenario["review_pages"] = [[_review(REVIEWER_BOT, HEAD)]]
    result = settled.run(REPO, config=_config(tmp_path))
    assert "may still be settling" not in result.stderr
    # One 15s pause: the unresolved count must hold across one interval.
    assert settled.sleeps == [15]


def test_the_review_wait_gives_up_with_a_note(settled: Harness, tmp_path: Path) -> None:
    """Another login's review of HEAD, or the reviewer's of an older head, isn't it."""
    settled.scenario["review_pages"] = [
        [_review("octocat", HEAD), _review(REVIEWER_BOT, OLD)]
    ]
    result = settled.run(REPO, config=_config(tmp_path))
    assert "CodeRabbit's review may still be settling" in result.stderr
    assert settled.sleeps == [15] * 12
    assert "UNRESOLVED REVIEW FINDINGS:" in result.stdout


@pytest.mark.parametrize(
    "body",
    [
        f"Reviewing files that changed between {OLD[:12]} and {HEAD[:12]}.",
        f'<!-- final_review_risk_coverage:{{"coveredCommitId":"{HEAD}"}} -->',
        f'<!-- change_assessment_commit:"{HEAD[:7]}" -->',
    ],
)
def test_a_summary_naming_head_by_any_sha_pattern_counts(
    settled: Harness, tmp_path: Path, body: str
) -> None:
    """The adapter's ``reviewed_sha_pattern`` may be a list; any entry can match."""
    settled.scenario["review_pages"] = [[_review(REVIEWER_BOT, OLD, body)]]
    result = settled.run(REPO, config=_config(tmp_path))
    assert "may still be settling" not in result.stderr


def test_a_single_sha_pattern_string_still_works(
    settled: Harness, tmp_path: Path
) -> None:
    config = _config(tmp_path, reviewed_sha_pattern=r"reviewed ([0-9a-f]{7,40})")
    settled.scenario["review_pages"] = [
        [_review(REVIEWER_BOT, OLD, f"reviewed {HEAD[:9]}")]
    ]
    result = settled.run(REPO, config=config)
    assert "may still be settling" not in result.stderr


def test_a_sha_naming_another_commit_does_not_count(
    settled: Harness, tmp_path: Path
) -> None:
    body = f'<!-- change_assessment_commit:"{OLD}" -->'
    settled.scenario["review_pages"] = [[_review(REVIEWER_BOT, OLD, body)]]
    result = settled.run(REPO, config=_config(tmp_path))
    assert "may still be settling" in result.stderr


def test_a_summary_issue_comment_counts_when_the_adapter_reads_them(
    settled: Harness, tmp_path: Path
) -> None:
    body = f'<!-- change_assessment_commit:"{HEAD}" -->'
    settled.scenario["comment_pages"] = [[_review(REVIEWER_BOT, "", body)]]
    result = settled.run(REPO, config=_config(tmp_path))
    assert "may still be settling" not in result.stderr


def test_issue_comments_are_not_read_when_the_adapter_says_not_to(
    settled: Harness, tmp_path: Path
) -> None:
    body = f'<!-- change_assessment_commit:"{HEAD}" -->'
    settled.scenario["comment_pages"] = [[_review(REVIEWER_BOT, "", body)]]
    config = _config(tmp_path, summary_in_issue_comments=False)
    result = settled.run(REPO, config=config)
    assert "may still be settling" in result.stderr
    assert settled.gh_calls("api", f"repos/{REPO}/issues/1/comments") == []


def test_check_lists_are_read_paginated(settled: Harness, tmp_path: Path) -> None:
    """Every list read pages through ``reviewer_state._gh_json`` (Review Focus 2).

    The fake prints one JSON document per page, as an older ``gh`` does; the
    reviewer's review of HEAD and the unresolved thread are both on page two.
    """
    settled.scenario["review_pages"] = [
        [_review("octocat", HEAD)],
        [_review(REVIEWER_BOT, HEAD)],
    ]
    settled.scenario["comment_pages"] = [[], []]
    settled.scenario["thread_pages"] = [
        [_thread("reviewer-a", "fixed", resolved=True)],
        [_thread("octocat", "Rename this")],
    ]
    result = settled.run(REPO, config=_config(tmp_path))
    assert "may still be settling" not in result.stderr
    assert "  - octocat src/app/main.py:3 — Rename this" in result.stdout
    api_calls = settled.gh_calls("api")
    assert {c[1] for c in api_calls} == {
        "graphql",
        f"repos/{REPO}/pulls/1/reviews",
        f"repos/{REPO}/issues/1/comments",
    }
    assert all("--paginate" in call for call in api_calls)


# --- the reviewer_state soft-wait cutoff and REVIEWER line --------------------


def _reviewer(
    outcome: str, verdict: str = "none", needs: bool = True, on_head: bool = True
) -> dict[str, Any]:
    return {
        "incremental_review_outcome": outcome,
        "incremental_review_on_head": on_head,
        "verdict": verdict,
        "needs_trigger": needs,
    }


@pytest.mark.parametrize("outcome", ["failed", "not_completed", "no_changes"])
def test_a_terminal_reviewer_reply_ends_the_soft_wait(
    harness: Harness, tmp_path: Path, outcome: str
) -> None:
    """A reviewer that already answered must not be waited on (or called no-review)."""
    harness.state = _reviewer(outcome)
    harness.scenario["checks"] = [LINT_PASS, _pending("CodeRabbit")]
    result = harness.run(
        REPO, "--bot-window-sec", "600", "--timeout-min", "20", config=_config(tmp_path)
    )
    assert result.returncode == 0, result.stderr
    assert f"reviewer answered its trigger ({outcome})" in result.stderr
    assert f"REVIEWER: {outcome} (verdict none, needs_trigger true)" in result.stderr
    assert "no-review" not in result.stderr
    assert harness.state_calls[0] == (REPO, 1)
    # The cutoff broke out of the poll loop on its first pass (no 30s poll pause).
    assert 30 not in harness.sleeps
    assert "waiting on external bot reviews" not in result.stderr


def test_a_terminal_reply_for_an_older_head_keeps_the_soft_wait_open(
    harness: Harness, tmp_path: Path
) -> None:
    """The reply answered a trigger posted before the current head."""
    harness.state = _reviewer("finished", on_head=False)
    harness.scenario["checks"] = [LINT_PASS, _pending("CodeRabbit")]
    result = harness.run("--bot-window-sec", "600", config=_config(tmp_path))
    assert result.returncode == 2
    assert "reviewer answered its trigger" not in result.stderr
    assert "waiting on external bot reviews" in result.stderr


def test_a_running_review_keeps_the_soft_wait_open(
    harness: Harness, tmp_path: Path
) -> None:
    harness.state = _reviewer("pending")
    harness.scenario["checks"] = [LINT_PASS, _pending("CodeRabbit")]
    result = harness.run("--bot-window-sec", "600", config=_config(tmp_path))
    assert result.returncode == 2
    assert "waiting on external bot reviews" in result.stderr


def test_the_reviewer_line_is_printed_after_the_window_elapses(
    harness: Harness, tmp_path: Path
) -> None:
    harness.state = _reviewer("finished", "pass", False)
    harness.scenario["checks"] = [LINT_PASS, _check("CodeRabbit", "pass")]
    result = harness.run(config=_config(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "REVIEWER: finished (verdict pass, needs_trigger false)" in result.stderr
    # No repo arg: the repo comes from `gh repo view`.
    assert harness.state_calls == [(REPO, 1)]


def test_an_unreadable_reviewer_state_falls_back_to_the_window(
    harness: Harness, tmp_path: Path
) -> None:
    """The reviewer's check registered, so only the unreadable-state path can report."""
    harness.scenario["checks"] = [LINT_PASS, _pending("CodeRabbit")]
    result = harness.run("--bot-window-sec", "0", config=_config(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "could not read reviewer_state.py" in result.stderr
    assert "REVIEWER:" not in result.stderr
    assert "did not register" not in result.stderr


def test_an_unreadable_reviewer_state_does_not_end_the_soft_wait(
    harness: Harness, tmp_path: Path
) -> None:
    harness.scenario["checks"] = [LINT_PASS, _pending("CodeRabbit")]
    result = harness.run("--bot-window-sec", "600", config=_config(tmp_path))
    assert result.returncode == 2
    assert "waiting on external bot reviews" in result.stderr


def test_an_unknown_repo_reads_as_an_unreadable_reviewer_state(
    harness: Harness, tmp_path: Path
) -> None:
    del harness.scenario["repo"]
    harness.state = _reviewer("finished", "pass", False)
    harness.scenario["checks"] = [LINT_PASS, _check("CodeRabbit", "pass")]
    result = harness.run(config=_config(tmp_path))
    assert "could not read reviewer_state.py" in result.stderr
    assert harness.state_calls == []


def test_no_reviewer_config_never_asks_reviewer_state(harness: Harness) -> None:
    harness.state = _reviewer("failed")
    result = harness.run()
    assert result.returncode == 0, result.stderr
    assert harness.state_calls == []
    assert "REVIEWER:" not in result.stderr


def test_a_refused_request_is_reported_with_its_retry_time(
    harness: Harness, tmp_path: Path
) -> None:
    """A rate-limit refusal is a wait for the SAME request, so say when."""
    harness.state = {
        **_reviewer("not_completed"),
        "rate_limit": {
            "refused": True,
            "request": "final",
            "retry_after": "2026-10-13T18:03:07Z",
        },
    }
    harness.scenario["checks"] = [LINT_PASS, _check("CodeRabbit", "pass")]
    result = harness.run(config=_config(tmp_path))
    assert result.returncode == 0, result.stderr
    assert (
        "REVIEWER: rate limited -- re-post the same final request after "
        "2026-10-13T18:03:07Z" in result.stderr
    )


def test_the_reviewer_state_comes_from_collect_state_in_process(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The default reader calls the sibling ``reviewer_state.collect_state``."""
    seen: list[tuple[str, int, str, Any]] = []

    def collect_state(
        repo: str, pr: int, adapter: Any, caps: Any = None
    ) -> dict[str, Any]:
        seen.append((repo, pr, adapter.key, caps))
        return _reviewer("finished", "pass", False)

    monkeypatch.setattr(wait, "_read_reviewer_state", _ORIGINAL_READ_STATE)
    monkeypatch.setattr(wait.reviewer_state, "collect_state", collect_state)
    config = _config(tmp_path)
    result = harness.run(config=config)
    assert "REVIEWER: finished (verdict pass, needs_trigger false)" in result.stderr
    assert seen == [(REPO, 1, "coderabbit", reviewer_state.load_round_caps(config))]


def test_a_crashing_reviewer_state_reads_as_unreadable(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An odd GraphQL shape must degrade to the note, never a traceback."""

    def collect_state(*_: Any, **__: Any) -> dict[str, Any]:
        raise AttributeError("'NoneType' object has no attribute 'get'")

    monkeypatch.setattr(wait, "_read_reviewer_state", _ORIGINAL_READ_STATE)
    monkeypatch.setattr(wait.reviewer_state, "collect_state", collect_state)
    harness.scenario["checks"] = [LINT_PASS, _check("CodeRabbit", "pass")]
    result = harness.run(config=_config(tmp_path))
    assert result.returncode == 0
    assert "could not read reviewer_state.py" in result.stderr


_ORIGINAL_READ_STATE = wait._read_reviewer_state
