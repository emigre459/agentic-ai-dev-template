#!/usr/bin/env python3
"""Reconcile this repo's live settings with .github/repo-settings/.

Covers the `main` ruleset, PR-merge prefs, and Dependabot security updates.
Reads the canonical settings, diffs them against the live repo (via `gh`), prints
the diff, and — unless ``--yes`` — asks for confirmation before applying. Idempotent.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_SETTINGS_DIR = Path(__file__).resolve().parent.parent / ".github" / "repo-settings"

# Ruleset fields we assert; everything else (id, timestamps, _links) is ignored.
_RULESET_KEYS = (
    "name",
    "target",
    "enforcement",
    "conditions",
    "rules",
    "bypass_actors",
)


@dataclass
class Desired:
    """The canonical settings loaded from disk."""

    ruleset: dict
    merge: dict
    security: dict


def load_desired(settings_dir: Path) -> Desired:
    """Load the desired ruleset + merge + security settings from ``settings_dir``."""
    ruleset = json.loads((settings_dir / "ruleset.json").read_text(encoding="utf-8"))
    merge = json.loads(
        (settings_dir / "merge-settings.json").read_text(encoding="utf-8")
    )
    security = json.loads(
        (settings_dir / "security-settings.json").read_text(encoding="utf-8")
    )
    return Desired(ruleset=ruleset, merge=merge, security=security)


def ruleset_for_phase(ruleset: dict, phase: str) -> dict:
    """Return the ruleset safe for the requested initialization phase.

    Parameters
    ----------
    ruleset
        Canonical final ruleset loaded from disk.
    phase
        ``bootstrap`` defers required CI contexts; ``final`` keeps every rule.

    Returns
    -------
    dict
        A deep copy suitable for reconciliation.

    Raises
    ------
    ValueError
        If ``phase`` is not supported.
    """
    if phase not in {"bootstrap", "final"}:
        raise ValueError(f"unknown settings phase: {phase!r}")
    phased = copy.deepcopy(ruleset)
    if phase == "bootstrap":
        phased["rules"] = [
            rule
            for rule in phased.get("rules", [])
            if rule.get("type") != "required_status_checks"
        ]
    return phased


def find_main_ruleset(existing: list[dict]) -> dict | None:
    """Return the ruleset named ``main`` from ``existing``, or None."""
    for rs in existing:
        if rs.get("name") == "main":
            return rs
    return None


def _is_subset(desired: object, current: object) -> bool:
    """Return True when ``desired`` is contained in ``current`` (recursively).

    GitHub's rulesets API returns rules/conditions with default-populated fields
    that our sanitized ruleset.json omits, so a strict ``==`` would never match an
    already-applied ruleset and we'd re-PUT it every run. Subset semantics: dicts
    match when every desired key is present and contained; lists match when each
    desired element is contained in some current element; scalars match on ``==``.
    """
    if isinstance(desired, dict):
        if not isinstance(current, dict):
            return False
        return all(
            k in current and _is_subset(v, current[k]) for k, v in desired.items()
        )
    if isinstance(desired, list):
        if not isinstance(current, list):
            return False
        return all(any(_is_subset(d, c) for c in current) for d in desired)
    return desired == current


def ruleset_matches(desired: dict, current: dict) -> bool:
    """Return True when ``current`` already satisfies ``desired`` on the asserted keys.

    Uses subset (not strict-equality) comparison so GitHub's API-default fields on
    the live ruleset don't cause a spurious "needs update" every run.
    """
    return all(
        _is_subset(desired[k], current.get(k)) for k in _RULESET_KEYS if k in desired
    )


def merge_settings_match(desired: dict, current: dict) -> bool:
    """Return True when every desired merge key already has the desired value."""
    return all(current.get(k) == v for k, v in desired.items())


def security_settings_match(desired: dict, current_repo: dict) -> bool:
    """Return True when the live repo's security config already satisfies desired.

    ``current_repo`` is the full `GET /repos/{owner}/{repo}` response (the same
    object already fetched for merge-settings matching) — its
    ``security_and_analysis`` block carries other toggles (secret scanning, etc.)
    our sanitized security-settings.json doesn't assert on, so subset (not
    strict-equality) comparison via ``_is_subset`` avoids a spurious mismatch.
    """
    return _is_subset(desired, current_repo)


def plan_actions(
    current_rulesets: list[dict],
    current_merge: dict,
    desired_ruleset: dict,
    desired_merge: dict,
    desired_security: dict,
    forbidden_rule_types: set[str] | None = None,
) -> list[tuple]:
    """Compute the minimal set of apply actions.

    Returns a list of tuples: ``("ruleset", "POST"|"PUT", id_or_None)``,
    ``("merge", "PATCH", None)``, and/or ``("security", "PATCH", None)``. Empty
    list means everything is already aligned.
    """
    actions: list[tuple] = []
    main = find_main_ruleset(current_rulesets)
    if main is None:
        actions.append(("ruleset", "POST", None))
    elif not ruleset_matches(desired_ruleset, main) or any(
        rule.get("type") in (forbidden_rule_types or set())
        for rule in main.get("rules", [])
    ):
        actions.append(("ruleset", "PUT", main["id"]))
    if not merge_settings_match(desired_merge, current_merge):
        actions.append(("merge", "PATCH", None))
    if not security_settings_match(desired_security, current_merge):
        actions.append(("security", "PATCH", None))
    return actions


def _run_gh(
    args: list[str],
    runner: Callable[..., Any] = subprocess.run,
    **kwargs: Any,
) -> Any:
    """Run GitHub CLI and translate failures into actionable messages."""
    try:
        return runner(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=True,
            **kwargs,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "GitHub CLI `gh` was not found. Install it, ensure it is on PATH, "
            "then run `gh auth login`."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "No error detail returned.").strip()
        permission_hint = ""
        if any(token in detail for token in ("HTTP 403", "HTTP 404", "accessible")):
            permission_hint = (
                " Confirm that the target repository is correct and the active "
                "GitHub account has administrator permission."
            )
        raise RuntimeError(
            f"GitHub command `gh {' '.join(args)}` failed. "
            f"GitHub said: {detail}.{permission_hint}"
        ) from exc


def _gh_json(args: list[str], runner: Callable[..., Any] = subprocess.run) -> Any:
    """Run a `gh` command and parse its stdout as JSON."""
    proc = _run_gh(args, runner)
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def _current_rulesets(
    repo: str, runner: Callable[..., Any] = subprocess.run
) -> list[dict]:
    """Fetch this repo's rulesets, with full detail for the ``main`` one.

    `GET /repos/{owner}/{repo}/rulesets` (the list endpoint) returns bare
    summaries — no ``conditions``/``rules``/``bypass_actors`` — so comparing
    those against ``desired.ruleset`` via ``ruleset_matches`` would always see
    a mismatch and re-PUT on every run. Fetch the single-ruleset endpoint for
    ``main`` specifically to get the fields we actually need to compare.
    """
    summaries = _gh_json(["api", f"repos/{repo}/rulesets"], runner) or []
    return [
        (
            _gh_json(["api", f"repos/{repo}/rulesets/{rs['id']}"], runner)
            if rs.get("name") == "main"
            else rs
        )
        for rs in summaries
    ]


def _apply_actions(
    actions: list[tuple],
    repo: str,
    desired: Desired,
    desired_ruleset: dict,
    runner: Callable[..., Any],
) -> None:
    """Apply a confirmed settings plan to the explicit repository target."""
    for kind, method, ident in actions:
        if kind == "ruleset":
            endpoint = f"repos/{repo}/rulesets"
            if method == "PUT":
                endpoint += f"/{ident}"
            _run_gh(
                ["api", "--method", method, endpoint, "--input", "-"],
                runner,
                input=json.dumps(desired_ruleset),
            )
        elif kind == "merge":
            _run_gh(
                ["api", "--method", "PATCH", f"repos/{repo}", "--input", "-"],
                runner,
                input=json.dumps(desired.merge),
            )
        elif kind == "security":
            _run_gh(
                ["api", "--method", "PATCH", f"repos/{repo}", "--input", "-"],
                runner,
                input=json.dumps(desired.security),
            )


def main(
    argv: list[str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        required=True,
        help="explicit GitHub target in owner/repository form",
    )
    parser.add_argument(
        "--phase",
        choices=("bootstrap", "final"),
        default="final",
        help="bootstrap defers required CI checks; final applies every rule",
    )
    parser.add_argument("--yes", action="store_true", help="apply without confirmation")
    parser.add_argument("--settings-dir", default=str(REPO_SETTINGS_DIR))
    args = parser.parse_args(argv)

    desired = load_desired(Path(args.settings_dir))
    repo = str(args.repo)
    desired_ruleset = ruleset_for_phase(desired.ruleset, args.phase)
    try:
        current_rulesets = _current_rulesets(repo, runner)
        current_merge = _gh_json(["api", f"repos/{repo}"], runner) or {}
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    actions = plan_actions(
        current_rulesets,
        current_merge,
        desired_ruleset,
        desired.merge,
        desired.security,
        forbidden_rule_types=(
            {"required_status_checks"} if args.phase == "bootstrap" else None
        ),
    )
    if not actions:
        print(f"{repo}: settings already aligned — no changes.")
        return 0

    print(f"{repo}: planned {args.phase} changes:")
    for kind, method, ident in actions:
        print(f"  - {kind}: {method}" + (f" (id={ident})" if ident else ""))

    if not args.yes:
        reply = input("Apply these changes? [y/N] ").strip().lower()
        if reply != "y":
            print("Aborted.")
            return 1

    try:
        _apply_actions(actions, repo, desired, desired_ruleset, runner)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Applied {args.phase} settings to {repo}.")
    if args.phase == "bootstrap":
        print(
            "Required CI checks are deferred until the setup PR merges. "
            "After merge, run `make finalize_repo_settings TARGET_REPO="
            f"{repo}`."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
