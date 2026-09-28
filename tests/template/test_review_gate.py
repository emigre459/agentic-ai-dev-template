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
