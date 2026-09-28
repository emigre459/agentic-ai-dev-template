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
import os
import subprocess  # nosec B404 -- fixed gh argv, no shell
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import reviewer_state

STATUS_CONTEXT = "review-gate"
_MAX_DESCRIPTION = 140  # GitHub rejects longer status descriptions


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
    """Compute the gate for ``pr`` and post it on the head it was computed for."""
    state = reviewer_state.collect_state(repo, pr, adapter)
    # Never a later head: a push mid-run triggers its own run for the new head.
    _post(repo, state["head_sha"], *gate_status(state))


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
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="reviewer config (default: <repo>/.github/pr-reviewer.json)",
    )
    args = parser.parse_args()
    if args.merge_group_sha:
        post_for_merge_group(args.repo, args.merge_group_sha)
    else:
        post_for_pr(args.repo, args.pr, reviewer_state.load_reviewer(args.config))


if __name__ == "__main__":
    main()
