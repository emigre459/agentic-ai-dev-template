"""Tests for resolve-pr-concerns' ``human_review.sh`` (Step 2h).

The script lists a human reviewer's open concerns -- line threads, file-level
threads, review bodies -- and whether their request for changes still stands.
These tests run the real script against a stub ``gh`` on ``PATH`` that serves
canned REST / GraphQL payloads (applying ``--jq`` with real ``jq``). The fixture
has a human line comment, a human file-level comment (``line`` is null), a
``CHANGES_REQUESTED`` body, a bot thread and marked agent answers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 -- fixed argv, in-repo script, no shell
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = (
    _REPO / ".agents" / "skills" / "resolve-pr-concerns" / "scripts" / "human_review.sh"
)

# Serves each endpoint's payload; GraphQL payloads are a list of pages, so the
# stub applies --jq per page the way `gh api graphql --paginate` does.
_FAKE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_CALLS"
expr="."; data="null"; pages=0
for arg in "$@"; do
  case "$arg" in
    graphql) data="$FAKE_THREADS"; pages=1 ;;
    */pulls/*/comments) data="$FAKE_INLINE" ;;
    */reviews) data="$FAKE_REVIEWS" ;;
    */issues/*/comments) data="$FAKE_COMMENTS" ;;
    repos/*/pulls/*) data="$FAKE_PULL" ;;
  esac
done
while [[ $# -gt 0 ]]; do [[ "$1" == "--jq" ]] && expr="$2"; shift; done
if [[ $pages -eq 1 ]]; then jq -c ".[] | $expr" <<< "$data"
else jq -c "$expr" <<< "$data"; fi
"""

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="human_review.sh needs bash + jq",
)

ANSWER = "<!-- resolve-pr-concerns:answer -->"
DAVE = ("dave", "User")
ADAM = ("adam", "User")
BOT = ("review-bot[bot]", "Bot")


def _review_answer(review_id: int) -> str:
    return f"<!-- resolve-pr-concerns:answer review={review_id} -->"


def _comment(
    cid: int,
    who: tuple[str, str],
    body: str,
    *,
    reply_to: int | None = None,
    subject_type: str = "line",
    line: int | None = 10,
    created: str = "2026-10-26T17:00:00Z",
) -> dict[str, Any]:
    return {
        "id": cid,
        "user": {"login": who[0], "type": who[1]},
        "body": body,
        "in_reply_to_id": reply_to,
        "subject_type": subject_type,
        "path": "src/a.py",
        "line": line,
        "created_at": created,
        "html_url": f"https://example.test/c/{cid}",
    }


def _review(
    rid: int, who: tuple[str, str], state: str, body: str, submitted: str
) -> dict[str, Any]:
    return {
        "id": rid,
        "user": {"login": who[0], "type": who[1]},
        "state": state,
        "body": body,
        "submitted_at": submitted,
        "html_url": f"https://example.test/r/{rid}",
    }


def _issue_comment(who: tuple[str, str], body: str) -> dict[str, Any]:
    return {"user": {"login": who[0], "type": who[1]}, "body": body}


def _threads(*resolved: tuple[int, bool]) -> list[dict[str, Any]]:
    """One GraphQL page per (root id, isResolved) pair, to exercise pagination."""
    return [
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "isResolved": flag,
                                    "comments": {"nodes": [{"databaseId": root}]},
                                }
                            ]
                        }
                    }
                }
            }
        }
        for root, flag in resolved
    ]


LINE = _comment(1, DAVE, "**Blocking.** this resolves retired nodes")
FILE = _comment(
    2, DAVE, "**Should fix.** two loops aren't paced", subject_type="file", line=None
)
BOT_ROOT = _comment(3, BOT, "Potential issue: off by one")
AGENT_ON_BOT = _comment(
    4, DAVE, "Fixed in abc123.", reply_to=3, created="2026-10-26T17:05:00Z"
)
INLINE = [LINE, FILE, BOT_ROOT, AGENT_ON_BOT]
CR = _review(
    100, DAVE, "CHANGES_REQUESTED", "A few things to fix.", "2026-10-26T17:25:47Z"
)


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "pr-reviewer.json"
    path.write_text(
        json.dumps(
            {
                "active": "a",
                "reviewers": {
                    "a": {"bot_logins": ["review-bot", "review-bot[bot]"]},
                    "b": {"bot_logins": ["other-bot"]},
                },
            }
        )
    )
    return path


def _run(
    tmp_path: Path,
    *,
    inline: list[dict[str, Any]] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
    threads: list[dict[str, Any]] | None = None,
    requested: list[str] | None = None,
    config: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH)
    gh.chmod(0o755)
    env: dict[str, str] = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_CALLS": str(tmp_path / "calls.log"),
        "FAKE_INLINE": json.dumps(INLINE if inline is None else inline),
        "FAKE_REVIEWS": json.dumps([CR] if reviews is None else reviews),
        "FAKE_COMMENTS": json.dumps(comments or []),
        "FAKE_THREADS": json.dumps(threads if threads is not None else _threads()),
        "FAKE_PULL": json.dumps(
            {"requested_reviewers": [{"login": login} for login in requested or []]}
        ),
    }
    cfg = config if config is not None else _config(tmp_path)
    return subprocess.run(  # nosec B603 B607 -- fixed argv, stubbed gh, no shell
        ["bash", str(_SCRIPT), "7", "o/r", "--config", str(cfg)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=tmp_path,
        timeout=30,
    )


def _state(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    result = _run(tmp_path, **kwargs)
    assert result.returncode == 0, result.stderr
    state: dict[str, Any] = json.loads(result.stdout)
    return state


def _roots(state: dict[str, Any]) -> list[int]:
    return [t["root_id"] for t in state["threads"]]


def test_script_parses() -> None:
    result = subprocess.run(  # nosec B603 B607 -- fixed argv, no shell
        ["bash", "-n", str(_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_usage_error_without_a_pr() -> None:
    result = subprocess.run(  # nosec B603 B607 -- fixed argv, no shell
        ["bash", str(_SCRIPT)], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 64


def test_a_missing_explicit_config_is_a_usage_error(tmp_path: Path) -> None:
    result = _run(tmp_path, config=tmp_path / "nope.json")
    assert result.returncode == 64


def test_a_changes_requested_review_lists_every_item(tmp_path: Path) -> None:
    """PR #2560's shape: a line thread, a file-level thread and the body."""
    state = _state(tmp_path)
    assert [(t["root_id"], t["subject_type"], t["line"]) for t in state["threads"]] == [
        (1, "line", 10),
        (2, "file", None),
    ]
    assert state["threads"][0]["reply_to"] == 1
    assert state["threads"][0]["author"] == "dave"
    assert [b["review_id"] for b in state["review_bodies"]] == [100]
    assert state["review_bodies"][0]["state"] == "CHANGES_REQUESTED"
    assert state["open_concern_count"] == 3
    assert state["changes_requested_by"] == ["dave"]
    assert state["rerequest_reviewers"] == []
    assert state["answer_marker"] == ANSWER


def test_every_read_is_paginated(tmp_path: Path) -> None:
    _state(tmp_path)
    calls = (tmp_path / "calls.log").read_text().splitlines()
    listed = [c for c in calls if "/comments" in c or "/reviews" in c or "graphql" in c]
    assert len(listed) == 4
    assert all("--paginate" in c for c in listed)


def test_bot_threads_stay_with_the_bot_path(tmp_path: Path) -> None:
    assert 3 not in _roots(_state(tmp_path))


def test_a_resolved_thread_on_a_later_page_is_not_a_concern(tmp_path: Path) -> None:
    state = _state(tmp_path, threads=_threads((1, False), (2, True)))
    assert _roots(state) == [1]


def test_a_marked_answer_closes_a_thread(tmp_path: Path) -> None:
    answer = _comment(
        5, DAVE, f"Fixed in d4.\n\n{ANSWER}", reply_to=1, created="2026-10-26T18:00:00Z"
    )
    assert _roots(_state(tmp_path, inline=[*INLINE, answer])) == [2]


def test_a_human_follow_up_reopens_the_thread(tmp_path: Path) -> None:
    answer = _comment(
        5, DAVE, f"Fixed.\n{ANSWER}", reply_to=1, created="2026-10-26T18:00:00Z"
    )
    follow = _comment(
        6,
        DAVE,
        "That misses the retired case.",
        reply_to=1,
        created="2026-10-26T19:00:00Z",
    )
    state = _state(tmp_path, inline=[*INLINE, answer, follow])
    assert _roots(state) == [1, 2]
    assert state["threads"][0]["body"] == "That misses the retired case."


def test_a_bot_reply_does_not_answer_a_human_thread(tmp_path: Path) -> None:
    bot_reply = _comment(
        7, BOT, f"I agree.\n{ANSWER}", reply_to=1, created="2026-10-26T18:00:00Z"
    )
    assert 1 in _roots(_state(tmp_path, inline=[*INLINE, bot_reply]))


def test_a_configured_bot_login_is_not_human_even_as_a_user(tmp_path: Path) -> None:
    other = _comment(8, ("other-bot", "User"), "lint nit")
    assert 8 not in _roots(_state(tmp_path, inline=[*INLINE, other]))


@pytest.mark.parametrize(
    "review",
    [
        _review(101, DAVE, "COMMENTED", "", "2026-10-26T17:00:00Z"),
        _review(102, DAVE, "APPROVED", "LGTM", "2026-10-26T17:00:00Z"),
        _review(103, DAVE, "DISMISSED", "old ask", "2026-10-26T17:00:00Z"),
        _review(
            104,
            BOT,
            "COMMENTED",
            "Actionable comments posted: 2",
            "2026-10-26T17:00:00Z",
        ),
        _review(105, DAVE, "COMMENTED", f"Fixed.\n{ANSWER}", "2026-10-26T17:00:00Z"),
    ],
)
def test_bodies_that_ask_nothing_are_skipped(
    tmp_path: Path, review: dict[str, Any]
) -> None:
    assert _state(tmp_path, reviews=[review])["review_bodies"] == []


def test_a_marked_top_level_comment_answers_bodies(tmp_path: Path) -> None:
    second = _review(107, DAVE, "COMMENTED", "One more thing.", "2026-10-26T18:00:00Z")
    answer = _issue_comment(
        DAVE, f"1. done\n2. done\n{_review_answer(100)}\n{_review_answer(107)}"
    )
    state = _state(tmp_path, reviews=[CR, second], comments=[answer])
    assert state["review_bodies"] == []


def test_a_bot_marker_or_a_thread_marker_does_not_answer_a_body(tmp_path: Path) -> None:
    comments = [_issue_comment(BOT, _review_answer(100)), _issue_comment(DAVE, ANSWER)]
    assert len(_state(tmp_path, comments=comments)["review_bodies"]) == 1


def test_a_later_comment_review_leaves_the_request_standing(tmp_path: Path) -> None:
    later = _review(108, DAVE, "COMMENTED", "Also this.", "2026-10-26T18:00:00Z")
    assert _state(tmp_path, reviews=[CR, later])["changes_requested_by"] == ["dave"]


@pytest.mark.parametrize("state", ["APPROVED", "DISMISSED"])
def test_a_later_approval_or_dismissal_clears_it(tmp_path: Path, state: str) -> None:
    later = _review(109, DAVE, state, "", "2026-10-26T18:00:00Z")
    assert _state(tmp_path, reviews=[later, CR])["changes_requested_by"] == []


def test_a_bot_request_for_changes_is_left_to_the_bot_path(tmp_path: Path) -> None:
    bot = _review(110, BOT, "CHANGES_REQUESTED", "Fix these.", "2026-10-26T18:00:00Z")
    assert _state(tmp_path, reviews=[bot])["changes_requested_by"] == []


def test_requests_are_listed_once_in_order(tmp_path: Path) -> None:
    reviews = [
        CR,
        _review(111, ADAM, "CHANGES_REQUESTED", "x", "2026-10-26T18:00:00Z"),
        _review(112, DAVE, "CHANGES_REQUESTED", "y", "2026-10-26T19:00:00Z"),
    ]
    assert _state(tmp_path, reviews=reviews)["changes_requested_by"] == ["adam", "dave"]


def _all_answered() -> dict[str, Any]:
    return {
        "inline": [
            *INLINE,
            _comment(
                5, DAVE, f"Fixed.\n{ANSWER}", reply_to=1, created="2026-10-26T18:00:00Z"
            ),
            _comment(
                6, DAVE, f"Fixed.\n{ANSWER}", reply_to=2, created="2026-10-26T18:00:00Z"
            ),
        ],
        "comments": [_issue_comment(DAVE, _review_answer(100))],
    }


def test_rerequest_once_everything_is_answered(tmp_path: Path) -> None:
    state = _state(tmp_path, **_all_answered())
    assert state["open_concern_count"] == 0
    assert state["changes_requested_by"] == ["dave"]
    assert state["rerequest_reviewers"] == ["dave"]
    assert state["awaiting_rereview"] == []


def test_no_rerequest_once_their_review_is_pending(tmp_path: Path) -> None:
    state = _state(tmp_path, requested=["dave"], **_all_answered())
    assert state["rerequest_reviewers"] == []
    assert state["awaiting_rereview"] == ["dave"]


def test_another_reviewers_open_item_does_not_hold_the_rerequest(
    tmp_path: Path,
) -> None:
    answered = _all_answered()
    adam = _comment(9, ADAM, "Rename this?", created="2026-10-26T18:30:00Z")
    state = _state(
        tmp_path, inline=[*answered["inline"], adam], comments=answered["comments"]
    )
    assert _roots(state) == [9]
    assert state["rerequest_reviewers"] == ["dave"]
