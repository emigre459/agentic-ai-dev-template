#!/usr/bin/env python3
"""Run one local review round on the pushed PR head and record it on the PR.

A PR review request spends the developer's scarce PR quota, so a reviewer that
declares ``local_review`` in ``.github/pr-reviewer.json`` runs its fix rounds here,
on the developer's machine, against a separate allowance; the PR gets one review
request, the final full review. Each round is recorded as a plain PR comment
carrying a hidden ``<!-- local-review:v1 {json} -->`` marker. It mentions nobody, so
it is not a review request, and ``reviewer_state`` budgets local rounds from those
records (``review_rounds.max_local_before_final`` / ``max_local_after_final``).

Run it from the PR's worktree with the head pushed. It reviews the head against
``merge-base HEAD origin/<base>``. It prints one JSON object and exits with its
status:

- ``reviewed`` (0): the round ran and was recorded; fix ``findings``, push, run again.
- ``already_reviewed`` (0): this head already has a local round; push new commits
  first.
- ``not_configured`` (0): the reviewer has no local review; use PR rounds.
- ``exhausted`` (75): the local allowance is spent or under the adapter's
  ``min_remaining`` (``review_preflight``), or the review command failed to start
  with the adapter's ``start_failure_pattern`` while a re-read shows the allowance
  under that minimum; wait ``recheck_after_seconds`` and run this again. A reviewer
  with no ``start_failure_pattern`` never reads a start failure as ``exhausted``.
- ``budget_spent`` (65): this phase's local rounds are used up; never run past the
  cap silently.
- ``head_mismatch`` (65): the local HEAD isn't the PR head; push, or check it out.
- ``failed`` (1): the review command, git or GitHub failed; ``detail`` and
  ``remedy`` say why. If the round ran but recording it failed, the result carries
  the ``findings`` and the ``record_body`` to post by hand: don't re-run.

Every result that ran a preflight carries ``remaining_before`` and, once the review
command ran, ``remaining_after``: the reviews left per the preflight before and
after it (``null`` when unreadable). The PR record carries both too.

A usage error or a bad reviewer config exits 64.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 -- fixed git/gh argv or the repo's own config
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import review_preflight
from reviewer_state import (
    BASE_PLACEHOLDER,
    LOCAL_FINDING_FIELDS,
    LocalReviewSpec,
    NoActiveReviewer,
    ReviewerAdapter,
    RoundCaps,
    _gh_json,
    load_reviewer,
    load_round_caps,
    local_review_marker,
    local_review_state,
)

EXIT_CODES = {
    "reviewed": 0,
    "already_reviewed": 0,
    "not_configured": 0,
    "exhausted": 75,
    "budget_spent": 65,
    "head_mismatch": 65,
    "failed": 1,
}
#: Findings listed in the PR record; the rest are counted, and the JSON has them all.
_MAX_LISTED = 30
#: Right after a push the PR API can still name the old head for a few seconds.
_API_CATCH_UP_TRIES = 6
_API_CATCH_UP_SECONDS = 5.0


class Deps(NamedTuple):
    """The outside world, injected so the round logic is testable without it."""

    gh: Callable[[Sequence[str]], Any]
    git: Callable[[Sequence[str]], str]
    run: Callable[[Sequence[str], float], tuple[int, str, str]]
    post: Callable[[str, int, str], str]
    preflight: Callable[[ReviewerAdapter], dict[str, Any]]
    sleep: Callable[[float], None]


def _git(args: Sequence[str]) -> str:
    done = subprocess.run(  # nosec B603 B607 -- fixed git argv, no shell
        ["git", *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _run(argv: Sequence[str], timeout: float) -> tuple[int, str, str]:
    done = subprocess.run(  # nosec B603 -- argv from the repo's own config, no shell
        list(argv), capture_output=True, text=True, timeout=timeout, check=False
    )
    return done.returncode, done.stdout, done.stderr


def _post(repo: str, pr: int, body: str) -> str:
    done = subprocess.run(  # nosec B603 B607 -- fixed gh argv, body on stdin
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{repo}/issues/{pr}/comments",
            "--input",
            "-",
        ],
        input=json.dumps({"body": body}),
        capture_output=True,
        text=True,
        check=True,
    )
    return str(json.loads(done.stdout).get("html_url") or "")


def real_deps() -> Deps:
    """Return the subprocess-backed dependencies for a real run.

    Returns
    -------
    Deps
        ``gh`` / ``git`` / the review command / the PR comment post / the preflight.
    """
    return Deps(
        gh=_gh_json,
        git=_git,
        run=_run,
        post=_post,
        preflight=review_preflight.check,
        sleep=time.sleep,
    )


def _pushed(deps: Deps, head_ref: str, local: str) -> bool:
    """Return whether the remote branch already points at ``local``."""
    try:
        listing = deps.git(["ls-remote", "origin", f"refs/heads/{head_ref}"])
    except subprocess.CalledProcessError:
        return False
    return listing.split()[:1] == [local]


def _stderr(exc: subprocess.CalledProcessError) -> str:
    return str(exc.stderr or "").strip()[-500:]


def _reread(deps: Deps, adapter: ReviewerAdapter) -> dict[str, Any]:
    """Re-run the preflight after a round; an unreadable one reads as ``unknown``.

    The round has already run (and spent its allowance), so a failing usage read must
    never turn it into a failure: it is logged and ``remaining`` is ``None``.

    Parameters
    ----------
    deps : Deps
        Injected calls; ``preflight`` is the one used.
    adapter : ReviewerAdapter
        The reviewer adapter to check.

    Returns
    -------
    dict[str, Any]
        The preflight verdict, or an ``unknown`` one with no count.
    """
    try:
        return deps.preflight(adapter)
    except Exception as exc:  # noqa: BLE001 -- best-effort read after a finished round
        print(f"post-round preflight failed: {exc!r}", file=sys.stderr)
        return {"status": "unknown", "remaining": None, "recheck_after_seconds": None}


def _start_failed_for_capacity(
    adapter: ReviewerAdapter, output: str, after_check: dict[str, Any]
) -> bool:
    """Return whether a start failure is the local allowance running short.

    The adapter must name its start failure (``local_review.start_failure_pattern``)
    and that pattern must appear in ``output``. Then the failure is capacity only when
    the preflight re-read after it gives a readable ``remaining`` under the adapter's
    ``min_remaining``: the pool provably cannot pay for a round. An ``exhausted``
    state with the count at or above the minimum (the state can be unavailable with
    plenty left), an unreadable count, or the pre-round reading is not enough: a start
    failure with the pool otherwise fine is a real CLI or network failure and must not
    be masked as a wait, so it stays ``failed``.

    Parameters
    ----------
    adapter : ReviewerAdapter
        The reviewer adapter (supplies the pattern and ``min_remaining``).
    output : str
        The review command's stdout and stderr.
    after_check : dict[str, Any]
        The preflight verdict re-read after the failure.

    Returns
    -------
    bool
        ``True`` to classify the failure as ``exhausted``.
    """
    preflight, spec = adapter.local_preflight, adapter.local_review
    if preflight is None or spec is None or spec.start_failure_pattern is None:
        return False
    if not spec.start_failure_pattern.search(output):
        return False
    after = after_check.get("remaining")
    return (
        isinstance(after, int)
        and not isinstance(after, bool)
        and after < preflight.min_remaining
    )


def _recheck_after(adapter: ReviewerAdapter, verdict: dict[str, Any]) -> int:
    """Return the seconds to wait after a capacity start failure.

    Uses the post-failure preflight's own wait when it reads ``exhausted`` (it knows
    when the pool refills); otherwise the adapter's ``max_recheck_seconds`` cap.

    Parameters
    ----------
    adapter : ReviewerAdapter
        The reviewer adapter; its ``local_preflight`` supplies the cap.
    verdict : dict[str, Any]
        The preflight verdict re-read after the failure.

    Returns
    -------
    int
        Seconds to wait before running again.

    Raises
    ------
    ValueError
        If the adapter has no ``local_preflight``: a capacity failure needs one.
    """
    recheck = verdict.get("recheck_after_seconds")
    if verdict.get("status") == "exhausted" and isinstance(recheck, int):
        return recheck
    if adapter.local_preflight is None:
        raise ValueError("a capacity failure needs a local_preflight to wait on")
    return adapter.local_preflight.max_recheck_seconds


def parse_events(
    stdout: str, spec: LocalReviewSpec
) -> tuple[list[dict[str, Any]], bool]:
    """Return the normalised findings and whether the review completed.

    Parameters
    ----------
    stdout : str
        The review command's output, one JSON event per line.
    spec : LocalReviewSpec
        The adapter's local review spec.

    Returns
    -------
    tuple[list[dict[str, Any]], bool]
        Findings keyed by ``LOCAL_FINDING_FIELDS``, and whether a
        ``complete_event`` line was seen.
    """
    findings: list[dict[str, Any]] = []
    completed = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = review_preflight.dig(event, spec.event_type_path)
        if kind == spec.finding_event:
            findings.append(
                {
                    field: review_preflight.dig(event, spec.finding_fields[field])
                    for field in LOCAL_FINDING_FIELDS
                }
            )
        elif kind == spec.complete_event:
            completed = True
    return findings, completed


def _record_body(
    record: dict[str, Any], phase: str, findings: list[dict[str, Any]]
) -> str:
    """Return the PR comment that records one local round (it mentions nobody)."""
    sha7, base7 = record["head"][:7], record["base"][:7]
    when = "before the full review" if phase == "before_final" else "after it"
    lines = [
        local_review_marker(record),
        f"**Local review**, round {record['round']} ({when}) of `{sha7}` against "
        f"`{base7}`: {len(findings)} finding(s).",
        "",
    ]
    for finding in findings[:_MAX_LISTED]:
        lines.append(
            f"- **{finding['severity']}** `{finding['path']}:{finding['line']}` "
            f"{finding['title']}"
        )
    if len(findings) > _MAX_LISTED:
        lines.append(f"- …and {len(findings) - _MAX_LISTED} more")
    before, after = record.get("remaining_before"), record.get("remaining_after")
    if before is not None or after is not None:
        lines += [
            "",
            f"Local allowance: {'?' if before is None else before} -> "
            f"{'?' if after is None else after} reviews left.",
        ]
    lines += [
        "",
        "<sub>Recorded by `local_review.py`: fix rounds run locally, so this PR's one "
        "reviewer request is the final full review. Not a review request.</sub>",
    ]
    return "\n".join(lines)


def review(
    repo: str, pr: int, adapter: ReviewerAdapter, caps: RoundCaps, deps: Deps
) -> tuple[dict[str, Any], int]:
    """Run one local review round on the PR head, within its budget, and record it.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    pr : int
        Pull request number.
    adapter : ReviewerAdapter
        Configured reviewer adapter.
    caps : RoundCaps
        Local round caps.
    deps : Deps
        Injected GitHub, git, command, post, preflight and sleep calls.

    Returns
    -------
    tuple[dict[str, Any], int]
        The JSON result and its exit code.
    """
    spec = adapter.local_review

    def done(status: str, detail: str, **extra: Any) -> tuple[dict[str, Any], int]:
        """Shape one result.

        Parameters
        ----------
        status : str
            One of ``EXIT_CODES``.
        detail : str
            What happened, for the user.
        **extra : Any
            Status-specific fields.

        Returns
        -------
        tuple[dict[str, Any], int]
            The JSON result and its exit code.
        """
        return {
            "reviewer": adapter.key,
            "status": status,
            "detail": detail,
            **extra,
        }, EXIT_CODES[status]

    if spec is None:
        return done(
            "not_configured",
            f"{adapter.display_name} has no local review: use PR review rounds",
        )
    try:
        pull = deps.gh(["api", f"repos/{repo}/pulls/{pr}"])
        head, base_ref = pull["head"]["sha"], pull["base"]["ref"]
        local = deps.git(["rev-parse", "HEAD"])
        if local != head and _pushed(deps, pull["head"]["ref"], local):
            # Pushed, but the PR API hasn't caught up yet: wait for it briefly.
            for _ in range(_API_CATCH_UP_TRIES):
                deps.sleep(_API_CATCH_UP_SECONDS)
                head = deps.gh(["api", f"repos/{repo}/pulls/{pr}"])["head"]["sha"]
                if head == local:
                    break
        if local != head:
            return done(
                "head_mismatch",
                f"local HEAD {local[:7]} is not PR #{pr}'s head {head[:7]}: push your "
                "commits (or check out the PR branch), then run this again",
                head=head,
                local_head=local,
            )
        comments = deps.gh(["api", f"repos/{repo}/issues/{pr}/comments", "--paginate"])
    except subprocess.CalledProcessError as exc:
        return done(
            "failed",
            f"could not read PR #{pr}'s head: `{' '.join(map(str, exc.cmd[:3]))}` "
            f"failed: {_stderr(exc)}",
        )
    state = local_review_state(comments or [], adapter, caps, head)
    if state["reviewed_head"]:
        return done(
            "already_reviewed",
            f"{head[:7]} already has a local review: push new commits first",
            head=head,
            head_findings=state["head_findings"],
        )
    phase = state["phase"]
    used, cap = state[phase]["used"], state[phase]["cap"]
    if not state["allowed"]:
        return done(
            "budget_spent",
            f"all {cap} local round(s) {phase.replace('_', ' ')} are used",
            phase=phase,
            used=used,
            cap=cap,
        )
    allowance = deps.preflight(adapter)
    remaining_before = allowance.get("remaining")
    if allowance["status"] == "exhausted":
        return done(
            "exhausted",
            allowance["detail"],
            recheck_after_seconds=allowance["recheck_after_seconds"],
            remaining_before=remaining_before,
        )
    try:
        deps.git(["fetch", "--quiet", "origin", base_ref])
        base = deps.git(["merge-base", "HEAD", f"origin/{base_ref}"])
    except subprocess.CalledProcessError as exc:
        return done(
            "failed",
            f"could not find the merge base with origin/{base_ref}: {_stderr(exc)}",
        )
    argv = [arg.replace(BASE_PLACEHOLDER, base) for arg in spec.command]
    remedies = adapter.local_preflight.remedies if adapter.local_preflight else {}
    try:
        code, stdout, stderr = deps.run(argv, float(spec.timeout_seconds))
    except FileNotFoundError:
        return done(
            "failed",
            f"`{argv[0]}` is not installed",
            remedy=remedies.get("not_installed"),
        )
    except subprocess.TimeoutExpired:
        return done(
            "failed",
            f"`{' '.join(argv)}` timed out after {spec.timeout_seconds}s",
            remedy=remedies.get("failed"),
        )
    findings, completed = parse_events(stdout, spec)
    # What the round left in the allowance (the CLI reports no cost of its own).
    after_check = _reread(deps, adapter)
    remaining_after = after_check.get("remaining")
    if code != 0 or not completed:
        if _start_failed_for_capacity(adapter, stdout + stderr, after_check):
            recheck = _recheck_after(adapter, after_check)
            return done(
                "exhausted",
                f"`{' '.join(argv)}` failed to start with {remaining_before} "
                f"review(s) left before and {remaining_after} after: the allowance "
                f"is too low for a round; check again in {recheck}s",
                recheck_after_seconds=recheck,
                remaining_before=remaining_before,
                remaining_after=remaining_after,
            )
        return done(
            "failed",
            f"`{' '.join(argv)}` exited {code} without completing: "
            f"{stderr.strip()[-500:]}",
            remedy=allowance.get("remedy") or remedies.get("failed"),
            remaining_before=remaining_before,
            remaining_after=remaining_after,
        )
    record = {
        "reviewer": adapter.key,
        "head": head,
        "base": base,
        "round": used + 1,
        "phase": phase,
        "findings": len(findings),
        "remaining_before": remaining_before,
        "remaining_after": remaining_after,
    }
    body = _record_body(record, phase, findings)
    try:
        url = deps.post(repo, pr, body)
    except subprocess.CalledProcessError as exc:
        # The review ran and spent its allowance, so hand back its findings and the
        # record: re-running would review the same head again.
        return done(
            "failed",
            f"the round ran but recording it on PR #{pr} failed: {_stderr(exc)}; "
            "post `record_body` as a PR comment once the PR is reachable, don't "
            "re-run",
            findings=findings,
            record_body=body,
            remaining_before=remaining_before,
            remaining_after=remaining_after,
        )
    return done(
        "reviewed",
        f"round {used + 1} of {cap} {phase.replace('_', ' ')}: "
        f"{len(findings)} finding(s)",
        phase=phase,
        round=used + 1,
        cap=cap,
        head=head,
        base=base,
        findings=findings,
        comment_url=url,
        remaining_before=remaining_before,
        remaining_after=remaining_after,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one local review round for ``--pr`` and print the result as JSON.

    Parameters
    ----------
    argv : Sequence[str] or None
        Command-line arguments, or ``None`` for ``sys.argv``.

    Returns
    -------
    int
        The result's exit code, or 64 for a bad config.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument(
        "--config", type=Path, default=None, help="reviewer config path"
    )
    args = parser.parse_args(argv)
    try:
        adapter = load_reviewer(args.config)
        caps = load_round_caps(args.config)
    except NoActiveReviewer as exc:
        result = {"reviewer": None, "status": "not_configured", "detail": str(exc)}
        print(json.dumps(result, indent=2))
        return EXIT_CODES["not_configured"]
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 64
    result, code = review(args.repo, args.pr, adapter, caps, real_deps())
    print(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
