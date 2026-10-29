#!/usr/bin/env python3
"""Authoritative state of the configured PR reviewer for a PR's current head SHA.

The active reviewer is declared in ``.github/pr-reviewer.json``: its check name,
trigger comment, bot logins, summary marker, and verdict patterns all live there,
so this script names no vendor. Vendor quirks that don't fit a config field live
in the resolve-pr-concerns skill's ``references/reviewers/<key>.md``.

A reviewer's verdict lives in its *review*, which is decoupled from its status
check: the check can read skipping/neutral/pending while a complete review with
findings already exists. Findings never appear in issue-comments, so reading
those always looks empty.

Also returns ``merge_state_status``, despite the module's name, so one call hands an
agent everything it needs to decide its next step (a ``BEHIND`` base is one of them).
``UNKNOWN`` means "not computed yet, or already merged" -- never "not BEHIND".

Also returns ``final_review_outcome``: the reviewer's one final full review, requested
with the adapter's ``final_review_comment`` once incremental reviews are clean. It is
judged from the reviewer's reply to that command (``command_reply_marker``), which the
reviewer edits in place when the review ends -- never from its status check.

Also returns ``rate_limit``: whether the reviewer refused the newest review request
(trigger or final-review comment) for its rate limit, and when to retry. Such a
request never ran: it is waited out and re-posted unchanged, never escalated.

Also returns ``local_review``: for a reviewer whose adapter declares ``local_review``
(a command-line review of the committed changes, run on the developer's machine
against an allowance separate from the PR quota), the fix rounds run locally through
``local_review.py`` and are budgeted here from the records it leaves on the PR
(``review_rounds`` in the config). Such a reviewer never needs a PR trigger: the PR's
one review request is the final full review.

Also returns ``incremental_review_outcome``: the reviewer's answer to the newest
trigger comment (``finished``, ``no_changes``, ``failed``, ``not_completed``,
``pending``, or ``not_requested`` / ``not_tracked``), read from its command replies.
``incremental_review_on_head`` says whether that request covered the current head
(it was posted once the head reached the PR, by ``head_committed_at`` and
``head_first_status_at``), and ``review_running`` that it is still running on this
head, so ``needs_trigger`` stays false instead of stacking a second request.

Also returns ``round_budget``: the PR's incremental review rounds (exact trigger
comments) counted before and after its first final full review, against the caps in
``review_rounds`` (or ``--max-rounds-before-final`` / ``--max-rounds-after-final``
for one run). A request that failed, was refused for the rate limit, or (when a
verdict carried) was answered "no files to review" spends no round. A reviewer with
a local review gets no PR incremental rounds.

Also returns ``verdict_carried_from`` / ``verdict_carry_reason``: a head with no
verdict of its own inherits the newest reviewed commit's verdict when the reviewer
replied "no files to review" to a request that covered it (``no_changes``), or when
the head's own diff is the same patch that commit was reviewed on (``same_patch``)
-- a rebase or a merge from the base that changed nothing in the PR's own diff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess  # nosec B404 -- fixed gh argv, no shell
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

# scripts/ -> resolve-pr-concerns/ -> skills/ -> .agents/ -> repo root
DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[4] / ".github" / "pr-reviewer.json"
)


class NoActiveReviewer(Exception):
    """The config sets ``"active": null``: the repo has no PR reviewer.

    Deliberately not a ``ValueError``: callers report a bad config as a usage error,
    while no reviewer is a supported setup (resolve-pr-concerns Step 1b).
    """


_REQUIRED_STRINGS = (
    "display_name",
    "check_name",
    "trigger_comment",
    "clean_pattern",
    "findings_pattern",
)
# Present-but-nullable: an absent key is a typo, not a deliberate null.
_NULLABLE_STRINGS = ("summary_marker",)
# The final full review: all set, or all null for a reviewer agents can't ask for one.
_FINAL_REVIEW_FIELDS = (
    "final_review_comment",
    "command_reply_marker",
    "final_review_done_pattern",
    "final_review_failed_pattern",
    "final_review_partial_pattern",
)
# The reviewer's replies to the trigger comment: all set, or all null for a reviewer
# that doesn't reply to it (then no incremental outcome is tracked).
_INCREMENTAL_REVIEW_FIELDS = (
    "incremental_review_done_pattern",
    "incremental_review_no_changes_pattern",
    "incremental_review_failed_pattern",
    "incremental_review_partial_pattern",
)


class PreflightSpec(NamedTuple):
    """A local, read-only command that reports the local review's remaining capacity.

    ``review_preflight.py`` runs it before every local review round, so a round never
    starts while the local allowance is spent. It reads the local pool only, never
    the PR quota. The paths are dotted keys into the command's JSON. When the command
    can't be read, ``auth_command`` tells a logged-out CLI from a broken one, and
    ``remedies`` holds what to tell the user for each cause. ``min_remaining``
    (optional) is the fewest reviews a round needs: below it the preflight reports
    the allowance exhausted even though its state is available, because a deep round
    can cost more than one review.
    """

    command: tuple[str, ...]
    state_path: str
    available_states: frozenset[str]
    remaining_path: str
    full_capacity_ms_path: str
    max_recheck_seconds: int
    auth_command: tuple[str, ...]
    authenticated_path: str
    remedies: dict[str, str]
    #: Fewest reviews that must remain for a round to start; one means any review
    #: left will do.
    min_remaining: int = 1


_PREFLIGHT_PATHS = (
    "state_path",
    "remaining_path",
    "full_capacity_ms_path",
    "authenticated_path",
)
_PREFLIGHT_REQUIRED_KEYS = frozenset(
    {
        "command",
        "auth_command",
        "available_states",
        "max_recheck_seconds",
        "remedies",
        *_PREFLIGHT_PATHS,
    }
)
#: Optional ``local_preflight`` keys: absent means the documented default.
_PREFLIGHT_OPTIONAL_KEYS = frozenset({"min_remaining"})
_PREFLIGHT_KEYS = _PREFLIGHT_REQUIRED_KEYS | _PREFLIGHT_OPTIONAL_KEYS
#: Why a preflight couldn't be read; each needs its own remedy text.
PREFLIGHT_FAILURES = ("not_installed", "not_authenticated", "failed")


def _argv(value: Any) -> bool:
    """Return whether ``value`` is a non-empty list of non-empty string arguments."""
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(arg, str) and arg for arg in value)
    )


def _positive_int(value: Any) -> bool:
    """Return whether ``value`` is a positive integer (``true`` is not a number)."""
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


def _load_preflight(path: Path, key: str, value: Any) -> PreflightSpec | None:
    """Validate one adapter's ``local_preflight`` block; ``None`` means no gate."""
    if value is None:
        return None
    where = f"{path}: reviewers.{key}.local_preflight"
    if (
        not isinstance(value, dict)
        or not _PREFLIGHT_REQUIRED_KEYS <= set(value) <= _PREFLIGHT_KEYS
    ):
        raise ValueError(
            f"{where} must be null or an object with the keys "
            f"{sorted(_PREFLIGHT_REQUIRED_KEYS)} (optional: "
            f"{sorted(_PREFLIGHT_OPTIONAL_KEYS)})."
        )
    for field in ("command", "auth_command"):
        if not _argv(value[field]):
            raise ValueError(f"{where}.{field} must be a non-empty list of arguments.")
    remedies = value["remedies"]
    if (
        not isinstance(remedies, dict)
        or set(remedies) != set(PREFLIGHT_FAILURES)
        or not all(isinstance(text, str) and text for text in remedies.values())
    ):
        raise ValueError(
            f"{where}.remedies must map each of {list(PREFLIGHT_FAILURES)} to the "
            "instruction the user needs."
        )
    for field in _PREFLIGHT_PATHS:
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"{where}.{field} must be a non-empty dotted path.")
    if not _argv(value["available_states"]):
        raise ValueError(f"{where}.available_states must be a non-empty list.")
    if not _positive_int(value["max_recheck_seconds"]):
        raise ValueError(f"{where}.max_recheck_seconds must be a positive integer.")
    minimum = value.get("min_remaining", 1)
    if not _positive_int(minimum):
        raise ValueError(f"{where}.min_remaining must be a positive integer.")
    return PreflightSpec(
        command=tuple(value["command"]),
        state_path=value["state_path"],
        available_states=frozenset(value["available_states"]),
        remaining_path=value["remaining_path"],
        full_capacity_ms_path=value["full_capacity_ms_path"],
        max_recheck_seconds=value["max_recheck_seconds"],
        auth_command=tuple(value["auth_command"]),
        authenticated_path=value["authenticated_path"],
        remedies=dict(remedies),
        min_remaining=minimum,
    )


class LocalReviewSpec(NamedTuple):
    """A local command-line review of the committed changes against a base.

    A PR review request spends the developer's scarce PR quota; a local review draws
    on a separate allowance, so fix rounds run here and the PR gets one final full
    review. ``command`` is the argv, with ``{base}`` standing for the base commit.
    The command prints one JSON event per line: the event type sits at
    ``event_type_path``, findings are ``finding_event`` lines whose fields
    ``finding_fields`` maps (``severity``, ``path``, ``line``, ``title``, ``body``),
    and a ``complete_event`` line ends a finished review. ``start_failure_pattern``
    (optional) matches the command's output when it could not start a review at all,
    which an exhausted allowance can cause; ``None`` means a start failure is never
    read as exhaustion.
    """

    command: tuple[str, ...]
    event_type_path: str
    finding_event: str
    complete_event: str
    finding_fields: dict[str, str]
    timeout_seconds: int
    start_failure_pattern: re.Pattern[str] | None = None


_LOCAL_REVIEW_REQUIRED_KEYS = frozenset(
    {
        "command",
        "event_type_path",
        "finding_event",
        "complete_event",
        "finding_fields",
        "timeout_seconds",
    }
)
#: Optional ``local_review`` keys: absent means the documented default.
_LOCAL_REVIEW_OPTIONAL_KEYS = frozenset({"start_failure_pattern"})
_LOCAL_REVIEW_KEYS = _LOCAL_REVIEW_REQUIRED_KEYS | _LOCAL_REVIEW_OPTIONAL_KEYS
#: The normalised finding fields every local review must map.
LOCAL_FINDING_FIELDS = ("severity", "path", "line", "title", "body")
#: Stands for the base commit in ``LocalReviewSpec.command``.
BASE_PLACEHOLDER = "{base}"


def _load_local_review(path: Path, key: str, value: Any) -> LocalReviewSpec | None:
    """Validate one adapter's ``local_review`` block; ``None`` means no local review."""
    if value is None:
        return None
    where = f"{path}: reviewers.{key}.local_review"
    if (
        not isinstance(value, dict)
        or not _LOCAL_REVIEW_REQUIRED_KEYS <= set(value) <= _LOCAL_REVIEW_KEYS
    ):
        raise ValueError(
            f"{where} must be null or an object with the keys "
            f"{sorted(_LOCAL_REVIEW_REQUIRED_KEYS)} (optional: "
            f"{sorted(_LOCAL_REVIEW_OPTIONAL_KEYS)})."
        )
    command = value["command"]
    if not _argv(command):
        raise ValueError(f"{where}.command must be a non-empty list of arguments.")
    if not any(BASE_PLACEHOLDER in arg for arg in command):
        raise ValueError(
            f"{where}.command must name the base commit as {BASE_PLACEHOLDER}."
        )
    for field in ("event_type_path", "finding_event", "complete_event"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"{where}.{field} must be a non-empty string.")
    fields = value["finding_fields"]
    if (
        not isinstance(fields, dict)
        or set(fields) != set(LOCAL_FINDING_FIELDS)
        or not all(isinstance(p, str) and p for p in fields.values())
    ):
        raise ValueError(
            f"{where}.finding_fields must map each of {list(LOCAL_FINDING_FIELDS)} "
            "to a dotted path in a finding event."
        )
    if not _positive_int(value["timeout_seconds"]):
        raise ValueError(f"{where}.timeout_seconds must be a positive integer.")
    start_failure = None
    if "start_failure_pattern" in value:
        text = value["start_failure_pattern"]
        if not isinstance(text, str) or not text:
            raise ValueError(
                f"{where}.start_failure_pattern must be a non-empty regex."
            )
        try:
            start_failure = re.compile(text, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(
                f"{where}.start_failure_pattern is not a valid regex: {exc}"
            ) from exc
    return LocalReviewSpec(
        command=tuple(command),
        event_type_path=value["event_type_path"],
        finding_event=value["finding_event"],
        complete_event=value["complete_event"],
        finding_fields=dict(fields),
        timeout_seconds=value["timeout_seconds"],
        start_failure_pattern=start_failure,
    )


class ReviewerAdapter(NamedTuple):
    """One reviewer's entry from ``.github/pr-reviewer.json``, validated + compiled."""

    key: str
    display_name: str
    check_name: str
    trigger_comment: str
    bot_logins: frozenset[str]
    summary_marker: str | None
    clean_pattern: re.Pattern[str]
    findings_pattern: re.Pattern[str]
    reviewed_sha_patterns: tuple[re.Pattern[str], ...]
    self_resolves_threads: bool
    summary_in_issue_comments: bool
    final_review_comment: str | None
    command_reply_marker: str | None
    final_review_done_pattern: re.Pattern[str] | None
    final_review_failed_pattern: re.Pattern[str] | None
    final_review_partial_pattern: re.Pattern[str] | None
    # The reviewer's replies to the trigger comment (all four set, or all null for a
    # reviewer that doesn't reply to it).
    incremental_review_done_pattern: re.Pattern[str] | None
    incremental_review_no_changes_pattern: re.Pattern[str] | None
    incremental_review_failed_pattern: re.Pattern[str] | None
    incremental_review_partial_pattern: re.Pattern[str] | None
    # Findings the reviewer states with no thread to reply on; each pattern has one
    # capture group holding the finding's text.
    unthreaded_finding_patterns: tuple[re.Pattern[str], ...]
    # A command reply saying the request was refused for the rate limit (it never
    # ran, so it is a wait, not a review), the wait it states (one group, minutes),
    # the wait to assume when it states none, and the notice comment the reviewer
    # edits in place on a refused attempt (it can state the wait when the reply
    # doesn't).
    rate_limited_pattern: re.Pattern[str] | None
    rate_limit_wait_pattern: re.Pattern[str] | None
    rate_limit_default_wait_minutes: int | None
    rate_limit_notice_marker: str | None
    # The capacity check for ``local_review``'s allowance, run before every local
    # round (it reads the local pool, not the PR quota), and the local review itself.
    local_preflight: PreflightSpec | None
    local_review: LocalReviewSpec | None


class RoundCaps(NamedTuple):
    """How many review rounds a PR gets around its final full review.

    Set in ``.github/pr-reviewer.json`` under ``review_rounds`` (raise the PR caps
    for one run with ``--max-rounds-before-final`` / ``--max-rounds-after-final``).
    ``before_final`` and ``after_final`` count incremental PR reviews (the trigger
    comment) before and after the final full review; ``fix_iterations`` is the fix
    loop's outer safety net (the agent counts its passes, this is only the limit).
    The ``local_*`` caps apply only to a reviewer that declares ``local_review``: its
    fix rounds run locally, before the PR's one final full review and after it, and
    ``pr_round_caps`` gives it no PR incremental rounds.
    """

    before_final: int = 1
    after_final: int = 3
    fix_iterations: int = 5
    local_before_final: int = 3
    local_after_final: int = 3


DEFAULT_ROUND_CAPS = RoundCaps()
_ROUND_CAP_KEYS = {
    "max_incremental_before_final": "before_final",
    "max_incremental_after_final": "after_final",
    "max_fix_iterations": "fix_iterations",
    "max_local_before_final": "local_before_final",
    "max_local_after_final": "local_after_final",
}


def load_round_caps(config_path: Path | None = None) -> RoundCaps:
    """Read ``review_rounds`` from the reviewer config; absent means the defaults.

    Parameters
    ----------
    config_path : Path or None
        Reviewer config path, or ``None`` for the default.

    Returns
    -------
    RoundCaps
        The configured caps, or ``DEFAULT_ROUND_CAPS`` when ``review_rounds`` is
        absent.

    Raises
    ------
    ValueError
        An unknown key (a misspelled knob must not fall back silently) or a value
        that is not a non-negative integer.
    """
    path = config_path or DEFAULT_CONFIG_PATH
    config = json.loads(path.read_text())
    block = config.get("review_rounds") if isinstance(config, dict) else None
    if block is None:
        return DEFAULT_ROUND_CAPS
    if not isinstance(block, dict):
        raise ValueError(f"{path}: review_rounds must be an object.")
    unknown = sorted(set(block) - set(_ROUND_CAP_KEYS))
    if unknown:
        raise ValueError(
            f"{path}: unknown review_rounds key(s) {unknown}; "
            f"expected {sorted(_ROUND_CAP_KEYS)}."
        )
    values = DEFAULT_ROUND_CAPS._asdict()
    for key, field in _ROUND_CAP_KEYS.items():
        if key not in block:
            continue
        value = block[key]
        # bool is an int subclass; `true` is not a round count.
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"{path}: review_rounds.{key} must be a non-negative integer, "
                f"got {value!r}."
            )
        values[field] = value
    return RoundCaps(**values)


def _compile(path: Path, key: str, field: str, pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(
            f"{path}: reviewers.{key}.{field} is not a valid regex: {exc}"
        ) from exc


def _one_group(path: Path, key: str, field: str, compiled: re.Pattern[str]) -> None:
    if compiled.groups != 1:
        raise ValueError(
            f"{path}: reviewers.{key}.{field} must have exactly one capture group "
            f"(found {compiled.groups})."
        )


def _pattern_list(
    path: Path, key: str, field: str, value: Any, *, allow_empty: bool
) -> tuple[re.Pattern[str], ...]:
    """Compile a list of regexes, each with exactly one capture group."""
    if (
        not isinstance(value, list)
        or not (value or allow_empty)
        or not all(isinstance(p, str) and p for p in value)
    ):
        raise ValueError(
            f"{path}: reviewers.{key}.{field} must be a list of non-empty regexes"
            + ("." if allow_empty else " (at least one), a single regex, or null.")
        )
    compiled = tuple(_compile(path, key, field, p) for p in value)
    for pattern in compiled:
        _one_group(path, key, field, pattern)
    return compiled


def load_reviewer(
    config_path: Path | None = None, key: str | None = None
) -> ReviewerAdapter:
    """Load and validate one reviewer adapter (the ``active`` one unless ``key``).

    Raises
    ------
    FileNotFoundError
        The config file does not exist.
    NoActiveReviewer
        ``active`` is null and no ``key`` was given.
    ValueError
        Invalid JSON, an unknown reviewer key, or a missing/mistyped field.
    """
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise FileNotFoundError(
            f"PR-reviewer config not found at {path}. Create .github/pr-reviewer.json "
            "naming the active reviewer (see the resolve-pr-concerns skill's "
            "references/reviewers/), or pass --config."
        )
    try:
        config = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc

    reviewers = config.get("reviewers") if isinstance(config, dict) else None
    if not isinstance(reviewers, dict) or not reviewers:
        raise ValueError(
            f"{path}: 'reviewers' must be a non-empty object keyed by reviewer name."
        )
    chosen = key if key is not None else config.get("active")
    if chosen is None and "active" in config:
        raise NoActiveReviewer(
            f"{path}: no PR reviewer is active ('active' is null). Set it to one of "
            f"{sorted(reviewers)} once that reviewer is installed on the repo."
        )
    if chosen not in reviewers:
        raise ValueError(
            f"{path}: reviewer {chosen!r} is not configured. "
            f"Expected one of {sorted(reviewers)}."
        )
    raw = reviewers[chosen]
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: reviewers.{chosen} must be an object.")

    for field in _REQUIRED_STRINGS:
        if not isinstance(raw.get(field), str) or not raw[field]:
            raise ValueError(
                f"{path}: reviewers.{chosen}.{field} must be a non-empty string."
            )
    for field in _NULLABLE_STRINGS:
        value = raw.get(field, ...)
        if not (value is None or (isinstance(value, str) and value)):
            raise ValueError(
                f"{path}: reviewers.{chosen}.{field} must be a non-empty string "
                "or null (and must be present)."
            )
    logins = raw.get("bot_logins")
    if (
        not isinstance(logins, list)
        or not logins
        or not all(isinstance(login, str) and login for login in logins)
    ):
        raise ValueError(
            f"{path}: reviewers.{chosen}.bot_logins must be a non-empty list of "
            "logins (list both the `x` and `x[bot]` forms)."
        )
    for field in ("self_resolves_threads", "summary_in_issue_comments"):
        if not isinstance(raw.get(field), bool):
            raise ValueError(
                f"{path}: reviewers.{chosen}.{field} must be true or false."
            )
    for field in _FINAL_REVIEW_FIELDS:
        value = raw.get(field, ...)
        if not (value is None or (isinstance(value, str) and value)):
            raise ValueError(
                f"{path}: reviewers.{chosen}.{field} must be a non-empty string "
                "or null (and must be present)."
            )
    if len({raw[field] is None for field in _FINAL_REVIEW_FIELDS}) != 1:
        raise ValueError(
            f"{path}: reviewers.{chosen}: {', '.join(_FINAL_REVIEW_FIELDS)} must all "
            "be set or all be null."
        )
    final_done, final_failed, final_partial = (
        None if raw[field] is None else _compile(path, chosen, field, raw[field])
        for field in (
            "final_review_done_pattern",
            "final_review_failed_pattern",
            "final_review_partial_pattern",
        )
    )

    for field in _INCREMENTAL_REVIEW_FIELDS:
        value = raw.get(field, ...)
        if not (value is None or (isinstance(value, str) and value)):
            raise ValueError(
                f"{path}: reviewers.{chosen}.{field} must be a non-empty string "
                "or null (and must be present)."
            )
    if len({raw[field] is None for field in _INCREMENTAL_REVIEW_FIELDS}) != 1:
        raise ValueError(
            f"{path}: reviewers.{chosen}: {', '.join(_INCREMENTAL_REVIEW_FIELDS)} must "
            "all be set or all be null."
        )
    if (
        raw[_INCREMENTAL_REVIEW_FIELDS[0]] is not None
        and raw["command_reply_marker"] is None
    ):
        raise ValueError(
            f"{path}: reviewers.{chosen}: the incremental_review_* patterns classify "
            "command replies, so command_reply_marker must be set too."
        )
    incremental = [
        None if raw[field] is None else _compile(path, chosen, field, raw[field])
        for field in _INCREMENTAL_REVIEW_FIELDS
    ]
    rate_limited = raw.get("rate_limited_pattern", ...)
    if not (rate_limited is None or (isinstance(rate_limited, str) and rate_limited)):
        raise ValueError(
            f"{path}: reviewers.{chosen}.rate_limited_pattern must be a non-empty "
            "string or null (and must be present)."
        )
    if rate_limited is not None and raw["command_reply_marker"] is None:
        raise ValueError(
            f"{path}: reviewers.{chosen}: rate_limited_pattern classifies command "
            "replies, so command_reply_marker must be set too."
        )
    wait = raw.get("rate_limit_wait_pattern", ...)
    if not (wait is None or (isinstance(wait, str) and wait)):
        raise ValueError(
            f"{path}: reviewers.{chosen}.rate_limit_wait_pattern must be a non-empty "
            "regex or null (and must be present)."
        )
    if wait is not None and rate_limited is None:
        raise ValueError(
            f"{path}: reviewers.{chosen}: rate_limit_wait_pattern reads a refusal, so "
            "rate_limited_pattern must be set too."
        )
    wait_pattern = None
    if wait is not None:
        wait_pattern = _compile(path, chosen, "rate_limit_wait_pattern", wait)
        _one_group(path, chosen, "rate_limit_wait_pattern", wait_pattern)
    default_wait = raw.get("rate_limit_default_wait_minutes", ...)
    if rate_limited is None:
        if default_wait is not None:
            raise ValueError(
                f"{path}: reviewers.{chosen}.rate_limit_default_wait_minutes must be "
                "null (and present) when rate_limited_pattern is null."
            )
    elif not _positive_int(default_wait):
        raise ValueError(
            f"{path}: reviewers.{chosen}.rate_limit_default_wait_minutes must be a "
            "positive number of minutes: the wait for a refusal that states none."
        )
    notice = raw.get("rate_limit_notice_marker", ...)
    if not (notice is None or (isinstance(notice, str) and notice)):
        raise ValueError(
            f"{path}: reviewers.{chosen}.rate_limit_notice_marker must be a non-empty "
            "string or null (and must be present)."
        )
    if notice is not None and rate_limited is None:
        raise ValueError(
            f"{path}: reviewers.{chosen}: rate_limit_notice_marker reads a refusal's "
            "wait, so rate_limited_pattern must be set too."
        )
    for field in ("local_preflight", "local_review"):
        if field not in raw:
            raise ValueError(
                f"{path}: reviewers.{chosen}.{field} must be present "
                "(an object, or null for a reviewer with no local review)."
            )
    local_review = _load_local_review(path, chosen, raw["local_review"])
    preflight = _load_preflight(path, chosen, raw["local_preflight"])
    if local_review is not None and raw["final_review_comment"] is None:
        raise ValueError(
            f"{path}: reviewers.{chosen}.local_review needs a final full review: the "
            "PR's one review request is final_review_comment, and the gate for a "
            "local reviewer relies on it."
        )
    if preflight is not None and local_review is None:
        raise ValueError(
            f"{path}: reviewers.{chosen}.local_preflight checks the local review's "
            "allowance, so local_review must be set too (or local_preflight null)."
        )

    findings = _compile(path, chosen, "findings_pattern", raw["findings_pattern"])
    _one_group(path, chosen, "findings_pattern", findings)
    # One regex or an ordered list: the reviewer's summary format has changed before,
    # and the first pattern that matches names the reviewed SHA.
    sha_value = raw.get("reviewed_sha_pattern", ...)
    if sha_value is ...:
        raise ValueError(
            f"{path}: reviewers.{chosen}.reviewed_sha_pattern must be present "
            "(a regex, a non-empty list of regexes, or null)."
        )
    reviewed_sha: tuple[re.Pattern[str], ...] = ()
    if sha_value is not None:
        reviewed_sha = _pattern_list(
            path,
            chosen,
            "reviewed_sha_pattern",
            [sha_value] if isinstance(sha_value, str) else sha_value,
            allow_empty=False,
        )
    unthreaded = _pattern_list(
        path,
        chosen,
        "unthreaded_finding_patterns",
        raw.get("unthreaded_finding_patterns"),
        allow_empty=True,
    )

    return ReviewerAdapter(
        key=chosen,
        display_name=raw["display_name"],
        check_name=raw["check_name"],
        trigger_comment=raw["trigger_comment"],
        bot_logins=frozenset(logins),
        summary_marker=raw["summary_marker"],
        clean_pattern=_compile(path, chosen, "clean_pattern", raw["clean_pattern"]),
        findings_pattern=findings,
        reviewed_sha_patterns=reviewed_sha,
        self_resolves_threads=raw["self_resolves_threads"],
        summary_in_issue_comments=raw["summary_in_issue_comments"],
        final_review_comment=raw["final_review_comment"],
        command_reply_marker=raw["command_reply_marker"],
        final_review_done_pattern=final_done,
        final_review_failed_pattern=final_failed,
        final_review_partial_pattern=final_partial,
        incremental_review_done_pattern=incremental[0],
        incremental_review_no_changes_pattern=incremental[1],
        incremental_review_failed_pattern=incremental[2],
        incremental_review_partial_pattern=incremental[3],
        unthreaded_finding_patterns=unthreaded,
        rate_limited_pattern=(
            None
            if rate_limited is None
            else _compile(path, chosen, "rate_limited_pattern", rate_limited)
        ),
        rate_limit_wait_pattern=wait_pattern,
        rate_limit_default_wait_minutes=default_wait,
        rate_limit_notice_marker=notice,
        local_preflight=preflight,
        local_review=local_review,
    )


_THREADS_QUERY = """
query($owner:String!, $name:String!, $pr:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$pr) {
      headRefOid
      mergeStateStatus
      commits(last: 1) {
        nodes { commit { committedDate } }
      }
      reviewThreads(first:100) {
        nodes {
          id
          isResolved
          comments(first:1) {
            nodes { author { login } path line body }
          }
        }
      }
    }
  }
}
"""

# Its own query, not part of _THREADS_QUERY: only a head with no verdict needs it.
_BASE_EVENTS_QUERY = """
query($owner:String!, $name:String!, $pr:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$pr) {
      baseRefOid
      timelineItems(
        last: 100, itemTypes: [BASE_REF_FORCE_PUSHED_EVENT, BASE_REF_CHANGED_EVENT]
      ) {
        pageInfo { hasPreviousPage }
        nodes {
          __typename
          ... on BaseRefForcePushedEvent { createdAt beforeCommit { oid } }
          ... on BaseRefChangedEvent { createdAt }
        }
      }
    }
  }
}
"""


def _is_summary(body: str, adapter: ReviewerAdapter) -> bool:
    """Return whether this review is the summary, not a reply or inline container."""
    if adapter.summary_marker is not None:
        return adapter.summary_marker in body
    # No marker: only a review carrying a verdict counts, so a later reply-style
    # review can't shadow the real summary and flip the verdict to "none".
    return bool(
        adapter.clean_pattern.search(body) or adapter.findings_pattern.search(body)
    )


def _reviewed_sha(body: str, adapter: ReviewerAdapter) -> str | None:
    """Return the SHA a summary body says it reviewed (first matching pattern)."""
    for pattern in adapter.reviewed_sha_patterns:
        match = pattern.search(body)
        if match:
            return match.group(1)
    return None


def _on_head(
    review: Mapping[str, Any], head_sha: str | None, adapter: ReviewerAdapter
) -> bool:
    if head_sha is None or review.get("commit_id") == head_sha:
        return True
    sha = _reviewed_sha(review.get("body") or "", adapter)
    return sha is not None and head_sha.lower().startswith(sha.lower())


def _live(review: Mapping[str, Any]) -> bool:
    """Return whether a review still stands: a dismissed review is not a verdict."""
    return str(review.get("state") or "").upper() != "DISMISSED"


def review_verdict(
    reviews: Iterable[Mapping[str, Any]],
    head_sha: str | None,
    adapter: ReviewerAdapter,
    windows: Sequence[tuple[str, str]] = (),
) -> tuple[str, int]:
    """Return (verdict, finding_count) for the newest reviewer summary on head_sha.

    A summary carrying a verdict decides. Without one, a reviewer review or summary
    on ``head_sha`` that carries no verdict (an empty-body or nitpick-only review; a
    summary naming ``head_sha`` as reviewed) passes only inside one of ``windows``
    -- see ``finished_windows``. Outside them it proves nothing: the reviewer also
    posts empty reviews to hold its thread replies, even while its review is failing.

    Parameters
    ----------
    reviews : Iterable[Mapping[str, Any]]
        PR reviews as returned by the GitHub API (summary comments shaped by
        ``_comment_as_review`` may be among them).
    head_sha : str or None
        Head commit SHA to judge; ``None`` matches any commit.
    adapter : ReviewerAdapter
        Configured reviewer adapter.
    windows : Sequence[tuple[str, str]]
        Completed review windows from ``finished_windows``.

    Returns
    -------
    tuple[str, int]
        ``"pass"`` (clean), ``"findings"`` (with the reported count), or ``"none"``
        (the reviewer has not reviewed this SHA -- trigger it), and the count.
    """
    on_head = [
        r
        for r in reviews
        if (r.get("user") or {}).get("login") in adapter.bot_logins
        and _live(r)
        and _on_head(r, head_sha, adapter)
    ]
    candidates = [r for r in on_head if _is_summary(r.get("body") or "", adapter)]
    if not candidates:
        in_window = any(
            start <= (r.get("submitted_at") or "") <= end
            for r in on_head
            for start, end in windows
        )
        return ("pass", 0) if in_window else ("none", 0)
    newest = max(candidates, key=lambda r: r.get("submitted_at") or "")
    body = newest.get("body") or ""
    if adapter.clean_pattern.search(body):
        return ("pass", 0)
    match = adapter.findings_pattern.search(body)
    if match:
        return ("findings", int(match.group(1)))
    return ("none", 0)


def open_findings(
    threads: Iterable[Mapping[str, Any]], adapter: ReviewerAdapter
) -> list[dict[str, Any]]:
    """Unresolved inline reviewer findings from GraphQL reviewThreads nodes."""
    found: list[dict[str, Any]] = []
    for thread in threads:
        if thread.get("isResolved"):
            continue
        nodes = ((thread.get("comments") or {}).get("nodes")) or []
        if not nodes:
            continue
        first = nodes[0]
        if ((first.get("author") or {}).get("login")) not in adapter.bot_logins:
            continue
        found.append(
            {
                "thread_id": thread.get("id"),
                "path": first.get("path"),
                "line": first.get("line"),
                "body": first.get("body"),
            }
        )
    return found


def unthreaded_findings(
    reviews: Iterable[Mapping[str, Any]],
    comments: Iterable[Mapping[str, Any]],
    head_sha: str,
    adapter: ReviewerAdapter,
) -> list[dict[str, Any]]:
    """Return the findings stated on ``head_sha`` with no thread to reply on.

    Matches the adapter's ``unthreaded_finding_patterns`` against its reviews on the
    head and, when its summary lives in issue comments, against summaries that name
    the head as reviewed. ``open_findings`` reads threads only, so these would be lost.

    Parameters
    ----------
    reviews : Iterable[Mapping[str, Any]]
        PR reviews as returned by the GitHub API.
    comments : Iterable[Mapping[str, Any]]
        PR issue comments.
    head_sha : str
        Head commit SHA the findings must be on.
    adapter : ReviewerAdapter
        Configured reviewer adapter.

    Returns
    -------
    list[dict[str, Any]]
        One dict per finding with ``source`` (``review`` or ``summary_comment``),
        ``id`` of that review or comment, and the finding's ``text``.
    """
    sources: list[tuple[str, Mapping[str, Any]]] = [("review", r) for r in reviews]
    if adapter.summary_in_issue_comments:
        sources += [("summary_comment", _comment_as_review(c)) for c in comments]
    found: list[dict[str, Any]] = []
    for source, item in sources:
        if (
            ((item.get("user") or {}).get("login")) not in adapter.bot_logins
            or not _live(item)
            or not _on_head(item, head_sha, adapter)
        ):
            continue
        body = item.get("body") or ""
        for pattern in adapter.unthreaded_finding_patterns:
            for match in pattern.finditer(body):
                text = match.group(1).strip()
                if text:
                    found.append({"source": source, "id": item.get("id"), "text": text})
    return found


def _requests(ordered: Sequence[Mapping[str, Any]], command: str) -> list[str]:
    """Return the creation times of the comments that are exactly ``command``."""
    return [
        c.get("created_at") or ""
        for c in ordered
        if (c.get("body") or "").strip() == command
    ]


def _replies_between(
    start: str,
    end: str | None,
    comments: Sequence[Mapping[str, Any]],
    adapter: ReviewerAdapter,
) -> list[Mapping[str, Any]]:
    """Return the reviewer's command replies created in ``[start, end)``.

    Every reply in the window, not just the first: an incremental command posted
    around the same time gets a reply with the same marker.
    """
    marker = adapter.command_reply_marker or ""
    return [
        c
        for c in comments
        if start <= (c.get("created_at") or "")
        and (end is None or (c.get("created_at") or "") < end)
        and ((c.get("user") or {}).get("login")) in adapter.bot_logins
        and marker in (c.get("body") or "")
    ]


def _classify(
    start: str,
    end: str | None,
    comments: Sequence[Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]],
    adapter: ReviewerAdapter,
) -> str:
    """Classify one full-review request from the replies before the next request."""
    done = adapter.final_review_done_pattern
    failed = adapter.final_review_failed_pattern
    partial = adapter.final_review_partial_pattern
    if done is None or failed is None or partial is None:  # all set with the comment
        raise ValueError(f"reviewer {adapter.key!r}: final-review patterns are unset")
    replies = _replies_between(start, end, comments, adapter)
    bodies = [r.get("body") or "" for r in replies]
    if any(done.search(b) for b in bodies):
        return "finished"
    # "Action not completed": the review can post while only its summary fails, so a
    # verdict-bearing review between the request and that reply's last edit counts --
    # unless an incremental command (or another command's reply) shares the window,
    # since that review may be the other command's (errs on blocking: stays pending).
    incremental = any(
        start <= (c.get("created_at") or "")
        and (end is None or (c.get("created_at") or "") < end)
        and (c.get("body") or "").strip() == adapter.trigger_comment
        for c in comments
    )
    # A request refused for the rate limit never ran: a review in its window belongs
    # to some other request.
    limited = adapter.rate_limited_pattern
    refused = limited is not None and any(limited.search(b) for b in bodies)
    if refused or incremental or any(not partial.search(b) for b in bodies):
        replies = []
    for reply in replies:
        if not partial.search(reply.get("body") or ""):
            continue
        edited = reply.get("updated_at") or reply.get("created_at") or ""
        for review in reviews:
            if (
                ((review.get("user") or {}).get("login")) in adapter.bot_logins
                and _live(review)
                and _is_summary(review.get("body") or "", adapter)
                and start <= (review.get("submitted_at") or "") <= edited
            ):
                return "finished"
    if any(failed.search(b) for b in bodies):
        return "failed"
    return "pending"


def final_review_outcome(
    comments: Iterable[Mapping[str, Any]],
    reviews: Iterable[Mapping[str, Any]],
    adapter: ReviewerAdapter,
) -> str:
    """Return the state of the reviewer's final full review on this PR.

    ``"not_required"`` when the adapter has no final review; ``"not_requested"``
    when nobody posted ``final_review_comment`` as a whole comment; otherwise
    ``"finished"`` if any request finished, else the newest request's ``"failed"``
    or ``"pending"``. Not tied to the head SHA: fixes after the full review are
    re-checked incrementally, through ``review_verdict``.
    """
    if adapter.final_review_comment is None:
        return "not_required"
    ordered = sorted(comments, key=lambda c: c.get("created_at") or "")
    review_list = list(reviews)
    commands = [
        c
        for c in ordered
        if (c.get("body") or "").strip() == adapter.final_review_comment
    ]
    if not commands:
        return "not_requested"
    starts = [c.get("created_at") or "" for c in commands]
    outcomes = [
        _classify(start, end, ordered, review_list, adapter)
        for start, end in zip(starts, [*starts[1:], None])
    ]
    return "finished" if "finished" in outcomes else outcomes[-1]


def _created(comment: Mapping[str, Any]) -> str:
    return comment.get("created_at") or ""


def _edited(comment: Mapping[str, Any]) -> str:
    return comment.get("updated_at") or comment.get("created_at") or ""


_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _iso(moment: datetime) -> str:
    return moment.strftime(_ISO_FORMAT)


def rate_limit_state(
    comments: Iterable[Mapping[str, Any]], adapter: ReviewerAdapter
) -> dict[str, Any]:
    """Return whether the reviewer refused the newest review request for its rate limit.

    The newest request is the newest exact trigger or final-review comment. A refusal
    is a command reply after it matching ``rate_limited_pattern``. ``retry_after`` is
    the reply's time plus the wait it states (``rate_limit_wait_pattern``, minutes);
    when it states none, the edit time of the rate-limit notice plus the wait the
    notice states (only a notice edited at or after the request describes it); else
    the reply's time plus ``rate_limit_default_wait_minutes``. The agent waits until
    then and re-posts the SAME request (``request``); a refusal never calls for a
    different one.

    Parameters
    ----------
    comments : Iterable[Mapping[str, Any]]
        PR issue comments.
    adapter : ReviewerAdapter
        Configured reviewer adapter.

    Returns
    -------
    dict[str, Any]
        ``refused``, ``request`` (``trigger``, ``final`` or ``None``),
        ``replied_at``, ``retry_after`` and ``wait_source`` (``reply``, ``notice``,
        ``default`` or ``None``).
    """
    ordered = sorted(comments, key=_created)
    kinds = {adapter.trigger_comment: "trigger"}
    if adapter.final_review_comment is not None:
        kinds[adapter.final_review_comment] = "final"
    newest = next(
        (c for c in reversed(ordered) if (c.get("body") or "").strip() in kinds), None
    )
    state: dict[str, Any] = {
        "refused": False,
        "request": None,
        "replied_at": None,
        "retry_after": None,
        "wait_source": None,
    }
    if newest is None:
        return state
    state["request"] = kinds[(newest.get("body") or "").strip()]
    limited = adapter.rate_limited_pattern
    if limited is None:
        return state
    refusal = next(
        (
            r
            for r in _replies_between(_created(newest), None, ordered, adapter)
            if limited.search(r.get("body") or "")
        ),
        None,
    )
    if refusal is None:
        return state
    replied = datetime.strptime(_created(refusal), _ISO_FORMAT)
    wait = adapter.rate_limit_wait_pattern
    stated = wait.search(refusal.get("body") or "") if wait is not None else None
    if stated:
        start, minutes, source = replied, int(stated.group(1)), "reply"
    else:
        start, minutes, source = (
            replied,
            adapter.rate_limit_default_wait_minutes or 0,
            "default",
        )
        notice = _rate_limit_notice(ordered, adapter, since=_created(newest))
        if notice is not None:
            start, minutes, source = notice[0], notice[1], "notice"
    state.update(
        refused=True,
        replied_at=_iso(replied),
        retry_after=_iso(start + timedelta(minutes=minutes)),
        wait_source=source,
    )
    return state


def _rate_limit_notice(
    ordered: Sequence[Mapping[str, Any]], adapter: ReviewerAdapter, since: str
) -> tuple[datetime, int] | None:
    """Return ``(edited at, minutes)`` from a rate-limit notice edited since ``since``.

    The reviewer edits one notice comment in place on each refused attempt, so only
    an edit at or after the request describes it; a notice left from an earlier
    attempt doesn't.
    """
    marker, wait = adapter.rate_limit_notice_marker, adapter.rate_limit_wait_pattern
    if marker is None or wait is None:
        return None
    for comment in reversed(ordered):
        body = comment.get("body") or ""
        if (
            ((comment.get("user") or {}).get("login")) in adapter.bot_logins
            and marker in body
            and _edited(comment) >= since
        ):
            match = wait.search(body)
            if match:
                edited = datetime.strptime(_edited(comment), _ISO_FORMAT)
                return edited, int(match.group(1))
    return None


#: Opens the hidden record a local review leaves on the PR (``local_review.py``).
LOCAL_REVIEW_MARKER = "<!-- local-review:v1 "
_LOCAL_RECORD_RE = re.compile(r"<!-- local-review:v1 (\{.*?\}) -->", re.DOTALL)


def local_review_marker(record: Mapping[str, Any]) -> str:
    """Return the hidden marker that records one local review round on the PR.

    Parameters
    ----------
    record : Mapping[str, Any]
        ``reviewer``, ``head``, ``base``, ``round``, ``phase`` and ``findings``.

    Returns
    -------
    str
        An HTML comment the PR renders invisibly.
    """
    return f"{LOCAL_REVIEW_MARKER}{json.dumps(dict(record), sort_keys=True)} -->"


def local_review_records(
    comments: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return every local review recorded on the PR, oldest first, with its ``at``.

    Parameters
    ----------
    comments : Iterable[Mapping[str, Any]]
        PR issue comments.

    Returns
    -------
    list[dict[str, Any]]
        The parsed records; a malformed marker is skipped.
    """
    records = []
    for comment in sorted(comments, key=_created):
        match = _LOCAL_RECORD_RE.search(comment.get("body") or "")
        if not match:
            continue
        try:
            record = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("head"), str):
            records.append(dict(record, at=_created(comment)))
    return records


def _first_final_at(
    ordered: Sequence[Mapping[str, Any]], adapter: ReviewerAdapter
) -> str | None:
    """Return when the first final-review request the reviewer didn't refuse was made.

    A refused request never ran, so it doesn't start the after-final phase.
    """
    command = adapter.final_review_comment
    if command is None:
        return None
    requests = [
        c
        for c in ordered
        if (c.get("body") or "").strip() in (command, adapter.trigger_comment)
    ]
    limited = adapter.rate_limited_pattern
    for position, comment in enumerate(requests):
        if (comment.get("body") or "").strip() != command:
            continue
        end = _created(requests[position + 1]) if position + 1 < len(requests) else None
        replies = _replies_between(_created(comment), end, ordered, adapter)
        if limited is None or not any(
            limited.search(r.get("body") or "") for r in replies
        ):
            return _created(comment)
    return None


def pr_round_caps(adapter: ReviewerAdapter, caps: RoundCaps) -> RoundCaps:
    """Return the PR incremental caps for this reviewer.

    A reviewer with a local review spends its fix rounds locally, so the PR gets no
    incremental rounds: its one review request is the final full review.

    Parameters
    ----------
    adapter : ReviewerAdapter
        Configured reviewer adapter.
    caps : RoundCaps
        Configured round caps.

    Returns
    -------
    RoundCaps
        ``caps``, with zero PR incremental rounds when ``local_review`` is set.
    """
    if adapter.local_review is None:
        return caps
    return caps._replace(before_final=0, after_final=0)


def local_review_state(
    comments: Iterable[Mapping[str, Any]],
    adapter: ReviewerAdapter,
    caps: RoundCaps,
    head_sha: str,
) -> dict[str, Any]:
    """Budget the PR's local review rounds from their records.

    Rounds before the first final-review request the reviewer didn't refuse, and
    rounds after it, are capped separately (``local_before_final`` /
    ``local_after_final``).

    Parameters
    ----------
    comments : Iterable[Mapping[str, Any]]
        PR issue comments.
    adapter : ReviewerAdapter
        Configured reviewer adapter.
    caps : RoundCaps
        Round caps.
    head_sha : str
        The PR's current head.

    Returns
    -------
    dict[str, Any]
        ``{"configured": False}`` with no local review; otherwise ``phase``,
        ``before_final`` / ``after_final`` ``{used, cap}``, ``allowed``,
        ``reviewed_head`` and ``head_findings``.
    """
    if adapter.local_review is None:
        return {"configured": False}
    ordered = sorted(comments, key=_created)
    first_final = _first_final_at(ordered, adapter)
    records = local_review_records(ordered)
    before = sum(1 for r in records if first_final is None or r["at"] < first_final)
    after = len(records) - before
    phase = "after_final" if first_final is not None else "before_final"
    budget = {
        "before_final": {"used": before, "cap": caps.local_before_final},
        "after_final": {"used": after, "cap": caps.local_after_final},
    }
    current = budget[phase]
    on_head = [r for r in records if r["head"] == head_sha]
    return {
        "configured": True,
        "phase": phase,
        **budget,
        "allowed": current["used"] < current["cap"],
        "reviewed_head": bool(on_head),
        "head_findings": on_head[-1].get("findings") if on_head else None,
    }


class IncrementalReview(NamedTuple):
    """The reviewer's answer to the newest trigger comment."""

    outcome: str
    requested_at: str | None
    replied_at: str | None


def _verdict_review_since(
    reviews: Iterable[Mapping[str, Any]],
    head_sha: str,
    since: str,
    adapter: ReviewerAdapter,
) -> list[str]:
    """Return when the reviewer posted verdict-bearing reviews of ``head_sha`` since.

    Only a review carrying a verdict counts: an empty or nitpick-only one also holds
    thread replies, so it says nothing about which request it answers.
    """
    return [
        r.get("submitted_at") or ""
        for r in reviews
        if r.get("commit_id") == head_sha
        and ((r.get("user") or {}).get("login")) in adapter.bot_logins
        and _live(r)
        and _is_summary(r.get("body") or "", adapter)
        and (r.get("submitted_at") or "") >= since
    ]


def incremental_review(
    comments: Iterable[Mapping[str, Any]],
    adapter: ReviewerAdapter,
    reviews: Iterable[Mapping[str, Any]] = (),
    head_sha: str | None = None,
) -> IncrementalReview:
    """Classify the reviewer's answer to the newest trigger comment.

    Every reply after the newest trigger counts, so a reply to an earlier trigger
    that lands late is attributed to the newest. The reply is not the only signal:
    it can lag the review by minutes. A verdict-bearing reviewer review on
    ``head_sha`` submitted after the trigger also answers it (``"finished"``, with
    ``replied_at`` that review's time). A verdict-less review does not: the reviewer
    posts those to hold thread replies too.

    Parameters
    ----------
    comments : Iterable[Mapping[str, Any]]
        PR issue comments.
    adapter : ReviewerAdapter
        Configured reviewer adapter.
    reviews : Iterable[Mapping[str, Any]]
        PR reviews, to detect a review that landed before its reply moved.
    head_sha : str or None
        Head commit SHA a landed review must be on; ``None`` skips that check.

    Returns
    -------
    IncrementalReview
        ``outcome`` is ``"not_tracked"`` (the adapter has no incremental reply
        patterns), ``"not_requested"`` (no trigger comment yet), ``"pending"`` (no
        answer yet), or ``"finished"`` / ``"no_changes"`` / ``"failed"`` /
        ``"not_completed"``; with the request and reply times.
    """
    patterns = (
        adapter.incremental_review_done_pattern,
        adapter.incremental_review_no_changes_pattern,
        adapter.incremental_review_failed_pattern,
        adapter.incremental_review_partial_pattern,
    )
    if patterns[0] is None:
        return IncrementalReview("not_tracked", None, None)
    ordered = sorted(comments, key=_created)
    starts = _requests(ordered, adapter.trigger_comment)
    if not starts:
        return IncrementalReview("not_requested", None, None)
    requested = starts[-1]
    replies = _replies_between(requested, None, ordered, adapter)
    outcomes = ("finished", "no_changes", "failed", "not_completed")
    for outcome, pattern in zip(outcomes, patterns):
        for reply in replies:
            if pattern is not None and pattern.search(reply.get("body") or ""):
                return IncrementalReview(outcome, requested, _created(reply))
    if head_sha is not None:
        landed = _verdict_review_since(reviews, head_sha, requested, adapter)
        if landed:
            return IncrementalReview("finished", requested, min(landed))
    return IncrementalReview("pending", requested, None)


def finished_windows(
    comments: Iterable[Mapping[str, Any]], adapter: ReviewerAdapter
) -> list[tuple[str, str]]:
    """Return ``(request, reply's last edit)`` for every review request that finished.

    Both trigger and full-review requests count. A review the reviewer posted inside
    one of these windows is part of a review that ran to completion. A window also
    ends at the next request of either kind: a late edit to one request's reply must
    not vouch for reviews posted while a later request ran (and maybe failed).

    Parameters
    ----------
    comments : Iterable[Mapping[str, Any]]
        PR issue comments.
    adapter : ReviewerAdapter
        Configured reviewer adapter.

    Returns
    -------
    list[tuple[str, str]]
        ``(request time, end time)`` pairs, the end being the done reply's last
        edit or the next request, whichever is earlier.
    """
    ordered = sorted(comments, key=_created)
    commands = [
        (command, done)
        for command, done in (
            (adapter.trigger_comment, adapter.incremental_review_done_pattern),
            (adapter.final_review_comment, adapter.final_review_done_pattern),
        )
        if command is not None and done is not None
    ]
    every_start = sorted(
        t for command, _ in commands for t in _requests(ordered, command)
    )
    windows: list[tuple[str, str]] = []
    for command, done in commands:
        starts = _requests(ordered, command)
        for start, end in zip(starts, [*starts[1:], None]):
            later = [t for t in every_start if t > start]
            for reply in _replies_between(start, end, ordered, adapter):
                if done.search(reply.get("body") or ""):
                    windows.append((start, min([_edited(reply), *later[:1]])))
    return windows


class CarrySource(NamedTuple):
    """The reviewed commit whose verdict a head may inherit."""

    sha: str
    verdict: str
    finding_count: int
    # The commit's earliest live reviewer review: the base it saw is the base then.
    reviewed_at: str


def carry_source(
    reviews: Sequence[Mapping[str, Any]],
    head_sha: str,
    adapter: ReviewerAdapter,
    windows: Sequence[tuple[str, str]],
    before: str | None = None,
) -> CarrySource | None:
    """Return the newest reviewed commit other than the head that has a verdict.

    Only the newest counts as a source: an older commit's verdict never stands in
    for a newer review. A commit whose reviews carry no verdict (e.g. its review
    failed after thread-reply reviews) is skipped.

    Parameters
    ----------
    reviews : Sequence[Mapping[str, Any]]
        PR reviews as returned by the GitHub API.
    head_sha : str
        Current head SHA, excluded as a source.
    adapter : ReviewerAdapter
        Configured reviewer adapter.
    windows : Sequence[tuple[str, str]]
        Completed review windows from ``finished_windows``.
    before : str or None
        Only consider reviews submitted at or before this timestamp.

    Returns
    -------
    CarrySource or None
        The newest reviewed commit carrying a verdict, or ``None``.
    """
    earlier = sorted(
        (
            r
            for r in reviews
            if ((r.get("user") or {}).get("login")) in adapter.bot_logins
            and _live(r)
            and r.get("commit_id") not in (None, head_sha)
            and (before is None or (r.get("submitted_at") or "") <= before)
        ),
        key=lambda r: r.get("submitted_at") or "",
        reverse=True,
    )
    seen: set[str] = set()
    for review in earlier:
        sha = review["commit_id"]
        if sha in seen:
            continue
        seen.add(sha)
        verdict, count = review_verdict(reviews, sha, adapter, windows)
        if verdict != "none":
            reviewed_at = min(
                r.get("submitted_at") or "" for r in earlier if r["commit_id"] == sha
            )
            return CarrySource(sha, verdict, count, reviewed_at)
    return None


# The numbers in a hunk header move with the base; the change under them doesn't.
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.MULTILINE)
# GitHub's compare API lists at most 300 files, so a list that long may be cut off.
_COMPARE_FILE_CAP = 300

#: The most size-omitted patches one compare may resolve by blob pair. Each costs
#: one contents call, and the review gate's sweep runs on every open PR, so a
#: compare with more of them than this is not vouched for (no carry).
_OMITTED_PATCH_LOOKUP_CAP = 20

#: ``(path, ref) -> blob SHA`` of ``path`` at commit ``ref``, or ``None`` when unknown.
BaseBlob = Callable[[str, str], "str | None"]


def patch_fingerprint(compare: Any, base_blob: BaseBlob | None = None) -> str | None:
    """Hash the diff in one compare response, ignoring hunk line numbers.

    Two commits with the same fingerprint make the same change to the same files,
    wherever their bases put it -- ``git patch-id``'s rule. A file with no patch and
    no changed lines (a binary, or a rename with no edits) counts by its blob SHA.

    A text file whose patch GitHub left out for size counts by its blob PAIR when
    ``base_blob`` is given: its blob at the compare's merge base and at the head.
    The same pair is the same change, exactly, so one very large file doesn't block
    every carry.

    Parameters
    ----------
    compare : Any
        GitHub compare API response.
    base_blob : BaseBlob or None
        Looks up a file's blob SHA at a commit, to fingerprint size-omitted patches.

    Returns
    -------
    str or None
        The fingerprint; ``None`` when the response can't vouch for the whole
        diff: a list at GitHub's 300-file cap, a payload of any other shape, or a
        size-omitted patch whose blob pair can't be read (no ``base_blob``, an
        unknown base blob, no merge base, a removed or copied file, or more than
        ``_OMITTED_PATCH_LOOKUP_CAP`` such files).
    """
    files = compare.get("files") if isinstance(compare, Mapping) else None
    if (
        not isinstance(files, list)
        or len(files) >= _COMPARE_FILE_CAP
        or not all(isinstance(f, Mapping) for f in files)
    ):
        return None
    omitted = [f for f in files if _patch_omitted_for_size(f)]
    if omitted and (base_blob is None or len(omitted) > _OMITTED_PATCH_LOOKUP_CAP):
        return None
    merge_base = _merge_base_sha(compare)
    digest = hashlib.sha256()
    for entry in sorted(files, key=lambda f: str(f.get("filename") or "")):
        if not _patch_omitted_for_size(entry):
            body = _file_body(entry)
        elif base_blob is not None:  # always set here: checked above
            body = _omitted_body(entry, merge_base, base_blob)
        else:
            body = None
        if body is None:
            return None
        for part in (
            entry["status"],
            entry.get("previous_filename"),
            entry["filename"],
            body,
        ):
            digest.update(str(part or "").encode())
            digest.update(b"\0")
    return digest.hexdigest()


def _patch_omitted_for_size(entry: Mapping[str, Any]) -> bool:
    """Return whether GitHub left a text file's patch out (no patch, lines changed)."""
    changes = entry.get("changes")
    return (
        entry.get("patch") is None
        and isinstance(changes, int)
        and not isinstance(changes, bool)
        and changes > 0
    )


def _merge_base_sha(compare: Mapping[str, Any]) -> str | None:
    """Return the compare's merge-base commit SHA, or ``None`` when it is missing."""
    commit = compare.get("merge_base_commit")
    sha = commit.get("sha") if isinstance(commit, Mapping) else None
    return sha if isinstance(sha, str) and sha else None


def _omitted_body(
    entry: Mapping[str, Any], merge_base: str | None, base_blob: BaseBlob
) -> str | None:
    """Return a size-omitted file's blob pair, or ``None`` if it can't be read.

    The head-side blob is the entry's own ``sha``. The base-side blob is read at the
    merge base: from the old path for a rename, and none at all for an added file.
    A removed, copied or otherwise-shaped entry is ``None`` (not vouched for).
    """
    status, head = entry.get("status"), entry.get("sha")
    if (
        not isinstance(head, str)
        or not head
        or not isinstance(entry.get("filename"), str)
    ):
        return None
    if status == "added":
        return f"omitted-patch none -> {head}"
    if status == "modified":
        path = entry["filename"]
    elif status == "renamed" and isinstance(entry.get("previous_filename"), str):
        path = entry["previous_filename"]
    else:
        return None
    if merge_base is None:
        return None
    base = base_blob(path, merge_base)
    if not isinstance(base, str) or not base:
        return None
    return f"omitted-patch {base} -> {head}"


def _file_body(entry: Mapping[str, Any]) -> str | None:
    """Return what one compare file entry contributes, or ``None`` if it can't vouch.

    A text change is its patch with hunk numbers removed. A file with no patch must
    show zero changed lines and a blob SHA (a binary, or a rename with no edits);
    anything else -- a patch GitHub left out for size, a missing name or status --
    is ``None``, so two incomplete entries can never fingerprint alike.
    """
    for field in ("filename", "status"):
        if not isinstance(entry.get(field), str) or not entry[field]:
            return None
    patch = entry.get("patch")
    if isinstance(patch, str):
        return _HUNK_HEADER.sub("@@", patch)
    changes, sha = entry.get("changes"), entry.get("sha")
    if (
        patch is not None
        or isinstance(changes, bool)
        or changes != 0
        or not isinstance(sha, str)
        or not sha
    ):
        return None
    return f"blob {sha}"


def base_at_review(
    events: Iterable[Any],
    reviewed_at: str,
    current_base: str,
    *,
    complete: bool,
) -> str | None:
    """Return a commit on the base lineage the PR had at ``reviewed_at``.

    The first base event at or after the review decides. A force-push gives the tip
    it replaced, which fast-forward pushes since the review only extended, so its
    merge-base with the reviewed commit is the base that review saw. No later event
    gives ``current_base``. A retarget (``BaseRefChangedEvent``) records no commit,
    so the base is unknown, and so is a node GraphQL couldn't resolve (it may be a
    retarget). When older events may be missing, the events must reach back past
    the review to be trusted.

    Parameters
    ----------
    events : Iterable[Any]
        Base-ref timeline events of the PR (GraphQL nodes; an unresolved one is
        ``null``).
    reviewed_at : str
        Timestamp of the review.
    current_base : str
        The PR's current base commit.
    complete : bool
        Whether ``events`` is the PR's full event history.

    Returns
    -------
    str or None
        A commit on the base lineage at ``reviewed_at``, or ``None`` when unknown.
    """
    listed = list(events)
    if not all(isinstance(e, Mapping) for e in listed):
        return None
    ordered = sorted(listed, key=lambda e: e.get("createdAt") or "")
    if not complete and (
        not ordered or (ordered[0].get("createdAt") or "") >= reviewed_at
    ):
        return None
    for event in ordered:
        if (event.get("createdAt") or "") < reviewed_at:
            continue
        if event.get("__typename") != "BaseRefForcePushedEvent":
            return None
        oid = (event.get("beforeCommit") or {}).get("oid")
        return oid if isinstance(oid, str) and oid else None
    return current_base


def round_budget(
    comments: Iterable[Mapping[str, Any]],
    adapter: ReviewerAdapter,
    caps: RoundCaps,
    *,
    free_no_changes: bool = True,
) -> dict[str, Any]:
    """Count the incremental rounds already requested on this PR against the caps.

    A round is one trigger comment (the exact ``trigger_comment``) that the reviewer
    did not answer with "failed" or "no changes" ("not completed" still counts: the
    review often posted), nor refuse for its rate limit (``rate_limited_pattern``;
    a refused final-review request doesn't start the after-final phase either).
    Rounds before the first final-review request and rounds after it are budgeted
    separately, split by position in the comment stream. A reviewer with no final
    review gets one budget of both caps combined.

    Parameters
    ----------
    comments : Iterable[Mapping[str, Any]]
        PR issue comments.
    adapter : ReviewerAdapter
        Configured reviewer adapter.
    caps : RoundCaps
        Round caps to budget against (``pr_round_caps`` for the live PR).
    free_no_changes : bool
        Whether a "no changes" answer leaves the round uncounted. ``False`` counts
        it: with no verdict to carry, re-posting would get the same answer forever,
        so the budget must bound it.

    Returns
    -------
    dict[str, Any]
        ``phase``; ``incremental_before_final`` / ``incremental_after_final`` as
        ``{"used": int, "cap": int}``; ``trigger_allowed`` (another trigger fits in
        the current phase); ``final_review_due`` (the before-final budget is spent,
        so the next review is the final full review); and ``max_fix_iterations``,
        the fix loop's outer limit, passed through so the skills read it from here.
    """
    ordered = sorted(comments, key=_created)

    def _is(comment: Mapping[str, Any], command: str | None) -> bool:
        return command is not None and (comment.get("body") or "").strip() == command

    requests = [
        i
        for i, c in enumerate(ordered)
        if _is(c, adapter.trigger_comment) or _is(c, adapter.final_review_comment)
    ]

    def _window(position: int) -> list[Mapping[str, Any]]:
        """Return the reviewer's replies to one request, up to the next request."""
        end = (
            _created(ordered[requests[position + 1]])
            if position + 1 < len(requests)
            else None
        )
        return _replies_between(
            _created(ordered[requests[position]]), end, ordered, adapter
        )

    # A request refused for the rate limit never ran. It spends no round, and a
    # refused final request doesn't start the after-final phase -- or one refusal
    # would make the final review "due" and invite a second refused request.
    limited = adapter.rate_limited_pattern
    refused = {
        requests[position]
        for position in range(len(requests))
        if limited is not None
        and any(limited.search(r.get("body") or "") for r in _window(position))
    }
    first_final = next(
        (
            i
            for i in requests
            if i not in refused and _is(ordered[i], adapter.final_review_comment)
        ),
        None,
    )
    # Only an outright failure is not a round. "Not completed" often means just the
    # summary failed while the review itself posted, so it counts; its retry is
    # governed by ``trigger_allowed`` like any other trigger.
    failed = adapter.incremental_review_failed_pattern
    no_changes = adapter.incremental_review_no_changes_pattern
    before_used = after_used = 0
    for position, i in enumerate(requests):
        if i in refused or not _is(ordered[i], adapter.trigger_comment):
            continue
        # A request the reviewer answered "failed" is not a round: re-posting it must
        # not spend the budget. Nor is one answered "no files to review": the head's
        # verdict carried from the commit before it.
        replies = _window(position)
        if any(
            pattern is not None and pattern.search(r.get("body") or "")
            for r in replies
            for pattern in (failed, no_changes if free_no_changes else None)
        ):
            continue
        # Partition by position, not timestamp: a trigger that ties with the final
        # request must land in exactly one phase.
        if first_final is None or i < first_final:
            before_used += 1
        else:
            after_used += 1
    if adapter.final_review_comment is None:
        before = {"used": before_used, "cap": caps.before_final + caps.after_final}
        after = {"used": 0, "cap": 0}
        phase = "before_final"
    else:
        before = {"used": before_used, "cap": caps.before_final}
        after = {"used": after_used, "cap": caps.after_final}
        phase = "after_final" if first_final is not None else "before_final"
    current = after if phase == "after_final" else before
    return {
        "phase": phase,
        "incremental_before_final": before,
        "incremental_after_final": after,
        "trigger_allowed": current["used"] < current["cap"],
        "final_review_due": (
            adapter.final_review_comment is not None
            and phase == "before_final"
            and before["used"] >= before["cap"]
        ),
        "max_fix_iterations": caps.fix_iterations,
    }


def _comment_as_review(comment: Mapping[str, Any]) -> dict[str, Any]:
    """Shape an issue-comment like a review so ``review_verdict`` can judge it.

    A summary comment is edited in place, so ``updated_at`` is its recency, and it
    carries no ``commit_id`` -- the adapter's ``reviewed_sha_pattern`` must say which
    head it covers.
    """
    return {
        "id": comment.get("id"),
        "body": comment.get("body"),
        "user": comment.get("user"),
        "commit_id": None,
        "submitted_at": comment.get("updated_at") or comment.get("created_at"),
    }


def _gh_json(args: Sequence[str]) -> Any:
    """Run ``gh`` and parse its JSON; ``--paginate`` pages of arrays are flattened.

    Recent ``gh`` merges array pages into one array, but a ``gh`` that prints one
    array per page must not break the parse.
    """
    out = subprocess.run(  # nosec B603 B607 -- fixed gh argv, no shell
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout
    decoder, docs, i = json.JSONDecoder(), [], 0
    while i < len(out):
        if out[i].isspace():
            i += 1
            continue
        doc, i = decoder.raw_decode(out, i)
        docs.append(doc)
    if not docs:
        return None
    if len(docs) > 1 and all(isinstance(d, list) for d in docs):
        return [item for page in docs for item in page]
    return docs[0] if len(docs) == 1 else docs


def _compare(repo: str, base: str, head: str) -> Any:
    """Return GitHub's compare of ``base...head``.

    ``per_page=1`` pages the commit list only; page one always lists every file.
    """
    return _gh_json(["api", f"repos/{repo}/compare/{base}...{head}?per_page=1"])


def _blob_at(repo: str, path: str, ref: str) -> str | None:
    """Return ``path``'s blob SHA at commit ``ref`` from the contents API.

    A directory listing, or any payload without a file ``sha``, is ``None``. A
    missing path makes ``gh`` fail, which the caller treats as "no carry".
    """
    payload = _gh_json(
        ["api", f"repos/{repo}/contents/{quote(path, safe='/')}?ref={ref}"]
    )
    if not isinstance(payload, Mapping) or payload.get("type") != "file":
        return None
    sha = payload.get("sha")
    return sha if isinstance(sha, str) and sha else None


def _same_patch_carry(repo: str, pr: int, head_sha: str, source: CarrySource) -> bool:
    """Return whether the head's own diff is the same patch ``source`` was reviewed on.

    The head's own diff is from the PR's current base; the source's is from the base
    the PR had when it was reviewed (``base_at_review``). Any doubt -- a missing
    field, a payload of the wrong shape, an unknown base, an incomplete compare, a
    ``gh`` error or output that isn't JSON -- is ``False``: the head then needs a
    review of its own.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    pr : int
        Pull request number.
    head_sha : str
        The PR's current head.
    source : CarrySource
        The reviewed commit whose verdict the head would inherit.

    Returns
    -------
    bool
        Whether both diffs fingerprint alike.
    """
    owner, name = repo.split("/", 1)
    try:
        graph = _gh_json(
            [
                "api",
                "graphql",
                "-f",
                f"query={_BASE_EVENTS_QUERY}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"pr={pr}",
            ]
        )
        pull: Any = graph
        for key in ("data", "repository", "pullRequest"):
            pull = pull.get(key) if isinstance(pull, Mapping) else None
        if not isinstance(pull, Mapping):
            return False
        current = pull.get("baseRefOid")
        timeline = pull.get("timelineItems")
        nodes = timeline.get("nodes") if isinstance(timeline, Mapping) else None
        if not isinstance(current, str) or not current or not isinstance(nodes, list):
            return False
        page = timeline.get("pageInfo") if isinstance(timeline, Mapping) else None
        has_previous = (
            page.get("hasPreviousPage") if isinstance(page, Mapping) else None
        )
        # An unknown page state is not a complete timeline.
        if not isinstance(has_previous, bool):
            return False
        base = base_at_review(
            nodes, source.reviewed_at, current, complete=not has_previous
        )
        if base is None:
            return False

        def blob(path: str, ref: str) -> str | None:
            return _blob_at(repo, path, ref)

        head_print = patch_fingerprint(_compare(repo, current, head_sha), blob)
        if head_print is None:
            return False
        return head_print == patch_fingerprint(_compare(repo, base, source.sha), blob)
    except (subprocess.CalledProcessError, ValueError):
        return False


def collect_state(
    repo: str,
    pr: int,
    adapter: ReviewerAdapter,
    caps: RoundCaps = DEFAULT_ROUND_CAPS,
) -> dict[str, Any]:
    """Fetch one PR's reviews, threads and comments and return the reviewer state.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    pr : int
        Pull request number.
    adapter : ReviewerAdapter
        Configured reviewer adapter.
    caps : RoundCaps
        Round caps (``review_rounds`` in the config) for the PR's round budget and
        its local review rounds.

    Returns
    -------
    dict[str, Any]
        The reviewer state agents and ``review_gate.py`` act on.
    """
    owner, name = repo.split("/", 1)
    graph = _gh_json(
        [
            "api",
            "graphql",
            "-f",
            f"query={_THREADS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"pr={pr}",
        ]
    )
    pull = graph["data"]["repository"]["pullRequest"]
    head_sha = pull["headRefOid"]
    commit_nodes = (pull.get("commits") or {}).get("nodes") or []
    committed_at = (
        ((commit_nodes[-1] or {}).get("commit") or {}).get("committedDate") or ""
        if commit_nodes
        else ""
    )
    threads = pull["reviewThreads"]["nodes"]
    raw_reviews = (
        _gh_json(["api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate"]) or []
    )
    # Always read the issue comments: trigger comments are issue comments, and the
    # incremental outcome counts them for every reviewer.
    comments: list[Any] = (
        _gh_json(["api", f"repos/{repo}/issues/{pr}/comments", "--paginate"]) or []
    )
    reviews = list(raw_reviews)
    if adapter.summary_in_issue_comments:
        # Some reviewers post a clean re-review as an empty review and keep the
        # verdict in an issue-comment they edit in place (see the adapter notes).
        reviews += [_comment_as_review(c) for c in comments]
    # When did the head reach the PR? committedDate is the commit's time, not the
    # push's: a commit made, then a trigger posted, then the push, would read as
    # "requested after the head". The head's first commit status is posted after the
    # push (the review gate posts one on every push), so the later of the two bounds
    # it. Only a reviewer whose replies are tracked needs it.
    first_status = ""
    if adapter.incremental_review_done_pattern is not None:
        statuses = (
            _gh_json(["api", f"repos/{repo}/commits/{head_sha}/statuses", "--paginate"])
            or []
        )
        first_status = min((st.get("created_at") or "" for st in statuses), default="")
    head_at = max(committed_at, first_status)
    final = final_review_outcome(comments, raw_reviews, adapter)
    incremental = incremental_review(comments, adapter, raw_reviews, head_sha)
    windows = finished_windows(comments, adapter)

    verdict, count = review_verdict(reviews, head_sha, adapter, windows)
    # A trigger older than the head asked about an earlier commit, so its answer
    # (running, or "no changes") says nothing about this head. A verdict-bearing
    # reviewer review of this head posted after the request also proves the request
    # covered it (a trigger posted seconds after a push can beat the first status).
    # Not a verdict-less one: those also hold thread replies.
    requested = incremental.requested_at
    on_head = requested is not None and (
        requested >= head_at
        or bool(_verdict_review_since(raw_reviews, head_sha, requested, adapter))
    )
    carried_from: str | None = None
    carry_reason: str | None = None
    if verdict == "none" and incremental.outcome == "no_changes" and on_head:
        # The reviewer had nothing new on this head: the newest verdict it had given
        # by the time it said so still describes it.
        source = carry_source(
            reviews, head_sha, adapter, windows, before=incremental.replied_at or ""
        )
        if source is not None:
            verdict, count = source.verdict, source.finding_count
            carried_from, carry_reason = source.sha, "no_changes"
    if verdict == "none":
        # A rebase or a merge from the base gave the head a new SHA but not a new
        # diff, so the newest verdict still describes it.
        source = carry_source(reviews, head_sha, adapter, windows)
        if source is not None and _same_patch_carry(repo, pr, head_sha, source):
            verdict, count = source.verdict, source.finding_count
            carried_from, carry_reason = source.sha, "same_patch"
    running = incremental.outcome == "pending" and on_head
    findings = open_findings(threads, adapter)
    unresolved_total = sum(1 for t in threads if not t.get("isResolved"))
    return {
        "head_sha": head_sha,
        "head_committed_at": committed_at or None,
        "head_first_status_at": first_status or None,
        # UNKNOWN when GitHub has not computed it yet, or when the PR is MERGED.
        # Treat UNKNOWN as "do not act", never as "not BEHIND".
        "merge_state_status": pull["mergeStateStatus"],
        "reviewer": adapter.key,
        "reviewer_name": adapter.display_name,
        "check_name": adapter.check_name,
        "trigger_comment": adapter.trigger_comment,
        "self_resolves_threads": adapter.self_resolves_threads,
        "verdict": verdict,
        "reported_finding_count": count,
        "verdict_carried_from": carried_from,
        "verdict_carry_reason": carry_reason,
        "open_findings": findings,
        "unresolved_thread_total": unresolved_total,
        "incremental_review_outcome": incremental.outcome,
        "incremental_review_on_head": on_head,
        # The newest trigger covered this head and has no answer yet: wait for it
        # rather than stack a second request.
        "review_running": running,
        "unthreaded_findings": unthreaded_findings(
            raw_reviews, comments, head_sha, adapter
        ),
        # A reviewer with a local review never takes a trigger on the PR: its fix
        # rounds run locally (``local_review.py``) and the PR's one review request
        # is the final full review.
        "needs_trigger": verdict == "none"
        and not running
        and adapter.local_review is None,
        "final_review_comment": adapter.final_review_comment,
        "final_review_outcome": final,
        "needs_final_review": final not in ("finished", "not_required"),
        # The newest request was refused for the rate limit: wait until
        # retry_after, then re-post the SAME request. Never escalate.
        "rate_limit": rate_limit_state(comments, adapter),
        "local_review": local_review_state(comments, adapter, caps, head_sha),
        # "No changes" is free unless the head is left with no verdict: then the
        # budget must bound re-posting a trigger that gets the same answer.
        "round_budget": round_budget(
            comments,
            adapter,
            pr_round_caps(adapter, caps),
            free_no_changes=not (
                incremental.outcome == "no_changes" and verdict == "none"
            ),
        ),
    }


def _non_negative_int(value: str) -> int:
    """Parse a round-cap CLI value, applying the same rule as ``load_round_caps``.

    Parameters
    ----------
    value : str
        The flag's value.

    Returns
    -------
    int
        The cap.

    Raises
    ------
    argparse.ArgumentTypeError
        The value is not an integer, or is negative.
    """
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"a round cap must be an integer, got {value!r}"
        ) from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"a round cap must be >= 0, got {number}")
    return number


def main() -> None:
    """Print ``collect_state`` for ``--repo`` / ``--pr`` as JSON.

    ``--max-rounds-before-final`` / ``--max-rounds-after-final`` override the
    configured PR incremental caps for this run only.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"reviewer config (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--max-rounds-before-final",
        type=_non_negative_int,
        default=None,
        help="override review_rounds.max_incremental_before_final for this run",
    )
    parser.add_argument(
        "--max-rounds-after-final",
        type=_non_negative_int,
        default=None,
        help="override review_rounds.max_incremental_after_final for this run",
    )
    args = parser.parse_args()
    try:
        adapter = load_reviewer(args.config)
    except NoActiveReviewer as exc:
        print(json.dumps({"reviewer": None, "configured": False, "detail": str(exc)}))
        return
    caps = load_round_caps(args.config)
    if args.max_rounds_before_final is not None:
        caps = caps._replace(before_final=args.max_rounds_before_final)
    if args.max_rounds_after_final is not None:
        caps = caps._replace(after_final=args.max_rounds_after_final)
    print(json.dumps(collect_state(args.repo, args.pr, adapter, caps), indent=2))


if __name__ == "__main__":
    main()
