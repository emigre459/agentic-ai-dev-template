"""Tests for the local review allowance check, ``review_preflight.py``.

It runs the active reviewer's ``local_preflight`` command and says whether a local
review round may run now. The command is stubbed: each test hands ``check`` a runner
returning the CLI's exit code and stdout (or raising the error a missing or hung CLI
raises), so nothing here touches the network.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess  # nosec B404 -- only TimeoutExpired, nothing is run
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPTS = (
    Path(__file__).resolve().parents[2] / ".agents" / "skills" / "resolve-pr-concerns"
) / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reviewer_state = _load("reviewer_state")
review_preflight = _load("review_preflight")

CODERABBIT = reviewer_state.load_reviewer(key="coderabbit")
BUGBOT = reviewer_state.load_reviewer(key="bugbot")

# The shape `coderabbit usage --agent` prints (observed), with a full pool and the
# identifying values replaced.
_AVAILABLE = {
    "type": "status",
    "phase": "usage",
    "status": "complete",
    "organization": "example-org",
    "includedReviews": {
        "state": "available",
        "repository": "example-org/example-repo",
        "fullCapacityInMs": 0,
        "limit": 10,
        "remaining": 10,
        "rollingWindowMs": 3600000,
    },
    "billingPeriod": {
        "state": "available",
        "scope": "user",
        "usageBillingStatus": "active",
        "user": "a-developer",
        "reviewsCount": 12,
        "spendCents": 0,
        "resetsAt": "2026-11-09T04:59:39.000Z",
    },
}


def _reading(**included: Any) -> dict[str, Any]:
    event: dict[str, Any] = json.loads(json.dumps(_AVAILABLE))
    event["includedReviews"].update(included)
    return event


def _runner(returncode: int = 0, *events: Any, raw: str | None = None) -> Any:
    calls: list[Sequence[str]] = []

    def run(command: Sequence[str], timeout: float) -> tuple[int, str]:
        calls.append(command)
        out = raw if raw is not None else "\n".join(json.dumps(e) for e in events)
        return returncode, out

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_the_real_reading_allows_a_round() -> None:
    run = _runner(0, _AVAILABLE)
    result = review_preflight.check(CODERABBIT, run=run)
    assert result["allowed"] is True
    assert result["status"] == "available"
    assert result["remaining"] == 10
    assert result["recheck_after_seconds"] is None
    assert run.calls == [("coderabbit", "usage", "--agent")]


def _with_min(min_remaining: int) -> Any:
    """Return the CodeRabbit adapter with its ``min_remaining`` replaced."""
    assert CODERABBIT.local_preflight is not None
    spec = CODERABBIT.local_preflight._replace(min_remaining=min_remaining)
    return CODERABBIT._replace(local_preflight=spec)


def test_one_review_left_is_enough_with_a_minimum_of_one() -> None:
    """A minimum of one keeps the plain behaviour: any review left runs."""
    run = _runner(0, _reading(remaining=1))
    result = review_preflight.check(_with_min(1), run=run)
    assert result["allowed"] is True
    assert result["status"] == "available"


def test_coderabbit_declares_a_minimum_of_two() -> None:
    assert CODERABBIT.local_preflight is not None
    assert CODERABBIT.local_preflight.min_remaining == 2


def test_below_the_minimum_is_exhausted_with_a_recheck_and_remaining() -> None:
    """One left is under the safety margin for a deep round, which can cost 1-2."""
    reading = _reading(remaining=1, fullCapacityInMs=150_500)
    result = review_preflight.check(CODERABBIT, run=_runner(0, reading))
    assert result["allowed"] is False
    assert result["status"] == "exhausted"
    assert result["remaining"] == 1
    assert result["recheck_after_seconds"] == 151
    assert "only 1 review(s) left, under the minimum of 2" in result["detail"]


def test_below_the_minimum_caps_the_recheck() -> None:
    reading = _reading(remaining=1, fullCapacityInMs=2_400_000)
    result = review_preflight.check(CODERABBIT, run=_runner(0, reading))
    assert result["recheck_after_seconds"] == 300


def test_at_the_minimum_is_available() -> None:
    result = review_preflight.check(CODERABBIT, run=_runner(0, _reading(remaining=2)))
    assert result["status"] == "available"
    assert result["remaining"] == 2


def test_above_the_minimum_is_available() -> None:
    result = review_preflight.check(CODERABBIT, run=_runner(0, _AVAILABLE))
    assert result["status"] == "available"
    assert result["remaining"] == 10


def test_an_empty_pool_does_not_claim_a_minimum_shortfall() -> None:
    reading = _reading(remaining=0, fullCapacityInMs=150_500)
    result = review_preflight.check(CODERABBIT, run=_runner(0, reading))
    assert result["status"] == "exhausted"
    assert "under the minimum" not in result["detail"]
    assert "no local review left" in result["detail"]


def test_an_unreadable_remaining_stays_state_only_and_says_the_gate_was_skipped() -> (
    None
):
    """With no ``remaining`` the minimum can't be applied; say so."""
    event = _reading()
    del event["includedReviews"]["remaining"]
    run = _by_command({_USAGE: (0, event), _AUTH: (0, _LOGGED_IN)})
    result = review_preflight.check(CODERABBIT, run=run)
    assert result["status"] == "unknown"
    assert result["allowed"] is True
    assert "minimum of 2" in result["detail"]
    assert "not applied" in result["detail"]


def test_an_unreadable_remaining_with_no_minimum_adds_no_note() -> None:
    event = _reading()
    del event["includedReviews"]["remaining"]
    run = _by_command({_USAGE: (0, event), _AUTH: (0, _LOGGED_IN)})
    result = review_preflight.check(_with_min(1), run=run)
    assert result["status"] == "unknown"
    assert "not applied" not in result["detail"]


def test_an_empty_pool_waits_at_most_the_recheck_cap() -> None:
    reading = _reading(remaining=0, fullCapacityInMs=2_400_000)
    result = review_preflight.check(CODERABBIT, run=_runner(0, reading))
    assert result["allowed"] is False
    assert result["status"] == "exhausted"
    assert result["remaining"] == 0
    # Full capacity is 40 min away, but one review frees sooner: re-check in 5.
    assert result["recheck_after_seconds"] == 300


def test_an_empty_pool_rechecks_when_full_capacity_is_sooner() -> None:
    reading = _reading(remaining=0, fullCapacityInMs=150_500)
    result = review_preflight.check(CODERABBIT, run=_runner(0, reading))
    assert result["recheck_after_seconds"] == 151


def test_the_recheck_is_never_under_a_minute() -> None:
    reading = _reading(remaining=0, fullCapacityInMs=0)
    result = review_preflight.check(CODERABBIT, run=_runner(0, reading))
    assert result["recheck_after_seconds"] == 60


def test_a_state_other_than_available_waits_even_with_reviews_left() -> None:
    reading = _reading(state="exhausted", remaining=3, fullCapacityInMs=600_000)
    result = review_preflight.check(CODERABBIT, run=_runner(0, reading))
    assert result["status"] == "exhausted"
    assert result["allowed"] is False


def test_the_reading_is_the_first_line_the_paths_resolve_on() -> None:
    run = _runner(0, {"type": "heartbeat"}, _reading(remaining=0, fullCapacityInMs=1))
    result = review_preflight.check(CODERABBIT, run=run)
    assert result["status"] == "exhausted"


_USAGE = ("coderabbit", "usage", "--agent")
_AUTH = ("coderabbit", "auth", "status", "--agent")
_LOGGED_IN = {"type": "status", "phase": "auth", "authenticated": True}
_LOGGED_OUT = {"type": "status", "phase": "auth", "authenticated": False}


def _by_command(answers: dict[tuple[str, ...], Any]) -> Any:
    """Build a runner answering each command with ``(code, event)``, or raising."""
    calls: list[tuple[str, ...]] = []

    def run(command: Sequence[str], timeout: float) -> tuple[int, str]:
        calls.append(tuple(command))
        answer = answers[tuple(command)]
        if isinstance(answer, BaseException):
            raise answer
        code, event = answer
        return code, event if isinstance(event, str) else json.dumps(event)

    run.calls = calls  # type: ignore[attr-defined]
    return run


@pytest.mark.parametrize(
    ("answers", "cause", "detail"),
    [
        (
            {_USAGE: FileNotFoundError("coderabbit")},
            "not_installed",
            "not installed",
        ),
        (
            {_USAGE: (1, "Error: not authenticated"), _AUTH: (0, _LOGGED_OUT)},
            "not_authenticated",
            "exited 1",
        ),
        (
            {_USAGE: (0, "not json at all"), _AUTH: (0, _LOGGED_OUT)},
            "not_authenticated",
            "no reading",
        ),
        (
            {
                _USAGE: subprocess.TimeoutExpired("coderabbit", 15),
                _AUTH: (0, _LOGGED_IN),
            },
            "failed",
            "timed out",
        ),
        (
            {_USAGE: (0, _reading(remaining="ten")), _AUTH: (0, _LOGGED_IN)},
            "failed",
            "no reading",
        ),
        (
            {_USAGE: (2, ""), _AUTH: (1, "boom")},
            "failed",
            "exited 2",
        ),
    ],
    ids=[
        "missing",
        "logged-out",
        "garbage-logged-out",
        "timeout",
        "bad-remaining",
        "auth-check-fails",
    ],
)
def test_an_unreadable_preflight_names_the_fix(
    answers: dict[tuple[str, ...], Any], cause: str, detail: str
) -> None:
    result = review_preflight.check(CODERABBIT, run=_by_command(answers))
    assert result["status"] == "unknown"
    assert result["allowed"] is True
    assert result["cause"] == cause
    assert detail in result["detail"]
    assert CODERABBIT.local_preflight is not None
    assert result["remedy"] == CODERABBIT.local_preflight.remedies[cause]


def test_a_missing_cli_points_at_the_install_guide() -> None:
    run = _by_command({_USAGE: FileNotFoundError("coderabbit")})
    result = review_preflight.check(CODERABBIT, run=run)
    assert "https://docs.coderabbit.ai/cli#getting-started" in result["remedy"]
    assert run.calls == [_USAGE]


def test_a_logged_out_cli_says_to_log_in() -> None:
    run = _by_command({_USAGE: (1, ""), _AUTH: (0, _LOGGED_OUT)})
    result = review_preflight.check(CODERABBIT, run=run)
    assert "coderabbit auth login" in result["remedy"]
    assert run.calls == [_USAGE, _AUTH]


def test_a_readable_preflight_never_runs_the_auth_check() -> None:
    run = _by_command({_USAGE: (0, _AVAILABLE)})
    result = review_preflight.check(CODERABBIT, run=run)
    assert result["cause"] is None and result["remedy"] is None
    assert run.calls == [_USAGE]


def test_a_reviewer_with_no_preflight_is_not_gated() -> None:
    run = _runner(0, _AVAILABLE)
    result = review_preflight.check(BUGBOT, run=run)
    assert result["allowed"] is True
    assert result["status"] == "not_configured"
    assert run.calls == []


@pytest.mark.parametrize(
    ("reading", "code"),
    [
        (_AVAILABLE, 0),
        (_reading(remaining=0, fullCapacityInMs=60_000), 75),
        (None, 0),
    ],
    ids=["available", "exhausted", "unknown"],
)
def test_main_exits_with_the_status_code(
    monkeypatch: Any, capsys: Any, tmp_path: Path, reading: Any, code: int
) -> None:
    """Pins CodeRabbit as active, so the test doesn't follow the repo's ``active``."""
    config = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    config["active"] = "coderabbit"
    path = tmp_path / "pr-reviewer.json"
    path.write_text(json.dumps(config))
    run = _runner(0, reading) if reading is not None else _runner(0, raw="")
    monkeypatch.setattr(review_preflight, "_run", run)
    assert review_preflight.main(["--config", str(path)]) == code
    assert json.loads(capsys.readouterr().out)["reviewer"] == "coderabbit"


def test_main_reports_a_bad_config_as_a_usage_error(
    tmp_path: Path, capsys: Any
) -> None:
    assert review_preflight.main(["--config", str(tmp_path / "absent.json")]) == 64
    assert "pr-reviewer" in capsys.readouterr().err


def test_main_with_no_active_reviewer_is_not_configured(
    tmp_path: Path, capsys: Any
) -> None:
    config = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    config["active"] = None
    path = tmp_path / "pr-reviewer.json"
    path.write_text(json.dumps(config))
    assert review_preflight.main(["--config", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["reviewer"], result["status"]) == (None, "not_configured")
    assert result["allowed"] is True
