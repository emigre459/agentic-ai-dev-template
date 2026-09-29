"""Tests for the vendor-neutral ``review-gate`` commit-status script.

``review_gate.py`` lives beside ``reviewer_state.py`` outside the importable package
tree, so both are loaded by path (``reviewer_state`` first, since ``review_gate``
imports it by name). ``gate_status`` is a pure function over ``collect_state``
output; posting is tested with ``_gh`` stubbed out.
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


reviewer_state = _load("reviewer_state")
review_gate = _load("review_gate")
gate_status = review_gate.gate_status

REPO = "octo/example"


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


def _run(monkeypatch: Any, head: _Head) -> _Head:
    monkeypatch.setattr(review_gate.reviewer_state, "collect_state", head.collect)
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


def test_the_sweep_posts_every_open_pr_and_isolates_failures(monkeypatch: Any) -> None:
    done: list[int] = []

    def _post_for_pr(repo: str, pr: int, adapter: Any) -> None:
        if pr == 2:
            raise RuntimeError("gh failed")
        done.append(pr)

    monkeypatch.setattr(review_gate, "_open_prs", lambda repo: [1, 2, 3])
    monkeypatch.setattr(review_gate, "post_for_pr", _post_for_pr)
    with pytest.raises(SystemExit, match="1 of 3"):
        review_gate.post_for_all_open_prs(REPO, reviewer_state.load_reviewer())
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
