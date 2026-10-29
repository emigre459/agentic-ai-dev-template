#!/usr/bin/env python3
"""Say whether a LOCAL review round may run now: the local allowance check.

It runs the active reviewer's ``local_preflight`` command from
``.github/pr-reviewer.json`` -- a local, read-only report of the developer's
remaining **local** review capacity -- so a local round never starts while that
allowance is spent. ``local_review.py`` calls it before every round; run it on its
own to check before a batch of rounds.

It never gates a PR review request (the trigger or final-review comment). The local
review's allowance is a separate pool from the PR quota, and no local command can
see the PR quota: a reading of "plenty left" says nothing about whether the next PR
request will be accepted. A refused PR request is read from the reviewer's reply
instead (``reviewer_state.py`` reports it as ``rate_limit.refused`` with its
``retry_after``).

Prints one JSON object and exits with its status:

- ``available`` (exit 0): at least one local review is left; run the round.
- ``exhausted`` (exit 75): wait ``recheck_after_seconds``, run this again, and run
  the round once it says ``available``.
- ``unknown`` (exit 0): the command is missing, logged out, failed, or printed
  nothing readable. Tell the user ``remedy`` (the adapter's fix for ``cause``).
- ``not_configured`` (exit 0): the reviewer declares no preflight; nothing to gate.

A usage error or a bad reviewer config exits 64.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess  # nosec B404 -- argv from the repo's own config, no shell
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from reviewer_state import PreflightSpec, ReviewerAdapter, load_reviewer

#: ``(argv, timeout_seconds) -> (exit code, stdout)``; raises when the CLI is
#: missing (``FileNotFoundError``) or hangs (``subprocess.TimeoutExpired``).
Runner = Callable[[Sequence[str], float], tuple[int, str]]

_TIMEOUT_SECONDS = 15.0
_MIN_RECHECK_SECONDS = 60
EXIT_CODES = {"available": 0, "not_configured": 0, "exhausted": 75, "unknown": 0}


def _run(command: Sequence[str], timeout: float) -> tuple[int, str]:
    done = subprocess.run(  # nosec B603 -- argv from the repo's own config, no shell
        list(command), capture_output=True, text=True, timeout=timeout, check=False
    )
    return done.returncode, done.stdout


def dig(document: Any, path: str) -> Any:
    """Return the value at a dotted ``path`` in ``document``, or ``None``.

    Parameters
    ----------
    document : Any
        A parsed JSON event.
    path : str
        Dotted keys, e.g. ``pool.remaining``.

    Returns
    -------
    Any
        The value there, or ``None`` when any key along the path is missing.
    """
    for key in path.split("."):
        if not isinstance(document, dict):
            return None
        document = document.get(key)
    return document


def _reading(stdout: str, spec: PreflightSpec) -> tuple[str, int, Any] | None:
    """Return ``(state, remaining, full_capacity_ms)`` from the first line with them."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        state = dig(event, spec.state_path)
        remaining = dig(event, spec.remaining_path)
        # bool is an int subclass; `true` is not a review count.
        if (
            isinstance(state, str)
            and isinstance(remaining, int)
            and not isinstance(remaining, bool)
        ):
            return state, remaining, dig(event, spec.full_capacity_ms_path)
    return None


def _cause(spec: PreflightSpec, run: Runner) -> str:
    """Return why an installed CLI gave no reading: logged out, or failed.

    Only an auth check that answers ``false`` means logged out. One that fails too
    says nothing about the login, so the user is pointed at the general diagnosis.
    """
    try:
        code, stdout = run(spec.auth_command, _TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return "failed"
    if code == 0:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if dig(event, spec.authenticated_path) is False:
                return "not_authenticated"
    return "failed"


def _recheck_after(spec: PreflightSpec, full_capacity_ms: Any) -> int:
    """Return the seconds to wait before checking again while the pool is empty.

    ``full_capacity_ms`` is when the whole pool is back; one review frees sooner, so
    the wait is capped at ``max_recheck_seconds`` and re-checked rather than slept
    out, and never under a minute.
    """
    if (
        isinstance(full_capacity_ms, (int, float))
        and not isinstance(full_capacity_ms, bool)
        and full_capacity_ms >= 0
    ):
        seconds = min(math.ceil(full_capacity_ms / 1000), spec.max_recheck_seconds)
    else:
        seconds = spec.max_recheck_seconds
    return max(seconds, _MIN_RECHECK_SECONDS)


def check(adapter: ReviewerAdapter, run: Runner | None = None) -> dict[str, Any]:
    """Run the adapter's preflight command and judge whether a local round may run.

    Parameters
    ----------
    adapter : ReviewerAdapter
        The reviewer adapter whose ``local_preflight`` to run.
    run : Runner or None
        Command runner, or ``None`` for a real subprocess.

    Returns
    -------
    dict[str, Any]
        ``reviewer``, ``allowed``, ``status``, ``remaining``,
        ``recheck_after_seconds``, ``detail``, and for ``unknown`` the ``cause`` and
        its ``remedy``.
    """
    spec = adapter.local_preflight
    runner = run or _run

    def result(
        status: str,
        detail: str,
        remaining: int | None = None,
        recheck: int | None = None,
        cause: str | None = None,
    ) -> dict[str, Any]:
        """Shape one verdict; only ``exhausted`` holds the round back.

        Parameters
        ----------
        status : str
            ``available``, ``exhausted``, ``unknown`` or ``not_configured``.
        detail : str
            What the check saw, for the user.
        remaining : int or None
            Reviews left, when read.
        recheck : int or None
            Seconds to wait before checking again, when exhausted.
        cause : str or None
            Why the check couldn't be read, when ``unknown``.

        Returns
        -------
        dict[str, Any]
            The verdict ``check`` prints.
        """
        return {
            "reviewer": adapter.key,
            "allowed": status != "exhausted",
            "status": status,
            "remaining": remaining,
            "recheck_after_seconds": recheck,
            "detail": detail,
            "cause": cause,
            "remedy": None if cause is None or spec is None else spec.remedies[cause],
        }

    if spec is None:
        return result("not_configured", f"{adapter.display_name} declares no preflight")
    command = " ".join(spec.command)
    reading = None
    try:
        code, stdout = runner(spec.command, _TIMEOUT_SECONDS)
    except FileNotFoundError:
        return result("unknown", f"`{command}` is not installed", cause="not_installed")
    except subprocess.TimeoutExpired:
        failure = f"`{command}` timed out after {_TIMEOUT_SECONDS:g}s"
    else:
        if code != 0:
            failure = f"`{command}` exited {code}"
        else:
            reading = _reading(stdout, spec)
            failure = (
                f"`{command}` printed no reading with {spec.state_path} and "
                f"{spec.remaining_path}"
            )
    if reading is None:
        return result("unknown", failure, cause=_cause(spec, runner))
    state, remaining, full_capacity_ms = reading
    if state in spec.available_states and remaining >= 1:
        return result("available", f"{remaining} local review(s) left", remaining)
    recheck = _recheck_after(spec, full_capacity_ms)
    return result(
        "exhausted",
        f"no local review left (state {state!r}, {remaining} remaining); "
        f"check again in {recheck}s",
        remaining,
        recheck,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Print the preflight verdict for the active reviewer and return its exit code.

    Parameters
    ----------
    argv : Sequence[str] or None
        Command-line arguments, or ``None`` for ``sys.argv``.

    Returns
    -------
    int
        0 to go ahead, 75 to wait and re-check, 64 for a bad config.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=None, help="reviewer config path"
    )
    args = parser.parse_args(argv)
    try:
        adapter = load_reviewer(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 64
    verdict = check(adapter)
    print(json.dumps(verdict, indent=2))
    return EXIT_CODES[verdict["status"]]


if __name__ == "__main__":
    sys.exit(main())
