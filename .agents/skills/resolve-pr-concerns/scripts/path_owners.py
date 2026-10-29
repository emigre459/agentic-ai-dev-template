r"""Path ownership for the ``review-gate`` status: who may merge on review + CI alone.

A CODEOWNERS file names owners per path (the last matching line wins; a line with no
owner leaves its paths open to everyone). A PR passes this half of the gate when its
author owns every changed file, or the file has no owner. Otherwise an owner of each
such file must approve the PR's head. GitHub's own "require code owner review" can't
express that: an author's approval never counts, so the owner of everything could
never merge their own PR without a bypass.

This gate resolves user logins only. A line naming a team (``@org/team``) or an email
owner, alone or beside users, is *skipped*: its paths are left to GitHub's own
code-owner rule, so a team member's approval is never wrongly rejected here. A line
whose pattern uses syntax GitHub doesn't support (``!``, ``[...]``, ``\``) is
skipped too. A skipped line still takes part in last-match-wins (matched as
written), so a path whose last matching line is skipped is ungated by this gate.
Parsing never raises. GitHub auto-requests a review from every owner the file names,
so a path open to its users gets an ownerless line rather than those users as
owners. Stdlib only: ``review_gate.py`` runs without the project installed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

#: Where GitHub looks for CODEOWNERS, in its priority order (the first found wins).
CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
_MAX_DESCRIPTION = 140  # GitHub rejects longer status descriptions
_LOGIN = re.compile(r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
_DECISIVE = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})
_UNSUPPORTED = "[]\\"


class Rule(NamedTuple):
    """One CODEOWNERS line.

    Attributes
    ----------
    pattern : str
        The path pattern as written.
    owners : tuple[str, ...]
        Lowercased owner logins; empty for an ownerless or a skipped line.
    regex : re.Pattern[str]
        The compiled pattern.
    skipped : bool
        True when this gate can't resolve the line (a team or email owner, or
        unsupported pattern syntax): its paths are left to GitHub.
    """

    pattern: str
    owners: tuple[str, ...]
    regex: re.Pattern[str]
    skipped: bool = False


def _translate(body: str) -> str:
    out, i = [], 0
    while i < len(body):
        if body.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif body.startswith("**", i):
            out.append(".*")
            i += 2
        elif body[i] == "*":
            out.append("[^/]*")
            i += 1
        elif body[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(body[i]))
            i += 1
    return "".join(out)


def _compile(pattern: str) -> re.Pattern[str]:
    """Compile a CODEOWNERS pattern (gitignore rules, as GitHub documents them).

    A slash at the start or in the middle anchors the pattern to the repo root;
    otherwise it matches at any depth. A trailing slash matches a directory's
    contents. A pattern naming a directory matches everything in it, except that a
    wildcard last segment does not recurse (``docs/*`` misses ``docs/a/b.md``).

    Parameters
    ----------
    pattern : str
        The pattern as written in CODEOWNERS.

    Returns
    -------
    re.Pattern[str]
        A regex to ``fullmatch`` against repo-relative paths.
    """
    dir_only = pattern.endswith("/")
    body = pattern.strip("/")
    anchored = pattern.startswith("/") or "/" in body
    prefix = "" if anchored else "(?:.*/)?"
    last = body.rsplit("/", 1)[-1]
    if dir_only:
        suffix = "/.*"
    elif "*" in last or "?" in last:
        suffix = ""
    else:
        suffix = "(?:/.*)?"
    return re.compile(f"{prefix}{_translate(body)}{suffix}")


def _resolvable(pattern: str, owners: Sequence[str]) -> bool:
    """Return whether this gate can enforce the line: supported syntax, users only."""
    if pattern.startswith("!") or any(c in pattern for c in _UNSUPPORTED):
        return False
    return all(_LOGIN.fullmatch(owner) for owner in owners)


def parse_codeowners(text: str) -> list[Rule]:
    """Parse CODEOWNERS text into rules, in file order. Never raises.

    Parameters
    ----------
    text : str
        The CODEOWNERS file contents.

    Returns
    -------
    list[Rule]
        One rule per pattern line; a line naming a team or email owner, or using
        unsupported pattern syntax, becomes a rule with no owners and
        ``skipped=True``.
    """
    rules = []
    for raw in text.splitlines():
        tokens = (
            raw.split("#", 1)[0].split() if not raw.lstrip().startswith("#") else []
        )
        if not tokens:
            continue
        pattern, owners = tokens[0], tokens[1:]
        regex = _compile(pattern)
        if _resolvable(pattern, owners):
            rules.append(Rule(pattern, tuple(o[1:].lower() for o in owners), regex))
        else:
            rules.append(Rule(pattern, (), regex, skipped=True))
    return rules


def find_codeowners(root: Path) -> Path | None:
    """Return the CODEOWNERS file GitHub would use under ``root``, if any.

    Parameters
    ----------
    root : Path
        A checkout's root directory.

    Returns
    -------
    Path or None
        The first of ``CODEOWNERS_LOCATIONS`` that is a file, or None.
    """
    for location in CODEOWNERS_LOCATIONS:
        path = root / location
        if path.is_file():
            return path
    return None


def load_rules(path: Path) -> list[Rule]:
    """Load the rules at ``path``; a missing file owns nothing.

    Parameters
    ----------
    path : Path
        The CODEOWNERS file.

    Returns
    -------
    list[Rule]
        Its rules, or none when the file doesn't exist. Undecodable bytes are
        replaced rather than raised on.
    """
    if not path.is_file():
        return []
    return parse_codeowners(path.read_text(encoding="utf-8", errors="replace"))


def owners_for(rules: Sequence[Rule], path: str) -> tuple[str, ...] | None:
    """Return the owners this gate enforces on ``path``: the last matching rule's.

    Parameters
    ----------
    rules : Sequence[Rule]
        Parsed CODEOWNERS rules.
    path : str
        Repo-relative file path.

    Returns
    -------
    tuple[str, ...] or None
        Lowercased owner logins, or None when no rule matches, the last match is
        ownerless (the path is open), or the last match is skipped (left to GitHub).
    """
    for rule in reversed(rules):
        if rule.regex.fullmatch(path):
            return rule.owners or None
    return None


def approvers(reviews: Iterable[Mapping[str, Any]], head_sha: str) -> set[str]:
    """Return who approves ``head_sha``: their latest decisive review approves it.

    A later comment keeps an approval; a later change request or a dismissal
    replaces it. An approval of an earlier commit doesn't count.

    Parameters
    ----------
    reviews : Iterable[Mapping[str, Any]]
        The PR's reviews, oldest first (REST order).
    head_sha : str
        The PR's head commit.

    Returns
    -------
    set[str]
        Lowercased logins.
    """
    latest: dict[str, Mapping[str, Any]] = {}
    for review in reviews:
        login = ((review.get("user") or {}).get("login") or "").lower()
        if login and review.get("state") in _DECISIVE:
            latest[login] = review
    return {
        login
        for login, review in latest.items()
        if review.get("state") == "APPROVED" and review.get("commit_id") == head_sha
    }


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_DESCRIPTION else text[: _MAX_DESCRIPTION - 1] + "…"


def ownership_status(
    rules: Sequence[Rule],
    files: Sequence[str],
    author: str,
    approved: set[str],
    *,
    changed_files: int,
    renamed_from: Sequence[str] = (),
) -> tuple[str, str] | None:
    """Return a failing status when the PR needs an owner's approval, else None.

    Parameters
    ----------
    rules : Sequence[Rule]
        Parsed CODEOWNERS rules.
    files : Sequence[str]
        The PR's changed files as listed (one per file).
    author : str
        The PR author's login.
    approved : set[str]
        Lowercased logins approving the head (see ``approvers``).
    changed_files : int
        GitHub's count of changed files; more than ``files`` lists means the list
        was truncated (the files API stops at 3,000).
    renamed_from : Sequence[str], optional
        Old paths of renamed files: moving a file out of an owned path changes
        that path too.

    Returns
    -------
    tuple[str, str] or None
        ``("failure", description)``, or None when this half of the gate passes.
    """
    author = author.lower()
    approved = approved - {author}
    if changed_files > len(files):
        # An unlisted file could fall under any owned rule, so the author or an
        # approver must cover every one of them.
        covering = approved | {author}
        uncovered = sorted(
            {
                o
                for rule in rules
                if not covering & set(rule.owners)
                for o in rule.owners
            }
        )
        if uncovered:
            who = ", ".join(f"@{o}" for o in uncovered)
            return "failure", _clip(
                f"{changed_files} files changed, too many to check: needs {who}"
            )
    blocked: dict[str, tuple[str, ...]] = {}
    for path in [*files, *renamed_from]:
        owners = owners_for(rules, path)
        if owners and author not in owners and not approved & set(owners):
            blocked.setdefault(path, owners)
    if not blocked:
        return None
    who = " or ".join(
        f"@{o}" for o in sorted({o for os in blocked.values() for o in os})
    )
    count = len(blocked)
    noun = "file" if count == 1 else "files"
    return "failure", _clip(
        f"{who} must approve: {count} {noun} outside {author}'s paths, "
        f"e.g. {next(iter(blocked))}"
    )
