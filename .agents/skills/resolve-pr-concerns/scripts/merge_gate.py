#!/usr/bin/env python3
"""Report a PR's merge readiness to the human: every blocker at once, not the first.

The output is a **report**. ``mergeable`` says that every condition this script
checks is met; it is never permission or an instruction to act. The agent never
lands a PR: a human reads this report and decides.

Blockers, all gathered in one call:

- **Checks.** The required checks are read live from the base branch's rules
  (``repos/{repo}/rules/branches/{base}``): each must report a satisfying
  conclusion (``success``, ``neutral`` or ``skipped``). With no required-checks
  rule (or a 404), every check reported on the head must be green instead. Check
  runs and commit statuses both count. No check name or ruleset id is hard-coded.
  Only rulesets are read: classic branch protection is not.
- **Reviewer** (``reviewer_state``, the reviewer named in
  ``.github/pr-reviewer.json``): a verdict on the head, the final full review, and
  no open reviewer finding. A reviewer with a local review needs no verdict on a
  head moved after its final full review. With ``"active": null`` (or no config at
  all) this half is skipped.
- **Humans** (``human_review``): a standing request for changes blocks until that
  reviewer's own new review clears it. Unresolved review threads block only while
  a ``pull_request`` rule sets ``required_review_thread_resolution``; a human
  resolves answered threads (the agent never resolves them). Otherwise their count
  is listed under ``info``, not as a blocker.
- **Merge state.** ``mergeStateStatus`` must be ``CLEAN``, ``UNSTABLE`` (only a
  non-required check fails) or ``HAS_HOOKS``. ``BEHIND`` blocks only while a
  required-checks rule sets ``strict_required_status_checks_policy``: a merge
  queue replaces the strict policy, and a PR behind its base is then fine.
- **Acceptance audit**, only when ``--acceptance-audit-clean false`` is given.

Unthreaded reviewer findings are listed under ``info``, never as blockers: they
have no stable per-finding id to key a disposition on, so answering them in the
one top-level disposition comment stays the agent's own check.

``--stack`` reports every layer of the stack ``--pr`` belongs to (read from
``repos/{repo}/stacks?pull_request=N``) and is clear only when every open layer
is. A PR in no stack, or a repository without that API, is reported as a
one-layer stack, and ``info`` says so.

Usage::

    python3 merge_gate.py --repo <owner>/<repo> --pr <n> [--stack]
        [--acceptance-audit-clean true|false] [--config PATH]

Prints JSON: ``mergeable``, ``already_merged``, ``blockers``, ``info`` (with
``unthreaded_findings`` and ``notes``) and, with ``--stack``, ``layers``.

Exit codes: 0 -- nothing blocks (or the PR is already merged); 1 -- blocked;
2 -- a ``gh`` call failed or GitHub answered an unexpected shape; 64 -- usage
error, or an explicit ``--config`` that does not exist or is invalid.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404 -- only to catch the gh failure ``reviewer_state`` raises
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple, NoReturn
from urllib.parse import quote

import human_review
import reviewer_state

EXIT_CLEAR = 0
EXIT_BLOCKED = 1
EXIT_GH_FAILED = 2
EXIT_USAGE = 64

# A required check counts as satisfied when GitHub treats it as non-failing.
SATISFYING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
# Check-run statuses and commit-status states of a check that has not finished.
UNFINISHED_STATES = frozenset(
    {"queued", "in_progress", "pending", "waiting", "requested", "expected"}
)
# UNSTABLE means a non-required check is failing; the rules still allow it.
PASSING_MERGE_STATES = frozenset({"CLEAN", "UNSTABLE", "HAS_HOOKS"})
_BEHIND = "BEHIND"

_THREADS_QUERY = """
query MergeGateThreads($owner:String!, $name:String!, $pr:Int!, $endCursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$pr) {
      reviewThreads(first:100, after:$endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved }
      }
    }
  }
}
"""


def _gh_json(args: Sequence[str]) -> Any:
    """Run ``gh`` and parse its JSON, flattening ``--paginate`` array pages."""
    return reviewer_state._gh_json(args)


def _not_found(exc: subprocess.CalledProcessError) -> bool:
    """Return whether a failed ``gh api`` call was an HTTP 404."""
    return "HTTP 404" in str(exc.stderr or "")


def _pages(value: Any) -> list[Any]:
    """Return a paginated object read as a list of its pages."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# --- required checks -------------------------------------------------------------


def branch_rules(repo: str, base: str) -> list[dict[str, Any]] | None:
    """Read the rules that apply to ``base``, from every active ruleset.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    base : str
        Base branch name.

    Returns
    -------
    list[dict[str, Any]] | None
        The branch's rules, or ``None`` when GitHub answers 404.

    Raises
    ------
    subprocess.CalledProcessError
        Any other ``gh`` failure.
    ValueError
        GitHub answered something other than a list of rules.
    """
    try:
        rules = _gh_json(
            [
                "api",
                f"repos/{repo}/rules/branches/{quote(base, safe='/')}",
                "--paginate",
            ]
        )
    except subprocess.CalledProcessError as exc:
        if _not_found(exc):
            return None
        raise
    if rules is None:
        return []
    if not isinstance(rules, list):  # a bad payload is a failed read, not a bug
        raise ValueError(  # noqa: TRY004
            f"repos/{repo}/rules/branches/{base}: expected a list of rules, got "
            f"{type(rules).__name__}"
        )
    return [r for r in rules if isinstance(r, dict)]


def _status_check_rules(
    rules: Iterable[Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    """Return the parameters of every ``required_status_checks`` rule."""
    return [
        rule.get("parameters") or {}
        for rule in rules or ()
        if rule.get("type") == "required_status_checks"
    ]


def required_checks_from_rules(
    rules: Iterable[Mapping[str, Any]] | None,
) -> tuple[str, ...] | None:
    """Return the required check contexts named by a branch's rules.

    Parameters
    ----------
    rules : Iterable[Mapping[str, Any]] | None
        The branch's rules (``branch_rules``); several rulesets can each add some.

    Returns
    -------
    tuple[str, ...] | None
        Every required context once, in rule order; ``None`` when no rule names
        one, so every reported check must be green instead.
    """
    contexts: dict[str, None] = {}
    for params in _status_check_rules(rules):
        for check in params.get("required_status_checks") or ():
            context = check.get("context") if isinstance(check, Mapping) else None
            if context:
                contexts[str(context)] = None
    return tuple(contexts) or None


def strict_up_to_date(rules: Iterable[Mapping[str, Any]] | None) -> bool:
    """Return whether the branch's rules require an up-to-date head.

    Parameters
    ----------
    rules : Iterable[Mapping[str, Any]] | None
        The branch's rules (``branch_rules``).

    Returns
    -------
    bool
        ``True`` when any required-checks rule sets
        ``strict_required_status_checks_policy``.
    """
    return any(
        params.get("strict_required_status_checks_policy") is True
        for params in _status_check_rules(rules)
    )


def threads_must_resolve(rules: Iterable[Mapping[str, Any]] | None) -> bool:
    """Return whether the branch's rules require review threads to be resolved.

    Parameters
    ----------
    rules : Iterable[Mapping[str, Any]] | None
        The branch's rules (``branch_rules``); ``None`` after a 404.

    Returns
    -------
    bool
        ``True`` when any ``pull_request`` rule sets
        ``required_review_thread_resolution``.
    """
    return any(
        rule.get("type") == "pull_request"
        and (rule.get("parameters") or {}).get("required_review_thread_resolution")
        is True
        for rule in rules or ()
    )


def required_checks(repo: str, base: str) -> tuple[str, ...] | None:
    """Read the required check contexts of ``base`` live from its rules.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    base : str
        Base branch name.

    Returns
    -------
    tuple[str, ...] | None
        The required contexts; ``None`` when there is no required-checks rule (or
        the rules read 404), meaning every reported check must be green.
    """
    return required_checks_from_rules(branch_rules(repo, base))


def _severity(conclusion: str) -> int:
    """Rank a conclusion: 0 satisfying, 1 not finished, 2 failing."""
    value = conclusion.lower()
    if value in SATISFYING_CONCLUSIONS:
        return 0
    return 1 if value in UNFINISHED_STATES else 2


def _record(found: dict[str, str], name: str, conclusion: str) -> None:
    """Keep the worse of two results reported under one name."""
    if name not in found or _severity(conclusion) > _severity(found[name]):
        found[name] = conclusion


def check_conclusions(repo: str, head: str) -> dict[str, str]:
    """Return every check reported on ``head``: check runs and commit statuses.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    head : str
        Head commit SHA.

    Returns
    -------
    dict[str, str]
        Conclusion (or, unfinished, status) per check name or status context.
    """
    found: dict[str, str] = {}
    runs = _gh_json(["api", f"repos/{repo}/commits/{head}/check-runs", "--paginate"])
    for page in _pages(runs):
        for run in (page or {}).get("check_runs") or []:
            conclusion = run.get("conclusion") or run.get("status") or "pending"
            _record(found, str(run.get("name")), str(conclusion))
    statuses = (
        _gh_json(["api", f"repos/{repo}/commits/{head}/statuses", "--paginate"]) or []
    )
    newest: dict[str, str] = {}
    for status in statuses:  # newest first: a context's first entry is its state
        newest.setdefault(str(status.get("context")), str(status.get("state")))
    for context, state in newest.items():
        _record(found, context, state)
    return found


def unresolved_threads(repo: str, pr: int) -> int:
    """Count the PR's unresolved review threads, across every page.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    pr : int
        Pull request number.

    Returns
    -------
    int
        Review threads not marked resolved.
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
    return sum(
        1
        for page in _pages(pages)
        for thread in page["data"]["repository"]["pullRequest"]["reviewThreads"][
            "nodes"
        ]
        if not thread.get("isResolved")
    )


# --- the gate --------------------------------------------------------------------


def _check_blockers(
    conclusions: Mapping[str, str],
    required: Sequence[str] | None,
    notes: list[str],
) -> list[str]:
    """Return the check blockers; note checks outside the required set."""
    blockers: list[str] = []
    if required is None:
        for name, conclusion in conclusions.items():
            severity = _severity(conclusion)
            if severity == 1:
                blockers.append(f"check has not finished: {name} = {conclusion}")
            elif severity == 2:
                blockers.append(f"check not green: {name} = {conclusion}")
        return blockers
    for name in required:
        reported = conclusions.get(name)
        if reported is None:
            blockers.append(f"required check has not reported: {name}")
        elif _severity(reported):
            blockers.append(f"required check not green: {name} = {reported}")
    others = [
        f"{name} = {conclusion}"
        for name, conclusion in conclusions.items()
        if name not in required and _severity(conclusion)
    ]
    if others:
        notes.append(
            "checks outside the required set are not green (not blockers): "
            + ", ".join(others)
        )
    return blockers


def _reviewer_blockers(
    *,
    verdict: str,
    open_findings: int,
    name: str,
    trigger_comment: str,
    final_outcome: str,
    final_comment: str | None,
    local_review: bool,
) -> list[str]:
    """Return the configured reviewer's blockers."""
    blockers: list[str] = []
    if verdict == "none" and local_review:
        pass  # gated on the final full review and open findings below
    elif verdict == "none":
        blockers.append(
            f"{name} has not reviewed the current head SHA — "
            f"trigger `{trigger_comment}`"
        )
    elif verdict not in ("pass", "findings"):
        blockers.append(f"unrecognized {name} verdict: {verdict}")
    if open_findings:
        blockers.append(f"{open_findings} unresolved {name} finding(s)")

    if final_outcome == "not_requested":
        blockers.append(
            f"{name} has not run its final full review — post `{final_comment}` "
            f"once {'local' if local_review else 'incremental'} reviews are clean"
        )
    elif final_outcome == "pending":
        blockers.append(f"{name}'s final full review has not finished")
    elif final_outcome == "failed":
        blockers.append(
            f"{name}'s final full review failed — wait 20+ minutes, post "
            f"`{final_comment}` once more, then escalate to a human if it fails again"
        )
    elif final_outcome not in ("finished", "not_required"):
        blockers.append(f"unrecognized final-review outcome: {final_outcome}")
    return blockers


def _merge_state_blocker(status: str, strict: bool, notes: list[str]) -> str | None:
    """Return the ``mergeStateStatus`` blocker, if any."""
    if status == _BEHIND and strict:
        return (
            f"mergeStateStatus is {_BEHIND} and the base branch's rules require an "
            "up-to-date branch — bring it up to date with its base"
        )
    if status == _BEHIND:
        notes.append(
            f"mergeStateStatus is {_BEHIND}, but the base branch's rules do not "
            "require an up-to-date branch (not a blocker)"
        )
        return None
    if status in PASSING_MERGE_STATES:
        return None
    passing = sorted(PASSING_MERGE_STATES | ({_BEHIND} if not strict else set()))
    return f"mergeStateStatus is {status} (need one of {', '.join(passing)})"


def evaluate_gate(
    *,
    check_conclusions: Mapping[str, str],
    merge_state_status: str,
    unresolved_thread_total: int,
    required_checks: Sequence[str] | None = None,
    strict_up_to_date: bool = False,
    threads_must_resolve: bool = False,
    pr_state: str = "OPEN",
    reviewer_verdict: str | None = None,
    open_reviewer_findings: int = 0,
    reviewer_name: str = "the PR reviewer",
    reviewer_key: str | None = None,
    trigger_comment: str = "",
    final_review_outcome: str = "not_required",
    final_review_comment: str | None = None,
    local_review: bool = False,
    unthreaded_findings: Sequence[Mapping[str, Any]] = (),
    verdict_carried_from: str | None = None,
    changes_requested_by: Sequence[str] = (),
    acceptance_audit_clean: bool | None = None,
) -> dict[str, Any]:
    """Return the merge-readiness report for one PR: every blocker, not the first.

    A pure function over already-fetched state. The reviewer arguments come from
    ``reviewer_state.collect_state`` and the configured adapter; they only shape the
    reviewer half, and ``reviewer_verdict=None`` (no reviewer configured) skips it.

    Parameters
    ----------
    check_conclusions : Mapping[str, str]
        Conclusion (or unfinished status) per check name or status context.
    merge_state_status : str
        GitHub ``mergeStateStatus`` of the PR.
    unresolved_thread_total : int
        Count of unresolved review threads, from anyone.
    required_checks : Sequence[str] | None
        Contexts the base branch's rules require; ``None`` when no rule names any,
        so every reported check must be green.
    strict_up_to_date : bool
        The rules require an up-to-date branch, so ``BEHIND`` blocks.
    threads_must_resolve : bool
        The rules require review-thread resolution, so unresolved threads block;
        otherwise their count is only listed under ``info``.
    pr_state : str
        GitHub PR state (``OPEN``, ``CLOSED`` or ``MERGED``).
    reviewer_verdict : str | None
        Reviewer verdict on the head (``pass``, ``findings`` or ``none``); ``None``
        when no reviewer is configured (``"active": null``).
    open_reviewer_findings : int
        Count of unresolved reviewer findings.
    reviewer_name : str
        Reviewer display name, used in blocker text.
    reviewer_key : str | None
        Reviewer key in the config, reported under ``info``.
    trigger_comment : str
        Comment that triggers an incremental review, used in blocker text.
    final_review_outcome : str
        ``reviewer_state``'s ``final_review_outcome``.
    final_review_comment : str | None
        Comment that requests the final full review.
    local_review : bool
        The reviewer runs fix rounds locally, so a head moved after the final full
        review needs no verdict of its own.
    unthreaded_findings : Sequence[Mapping[str, Any]]
        Reviewer findings with no thread; listed under ``info``, never blockers.
    verdict_carried_from : str | None
        The reviewed commit whose verdict the head inherited, if any.
    changes_requested_by : Sequence[str]
        Humans whose request for changes still stands; only their new review
        clears it.
    acceptance_audit_clean : bool | None
        ``False`` adds a blocker; ``True`` or ``None`` (not asserted) adds none.

    Returns
    -------
    dict[str, Any]
        ``mergeable`` (every checked condition is met -- a report, not
        permission), ``already_merged`` (terminal: nothing left to check),
        ``blockers`` (every unmet condition) and ``info``.
    """
    notes: list[str] = []
    info: dict[str, Any] = {
        "reviewer": (
            None if reviewer_verdict is None else (reviewer_key or reviewer_name)
        ),
        "required_checks": None if required_checks is None else list(required_checks),
        "strict_up_to_date": strict_up_to_date,
        "threads_must_resolve": threads_must_resolve,
        "unresolved_threads": unresolved_thread_total,
        "verdict_carried_from": verdict_carried_from,
        "unthreaded_findings": [dict(f) for f in unthreaded_findings],
        "notes": notes,
    }
    # A MERGED PR stops having a computable mergeStateStatus (GitHub reports
    # UNKNOWN), so the normal gate would list a false blocker on work that landed.
    if pr_state == "MERGED":
        notes.append("the PR is already MERGED: nothing left to check")
        return {
            "mergeable": False,
            "already_merged": True,
            "blockers": [],
            "info": info,
        }

    blockers: list[str] = []
    if pr_state == "CLOSED":
        blockers.append("PR is CLOSED without being merged")

    if required_checks is None:
        notes.append(
            "the base branch has no required-checks rule: every reported check "
            "must be green (only rulesets are read; classic branch protection is "
            "not)"
        )
    blockers += _check_blockers(check_conclusions, required_checks, notes)

    if reviewer_verdict is None:
        notes.append(
            'no PR reviewer is configured ("active": null): the reviewer half is '
            "not checked"
        )
    else:
        blockers += _reviewer_blockers(
            verdict=reviewer_verdict,
            open_findings=open_reviewer_findings,
            name=reviewer_name,
            trigger_comment=trigger_comment,
            final_outcome=final_review_outcome,
            final_comment=final_review_comment,
            local_review=local_review,
        )
    if unthreaded_findings:
        notes.append(
            f"{len(unthreaded_findings)} unthreaded reviewer finding(s): answer them "
            "in the one top-level disposition comment (not blockers: they have no "
            "per-finding id)"
        )

    for login in changes_requested_by:
        blockers.append(
            f"@{login} requested changes — answer every item (human_review.py), "
            "then re-request their review; only their new review clears it"
        )

    if unresolved_thread_total and threads_must_resolve:
        blockers.append(
            f"{unresolved_thread_total} unresolved review thread(s); the base "
            "branch's rules require resolution: a human resolves answered threads "
            "(the agent never resolves them)"
        )
    elif unresolved_thread_total:
        notes.append(
            f"{unresolved_thread_total} unresolved review thread(s) (not a blocker: "
            "the base branch's rules do not require resolution)"
        )

    state_blocker = _merge_state_blocker(merge_state_status, strict_up_to_date, notes)
    if state_blocker:
        blockers.append(state_blocker)

    if acceptance_audit_clean is False:
        blockers.append("acceptance-criteria audit is not clean")

    return {
        "mergeable": not blockers,
        "already_merged": False,
        "blockers": blockers,
        "info": info,
    }


# --- stacks ----------------------------------------------------------------------


def evaluate_stack(
    layers: Sequence[tuple[int, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Combine per-layer reports for a PR stack, listed bottom to top.

    A stack lands as a unit, so it is clear only when every open layer is. A layer
    that already landed blocks nothing, and a stack whose every layer landed is
    terminal. Each blocker, note and unthreaded finding is tagged with its layer.

    Parameters
    ----------
    layers : Sequence[tuple[int, Mapping[str, Any]]]
        ``(pr number, report)`` pairs, bottom layer first.

    Returns
    -------
    dict[str, Any]
        The combined ``mergeable``, ``already_merged``, ``blockers`` and ``info``,
        plus the per-PR ``layers``.

    Raises
    ------
    ValueError
        ``layers`` is empty: an empty stack must never read as clear.
    """
    if not layers:
        raise ValueError("stack has no pull requests")
    blockers = [f"#{pr}: {b}" for pr, verdict in layers for b in verdict["blockers"]]
    all_merged = all(verdict["already_merged"] for _, verdict in layers)
    infos = [(pr, verdict.get("info") or {}) for pr, verdict in layers]
    return {
        "mergeable": not all_merged and not blockers,
        "already_merged": all_merged,
        "blockers": blockers,
        "info": {
            "unthreaded_findings": [
                {"pr": pr, **finding}
                for pr, info in infos
                for finding in info.get("unthreaded_findings") or []
            ],
            "notes": [
                f"#{pr}: {note}"
                for pr, info in infos
                for note in info.get("notes") or []
            ],
        },
        "layers": [{"pr": pr, **verdict} for pr, verdict in layers],
    }


def single_layer_stack(pr: int, verdict: Mapping[str, Any]) -> dict[str, Any]:
    """Report a PR in no stack as a one-layer stack, saying so under ``info``.

    Parameters
    ----------
    pr : int
        Pull request number.
    verdict : Mapping[str, Any]
        Its single-PR report.

    Returns
    -------
    dict[str, Any]
        The ``evaluate_stack`` report of the one layer, with a note.
    """
    report = evaluate_stack([(pr, verdict)])
    report["info"]["notes"].append(
        f"PR #{pr} is not in a stack (or the repository has no stacks API): "
        "reported as a one-layer stack"
    )
    return report


def stack_prs(repo: str, pr: int) -> list[int] | None:
    """Return the PR numbers of the stack that ``pr`` belongs to, bottom to top.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    pr : int
        Pull request number in the stack.

    Returns
    -------
    list[int] | None
        PR numbers of every layer, bottom to top; ``None`` when ``pr`` is in no
        stack or the stacks API answers 404.

    Raises
    ------
    subprocess.CalledProcessError
        Any other ``gh`` failure.
    """
    try:
        stacks = _gh_json(
            ["api", f"repos/{repo}/stacks?pull_request={pr}", "--paginate"]
        )
    except subprocess.CalledProcessError as exc:
        if _not_found(exc):
            return None
        raise
    for stack in _pages(stacks):
        if not isinstance(stack, Mapping):
            continue
        numbers = [int(p["number"]) for p in stack.get("pull_requests") or []]
        if pr in numbers:
            return numbers
    return None


# --- live collection -------------------------------------------------------------


class ReviewConfig(NamedTuple):
    """The reviewer config, loaded once per run and shared by every stack layer."""

    adapter: reviewer_state.ReviewerAdapter | None
    caps: reviewer_state.RoundCaps | None
    bot_logins: frozenset[str]


def load_config(config_path: Path | None) -> ReviewConfig:
    """Load the reviewer adapter, its round caps and every reviewer's bot logins.

    Parameters
    ----------
    config_path : Path | None
        An explicit reviewer config, which must exist; or ``None`` for the repo's
        default, whose absence means no reviewer is configured.

    Returns
    -------
    ReviewConfig
        The adapter and caps (``None`` with ``"active": null`` or no default
        config) and the bot logins ``human_review`` treats as non-human.

    Raises
    ------
    FileNotFoundError
        An explicit ``config_path`` does not exist.
    ValueError
        The config is invalid.
    """
    try:
        adapter: reviewer_state.ReviewerAdapter | None = reviewer_state.load_reviewer(
            config_path
        )
    except reviewer_state.NoActiveReviewer:
        adapter = None
    except FileNotFoundError:
        if config_path is not None:
            raise
        adapter = None
    caps = reviewer_state.load_round_caps(config_path) if adapter else None
    return ReviewConfig(adapter, caps, human_review.load_bot_logins(config_path))


def collect_and_evaluate(
    repo: str,
    pr: int,
    *,
    acceptance_audit_clean: bool | None = None,
    config_path: Path | None = None,
    config: ReviewConfig | None = None,
) -> dict[str, Any]:
    """Fetch one PR's live state and report its merge readiness.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    pr : int
        Pull request number.
    acceptance_audit_clean : bool | None
        ``False`` adds a blocker; ``None`` means not asserted.
    config_path : Path | None
        Reviewer config path, or ``None`` for the default; read only when
        ``config`` is not given.
    config : ReviewConfig | None
        An already-loaded reviewer config (``load_config``).

    Returns
    -------
    dict[str, Any]
        The ``evaluate_gate`` report.

    Raises
    ------
    subprocess.CalledProcessError
        A ``gh`` call failed.
    """
    # Load first: a bad reviewer config fails before any GitHub call.
    adapter, caps, bots = config if config is not None else load_config(config_path)
    pull = _gh_json(
        [
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "state,mergeStateStatus,headRefOid,baseRefName",
        ]
    )
    if pull["state"] == "MERGED":
        return evaluate_gate(
            check_conclusions={},
            merge_state_status=pull["mergeStateStatus"],
            unresolved_thread_total=0,
            pr_state="MERGED",
        )
    rules = branch_rules(repo, pull["baseRefName"])
    reviewer: dict[str, Any] = {}
    if adapter is not None and caps is not None:
        state = reviewer_state.collect_state(repo, pr, adapter, caps)
        reviewer = {
            "reviewer_verdict": state["verdict"],
            "open_reviewer_findings": len(state["open_findings"]),
            "reviewer_name": adapter.display_name,
            "reviewer_key": adapter.key,
            "trigger_comment": adapter.trigger_comment,
            "final_review_outcome": state["final_review_outcome"],
            "final_review_comment": adapter.final_review_comment,
            "local_review": adapter.local_review is not None,
            "unthreaded_findings": state.get("unthreaded_findings") or [],
            "verdict_carried_from": state.get("verdict_carried_from"),
        }
    return evaluate_gate(
        check_conclusions=check_conclusions(repo, pull["headRefOid"]),
        merge_state_status=pull["mergeStateStatus"],
        unresolved_thread_total=unresolved_threads(repo, pr),
        required_checks=required_checks_from_rules(rules),
        strict_up_to_date=strict_up_to_date(rules),
        threads_must_resolve=threads_must_resolve(rules),
        pr_state=pull["state"],
        changes_requested_by=human_review.standing_requests(repo, pr, bots),
        acceptance_audit_clean=acceptance_audit_clean,
        **reviewer,
    )


def collect_and_evaluate_stack(
    repo: str,
    pr: int,
    *,
    acceptance_audit_clean: bool | None = None,
    config_path: Path | None = None,
    config: ReviewConfig | None = None,
) -> dict[str, Any]:
    """Report every layer of ``pr``'s stack, then combine them.

    The reviewer config is loaded once and shared by every layer.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    pr : int
        Pull request number of any layer in the stack.
    acceptance_audit_clean : bool | None
        ``False`` adds a blocker to every layer; ``None`` means not asserted.
    config_path : Path | None
        Reviewer config path, or ``None`` for the default; read only when
        ``config`` is not given.
    config : ReviewConfig | None
        An already-loaded reviewer config (``load_config``).

    Returns
    -------
    dict[str, Any]
        The combined ``evaluate_stack`` report; a PR in no stack is a one-layer
        stack (``single_layer_stack``).
    """
    loaded = config if config is not None else load_config(config_path)
    numbers = stack_prs(repo, pr)
    if numbers is None:
        single = collect_and_evaluate(
            repo, pr, acceptance_audit_clean=acceptance_audit_clean, config=loaded
        )
        return single_layer_stack(pr, single)
    return evaluate_stack(
        [
            (
                number,
                collect_and_evaluate(
                    repo,
                    number,
                    acceptance_audit_clean=acceptance_audit_clean,
                    config=loaded,
                ),
            )
            for number in numbers
        ]
    )


# --- command line ----------------------------------------------------------------


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


def _bool_arg(value: str) -> bool:
    """Parse ``true`` / ``false``."""
    lowered = value.lower()
    if lowered not in ("true", "false"):
        raise argparse.ArgumentTypeError(f"expected true or false, got {value!r}")
    return lowered == "true"


def main() -> NoReturn:
    """Print the merge-readiness report for ``--pr`` (or its stack) as JSON."""
    parser = _Parser(
        description=(
            "Report a PR's merge readiness as JSON: every blocker at once. "
            "A report for the human, never permission to land the PR."
        )
    )
    parser.add_argument("--repo", required=True, type=_repo_arg, help="owner/name")
    parser.add_argument("--pr", required=True, type=int, help="pull request number")
    parser.add_argument(
        "--stack",
        action="store_true",
        help="report every layer of the stack --pr belongs to",
    )
    parser.add_argument(
        "--acceptance-audit-clean",
        type=_bool_arg,
        default=None,
        metavar="true|false",
        help="false adds a blocker; omitted adds none",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"reviewer config (default: {reviewer_state.DEFAULT_CONFIG_PATH}); "
        "must exist when given",
    )
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from exc
    collect = collect_and_evaluate_stack if args.stack else collect_and_evaluate
    try:
        report = collect(
            args.repo,
            args.pr,
            acceptance_audit_clean=args.acceptance_audit_clean,
            config=config,
        )
    except subprocess.CalledProcessError as exc:
        print(f"gh failed: {(exc.stderr or '').strip() or exc}", file=sys.stderr)
        raise SystemExit(EXIT_GH_FAILED) from exc
    except (KeyError, TypeError, ValueError) as exc:
        # A payload shape this script doesn't know: a failed read, never a report.
        print(f"unexpected GitHub payload: {exc!r}", file=sys.stderr)
        raise SystemExit(EXIT_GH_FAILED) from exc
    print(json.dumps(report, indent=2))
    raise SystemExit(EXIT_BLOCKED if report["blockers"] else EXIT_CLEAR)


if __name__ == "__main__":
    main()
