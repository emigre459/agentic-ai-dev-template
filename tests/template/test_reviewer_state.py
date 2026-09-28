"""Tests for the resolve-pr-concerns skill's authoritative PR-reviewer state reader.

The script lives outside the importable package tree
(``.agents/skills/resolve-pr-concerns/scripts/reviewer_state.py``), so it is loaded
by path. The verdict/finding seams are pure functions over GitHub API payloads
plus a reviewer adapter loaded from the real ``.github/pr-reviewer.json``, so the
tests hand them literal review and review-thread nodes rather than hitting the
network. Every adapter in the config is exercised, not just the active one.
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
    # Registered before exec so the NamedTuple adapter resolves its module the same
    # way review_gate's by-name import does.
    sys.modules["reviewer_state"] = module
    spec.loader.exec_module(module)
    return module


reviewer_state = _load_module()
review_verdict = reviewer_state.review_verdict
open_findings = reviewer_state.open_findings
load_reviewer = reviewer_state.load_reviewer

BUGBOT = load_reviewer(key="bugbot")
CODERABBIT = load_reviewer(key="coderabbit")

MARKER = "<!-- BUGBOT_REVIEW -->"
SHA = "abc123"


def _review(
    body: str,
    login: str = "cursor[bot]",
    commit: str | None = SHA,
    submitted: str = "2026-08-11T10:00:00Z",
) -> dict[str, Any]:
    return {
        "body": body,
        "user": {"login": login},
        "commit_id": commit,
        "submitted_at": submitted,
    }


# --- Bugbot adapter: the original behavior, kept as regressions --------------


def test_verdict_none_when_no_review() -> None:
    assert review_verdict([], SHA, BUGBOT) == ("none", 0)


def test_verdict_pass_on_clean_review() -> None:
    reviews = [_review(f"{MARKER}\nBugbot found no new issues.")]
    assert review_verdict(reviews, SHA, BUGBOT) == ("pass", 0)


def test_verdict_counts_findings() -> None:
    reviews = [_review(f"{MARKER}\nBugbot found 3 potential issues.")]
    assert review_verdict(reviews, SHA, BUGBOT) == ("findings", 3)


def test_verdict_ignores_review_on_a_stale_sha() -> None:
    """A review on an older head does not vouch for the current code."""
    reviews = [_review(f"{MARKER}\nfound no new issues", commit="oldsha")]
    assert review_verdict(reviews, SHA, BUGBOT) == ("none", 0)


def test_verdict_accepts_cursor_bot_as_summary_author() -> None:
    """Observed on a real PR: the summary review is `cursor[bot]`.

    Filtering on `cursor` alone reported "never reviewed" for a clean review,
    which would have deadlocked every merge.
    """
    reviews = [_review(f"{MARKER}\nfound no new issues", login="cursor[bot]")]
    assert review_verdict(reviews, SHA, BUGBOT) == ("pass", 0)


def test_verdict_accepts_bare_cursor_as_summary_author() -> None:
    reviews = [_review(f"{MARKER}\nfound no new issues", login="cursor")]
    assert review_verdict(reviews, SHA, BUGBOT) == ("pass", 0)


def test_verdict_ignores_non_reviewer_author() -> None:
    reviews = [_review(f"{MARKER}\nfound no new issues", login="copilot")]
    assert review_verdict(reviews, SHA, BUGBOT) == ("none", 0)


def test_verdict_ignores_review_without_marker() -> None:
    reviews = [_review("LGTM, found no new issues")]
    assert review_verdict(reviews, SHA, BUGBOT) == ("none", 0)


def test_verdict_uses_newest_review_by_submitted_at() -> None:
    reviews = [
        _review(
            f"{MARKER}\nfound 2 potential issues", submitted="2026-08-11T10:00:00Z"
        ),
        _review(f"{MARKER}\nfound no new issues", submitted="2026-08-11T11:00:00Z"),
    ]
    assert review_verdict(reviews, SHA, BUGBOT) == ("pass", 0)


def test_bugbot_ignores_a_body_sha_because_its_pattern_is_null() -> None:
    """Bugbot matches on `commit_id` only -- unchanged from the pre-seam script."""
    reviews = [_review(f"{MARKER}\nfound no new issues for commit abc123", commit=None)]
    assert review_verdict(reviews, SHA, BUGBOT) == ("none", 0)


# --- CodeRabbit adapter ------------------------------------------------------


def _rabbit(body: str, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("login", "coderabbitai[bot]")
    return _review(body, **kwargs)


def test_coderabbit_clean_review_passes() -> None:
    reviews = [_rabbit("No actionable comments were generated in the recent review.")]
    assert review_verdict(reviews, SHA, CODERABBIT) == ("pass", 0)


def test_coderabbit_counts_actionable_comments() -> None:
    reviews = [_rabbit("**Actionable comments posted: 2**\n\n<details>...</details>")]
    assert review_verdict(reviews, SHA, CODERABBIT) == ("findings", 2)


def test_coderabbit_accepts_both_logins() -> None:
    body = "**Actionable comments posted: 1**"
    for login in ("coderabbitai", "coderabbitai[bot]"):
        assert review_verdict([_rabbit(body, login=login)], SHA, CODERABBIT) == (
            "findings",
            1,
        )


def test_coderabbit_ignores_a_cursor_review() -> None:
    reviews = [_review(f"{MARKER}\nfound no new issues", login="cursor[bot]")]
    assert review_verdict(reviews, SHA, CODERABBIT) == ("none", 0)


def test_markerless_adapter_ignores_reviews_matching_neither_pattern() -> None:
    """With no summary marker, a later reply-style review must not shadow the summary."""
    reviews = [
        _rabbit("**Actionable comments posted: 1**", submitted="2026-09-25T10:00:00Z"),
        _rabbit("Nit reply on a thread", submitted="2026-09-25T11:00:00Z"),
    ]
    assert review_verdict(reviews, SHA, CODERABBIT) == ("findings", 1)


def test_reviewed_sha_pattern_matches_short_sha_prefix() -> None:
    head = "abc1234" + "0" * 33
    body = (
        "**Actionable comments posted: 1**\n"
        "Reviewing files that changed from the base of the PR and between "
        "1111111 and abc1234."
    )
    reviews = [_rabbit(body, commit=None)]
    assert review_verdict(reviews, head, CODERABBIT) == ("findings", 1)


def test_reviewed_sha_pattern_rejects_a_different_head() -> None:
    head = "fff9999" + "0" * 33
    body = "**Actionable comments posted: 1**\nbetween 1111111 and abc1234."
    assert review_verdict([_rabbit(body, commit=None)], head, CODERABBIT) == (
        "none",
        0,
    )


# --- Inline findings ---------------------------------------------------------


def _thread(
    resolved: bool,
    login: str = "cursor[bot]",
    path: str = "src/a.py",
    line: int = 10,
    body: str = "Bug here",
) -> dict[str, Any]:
    return {
        "id": "PRRT_1",
        "isResolved": resolved,
        "comments": {
            "nodes": [
                {"author": {"login": login}, "path": path, "line": line, "body": body}
            ]
        },
    }


def test_open_findings_skips_resolved() -> None:
    assert open_findings([_thread(True)], BUGBOT) == []


def test_open_findings_returns_unresolved_reviewer_thread() -> None:
    result = open_findings([_thread(False)], BUGBOT)
    assert len(result) == 1
    assert result[0]["path"] == "src/a.py"
    assert result[0]["thread_id"] == "PRRT_1"


def test_open_findings_accepts_an_inline_thread_authored_by_cursor() -> None:
    """Observed on a real PR: the INLINE thread was `cursor`.

    The summary/inline login split is not stable, and this is the same defect that
    deadlocked the summary path. With the filter pinned to `cursor[bot]` alone, a real
    High-severity finding reported by the review summary came back as an empty
    findings list -- so the agent was never handed the finding it had to address.
    """
    result = open_findings([_thread(False, login="cursor")], BUGBOT)
    assert len(result) == 1
    assert result[0]["path"] == "src/a.py"


def test_open_findings_ignores_other_authors() -> None:
    """Copilot and human threads block merge too, but they are not reviewer findings."""
    assert open_findings([_thread(False, login="copilot")], BUGBOT) == []


def test_open_findings_follow_the_adapter_logins() -> None:
    threads = [_thread(False, login="coderabbitai"), _thread(False, login="cursor")]
    result = open_findings(threads, CODERABBIT)
    assert len(result) == 1


def test_open_findings_tolerates_empty_comment_list() -> None:
    assert (
        open_findings(
            [{"id": "x", "isResolved": False, "comments": {"nodes": []}}], BUGBOT
        )
        == []
    )


# --- Loader: fail hard on a bad config ---------------------------------------


def _valid_entry() -> dict[str, Any]:
    return json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())["reviewers"][
        "bugbot"
    ]


def _write(tmp_path: Path, config: Any) -> Path:
    path = tmp_path / "pr-reviewer.json"
    path.write_text(config if isinstance(config, str) else json.dumps(config))
    return path


def _config_with(**entry_overrides: Any) -> dict[str, Any]:
    entry = _valid_entry()
    entry.update(entry_overrides)
    return {"active": "x", "reviewers": {"x": entry}}


def test_loader_reads_the_active_reviewer_by_default() -> None:
    active = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())["active"]
    assert load_reviewer().key == active


def test_loader_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="pr-reviewer"):
        load_reviewer(tmp_path / "absent.json")


def test_loader_bad_json_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        load_reviewer(_write(tmp_path, "{not json"))


def test_loader_unknown_active_lists_known_keys(tmp_path: Path) -> None:
    config = {"active": "nope", "reviewers": {"bugbot": _valid_entry()}}
    with pytest.raises(ValueError, match=r"'nope'.*\['bugbot'\]"):
        load_reviewer(_write(tmp_path, config))


def test_loader_missing_check_name_raises(tmp_path: Path) -> None:
    config = _config_with()
    del config["reviewers"]["x"]["check_name"]
    with pytest.raises(ValueError, match="check_name"):
        load_reviewer(_write(tmp_path, config))


def test_loader_missing_optional_field_raises(tmp_path: Path) -> None:
    """Nullable fields must still be present -- an absent key is a typo, not a null."""
    config = _config_with()
    del config["reviewers"]["x"]["summary_marker"]
    with pytest.raises(ValueError, match="summary_marker"):
        load_reviewer(_write(tmp_path, config))


def test_loader_findings_pattern_needs_one_group(tmp_path: Path) -> None:
    config = _config_with(findings_pattern="found potential issues")
    with pytest.raises(ValueError, match="capture group"):
        load_reviewer(_write(tmp_path, config))


def test_loader_reviewed_sha_pattern_needs_one_group(tmp_path: Path) -> None:
    config = _config_with(reviewed_sha_pattern="(a)(b)")
    with pytest.raises(ValueError, match="capture group"):
        load_reviewer(_write(tmp_path, config))


def test_loader_invalid_regex_raises(tmp_path: Path) -> None:
    config = _config_with(clean_pattern="(unclosed")
    with pytest.raises(ValueError, match="clean_pattern"):
        load_reviewer(_write(tmp_path, config))


def test_loader_empty_bot_logins_raises(tmp_path: Path) -> None:
    config = _config_with(bot_logins=[])
    with pytest.raises(ValueError, match="bot_logins"):
        load_reviewer(_write(tmp_path, config))


def test_loader_non_bool_self_resolves_raises(tmp_path: Path) -> None:
    config = _config_with(self_resolves_threads="yes")
    with pytest.raises(ValueError, match="self_resolves_threads"):
        load_reviewer(_write(tmp_path, config))


def test_loader_final_review_fields_must_be_present(tmp_path: Path) -> None:
    config = _config_with()
    del config["reviewers"]["x"]["command_reply_marker"]
    with pytest.raises(ValueError, match="command_reply_marker"):
        load_reviewer(_write(tmp_path, config))


def test_loader_final_review_fields_are_all_or_nothing(tmp_path: Path) -> None:
    config = _config_with(final_review_comment="@bot full review")
    with pytest.raises(ValueError, match="all be set or all be null"):
        load_reviewer(_write(tmp_path, config))


def test_loader_final_review_pattern_must_compile(tmp_path: Path) -> None:
    config = _config_with(
        final_review_comment="@bot full review",
        command_reply_marker="<!-- reply -->",
        final_review_done_pattern="(unclosed",
        final_review_failed_pattern="failed",
    )
    with pytest.raises(ValueError, match="final_review_done_pattern"):
        load_reviewer(_write(tmp_path, config))


def test_coderabbit_adapter_carries_the_final_review_fields() -> None:
    assert CODERABBIT.final_review_comment == "@coderabbitai full review"
    assert CODERABBIT.command_reply_marker == (
        "<!-- This is an auto-generated reply by CodeRabbit -->"
    )
    done, failed = (
        CODERABBIT.final_review_done_pattern,
        CODERABBIT.final_review_failed_pattern,
    )
    assert done is not None and failed is not None
    assert done.search("Full review finished.")
    assert not done.search("Review finished.")
    assert failed.search("Review failed.")


def test_bugbot_has_no_final_review_and_self_resolves() -> None:
    assert BUGBOT.final_review_comment is None
    assert BUGBOT.command_reply_marker is None
    assert BUGBOT.final_review_done_pattern is None
    assert BUGBOT.final_review_failed_pattern is None
    assert BUGBOT.self_resolves_threads is True


# --- collect_state -----------------------------------------------------------


def test_the_query_requests_merge_state_status() -> None:
    """An agent decides whether to clear a BEHIND base from THIS tool's output.

    Guard-by-construction: if the instructions key on `mergeStateStatus` while the
    tool they name does not return it, a literal pass skips the branch update.
    Trimming this field from the query would do that silently -- the agent would
    read `None` and conclude "not BEHIND".
    """
    # Asserted as a REQUESTED FIELD -- a bare substring check passes on the strength
    # of any comment or docstring that merely names the field, which is how the first
    # version of this guard passed against a query with the field deleted.
    requested = {line.strip() for line in reviewer_state._THREADS_QUERY.splitlines()}
    assert "mergeStateStatus" in requested


def test_collect_state_surfaces_merge_state_and_reviewer(monkeypatch: Any) -> None:
    """The emitted payload carries the field and the reviewer the agent must act on."""

    def _fake_gh_json(args: list[str]) -> Any:
        if "graphql" in args:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "headRefOid": "abc123",
                            "mergeStateStatus": "BEHIND",
                            "reviewThreads": {"nodes": []},
                        }
                    }
                }
            }
        return []

    monkeypatch.setattr(reviewer_state, "_gh_json", _fake_gh_json)
    state = reviewer_state.collect_state("octo/example", 7, BUGBOT)
    assert state["merge_state_status"] == "BEHIND"
    assert state["head_sha"] == "abc123"
    assert state["reviewer"] == "bugbot"
    assert state["reviewer_name"] == "Cursor Bugbot"
    assert state["check_name"] == "Cursor Bugbot"
    assert state["trigger_comment"] == "bugbot run"
    assert state["self_resolves_threads"] is True
    assert state["open_findings"] == []
    assert state["needs_trigger"] is True
    assert state["final_review_outcome"] == "not_required"
    assert state["needs_final_review"] is False


def _fake_gh(reviews: list[Any], comments: list[Any], calls: list[str]) -> Any:
    def _gh_json(args: list[str]) -> Any:
        calls.append(" ".join(args))
        if "graphql" in args:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "headRefOid": RABBIT_HEAD,
                            "mergeStateStatus": "CLEAN",
                            "reviewThreads": {"nodes": []},
                        }
                    }
                }
            }
        if any(a.endswith("/reviews") for a in args):
            return reviews
        if any(a.endswith("/comments") for a in args):
            return comments
        raise AssertionError(f"unexpected gh call: {args}")

    return _gh_json


# Shapes observed on a real PR: CodeRabbit's clean incremental
# re-review posts an EMPTY-body review, and the verdict + reviewed range live only
# in its walkthrough issue-comment (edited in place).
RABBIT_HEAD = "54e760cc73a0cd43acb5e864bbae6bc7acf6828f"
_WALKTHROUGH_CLEAN = (
    "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
    "No actionable comments were generated in the recent review. 🎉\n"
    "Reviewing files that changed from the base of the PR and between "
    f"675be3972f9e0016dc579c5aaef857e47d9ff5e6 and {RABBIT_HEAD}."
)


def _walkthrough(body: str, updated: str = "2026-09-25T04:23:50Z") -> dict[str, Any]:
    return {
        "body": body,
        "user": {"login": "coderabbitai[bot]"},
        "created_at": "2026-09-25T04:04:39Z",
        "updated_at": updated,
    }


def test_coderabbit_clean_verdict_is_read_from_its_walkthrough_comment(
    monkeypatch: Any,
) -> None:
    reviews = [
        _rabbit(
            "**Actionable comments posted: 1**",
            commit="675be3972f9e0016dc579c5aaef857e47d9ff5e6",
            submitted="2026-09-25T04:12:22Z",
        ),
        _rabbit("", commit=RABBIT_HEAD, submitted="2026-09-25T04:20:53Z"),
    ]
    calls: list[str] = []
    monkeypatch.setattr(
        reviewer_state,
        "_gh_json",
        _fake_gh(reviews, [_walkthrough(_WALKTHROUGH_CLEAN)], calls),
    )
    state = reviewer_state.collect_state("octo/example", 7, CODERABBIT)
    assert state["verdict"] == "pass"
    assert state["needs_trigger"] is False


def test_a_walkthrough_naming_an_older_head_does_not_vouch_for_the_new_one(
    monkeypatch: Any,
) -> None:
    """A push lands before the re-review: the walkthrough still names the old range."""
    stale = _WALKTHROUGH_CLEAN.replace(RABBIT_HEAD, "1" * 40)
    calls: list[str] = []
    monkeypatch.setattr(
        reviewer_state, "_gh_json", _fake_gh([], [_walkthrough(stale)], calls)
    )
    state = reviewer_state.collect_state("octo/example", 7, CODERABBIT)
    assert state["verdict"] == "none"
    assert state["needs_trigger"] is True


def test_bugbot_never_reads_issue_comments(monkeypatch: Any) -> None:
    """Bugbot's summary is a review; fetching issue-comments would change its behavior."""
    calls: list[str] = []
    comments = [_walkthrough(f"{MARKER}\nfound no new issues")]
    monkeypatch.setattr(reviewer_state, "_gh_json", _fake_gh([], comments, calls))
    state = reviewer_state.collect_state("octo/example", 1, BUGBOT)
    assert state["verdict"] == "none"
    assert not any("issues/" in call for call in calls)


def test_loader_non_bool_summary_in_issue_comments_raises(tmp_path: Path) -> None:
    config = _config_with(summary_in_issue_comments="no")
    with pytest.raises(ValueError, match="summary_in_issue_comments"):
        load_reviewer(_write(tmp_path, config))


def test_coderabbit_counts_outside_diff_only_findings() -> None:
    """Count findings that live only in an outside-diff review body.

    Observed shape: no `Actionable comments posted` line and no
    inline thread. Reading that as "none" would re-trigger forever on a reviewed head.
    """
    body = (
        "> [!CAUTION]\n> Some comments are outside the diff and can’t be posted "
        "inline due to GitHub limitations.\n> \n> **⚠️ Outside diff range comments (1)**"
    )
    assert review_verdict([_rabbit(body)], SHA, CODERABBIT) == ("findings", 1)


def test_coderabbit_actionable_count_wins_when_both_are_present() -> None:
    body = "**Actionable comments posted: 2**\n\n**⚠️ Outside diff range comments (1)**"
    assert review_verdict([_rabbit(body)], SHA, CODERABBIT) == ("findings", 2)


# --- Final full review: classified from the reviewer's command replies ---------
# Reply bodies as CodeRabbit posts them (observed on a CodeRabbit trial): a reply
# comment is created within seconds of the command and edited in place when the
# review ends.

_REPLY = "<!-- This is an auto-generated reply by CodeRabbit -->\n"
_FULL_DONE = (
    _REPLY + "<details>\n<summary>✅ Action performed</summary>\n\n"
    "Full review finished.\n\n</details>"
)
_FULL_FAILED = (
    _REPLY + "<details>\n<summary>❌ Action failed</summary>\n\n"
    "Review failed.\n\n</details>"
)
_NOT_COMPLETED = _REPLY + (
    "<details>\n<summary>⚠️ Action not completed</summary>\n\n"
    "Deferred architecture/priority summary could not be published.\n\n</details>"
)
_INCREMENTAL_DONE = _REPLY + (
    "<details>\n<summary>✅ Action performed</summary>\n\nReview finished.\n\n"
    "> Note: CodeRabbit is an incremental review system and does not re-review "
    "already reviewed commits.\n\n</details>"
)
_IN_PROGRESS = _REPLY + "Full review triggered."


def _comment(
    body: str,
    created: str,
    updated: str | None = None,
    login: str = "coderabbitai[bot]",
) -> dict[str, Any]:
    return {
        "body": body,
        "user": {"login": login},
        "created_at": created,
        "updated_at": updated or created,
    }


def _command(created: str, body: str = "@coderabbitai full review") -> dict[str, Any]:
    return _comment(body, created, login="a-human")


def _outcome(
    comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]] | None = None,
    adapter: Any = CODERABBIT,
) -> str:
    return str(reviewer_state.final_review_outcome(comments, reviews or [], adapter))


def test_final_review_not_required_without_a_configured_comment() -> None:
    assert _outcome([_command("2026-09-27T17:59:45Z")], adapter=BUGBOT) == (
        "not_required"
    )


def test_final_review_not_requested_without_a_command() -> None:
    comments = [_comment(_INCREMENTAL_DONE, "2026-09-28T01:40:34Z")]
    assert _outcome(comments) == "not_requested"


def test_final_review_finished() -> None:
    comments = [
        _command("2026-09-27T17:59:45Z"),
        _comment(_FULL_DONE, "2026-09-27T17:59:53Z", "2026-09-27T18:11:04Z"),
    ]
    assert _outcome(comments) == "finished"


def test_final_review_failed() -> None:
    comments = [
        _command("2026-09-27T18:25:34Z"),
        _comment(_FULL_FAILED, "2026-09-27T18:25:43Z", "2026-09-27T18:25:46Z"),
    ]
    assert _outcome(comments) == "failed"


def test_final_review_pending_until_the_reply_lands() -> None:
    assert _outcome([_command("2026-09-27T18:25:34Z")]) == "pending"
    comments = [
        _command("2026-09-27T18:25:34Z"),
        _comment(_IN_PROGRESS, "2026-09-27T18:25:43Z"),
    ]
    assert _outcome(comments) == "pending"


def test_not_completed_counts_when_its_review_posted() -> None:
    """Findings posted at 17:21 and only the summary failed (reply edited 17:23)."""
    comments = [
        _command("2026-09-27T17:13:15Z"),
        _comment(_NOT_COMPLETED, "2026-09-27T17:13:23Z", "2026-09-27T17:23:20Z"),
    ]
    review = _rabbit(
        "**Actionable comments posted: 1**", submitted="2026-09-27T17:21:02Z"
    )
    assert _outcome(comments, [review]) == "finished"
    assert _outcome(comments) == "pending"


def test_a_review_outside_the_window_does_not_rescue_not_completed() -> None:
    comments = [
        _command("2026-09-27T17:13:15Z"),
        _comment(_NOT_COMPLETED, "2026-09-27T17:13:23Z", "2026-09-27T17:23:20Z"),
    ]
    early = _rabbit(
        "**Actionable comments posted: 1**", submitted="2026-09-27T17:00:00Z"
    )
    assert _outcome(comments, [early]) == "pending"


def test_incremental_reply_never_counts_as_a_full_review() -> None:
    comments = [
        _command("2026-09-28T01:40:25Z"),
        _comment(_INCREMENTAL_DONE, "2026-09-28T01:40:34Z", "2026-09-28T01:44:54Z"),
    ]
    assert _outcome(comments) == "pending"


def test_a_human_pasting_the_reply_text_does_not_count() -> None:
    comments = [
        _command("2026-09-27T17:59:45Z"),
        _comment(_FULL_DONE, "2026-09-27T18:00:00Z", login="a-human"),
    ]
    assert _outcome(comments) == "pending"


def test_quoting_the_command_is_not_a_request() -> None:
    body = "Next: `@coderabbitai full review`"
    assert _outcome([_command("2026-09-27T17:59:45Z", body=body)]) == "not_requested"


def test_failed_then_retried_is_finished() -> None:
    comments = [
        _command("2026-09-27T18:25:34Z"),
        _comment(_FULL_FAILED, "2026-09-27T18:25:43Z", "2026-09-27T18:25:46Z"),
        _command("2026-09-27T18:52:25Z"),
        _comment(_FULL_DONE, "2026-09-27T18:52:33Z", "2026-09-27T19:06:28Z"),
    ]
    assert _outcome(comments) == "finished"


def test_a_stray_later_request_does_not_reblock_a_finished_review() -> None:
    comments = [
        _command("2026-09-27T17:59:45Z"),
        _comment(_FULL_DONE, "2026-09-27T17:59:53Z", "2026-09-27T18:11:04Z"),
        _command("2026-09-27T18:25:34Z"),
        _comment(_FULL_FAILED, "2026-09-27T18:25:43Z", "2026-09-27T18:25:46Z"),
    ]
    assert _outcome(comments) == "finished"


def test_collect_state_reports_the_final_review(monkeypatch: Any) -> None:
    calls: list[str] = []
    comments = [
        _walkthrough(_WALKTHROUGH_CLEAN),
        _command("2026-09-25T04:30:00Z"),
        _comment(_FULL_DONE, "2026-09-25T04:30:08Z", "2026-09-25T04:40:00Z"),
    ]
    monkeypatch.setattr(reviewer_state, "_gh_json", _fake_gh([], comments, calls))
    state = reviewer_state.collect_state("octo/example", 7, CODERABBIT)
    assert state["verdict"] == "pass"
    assert state["final_review_comment"] == "@coderabbitai full review"
    assert state["final_review_outcome"] == "finished"
    assert state["needs_final_review"] is False
    assert sum("issues/" in call for call in calls) == 1  # fetched once, reused


def test_collect_state_needs_the_final_review_until_requested(
    monkeypatch: Any,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        reviewer_state,
        "_gh_json",
        _fake_gh([], [_walkthrough(_WALKTHROUGH_CLEAN)], calls),
    )
    state = reviewer_state.collect_state("octo/example", 7, CODERABBIT)
    assert state["final_review_outcome"] == "not_requested"
    assert state["needs_final_review"] is True


# Final review: an incremental command interleaved with the full one. Both
# replies carry the same marker, so pairing must not stop at the first one.

_INCREMENTAL_NOT_COMPLETED = _REPLY + (
    "<details>\n<summary>⚠️ Action not completed</summary>\n\n"
    "Deferred architecture/priority summary could not be published.\n\n"
    "> Note: CodeRabbit is an incremental review system and does not re-review "
    "already reviewed commits.\n\n</details>"
)
_INCREMENTAL_FAILED = _REPLY + (
    "<details>\n<summary>❌ Action failed</summary>\n\nReview failed.\n\n"
    "> Note: CodeRabbit is an incremental review system and does not re-review "
    "already reviewed commits.\n\n</details>"
)


def _interleaved(full_reply: str) -> list[dict[str, Any]]:
    return [
        _command("2026-09-28T10:00:00Z", body="@coderabbitai review"),
        _command("2026-09-28T10:00:04Z"),
        _comment(_INCREMENTAL_DONE, "2026-09-28T10:00:08Z", "2026-09-28T10:03:00Z"),
        _comment(full_reply, "2026-09-28T10:00:12Z", "2026-09-28T10:09:00Z"),
    ]


_INCREMENTAL_REVIEW = _rabbit(
    "**Actionable comments posted: 1**", submitted="2026-09-28T10:02:30Z"
)


def test_an_interleaved_incremental_reply_does_not_finish_a_running_full_review() -> (
    None
):
    comments = _interleaved(_IN_PROGRESS)
    assert _outcome(comments, [_INCREMENTAL_REVIEW]) == "pending"


def test_the_full_reply_is_found_past_an_interleaved_incremental_reply() -> None:
    assert _outcome(_interleaved(_FULL_DONE), [_INCREMENTAL_REVIEW]) == "finished"


def test_a_reply_in_the_same_second_as_the_command_counts() -> None:
    comments = [
        _command("2026-09-27T17:59:45Z"),
        _comment(_FULL_DONE, "2026-09-27T17:59:45Z", "2026-09-27T18:11:04Z"),
    ]
    assert _outcome(comments) == "finished"


def test_an_incremental_not_completed_reply_does_not_rescue_a_full_review() -> None:
    comments = [
        _command("2026-09-28T10:00:04Z"),
        _comment(
            _INCREMENTAL_NOT_COMPLETED, "2026-09-28T10:00:08Z", "2026-09-28T10:03:00Z"
        ),
    ]
    assert _outcome(comments, [_INCREMENTAL_REVIEW]) == "pending"


def test_an_incremental_failure_does_not_fail_the_full_review() -> None:
    comments = [
        _command("2026-09-28T10:00:04Z"),
        _comment(_INCREMENTAL_FAILED, "2026-09-28T10:00:08Z", "2026-09-28T10:00:11Z"),
    ]
    assert _outcome(comments) == "pending"


def test_loader_requires_the_partial_pattern_with_the_others(tmp_path: Path) -> None:
    config = _config_with(
        final_review_comment="@bot full review",
        command_reply_marker="<!-- reply -->",
        final_review_done_pattern="done",
        final_review_failed_pattern="failed",
    )
    with pytest.raises(ValueError, match="final_review_partial_pattern"):
        load_reviewer(_write(tmp_path, config))


def test_the_full_reply_is_read_even_with_no_review_in_the_incremental_window() -> None:
    assert _outcome(_interleaved(_FULL_DONE)) == "finished"


# Hardening from CodeRabbit's review of the template port (observed on a CodeRabbit review).


def test_a_dismissed_clean_review_does_not_count() -> None:
    """A withdrawn review is not a verdict: `pull_request_review` fires on `dismissed`."""
    dismissed = {
        **_review(f"{MARKER}\nBugbot found no new issues."),
        "state": "DISMISSED",
    }
    assert review_verdict([dismissed], SHA, BUGBOT) == ("none", 0)


def test_a_dismissed_review_never_rescues_a_partial_full_review() -> None:
    comments = [
        _command("2026-09-27T17:13:15Z"),
        _comment(_NOT_COMPLETED, "2026-09-27T17:13:23Z", "2026-09-27T17:23:20Z"),
    ]
    review = {
        **_rabbit(
            "**Actionable comments posted: 1**", submitted="2026-09-27T17:21:02Z"
        ),
        "state": "DISMISSED",
    }
    assert _outcome(comments, [review]) == "pending"


def test_an_interleaved_incremental_review_does_not_rescue_a_partial_full_review() -> (
    None
):
    """The review in the window may belong to an incremental command, not the full one.

    With another command's reply in the window, the review can't be attributed to the
    full review, so the partial reply stays `pending` (errs on blocking).
    """
    comments = [
        _command("2026-09-28T10:00:04Z"),
        _command("2026-09-28T10:00:06Z", body="@coderabbitai review"),
        _comment(_INCREMENTAL_DONE, "2026-09-28T10:00:09Z", "2026-09-28T10:03:00Z"),
        _comment(_NOT_COMPLETED, "2026-09-28T10:00:12Z", "2026-09-28T10:09:00Z"),
    ]
    assert _outcome(comments, [_INCREMENTAL_REVIEW]) == "pending"
