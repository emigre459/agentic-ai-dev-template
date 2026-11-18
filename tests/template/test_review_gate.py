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
import subprocess
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
human_review = _load("human_review")
merge_gate = _load("merge_gate")
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


def test_local_reviewer_open_threads_do_not_block_gate() -> None:
    """The ruleset's conversation resolution blocks the merge; the gate must not lag."""
    state, description = gate_status(
        _local(open_findings=[{"path": "a.py"}] * 2), threads_enforced=True
    )
    assert state == "success"
    assert "full review finished" in description
    assert "thread" not in description


def test_local_reviewer_open_threads_hold_the_gate_without_the_rule() -> None:
    """No rule requires conversation resolution: the gate itself waits on threads."""
    state, description = gate_status(_local(open_findings=[{"path": "a.py"}] * 2))
    assert state == "failure"
    assert description == "2 CodeRabbit thread(s) still open"


_THREAD_RULE = {
    "type": "pull_request",
    "parameters": {"required_review_thread_resolution": True},
}


def _rules_run(
    monkeypatch: Any, rules: Any, reads: list[tuple[str, str]] | None = None
) -> _Head:
    """Run a locally reviewed PR with two open threads against ``rules``."""

    def _branch_rules(repo: str, base: str) -> Any:
        if reads is not None:
            reads.append((repo, base))
        if isinstance(rules, BaseException):
            raise rules
        return rules

    monkeypatch.setattr(review_gate.merge_gate, "branch_rules", _branch_rules)
    monkeypatch.setattr(
        review_gate.reviewer_state,
        "_gh_json",
        lambda args: (
            {"default_branch": "trunk"}
            if args == ["api", f"repos/{REPO}"]
            else pytest.fail(f"unexpected gh read {args}")
        ),
    )
    monkeypatch.setattr(review_gate, "DOCS_ONLY_PATHS_FILE", Path("/nonexistent.json"))
    state = _local(open_findings=[{"path": "a.py"}] * 2)
    return _run(monkeypatch, _Head([state]))


def test_open_threads_pass_when_the_default_branch_rules_resolve_them(
    monkeypatch: Any,
) -> None:
    reads: list[tuple[str, str]] = []
    head = _rules_run(monkeypatch, [_THREAD_RULE], reads)
    assert set(reads) == {(REPO, "trunk")}  # the default branch, once per pass
    assert head.posts[0][0] == "success"
    assert "thread" not in head.posts[0][1]


@pytest.mark.parametrize(
    "rules",
    [
        None,
        [],
        [{"type": "pull_request", "parameters": {}}],
        subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 500"),
    ],
    ids=["404", "no-rules", "rule-off", "read-failed"],
)
def test_open_threads_fail_when_no_default_branch_rule_resolves_them(
    monkeypatch: Any, rules: Any
) -> None:
    head = _rules_run(monkeypatch, rules)
    assert head.posts == [("failure", "2 CodeRabbit thread(s) still open")]


def test_no_open_threads_reads_no_rules(monkeypatch: Any) -> None:
    def _boom(repo: str, base: str) -> Any:
        raise AssertionError("rules read with no open thread")

    monkeypatch.setattr(review_gate.merge_gate, "branch_rules", _boom)
    head = _run(monkeypatch, _Head([_local()]))
    assert head.posts[0][0] == "success"


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


# --- the opt-in docs-only lane ---------------------------------------------------

EXAMPLE = (
    _SCRIPTS.parent / "references" / "docs-only-paths.example.json"
)  # the skill ships it; a repo copies it to .github/docs-only-paths.json to opt in
REPO_ROOT = Path(__file__).resolve().parents[2]


def _allowlist() -> Any:
    allowlist = review_gate.load_docs_allowlist(EXAMPLE)
    assert allowlist is not None
    return allowlist


def _files(*names: str, **renamed: str) -> list[dict[str, str]]:
    listed = [{"filename": name} for name in names]
    listed += [
        {"filename": new, "previous_filename": old} for new, old in renamed.items()
    ]
    return listed


def test_docs_only_pr_passes_without_review() -> None:
    files = _files("docs/guide.md", "docs/img/a.PNG", "README.md")
    status = review_gate.docs_only_status(files, len(files), _allowlist())
    assert (
        status
        == review_gate.DOCS_ONLY_STATUS
        == (
            "success",
            "docs-only: no review required",
        )
    )


def test_absent_allowlist_makes_nothing_docs_only(tmp_path: Path) -> None:
    assert review_gate.load_docs_allowlist(tmp_path / "docs-only-paths.json") is None
    files = _files("docs/guide.md")
    assert review_gate.docs_only_status(files, 1, None) is None


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        "not json",
        json.dumps(
            {
                "dir_suffixes": [".md"],
                "top_level_suffix": ".md",
                "excluded_names": ["AGENTS.md"],
            }
        ),
        json.dumps(
            {
                "dirs": [],
                "dir_suffixes": [".md"],
                "top_level_suffix": ".md",
                "excluded_names": ["AGENTS.md"],
            }
        ),
        json.dumps(
            {
                "dirs": "docs/",
                "dir_suffixes": [".md"],
                "top_level_suffix": ".md",
                "excluded_names": ["AGENTS.md"],
            }
        ),
        json.dumps(
            {
                "dirs": ["docs/"],
                "dir_suffixes": [".md"],
                "top_level_suffix": 1,
                "excluded_names": ["AGENTS.md"],
            }
        ),
    ],
    ids=[
        "non-object",
        "unparseable",
        "missing-key",
        "empty-list",
        "wrong-type",
        "wrong-suffix-type",
    ],
)
def test_malformed_allowlist_fails_closed(tmp_path: Path, content: str) -> None:
    path = tmp_path / "docs-only-paths.json"
    path.write_text(content)
    assert review_gate.load_docs_allowlist(path) is None


def test_capped_file_list_is_never_docs_only() -> None:
    files = _files("docs/guide.md")
    assert review_gate.docs_only_status(files, len(files) + 1, _allowlist()) is None


def test_a_pr_with_no_listed_files_is_never_docs_only() -> None:
    assert review_gate.docs_only_status([], 0, _allowlist()) is None


def test_one_file_outside_the_list_needs_review() -> None:
    files = _files("docs/guide.md", "src/app.py", "docs/data.json")
    assert review_gate.docs_only_status(files, len(files), _allowlist()) is None


def test_renamed_from_outside_list_needs_review() -> None:
    files = _files(**{"docs/moved.md": "src/app.py"})
    assert review_gate.docs_only_status(files, 1, _allowlist()) is None


@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",
        "CLAUDE.md",
        "docs/AGENTS.md",
        "docs/deep/er/CLAUDE.md",
        "sub/AGENTS.md",
        "agents.md",
        "docs/claude.md",
        "GEMINI.md",
        "docs/Gemini.md",
    ],
)
def test_agents_and_claude_md_at_any_depth_need_review(path: str) -> None:
    files = _files("docs/guide.md", path)
    assert review_gate.docs_only_status(files, len(files), _allowlist()) is None


def _docs_only_run(monkeypatch: Any, head: _Head, owners: Any, tmp_path: Path) -> _Head:
    allow = tmp_path / "docs-only-paths.json"
    allow.write_text(EXAMPLE.read_text())
    monkeypatch.setattr(review_gate, "DOCS_ONLY_PATHS_FILE", allow)
    answers = {
        f"repos/{REPO}/pulls/7": {
            "head": {"sha": _state()["head_sha"]},
            "changed_files": 1,
        },
        f"repos/{REPO}/pulls/7/files?per_page=100": [{"filename": "docs/a.md"}],
    }
    monkeypatch.setattr(
        review_gate.reviewer_state, "_gh_json", lambda args: answers[args[-1]]
    )
    return _run(monkeypatch, head, owners=owners)


def test_docs_only_pr_posts_the_pass_instead_of_the_reviewer_verdict(
    monkeypatch: Any, tmp_path: Path
) -> None:
    head = _docs_only_run(monkeypatch, _Head([_state(verdict="none")]), None, tmp_path)
    assert head.posts == [review_gate.DOCS_ONLY_STATUS]


def test_path_owner_approval_still_comes_first(
    monkeypatch: Any, tmp_path: Path
) -> None:
    head = _docs_only_run(
        monkeypatch, _Head([_state(verdict="none")]), OWNER_NEEDED, tmp_path
    )
    assert head.posts == [OWNER_NEEDED]


def test_a_head_that_moved_under_the_file_read_is_not_docs_only(
    monkeypatch: Any, tmp_path: Path
) -> None:
    allow = tmp_path / "docs-only-paths.json"
    allow.write_text(EXAMPLE.read_text())
    monkeypatch.setattr(review_gate, "DOCS_ONLY_PATHS_FILE", allow)
    monkeypatch.setattr(
        review_gate.reviewer_state,
        "_gh_json",
        lambda args: {"head": {"sha": "9" * 40}, "changed_files": 1},
    )
    assert review_gate._docs_only(REPO, 7, "a" * 40) is None


def test_without_an_allowlist_a_docs_only_pr_still_needs_the_reviewer(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A repo that never opted in: the PR is not even read for docs-only."""
    monkeypatch.setattr(review_gate, "DOCS_ONLY_PATHS_FILE", tmp_path / "absent.json")

    def _boom(args: list[str]) -> Any:
        raise AssertionError("PR files read without an allowlist")

    monkeypatch.setattr(review_gate.reviewer_state, "_gh_json", _boom)
    head = _run(monkeypatch, _Head([_state(verdict="none")]))
    assert head.posts == [gate_status(_state(verdict="none"))]


def test_template_ships_no_docs_only_file() -> None:
    assert not (REPO_ROOT / ".github" / "docs-only-paths.json").exists()
    assert review_gate.DOCS_ONLY_PATHS_FILE == (
        REPO_ROOT / ".github" / "docs-only-paths.json"
    )


def test_example_allowlist_parses() -> None:
    assert review_gate.load_docs_allowlist(EXAMPLE) is not None
    assert "private" not in EXAMPLE.read_text().lower()


# --- the skills-update lane ----------------------------------------------------

SHA = _state()["head_sha"]
LANE = review_gate.SkillsUpdateLane("skills-update", ("acme-skills[bot]",))
FOLDERS = (".agents/skills/alpha", ".claude/skills/alpha")
LANE_EXAMPLE = (
    _SCRIPTS.parent / "references" / "skills-update-lane.example.json"
)  # a repo copies it to .github/skills-update-lane.json to opt in


def _pull(**over: Any) -> dict[str, Any]:
    pull: dict[str, Any] = {
        "user": {"login": "acme-skills[bot]"},
        "head": {"ref": "skills-update", "sha": SHA, "repo": {"full_name": REPO}},
        "changed_files": 2,
    }
    pull.update(over)
    return pull


def _lane_status(
    names: tuple[str, ...] = ("skills-lock.json", ".agents/skills/alpha/SKILL.md"),
    *,
    pull: dict[str, Any] | None = None,
    lane: Any = LANE,
    folders: Any = FOLDERS,
    pusher: Any = "acme-skills[bot]",
    **renamed: str,
) -> Any:
    files = _files(*names, **renamed)
    pull = pull if pull is not None else _pull(changed_files=len(files))
    return review_gate.skills_update_status(pull, files, lane, folders, REPO, pusher)


def test_lane_passes_lock_and_tag_pinned_folders() -> None:
    assert _lane_status() == review_gate.SKILLS_UPDATE_STATUS


def test_lane_passes_claude_skills_root() -> None:
    assert (
        _lane_status(("skills-lock.json", ".claude/skills/alpha/x.md"))
        == review_gate.SKILLS_UPDATE_STATUS
    )


def test_lane_status_fits_a_commit_status() -> None:
    assert len(review_gate.SKILLS_UPDATE_STATUS[1]) <= 140


@pytest.mark.parametrize(
    "case",
    [
        {"pull": _pull(user={"login": "alice"})},
        {"pull": _pull(head={"ref": "main", "sha": SHA, "repo": {"full_name": REPO}})},
        {
            "pull": _pull(
                head={
                    "ref": "skills-update",
                    "sha": SHA,
                    "repo": {"full_name": "evil/repo"},
                }
            )
        },
        {"pull": _pull(head={"ref": "skills-update", "sha": SHA, "repo": None})},
        {"names": ("skills-lock.json", ".agents/skills/beta/SKILL.md")},
        {"names": ("skills-lock.json", "README.md")},
        {
            "names": ("skills-lock.json",),
            "renamed": {".agents/skills/alpha/x": "src/x.py"},
        },
        {"pull": _pull(changed_files=3)},
        {"names": ()},
        {"names": ("skills-lock.json", ".agents/skills/alpha-x/SKILL.md")},
        {"lane": None},
        {"folders": ()},
        {"pusher": "alice"},
        {"pusher": None},
    ],
    ids=[
        "wrong-author",
        "wrong-branch",
        "fork",
        "no-head-repo",
        "branch-tracking-folder",
        "non-skill-path",
        "rename-from-outside",
        "capped-file-list",
        "no-files",
        "lookalike-folder",
        "no-lane",
        "no-folders",
        "pushed-by-a-person",
        "no-pusher",
    ],
)
def test_lane_refuses(case: dict[str, Any]) -> None:
    kwargs = {k: v for k, v in case.items() if k not in ("names", "renamed")}
    names = case.get("names", ("skills-lock.json", ".agents/skills/alpha/SKILL.md"))
    assert _lane_status(names, **case.get("renamed", {}), **kwargs) is None


def test_head_pusher_returns_the_newest_actor_when_it_made_the_head() -> None:
    activities = [
        {"after": SHA, "actor": {"login": "acme-skills[bot]"}},
        {"after": "1" * 40, "actor": {"login": "alice"}},
    ]
    assert review_gate.head_pusher(activities, SHA) == "acme-skills[bot]"


@pytest.mark.parametrize(
    "activities",
    [
        [],
        {},
        [
            {"after": "1" * 40, "actor": {"login": "acme-skills[bot]"}},
            {"after": SHA, "actor": {"login": "acme-skills[bot]"}},
        ],
        [{"after": SHA, "actor": None}],
        ["x"],
        [{"after": SHA, "actor": {"login": 7}}],
    ],
    ids=[
        "empty",
        "not-a-list",
        "bot-pushed-sha-earlier-not-newest",
        "null-actor",
        "entry-not-a-dict",
        "login-not-a-string",
    ],
)
def test_head_pusher_refuses(activities: Any) -> None:
    assert review_gate.head_pusher(activities, SHA) is None


def _write_lock(root: Path, skills: Any) -> Path:
    lock = root / "skills-lock.json"
    lock.write_text(json.dumps({"version": 2, "skills": skills}))
    return lock


def test_tag_pinned_folders_use_the_lock_key_not_skill_path(tmp_path: Path) -> None:
    lock = _write_lock(
        tmp_path,
        {
            "alpha": {"ref": "v1.2.3", "skillPath": "skills/alpha/SKILL.md"},
            "beta": {"ref": "main"},
            "gamma": {},
            "src": {"ref": "v1.0.0", "skillPath": "src/foo/SKILL.md"},
            "..": {"ref": "v1.0.0"},
            "a/b": {"ref": "v1.0.0"},
            "c\\d": {"ref": "v1.0.0"},
        },
    )
    folders = review_gate.tag_pinned_skill_folders(lock)
    assert sorted(folders) == [
        ".agents/skills/alpha",
        ".agents/skills/src",
        ".claude/skills/alpha",
        ".claude/skills/src",
    ]
    assert not any("foo" in f for f in folders)


@pytest.mark.parametrize("content", [None, "not json", '{"skills": []}'])
def test_tag_pinned_folders_missing_or_malformed_lock_is_empty(
    tmp_path: Path, content: str | None
) -> None:
    lock = tmp_path / "skills-lock.json"
    if content is not None:
        lock.write_text(content)
    assert review_gate.tag_pinned_skill_folders(lock) == ()


def test_lane_config_loading(tmp_path: Path) -> None:
    path = tmp_path / "lane.json"
    assert review_gate.load_skills_update_lane(path) is None
    good = {"branch": "skills-update", "authors": ["a[bot]"], "description": "x"}
    path.write_text(json.dumps(good))
    assert review_gate.load_skills_update_lane(path) == review_gate.SkillsUpdateLane(
        "skills-update", ("a[bot]",)
    )
    for bad in (
        json.dumps({**good, "branch": ""}),
        json.dumps({**good, "authors": []}),
        json.dumps({**good, "authors": [""]}),
        json.dumps({"authors": ["a[bot]"]}),
        "[]",
        "not json",
    ):
        path.write_text(bad)
        assert review_gate.load_skills_update_lane(path) is None, bad


def test_shipped_lane_example_is_valid_and_template_ships_no_lane_file() -> None:
    assert review_gate.load_skills_update_lane(LANE_EXAMPLE) is not None
    assert "head pushed by" in json.loads(LANE_EXAMPLE.read_text())["description"]
    assert not (REPO_ROOT / ".github" / "skills-update-lane.json").exists()


class _LaneRepo:
    """Fake GitHub answers for one PR in the lane tests."""

    def __init__(self, monkeypatch: Any, root: Path, **over: Any) -> None:
        (root / ".github").mkdir(exist_ok=True)
        lane = root / ".github" / "skills-update-lane.json"
        lane.write_text(
            json.dumps({"branch": "skills-update", "authors": ["acme-skills[bot]"]})
        )
        lock = _write_lock(root, {"alpha": {"ref": "v1.2.3"}})
        monkeypatch.setattr(review_gate, "SKILLS_UPDATE_LANE_FILE", lane)
        monkeypatch.setattr(review_gate, "SKILLS_LOCK_FILE", lock)
        review_gate._cached_skills_update_lane.cache_clear()
        review_gate._cached_tag_pinned_folders.cache_clear()
        self.activity: Any = over.pop(
            "activity",
            [
                {
                    "activity_type": "force_push",
                    "after": SHA,
                    "actor": {"login": "acme-skills[bot]"},
                }
            ],
        )
        self.pull = _pull(**over)
        self.names = ["skills-lock.json", ".agents/skills/alpha/SKILL.md"]
        self.pull["changed_files"] = over.get("changed_files", len(self.names))

        def gh_json(args: list[str]) -> Any:
            if args[-1] == f"repos/{REPO}/pulls/1":
                return self.pull
            if args[-1] == f"repos/{REPO}/pulls/1/reviews?per_page=100":
                return []
            if args[-1] == (
                f"repos/{REPO}/activity?ref=refs/heads/skills-update&per_page=1"
            ):
                if isinstance(self.activity, Exception):
                    raise self.activity
                return self.activity
            assert args[-1] == f"repos/{REPO}/pulls/1/files?per_page=100"
            return [{"filename": n} for n in self.names]

        monkeypatch.setattr(review_gate.reviewer_state, "_gh_json", gh_json)


def _lane_run(
    monkeypatch: Any,
    tmp_path: Path,
    state: dict[str, Any] | None,
    extra_names: list[str] | None = None,
    **over: Any,
) -> list[tuple[str, str]]:
    _codeowners(monkeypatch, tmp_path, "* @owner\n")
    repo = _LaneRepo(monkeypatch, tmp_path, **over)
    if extra_names:
        repo.names += extra_names
        repo.pull["changed_files"] = len(repo.names)
    posts: list[tuple[str, str]] = []
    if state is not None:
        head = _Head([state])
        monkeypatch.setattr(review_gate.reviewer_state, "collect_state", head.collect)
    else:
        monkeypatch.setattr(
            review_gate.reviewer_state,
            "collect_state",
            lambda *a: pytest.fail("no reviewer configured"),
        )
    monkeypatch.setattr(review_gate, "_current_gate_status", lambda r, s: None)

    def post(repo: str, sha: str, state_: str, description: str) -> None:
        posts.append((state_, description))
        monkeypatch.setattr(
            review_gate, "_current_gate_status", lambda r, s: (state_, description)
        )

    monkeypatch.setattr(review_gate, "_post", post)
    adapter = reviewer_state.load_reviewer(key="coderabbit") if state else None
    review_gate.post_for_pr(REPO, 1, adapter)
    return posts


def test_update_pr_passes_both_halves_under_a_catch_all_codeowners(
    monkeypatch: Any, tmp_path: Path
) -> None:
    posts = _lane_run(monkeypatch, tmp_path, _state(verdict="none"))
    assert posts == [review_gate.SKILLS_UPDATE_STATUS]


def test_a_persons_pr_on_skill_paths_still_needs_the_owner(
    monkeypatch: Any, tmp_path: Path
) -> None:
    posts = _lane_run(
        monkeypatch, tmp_path, _state(verdict="none"), user={"login": "alice"}
    )
    assert [p[0] for p in posts] == ["failure"]
    assert "@owner must approve" in posts[0][1]


def test_an_update_pr_with_one_non_skill_path_needs_both_halves(
    monkeypatch: Any, tmp_path: Path
) -> None:
    posts = _lane_run(monkeypatch, tmp_path, _state(), extra_names=["src/x.py"])
    assert [p[0] for p in posts] == ["failure"]
    assert "@owner must approve" in posts[0][1]


def test_a_moved_head_fails_closed(monkeypatch: Any, tmp_path: Path) -> None:
    moved = {"ref": "skills-update", "sha": "9" * 40, "repo": {"full_name": REPO}}
    posts = _lane_run(monkeypatch, tmp_path, _state(verdict="none"), head=moved)
    assert [p[0] for p in posts] == ["failure"]
    assert "@owner must approve" in posts[0][1]


def test_the_lane_passes_with_no_reviewer_configured(
    monkeypatch: Any, tmp_path: Path
) -> None:
    posts = _lane_run(monkeypatch, tmp_path, None)
    assert posts == [review_gate.SKILLS_UPDATE_STATUS]


def test_a_non_lane_pr_reads_no_files_and_needs_the_owner_with_no_reviewer(
    monkeypatch: Any, tmp_path: Path
) -> None:
    posts = _lane_run(monkeypatch, tmp_path, None, user={"login": "alice"})
    assert [p[0] for p in posts] == ["failure"]
    assert "@owner must approve" in posts[0][1]


_PERSON_PUSHED = [
    {"after": SHA, "actor": {"login": "alice"}},
    {"after": "1" * 40, "actor": {"login": "acme-skills[bot]"}},
]


def test_a_persons_push_on_the_bots_branch_needs_both_halves(
    monkeypatch: Any, tmp_path: Path
) -> None:
    posts = _lane_run(
        monkeypatch, tmp_path, _state(verdict="none"), activity=_PERSON_PUSHED
    )
    assert [p[0] for p in posts] == ["failure"]
    assert "@owner must approve" in posts[0][1]


def test_a_persons_push_with_no_reviewer_needs_the_owner(
    monkeypatch: Any, tmp_path: Path
) -> None:
    posts = _lane_run(monkeypatch, tmp_path, None, activity=_PERSON_PUSHED)
    assert [p[0] for p in posts] == ["failure"]
    assert "@owner must approve" in posts[0][1]


def test_a_lagging_activity_log_fails_closed(monkeypatch: Any, tmp_path: Path) -> None:
    stale = [{"after": "1" * 40, "actor": {"login": "acme-skills[bot]"}}]
    posts = _lane_run(monkeypatch, tmp_path, _state(verdict="none"), activity=stale)
    assert [p[0] for p in posts] == ["failure"]


def test_an_unreadable_activity_log_fails_closed(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    posts = _lane_run(
        monkeypatch,
        tmp_path,
        _state(verdict="none"),
        activity=subprocess.CalledProcessError(1, "gh"),
    )
    assert [p[0] for p in posts] == ["failure"]
    assert "activity unreadable" in capsys.readouterr().err


def test_a_malformed_lane_file_and_lock_say_so_on_stderr(
    tmp_path: Path, capsys: Any
) -> None:
    lane = tmp_path / "lane.json"
    lane.write_text('{"branch": ""}')
    assert review_gate.load_skills_update_lane(lane) is None
    assert f"{lane}: malformed skills-update lane" in capsys.readouterr().err
    lock = tmp_path / "skills-lock.json"
    lock.write_text("not json")
    assert review_gate.tag_pinned_skill_folders(lock) == ()
    assert f"{lock}: unreadable skills lock" in capsys.readouterr().err


def test_empty_and_dot_lock_keys_are_skipped(tmp_path: Path) -> None:
    lock = _write_lock(
        tmp_path,
        {"": {"ref": "v1.0.0"}, ".": {"ref": "v1.0.0"}, "ok": {"ref": "v1.0.0"}},
    )
    assert review_gate.tag_pinned_skill_folders(lock) == (
        ".agents/skills/ok",
        ".claude/skills/ok",
    )
