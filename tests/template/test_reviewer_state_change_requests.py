"""A human's change request after the final full review buys more local rounds.

With a local reviewer, the PR's one automated request is the final full review and
later fixes are re-checked locally, within ``max_local_after_final`` rounds. A human
reviewer who requests changes after that review asks for more fixes, and those need
local rounds too, or the agent runs out and reaches for a PR review request. So each
human ``CHANGES_REQUESTED`` review submitted after the first final-review request
adds ``local_rounds_per_change_request`` rounds to the after-final cap, and
``reviewer_state`` reports how many reviews did (``after_final.from_change_requests``).

Adapters are loaded from the real ``.github/pr-reviewer.json``; comment and review
payloads are literal GitHub REST shapes, and ``collect_state`` runs against a fake
``gh``.
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
RoundCaps = reviewer_state.RoundCaps
CODERABBIT = reviewer_state.load_reviewer(key="coderabbit")
BUGBOT = reviewer_state.load_reviewer(key="bugbot")

HEAD = "1111111111111111111111111111111111111111"
OLD = "2222222222222222222222222222222222222222"
BASE = "3333333333333333333333333333333333333333"

FINAL = "@coderabbitai full review"
_REPLY = "<!-- This is an auto-generated reply by CodeRabbit -->\n"
_FULL_DONE = _REPLY + "<summary>✅ Action performed</summary>\n\nFull review finished."

_BEFORE_FINAL = "2026-10-13T01:30:00Z"
_FINAL_AT = "2026-10-13T02:00:00Z"
_AFTER_FINAL = "2026-10-13T03:00:00Z"

OCTOCAT = {"login": "octocat", "type": "User"}


def _comment(body: str, at: str, login: str = "a-developer") -> dict[str, Any]:
    return {"body": body, "created_at": at, "updated_at": at, "user": {"login": login}}


def _record(head: str, at: str, round_: int = 1) -> dict[str, Any]:
    marker = reviewer_state.local_review_marker(
        {"reviewer": "x", "head": head, "base": BASE, "round": round_, "findings": 0}
    )
    return _comment(marker + "\nLocal review: 0 findings.", at)


def _final_done() -> list[dict[str, Any]]:
    """Return the comments of a final full review that ran."""
    return [
        _comment(FINAL, _FINAL_AT),
        _comment(_FULL_DONE, "2026-10-13T02:10:00Z", "coderabbitai[bot]"),
    ]


def _review(
    state: str = "CHANGES_REQUESTED",
    at: str = _AFTER_FINAL,
    user: dict[str, Any] | None = OCTOCAT,
) -> dict[str, Any]:
    return {"id": 1, "user": user, "state": state, "submitted_at": at, "body": "x"}


def _state(
    comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    caps: Any = None,
    adapter: Any = CODERABBIT,
) -> Any:
    return reviewer_state.local_review_state(
        comments, adapter, caps or RoundCaps(), HEAD, reviews=reviews
    )


# --- The bump -------------------------------------------------------------------


@pytest.mark.parametrize(("requests", "cap"), [(0, 3), (1, 5), (2, 7)])
def test_each_human_change_request_after_the_final_adds_rounds(
    requests: int, cap: int
) -> None:
    reviews = [_review(at=f"2026-10-13T0{3 + i}:00:00Z") for i in range(requests)]
    state = _state(_final_done(), reviews)
    assert state["phase"] == "after_final"
    assert state["after_final"] == {
        "used": 0,
        "cap": cap,
        "from_change_requests": requests,
    }


@pytest.mark.parametrize(
    "user",
    [
        {"login": "coderabbitai[bot]", "type": "Bot"},
        {"login": "coderabbitai", "type": "User"},  # an adapter login, no suffix
        {"login": "another-app[bot]", "type": "Bot"},
        None,  # a deleted account
    ],
)
def test_a_bot_change_request_buys_no_rounds(user: dict[str, Any] | None) -> None:
    state = _state(_final_done(), [_review(user=user)])
    assert state["after_final"]["from_change_requests"] == 0
    assert state["after_final"]["cap"] == 3


def test_a_change_request_before_the_final_buys_no_rounds() -> None:
    """The final full review already re-checked what it asked for."""
    state = _state(_final_done(), [_review(at=_BEFORE_FINAL)])
    assert state["after_final"]["from_change_requests"] == 0
    assert state["after_final"]["cap"] == 3


def test_a_change_request_with_no_final_review_yet_buys_no_rounds() -> None:
    state = _state([], [_review()])
    assert state["phase"] == "before_final"
    assert state["before_final"]["cap"] == 3
    assert state["after_final"]["from_change_requests"] == 0


@pytest.mark.parametrize("review_state", ["APPROVED", "COMMENTED", "DISMISSED"])
def test_only_a_change_request_buys_rounds(review_state: str) -> None:
    """A dismissed change request reads DISMISSED on GitHub: it no longer counts."""
    state = _state(_final_done(), [_review(state=review_state)])
    assert state["after_final"]["from_change_requests"] == 0
    assert state["after_final"]["cap"] == 3


def test_the_configured_rounds_per_change_request_are_used() -> None:
    caps = RoundCaps(local_after_final=1, local_per_change_request=4)
    state = _state(_final_done(), [_review()], caps)
    assert state["after_final"]["cap"] == 5


def test_a_change_request_lifts_a_spent_budget() -> None:
    comments = [
        *_final_done(),
        _record(OLD, "2026-10-13T02:20:00Z"),
        _record(OLD, "2026-10-13T02:30:00Z", 2),
        _record(OLD, "2026-10-13T02:40:00Z", 3),
    ]
    assert _state(comments, [])["allowed"] is False
    state = _state(comments, [_review()])
    assert state["after_final"] == {"used": 3, "cap": 5, "from_change_requests": 1}
    assert state["allowed"] is True


def test_a_reviewer_without_a_local_review_is_unaffected() -> None:
    assert _state(_final_done(), [_review()], adapter=BUGBOT) == {"configured": False}


def test_the_pr_round_caps_ignore_change_requests() -> None:
    """A PR-round reviewer's budget is unchanged: the bump is for local rounds."""
    caps = RoundCaps(local_per_change_request=9)
    pr_only = CODERABBIT._replace(local_review=None, local_preflight=None)
    assert reviewer_state.pr_round_caps(pr_only, caps) == caps
    assert reviewer_state.pr_round_caps(CODERABBIT, caps) == caps._replace(
        before_final=0, after_final=0
    )


# --- The config knob ------------------------------------------------------------


def _write(tmp_path: Path, rounds: dict[str, Any]) -> Path:
    config = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    config["review_rounds"] = rounds
    path = tmp_path / "pr-reviewer.json"
    path.write_text(json.dumps(config))
    return path


def test_the_default_is_two_rounds_per_change_request() -> None:
    assert reviewer_state.DEFAULT_ROUND_CAPS.local_per_change_request == 2


def test_the_repo_config_declares_the_knob() -> None:
    """The shipped config names it, so a repo can find and change it."""
    config = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    assert config["review_rounds"]["local_rounds_per_change_request"] == 2
    assert reviewer_state.load_round_caps().local_per_change_request == 2


def test_the_config_value_is_read(tmp_path: Path) -> None:
    path = _write(tmp_path, {"local_rounds_per_change_request": 5})
    assert reviewer_state.load_round_caps(path).local_per_change_request == 5


@pytest.mark.parametrize(
    "rounds",
    [
        {"local_round_per_change_request": 2},  # misspelled: must not fall back
        {"local_rounds_per_change_request": -1},
        {"local_rounds_per_change_request": True},
        {"local_rounds_per_change_request": "2"},
    ],
)
def test_a_bad_knob_fails_loudly(tmp_path: Path, rounds: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="review_rounds"):
        reviewer_state.load_round_caps(_write(tmp_path, rounds))


# --- collect_state reads the PR's reviews ---------------------------------------


def _fake_gh(comments: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> Any:
    def _fake(args: list[str]) -> Any:
        if "graphql" in args:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "headRefOid": HEAD,
                            "mergeStateStatus": "CLEAN",
                            "commits": {"nodes": []},
                            "reviewThreads": {"nodes": []},
                        }
                    }
                }
            }
        if any(a.endswith("/pulls/1/reviews") for a in args):
            return reviews
        if any(a.endswith("/issues/1/comments") for a in args):
            return comments
        return []

    return _fake


def test_collect_state_passes_the_reviews_through(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        reviewer_state, "_gh_json", _fake_gh(_final_done(), [_review()])
    )
    state = reviewer_state.collect_state("o/r", 1, CODERABBIT, RoundCaps())
    assert state["local_review"]["after_final"] == {
        "used": 0,
        "cap": 5,
        "from_change_requests": 1,
    }
