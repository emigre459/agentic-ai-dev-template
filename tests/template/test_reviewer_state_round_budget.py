"""Tests for the PR review-round budget in ``reviewer_state.py``.

``.github/pr-reviewer.json`` sets how many incremental reviewer rounds a PR gets
before and after its final full review (``review_rounds``). ``reviewer_state``
counts the trigger comments already on the PR and reports the budget, so the skills
act on a computed number instead of counting rounds themselves: a cap written only
in prose lets a PR run round after round.

Adapters are loaded from the real ``.github/pr-reviewer.json``; comment payloads are
literal GitHub API shapes, and ``collect_state`` runs against a fake ``gh``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "resolve-pr-concerns"
    / "scripts"
    / "reviewer_state.py"
)


def _load_module() -> ModuleType:
    """Load the standalone reviewer_state script as a module."""
    spec = importlib.util.spec_from_file_location("reviewer_state", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["reviewer_state"] = module
    spec.loader.exec_module(module)
    return module


reviewer_state = _load_module()
CODERABBIT = reviewer_state.load_reviewer(key="coderabbit")
BUGBOT = reviewer_state.load_reviewer(key="bugbot")
# The PR-incremental mechanics (triggers, carries, budgets) serve a reviewer with no
# local review; the CodeRabbit adapter reviews locally, so these tests drive a copy
# of it without one.
CODERABBIT_PR = CODERABBIT._replace(local_review=None, local_preflight=None)
RoundCaps = reviewer_state.RoundCaps


def _comment(body: str, at: str) -> dict[str, Any]:
    return {"body": body, "created_at": at, "user": {"login": "octocat"}}


TRIGGER = "@coderabbitai review"
FINAL = "@coderabbitai full review"


def _budget(comments: list[dict[str, Any]], caps: Any = None) -> Any:
    return reviewer_state.round_budget(
        comments, CODERABBIT, caps or RoundCaps(before_final=2, after_final=1)
    )


def test_a_fresh_pr_may_trigger() -> None:
    budget = _budget([])
    assert budget["phase"] == "before_final"
    assert budget["incremental_before_final"] == {"used": 0, "cap": 2}
    assert budget["trigger_allowed"] is True
    assert budget["final_review_due"] is False


def test_the_before_final_cap_makes_the_final_review_due() -> None:
    budget = _budget(
        [
            _comment(TRIGGER, "2026-09-29T01:00:00Z"),
            _comment(TRIGGER, "2026-09-29T02:00:00Z"),
        ]
    )
    assert budget["incremental_before_final"] == {"used": 2, "cap": 2}
    assert budget["trigger_allowed"] is False
    assert budget["final_review_due"] is True


def test_rounds_after_the_final_review_count_separately() -> None:
    comments = [
        _comment(TRIGGER, "2026-09-29T01:00:00Z"),
        _comment(FINAL, "2026-09-29T02:00:00Z"),
    ]
    budget = _budget(comments)
    assert budget["phase"] == "after_final"
    assert budget["incremental_after_final"] == {"used": 0, "cap": 1}
    assert budget["trigger_allowed"] is True
    comments.append(_comment(TRIGGER, "2026-09-29T03:00:00Z"))
    budget = _budget(comments)
    assert budget["incremental_after_final"] == {"used": 1, "cap": 1}
    assert budget["trigger_allowed"] is False
    assert budget["final_review_due"] is False


def test_only_exact_trigger_comments_count() -> None:
    """A comment that merely mentions the trigger is discussion, not a request."""
    budget = _budget(
        [
            _comment(f"Re-running: {TRIGGER}", "2026-09-29T01:00:00Z"),
            _comment(f"  {TRIGGER}\n", "2026-09-29T02:00:00Z"),
        ]
    )
    assert budget["incremental_before_final"]["used"] == 1


def test_a_reviewer_without_a_final_review_gets_one_combined_budget() -> None:
    comments = [_comment("bugbot run", f"2026-09-29T0{i}:00:00Z") for i in range(1, 4)]
    budget = reviewer_state.round_budget(
        comments, BUGBOT, RoundCaps(before_final=2, after_final=1)
    )
    assert budget["phase"] == "before_final"
    assert budget["incremental_before_final"] == {"used": 3, "cap": 3}
    assert budget["trigger_allowed"] is False
    assert budget["final_review_due"] is False


def test_a_raised_cap_allows_more_rounds() -> None:
    comments = [_comment(TRIGGER, f"2026-09-29T0{i}:00:00Z") for i in range(1, 4)]
    assert _budget(comments)["trigger_allowed"] is False
    raised = _budget(comments, RoundCaps(before_final=5, after_final=1))
    assert raised["trigger_allowed"] is True


def test_the_budget_passes_the_fix_iteration_limit_through() -> None:
    caps = RoundCaps(before_final=1, after_final=1, fix_iterations=7)
    assert _budget([], caps)["max_fix_iterations"] == 7


# --- A reviewer that reviews locally gets no PR incremental rounds ---------------


def test_a_local_reviewer_gets_no_pr_incremental_rounds() -> None:
    """Its fix rounds run locally; the PR's one review request is the final review."""
    caps = RoundCaps(before_final=2, after_final=3)
    assert CODERABBIT.local_review is not None
    local = reviewer_state.pr_round_caps(CODERABBIT, caps)
    assert (local.before_final, local.after_final) == (0, 0)
    # Only the PR incremental caps move: the local and fix-loop caps are untouched.
    assert local._replace(before_final=2, after_final=3) == caps
    assert reviewer_state.pr_round_caps(CODERABBIT_PR, caps) == caps
    budget = reviewer_state.round_budget([], CODERABBIT, local)
    assert budget["incremental_before_final"] == {"used": 0, "cap": 0}
    assert budget["trigger_allowed"] is False


def test_collect_state_gives_a_local_reviewer_no_pr_rounds(monkeypatch: Any) -> None:
    monkeypatch.setattr(reviewer_state, "_gh_json", _fake_gh([]))
    state = reviewer_state.collect_state(
        "owner/repo", 1, CODERABBIT, RoundCaps(before_final=2, after_final=3)
    )
    assert state["round_budget"]["incremental_before_final"] == {"used": 0, "cap": 0}
    assert state["round_budget"]["incremental_after_final"] == {"used": 0, "cap": 0}
    assert state["round_budget"]["trigger_allowed"] is False
    assert state["needs_trigger"] is False


# --- The caps come from the config ---------------------------------------------


def _write(tmp_path: Path, config: dict[str, Any]) -> Path:
    path = tmp_path / "pr-reviewer.json"
    path.write_text(json.dumps(config))
    return path


def _config(**rounds: Any) -> dict[str, Any]:
    config: dict[str, Any] = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    if rounds:
        config["review_rounds"] = rounds
    else:
        config.pop("review_rounds", None)
    return config


def test_the_repo_config_sets_the_caps() -> None:
    config = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    caps = reviewer_state.load_round_caps()
    assert caps == RoundCaps(
        before_final=config["review_rounds"]["max_incremental_before_final"],
        after_final=config["review_rounds"]["max_incremental_after_final"],
        fix_iterations=config["review_rounds"]["max_fix_iterations"],
        local_before_final=config["review_rounds"]["max_local_before_final"],
        local_after_final=config["review_rounds"]["max_local_after_final"],
    )
    assert caps == RoundCaps(before_final=1, after_final=3, fix_iterations=5)


def test_a_config_without_the_block_uses_the_defaults(tmp_path: Path) -> None:
    caps = reviewer_state.load_round_caps(_write(tmp_path, _config()))
    assert caps == reviewer_state.DEFAULT_ROUND_CAPS


def test_a_config_value_is_honored(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _config(
            max_incremental_before_final=4,
            max_incremental_after_final=2,
            max_fix_iterations=8,
        ),
    )
    assert reviewer_state.load_round_caps(path) == RoundCaps(
        before_final=4, after_final=2, fix_iterations=8
    )


@pytest.mark.parametrize("bad", [-1, "2", 1.5, True, None])
def test_a_bad_cap_fails_loudly(tmp_path: Path, bad: Any) -> None:
    path = _write(
        tmp_path,
        _config(max_incremental_before_final=bad, max_incremental_after_final=1),
    )
    with pytest.raises(ValueError, match="max_incremental_before_final"):
        reviewer_state.load_round_caps(path)


def test_an_unknown_key_fails_loudly(tmp_path: Path) -> None:
    """A misspelled knob must not silently fall back to the default."""
    path = _write(
        tmp_path,
        _config(
            max_incremental_before_final=2,
            max_incremental_after_final=1,
            max_incremental_before_finl=9,
        ),
    )
    with pytest.raises(ValueError, match="max_incremental_before_finl"):
        reviewer_state.load_round_caps(path)


# --- The live path and the CLI ---------------------------------------------------


def _fake_gh(comments: list[dict[str, Any]]) -> Any:
    def _gh_json(args: list[str]) -> Any:
        if "graphql" in args:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "headRefOid": "abc",
                            "mergeStateStatus": "CLEAN",
                            "commits": {"nodes": []},
                            "reviewThreads": {"nodes": []},
                        }
                    }
                }
            }
        if any("issues/1/comments" in a for a in args):
            return comments
        return []

    return _gh_json


def test_collect_state_reports_the_budget(monkeypatch: Any) -> None:
    """The live path carries the budget, so the skills never count rounds by hand."""
    monkeypatch.setattr(
        reviewer_state,
        "_gh_json",
        _fake_gh([_comment(TRIGGER, "2026-09-29T01:00:00Z")]),
    )
    state = reviewer_state.collect_state(
        "owner/repo", 1, CODERABBIT_PR, RoundCaps(before_final=1, after_final=1)
    )
    assert state["round_budget"]["incremental_before_final"] == {"used": 1, "cap": 1}
    assert state["round_budget"]["final_review_due"] is True
    assert state["round_budget"]["max_fix_iterations"] == 5


def _active_config(tmp_path: Path) -> Path:
    """Write the shipped config with a reviewer active, so ``main`` reaches it."""
    config: dict[str, Any] = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    config["active"] = "coderabbit"
    return _write(tmp_path, config)


def test_the_cli_can_raise_the_caps_for_one_run(
    monkeypatch: Any, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    def _collect(repo: str, pr: int, adapter: Any, caps: Any) -> dict[str, Any]:
        seen["caps"] = caps
        return {}

    config = _active_config(tmp_path)
    monkeypatch.setattr(reviewer_state, "collect_state", _collect)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reviewer_state.py",
            "--repo",
            "owner/repo",
            "--pr",
            "1",
            "--config",
            str(config),
            "--max-rounds-before-final",
            "5",
        ],
    )
    reviewer_state.main()
    # Only the flagged cap moves; the other comes from the repo config.
    assert seen["caps"] == reviewer_state.load_round_caps(config)._replace(
        before_final=5
    )


def test_the_cli_can_raise_the_after_final_cap(
    monkeypatch: Any, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    def _collect(repo: str, pr: int, adapter: Any, caps: Any) -> dict[str, Any]:
        seen["caps"] = caps
        return {}

    config = _active_config(tmp_path)
    monkeypatch.setattr(reviewer_state, "collect_state", _collect)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reviewer_state.py",
            "--repo",
            "owner/repo",
            "--pr",
            "1",
            "--config",
            str(config),
            "--max-rounds-after-final",
            "0",
        ],
    )
    reviewer_state.main()
    assert seen["caps"] == reviewer_state.load_round_caps(config)._replace(
        after_final=0
    )


@pytest.mark.parametrize(
    "flag", ["--max-rounds-before-final", "--max-rounds-after-final"]
)
@pytest.mark.parametrize("value", ["-1", "two"])
def test_the_cli_rejects_a_bad_cap(
    monkeypatch: Any, capsys: Any, flag: str, value: str
) -> None:
    """The CLI override follows the config's rule: a cap is a non-negative integer."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["reviewer_state.py", "--repo", "owner/repo", "--pr", "1", flag, value],
    )
    with pytest.raises(SystemExit) as exc:
        reviewer_state.main()
    assert exc.value.code == 2
    # The flag is known and its value rejected, not an unrecognized argument.
    assert f"argument {flag}:" in capsys.readouterr().err


# --- Replies that free a round ---------------------------------------------------

_REPLY_MARKER = "<!-- This is an auto-generated reply by CodeRabbit -->\n"


def _bot_reply(body: str, at: str) -> dict[str, Any]:
    return {
        "body": _REPLY_MARKER + body,
        "created_at": at,
        "updated_at": at,
        "user": {"login": "coderabbitai[bot]"},
    }


_INCREMENTAL_NOTE = (
    "\n\n> Note: CodeRabbit is an incremental review system and does not re-review "
    "already reviewed commits."
)


def test_a_failed_request_is_not_a_round() -> None:
    """A retry after a failure must not spend the budget."""
    comments = [
        _comment(TRIGGER, "2026-09-29T01:00:00Z"),
        _bot_reply(
            "<summary>❌ Action failed</summary>\n\nReview failed." + _INCREMENTAL_NOTE,
            "2026-09-29T01:00:05Z",
        ),
        _comment(TRIGGER, "2026-09-29T01:30:00Z"),
    ]
    budget = _budget(comments)
    assert budget["incremental_before_final"]["used"] == 1
    assert budget["trigger_allowed"] is True


def test_a_not_completed_request_still_counts() -> None:
    """A "not completed" reply often means only the summary failed; the review posted."""
    comments = [
        _comment(TRIGGER, "2026-09-29T01:00:00Z"),
        _bot_reply(
            "<summary>⚠️ Action not completed</summary>" + _INCREMENTAL_NOTE,
            "2026-09-29T01:00:05Z",
        ),
    ]
    assert _budget(comments)["incremental_before_final"]["used"] == 1


def test_a_trigger_tied_with_the_final_request_is_counted_once() -> None:
    """Partition by position, not by strict timestamp comparison."""
    budget = _budget(
        [
            _comment(TRIGGER, "2026-09-29T01:00:00Z"),
            _comment(FINAL, "2026-09-29T01:00:00Z"),
        ]
    )
    used = (
        budget["incremental_before_final"]["used"]
        + budget["incremental_after_final"]["used"]
    )
    assert used == 1


_NO_CHANGES = "CodeRabbit is an incremental review system ... No files to review."


def test_a_trigger_answered_with_no_changes_is_not_a_round() -> None:
    """A rebase-only head the reviewer had nothing new on costs no round."""
    comments = [
        _comment(TRIGGER, "2026-09-29T01:00:00Z"),
        _comment(TRIGGER, "2026-09-29T02:00:00Z"),
        _bot_reply(_NO_CHANGES, "2026-09-29T02:00:05Z"),
    ]
    budget = _budget(comments)
    assert budget["incremental_before_final"] == {"used": 1, "cap": 2}
    assert budget["trigger_allowed"] is True
    # With no verdict to carry, re-posting gets the same answer forever: counted.
    bounded = reviewer_state.round_budget(
        comments,
        CODERABBIT,
        RoundCaps(before_final=2, after_final=1),
        free_no_changes=False,
    )
    assert bounded["incremental_before_final"] == {"used": 2, "cap": 2}


# --- A rate-limit reply is a wait, never a spent round ---------------------------

# The reviewer's replies when it refuses a trigger and then the final-review request
# that followed it for its rate limit (observed shapes, trimmed).
_RATE_LIMITED_INCREMENTAL = (
    "<details>\n<summary>⚠️ Action not completed</summary>\n\nReview rate limited."
    + _INCREMENTAL_NOTE
    + " This command is applicable only when automatic reviews are paused.\n\n"
    "</details>"
)
_RATE_LIMITED_FULL = (
    "<details>\n<summary>⚠️ Action not completed</summary>\n\nReview rate limited.\n\n"
    "---\n\nYour included review limit is currently reached under our Fair Usage "
    "Limits Policy. This review may still proceed through usage-based billing if "
    "eligible. Your next included review will be available in 13 minutes.\n\n"
    "</details>"
)
_REFUSED_PAIR = [
    _comment(TRIGGER, "2026-10-13T07:25:49Z"),
    _bot_reply(_RATE_LIMITED_INCREMENTAL, "2026-10-13T07:25:58Z"),
    _comment(FINAL, "2026-10-13T07:26:53Z"),
    _bot_reply(_RATE_LIMITED_FULL, "2026-10-13T07:27:03Z"),
]
_REPO_CAPS = RoundCaps(before_final=1, after_final=3)


def test_a_rate_limited_trigger_spends_no_round() -> None:
    """Counting it would make the final review "due", inviting a second refusal."""
    budget = _budget(_REFUSED_PAIR[:2], _REPO_CAPS)
    assert budget["incremental_before_final"] == {"used": 0, "cap": 1}
    assert budget["trigger_allowed"] is True
    assert budget["final_review_due"] is False


def test_a_rate_limited_final_request_does_not_start_the_after_final_phase() -> None:
    """The final review never ran, so the PR is still before it."""
    budget = _budget(_REFUSED_PAIR, _REPO_CAPS)
    assert budget["phase"] == "before_final"
    assert budget["incremental_before_final"]["used"] == 0
    assert budget["final_review_due"] is False


def test_a_final_review_after_a_rate_limited_one_starts_the_phase() -> None:
    comments = [
        *_REFUSED_PAIR,
        _comment(FINAL, "2026-10-13T13:35:30Z"),
        _bot_reply(
            "<summary>✅ Action performed</summary>\n\nFull review finished.",
            "2026-10-13T13:35:39Z",
        ),
        _comment(TRIGGER, "2026-10-13T14:00:00Z"),
    ]
    budget = _budget(comments, _REPO_CAPS)
    assert budget["phase"] == "after_final"
    assert budget["incremental_after_final"]["used"] == 1


def test_the_rate_limit_reply_is_still_read_as_not_completed() -> None:
    """The backstop is budget-only: the outcomes and the gates are unchanged."""
    outcome = reviewer_state.incremental_review(_REFUSED_PAIR[:2], CODERABBIT)
    assert outcome.outcome == "not_completed"


# A final-review request refused for the allowance under "Review failed", with no
# "Review rate limited" line (observed shape, trimmed).
_RATE_LIMITED_FULL_FAILED = (
    "<details>\n<summary>❌ Action failed</summary>\n\nReview failed.\n\n---\n\n"
    "Your included review limit is currently reached under our Fair Usage Limits "
    "Policy. This review may still proceed through usage-based billing if "
    "eligible. Your next included review will be available in 47 minutes.\n\n"
    "</details>"
)
_REFUSED_AS_FAILED = [
    _comment(FINAL, "2026-10-14T22:24:49Z"),
    _bot_reply(_RATE_LIMITED_FULL_FAILED, "2026-10-14T22:25:12Z"),
]


def test_a_review_failed_for_the_allowance_is_a_refusal() -> None:
    """Read as a failed final review, it would invite an immediate re-post."""
    state = reviewer_state.rate_limit_state(_REFUSED_AS_FAILED, CODERABBIT)
    assert state == {
        "refused": True,
        "request": "final",
        "replied_at": "2026-10-14T22:25:12Z",
        "retry_after": "2026-10-14T23:12:12Z",
        "wait_source": "reply",
    }


def test_a_final_request_failed_for_the_allowance_stays_before_final() -> None:
    budget = _budget(_REFUSED_AS_FAILED, _REPO_CAPS)
    assert budget["phase"] == "before_final"
    assert budget["final_review_due"] is False


# The same allowance sentence on a final review that ran anyway (usage-based
# billing), under "Action performed".
_ALLOWANCE_NOTE_ON_A_TRIGGERED_REVIEW = (
    "<details>\n<summary>Action performed</summary>\n\nFull review triggered.\n\n"
    "---\n\nYour included review limit is currently reached under our Fair Usage "
    "Limits Policy. This review may still proceed through usage-based billing if "
    "eligible. Your next included review will be available in 16 minutes.\n\n"
    "</details>"
)


def test_an_allowance_note_on_a_triggered_review_is_no_refusal() -> None:
    """The review ran, so waiting and re-posting would request it twice."""
    comments = [
        _comment(FINAL, "2026-10-14T23:56:08Z"),
        _bot_reply(_ALLOWANCE_NOTE_ON_A_TRIGGERED_REVIEW, "2026-10-14T23:56:51Z"),
    ]
    state = reviewer_state.rate_limit_state(comments, CODERABBIT)
    assert state["refused"] is False
    assert state["request"] == "final"
    budget = _budget(comments, _REPO_CAPS)
    assert budget["phase"] == "after_final"


def test_collect_state_budget_ignores_a_refused_final_request(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(reviewer_state, "_gh_json", _fake_gh(_REFUSED_PAIR))
    state = reviewer_state.collect_state("owner/repo", 1, CODERABBIT_PR, _REPO_CAPS)
    assert state["rate_limit"]["refused"] is True
    assert state["rate_limit"]["retry_after"] == "2026-10-13T07:40:03Z"
    assert state["round_budget"]["phase"] == "before_final"
    assert state["round_budget"]["final_review_due"] is False
