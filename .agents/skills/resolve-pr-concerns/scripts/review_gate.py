#!/usr/bin/env python3
"""Post the vendor-neutral ``review-gate`` commit status for a PR.

The PR reviewer's own status check passes on heads it never reviewed (skipped or
rate-limited pushes), so a ruleset should require this status instead. It is green
only when the configured reviewer (``.github/pr-reviewer.json``) has a verdict on the
current head AND its final full review finished -- the state ``reviewer_state``
reports. Run by ``.github/workflows/review-gate.yml`` from the
default branch; never from PR code.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 -- fixed gh argv, no shell
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import reviewer_state

STATUS_CONTEXT = "review-gate"
_MAX_DESCRIPTION = 140  # GitHub rejects longer status descriptions
# Posts per run before giving up on a state that keeps changing (the sweep retries).
_MAX_POSTS = 5


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_DESCRIPTION else text[: _MAX_DESCRIPTION - 1] + "…"


def gate_status(state: Mapping[str, Any]) -> tuple[str, str]:
    """Return (status state, description) for one PR's ``collect_state`` output."""
    name, sha7 = state["reviewer_name"], state["head_sha"][:7]
    if state["verdict"] not in ("pass", "findings"):
        return "failure", _clip(
            f"{name} has not reviewed {sha7}: post `{state['trigger_comment']}`"
        )
    final = state["final_review_outcome"]
    command = state.get("final_review_comment")
    if final in ("finished", "not_required"):
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


def _gh(args: Sequence[str]) -> None:
    subprocess.run(  # nosec B603 B607 -- fixed gh argv, no shell
        ["gh", *args], check=True, capture_output=True, text=True
    )


def _gh_output(args: Sequence[str]) -> str:
    return subprocess.run(  # nosec B603 B607 -- fixed gh argv, no shell
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


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


def post_for_pr(repo: str, pr: int, adapter: reviewer_state.ReviewerAdapter) -> None:
    """Compute the gate for ``pr`` and post it on the head it was computed for.

    Runs are never cancelled (a cancelled run is a red X on the PR and on `main`),
    so several can overlap. A run posts only when the head's newest status differs
    from what it computed, then re-reads and repeats until they match: if an
    overlapping run's stale result lands on top, the next pass puts the fresh one
    back. Skipping unchanged statuses also keeps the periodic sweep well under
    GitHub's 1,000-statuses-per-SHA limit.
    """
    posts = 0
    while True:
        state = reviewer_state.collect_state(repo, pr, adapter)
        want = gate_status(state)
        # Never a later head: a push mid-run triggers its own run for the new head.
        if _current_gate_status(repo, state["head_sha"]) == want or posts == _MAX_POSTS:
            return
        _post(repo, state["head_sha"], *want)
        posts += 1


def post_for_all_open_prs(repo: str, adapter: reviewer_state.ReviewerAdapter) -> None:
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
    elif args.all_open:
        post_for_all_open_prs(args.repo, reviewer_state.load_reviewer(args.config))
    else:
        post_for_pr(args.repo, args.pr, reviewer_state.load_reviewer(args.config))


if __name__ == "__main__":
    main()
