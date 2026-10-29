"""Tests for path ownership in the ``review-gate`` status (``path_owners.py``).

A CODEOWNERS file says who may merge a PR on the reviewer and CI alone: a PR passes
when its author owns every changed file (or the file has no owner); otherwise an
owner of each such file must approve the head. A line this gate can't resolve (a
team or email owner) or whose pattern syntax GitHub doesn't support is skipped,
never fatal. The script lives outside the importable package tree, so it is loaded
by path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPTS = (
    Path(__file__).resolve().parents[2] / ".agents" / "skills" / "resolve-pr-concerns"
) / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


path_owners = _load("path_owners")

HEAD = "0123456789abcdef0123456789abcdef01234567"
OLD = "fedcba9876543210fedcba9876543210fedcba98"

POLICY = """
# comment
* @Owner

/src/app/open/
/tests/
docs/*.md @docs-writer
"""


def _owners(text: str, path: str) -> tuple[str, ...] | None:
    result: tuple[str, ...] | None = path_owners.owners_for(
        path_owners.parse_codeowners(text), path
    )
    return result


# --- pattern matching (the CODEOWNERS subset GitHub documents) ---------------------


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("*", "anything/at/all.py", True),
        ("*.js", "a/b/c.js", True),
        ("*.js", "a/b/c.jsx", False),
        ("/build/logs/", "build/logs/x/y.log", True),
        ("/build/logs/", "other/build/logs/y.log", False),
        ("docs/", "a/docs/x.md", True),  # a trailing-slash-only pattern: anywhere
        ("docs/*", "docs/x.md", True),
        ("docs/*", "docs/sub/x.md", False),  # GitHub: `docs/*` does not recurse
        ("/apps/web", "apps/web/x.py", True),  # a directory matches its contents
        ("/apps/web", "apps/webber.py", False),
        ("apps/", "apps/x.py", True),
        ("**/logs", "a/b/logs/x.log", True),
        ("/src/**/api/", "src/a/b/api/x.py", True),
        ("/conf/settings.yaml", "conf/settings.yaml", True),
        ("/conf/settings.yaml", "conf/settings.yaml.bak", False),
    ],
)
def test_patterns_follow_github_codeowners_rules(
    pattern: str, path: str, matches: bool
) -> None:
    assert _owners(f"{pattern} @someone", path) == (("someone",) if matches else None)


def test_the_last_matching_line_wins() -> None:
    assert _owners(POLICY, "src/app/core.py") == ("owner",)
    assert _owners(POLICY, "src/app/open/agent.py") is None
    assert _owners(POLICY, "docs/readme.md") == ("docs-writer",)


def test_logins_are_case_insensitive() -> None:
    assert _owners("* @MixedCase", "x.py") == ("mixedcase",)


# --- lines this gate can't resolve: skipped, never fatal ---------------------------


@pytest.mark.parametrize(
    "owner", ["@owner/infra-team", "someone@example.com"], ids=["team", "email"]
)
def test_team_owner_lines_are_skipped_not_fatal(owner: str) -> None:
    """A team or email owner is left to GitHub's own code-owner rule."""
    rules = path_owners.parse_codeowners(f"* @octocat\n/src/app/infra/ {owner}\n")
    assert [r.skipped for r in rules] == [False, True]
    assert rules[1].owners == ()
    # The skipped line still wins for its paths, so they are ungated here...
    assert path_owners.owners_for(rules, "src/app/infra/main.tf") is None
    # ...and the earlier user-owned line still gates everything else.
    assert path_owners.owners_for(rules, "src/app/core.py") == ("octocat",)


def test_a_mixed_user_and_team_line_is_skipped() -> None:
    """Gating on the user alone would wrongly reject a team member's approval."""
    rules = path_owners.parse_codeowners(
        "* @hubot\n/src/app/api/ @octocat @owner/api-team\n"
    )
    assert rules[1].skipped and rules[1].owners == ()
    assert path_owners.owners_for(rules, "src/app/api/routes.py") is None
    status = path_owners.ownership_status(
        rules, ["src/app/api/routes.py"], "reviewer-a", set(), changed_files=1
    )
    assert status is None


@pytest.mark.parametrize(
    "pattern",
    ["!keep.py", "[ab].py", "src/a\\b.py"],
    ids=["negation", "character-range", "backslash"],
)
def test_bang_and_bracket_patterns_are_skipped(pattern: str) -> None:
    """Syntax GitHub doesn't support skips its line; the rest of the file still gates."""
    rules = path_owners.parse_codeowners(f"* @octocat\n{pattern} @hubot\n")
    assert [r.skipped for r in rules] == [False, True]
    assert rules[1].owners == ()
    # Matched as written, the skipped line still takes part in last-match-wins.
    assert path_owners.owners_for(rules, pattern) is None
    status = path_owners.ownership_status(
        rules, ["src/app/core.py", pattern], "reviewer-a", set(), changed_files=2
    )
    assert status is not None and "@hubot" not in status[1]


def test_an_ownerless_line_is_open_not_skipped() -> None:
    rules = path_owners.parse_codeowners("* @octocat\n/tests/\n")
    assert [r.skipped for r in rules] == [False, False]
    assert path_owners.owners_for(rules, "tests/test_x.py") is None


def test_an_unmatched_path_has_no_owner() -> None:
    assert _owners("/src/ @octocat", "README.md") is None


@pytest.mark.parametrize("text", ["", "\n\n", "# only a comment\n   # indented\n"])
def test_an_empty_codeowners_gates_nothing(text: str) -> None:
    rules = path_owners.parse_codeowners(text)
    assert rules == []
    status = path_owners.ownership_status(
        rules, ["src/app/core.py"], "reviewer-a", set(), changed_files=1
    )
    assert status is None
    # Even a PR too large to list needs nobody when nothing is owned.
    assert (
        path_owners.ownership_status(
            rules, ["src/app/core.py"], "reviewer-a", set(), changed_files=5000
        )
        is None
    )


def test_a_missing_file_gates_nothing(tmp_path: Path) -> None:
    assert path_owners.load_rules(tmp_path / "CODEOWNERS") == []


def test_codeowners_is_found_at_githubs_locations_in_order(tmp_path: Path) -> None:
    assert path_owners.find_codeowners(tmp_path) is None
    for location in reversed(path_owners.CODEOWNERS_LOCATIONS):
        (tmp_path / location).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / location).write_text("* @octocat\n")
        assert path_owners.find_codeowners(tmp_path) == tmp_path / location
    assert path_owners.CODEOWNERS_LOCATIONS == (
        ".github/CODEOWNERS",
        "CODEOWNERS",
        "docs/CODEOWNERS",
    )


# --- approvals ---------------------------------------------------------------------


def _review(user: str, state: str, commit: str = HEAD) -> dict[str, Any]:
    return {"user": {"login": user}, "state": state, "commit_id": commit}


def test_an_approval_of_the_head_counts() -> None:
    assert path_owners.approvers([_review("Owner", "APPROVED")], HEAD) == {"owner"}


def test_an_approval_of_an_older_commit_does_not_count() -> None:
    assert path_owners.approvers([_review("owner", "APPROVED", OLD)], HEAD) == set()


@pytest.mark.parametrize("later", ["CHANGES_REQUESTED", "DISMISSED"])
def test_a_later_decisive_review_replaces_the_approval(later: str) -> None:
    reviews = [_review("owner", "APPROVED"), _review("owner", later)]
    assert path_owners.approvers(reviews, HEAD) == set()


def test_a_later_comment_keeps_the_approval() -> None:
    reviews = [_review("owner", "APPROVED"), _review("owner", "COMMENTED")]
    assert path_owners.approvers(reviews, HEAD) == {"owner"}


# --- the gate ----------------------------------------------------------------------


def _gate(
    files: list[str],
    author: str,
    approved: set[str] | None = None,
    changed_files: int | None = None,
) -> tuple[str, str] | None:
    result: tuple[str, str] | None = path_owners.ownership_status(
        path_owners.parse_codeowners(POLICY),
        files,
        author,
        approved or set(),
        changed_files=len(files) if changed_files is None else changed_files,
    )
    return result


def test_the_owner_passes_anywhere() -> None:
    assert _gate(["src/app/core.py", "tests/test_x.py"], "Owner") is None


def test_anyone_passes_on_unowned_paths() -> None:
    assert _gate(["src/app/open/agent.py", "tests/test_x.py"], "reviewer-a") is None


def test_an_owned_file_needs_its_owner_to_approve() -> None:
    status = _gate(["src/app/open/agent.py", "src/app/capture.py"], "reviewer-a")
    assert status is not None
    state, description = status
    assert state == "failure"
    assert "@owner" in description and "src/app/capture.py" in description
    assert "1 file" in description


def test_the_owners_approval_of_the_head_satisfies_the_gate() -> None:
    assert _gate(["src/app/capture.py"], "reviewer-a", approved={"owner"}) is None


def test_another_owners_approval_covers_only_their_files() -> None:
    status = _gate(
        ["src/app/capture.py", "docs/a.md"], "reviewer-a", approved={"docs-writer"}
    )
    assert status is not None and "src/app/capture.py" in status[1]


def test_a_pr_too_large_to_list_needs_every_owner_covered() -> None:
    """The files API stops at 3,000 files; an unlisted file could be anyone's."""
    listed = ["tests/test_x.py"]
    status = _gate(listed, "reviewer-a", changed_files=3001)
    assert status is not None and "3001" in status[1]
    assert "@owner" in status[1] and "@docs-writer" in status[1]
    # One owner's approval can't clear a file another owner may hold.
    assert _gate(listed, "reviewer-a", {"owner"}, changed_files=3001) is not None
    assert _gate(listed, "owner", changed_files=3001) is not None
    assert _gate(listed, "owner", {"docs-writer"}, changed_files=3001) is None
    assert (
        _gate(listed, "reviewer-a", {"owner", "docs-writer"}, changed_files=3001)
        is None
    )


def test_moving_a_file_out_of_an_owned_path_changes_that_path() -> None:
    status = path_owners.ownership_status(
        path_owners.parse_codeowners(POLICY),
        ["src/app/open/capture.py"],
        "reviewer-a",
        set(),
        changed_files=1,
        renamed_from=["src/app/capture.py"],
    )
    assert status is not None and "src/app/capture.py" in status[1]


def test_the_description_fits_the_status_api() -> None:
    status = _gate([f"src/app/{'x' * 200}.py"], "reviewer-a")
    assert status is not None and len(status[1]) <= 140


# --- a typical single-maintainer policy --------------------------------------------
# One maintainer owns everything; ownerless lines open the trees anyone may merge;
# a later owned line takes back a file inside an open tree.

_MAINTAINER_POLICY = """
* @octocat

# Open to every contributor.
/src/app/analysis/
/tests/
/scripts/
/src/app/plugins/legacy/

# ...except what feeds shared code.
/src/app/plugins/*/offline.py @octocat
"""
_MAINTAINER_RULES = path_owners.parse_codeowners(_MAINTAINER_POLICY)


@pytest.mark.parametrize(
    "path",
    [
        "src/app/analysis/runner.py",
        "tests/test_anything.py",
        "scripts/build_data.py",
        "src/app/plugins/legacy/driver.py",
    ],
)
def test_the_policys_open_trees_are_open(path: str) -> None:
    assert path_owners.owners_for(_MAINTAINER_RULES, path) is None


@pytest.mark.parametrize(
    "path",
    [
        "src/app/config.py",
        "src/app/plugins/shared/base.py",
        "pyproject.toml",
        ".github/CODEOWNERS",
        ".github/workflows/review-gate.yml",
        # The review gate's own code: opening it would let a PR loosen the gate.
        ".agents/skills/resolve-pr-concerns/scripts/path_owners.py",
        ".agents/skills/resolve-pr-concerns/scripts/review_gate.py",
    ],
)
def test_everything_else_is_the_maintainers(path: str) -> None:
    assert path_owners.owners_for(_MAINTAINER_RULES, path) == ("octocat",)


def test_a_later_owned_line_takes_back_a_file_in_an_open_tree() -> None:
    assert path_owners.owners_for(
        _MAINTAINER_RULES, "src/app/plugins/legacy/offline.py"
    ) == ("octocat",)


def test_the_policy_names_no_other_owner() -> None:
    """A second owner would be auto-requested as a reviewer on every matching PR."""
    assert {o for rule in _MAINTAINER_RULES for o in rule.owners} == {"octocat"}
