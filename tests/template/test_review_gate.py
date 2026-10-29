"""Tests for the vendor-neutral ``review-gate`` commit-status script.

``review_gate.py`` lives beside ``reviewer_state.py`` outside the importable package
tree, so all three are loaded by path (``reviewer_state`` and ``path_owners`` first,
since ``review_gate`` imports them by name). ``gate_status`` is a pure function over
``collect_state`` output; posting is tested with ``_gh`` stubbed out. The CODEOWNERS
half reads the default branch's checkout, so tests point ``REPO_ROOT`` at a
``tmp_path`` holding (or lacking) a CODEOWNERS file.
"""

from __future__ import annotations

import importlib.util
import json
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


reviewer_state = _load("reviewer_state")
path_owners = _load("path_owners")
review_gate = _load("review_gate")
gate_status = review_gate.gate_status

REPO = "owner/repo"


def _state(**over: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "head_sha": "0123456789abcdef0123456789abcdef01234567",
        "verdict": "pass",
        "reviewer_name": "CodeRabbit",
        "trigger_comment": "@coderabbitai review",
        "final_review_outcome": "finished",
        "final_review_comment": "@coderabbitai full review",
    }
    state.update(over)
    return state


@pytest.mark.parametrize("verdict", ["pass", "findings"])
@pytest.mark.parametrize("final", ["finished", "not_required"])
def test_reviewed_head_with_final_review_passes(verdict: str, final: str) -> None:
    status = gate_status(_state(verdict=verdict, final_review_outcome=final))
    assert status[0] == "success"


def test_unreviewed_head_fails_naming_the_trigger() -> None:
    state, description = gate_status(_state(verdict="none"))
    assert state == "failure"
    assert "0123456" in description
    assert "`@coderabbitai review`" in description


def test_a_running_review_is_pending_not_failed() -> None:
    """The reviewer is reviewing this head; its reply edit re-runs the gate."""
    state, description = gate_status(_state(verdict="none", review_running=True))
    assert state == "pending"
    assert description == "CodeRabbit is reviewing 0123456"


@pytest.mark.parametrize("outcome", ["failed", "not_completed"])
def test_a_failed_review_request_says_so(outcome: str) -> None:
    state, description = gate_status(
        _state(
            verdict="none",
            incremental_review_outcome=outcome,
            incremental_review_on_head=True,
        )
    )
    assert state == "failure"
    assert "last review request did not complete" in description
    assert "`@coderabbitai review`" in description and "0123456" in description


@pytest.mark.parametrize("outcome", ["failed", "not_completed"])
def test_a_failed_request_on_an_older_commit_asks_for_a_review_of_the_head(
    outcome: str,
) -> None:
    """A request that failed before the latest push says nothing about the head."""
    state, description = gate_status(
        _state(
            verdict="none",
            incremental_review_outcome=outcome,
            incremental_review_on_head=False,
        )
    )
    assert state == "failure"
    assert "did not complete" not in description
    assert (
        description
        == "CodeRabbit has not reviewed 0123456: post `@coderabbitai review`"
    )


def test_a_verdict_wins_over_a_later_failed_request() -> None:
    status = gate_status(_state(verdict="pass", incremental_review_outcome="failed"))
    assert status[0] == "success"


def test_missing_final_review_fails_naming_the_command() -> None:
    state, description = gate_status(_state(final_review_outcome="not_requested"))
    assert state == "failure"
    assert "`@coderabbitai full review`" in description


def test_running_final_review_is_pending() -> None:
    assert gate_status(_state(final_review_outcome="pending"))[0] == "pending"


@pytest.mark.parametrize("final", ["failed", "surprise"])
def test_failed_or_unknown_final_review_fails(final: str) -> None:
    assert gate_status(_state(final_review_outcome=final))[0] == "failure"


def test_description_is_clipped_to_the_api_limit() -> None:
    _, description = gate_status(_state(verdict="none", reviewer_name="R" * 300))
    assert len(description) <= 140


def test_status_posts_on_the_sha_the_state_was_computed_for(monkeypatch: Any) -> None:
    sha = "feedface" * 5
    posted: list[list[str]] = []
    monkeypatch.setattr(
        review_gate.reviewer_state,
        "collect_state",
        lambda repo, pr, adapter: _state(head_sha=sha),
    )
    monkeypatch.setattr(review_gate, "_gh", posted.append)
    monkeypatch.setattr(review_gate, "ownership_gate_status", lambda r, p, h: None)
    current = iter([None, gate_status(_state(head_sha=sha))])
    monkeypatch.setattr(review_gate, "_current_gate_status", lambda r, h: next(current))
    review_gate.post_for_pr(REPO, 7, reviewer_state.load_reviewer(key="coderabbit"))
    (args,) = posted
    assert f"repos/{REPO}/statuses/{sha}" in args
    assert "context=review-gate" in args
    assert "state=success" in args


def test_status_links_the_workflow_run_when_there_is_one(monkeypatch: Any) -> None:
    posted: list[list[str]] = []
    monkeypatch.setattr(review_gate, "_gh", posted.append)
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    review_gate.post_for_merge_group(REPO, "a" * 40)
    (args,) = posted
    assert f"target_url=https://github.com/{REPO}/actions/runs/42" in args


def test_merge_group_passes_through(monkeypatch: Any) -> None:
    posted: list[list[str]] = []
    monkeypatch.setattr(review_gate, "_gh", posted.append)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    review_gate.post_for_merge_group(REPO, "b" * 40)
    (args,) = posted
    assert f"repos/{REPO}/statuses/{'b' * 40}" in args
    assert "state=success" in args and "context=review-gate" in args


# Runs are never cancelled (a cancelled run puts a red X on every PR and on the default
# branch). Instead a run posts only when the head's status differs from what it
# computed, then re-reads and repeats until they match, so a stale overwrite by an
# overlapping run is repaired by that same run.


class _Head:
    """A fake head: its newest review-gate status plus a scripted state sequence."""

    def __init__(self, states: list[dict[str, Any]], current: Any = None) -> None:
        self.states = iter(states)
        self.last: dict[str, Any] = states[0]
        self.current = current
        self.posts: list[tuple[str, str]] = []

    def collect(self, repo: str, pr: int, adapter: Any) -> dict[str, Any]:
        self.last = next(self.states, self.last)
        return self.last

    def post(self, repo: str, sha: str, state: str, description: str) -> None:
        self.posts.append((state, description))
        self.current = (state, description)


def _run(monkeypatch: Any, head: _Head, owners: Any = None) -> _Head:
    monkeypatch.setattr(review_gate.reviewer_state, "collect_state", head.collect)
    monkeypatch.setattr(review_gate, "ownership_gate_status", lambda r, p, h: owners)
    monkeypatch.setattr(review_gate, "_post", head.post)
    monkeypatch.setattr(
        review_gate, "_current_gate_status", lambda repo, sha: head.current
    )
    review_gate.post_for_pr(REPO, 7, reviewer_state.load_reviewer(key="coderabbit"))
    return head


def test_an_unchanged_status_is_not_posted_again(monkeypatch: Any) -> None:
    """The sweep must not re-post (GitHub caps statuses at 1,000 per SHA+context)."""
    current = gate_status(_state())
    assert _run(monkeypatch, _Head([_state()], current=current)).posts == []


def test_a_changed_status_is_posted_once(monkeypatch: Any) -> None:
    head = _run(monkeypatch, _Head([_state()], current=None))
    assert head.posts == [gate_status(_state())]


def test_a_stale_overwrite_is_repaired_by_the_same_run(monkeypatch: Any) -> None:
    """A read the old verdict, B already posted the new one; A must end on the new one."""
    old, new = _state(verdict="none"), _state()
    head = _run(monkeypatch, _Head([old, new], current=gate_status(new)))
    assert head.posts == [gate_status(old), gate_status(new)]
    assert head.current == gate_status(new)


def test_a_state_that_never_settles_stops_after_a_few_posts(monkeypatch: Any) -> None:
    flipping = [_state(verdict="none"), _state()] * 10
    head = _run(monkeypatch, _Head(flipping, current=None))
    assert len(head.posts) == review_gate._MAX_POSTS


OWNER_NEEDED = (
    "failure",
    "@octocat must approve: 1 file outside reviewer-a's paths, e.g. x.py",
)
OWNERS_CLEAR = ("success", review_gate.OWNERSHIP_CLEAR)


@pytest.mark.parametrize("verdict", ["pass", "none"])
def test_a_missing_owner_approval_fails_the_gate_whatever_the_review(
    monkeypatch: Any, verdict: str
) -> None:
    head = _run(monkeypatch, _Head([_state(verdict=verdict)]), owners=OWNER_NEEDED)
    assert head.posts == [OWNER_NEEDED]


@pytest.mark.parametrize("owners", [None, OWNERS_CLEAR], ids=["no-file", "clear"])
def test_with_no_owner_approval_missing_the_reviewer_decides(
    monkeypatch: Any, owners: Any
) -> None:
    """No CODEOWNERS leaves the status exactly as it was before the ownership half."""
    head = _run(monkeypatch, _Head([_state(verdict="none")]), owners=owners)
    assert head.posts == [gate_status(_state(verdict="none"))]


# --- the CODEOWNERS half, read from the default branch's checkout ---------------

CODEOWNERS = """
* @octocat
/src/app/analysis/
"""


def _codeowners(monkeypatch: Any, root: Path, text: str | None) -> None:
    """Point the reader at ``root``; write ``text`` as its CODEOWNERS unless None."""
    if text is not None:
        (root / ".github").mkdir(exist_ok=True)
        (root / ".github" / "CODEOWNERS").write_text(text)
    monkeypatch.setattr(review_gate, "REPO_ROOT", root)


def _inputs(**over: Any) -> Any:
    fields: dict[str, Any] = {
        "author": "reviewer-a",
        "changed_files": 2,
        "files": ["src/app/analysis/x.py", "src/app/api/y.py"],
        "renamed_from": [],
        "reviews": [],
        "head_sha": "f" * 40,
    }
    fields.update(over)
    return review_gate.OwnershipInputs(**fields)


def _owner_gate(monkeypatch: Any, inputs: Any) -> Any:
    monkeypatch.setattr(review_gate, "_ownership_inputs", lambda repo, pr: inputs)
    return review_gate.ownership_gate_status(REPO, 7, _state()["head_sha"])


def test_the_codeowners_gates_a_non_owners_change(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _codeowners(monkeypatch, tmp_path, CODEOWNERS)
    status = _owner_gate(monkeypatch, _inputs())
    assert status is not None and status[0] == "failure"
    assert "@octocat" in status[1] and "src/app/api/y.py" in status[1]


def test_the_owners_head_approval_clears_it(monkeypatch: Any, tmp_path: Path) -> None:
    _codeowners(monkeypatch, tmp_path, CODEOWNERS)
    approval = {
        "user": {"login": "octocat"},
        "state": "APPROVED",
        "commit_id": _state()["head_sha"],
    }
    assert _owner_gate(monkeypatch, _inputs(reviews=[approval])) == OWNERS_CLEAR


def test_moving_a_file_out_of_an_owned_path_needs_the_owner(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _codeowners(monkeypatch, tmp_path, CODEOWNERS)
    moved = _inputs(
        changed_files=1,
        files=["src/app/analysis/y.py"],
        renamed_from=["src/app/api/y.py"],
    )
    status = _owner_gate(monkeypatch, moved)
    assert status is not None and status[0] == "failure"


def test_no_codeowners_on_the_default_branch_gates_nothing(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _codeowners(monkeypatch, tmp_path, None)

    def _never(repo: str, pr: int) -> Any:
        raise AssertionError("no CODEOWNERS: the PR must not be read")

    monkeypatch.setattr(review_gate, "_ownership_inputs", _never)
    assert review_gate.ownership_gate_status(REPO, 7, _state()["head_sha"]) is None


NO_USER_OWNER = pytest.mark.parametrize(
    "text",
    [
        "",
        "# owners go here\n",
        "* @owner/all-devs\n/docs/ docs-team@example.com\n",
        "[ab].py @octocat\n!keep.py @octocat\n",
    ],
    ids=["empty", "comments", "teams-and-emails", "unsupported-syntax"],
)


@NO_USER_OWNER
def test_an_empty_codeowners_gates_nothing(
    monkeypatch: Any, tmp_path: Path, text: str
) -> None:
    """A file naming no user owner counts as no file: the PR isn't even read."""
    _codeowners(monkeypatch, tmp_path, text)

    def _never(repo: str, pr: int) -> Any:
        raise AssertionError("no enforceable owner: the PR must not be read")

    monkeypatch.setattr(review_gate, "_ownership_inputs", _never)
    assert review_gate.ownership_gate_status(REPO, 7, _state()["head_sha"]) is None


def test_a_team_owned_codeowners_line_never_crashes_the_gate(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A team-owned path is left to GitHub's own code-owner rule."""
    _codeowners(monkeypatch, tmp_path, "* @octocat\n/src/app/api/ @owner/api-team\n")
    inputs = _inputs(files=["src/app/api/y.py"], changed_files=1)
    assert _owner_gate(monkeypatch, inputs) == OWNERS_CLEAR


@pytest.mark.parametrize("location", ["CODEOWNERS", "docs/CODEOWNERS"])
def test_codeowners_at_any_github_location_turns_the_gate_on(
    monkeypatch: Any, tmp_path: Path, location: str
) -> None:
    (tmp_path / location).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / location).write_text(CODEOWNERS)
    _codeowners(monkeypatch, tmp_path, None)
    status = _owner_gate(monkeypatch, _inputs())
    assert status is not None and status[0] == "failure"


def test_ownership_inputs_read_author_files_renames_and_reviews(
    monkeypatch: Any,
) -> None:
    answers = {
        f"repos/{REPO}/pulls/7": {
            "user": {"login": "reviewer-a"},
            "changed_files": 2,
            "head": {"sha": "c" * 40},
        },
        f"repos/{REPO}/pulls/7/files?per_page=100": [
            {"filename": "a.py"},
            {"filename": "b.py", "previous_filename": "old/b.py"},
        ],
        f"repos/{REPO}/pulls/7/reviews?per_page=100": [{"state": "APPROVED"}],
    }
    calls: list[list[str]] = []

    def _json(args: list[str]) -> Any:
        calls.append(args)
        return answers[args[-1]]

    monkeypatch.setattr(review_gate.reviewer_state, "_gh_json", _json)
    got = review_gate._ownership_inputs(REPO, 7)
    assert got == review_gate.OwnershipInputs(
        "reviewer-a",
        2,
        ["a.py", "b.py"],
        ["old/b.py"],
        [{"state": "APPROVED"}],
        "c" * 40,
    )
    assert all("--paginate" in c for c in calls if c[-1] != f"repos/{REPO}/pulls/7")


def test_the_sweep_posts_every_open_pr_and_isolates_failures(monkeypatch: Any) -> None:
    done: list[int] = []

    def _post_for_pr(repo: str, pr: int, adapter: Any) -> None:
        if pr == 2:
            raise RuntimeError("gh failed")
        done.append(pr)

    monkeypatch.setattr(review_gate, "_open_prs", lambda repo: [1, 2, 3])
    monkeypatch.setattr(review_gate, "post_for_pr", _post_for_pr)
    with pytest.raises(SystemExit, match="1 of 3"):
        review_gate.post_for_all_open_prs(
            REPO, reviewer_state.load_reviewer(key="bugbot")
        )
    assert done == [1, 3]


def test_the_current_status_is_the_newest_for_the_context(monkeypatch: Any) -> None:
    listed = (
        '[{"context": "ci", "state": "success", "description": "x"},'
        ' {"context": "review-gate", "state": "pending", "description": "newest"},'
        ' {"context": "review-gate", "state": "failure", "description": "older"}]'
    )
    monkeypatch.setattr(review_gate, "_gh_output", lambda args: listed)
    assert review_gate._current_gate_status(REPO, "a" * 40) == ("pending", "newest")


def test_the_sweep_lists_every_open_pr_without_a_cap(monkeypatch: Any) -> None:
    """A fixed `--limit` would silently skip PRs past it."""
    calls: list[list[str]] = []

    def _out(args: list[str]) -> str:
        calls.append(args)
        return "11\n12\n13\n"

    monkeypatch.setattr(review_gate, "_gh_output", _out)
    assert review_gate._open_prs(REPO) == [11, 12, 13]
    (args,) = calls
    assert "--paginate" in args and not any(a.startswith("--limit") for a in args)


CARRIED = "fedcba9876543210fedcba9876543210fedcba98"


@pytest.mark.parametrize(
    ("reason", "why"), [("same_patch", "same diff"), ("no_changes", "nothing new")]
)
def test_a_carried_verdict_passes(reason: str, why: str) -> None:
    """A rebase-only head keeps the verdict of the commit it matches, naming it."""
    state, description = gate_status(
        _state(verdict_carried_from=CARRIED, verdict_carry_reason=reason)
    )
    assert state == "success"
    assert "fedcba9" in description and "0123456" in description
    assert why in description
    assert len(description) <= 140


def test_a_carried_verdict_still_needs_the_final_review() -> None:
    state, _ = gate_status(
        _state(
            verdict_carried_from=CARRIED,
            verdict_carry_reason="same_patch",
            final_review_outcome="not_requested",
        )
    )
    assert state == "failure"


# --- A locally reviewed PR is gated on its one final full review ---------------


def _local(**over: Any) -> dict[str, Any]:
    """Return the state of a PR whose head moved after its final full review."""
    state = _state(
        verdict="none",
        local_review={"configured": True, "reviewed_head": True},
        open_findings=[],
    )
    state.update(over)
    return state


def test_a_finished_full_review_with_every_thread_resolved_passes() -> None:
    state, description = gate_status(_local())
    assert state == "success"
    assert "full review finished" in description
    assert "local review on 0123456" in description


def test_a_head_with_no_local_review_still_passes_but_says_so() -> None:
    state, description = gate_status(
        _local(local_review={"configured": True, "reviewed_head": False})
    )
    assert state == "success"
    assert "no local review on 0123456" in description


def test_an_open_reviewer_thread_holds_the_gate() -> None:
    state, description = gate_status(_local(open_findings=[{"path": "a.py"}] * 2))
    assert state == "failure"
    assert "2 CodeRabbit thread(s) still open" in description


def test_a_locally_reviewed_pr_is_asked_for_the_full_review_not_a_trigger() -> None:
    state, description = gate_status(_local(final_review_outcome="not_requested"))
    assert state == "failure"
    assert "`@coderabbitai full review`" in description
    assert "`@coderabbitai review`" not in description


def test_a_running_full_review_on_a_locally_reviewed_pr_is_pending() -> None:
    assert gate_status(_local(final_review_outcome="pending"))[0] == "pending"


@pytest.mark.parametrize(
    ("final", "text"), [("failed", "wait 20+ min"), ("surprise", "unrecognized")]
)
def test_a_failed_full_review_on_a_locally_reviewed_pr_fails(
    final: str, text: str
) -> None:
    state, description = gate_status(_local(final_review_outcome=final))
    assert state == "failure"
    assert text in description


def test_a_reviewer_without_a_local_review_still_needs_a_verdict_on_the_head() -> None:
    state, _ = gate_status(_state(verdict="none", local_review={"configured": False}))
    assert state == "failure"


def _main_without_reviewer(
    tmp_path: Path, monkeypatch: Any, target: list[str], codeowners: str | None
) -> list[tuple[str, str, str]]:
    """Run ``main`` with ``"active": null``; return the (sha, state, text) it posts."""
    config = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    config["active"] = None
    path = tmp_path / "pr-reviewer.json"
    path.write_text(json.dumps(config))
    root = tmp_path / "checkout"
    root.mkdir()
    _codeowners(monkeypatch, root, codeowners)
    posted: list[tuple[str, str, str]] = []
    current: dict[str, tuple[str, str]] = {}

    def _post(repo: str, sha: str, state: str, description: str) -> None:
        posted.append((sha, state, description))
        current[sha] = (state, description)

    def _no_reviewer_state(repo: str, pr: int, adapter: Any) -> Any:
        raise AssertionError("no reviewer: its state must not be read")

    monkeypatch.setattr(review_gate, "_post", _post)
    monkeypatch.setattr(
        review_gate, "_current_gate_status", lambda r, h: current.get(h)
    )
    monkeypatch.setattr(review_gate, "_open_prs", lambda repo: [7])
    monkeypatch.setattr(review_gate.reviewer_state, "collect_state", _no_reviewer_state)
    monkeypatch.setattr(
        sys, "argv", ["review_gate.py", "--repo", REPO, *target, "--config", str(path)]
    )
    review_gate.main()
    return posted


TARGETS = pytest.mark.parametrize("target", [["--pr", "7"], ["--all-open"]])


@TARGETS
def test_no_active_reviewer_posts_no_status(
    tmp_path: Path, monkeypatch: Any, capsys: Any, target: list[str]
) -> None:
    """With no reviewer and no CODEOWNERS there is nothing to gate: post nothing.

    That keeps the workflow green instead of failing on every PR event.
    """
    assert _main_without_reviewer(tmp_path, monkeypatch, target, None) == []
    assert "no PR reviewer" in capsys.readouterr().out


@TARGETS
def test_no_codeowners_and_no_reviewer_posts_nothing(
    tmp_path: Path, monkeypatch: Any, target: list[str]
) -> None:
    """Today's behavior, unchanged: not even a GitHub read happens."""

    def _no_github(args: Any) -> Any:
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(review_gate, "_gh", _no_github)
    monkeypatch.setattr(review_gate, "_gh_output", _no_github)
    monkeypatch.setattr(review_gate.reviewer_state, "_gh_json", _no_github)
    assert _main_without_reviewer(tmp_path, monkeypatch, target, None) == []


@TARGETS
@pytest.mark.parametrize(
    ("reviews", "want"),
    [
        ([], "failure"),
        (
            [
                {
                    "user": {"login": "octocat"},
                    "state": "APPROVED",
                    "commit_id": "f" * 40,
                }
            ],
            "success",
        ),
    ],
    ids=["owner-needed", "owner-approved"],
)
def test_codeowners_with_no_reviewer_posts_an_ownership_only_status(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    target: list[str],
    reviews: list[dict[str, Any]],
    want: str,
) -> None:
    """CODEOWNERS alone turns the status on (with no reviewer half to add)."""
    monkeypatch.setattr(
        review_gate, "_ownership_inputs", lambda repo, pr: _inputs(reviews=reviews)
    )
    posted = _main_without_reviewer(tmp_path, monkeypatch, target, CODEOWNERS)
    ((sha, state, description),) = posted
    assert sha == "f" * 40 and state == want
    if want == "failure":
        assert "@octocat must approve" in description
    else:
        assert description == review_gate.OWNERSHIP_CLEAR
    assert "no PR reviewer" in capsys.readouterr().out


@TARGETS
@NO_USER_OWNER
def test_an_all_team_codeowners_counts_as_no_file_without_a_reviewer(
    tmp_path: Path, monkeypatch: Any, capsys: Any, target: list[str], text: str
) -> None:
    """Invisible until needed: no user owner and no reviewer posts nothing."""

    def _no_github(args: Any) -> Any:
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(review_gate.reviewer_state, "_gh_json", _no_github)
    assert _main_without_reviewer(tmp_path, monkeypatch, target, text) == []
    out = capsys.readouterr().out
    assert "no PR reviewer" in out and "CODEOWNERS half" not in out


@NO_USER_OWNER
def test_an_all_team_codeowners_leaves_the_reviewer_status_unchanged(
    monkeypatch: Any, tmp_path: Path, text: str
) -> None:
    _codeowners(monkeypatch, tmp_path, text)
    head = _Head([_state(verdict="none")])
    monkeypatch.setattr(review_gate.reviewer_state, "collect_state", head.collect)
    monkeypatch.setattr(review_gate, "_post", head.post)
    monkeypatch.setattr(
        review_gate, "_current_gate_status", lambda repo, sha: head.current
    )
    review_gate.post_for_pr(REPO, 7, reviewer_state.load_reviewer(key="coderabbit"))
    assert head.posts == [gate_status(_state(verdict="none"))]
