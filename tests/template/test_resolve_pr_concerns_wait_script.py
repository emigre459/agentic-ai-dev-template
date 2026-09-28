"""Tests for resolve-pr-concerns' ``wait_for_pr_checks.sh`` reviewer-config seam.

The script names no PR reviewer: its default expected bot and its soft-pending
("external reviewer") set come from ``.github/pr-reviewer.json``. These tests run
the real script against a stub ``gh`` on ``PATH`` that serves canned check JSON
(and applies ``--jq`` with real ``jq``), with ``--timeout-min 0`` so every run
evaluates exactly one poll and exits before sleeping. No ``<owner>/<repo>`` arg
is passed, so the post-settle review-thread poll (which needs one) is skipped --
and the ``repo_arg`` array stays empty, which the bash 3.2 test relies on.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 -- fixed argv, in-repo script, no shell
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = (
    _REPO
    / ".agents"
    / "skills"
    / "resolve-pr-concerns"
    / "scripts"
    / "wait_for_pr_checks.sh"
)
_CONFIG = _REPO / ".github" / "pr-reviewer.json"

_FAKE_GH = """#!/usr/bin/env bash
if [[ "$1 $2" == "pr view" ]]; then echo "MERGEABLE/CLEAN"; exit 0; fi
expr=""
while [[ $# -gt 0 ]]; do [[ "$1" == "--jq" ]] && expr="$2"; shift; done
if [[ -n "$expr" ]]; then jq -r "$expr" <<< "$FAKE_CHECKS"; else echo "$FAKE_CHECKS"; fi
"""

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="wait_for_pr_checks.sh needs bash + jq",
)


def _config(tmp_path: Path, active: str = "coderabbit", **drop: bool) -> Path:
    config = json.loads(_CONFIG.read_text())
    config["active"] = active
    for field in drop:
        del config["reviewers"][active][field]
    path = tmp_path / "pr-reviewer.json"
    path.write_text(json.dumps(config))
    return path


def _run(
    tmp_path: Path,
    checks: list[dict[str, str]],
    config: Path | None,
    *extra: str,
    bash: str = "bash",
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH)
    gh.chmod(0o755)
    env: dict[str, Any] = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CHECKS": json.dumps(checks),
    }
    config_args = [] if config is None else ["--config", str(config)]
    # cwd = tmp_path (not a git repo), so the default config path is
    # <tmp_path>/.github/pr-reviewer.json -- absent unless a test writes it.
    return subprocess.run(  # nosec B603 B607 -- fixed argv, stub gh, no shell
        [bash, str(_SCRIPT), "1", *config_args, "--timeout-min", "0", *extra],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=30,
    )


LINT_PASS = {"name": "lint", "bucket": "pass", "state": "SUCCESS", "link": ""}


def _pending(name: str) -> dict[str, str]:
    return {"name": name, "bucket": "pending", "state": "PENDING", "link": ""}


def test_missing_explicit_config_fails_hard(tmp_path: Path) -> None:
    """An explicit --config that doesn't exist is a typo -- fail, don't guess."""
    result = _run(tmp_path, [LINT_PASS], tmp_path / "absent.json")
    assert result.returncode == 64
    assert "pr-reviewer" in result.stderr


def test_repo_without_a_reviewer_config_still_settles(tmp_path: Path) -> None:
    """resolve-pr-concerns Step 1b supports repos with no reviewer configured.

    With no default config the script must still settle CI (no expected reviewer
    bot), and say so, rather than exit before polling anything.
    """
    result = _run(tmp_path, [LINT_PASS], None, "--bot-window-sec", "600")
    assert result.returncode == 0, result.stderr
    assert "no PR reviewer configured" in result.stderr
    assert "not yet registered" not in result.stderr


def test_default_config_is_used_when_present(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    config = json.loads(_CONFIG.read_text())
    config["active"] = "coderabbit"
    (tmp_path / ".github" / "pr-reviewer.json").write_text(json.dumps(config))
    result = _run(tmp_path, [LINT_PASS], None, "--bot-window-sec", "600")
    assert result.returncode == 2
    assert "CodeRabbit (not yet registered)" in result.stderr


def test_invalid_default_config_fails_hard(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "pr-reviewer.json").write_text("{not json")
    result = _run(tmp_path, [LINT_PASS], None)
    assert result.returncode == 64


def test_active_reviewer_without_check_name_fails_hard(tmp_path: Path) -> None:
    result = _run(tmp_path, [LINT_PASS], _config(tmp_path, check_name=True))
    assert result.returncode == 64
    assert "check_name" in result.stderr


def test_configured_reviewer_check_is_soft_pending(tmp_path: Path) -> None:
    """A pending reviewer check stops blocking once the bot window has elapsed."""
    result = _run(
        tmp_path,
        [LINT_PASS, _pending("CodeRabbit")],
        _config(tmp_path),
        "--bot-window-sec",
        "0",
    )
    assert result.returncode == 0, result.stderr


def test_every_configured_reviewer_check_is_soft_pending(tmp_path: Path) -> None:
    """An inactive adapter's check (mid-cutover, both bots installed) is soft too."""
    result = _run(
        tmp_path,
        [LINT_PASS, _pending("Cursor Bugbot")],
        _config(tmp_path, active="coderabbit"),
        "--bot-window-sec",
        "0",
    )
    assert result.returncode == 0, result.stderr


def test_non_reviewer_pending_check_is_hard(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        [LINT_PASS, _pending("deploy")],
        _config(tmp_path),
        "--bot-window-sec",
        "0",
    )
    assert result.returncode == 2
    assert "deploy" in result.stderr


def test_default_expected_bot_is_the_active_reviewers_check(tmp_path: Path) -> None:
    result = _run(tmp_path, [LINT_PASS], _config(tmp_path), "--bot-window-sec", "600")
    assert result.returncode == 2
    assert "CodeRabbit (not yet registered)" in result.stderr


def test_expected_bot_flag_overrides_the_config_default(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        [LINT_PASS],
        _config(tmp_path),
        "--bot-window-sec",
        "600",
        "--expected-bot",
        "Some Other Bot",
    )
    assert "Some Other Bot (not yet registered)" in result.stderr
    assert "CodeRabbit (not yet registered)" not in result.stderr


def _is_old_bash(path: str) -> bool:
    if not Path(path).exists():
        return False
    out = subprocess.run(  # nosec B603 -- fixed argv, local bash, no shell
        [path, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True
    ).stdout.strip()
    return out.isdigit() and int(out) < 4


@pytest.mark.skipif(
    not _is_old_bash("/bin/bash"), reason="needs a bash < 4 at /bin/bash (macOS)"
)
def test_empty_arrays_survive_set_u_on_bash_3(tmp_path: Path) -> None:
    """No repo arg + no reviewer config leaves both arrays empty.

    Under `set -u`, bash 3.2 (macOS /bin/bash) treats an empty "${arr[@]}" as an
    unbound variable, so a bare expansion crashes before it polls anything.
    """
    result = _run(tmp_path, [LINT_PASS], None, bash="/bin/bash")
    assert result.returncode == 0, result.stderr
    assert "unbound variable" not in result.stderr


def test_explicitly_empty_config_fails_hard(tmp_path: Path) -> None:
    """`--config ""` is an explicit (broken) value, not a request for the default."""
    result = _run(tmp_path, [LINT_PASS], None, "--config", "")
    assert result.returncode == 64


@pytest.mark.parametrize(
    "flag",
    ["--config", "--timeout-min", "--bot-window-sec", "--poll-sec", "--expected-bot"],
)
def test_a_value_flag_without_its_value_is_a_usage_error(
    tmp_path: Path, flag: str
) -> None:
    """A trailing value flag must exit 64, not crash on an unbound $2 under set -u."""
    result = _run(tmp_path, [LINT_PASS], None, flag)
    assert result.returncode == 64
    assert f"{flag} needs a value" in result.stderr


def _check(name: str, bucket: str) -> dict[str, str]:
    return {"name": name, "bucket": bucket, "state": bucket.upper(), "link": ""}


def test_a_superseded_review_gate_run_is_not_a_failure(tmp_path: Path) -> None:
    """review-gate.yml cancels superseded runs; the cancelled run is noise.

    Its verdict is the `review-gate` commit status, so a cancelled run of the job
    that posts it must not read as a failed CI check.
    """
    checks = [
        LINT_PASS,
        _check("CodeRabbit", "pass"),
        _check("compute review gate status", "cancel"),
        _check("review-gate", "pass"),
    ]
    result = _run(tmp_path, checks, _config(tmp_path))
    assert result.returncode == 0, result.stderr


def test_any_other_cancelled_check_still_fails(tmp_path: Path) -> None:
    checks = [LINT_PASS, _check("CodeRabbit", "pass"), _check("tests", "cancel")]
    result = _run(tmp_path, checks, _config(tmp_path))
    assert result.returncode == 1


def test_a_cancelled_review_gate_run_needs_a_passing_review_gate_status(
    tmp_path: Path,
) -> None:
    """Cancelled gate runs are noise only if a run that finished posted a pass."""
    checks = [
        LINT_PASS,
        _check("CodeRabbit", "pass"),
        _check("compute review gate status", "cancel"),
    ]
    result = _run(tmp_path, checks, _config(tmp_path))
    assert result.returncode == 1
    assert "review-gate" in result.stderr
