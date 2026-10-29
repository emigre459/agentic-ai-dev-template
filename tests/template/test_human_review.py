"""Tests for resolve-pr-concerns' ``human_review.py`` (Step 2h).

The script lists a human reviewer's open concerns -- line threads, file-level
threads, review bodies -- and whether their request for changes still stands.
It lives beside ``reviewer_state.py`` outside the importable package tree, so both
are loaded by path (``reviewer_state`` first: ``human_review`` imports it by name).
Its seams are pure functions over GitHub REST payloads, so the tests hand them a
fixture with a human line comment, a human file-level comment (``line`` is null), a
``CHANGES_REQUESTED`` body, plus a bot thread and an agent answer that must not read
as concerns. The collector is driven with ``_gh_json`` stubbed out.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess  # nosec B404 -- fixed argv, in-repo script, no shell
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPTS = (
    Path(__file__).resolve().parents[2] / ".agents" / "skills" / "resolve-pr-concerns"
) / "scripts"


def _load(name: str) -> ModuleType:
    """Load a standalone skill script as a module, registered by its name."""
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reviewer_state = _load("reviewer_state")
human_review = _load("human_review")

REPO = "owner/repo"
BOTS = frozenset({"review-bot", "review-bot[bot]"})
OCTOCAT = {"login": "octocat", "type": "User"}
BOT = {"login": "review-bot[bot]", "type": "Bot"}
ANSWER = human_review.ANSWER_MARKER


def _comment(
    cid: int,
    user: dict[str, str],
    body: str,
    *,
    reply_to: int | None = None,
    subject_type: str = "line",
    line: int | None = 10,
    created: str = "2026-10-26T17:00:00Z",
) -> dict[str, Any]:
    return {
        "id": cid,
        "user": user,
        "body": body,
        "in_reply_to_id": reply_to,
        "subject_type": subject_type,
        "path": "src/app/a.py",
        "line": line,
        "created_at": created,
        "html_url": f"https://example.test/c/{cid}",
    }


def _review(
    rid: int, user: dict[str, str], state: str, body: str, submitted: str
) -> dict[str, Any]:
    return {
        "id": rid,
        "user": user,
        "state": state,
        "body": body,
        "submitted_at": submitted,
        "html_url": f"https://example.test/r/{rid}",
    }


LINE = _comment(1, OCTOCAT, "**Blocking.** this resolves retired nodes")
FILE = _comment(
    2,
    OCTOCAT,
    "**Should fix.** two loops aren't paced",
    subject_type="file",
    line=None,
)
BOT_ROOT = _comment(3, BOT, "Potential issue: off by one")
AGENT_ON_BOT = _comment(
    4, OCTOCAT, "Fixed in abc123.", reply_to=3, created="2026-10-26T17:05:00Z"
)
INLINE = [LINE, FILE, BOT_ROOT, AGENT_ON_BOT]
CR_REVIEW = _review(
    100, OCTOCAT, "CHANGES_REQUESTED", "A few things to fix.", "2026-10-26T17:25:47Z"
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


def _thread_page(root: int, resolved: bool) -> dict[str, Any]:
    """One GraphQL page holding a single review thread."""
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "isResolved": resolved,
                                "comments": {"nodes": [{"databaseId": root}]},
                            }
                        ]
                    }
                }
            }
        }
    }


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 -- fixed argv, in-repo script, no shell
        [sys.executable, str(_SCRIPTS / "human_review.py"), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


# --- who counts as human ----------------------------------------------------


def test_a_user_is_human() -> None:
    assert human_review.is_human(OCTOCAT, BOTS)


@pytest.mark.parametrize(
    "user",
    [
        {"login": "review-bot", "type": "User"},  # a configured login, any type
        {"login": "other-bot", "type": "Bot"},
        {"login": "copilot-thing[bot]", "type": "User"},
        None,
    ],
)
def test_bots_and_unknown_authors_are_not_human(user: Any) -> None:
    assert not human_review.is_human(user, BOTS)


def test_bot_logins_cover_every_configured_reviewer(tmp_path: Path) -> None:
    config = tmp_path / "pr-reviewer.json"
    config.write_text(
        json.dumps(
            {
                "active": "a",
                "reviewers": {
                    "a": {"bot_logins": ["a-bot", "a-bot[bot]"]},
                    "b": {"bot_logins": ["b-bot"]},
                },
            }
        )
    )
    assert human_review.load_bot_logins(config) == {"a-bot", "a-bot[bot]", "b-bot"}


def test_bot_logins_load_from_the_real_config() -> None:
    assert human_review.load_bot_logins()  # every adapter declares its logins


# --- threads ----------------------------------------------------------------


def test_human_line_and_file_level_threads_are_concerns() -> None:
    concerns = human_review.thread_concerns(INLINE, {}, BOTS)
    assert [(c["root_id"], c["subject_type"], c["line"]) for c in concerns] == [
        (1, "line", 10),
        (2, "file", None),
    ]
    assert concerns[0]["author"] == "octocat"
    assert concerns[0]["body"] == LINE["body"]
    assert concerns[0]["reply_to"] == 1


def test_bot_threads_stay_with_the_bot_path() -> None:
    """A bot-rooted thread is never a human concern, even with an unmarked reply."""
    roots = [c["root_id"] for c in human_review.thread_concerns(INLINE, {}, BOTS)]
    assert 3 not in roots


def test_a_resolved_human_thread_is_not_a_concern() -> None:
    concerns = human_review.thread_concerns(INLINE, {1: True, 2: False}, BOTS)
    assert [c["root_id"] for c in concerns] == [2]


def test_a_thread_answered_with_the_marker_is_not_a_concern() -> None:
    answer = _comment(
        5,
        OCTOCAT,
        f"Fixed in def456.\n\n{ANSWER}",
        reply_to=1,
        created="2026-10-26T18:00Z",
    )
    concerns = human_review.thread_concerns([*INLINE, answer], {}, BOTS)
    assert [c["root_id"] for c in concerns] == [2]


def test_a_human_follow_up_after_the_answer_reopens_the_thread() -> None:
    answer = _comment(
        5, OCTOCAT, f"Fixed.\n{ANSWER}", reply_to=1, created="2026-10-26T18:00Z"
    )
    follow = _comment(
        6,
        OCTOCAT,
        "That misses the retired case.",
        reply_to=1,
        created="2026-10-26T19:00Z",
    )
    concerns = human_review.thread_concerns([*INLINE, answer, follow], {}, BOTS)
    assert concerns[0]["root_id"] == 1
    assert concerns[0]["body"] == "That misses the retired case."
    assert concerns[0]["reply_to"] == 1


def test_a_bot_reply_does_not_answer_a_human_thread() -> None:
    bot_reply = _comment(7, BOT, "I agree.", reply_to=1, created="2026-10-26T18:00Z")
    roots = [
        c["root_id"]
        for c in human_review.thread_concerns([*INLINE, bot_reply], {}, BOTS)
    ]
    assert 1 in roots


def test_a_reply_to_a_reply_joins_the_root_thread() -> None:
    answer = _comment(
        5, OCTOCAT, f"Fixed.\n{ANSWER}", reply_to=1, created="2026-10-26T18:00Z"
    )
    nested = _comment(
        6, OCTOCAT, f"And the test.\n{ANSWER}", reply_to=5, created="2026-10-26T18:01Z"
    )
    roots = [
        c["root_id"]
        for c in human_review.thread_concerns([*INLINE, answer, nested], {}, BOTS)
    ]
    assert roots == [2]


# --- review bodies ----------------------------------------------------------


def test_a_changes_requested_body_is_a_concern() -> None:
    concerns = human_review.review_body_concerns([CR_REVIEW], [], BOTS)
    assert concerns == [
        {
            "review_id": 100,
            "author": "octocat",
            "state": "CHANGES_REQUESTED",
            "submitted_at": "2026-10-26T17:25:47Z",
            "body": "A few things to fix.",
            "url": "https://example.test/r/100",
        }
    ]


@pytest.mark.parametrize(
    "review",
    [
        _review(101, OCTOCAT, "COMMENTED", "", "2026-10-26T17:00Z"),  # inline container
        _review(102, OCTOCAT, "APPROVED", "LGTM", "2026-10-26T17:00Z"),
        _review(103, OCTOCAT, "DISMISSED", "old ask", "2026-10-26T17:00Z"),
        _review(
            104, BOT, "COMMENTED", "Actionable comments posted: 2", "2026-10-26T17:00Z"
        ),
        _review(105, OCTOCAT, "COMMENTED", f"Fixed.\n{ANSWER}", "2026-10-26T17:00Z"),
    ],
)
def test_bodies_that_ask_nothing_of_the_agent_are_skipped(
    review: dict[str, Any],
) -> None:
    assert human_review.review_body_concerns([review], [], BOTS) == []


def test_a_commented_body_is_a_concern() -> None:
    review = _review(106, OCTOCAT, "COMMENTED", "Why this name?", "2026-10-26T17:00Z")
    assert [
        c["review_id"] for c in human_review.review_body_concerns([review], [], BOTS)
    ] == [106]


def test_a_body_answered_by_a_marked_top_level_comment_is_not_a_concern() -> None:
    answer = {
        "id": 900,
        "user": OCTOCAT,
        "body": f"Answers:\n1. done\n{human_review.review_answer_marker(100)}",
    }
    assert human_review.review_body_concerns([CR_REVIEW], [answer], BOTS) == []


def test_one_answer_comment_can_cover_several_reviews() -> None:
    second = _review(107, OCTOCAT, "COMMENTED", "One more thing.", "2026-10-26T18:00Z")
    answer = {
        "id": 900,
        "user": OCTOCAT,
        "body": "\n".join(
            [
                human_review.review_answer_marker(100),
                human_review.review_answer_marker(107),
            ]
        ),
    }
    assert human_review.review_body_concerns([CR_REVIEW, second], [answer], BOTS) == []


def test_a_marker_from_a_bot_does_not_answer_a_body() -> None:
    answer = {"id": 900, "user": BOT, "body": human_review.review_answer_marker(100)}
    assert len(human_review.review_body_concerns([CR_REVIEW], [answer], BOTS)) == 1


def test_a_thread_reply_marker_is_not_a_body_answer() -> None:
    answer = {"id": 900, "user": OCTOCAT, "body": ANSWER}
    assert len(human_review.review_body_concerns([CR_REVIEW], [answer], BOTS)) == 1


# --- outstanding requests for changes ----------------------------------------


def test_changes_requested_is_outstanding() -> None:
    assert human_review.changes_requested_by([CR_REVIEW], BOTS) == ["octocat"]


def test_a_later_comment_review_does_not_clear_a_request() -> None:
    later = _review(108, OCTOCAT, "COMMENTED", "Also this.", "2026-10-26T18:00Z")
    assert human_review.changes_requested_by([CR_REVIEW, later], BOTS) == ["octocat"]


@pytest.mark.parametrize("state", ["APPROVED", "DISMISSED"])
def test_a_later_approval_or_dismissal_clears_a_request(state: str) -> None:
    later = _review(109, OCTOCAT, state, "", "2026-10-26T18:00Z")
    assert human_review.changes_requested_by([later, CR_REVIEW], BOTS) == []


def test_a_bot_request_for_changes_is_left_to_the_bot_path() -> None:
    bot = _review(110, BOT, "CHANGES_REQUESTED", "Fix these.", "2026-10-26T18:00Z")
    assert human_review.changes_requested_by([bot], BOTS) == []


def test_each_requesting_reviewer_is_listed_once_in_order() -> None:
    reviewer_b = {"login": "reviewer-b", "type": "User"}
    reviews = [
        CR_REVIEW,
        _review(111, reviewer_b, "CHANGES_REQUESTED", "x", "2026-10-26T18:00Z"),
        _review(112, OCTOCAT, "CHANGES_REQUESTED", "y", "2026-10-26T19:00Z"),
    ]
    assert human_review.changes_requested_by(reviews, BOTS) == ["reviewer-b", "octocat"]


# --- re-request --------------------------------------------------------------


def test_rerequest_once_every_item_is_answered() -> None:
    assert human_review.rerequest_reviewers(["octocat"], [], []) == ["octocat"]


def test_no_rerequest_while_an_item_of_theirs_is_open() -> None:
    open_items = [{"author": "octocat"}]
    assert human_review.rerequest_reviewers(["octocat"], open_items, []) == []


def test_another_reviewers_open_item_does_not_hold_the_rerequest() -> None:
    open_items = [{"author": "reviewer-b"}]
    assert human_review.rerequest_reviewers(["octocat"], open_items, []) == ["octocat"]


def test_no_rerequest_while_their_review_is_already_requested() -> None:
    assert human_review.rerequest_reviewers(["octocat"], [], ["octocat"]) == []


# --- collector ---------------------------------------------------------------


class _FakeGh:
    """A ``_gh_json`` stand-in serving canned payloads and recording each call."""

    def __init__(
        self,
        *,
        inline: list[dict[str, Any]] | None = None,
        reviews: list[dict[str, Any]] | None = None,
        comments: list[dict[str, Any]] | None = None,
        threads: Any = None,
        requested: list[str] | None = None,
    ) -> None:
        self.inline = INLINE if inline is None else inline
        self.reviews = [CR_REVIEW] if reviews is None else reviews
        self.comments = comments or []
        self.threads = threads
        self.requested = requested or []
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> Any:
        self.calls.append(args)
        joined = " ".join(args)
        if "graphql" in joined:
            return self.threads
        if "/pulls/7/comments" in joined:
            return self.inline
        if "/pulls/7/reviews" in joined:
            return self.reviews
        if "/issues/7/comments" in joined:
            return self.comments
        if joined.startswith("api repos/owner/repo/pulls/7"):
            return {"requested_reviewers": [{"login": x} for x in self.requested]}
        raise AssertionError(f"unexpected gh call: {args}")

    @property
    def listed(self) -> list[list[str]]:
        """The calls that read a list (inline comments, reviews, issue comments, threads)."""
        return [
            c
            for c in self.calls
            if "graphql" in c or "/comments" in c[1] or "/reviews" in c[1]
        ]


def _collect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake: _FakeGh
) -> dict[str, Any]:
    monkeypatch.setattr(human_review, "_gh_json", fake)
    return human_review.collect_state(REPO, 7, _config(tmp_path))


def test_collect_state_on_a_changes_requested_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole read, end to end, with ``gh`` stubbed."""
    fake = _FakeGh(threads=[_thread_page(1, False), _thread_page(2, True)])
    state = _collect(monkeypatch, tmp_path, fake)

    assert [t["root_id"] for t in state["threads"]] == [1]  # 2 resolved on page two
    assert [b["review_id"] for b in state["review_bodies"]] == [100]
    assert state["open_concern_count"] == 2
    assert state["changes_requested_by"] == ["octocat"]
    assert state["awaiting_rereview"] == []
    assert state["rerequest_reviewers"] == []
    assert state["answer_marker"] == ANSWER
    assert set(state) == {
        "threads",
        "review_bodies",
        "open_concern_count",
        "changes_requested_by",
        "awaiting_rereview",
        "rerequest_reviewers",
        "answer_marker",
    }


def test_a_resolved_thread_on_a_later_page_is_not_a_concern(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeGh(threads=[_thread_page(1, False), _thread_page(2, True)])
    state = _collect(monkeypatch, tmp_path, fake)
    assert 2 not in [t["root_id"] for t in state["threads"]]


def test_a_single_page_of_threads_is_read_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_gh_json`` returns a bare object, not a list, when there is one page."""
    fake = _FakeGh(threads=_thread_page(1, True))
    state = _collect(monkeypatch, tmp_path, fake)
    assert [t["root_id"] for t in state["threads"]] == [2]


def test_every_read_is_paginated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeGh(threads=[])
    _collect(monkeypatch, tmp_path, fake)
    assert len(fake.listed) == 4  # inline, reviews, issue comments, review threads
    assert all("--paginate" in call for call in fake.listed)


def test_collect_state_rerequests_once_everything_is_answered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answers = [
        _comment(
            5, OCTOCAT, f"Fixed.\n{ANSWER}", reply_to=1, created="2026-10-26T18:00Z"
        ),
        _comment(
            6, OCTOCAT, f"Fixed.\n{ANSWER}", reply_to=2, created="2026-10-26T18:00Z"
        ),
    ]
    body_answer = {
        "id": 900,
        "user": OCTOCAT,
        "body": human_review.review_answer_marker(100),
    }
    fake = _FakeGh(inline=[*INLINE, *answers], comments=[body_answer], threads=[])
    state = _collect(monkeypatch, tmp_path, fake)
    assert state["open_concern_count"] == 0
    assert state["changes_requested_by"] == ["octocat"]
    assert state["rerequest_reviewers"] == ["octocat"]
    assert state["awaiting_rereview"] == []


def test_a_pending_review_request_is_awaited_not_repeated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeGh(requested=["octocat"], threads=[])
    state = _collect(monkeypatch, tmp_path, fake)
    assert state["awaiting_rereview"] == ["octocat"]
    assert state["rerequest_reviewers"] == []


def test_standing_requests_reads_every_page_of_reviews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_gh(args: list[str]) -> Any:
        calls.append(args)
        return [CR_REVIEW]

    monkeypatch.setattr(human_review, "_gh_json", fake_gh)
    assert human_review.standing_requests(REPO, 7, BOTS) == ["octocat"]
    assert calls == [["api", f"repos/{REPO}/pulls/7/reviews", "--paginate"]]


# --- reviewer config ---------------------------------------------------------


def test_no_config_excludes_only_github_bots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no reviewer configured (Step 1b), a plain user is human whatever its name."""
    monkeypatch.setattr(human_review, "DEFAULT_CONFIG_PATH", tmp_path / "absent.json")
    assert human_review.load_bot_logins(None) == frozenset()
    lookalike = _comment(8, {"login": "review-bot", "type": "User"}, "lint nit")
    fake = _FakeGh(inline=[*INLINE, lookalike], threads=[])
    monkeypatch.setattr(human_review, "_gh_json", fake)
    state = human_review.collect_state(REPO, 7, None)
    roots = [t["root_id"] for t in state["threads"]]
    assert 8 in roots  # a User is human when no config names it as a bot
    assert 3 not in roots  # a GitHub Bot account never is


def test_the_default_config_is_the_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    assert human_review.DEFAULT_CONFIG_PATH == reviewer_state.DEFAULT_CONFIG_PATH


def test_an_explicit_missing_config_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        human_review.load_bot_logins(tmp_path / "nope.json")


def test_an_invalid_config_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        human_review.load_bot_logins(bad)


def test_a_config_without_bot_logins_is_tolerated(tmp_path: Path) -> None:
    """The reader is defensive: a reviewer with no ``bot_logins`` adds nothing."""
    path = tmp_path / "pr-reviewer.json"
    path.write_text(json.dumps({"active": None, "reviewers": {"a": {}, "b": None}}))
    assert human_review.load_bot_logins(path) == frozenset()
    path.write_text(json.dumps({"active": None}))
    assert human_review.load_bot_logins(path) == frozenset()


# --- command line ------------------------------------------------------------


def test_help_exits_zero() -> None:
    result = _run_cli("--help")
    assert result.returncode == 0
    assert "--repo" in result.stdout and "--pr" in result.stdout


def test_short_help_exits_zero() -> None:
    assert _run_cli("-h").returncode == 0


def test_usage_error_without_a_pr() -> None:
    assert _run_cli().returncode == 64
    assert _run_cli("--repo", REPO).returncode == 64


def test_a_malformed_repo_is_a_usage_error() -> None:
    assert _run_cli("--repo", "no-slash", "--pr", "7").returncode == 64


def test_a_missing_explicit_config_is_a_usage_error(tmp_path: Path) -> None:
    result = _run_cli(
        "--repo", REPO, "--pr", "7", "--config", str(tmp_path / "nope.json")
    )
    assert result.returncode == 64
    assert "not found" in result.stderr


def test_an_invalid_explicit_config_is_a_usage_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    result = _run_cli("--repo", REPO, "--pr", "7", "--config", str(bad))
    assert result.returncode == 64
    assert "not valid JSON" in result.stderr


def test_main_prints_the_state_as_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(human_review, "_gh_json", _FakeGh(threads=[]))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "human_review.py",
            "--repo",
            REPO,
            "--pr",
            "7",
            "--config",
            str(_config(tmp_path)),
        ],
    )
    human_review.main()
    state = json.loads(capsys.readouterr().out)
    assert state["open_concern_count"] == 3
    assert state["answer_marker"] == ANSWER


def test_a_gh_failure_is_reported_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(args: list[str]) -> Any:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="HTTP 404")

    monkeypatch.setattr(human_review, "_gh_json", boom)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "human_review.py",
            "--repo",
            REPO,
            "--pr",
            "7",
            "--config",
            str(_config(tmp_path)),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        human_review.main()
    assert exc.value.code == 1
    assert "HTTP 404" in capsys.readouterr().err
