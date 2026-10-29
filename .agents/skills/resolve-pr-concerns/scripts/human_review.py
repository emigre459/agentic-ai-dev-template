#!/usr/bin/env python3
"""A human reviewer's open concerns on a PR, and whether their request stands.

``reviewer_state`` reads only the configured bot's logins, so a human's review --
line threads, file-level threads, the review body, ``CHANGES_REQUESTED`` -- would
never be listed and the PR could read merge-ready while it sat unanswered. This
script lists it (resolve-pr-concerns Step 2h). It leaves the bot's findings to
``reviewer_state``.

A **human** is any author that isn't a GitHub ``Bot``, doesn't end in ``[bot]`` and
isn't one of the logins of any reviewer in ``.github/pr-reviewer.json``. With no
reviewer config at all (Step 1b), only GitHub Bot accounts are excluded.

The agent posts as whoever runs it, who can be the reviewer, so its answers are told
apart by a hidden marker, never by login: ``ANSWER_MARKER`` on a thread reply, and
``review_answer_marker(<review id>)`` on the top-level comment answering a review
body. A thread is answered when its newest non-bot comment carries the marker; a
human follow-up after that reopens it.

``changes_requested_by`` is computed from the human reviews themselves, not the PR's
``reviewDecision``, so a bot's request for changes never changes bot gating. Only the
reviewer's own new review (or a dismissal) clears it; re-requesting their review once
everything is answered (``rerequest_reviewers``) asks them for it.

Usage::

    python3 human_review.py --repo <owner>/<repo> --pr <n> [--config PATH]

Prints JSON with ``threads`` (open inline threads; reply on ``reply_to``),
``review_bodies`` (human review bodies not yet answered), ``open_concern_count``,
``changes_requested_by``, ``awaiting_rereview``, ``rerequest_reviewers`` (run
``gh pr edit <pr> --add-reviewer <login>``) and ``answer_marker``.

Exit codes: 0 -- state printed; 64 -- usage error, an explicit ``--config`` that does
not exist, or an invalid one (default path: the repo root's
``.github/pr-reviewer.json``); 1 -- a ``gh`` call failed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404 -- only to catch the gh failure ``reviewer_state`` raises
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

import reviewer_state

# scripts/ -> resolve-pr-concerns/ -> skills/ -> .agents/ -> repo root
DEFAULT_CONFIG_PATH = reviewer_state.DEFAULT_CONFIG_PATH

EXIT_USAGE = 64

ANSWER_MARKER = "<!-- resolve-pr-concerns:answer -->"
_ANSWER_PREFIX = "<!-- resolve-pr-concerns:answer"
_REVIEW_ANSWER_RE = re.compile(r"<!-- resolve-pr-concerns:answer review=(\d+) -->")

# Review states whose body asks something of the agent. An APPROVED body is a
# sign-off, a DISMISSED one is withdrawn.
_BODY_STATES = frozenset({"CHANGES_REQUESTED", "COMMENTED"})
# Review states that set or clear a reviewer's standing; COMMENTED doesn't (GitHub).
_DECIDING_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})

_THREADS_QUERY = """
query($owner:String!, $name:String!, $pr:Int!, $endCursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$pr) {
      reviewThreads(first:100, after:$endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          comments(first:1) { nodes { databaseId } }
        }
      }
    }
  }
}
"""


def review_answer_marker(review_id: int) -> str:
    """Return the hidden marker that records an answer to review ``review_id``'s body.

    Parameters
    ----------
    review_id : int
        The review's REST ``id``.

    Returns
    -------
    str
        The marker to put in the top-level answer comment.
    """
    return f"<!-- resolve-pr-concerns:answer review={review_id} -->"


def load_bot_logins(config_path: Path | None = None) -> frozenset[str]:
    """Return every configured reviewer's bot logins, not just the active one's.

    Parameters
    ----------
    config_path : Path | None
        An explicit reviewer config, which must exist; or ``None`` for the repo's
        default, whose absence means no reviewer is configured (Step 1b) and yields
        an empty set, so only GitHub Bot accounts are excluded as non-human.

    Returns
    -------
    frozenset[str]
        All ``bot_logins`` across the config's reviewers.

    Raises
    ------
    FileNotFoundError
        An explicit ``config_path`` does not exist.
    ValueError
        The config is not valid JSON, or not a JSON object.
    """
    path = config_path if config_path is not None else DEFAULT_CONFIG_PATH
    if not path.is_file():
        if config_path is not None:
            raise FileNotFoundError(f"PR-reviewer config not found: {path}")
        return frozenset()
    try:
        config = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"PR-reviewer config {path} is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):  # a wrong-shaped config is a bad config, not a bug
        raise ValueError(  # noqa: TRY004
            f"PR-reviewer config {path} is not valid JSON: not an object"
        )
    reviewers = config.get("reviewers")
    if not isinstance(reviewers, dict):
        return frozenset()
    return frozenset(
        login
        for reviewer in reviewers.values()
        if isinstance(reviewer, dict)
        for login in reviewer.get("bot_logins") or ()
    )


def is_human(user: Mapping[str, Any] | None, bot_logins: Iterable[str]) -> bool:
    """Return whether ``user`` is a person rather than a bot or a deleted account.

    Parameters
    ----------
    user : Mapping[str, Any] | None
        A REST ``user`` object.
    bot_logins : Iterable[str]
        Logins of every configured reviewer.

    Returns
    -------
    bool
        ``True`` for a person.
    """
    if not user:
        return False
    login = user.get("login") or ""
    return bool(
        login
        and user.get("type") != "Bot"
        and not login.endswith("[bot]")
        and login not in set(bot_logins)
    )


def is_answer(body: str | None) -> bool:
    """Return whether a comment is one of the agent's marked answers.

    Parameters
    ----------
    body : str | None
        Comment body.

    Returns
    -------
    bool
        ``True`` when it carries an answer marker.
    """
    return _ANSWER_PREFIX in (body or "")


def _root_of(comment_id: int, parent: Mapping[int, int | None]) -> int:
    """Follow ``in_reply_to_id`` up to the thread's first comment."""
    seen = {comment_id}
    current = comment_id
    while (up := parent.get(current)) is not None and up not in seen:
        seen.add(up)
        current = up
    return current


def thread_concerns(
    comments: Iterable[Mapping[str, Any]],
    resolved: Mapping[int, bool],
    bot_logins: Iterable[str],
) -> list[dict[str, Any]]:
    """Return the human-rooted inline threads still waiting on the agent.

    A bot-rooted thread is left to ``reviewer_state``. A human thread is open until
    it is resolved or its newest non-bot comment is a marked answer. A reply to a
    reply joins its thread's root, however deep the chain.

    Parameters
    ----------
    comments : Iterable[Mapping[str, Any]]
        Every REST PR review comment (all pages).
    resolved : Mapping[int, bool]
        ``isResolved`` keyed by each thread's first comment id; a thread missing from
        it counts as unresolved.
    bot_logins : Iterable[str]
        Logins of every configured reviewer.

    Returns
    -------
    list[dict[str, Any]]
        One dict per open thread, oldest root first, with ``root_id``, ``reply_to``
        (the id to reply on), ``author``, ``subject_type``, ``path``, ``line``,
        ``body`` (the newest human comment that asks something) and ``url``.
    """
    bots = frozenset(bot_logins)
    items = list(comments)
    parent = {c["id"]: c.get("in_reply_to_id") for c in items}
    threads: dict[int, list[Mapping[str, Any]]] = {}
    for comment in items:
        threads.setdefault(_root_of(comment["id"], parent), []).append(comment)
    found: list[dict[str, Any]] = []
    for root_id in sorted(threads):
        thread = sorted(
            threads[root_id], key=lambda c: (c.get("created_at") or "", c["id"])
        )
        root = thread[0]
        if root["id"] != root_id or resolved.get(root_id, False):
            continue
        if not is_human(root.get("user"), bots) or is_answer(root.get("body")):
            continue
        people = [c for c in thread if is_human(c.get("user"), bots)]
        if is_answer(people[-1].get("body")):
            continue
        asks = [c for c in people if not is_answer(c.get("body"))]
        found.append(
            {
                "root_id": root_id,
                "reply_to": root_id,
                "author": (asks[-1].get("user") or {}).get("login"),
                "subject_type": root.get("subject_type"),
                "path": root.get("path"),
                "line": root.get("line"),
                "body": asks[-1].get("body"),
                "url": asks[-1].get("html_url"),
            }
        )
    return found


def answered_review_ids(
    comments: Iterable[Mapping[str, Any]], bot_logins: Iterable[str]
) -> set[int]:
    """Return the review ids that a marked, non-bot top-level comment answers.

    Parameters
    ----------
    comments : Iterable[Mapping[str, Any]]
        The PR's issue comments.
    bot_logins : Iterable[str]
        Logins of every configured reviewer.

    Returns
    -------
    set[int]
        Review ids named by an answer marker.
    """
    bots = frozenset(bot_logins)
    return {
        int(match)
        for c in comments
        if is_human(c.get("user"), bots)
        for match in _REVIEW_ANSWER_RE.findall(c.get("body") or "")
    }


def review_body_concerns(
    reviews: Iterable[Mapping[str, Any]],
    comments: Iterable[Mapping[str, Any]],
    bot_logins: Iterable[str],
) -> list[dict[str, Any]]:
    """Return the human review bodies that no marked comment has answered yet.

    Parameters
    ----------
    reviews : Iterable[Mapping[str, Any]]
        The PR's REST reviews (all pages).
    comments : Iterable[Mapping[str, Any]]
        The PR's issue comments.
    bot_logins : Iterable[str]
        Logins of every configured reviewer.

    Returns
    -------
    list[dict[str, Any]]
        One dict per open body with ``review_id``, ``author``, ``state``,
        ``submitted_at``, ``body`` and ``url``. The agent splits a body into its
        items and answers them all in one comment.
    """
    bots = frozenset(bot_logins)
    answered = answered_review_ids(comments, bots)
    return [
        {
            "review_id": r["id"],
            "author": (r.get("user") or {}).get("login"),
            "state": r.get("state"),
            "submitted_at": r.get("submitted_at"),
            "body": r.get("body"),
            "url": r.get("html_url"),
        }
        for r in reviews
        if is_human(r.get("user"), bots)
        and r.get("state") in _BODY_STATES
        and (r.get("body") or "").strip()
        and not is_answer(r.get("body"))
        and r["id"] not in answered
    ]


def changes_requested_by(
    reviews: Iterable[Mapping[str, Any]], bot_logins: Iterable[str]
) -> list[str]:
    """Return the humans whose request for changes still stands, by first request.

    Each reviewer's newest ``APPROVED`` / ``CHANGES_REQUESTED`` / ``DISMISSED`` review
    decides; a ``COMMENTED`` review leaves a request standing, as on GitHub.

    Parameters
    ----------
    reviews : Iterable[Mapping[str, Any]]
        The PR's REST reviews (all pages).
    bot_logins : Iterable[str]
        Logins of every configured reviewer.

    Returns
    -------
    list[str]
        Logins with an outstanding ``CHANGES_REQUESTED``.
    """
    bots = frozenset(bot_logins)
    deciding = sorted(
        (
            r
            for r in reviews
            if is_human(r.get("user"), bots) and r.get("state") in _DECIDING_STATES
        ),
        key=lambda r: r.get("submitted_at") or "",
    )
    latest: dict[str, str] = {}
    for review in deciding:
        login = review["user"]["login"]
        latest.pop(login, None)  # re-insert so order follows the deciding review
        latest[login] = review["state"]
    return [login for login, state in latest.items() if state == "CHANGES_REQUESTED"]


def rerequest_reviewers(
    requesting: Sequence[str],
    open_concerns: Iterable[Mapping[str, Any]],
    requested_logins: Iterable[str],
) -> list[str]:
    """Return the reviewers to ask for a new review now.

    Parameters
    ----------
    requesting : Sequence[str]
        Logins with an outstanding request for changes.
    open_concerns : Iterable[Mapping[str, Any]]
        Open thread and body concerns.
    requested_logins : Iterable[str]
        Logins with a pending review request.

    Returns
    -------
    list[str]
        Requesting reviewers with nothing of theirs open and no pending request.
    """
    waiting = {c.get("author") for c in open_concerns}
    pending = set(requested_logins)
    return [
        login for login in requesting if login not in waiting and login not in pending
    ]


def _gh_json(args: Sequence[str]) -> Any:
    """Run ``gh`` and parse its JSON, flattening ``--paginate`` array pages."""
    return reviewer_state._gh_json(args)


def _resolved_by_root(repo: str, pr: int) -> dict[int, bool]:
    """Return ``isResolved`` for every review thread, keyed by its first comment id."""
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
    resolved: dict[int, bool] = {}
    for page in pages if isinstance(pages, list) else [pages]:
        threads = page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        for thread in threads:
            first = (thread.get("comments") or {}).get("nodes") or []
            if first and first[0].get("databaseId") is not None:
                resolved[int(first[0]["databaseId"])] = bool(thread.get("isResolved"))
    return resolved


def standing_requests(repo: str, pr: int, bot_logins: Iterable[str]) -> list[str]:
    """Fetch a PR's reviews and return the humans whose request for changes stands.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    pr : int
        Pull request number.
    bot_logins : Iterable[str]
        Logins of every configured reviewer.

    Returns
    -------
    list[str]
        Logins with an outstanding ``CHANGES_REQUESTED``.
    """
    reviews = _gh_json(["api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate"]) or []
    return changes_requested_by(reviews, bot_logins)


def _collect(repo: str, pr: int, bots: frozenset[str]) -> dict[str, Any]:
    """Fetch a PR's human review state, treating ``bots`` as non-human."""
    inline = _gh_json(["api", f"repos/{repo}/pulls/{pr}/comments", "--paginate"]) or []
    reviews = _gh_json(["api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate"]) or []
    comments = (
        _gh_json(["api", f"repos/{repo}/issues/{pr}/comments", "--paginate"]) or []
    )
    pull = _gh_json(["api", f"repos/{repo}/pulls/{pr}"]) or {}
    threads = thread_concerns(inline, _resolved_by_root(repo, pr), bots)
    bodies = review_body_concerns(reviews, comments, bots)
    requesting = changes_requested_by(reviews, bots)
    requested = [u.get("login") for u in pull.get("requested_reviewers") or []]
    return {
        "threads": threads,
        "review_bodies": bodies,
        "open_concern_count": len(threads) + len(bodies),
        "changes_requested_by": requesting,
        "awaiting_rereview": [login for login in requesting if login in requested],
        "rerequest_reviewers": rerequest_reviewers(
            requesting, [*threads, *bodies], requested
        ),
        "answer_marker": ANSWER_MARKER,
    }


def collect_state(
    repo: str, pr: int, config_path: Path | None = None
) -> dict[str, Any]:
    """Fetch a PR's human review state.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    pr : int
        Pull request number.
    config_path : Path | None
        Reviewer config naming the bot logins to exclude; ``None`` for the repo's
        default (see ``load_bot_logins``).

    Returns
    -------
    dict[str, Any]
        ``threads`` and ``review_bodies`` still waiting on the agent,
        ``open_concern_count``, ``changes_requested_by``, ``awaiting_rereview``,
        ``rerequest_reviewers`` and the ``answer_marker`` to put on thread replies.

    Raises
    ------
    FileNotFoundError
        An explicit ``config_path`` does not exist.
    ValueError
        The config is not valid JSON.
    subprocess.CalledProcessError
        A ``gh`` call failed.
    """
    return _collect(repo, pr, load_bot_logins(config_path))


class _Parser(argparse.ArgumentParser):
    """An argument parser whose usage errors exit 64, not argparse's 2."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _repo_arg(value: str) -> str:
    """Validate ``owner/name``."""
    owner, _, name = value.partition("/")
    if not owner or not name or "/" in name:
        raise argparse.ArgumentTypeError(f"expected <owner>/<repo>, got {value!r}")
    return value


def main() -> None:
    """Print the human review state of ``--pr`` as JSON."""
    parser = _Parser(
        description=(
            "Print a human reviewer's open concerns on a PR as JSON, and whether "
            "their request for changes still stands."
        )
    )
    parser.add_argument("--repo", required=True, type=_repo_arg, help="owner/name")
    parser.add_argument("--pr", required=True, type=int, help="pull request number")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"reviewer config (default: {DEFAULT_CONFIG_PATH}); "
        "must exist when given",
    )
    args = parser.parse_args()
    try:
        bots = load_bot_logins(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from exc
    try:
        state = _collect(args.repo, args.pr, bots)
    except subprocess.CalledProcessError as exc:
        print(f"gh failed: {(exc.stderr or '').strip() or exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
