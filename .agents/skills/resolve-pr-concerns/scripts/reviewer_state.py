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
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404 -- fixed gh argv, no shell
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

# scripts/ -> resolve-pr-concerns/ -> skills/ -> .agents/ -> repo root
DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[4] / ".github" / "pr-reviewer.json"
)

_REQUIRED_STRINGS = (
    "display_name",
    "check_name",
    "trigger_comment",
    "clean_pattern",
    "findings_pattern",
)
# Present-but-nullable: an absent key is a typo, not a deliberate null.
_NULLABLE_STRINGS = ("summary_marker", "reviewed_sha_pattern")
# The final full review: all set, or all null for a reviewer agents can't ask for one.
_FINAL_REVIEW_FIELDS = (
    "final_review_comment",
    "command_reply_marker",
    "final_review_done_pattern",
    "final_review_failed_pattern",
    "final_review_partial_pattern",
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
    reviewed_sha_pattern: re.Pattern[str] | None
    self_resolves_threads: bool
    summary_in_issue_comments: bool
    final_review_comment: str | None
    command_reply_marker: str | None
    final_review_done_pattern: re.Pattern[str] | None
    final_review_failed_pattern: re.Pattern[str] | None
    final_review_partial_pattern: re.Pattern[str] | None


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


def load_reviewer(
    config_path: Path | None = None, key: str | None = None
) -> ReviewerAdapter:
    """Load and validate one reviewer adapter (the ``active`` one unless ``key``).

    Raises
    ------
    FileNotFoundError
        The config file does not exist.
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

    findings = _compile(path, chosen, "findings_pattern", raw["findings_pattern"])
    _one_group(path, chosen, "findings_pattern", findings)
    reviewed_sha = None
    if raw["reviewed_sha_pattern"] is not None:
        reviewed_sha = _compile(
            path, chosen, "reviewed_sha_pattern", raw["reviewed_sha_pattern"]
        )
        _one_group(path, chosen, "reviewed_sha_pattern", reviewed_sha)

    return ReviewerAdapter(
        key=chosen,
        display_name=raw["display_name"],
        check_name=raw["check_name"],
        trigger_comment=raw["trigger_comment"],
        bot_logins=frozenset(logins),
        summary_marker=raw["summary_marker"],
        clean_pattern=_compile(path, chosen, "clean_pattern", raw["clean_pattern"]),
        findings_pattern=findings,
        reviewed_sha_pattern=reviewed_sha,
        self_resolves_threads=raw["self_resolves_threads"],
        summary_in_issue_comments=raw["summary_in_issue_comments"],
        final_review_comment=raw["final_review_comment"],
        command_reply_marker=raw["command_reply_marker"],
        final_review_done_pattern=final_done,
        final_review_failed_pattern=final_failed,
        final_review_partial_pattern=final_partial,
    )


_THREADS_QUERY = """
query($owner:String!, $name:String!, $pr:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$pr) {
      headRefOid
      mergeStateStatus
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


def _is_summary(body: str, adapter: ReviewerAdapter) -> bool:
    """Return whether this review is the summary, not a reply or inline container."""
    if adapter.summary_marker is not None:
        return adapter.summary_marker in body
    # No marker: only a review carrying a verdict counts, so a later reply-style
    # review can't shadow the real summary and flip the verdict to "none".
    return bool(
        adapter.clean_pattern.search(body) or adapter.findings_pattern.search(body)
    )


def _on_head(
    review: Mapping[str, Any], head_sha: str | None, adapter: ReviewerAdapter
) -> bool:
    if head_sha is None or review.get("commit_id") == head_sha:
        return True
    if adapter.reviewed_sha_pattern is None:
        return False
    match = adapter.reviewed_sha_pattern.search(review.get("body") or "")
    if match is None:
        return False
    return head_sha.lower().startswith(match.group(1).lower())


def _live(review: Mapping[str, Any]) -> bool:
    """Return whether a review still stands: a dismissed review is not a verdict."""
    return str(review.get("state") or "").upper() != "DISMISSED"


def review_verdict(
    reviews: Iterable[Mapping[str, Any]],
    head_sha: str | None,
    adapter: ReviewerAdapter,
) -> tuple[str, int]:
    """Return (verdict, finding_count) for the newest reviewer summary on head_sha.

    verdict is "pass" (clean), "findings" (N inline findings), or "none"
    (the reviewer has not reviewed this SHA — trigger it).
    """
    candidates = [
        r
        for r in reviews
        if (r.get("user") or {}).get("login") in adapter.bot_logins
        and _live(r)
        and _is_summary(r.get("body") or "", adapter)
        and _on_head(r, head_sha, adapter)
    ]
    if not candidates:
        return ("none", 0)
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
    if incremental or any(not partial.search(b) for b in bodies):
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


def _comment_as_review(comment: Mapping[str, Any]) -> dict[str, Any]:
    """Shape an issue-comment like a review so ``review_verdict`` can judge it.

    A summary comment is edited in place, so ``updated_at`` is its recency, and it
    carries no ``commit_id`` -- the adapter's ``reviewed_sha_pattern`` must say which
    head it covers.
    """
    return {
        "body": comment.get("body"),
        "user": comment.get("user"),
        "commit_id": None,
        "submitted_at": comment.get("updated_at") or comment.get("created_at"),
    }


def _gh_json(args: Sequence[str]) -> Any:
    out = subprocess.run(  # nosec B603 B607 -- fixed gh argv, no shell
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout
    return json.loads(out) if out.strip() else None


def collect_state(repo: str, pr: int, adapter: ReviewerAdapter) -> dict[str, Any]:
    """Fetch one PR's reviews, threads and comments and return the reviewer state."""
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
    threads = pull["reviewThreads"]["nodes"]
    raw_reviews = (
        _gh_json(["api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate"]) or []
    )
    comments: list[Any] = []
    if adapter.summary_in_issue_comments or adapter.final_review_comment is not None:
        comments = (
            _gh_json(["api", f"repos/{repo}/issues/{pr}/comments", "--paginate"]) or []
        )
    reviews = list(raw_reviews)
    if adapter.summary_in_issue_comments:
        # Some reviewers post a clean re-review as an empty review and keep the
        # verdict in an issue-comment they edit in place (see the adapter notes).
        reviews += [_comment_as_review(c) for c in comments]
    final = final_review_outcome(comments, raw_reviews, adapter)

    verdict, count = review_verdict(reviews, head_sha, adapter)
    findings = open_findings(threads, adapter)
    unresolved_total = sum(1 for t in threads if not t.get("isResolved"))
    return {
        "head_sha": head_sha,
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
        "open_findings": findings,
        "unresolved_thread_total": unresolved_total,
        "needs_trigger": verdict == "none",
        "final_review_comment": adapter.final_review_comment,
        "final_review_outcome": final,
        "needs_final_review": final not in ("finished", "not_required"),
    }


def main() -> None:
    """Print ``collect_state`` for ``--repo`` / ``--pr`` as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"reviewer config (default: {DEFAULT_CONFIG_PATH})",
    )
    args = parser.parse_args()
    adapter = load_reviewer(args.config)
    print(json.dumps(collect_state(args.repo, args.pr, adapter), indent=2))


if __name__ == "__main__":
    main()
