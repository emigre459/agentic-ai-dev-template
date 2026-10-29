#!/usr/bin/env python3
"""Post the vendor-neutral ``review-gate`` commit status for a PR.

The PR reviewer's own status check passes on heads it never reviewed (skipped or
rate-limited pushes), so a ruleset should require this status instead. It is green
only when the configured reviewer (``.github/pr-reviewer.json``) has a verdict on the
current head AND its final full review finished -- the state ``reviewer_state``
reports. A verdict carried onto a rebase-only head (``verdict_carried_from``) counts
like one on the head itself. A reviewer with a local review runs its fix rounds off
the PR, so its gate is the final full review finished with every reviewer thread
resolved, whatever the head.

With a CODEOWNERS file (see ``path_owners``), a PR touching a path its author
doesn't own also needs that path's owner to approve the head; that half is reported
first. CODEOWNERS alone turns the status on: with no reviewer configured
(``"active": null``) the status is the ownership half only. A CODEOWNERS file that
names no user owner (empty, or only team, email or unsupported-syntax lines, which
``path_owners`` skips) counts as no file. With neither, nothing is posted. Run by
``.github/workflows/review-gate.yml`` from the default branch's checkout, never from
PR code, so a PR can't loosen the CODEOWNERS that gates it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 -- fixed gh argv, no shell
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import path_owners
import reviewer_state

STATUS_CONTEXT = "review-gate"
# scripts/ -> resolve-pr-concerns/ -> skills/ -> .agents/ -> repo root. The workflow
# checks out the default branch, so the CODEOWNERS read here is the default branch's.
REPO_ROOT = Path(__file__).resolve().parents[4]
OWNERSHIP_CLEAR = "no changed path needs another owner's approval"
_MAX_DESCRIPTION = 140  # GitHub rejects longer status descriptions
# Posts per run before giving up on a state that keeps changing (the sweep retries).
_MAX_POSTS = 5


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_DESCRIPTION else text[: _MAX_DESCRIPTION - 1] + "…"


def gate_status(state: Mapping[str, Any]) -> tuple[str, str]:
    """Return (status state, description) for one PR's ``collect_state`` output.

    Parameters
    ----------
    state : Mapping[str, Any]
        Reviewer state as returned by ``reviewer_state.collect_state``.

    Returns
    -------
    tuple[str, str]
        The commit-status state (``success``, ``pending`` or ``failure``) and its
        clipped description.
    """
    if (state.get("local_review") or {}).get("configured"):
        return _local_gate_status(state)
    name, sha7 = state["reviewer_name"], state["head_sha"][:7]
    trigger = state["trigger_comment"]
    if state["verdict"] not in ("pass", "findings"):
        # Pending, not failure: the reviewer's reply edit re-runs this workflow.
        if state.get("review_running"):
            return "pending", _clip(f"{name} is reviewing {sha7}")
        if state.get("incremental_review_on_head") and state.get(
            "incremental_review_outcome"
        ) in ("failed", "not_completed"):
            return "failure", _clip(
                f"{name}'s last review request did not complete: "
                f"post `{trigger}` for {sha7}"
            )
        return "failure", _clip(f"{name} has not reviewed {sha7}: post `{trigger}`")
    final = state["final_review_outcome"]
    command = state.get("final_review_comment")
    if final in ("finished", "not_required"):
        source = state.get("verdict_carried_from")
        if source:
            why = (
                "same diff"
                if state.get("verdict_carry_reason") == "same_patch"
                else "nothing new to review"
            )
            return "success", _clip(
                f"{name}'s verdict on {source[:7]} carries to {sha7} ({why})"
            )
        return "success", _clip(f"{name} reviewed {sha7}")
    if final == "not_requested":
        return "failure", _clip(
            f"{name} reviewed {sha7}; final full review not run: post `{command}`"
        )
    if final == "pending":
        return "pending", _clip(f"{name}'s final full review is still running")
    if final == "failed":
        return "failure", _clip(
            f"{name}'s final full review failed: wait 20+ min, "
            f"post `{command}` once more"
        )
    return "failure", _clip(f"unrecognized final-review outcome: {final}")


def _local_gate_status(state: Mapping[str, Any]) -> tuple[str, str]:
    """Gate a PR whose fix rounds ran locally on its one final full review.

    Fixes pushed after the full review move the head, and are re-checked locally
    and on the reviewer's threads rather than by another PR review (which would
    spend the scarce PR quota). So the gate needs the full review finished and every
    reviewer thread resolved, not a verdict on the current head.

    Parameters
    ----------
    state : Mapping[str, Any]
        Reviewer state as returned by ``reviewer_state.collect_state``.

    Returns
    -------
    tuple[str, str]
        The commit-status state and its clipped description.
    """
    name, sha7 = state["reviewer_name"], state["head_sha"][:7]
    command = state.get("final_review_comment")
    final = state["final_review_outcome"]
    if final == "not_requested":
        return "failure", _clip(f"{name}'s full review not run: post `{command}`")
    if final == "pending":
        return "pending", _clip(f"{name}'s full review is still running")
    if final == "failed":
        return "failure", _clip(
            f"{name}'s full review failed: wait 20+ min, post `{command}` once more"
        )
    if final not in ("finished", "not_required"):
        return "failure", _clip(f"unrecognized final-review outcome: {final}")
    open_threads = len(state.get("open_findings") or [])
    if open_threads:
        return "failure", _clip(f"{open_threads} {name} thread(s) still open")
    local = (
        "local review"
        if state["local_review"].get("reviewed_head")
        else "no local review"
    )
    return "success", _clip(
        f"{name}'s full review finished, threads resolved; {local} on {sha7}"
    )


def _gh(args: Sequence[str]) -> None:
    subprocess.run(  # nosec B603 B607 -- fixed gh argv, no shell
        ["gh", *args], check=True, capture_output=True, text=True
    )


def _gh_output(args: Sequence[str]) -> str:
    return subprocess.run(  # nosec B603 B607 -- fixed gh argv, no shell
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


class OwnershipInputs(NamedTuple):
    """What the ownership half of the gate reads from a PR.

    Attributes
    ----------
    author : str
        The PR author's login.
    changed_files : int
        GitHub's count of changed files (may exceed ``files`` when truncated).
    files : list[str]
        The changed files as listed.
    renamed_from : list[str]
        Old paths of renamed files.
    reviews : list[Mapping[str, Any]]
        The PR's reviews, oldest first.
    head_sha : str
        The PR's head commit when it was read.
    """

    author: str
    changed_files: int
    files: list[str]
    renamed_from: list[str]
    reviews: list[Mapping[str, Any]]
    head_sha: str


def _ownership_inputs(repo: str, pr: int) -> OwnershipInputs:
    pull = reviewer_state._gh_json(["api", f"repos/{repo}/pulls/{pr}"])
    listed = (
        reviewer_state._gh_json(
            ["api", "--paginate", f"repos/{repo}/pulls/{pr}/files?per_page=100"]
        )
        or []
    )
    reviews = (
        reviewer_state._gh_json(
            ["api", "--paginate", f"repos/{repo}/pulls/{pr}/reviews?per_page=100"]
        )
        or []
    )
    return OwnershipInputs(
        author=pull["user"]["login"],
        changed_files=int(pull["changed_files"]),
        files=[f["filename"] for f in listed],
        renamed_from=[
            f["previous_filename"] for f in listed if f.get("previous_filename")
        ],
        reviews=reviews,
        head_sha=str(pull["head"]["sha"]),
    )


def _codeowners_rules() -> list[path_owners.Rule] | None:
    """Return the checkout's CODEOWNERS rules, or None when there are none to enforce.

    A file naming no user owner (empty, ownerless, or only lines ``path_owners``
    skips) gates nothing, so it counts as no file: the gate stays invisible.
    """
    path = path_owners.find_codeowners(REPO_ROOT)
    if path is None:
        return None
    rules = path_owners.load_rules(path)
    return rules if any(rule.owners for rule in rules) else None


def ownership_gate_status(repo: str, pr: int, head_sha: str) -> tuple[str, str] | None:
    """Return the ownership half of the gate for ``pr``, or None without CODEOWNERS.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    pr : int
        Pull request number.
    head_sha : str
        The head the gate is being computed for.

    Returns
    -------
    tuple[str, str] or None
        ``("failure", description)`` while a path owner's approval is missing,
        ``("success", OWNERSHIP_CLEAR)`` when none is, or None when the default
        branch has no CODEOWNERS naming a user owner (the PR isn't read at all).
    """
    return _ownership(repo, pr, head_sha)[1]


def _ownership(
    repo: str, pr: int, head_sha: str | None
) -> tuple[str, tuple[str, str] | None]:
    """Return (head SHA, ownership status) for ``pr``; see ``ownership_gate_status``.

    With ``head_sha`` None the head is the one the PR read reports (no reviewer
    state to take it from), so the pull is fetched once.
    """
    rules = _codeowners_rules()
    if rules is None:
        return head_sha or "", None
    pr_in = _ownership_inputs(repo, pr)
    head = head_sha or pr_in.head_sha
    status = path_owners.ownership_status(
        rules,
        pr_in.files,
        pr_in.author,
        path_owners.approvers(pr_in.reviews, head),
        changed_files=pr_in.changed_files,
        renamed_from=pr_in.renamed_from,
    )
    return head, status or ("success", OWNERSHIP_CLEAR)


def _current_gate_status(repo: str, sha: str) -> tuple[str, str] | None:
    """Return the newest ``review-gate`` (state, description) on ``sha``, if any.

    GitHub lists a commit's statuses newest first.
    """
    statuses = json.loads(
        _gh_output(["api", f"repos/{repo}/commits/{sha}/statuses?per_page=100"]) or "[]"
    )
    for status in statuses:
        if status.get("context") == STATUS_CONTEXT:
            return (status.get("state") or "", status.get("description") or "")
    return None


def _open_prs(repo: str) -> list[int]:
    """Return every open PR number (REST pagination, no cap)."""
    out = _gh_output(
        [
            "api",
            "--paginate",
            f"repos/{repo}/pulls?state=open&per_page=100",
            "--jq",
            ".[].number",
        ]
    )
    return [int(n) for n in out.split()]


def _post(repo: str, sha: str, state: str, description: str) -> None:
    args = [
        "api",
        "-X",
        "POST",
        f"repos/{repo}/statuses/{sha}",
        "-f",
        f"state={state}",
        "-f",
        f"context={STATUS_CONTEXT}",
        "-f",
        f"description={description}",
    ]
    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        args += ["-f", f"target_url={server}/{repo}/actions/runs/{run_id}"]
    _gh(args)


def _want(
    repo: str, pr: int, adapter: reviewer_state.ReviewerAdapter | None
) -> tuple[str, tuple[str, str] | None]:
    """Return (head SHA, status to post) for ``pr``; no status when nothing gates it.

    A missing owner's approval wins (it is the slower, human half); otherwise the
    reviewer's gate decides, or the ownership half alone when there is no reviewer.
    """
    if adapter is None:
        return _ownership(repo, pr, None)
    state = reviewer_state.collect_state(repo, pr, adapter)
    head_sha = state["head_sha"]
    owners = ownership_gate_status(repo, pr, head_sha)
    if owners is not None and owners[0] == "failure":
        return head_sha, owners
    return head_sha, gate_status(state)


def post_for_pr(
    repo: str, pr: int, adapter: reviewer_state.ReviewerAdapter | None
) -> None:
    """Compute the gate for ``pr`` and post it on the head it was computed for.

    The gate fails while a path owner's approval is missing, whatever the reviewer's
    state; otherwise it is the reviewer's gate (``gate_status``). With no reviewer
    (``adapter`` None) it is the ownership half alone, and nothing is posted when
    there is no CODEOWNERS file naming a user owner either.

    Runs are never cancelled (a cancelled run is a red X on the PR and on `main`),
    so several can overlap. A run posts only when the head's newest status differs
    from what it computed, then re-reads and repeats until they match: if an
    overlapping run's stale result lands on top, the next pass puts the fresh one
    back. Skipping unchanged statuses also keeps the periodic sweep well under
    GitHub's 1,000-statuses-per-SHA limit.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    pr : int
        Pull request number.
    adapter : reviewer_state.ReviewerAdapter or None
        Configured reviewer adapter, or None when no reviewer is active.
    """
    posts = 0
    while True:
        head_sha, want = _want(repo, pr, adapter)
        # Never a later head: a push mid-run triggers its own run for the new head.
        if (
            want is None
            or _current_gate_status(repo, head_sha) == want
            or posts == _MAX_POSTS
        ):
            return
        _post(repo, head_sha, *want)
        posts += 1


def post_for_all_open_prs(
    repo: str, adapter: reviewer_state.ReviewerAdapter | None
) -> None:
    """Recompute every open PR (the periodic sweep heals any status left stale).

    One PR's failure doesn't stop the rest; the run fails at the end if any did.
    """
    prs = _open_prs(repo)
    failed = []
    for pr in prs:
        try:
            post_for_pr(repo, pr, adapter)
        except Exception as exc:  # noqa: BLE001 -- isolate per PR, report below
            print(f"#{pr}: {exc}", file=sys.stderr)
            failed.append(pr)
    if failed:
        raise SystemExit(f"{len(failed)} of {len(prs)} PRs failed: {failed}")


def post_for_merge_group(repo: str, sha: str) -> None:
    """Pass a merge-group commit: a PR must satisfy its checks to be enqueued."""
    _post(repo, sha, "success", "PR-level review gate held when it was enqueued")


def main() -> None:
    """Post the gate for ``--pr``, or pass a ``--merge-group-sha`` commit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=int)
    target.add_argument("--merge-group-sha")
    target.add_argument("--all-open", action="store_true", help="every open PR")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="reviewer config (default: <repo>/.github/pr-reviewer.json)",
    )
    args = parser.parse_args()
    if args.merge_group_sha:
        post_for_merge_group(args.repo, args.merge_group_sha)
        return
    adapter: reviewer_state.ReviewerAdapter | None
    try:
        adapter = reviewer_state.load_reviewer(args.config)
    except reviewer_state.NoActiveReviewer as exc:
        adapter = None
        if _codeowners_rules() is None:
            # Nothing to gate. Post nothing: a ruleset that requires review-gate
            # with no reviewer active stays blocked, which is the right signal.
            print(f"::notice::{exc}")
            return
        print(f"::notice::{exc} Posting the CODEOWNERS half of the gate only.")
    if args.all_open:
        post_for_all_open_prs(args.repo, adapter)
    else:
        post_for_pr(args.repo, args.pr, adapter)


if __name__ == "__main__":
    main()
