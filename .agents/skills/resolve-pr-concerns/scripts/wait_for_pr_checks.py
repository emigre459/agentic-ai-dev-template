#!/usr/bin/env python3
"""Wait until every check on a PR has settled, then report whether it can merge.

Usage::

    python3 wait_for_pr_checks.py <pr-number> [<owner>/<repo>] [--timeout-min N]
        [--bot-window-sec S] [--poll-sec S] [--expected-bot NAME] [--config PATH]

Behaviour:

- Preflight: a CONFLICTING / DIRTY branch runs NO pull_request CI, so polling for
  checks would only spin to timeout. The merge state is read first (GitHub computes
  it asynchronously, so an UNKNOWN read is retried up to 5 times,
  ``MERGESTATE_POLL_SEC`` seconds apart, default 2) and a conflict exits 3 with a
  pointer to resolving it. A BEHIND branch DOES run CI -- on a stale tree -- so it
  only warns. While no checks are registered, the merge state is re-read each poll.
- Polls ``gh pr checks`` every 30s (``--poll-sec``) until no check is pending, or
  ``--timeout-min`` (default 20) passes. A check is settled when its bucket is
  pass / fail / cancel / skipping.
- The PR reviewer is whichever one ``.github/pr-reviewer.json`` names (``--config``
  overrides the path; the default is the current git repo's). Reviewer bots don't
  run on every push, so a pending check from any configured reviewer (any
  adapter's ``check_name``) or from Copilot is soft: once only soft checks remain
  and ``--bot-window-sec`` (default 600) has passed, the wait stops and they are
  reported, not waited on.
- A bot may take a while to *register* its check after a push. ``--expected-bot
  NAME`` (repeatable; default: the active reviewer's ``check_name``) keeps the soft
  window open for a bot not yet in the check list.
- With a reviewer configured, the soft wait also asks the sibling
  ``reviewer_state`` for the reviewer's reply to the newest trigger comment. A
  terminal reply (finished / failed / no_changes / not_completed) to a trigger that
  covers the current head ends the soft wait early, and the report prints
  ``REVIEWER: <outcome> (verdict <v>, needs_trigger <b>)`` -- or, when the newest
  request was refused for the rate limit, ``REVIEWER: rate limited -- re-post the
  same <request> request after <retry_after>`` -- instead of guessing "no-review"
  from an absent check. If the state can't be read, it says so and the window
  rules alone apply.
- With ``<owner>/<repo>``, once checks settle it waits (up to ~3 min) for the
  reviewer's review of HEAD -- a review on the head commit, or a summary whose
  ``reviewed_sha_pattern`` names it -- and for the unresolved-thread count to hold
  still, then prints UNRESOLVED REVIEW FINDINGS. A failed thread query is reported
  as an ERROR, never as "none unresolved".
- An explicit ``--config`` that is missing, or any config that is invalid (an
  active reviewer with no ``check_name``, or with ``bot_logins`` not a non-empty
  list of logins), exits 64 before any ``gh`` call. An absent DEFAULT config, or
  ``"active": null``, means no reviewer: wait on CI only (every adapter's check
  stays soft -- a reviewer app may still be installed).

Exit codes:
    0  -- all required checks passed (or none were reported)
    1  -- one or more checks failed (or were cancelled)
    2  -- timed out before settling, or the final check list could not be read
    3  -- the merge state blocks CI (CONFLICTING/DIRTY): resolve it, then re-run
    64 -- usage error, a missing explicit --config, or an invalid config

Designed for the resolve-pr-concerns skill -- not a general-purpose tool. Needs only
``gh`` and the standard library.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess  # nosec B404 -- fixed gh/git argv, no shell
import sys
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import reviewer_state

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_TIMEOUT = 2
EXIT_MERGE_STATE = 3
EXIT_USAGE = 64

DEFAULT_TIMEOUT_MIN = 20
DEFAULT_BOT_WINDOW_SEC = 600  # don't wait longer than this for external bot reviews
DEFAULT_POLL_SEC = 30
# GitHub computes mergeability asynchronously -- the first read is often UNKNOWN
# (the read itself kicks the calculation off). Poll a few times before trusting it.
MERGESTATE_TRIES = 5
DEFAULT_MERGESTATE_POLL_SEC = 2.0
# The post-settle wait for the reviewer's review of HEAD: up to ~3 min.
SETTLE_TRIES = 12
SETTLE_POLL_SEC = 15
# The review gate workflow's job. It used to cancel superseded runs; it no longer
# does, but heads pushed before that change still carry cancelled runs of this job.
# Its verdict is the `review-gate` status, so a cancelled run of the job is noise.
REVIEW_GATE_JOB = "compute review gate status"
REVIEW_GATE_STATUS = "review-gate"

TERMINAL_OUTCOMES = frozenset({"finished", "failed", "no_changes", "not_completed"})
_COPILOT_RE = re.compile(r"[Cc]opilot")
_COUNT_RE = re.compile(r"[0-9]+")
_REPO_RE = re.compile(r"[^/\s]+/[^/\s]+")
_VALUE_FLAGS = (
    "--timeout-min",
    "--bot-window-sec",
    "--poll-sec",
    "--expected-bot",
    "--config",
)
_USAGE = (
    "usage: wait_for_pr_checks.py <pr-number> [<owner>/<repo>] [--timeout-min N] "
    "[--bot-window-sec S] [--poll-sec S] [--expected-bot NAME] [--config PATH]"
)
# Errors a gh read (or an odd payload) can raise; each read degrades on them.
_READ_ERRORS = (
    subprocess.CalledProcessError,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    IndexError,
    AttributeError,
)

_THREADS_QUERY = """
query($owner:String!, $name:String!, $pr:Int!, $endCursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$pr) {
      reviewThreads(first:100, after:$endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          comments(first:1) { nodes { author { login } path line body } }
        }
      }
    }
  }
}
"""


class _Stop(Exception):
    """Unwind to ``main`` with an exit code."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class Options(NamedTuple):
    """The parsed command line.

    Attributes
    ----------
    pr : int
        Pull request number.
    repo : str | None
        ``owner/name``, or ``None`` for the current repo (and no post-settle poll).
    timeout_min : int
        Minutes to wait for checks to settle.
    bot_window_sec : int
        Seconds to keep waiting on soft (reviewer bot) checks.
    poll_sec : int
        Seconds between check polls.
    expected_bots : tuple[str, ...]
        Check names to wait for even before they register (``--expected-bot``).
    config : str | None
        The explicit ``--config`` value, or ``None`` for the default.
    """

    pr: int
    repo: str | None
    timeout_min: int
    bot_window_sec: int
    poll_sec: int
    expected_bots: tuple[str, ...]
    config: str | None


class Reviewer(NamedTuple):
    """What the reviewer config says.

    Attributes
    ----------
    adapter : reviewer_state.ReviewerAdapter | None
        The active reviewer, or ``None`` when none is configured or active.
    config : Path
        The config path read (or the absent default).
    checks : frozenset[str]
        Every adapter's ``check_name``: all are soft, active or not.
    """

    adapter: reviewer_state.ReviewerAdapter | None
    config: Path
    checks: frozenset[str]


class ReviewerReport(NamedTuple):
    """The reviewer's reply to its newest trigger, as ``reviewer_state`` reads it.

    Attributes
    ----------
    outcome : str
        ``incremental_review_outcome``.
    verdict : str
        The verdict on the head.
    needs_trigger : str
        ``true`` / ``false``.
    refused : bool
        The newest request was refused for the rate limit.
    request : str
        The refused request's kind, or ``-``.
    retry_after : str
        When to re-post it, or ``-``.
    on_head : bool
        The trigger covered the current head.
    """

    outcome: str
    verdict: str
    needs_trigger: str
    refused: bool
    request: str
    retry_after: str
    on_head: bool


class GhResult(NamedTuple):
    """One ``gh`` run.

    Attributes
    ----------
    returncode : int
        Exit code (127 when ``gh`` could not be started).
    stdout : str
        Standard output.
    stderr : str
        Standard error.
    """

    returncode: int
    stdout: str
    stderr: str


# --- seams (tests replace these) ----------------------------------------------


def _now() -> float:
    """Return a monotonic clock reading in seconds."""
    return time.monotonic()


def _sleep(seconds: float) -> None:
    """Pause between polls."""
    time.sleep(seconds)


def _gh(args: Sequence[str]) -> GhResult:
    """Run ``gh`` and capture its output; a missing ``gh`` reads as exit 127."""
    try:
        done = subprocess.run(  # nosec B603 B607 -- fixed gh argv, no shell
            ["gh", *args], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        return GhResult(127, "", str(exc))
    return GhResult(done.returncode, done.stdout, done.stderr)


def _gh_json(args: Sequence[str]) -> Any:
    """Run ``gh`` and parse its JSON, flattening ``--paginate`` array pages."""
    return reviewer_state._gh_json(args)


def _read_reviewer_state(
    repo: str, pr: int, adapter: reviewer_state.ReviewerAdapter, config: Path
) -> Mapping[str, Any]:
    """Return ``reviewer_state.collect_state`` for the PR, in-process."""
    caps = reviewer_state.load_round_caps(config)
    return reviewer_state.collect_state(repo, pr, adapter, caps)


# --- helpers ------------------------------------------------------------------


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _jq_text(value: Any) -> str:
    """Render a JSON value the way jq string interpolation does."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _gh_doc(args: Sequence[str]) -> Any:
    """Return ``gh``'s JSON output, or ``None`` if it failed or isn't JSON."""
    result = _gh(args)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def _pr_args(opts: Options, command: str) -> list[str]:
    repo = ["--repo", opts.repo] if opts.repo else []
    return ["pr", command, str(opts.pr), *repo]


def _count(flag: str, value: str) -> int:
    if not _COUNT_RE.fullmatch(value):
        _err(f"{flag} must be a non-negative whole number, got {value!r}")
        raise _Stop(EXIT_USAGE)
    return int(value)


# --- command line and config --------------------------------------------------


def parse_args(argv: Sequence[str]) -> Options:
    """Parse the command line (the bash script's contract, kept verbatim).

    Parameters
    ----------
    argv : Sequence[str]
        Arguments after the program name.

    Returns
    -------
    Options
        The parsed options.

    Raises
    ------
    _Stop
        Exit 0 after ``--help``; exit 64 on a usage error.
    """
    positional: list[str] = []
    values: dict[str, str] = {}
    bots: list[str] = []
    config: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in _VALUE_FLAGS:
            if i + 1 >= len(argv):
                _err(f"{arg} needs a value")
                raise _Stop(EXIT_USAGE)
            value = argv[i + 1]
            if arg == "--expected-bot":
                bots.append(value)
            elif arg == "--config":
                config = value
            else:
                values[arg] = value
            i += 2
            continue
        if arg in ("-h", "--help"):
            print(__doc__)
            raise _Stop(EXIT_OK)
        if arg.startswith("-") or len(positional) == 2:
            _err(f"unexpected arg: {arg}")
            raise _Stop(EXIT_USAGE)
        positional.append(arg)
        i += 1
    if not positional:
        _err(_USAGE)
        raise _Stop(EXIT_USAGE)
    pr = positional[0]
    if not _COUNT_RE.fullmatch(pr) or int(pr) == 0:
        _err(f"<pr-number> must be a pull request number, got {pr!r}")
        raise _Stop(EXIT_USAGE)
    repo = positional[1] if len(positional) == 2 else None
    if repo is not None and not _REPO_RE.fullmatch(repo):
        _err(f"expected <owner>/<repo>, got {repo!r}")
        raise _Stop(EXIT_USAGE)
    return Options(
        pr=int(pr),
        repo=repo,
        timeout_min=_count(
            "--timeout-min", values.get("--timeout-min", str(DEFAULT_TIMEOUT_MIN))
        ),
        bot_window_sec=_count(
            "--bot-window-sec",
            values.get("--bot-window-sec", str(DEFAULT_BOT_WINDOW_SEC)),
        ),
        poll_sec=_count("--poll-sec", values.get("--poll-sec", str(DEFAULT_POLL_SEC))),
        expected_bots=tuple(bots),
        config=config,
    )


def mergestate_poll_sec(environ: Mapping[str, str]) -> float:
    """Return ``MERGESTATE_POLL_SEC`` (default 2; empty means the default).

    Raises
    ------
    _Stop
        Exit 64 when it isn't a finite, non-negative number.
    """
    raw = environ.get("MERGESTATE_POLL_SEC", "")
    if raw == "":
        return DEFAULT_MERGESTATE_POLL_SEC
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        _err(f"MERGESTATE_POLL_SEC must be a non-negative number, got {raw!r}")
        raise _Stop(EXIT_USAGE)
    return value


def _default_config() -> Path:
    """Return ``<git toplevel, or the cwd>/.github/pr-reviewer.json``."""
    try:
        done = subprocess.run(  # nosec B603 B607 -- fixed git argv, no shell
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        root = done.stdout.strip() if done.returncode == 0 else ""
    except OSError:
        root = ""
    return Path(root or os.getcwd()) / ".github" / "pr-reviewer.json"


def _check_names(config: Any) -> frozenset[str]:
    reviewers = config.get("reviewers") if isinstance(config, dict) else None
    if not isinstance(reviewers, dict):
        return frozenset()
    return frozenset(
        r["check_name"]
        for r in reviewers.values()
        if isinstance(r, dict) and isinstance(r.get("check_name"), str)
        if r["check_name"]
    )


def load_reviewer(explicit: str | None) -> Reviewer:
    """Read the reviewer config: the active adapter and every adapter's check.

    Parameters
    ----------
    explicit : str | None
        ``--config``'s value (which must exist), or ``None`` for the default,
        whose absence means no reviewer is configured.

    Returns
    -------
    Reviewer
        The active adapter (``None`` for no reviewer) and the soft check names.

    Raises
    ------
    _Stop
        Exit 64 for a missing explicit config or an invalid one.
    """
    path = _default_config() if explicit is None else Path(explicit)
    if explicit is None and not path.exists():
        _err(
            f"NOTE: no PR reviewer configured ({path} absent) -- waiting on CI only "
            "(plus Copilot, and any --expected-bot)."
        )
        return Reviewer(None, path, frozenset())
    # `--config ""` is an explicit (broken) value, not a request for the default.
    if explicit == "" or not path.is_file():
        _err(f"PR-reviewer config not found: {path if explicit is None else explicit}")
        _err(
            "Create .github/pr-reviewer.json naming the active reviewer (see "
            "resolve-pr-concerns references/reviewers/), or pass --config PATH."
        )
        raise _Stop(EXIT_USAGE)
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _err(f"PR-reviewer config {path} is not valid JSON: {exc}")
        raise _Stop(EXIT_USAGE) from exc
    if isinstance(raw, dict) and "active" in raw and raw["active"] is None:
        # The repo deliberately has no reviewer. Wait on CI only, but keep every
        # adapter's check soft: a reviewer app may still be installed.
        _err(
            f'NOTE: no PR reviewer active ("active" is null in {path}) -- waiting '
            "on CI only (plus Copilot, and any --expected-bot)."
        )
        return Reviewer(None, path, _check_names(raw))
    try:
        adapter = reviewer_state.load_reviewer(path)
    except (ValueError, OSError, reviewer_state.NoActiveReviewer) as exc:
        # Includes an active adapter with no check_name, or bot_logins that could
        # never match a review (empty, or not a list of non-empty logins).
        _err(f"PR-reviewer config is invalid for its active reviewer: {exc}")
        raise _Stop(EXIT_USAGE) from exc
    return Reviewer(adapter, path, _check_names(raw))


# --- merge state --------------------------------------------------------------


def _merge_state(opts: Options) -> str:
    """Return ``<mergeable>/<mergeStateStatus>``; anything unread is UNKNOWN."""
    doc = _gh_doc([*_pr_args(opts, "view"), "--json", "mergeable,mergeStateStatus"])
    if not isinstance(doc, dict):
        return "UNKNOWN/UNKNOWN"
    mergeable = doc.get("mergeable") or "UNKNOWN"
    status = doc.get("mergeStateStatus") or "UNKNOWN"
    return f"{mergeable}/{status}"


def _exit_if_conflicting(state: str) -> None:
    """Exit 3 (the merge state blocks CI) when ``state`` is a conflict."""
    mergeable, _, status = state.partition("/")
    if mergeable == "CONFLICTING" or status == "DIRTY":
        _err(
            f"MERGE STATE {state}: this PR conflicts with its base, so GitHub runs "
            "NO pull_request CI."
        )
        _err(
            "Resolve the merge state first (resolve-pr-concerns Step 2f): fetch + "
            "'git merge origin/<base>', resolve conflicts, push — the push is what "
            "triggers CI. Then re-run this script."
        )
        raise _Stop(EXIT_MERGE_STATE)


def preflight_merge_state(opts: Options, poll_sec: float) -> str:
    """Read the merge state before polling checks; exit 3 on a conflict.

    Mergeability is computed asynchronously, so an UNKNOWN read is retried (up to
    ``MERGESTATE_TRIES`` reads) before it is trusted. A state that never resolves is
    re-checked in the poll loop whenever no checks are registered.

    Parameters
    ----------
    opts : Options
        The parsed command line.
    poll_sec : float
        Seconds between reads.

    Returns
    -------
    str
        The last ``<mergeable>/<mergeStateStatus>`` read.
    """
    state = "UNKNOWN/UNKNOWN"
    for attempt in range(MERGESTATE_TRIES):
        state = _merge_state(opts)
        if not state.startswith("UNKNOWN/"):
            break
        if attempt < MERGESTATE_TRIES - 1:
            _sleep(poll_sec)
    _exit_if_conflicting(state)
    if state.endswith("/BEHIND"):
        _err(
            f"NOTE: branch is BEHIND its base ({state}); CI runs but on a stale "
            "tree — update it before calling merge-ready (Step 2f)."
        )
    elif state.startswith("UNKNOWN/"):
        _err(
            f"NOTE: GitHub did not resolve mergeability within {MERGESTATE_TRIES} "
            "tries; will re-check if checks fail to register."
        )
    return state


# --- checks -------------------------------------------------------------------


# gh's stderr when the PR has no checks at all: an answer, not a failed read.
_NO_CHECKS_RE = re.compile(r"no checks reported", re.IGNORECASE)


def _checks(opts: Options, fields: str) -> list[Mapping[str, Any]] | None:
    """Return the PR's checks with ``fields``, or ``None`` if they can't be read.

    Parameters
    ----------
    opts : Options
        The parsed command line.
    fields : str
        The comma-separated ``--json`` fields to ask for.

    Returns
    -------
    list[Mapping[str, Any]] | None
        The checks -- empty when ``gh`` says none are reported -- or ``None``
        when the read failed or its output isn't a JSON list.
    """
    result = _gh([*_pr_args(opts, "checks"), "--json", fields])
    if result.returncode != 0:
        return [] if _NO_CHECKS_RE.search(result.stderr) else None
    try:
        doc = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(doc, list):
        return None
    return [c for c in doc if isinstance(c, Mapping)]


def _is_external_bot(name: str, reviewer: Reviewer) -> bool:
    """Return whether ``name`` is a reviewer bot's check (soft pending)."""
    return name in reviewer.checks or bool(_COPILOT_RE.search(name))


def _print_table(opts: Options, stream: Any) -> None:
    """Print ``gh pr checks``' table (its exit code says nothing more)."""
    table = _gh(_pr_args(opts, "checks"))
    stream.write(table.stdout)
    sys.stderr.write(table.stderr)


# --- the reviewer's state -----------------------------------------------------


def _current_repo() -> str | None:
    doc = _gh_doc(["repo", "view", "--json", "nameWithOwner"])
    name = doc.get("nameWithOwner") if isinstance(doc, dict) else None
    return name if isinstance(name, str) and name else None


def _report_from(state: Any) -> ReviewerReport | None:
    if not isinstance(state, Mapping):
        return None
    rate = state.get("rate_limit")
    rate = rate if isinstance(rate, Mapping) else {}
    return ReviewerReport(
        outcome=_jq_text(state.get("incremental_review_outcome")),
        verdict=_jq_text(state.get("verdict")),
        needs_trigger=_jq_text(state.get("needs_trigger")),
        refused=rate.get("refused") is True,
        request=_jq_text(rate.get("request") or "-"),
        retry_after=_jq_text(rate.get("retry_after") or "-"),
        on_head=state.get("incremental_review_on_head") is True,
    )


def reviewer_report(opts: Options, reviewer: Reviewer) -> ReviewerReport | None:
    """Read the reviewer's reply to its newest trigger; ``None`` if unknown.

    Parameters
    ----------
    opts : Options
        The parsed command line (the repo defaults to the current one).
    reviewer : Reviewer
        The reviewer config; with no active adapter there is nothing to read.

    Returns
    -------
    ReviewerReport | None
        The reply, or ``None`` when no reviewer is active or its state can't be
        read (any failure degrades to "unknown", never a crash).
    """
    if reviewer.adapter is None:
        return None
    repo = opts.repo or _current_repo()
    if repo is None:
        return None
    try:
        state = _read_reviewer_state(repo, opts.pr, reviewer.adapter, reviewer.config)
    except Exception:  # noqa: BLE001 -- any failure means "state unknown"; said so
        return None
    return _report_from(state)


def _print_reviewer_line(report: ReviewerReport) -> None:
    if report.refused:
        # A refusal is a wait for the SAME request, never a different one.
        _err(
            f"REVIEWER: rate limited -- re-post the same {report.request} request "
            f"after {report.retry_after} -- from reviewer_state.py."
        )
    else:
        _err(
            f"REVIEWER: {report.outcome} (verdict {report.verdict}, needs_trigger "
            f"{report.needs_trigger}) -- from reviewer_state.py."
        )


# --- post-settle: the reviewer's review of HEAD and unresolved findings --------


def _review_threads(repo: str, pr: int) -> list[Mapping[str, Any]]:
    """Return every review thread of the PR, across all pages.

    Raises
    ------
    subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError
        The query failed or returned an unexpected shape.
    """
    owner, name = repo.split("/", 1)
    pages = _gh_json(
        [
            "api",
            "graphql",
            "--paginate",
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
    threads: list[Mapping[str, Any]] = []
    for page in pages if isinstance(pages, list) else [pages]:
        threads += page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    return threads


def _unresolved(threads: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    # The GraphQL isResolved flag is the source of truth -- NOT a comment's
    # commit_id: GitHub re-points resolved comments' commit_id to HEAD too.
    return [t for t in threads if t.get("isResolved") is False]


def _unresolved_count(repo: str, pr: int) -> int:
    try:
        return len(_unresolved(_review_threads(repo, pr)))
    except _READ_ERRORS:
        return 0


def _listed(path: str) -> list[Mapping[str, Any]]:
    """Return a paginated REST list; a failed read is an empty list."""
    try:
        items = _gh_json(["api", path, "--paginate"]) or []
    except _READ_ERRORS:
        return []
    return (
        [i for i in items if isinstance(i, Mapping)] if isinstance(items, list) else []
    )


def vouches_for_head(
    item: Mapping[str, Any], head: str, adapter: reviewer_state.ReviewerAdapter
) -> bool:
    """Return whether a reviewer review / comment says it reviewed ``head``.

    It must be by one of the adapter's logins, and either sit on the head commit
    (``commit_id``) or name it in its body through any ``reviewed_sha_pattern``
    match (a capture of at least 7 characters that prefixes ``head``).

    Parameters
    ----------
    item : Mapping[str, Any]
        A REST review or issue comment.
    head : str
        The PR's head SHA; empty when unknown (nothing vouches for it).
    adapter : reviewer_state.ReviewerAdapter
        The active reviewer.

    Returns
    -------
    bool
        Whether ``item`` vouches for ``head``.
    """
    user = item.get("user")
    login = user.get("login") if isinstance(user, Mapping) else None
    if not head or login not in adapter.bot_logins:
        return False
    if (item.get("commit_id") or "") == head:
        return True
    body = item.get("body") or ""
    for pattern in adapter.reviewed_sha_patterns:
        for match in pattern.finditer(body):
            sha = match.group(1)
            if sha is not None and len(sha) >= 7 and head.startswith(sha):
                return True
    return False


def _reviewed_head_count(
    opts: Options, repo: str, head: str, adapter: reviewer_state.ReviewerAdapter
) -> int:
    # The REST reviews endpoint, not `gh pr view --json reviews`, whose commit id
    # can be null for some reviewers.
    items = _listed(f"repos/{repo}/pulls/{opts.pr}/reviews")
    if adapter.summary_in_issue_comments:
        # Some reviewers keep the verdict in an issue-comment they edit in place.
        items += _listed(f"repos/{repo}/issues/{opts.pr}/comments")
    return sum(1 for item in items if vouches_for_head(item, head, adapter))


def _finding_line(thread: Mapping[str, Any]) -> str:
    nodes = (thread.get("comments") or {}).get("nodes") or []
    first = nodes[0] if nodes and isinstance(nodes[0], Mapping) else {}
    author = first.get("author")
    login = author.get("login") if isinstance(author, Mapping) else None
    title = re.sub(r"^### ", "", (first.get("body") or "").split("\n")[0])
    where = f"{_jq_text(first.get('path'))}:{_jq_text(first.get('line'))}"
    return f"  - {_jq_text(login)} {where} — {title}"


def post_settle(opts: Options, repo: str, reviewer: Reviewer) -> None:
    """Wait for the reviewer's review of HEAD, then list unresolved findings.

    Reviewer bots report findings as review comments, not a failing check, and the
    review can land seconds after the check settles. So give the reviewer a short
    window to review HEAD and let the unresolved count hold still across one
    interval (late inline findings), then print every unresolved thread. A failed
    thread query is an ERROR -- never read as "none unresolved".

    Parameters
    ----------
    opts : Options
        The parsed command line.
    repo : str
        ``owner/name``.
    reviewer : Reviewer
        The reviewer config; with no active adapter there is no review to wait for.
    """
    adapter = reviewer.adapter
    head_doc = _gh_doc([*_pr_args(opts, "view"), "--json", "headRefOid"])
    head = head_doc.get("headRefOid") if isinstance(head_doc, dict) else None
    head = head if isinstance(head, str) else ""
    previous = -1
    settled = False
    for _ in range(SETTLE_TRIES):
        # No reviewer configured -> nothing to wait for; just let the count settle.
        reviewed = (
            1 if adapter is None else _reviewed_head_count(opts, repo, head, adapter)
        )
        count = _unresolved_count(repo, opts.pr)
        if reviewed > 0 and count == previous:
            settled = True
            break
        previous = count
        _sleep(SETTLE_POLL_SEC)
    if not settled:
        name = adapter.display_name if adapter is not None else "the reviewer"
        _err(
            f"  (note: {name}'s review may still be settling — re-check if it just "
            "landed)"
        )
    print("UNRESOLVED REVIEW FINDINGS:")
    try:
        findings = [
            _finding_line(t) for t in _unresolved(_review_threads(repo, opts.pr))
        ]
    except _READ_ERRORS:
        _err(
            "  ERROR: could not query review threads (gh/GraphQL failed) — do NOT "
            "assume clear; verify manually before merging."
        )
        return
    if findings:
        print("\n".join(findings))
        _err(
            "  (unresolved review threads remain — answer each one and leave "
            "resolving to a human; do NOT merge yet.)"
        )
    else:
        print("  none unresolved.")


# --- the wait -----------------------------------------------------------------


def wait_for_checks(
    opts: Options, reviewer: Reviewer, expected: Sequence[str]
) -> ReviewerReport | None:
    """Poll until no hard check is pending and the soft wait is over.

    Parameters
    ----------
    opts : Options
        The parsed command line.
    reviewer : Reviewer
        The reviewer config (soft checks, and whose state to ask).
    expected : Sequence[str]
        Bots to wait for even before they register a check.

    Returns
    -------
    ReviewerReport | None
        The reviewer's state as last read during the soft wait, if it was.

    Raises
    ------
    _Stop
        Exit 2 on timeout; exit 3 when no checks registered and the PR conflicts.
    """
    start = _now()
    deadline = start + opts.timeout_min * 60
    window = opts.bot_window_sec
    report: ReviewerReport | None = None
    _err(
        f"Waiting for PR #{opts.pr} checks to settle (timeout: {opts.timeout_min}m, "
        f"poll: {opts.poll_sec}s)..."
    )
    while True:
        checks = _checks(opts, "name,bucket,state,link")
        if checks is None:
            _err("  (could not read the PR's checks — retrying)")
        elif not checks:
            # A conflicting PR registers no checks -- and a merge state the
            # preflight saw as UNKNOWN may have resolved to CONFLICTING by now.
            _exit_if_conflicting(_merge_state(opts))
            _err("  (no checks reported yet — waiting)")
        else:
            buckets = Counter(_jq_text(c.get("bucket")) for c in checks)
            summary = " ".join(f"{b}={n}" for b, n in sorted(buckets.items()))
            names = {c.get("name") for c in checks}
            pending = [
                str(c["name"])
                for c in checks
                if c.get("bucket") == "pending" and c.get("name")
            ]
            soft = [n for n in pending if _is_external_bot(n, reviewer)]
            hard = [n for n in pending if not _is_external_bot(n, reviewer)]
            # Expected bots not in the list yet may still be booting after the push.
            missing = [
                f"{bot} (not yet registered)" for bot in expected if bot not in names
            ]
            elapsed = int(_now() - start)
            if hard:
                _err(f"  {summary} (still pending: {','.join(hard)})")
            else:
                blockers = soft + missing
                if not blockers or elapsed >= window:
                    break
                # A reviewer that already answered its trigger isn't worth waiting
                # on -- unless that trigger predates the current head.
                report = reviewer_report(opts, reviewer)
                if report and report.outcome in TERMINAL_OUTCOMES and report.on_head:
                    _err(
                        f"  reviewer answered its trigger ({report.outcome}); not "
                        "waiting on its check."
                    )
                    break
                _err(
                    f"  {summary} (waiting on external bot reviews — "
                    f"{elapsed}s/{window}s window: {', '.join(blockers)})"
                )
        if _now() >= deadline:
            _err(f"TIMEOUT after {opts.timeout_min}m. Latest:")
            _print_table(opts, sys.stderr)
            raise _Stop(EXIT_TIMEOUT)
        _sleep(opts.poll_sec)
    return report


def verdict(opts: Options) -> int:
    """Return the exit code for the settled checks (0 mergeable, 1 not, 2 unread)."""
    checks = _checks(opts, "name,bucket")
    if checks is None:
        _err(
            "ERROR: could not read the PR's checks (gh pr checks failed), so "
            "whether they passed is unknown. Re-run once gh can read them."
        )
        return EXIT_TIMEOUT
    if not checks:
        _err("No checks reported on the PR -- nothing passed or failed.")
        return EXIT_OK
    fails = sum(
        1
        for c in checks
        if c.get("bucket") == "fail"
        or (c.get("bucket") == "cancel" and c.get("name") != REVIEW_GATE_JOB)
    )
    if fails:
        _err(f"{fails} check(s) failed or were cancelled. PR is NOT mergeable.")
        return EXIT_FAILED
    # A cancelled review-gate job run is noise only when a run that did finish
    # posted a passing review-gate status.
    gate_cancelled = any(
        c.get("name") == REVIEW_GATE_JOB and c.get("bucket") == "cancel" for c in checks
    )
    gate_passed = any(
        c.get("name") == REVIEW_GATE_STATUS and c.get("bucket") == "pass"
        for c in checks
    )
    if gate_cancelled and not gate_passed:
        _err(
            "The review-gate run was cancelled and no passing review-gate status "
            "exists. PR is NOT mergeable."
        )
        return EXIT_FAILED
    _err("All required checks passed.")
    return EXIT_OK


def run(argv: Sequence[str]) -> int:
    """Run the whole wait and return its exit code.

    Raises
    ------
    _Stop
        For every early exit (help, usage, config, timeout, merge state).
    """
    opts = parse_args(argv)
    merge_poll = mergestate_poll_sec(os.environ)
    reviewer = load_reviewer(opts.config)
    adapter = reviewer.adapter
    # Default expected bots: the active reviewer's check. --expected-bot replaces it.
    expected = opts.expected_bots or ((adapter.check_name,) if adapter else ())

    preflight_merge_state(opts, merge_poll)
    report = wait_for_checks(opts, reviewer, expected)

    # Final report: the full table on stdout so the caller can grep it.
    print("FINAL CHECK RESULTS:")
    _print_table(opts, sys.stdout)
    final_names = {c.get("name") for c in _checks(opts, "name") or []}
    if report is None:
        report = reviewer_report(opts, reviewer)
    if report is not None:
        _print_reviewer_line(report)
    elif adapter is not None:
        _err("NOTE: could not read reviewer_state.py; the reviewer's state is unknown.")
    for bot in expected:
        # reviewer_state's answer beats guessing from an absent check.
        if report is not None and adapter is not None and bot == adapter.check_name:
            continue
        if bot not in final_names:
            _err(
                f"NOTE: expected bot '{bot}' did not register a check within "
                f"{opts.bot_window_sec}s — treating as no-review."
            )
    if opts.repo:
        post_settle(opts, opts.repo, reviewer)
    return verdict(opts)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: run the wait and return its exit code.

    Parameters
    ----------
    argv : Sequence[str] | None
        Arguments after the program name; ``None`` reads ``sys.argv``.

    Returns
    -------
    int
        The exit code (see the module docstring).
    """
    try:
        return run(list(sys.argv[1:] if argv is None else argv))
    except _Stop as stop:
        return stop.code


if __name__ == "__main__":
    sys.exit(main())
