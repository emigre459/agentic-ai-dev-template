#!/usr/bin/env python3
"""Daily-update report for the invoking GitHub user on the current repo.

Identifies the invoking user, runs a gh-permission preflight, reads a per-user
state file to pick the report window, fans out read-only ``gh`` queries in
parallel, classifies the results, and emits a markdown standup or a JSON blob.

The repo comes from ``gh repo view``. The project board, if any, comes from
``.agents/skill-config/daily-update.md`` (``project-owner`` and
``project-number``), else the repo's linked project, else there is no board.
Nothing here posts anywhere; Slack posting is the skill's job and only on
request. ``SKILL.md`` is the contract; this module is the engine.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

Gh = Callable[[list[str]], str]

CONFIG_RELPATH = Path(".agents") / "skill-config" / "daily-update.md"

ACTIVE_BOARD_STATUSES = {"In progress", "In review"}
NEXT_UP_PRIMARY_STATUS = "Ready"
NEXT_UP_FALLBACK_STATUS = "Backlog"

# Each capability is satisfied by ANY one of its listed OAuth scopes (more access
# is always fine). ``read:project`` is only required when a board is in play.
SCOPE_REQUIREMENTS: dict[str, set[str]] = {
    "repo": {"repo"},
    "read:project": {"read:project", "project"},
}

# Lower rank = earlier pick; unset ranks last.
_PRIORITY_RANK: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2}
_SIZE_RANK: dict[str, int] = {"XS": 0, "S": 1, "M": 2, "L": 3, "XL": 4}
_UNSET_PRIORITY_RANK = 3
_UNSET_SIZE_RANK = 5

_BOARD_MAX_ITEMS = 600  # safety cap on paging
_BOARD_GRAPHQL_QUERY = """
query($owner: String!, $number: Int!, $after: String) {
  repositoryOwner(login: $owner) {
    ... on Organization { projectV2(number: $number) { ...Board } }
    ... on User { projectV2(number: $number) { ...Board } }
  }
}
fragment Board on ProjectV2 {
  items(first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      fieldValues(first: 30) {
        nodes {
          __typename
          ... on ProjectV2ItemFieldSingleSelectValue {
            name
            field { ... on ProjectV2SingleSelectField { name } }
          }
        }
      }
      content {
        __typename
        ... on Issue {
          number title url updatedAt
          assignees(first: 20) { nodes { login } }
        }
        ... on PullRequest {
          number title url updatedAt
          assignees(first: 20) { nodes { login } }
        }
      }
    }
  }
}
"""

_LINKED_PROJECT_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    projectsV2(first: 1) {
      nodes { number owner { ... on Organization { login } ... on User { login } } }
    }
  }
}
"""


@dataclass
class Board:
    """A GitHub Projects v2 board: its owner login and project number."""

    owner: str
    number: int


@dataclass
class Config:
    """Settings read from ``.agents/skill-config/daily-update.md``.

    Attributes
    ----------
    board : Board | None
        The configured board, or ``None`` when the file does not name one.
    slack_channel : str | None
        Channel the skill may post to when the user asks it to post without naming
        one. Informational here; the script never posts.
    """

    board: Board | None = None
    slack_channel: str | None = None


@dataclass
class Window:
    """The reporting window for one invocation.

    Attributes
    ----------
    since : datetime
        Inclusive lower bound (UTC).
    until : datetime
        Upper bound, captured once so every query agrees on "now".
    is_first_run : bool
        True when no state file existed for this user and repo on this machine.
    """

    since: datetime
    until: datetime
    is_first_run: bool


@dataclass
class IssueLite:
    """Minimal issue or PR view used by the report composer."""

    number: int
    title: str
    url: str
    is_epic: bool = False
    assignees: list[str] = field(default_factory=list)
    parent_number: int | None = None
    status: str | None = None
    priority: str | None = None
    size: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class BranchActivity:
    """A pushed branch the user committed to in the window, with no PR yet."""

    name: str
    url: str
    commit_count: int
    latest_subject: str
    latest_committed_at: str


@dataclass
class Context:
    """Everything a query needs: the ``gh`` callable, the repo and the board."""

    gh: Gh
    owner: str
    repo: str
    board: Board | None = None

    @property
    def slug(self) -> str:
        """Return ``owner/repo``."""
        return f"{self.owner}/{self.repo}"


def make_gh(runner: Callable[..., Any] = subprocess.run) -> Gh:
    """Build a ``gh`` caller on top of ``runner`` (``subprocess.run`` by default).

    Parameters
    ----------
    runner : Callable[..., Any]
        Called as ``runner(["gh", *args], capture_output=True, text=True)``; must
        return an object with ``returncode``, ``stdout`` and ``stderr``.

    Returns
    -------
    Gh
        ``run_gh(args)`` returning stripped stdout.

    Raises
    ------
    RuntimeError
        From the returned callable, when ``gh`` is missing or exits non-zero. A
        status report fails loudly rather than degrading silently.
    """

    def run_gh(args: list[str]) -> str:
        try:
            proc = runner(["gh", *args], capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "`gh` CLI not found on PATH. Install via https://cli.github.com."
            ) from exc
        if proc.returncode != 0:
            raise RuntimeError(
                f"gh {' '.join(args)} failed (exit {proc.returncode}):\n"
                f"{(proc.stderr or '').strip()}"
            )
        return (proc.stdout or "").strip()

    return run_gh


def resolve_repo(gh: Gh) -> tuple[str, str]:
    """Return ``(owner, repo)`` of the current checkout from ``gh repo view``."""
    slug = gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
    owner, sep, repo = slug.partition("/")
    if not sep or not owner or not repo:
        raise RuntimeError(f"Could not resolve the repo from `gh repo view`: {slug!r}")
    return owner, repo


def find_config(start: Path) -> Path | None:
    """Return the nearest skill-config file at or above ``start``, else None."""
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_RELPATH
        if candidate.is_file():
            return candidate
    return None


def parse_config(text: str) -> Config:
    """Parse ``key: value`` lines (bullets and backticks tolerated) into a Config.

    Recognized keys: ``project-owner``, ``project-number``, ``slack-channel``.
    A board needs both project keys; a non-integer number is an error.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*").strip()
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip().strip("`").lower()] = value.strip().strip("`").strip()
    board = None
    owner, number = values.get("project-owner"), values.get("project-number")
    if owner and number:
        if not number.isdigit():
            raise RuntimeError(f"project-number must be an integer, got {number!r}")
        board = Board(owner=owner, number=int(number))
    channel = (values.get("slack-channel") or "").lstrip("#") or None
    return Config(board=board, slack_channel=channel)


def load_config(path: Path | None) -> Config:
    """Read the skill config at ``path``; an absent file means the defaults."""
    return parse_config(path.read_text()) if path else Config()


def discover_linked_board(gh: Gh, owner: str, repo: str) -> Board | None:
    """Return the repo's first linked Projects v2 board, or ``None``."""
    raw = gh(
        [
            "api",
            "graphql",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={repo}",
            "-f",
            f"query={_LINKED_PROJECT_QUERY}",
        ]
    )
    payload = json.loads(raw) if raw else {}
    nodes = (
        ((payload.get("data") or {}).get("repository") or {}).get("projectsV2") or {}
    ).get("nodes") or []
    for node in nodes:
        login = (node.get("owner") or {}).get("login")
        if node.get("number") and login:
            return Board(owner=login, number=int(node["number"]))
    return None


def identify_user(gh: Gh) -> tuple[str, str, str | None]:
    """Resolve the invoking login, display name and public email.

    The login is whoever ``gh api user`` says; the display name and email come
    from ``gh api users/<login>`` (no names are hard-coded).

    Returns
    -------
    tuple[str, str, str | None]
        ``(login, display_name, email)``. The display name falls back to the
        login. The email is ``None`` when private; it is only a fallback for
        attributing branch commits whose ``author.login`` is null.

    Raises
    ------
    RuntimeError
        When ``gh`` is not authenticated or returns no login.
    """
    login = gh(["api", "user", "--jq", ".login"]).strip()
    if not login:
        raise RuntimeError(
            "`gh api user` returned no login. Run `gh auth status` to diagnose."
        )
    raw = gh(["api", f"users/{login}"])
    profile = json.loads(raw) if raw else {}
    display = (profile.get("name") or "").strip() or login
    email = (profile.get("email") or "").strip() or None
    return login, display, email


def _extract_header(raw_response: str, name: str) -> str | None:
    """Return a header value from ``gh api -i`` output, ``None`` when absent."""
    wanted = name.lower()
    for line in raw_response.splitlines():
        if not line.strip():
            break
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == wanted:
            return value.strip()
    return None


def _missing_scopes(granted: set[str], need_board: bool) -> list[str]:
    """Return the required capabilities no granted scope satisfies."""
    return [
        label
        for label, satisfying in SCOPE_REQUIREMENTS.items()
        if (need_board or label != "read:project") and not (satisfying & granted)
    ]


def _remediation(granted: set[str], missing: list[str]) -> str:
    """Build the additive ``gh auth refresh`` remediation message."""
    lines = [
        "daily-update: the active gh token is missing required GitHub permissions.",
        f"  Missing: {', '.join(missing)}",
    ]
    if granted:
        lines.append(f"  Currently granted: {', '.join(sorted(granted))}")
    for scope in missing:
        lines.append(f"    gh auth refresh -h github.com -s {scope}")
    lines.append(
        "  (gh auth refresh is additive: it keeps your existing scopes. "
        "Fine-grained PATs: grant Projects: Read-only plus Contents/Metadata and "
        "Issues/Pull requests: Read.)"
    )
    return "\n".join(lines)


def _functional_probe(ctx: Context) -> tuple[bool, str]:
    """Check access by exercising the queries (fine-grained PAT / App tokens)."""
    try:
        ctx.gh(["api", f"repos/{ctx.slug}", "--jq", ".id"])
    except RuntimeError as exc:
        return False, _remediation(set(), ["repo"]) + f"\n  ({exc})"
    if ctx.board:
        try:
            ctx.gh(
                [
                    "api",
                    "graphql",
                    "-F",
                    f"owner={ctx.board.owner}",
                    "-F",
                    f"number={ctx.board.number}",
                    "-f",
                    (
                        "query=query($owner:String!,$number:Int!)"
                        "{repositoryOwner(login:$owner){login}}"
                    ),
                ]
            )
        except RuntimeError as exc:
            return False, _remediation(set(), ["read:project"]) + f"\n  ({exc})"
    return True, "gh permissions OK (verified by functional probe)."


def check_gh_permissions(ctx: Context) -> tuple[bool, str]:
    """Preflight: confirm the token can produce a full report.

    Classic OAuth scopes come from the ``X-OAuth-Scopes`` header; tokens that do
    not expose scopes get a functional probe. ``read:project`` is demanded only
    when a board is configured or linked.

    Returns
    -------
    tuple[bool, str]
        ``(ok, message)``; the message is remediation when ``ok`` is False.

    Raises
    ------
    RuntimeError
        When ``gh`` cannot reach the API at all (not authenticated, offline).
    """
    header = _extract_header(ctx.gh(["api", "-i", "user"]), "X-OAuth-Scopes")
    if not header:
        return _functional_probe(ctx)
    granted = {s.strip() for s in header.split(",") if s.strip()}
    missing = _missing_scopes(granted, need_board=ctx.board is not None)
    if missing:
        return False, _remediation(granted, missing)
    return True, "gh token has the required scopes: " + ", ".join(sorted(granted))


def state_path(env: Mapping[str, str], owner: str, repo: str, login: str) -> Path:
    """Return the per-user, per-repo state file path (not created here).

    Parameters
    ----------
    env : Mapping[str, str]
        Environment; ``XDG_STATE_HOME`` is honored when non-empty, else
        ``~/.local/state``.
    owner, repo : str
        The repo, so one user's windows on different repos do not collide.
    login : str
        GitHub login, so users sharing a machine do not collide.

    Returns
    -------
    Path
        ``<state home>/daily-update/<owner>-<repo>/<login>.json``.
    """
    xdg = env.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path(env.get("HOME") or Path.home()) / ".local/state"
    return base / "daily-update" / f"{owner}-{repo}" / f"{login}.json"


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (``Z`` accepted) into an aware UTC datetime."""
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def load_window(path: Path, *, override_since: str | None) -> Window:
    """Compute the window: ``--since``, else the state file, else the last 24h."""
    now = datetime.now(UTC)
    if override_since:
        return Window(since=_parse_iso(override_since), until=now, is_first_run=False)
    if path.exists():
        stored = json.loads(path.read_text())
        return Window(
            since=_parse_iso(stored["last_run_at"]), until=now, is_first_run=False
        )
    return Window(since=now - timedelta(hours=24), until=now, is_first_run=True)


def write_state(path: Path, when: datetime) -> None:
    """Persist ``when`` as the next window's start, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps({"last_run_at": stamp}, indent=2))


def fetch_merged_prs(ctx: Context, login: str, since: datetime) -> list[IssueLite]:
    """Return PRs the user merged in the window, newest first."""
    raw = ctx.gh(
        [
            "pr",
            "list",
            "--repo",
            ctx.slug,
            "--search",
            f"is:merged author:{login} merged:>={since:%Y-%m-%d}",
            "--state",
            "merged",
            "--limit",
            "50",
            "--json",
            "number,title,url,mergedAt",
        ]
    )
    items = json.loads(raw) if raw else []
    items.sort(key=lambda it: it.get("mergedAt", ""), reverse=True)
    return [
        IssueLite(number=i["number"], title=i["title"], url=i["url"]) for i in items
    ]


def fetch_open_prs(ctx: Context, login: str) -> list[IssueLite]:
    """Return the user's open PRs, with draft and review state in ``extra``."""
    raw = ctx.gh(
        [
            "pr",
            "list",
            "--repo",
            ctx.slug,
            "--author",
            login,
            "--state",
            "open",
            "--limit",
            "50",
            "--json",
            "number,title,url,isDraft,reviewDecision,headRefName",
        ]
    )
    return [
        IssueLite(
            number=it["number"],
            title=it["title"],
            url=it["url"],
            extra={
                "isDraft": it.get("isDraft", False),
                "reviewDecision": it.get("reviewDecision") or "",
                "headRefName": it.get("headRefName", ""),
            },
        )
        for it in (json.loads(raw) if raw else [])
    ]


def fetch_user_created_issues(
    ctx: Context, login: str, since: datetime
) -> list[IssueLite]:
    """Return open issues the user authored in the window, newest first.

    Each gets a follow-up REST call (N+1 by design; a user authors a handful per
    window) for ``sub_issues_summary`` and the native parent link, so "needs an
    owner" can group by epic.
    """
    raw = ctx.gh(
        [
            "issue",
            "list",
            "--repo",
            ctx.slug,
            "--author",
            login,
            "--state",
            "open",
            "--search",
            f"created:>={since:%Y-%m-%d}",
            "--limit",
            "100",
            "--json",
            "number,title,url,assignees,createdAt",
        ]
    )
    items = json.loads(raw) if raw else []
    items.sort(key=lambda it: it.get("createdAt", ""), reverse=True)
    out: list[IssueLite] = []
    for it in items:
        epic, parent = _fetch_epic_flags(ctx, it["number"])
        out.append(
            IssueLite(
                number=it["number"],
                title=it["title"],
                url=it["url"],
                is_epic=epic,
                assignees=[a.get("login", "") for a in it.get("assignees", [])],
                parent_number=parent,
            )
        )
    return out


def _fetch_epic_flags(ctx: Context, issue_number: int) -> tuple[bool, int | None]:
    """Return ``(is_epic, parent_number)``; an epic has at least one sub-issue."""
    raw = ctx.gh(
        [
            "api",
            f"repos/{ctx.slug}/issues/{issue_number}",
            "--jq",
            (
                "{sub: (.sub_issues_summary // {total:0}).total, "
                'parent_url: (.parent_issue_url // "")}'
            ),
        ]
    )
    if not raw:
        return False, None
    payload = json.loads(raw)
    sub_total = payload.get("sub") or 0
    return sub_total > 0, _parse_parent_number(payload.get("parent_url") or "")


def _parse_parent_number(parent_url: str) -> int | None:
    """Return the issue number at the end of a ``parent_issue_url``, else None."""
    tail = parent_url.rstrip("/").split("/")[-1]
    return int(tail) if tail.isdigit() else None


def fetch_board_items(ctx: Context, login: str) -> list[IssueLite]:
    """Return board items assigned to the user, with Status, Priority and Size.

    Uses Projects v2 GraphQL because ``gh project item-list`` omits the custom
    fields the "next up" rank needs. Draft cards have no link and are skipped.
    Returns an empty list when the context has no board.
    """
    board = ctx.board
    if board is None:
        return []
    nodes: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        args = [
            "api",
            "graphql",
            "-F",
            f"owner={board.owner}",
            "-F",
            f"number={board.number}",
            "-f",
            f"query={_BOARD_GRAPHQL_QUERY}",
        ]
        if cursor:
            args.extend(["-f", f"after={cursor}"])
        raw = ctx.gh(args)
        if not raw:
            break
        project = (
            (json.loads(raw).get("data") or {}).get("repositoryOwner") or {}
        ).get("projectV2") or {}
        page = project.get("items") or {}
        nodes.extend(page.get("nodes") or [])
        info = page.get("pageInfo") or {}
        cursor = info.get("endCursor")
        if not info.get("hasNextPage") or len(nodes) >= _BOARD_MAX_ITEMS or not cursor:
            break
    return [item for node in nodes if (item := _board_item(node, login))]


def _board_item(node: dict[str, Any], login: str) -> IssueLite | None:
    """Convert one board node to an IssueLite, or None when not the user's."""
    content = node.get("content") or {}
    if content.get("__typename") not in {"Issue", "PullRequest"}:
        return None
    assignees = [
        a["login"]
        for a in (content.get("assignees") or {}).get("nodes") or []
        if a.get("login")
    ]
    if login not in assignees or not content.get("number") or not content.get("url"):
        return None
    fields: dict[str, str | None] = {}
    for fv in (node.get("fieldValues") or {}).get("nodes") or []:
        if fv.get("__typename") == "ProjectV2ItemFieldSingleSelectValue":
            fields[(fv.get("field") or {}).get("name") or ""] = fv.get("name")
    return IssueLite(
        number=content["number"],
        title=content.get("title") or "",
        url=content["url"],
        status=fields.get("Status"),
        assignees=assignees,
        priority=fields.get("Priority"),
        size=fields.get("Size"),
        updated_at=content.get("updatedAt"),
    )


def fetch_active_branches(
    ctx: Context, login: str, since: datetime, *, user_email: str | None = None
) -> list[BranchActivity]:
    """Return pushed branches with the user's in-window commits and no PR yet.

    GitHub-API-only (no local git), so it behaves the same locally and in a
    scheduled cloud run. Only pushed commits are visible. Branches that have a PR
    in any state are excluded (open ones show as PR lines already), then each
    remaining branch is compared against the default branch.
    """
    default_branch = ctx.gh(["api", f"repos/{ctx.slug}", "--jq", ".default_branch"])
    if not default_branch:
        return []
    pr_raw = ctx.gh(
        [
            "pr",
            "list",
            "--repo",
            ctx.slug,
            "--state",
            "all",
            "--limit",
            "200",
            "--json",
            "headRefName",
        ]
    )
    pr_heads = {pr.get("headRefName") for pr in (json.loads(pr_raw) if pr_raw else [])}
    names = ctx.gh(
        ["api", f"repos/{ctx.slug}/branches", "--paginate", "--jq", ".[].name"]
    )
    candidates = [
        b for b in names.splitlines() if b and b != default_branch and b not in pr_heads
    ]
    out: list[BranchActivity] = []
    for branch in candidates:
        compare = ctx.gh(
            ["api", f"repos/{ctx.slug}/compare/{default_branch}...{branch}"]
        )
        commits = (json.loads(compare) if compare else {}).get("commits") or []
        mine = [c for c in commits if _commit_is_mine(c, login, user_email, since)]
        if not mine:
            continue
        newest = max(mine, key=_commit_date)
        out.append(
            BranchActivity(
                name=branch,
                url=f"https://github.com/{ctx.slug}/tree/{branch}",
                commit_count=len(mine),
                latest_subject=(newest["commit"]["message"].splitlines() or [""])[0],
                latest_committed_at=_commit_date(newest),
            )
        )
    out.sort(key=lambda b: b.latest_committed_at, reverse=True)
    return out


def _commit_date(commit: dict[str, Any]) -> str:
    """Return a compare-API commit's authoring date string (may be empty)."""
    return str(((commit.get("commit") or {}).get("author") or {}).get("date", ""))


def _commit_is_mine(
    commit: dict[str, Any], login: str, user_email: str | None, since: datetime
) -> bool:
    """Say whether the commit is the user's (login, else email) and in-window."""
    date = _commit_date(commit)
    if not date or _parse_iso(date) < since:
        return False
    author = commit.get("author")
    if author and author.get("login"):
        return bool(author["login"] == login)
    email = ((commit.get("commit") or {}).get("author") or {}).get("email")
    return bool(user_email) and email == user_email


def _rank_key(item: IssueLite) -> tuple[int, int, str]:
    """Sort key for "next up": Priority, then Size (small first), then stalest."""
    return (
        _PRIORITY_RANK.get(item.priority or "", _UNSET_PRIORITY_RANK),
        _SIZE_RANK.get(item.size or "", _UNSET_SIZE_RANK),
        item.updated_at or "",
    )


def compose_markdown(
    *,
    display_name: str,
    window: Window,
    merged: list[IssueLite],
    open_prs: list[IssueLite],
    created_issues: list[IssueLite],
    board: list[IssueLite],
    has_board: bool,
    branches: list[BranchActivity],
    next_override: str | None,
) -> str:
    """Render the standup as GitHub-flavored markdown.

    Slack's MCP converts GitHub markdown server-side, so one flavor serves both
    the terminal and a requested Slack post.

    Parameters
    ----------
    display_name : str
        Human-readable name of the invoking user.
    window : Window
        The reporting window.
    merged, open_prs, created_issues, board, branches
        The report buckets. ``board`` is the user's board items, all statuses.
    has_board : bool
        Whether a board is in play. Without one there are no board lines and no
        auto-suggested "Next up".
    next_override : str | None
        Verbatim "next up" text; replaces the suggestion.

    Returns
    -------
    str
        The report.
    """
    lines: list[str] = [
        f"# Daily update — {display_name} ({window.until:%Y-%m-%d})",
        (
            f"_Window: {window.since:%Y-%m-%d %H:%M} UTC → "
            f"{window.until:%Y-%m-%d %H:%M} UTC_"
        ),
        "",
    ]
    if window.is_first_run:
        lines += ["_First run on this machine — defaulted window to the last 24h._", ""]
    _section_what_i_did(lines, merged)
    _section_in_flight(lines, open_prs, board, branches)
    _section_next_up(lines, next_override, board, has_board)
    _section_needs_owner(lines, created_issues)
    return "\n".join(lines).rstrip() + "\n"


def _section_what_i_did(lines: list[str], merged: list[IssueLite]) -> None:
    """Append "What I did"; omitted when empty."""
    if not merged:
        return
    lines.append("## What I did")
    lines += [f"- Merged [#{pr.number}]({pr.url}): {pr.title}" for pr in merged]
    lines.append("")


def _section_in_flight(
    lines: list[str],
    open_prs: list[IssueLite],
    board: list[IssueLite],
    branches: list[BranchActivity],
) -> None:
    """Append "In flight": open PRs, active board items, branches without a PR.

    Never omitted: with all three empty it renders a "between tasks" line, since
    a standup that drops "what I'm working on" reads as broken and hides that
    these signals only see pushed work.
    """
    active = [b for b in board if b.status in ACTIVE_BOARD_STATUSES]
    lines.append("## In flight")
    if not open_prs and not active and not branches:
        lines += [
            (
                "I'm between tasks at the moment — no open PR, active board item, "
                "or pushed branch with commits since the last report."
            ),
            "",
        ]
        return
    for pr in open_prs:
        lines.append(f"- PR [#{pr.number}]({pr.url}): {pr.title} — {_pr_state(pr)}")
    for item in active:
        lines.append(
            f"- Board: [#{item.number}]({item.url}) {item.title} "
            f"— Status: {item.status}, Priority: {item.priority or '—'}"
        )
    for branch in branches:
        noun = "commit" if branch.commit_count == 1 else "commits"
        lines.append(
            f"- Branch `{branch.name}` ({branch.commit_count} {noun} since "
            f'window start, latest: "{_truncate(branch.latest_subject, 72)}") '
            "— no PR yet"
        )
    lines.append("")


def _truncate(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, ending with an ellipsis if cut."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _pr_state(pr: IssueLite) -> str:
    """Return ``draft``, ``approved``, ``changes requested`` or ``review pending``."""
    if pr.extra.get("isDraft"):
        return "draft"
    decision = (pr.extra.get("reviewDecision") or "").upper()
    return {"APPROVED": "approved", "CHANGES_REQUESTED": "changes requested"}.get(
        decision, "review pending"
    )


def _section_next_up(
    lines: list[str],
    next_override: str | None,
    board: list[IssueLite],
    has_board: bool,
) -> None:
    """Append "Next up": the override, else a board suggestion, else nothing.

    Without a board and without an override there is nothing honest to suggest,
    so the section is omitted.
    """
    if next_override:
        lines += ["## Next up", f"Per your input: {next_override}", ""]
        return
    if not has_board:
        return
    ready = sorted(
        (b for b in board if b.status == NEXT_UP_PRIMARY_STATUS), key=_rank_key
    )
    backlog = sorted(
        (b for b in board if b.status == NEXT_UP_FALLBACK_STATUS), key=_rank_key
    )
    pick = ready[0] if ready else (backlog[0] if backlog else None)
    lines.append("## Next up")
    if pick is None:
        lines += [
            (
                "Nothing assigned to you with Status: Ready or Backlog on the board. "
                "Pick a fresh card or plan some issues."
            ),
            "",
        ]
        return
    why = (
        f"it's assigned to you with Status: {pick.status}."
        if pick.status == NEXT_UP_PRIMARY_STATUS
        else f"nothing in Ready right now — this is your next Backlog item "
        f"(Status: {pick.status})."
    )
    lines += [
        (
            f"The next natural thing for you to work on is "
            f"**[#{pick.number}]({pick.url}) {pick.title}** — {why}"
        ),
        "",
    ]


def _section_needs_owner(lines: list[str], created_issues: list[IssueLite]) -> None:
    """Append "Needs an owner": the user's new unassigned issues, grouped by epic.

    New unowned epics head their unowned children; children of an existing epic
    group under it; parentless unowned issues are flagged as orphans.
    """
    unowned = [i for i in created_issues if not i.assignees]
    if not unowned:
        return
    epics = {i.number: i for i in unowned if i.is_epic}
    by_parent: dict[int | None, list[IssueLite]] = {}
    for iss in (i for i in unowned if not i.is_epic):
        by_parent.setdefault(iss.parent_number, []).append(iss)
    lines.append("## Needs an owner (you created these, no assignee yet)")
    for epic in epics.values():
        lines.append(
            f"- New epic you created: **[#{epic.number}]({epic.url}) {epic.title}** "
            "— no assignee yet. Worth a quick discussion to confirm scope."
        )
        lines += [
            f"  - [#{c.number}]({c.url}) {c.title}"
            for c in by_parent.pop(epic.number, [])
        ]
    for parent, children in by_parent.items():
        if parent is None:
            lines += [
                f"- [#{c.number}]({c.url}) {c.title} — orphan (no parent epic)."
                for c in children
            ]
        else:
            lines.append(f"- Sits under existing epic #{parent}:")
            lines += [f"  - [#{c.number}]({c.url}) {c.title}" for c in children]
    lines.append("")


def _issue_to_dict(issue: IssueLite) -> dict[str, Any]:
    """Flatten an IssueLite for JSON output."""
    return {
        "number": issue.number,
        "title": issue.title,
        "url": issue.url,
        "is_epic": issue.is_epic,
        "assignees": issue.assignees,
        "parent_number": issue.parent_number,
        "status": issue.status,
        "priority": issue.priority,
        "size": issue.size,
        "updated_at": issue.updated_at,
        "extra": issue.extra,
    }


def to_json_payload(
    *,
    login: str,
    display_name: str,
    repo: str,
    board_ref: Board | None,
    window: Window,
    merged: list[IssueLite],
    open_prs: list[IssueLite],
    created_issues: list[IssueLite],
    board: list[IssueLite],
    branches: list[BranchActivity],
    next_override: str | None,
) -> dict[str, Any]:
    """Serialize the whole report as a dict for ``--json``."""
    return {
        "login": login,
        "display_name": display_name,
        "repo": repo,
        "board": {"owner": board_ref.owner, "number": board_ref.number}
        if board_ref
        else None,
        "window": {
            "since": window.since.isoformat(),
            "until": window.until.isoformat(),
            "is_first_run": window.is_first_run,
        },
        "merged_prs": [_issue_to_dict(i) for i in merged],
        "open_prs": [_issue_to_dict(i) for i in open_prs],
        "created_issues": [_issue_to_dict(i) for i in created_issues],
        "board_items": [_issue_to_dict(i) for i in board],
        "branch_activity": [vars(b) for b in branches],
        "next_override": next_override,
    }


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse the CLI surface documented in ``SKILL.md``."""
    parser = argparse.ArgumentParser(
        description="Per-user daily update for the current repo."
    )
    parser.add_argument("--next", dest="next_override", default=None)
    parser.add_argument("--since", default=None, help="ISO-8601; beats the state file.")
    parser.add_argument(
        "--no-write-state", action="store_true", help="Do not persist last_run_at."
    )
    parser.add_argument(
        "--write-state-only",
        action="store_true",
        help="Skip the report; just bump the state file to now.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON, not markdown.")
    parser.add_argument(
        "--check-permissions",
        action="store_true",
        help="Run only the gh-permission preflight and exit 0/1.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Skill config file (default: nearest {CONFIG_RELPATH}).",
    )
    return parser.parse_args(list(argv))


def _resolve_board(gh: Gh, owner: str, repo: str, config: Config) -> Board | None:
    """Pick the board: skill-config, else the repo's linked project, else none.

    Only "the repo has no linked project" means no board. A failed read of the
    linked project (most often a token without ``read:project``) is a hard
    error, so a board-less report never passes for a full one.

    Raises
    ------
    RuntimeError
        When the linked-project query fails; the message names the cause and the
        remedy.
    """
    if config.board:
        return config.board
    try:
        return discover_linked_board(gh, owner, repo)
    except RuntimeError as exc:
        raise RuntimeError(
            f"could not read the repo's linked project ({exc}). If the token "
            "lacks project access, run `gh auth refresh -s read:project`; "
            "otherwise fix the error above and retry."
        ) from exc


def main(
    argv: list[str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    """Drive the report end to end.

    Parameters
    ----------
    argv : list[str] | None
        CLI arguments; defaults to ``sys.argv[1:]``.
    runner : Callable[..., Any]
        ``subprocess.run`` stand-in; tests pass a ``gh`` stub.

    Returns
    -------
    int
        0 on success; 1 on a hard failure, with the reason on stderr.
    """
    args = parse_args(argv if argv is not None else sys.argv[1:])
    gh = make_gh(runner)
    try:
        owner, repo = resolve_repo(gh)
        config = load_config(args.config or find_config(Path.cwd()))
        board_ref = _resolve_board(gh, owner, repo, config)
        ctx = Context(gh=gh, owner=owner, repo=repo, board=board_ref)
        if args.check_permissions:
            ok, message = check_gh_permissions(ctx)
            print(message)
            return 0 if ok else 1
        login, display, user_email = identify_user(gh)
        path = state_path(os.environ, owner, repo, login)
        if args.write_state_only:
            write_state(path, datetime.now(UTC))
            return 0
        ok, message = check_gh_permissions(ctx)
        if not ok:
            print(f"daily-update aborted:\n{message}", file=sys.stderr)
            return 1
        window = load_window(path, override_since=args.since)
        with ThreadPoolExecutor(max_workers=5) as pool:
            merged_f = pool.submit(fetch_merged_prs, ctx, login, window.since)
            open_f = pool.submit(fetch_open_prs, ctx, login)
            issues_f = pool.submit(fetch_user_created_issues, ctx, login, window.since)
            board_f = pool.submit(fetch_board_items, ctx, login)
            branches_f = pool.submit(
                fetch_active_branches, ctx, login, window.since, user_email=user_email
            )
            merged, open_prs = merged_f.result(), open_f.result()
            created, board = issues_f.result(), board_f.result()
            branches = branches_f.result()
        if args.json:
            payload = to_json_payload(
                login=login,
                display_name=display,
                repo=ctx.slug,
                board_ref=board_ref,
                window=window,
                merged=merged,
                open_prs=open_prs,
                created_issues=created,
                board=board,
                branches=branches,
                next_override=args.next_override,
            )
            print(json.dumps(payload, indent=2, default=str))
        else:
            print(
                compose_markdown(
                    display_name=display,
                    window=window,
                    merged=merged,
                    open_prs=open_prs,
                    created_issues=created,
                    board=board,
                    has_board=board_ref is not None,
                    branches=branches,
                    next_override=args.next_override,
                ),
                end="",
            )
        if not args.no_write_state:
            write_state(path, window.until)
        return 0
    except RuntimeError as exc:
        print(f"daily-update failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
