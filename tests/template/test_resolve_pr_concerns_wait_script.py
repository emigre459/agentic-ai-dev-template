"""Tests for resolve-pr-concerns' ``wait_for_pr_checks.sh`` reviewer-config seam.

The script names no PR reviewer: its default expected bot and its soft-pending
("external reviewer") set come from ``.github/pr-reviewer.json``. These tests run
the real script against a stub ``gh`` on ``PATH`` that serves canned check JSON
(and applies ``--jq`` with real ``jq``), with ``--timeout-min 0`` so every run
evaluates exactly one poll and exits before sleeping. No ``<owner>/<repo>`` arg
is passed, so the post-settle review-thread poll (which needs one) is skipped.
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
    tmp_path: Path, checks: list[dict[str, str]], config: Path, *extra: str
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
    return subprocess.run(  # nosec B603 B607 -- fixed argv, stub gh, no shell
        [
            "bash",
            str(_SCRIPT),
            "1",
            "--config",
            str(config),
            "--timeout-min",
            "0",
            *extra,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


LINT_PASS = {"name": "lint", "bucket": "pass", "state": "SUCCESS", "link": ""}


def _pending(name: str) -> dict[str, str]:
    return {"name": name, "bucket": "pending", "state": "PENDING", "link": ""}


def test_missing_config_fails_hard(tmp_path: Path) -> None:
    result = _run(tmp_path, [LINT_PASS], tmp_path / "absent.json")
    assert result.returncode == 64
    assert "pr-reviewer" in result.stderr


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
