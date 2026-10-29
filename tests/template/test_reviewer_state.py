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
# The PR-trigger mechanics (needs_trigger on an unreviewed head) still serve a
# reviewer with no local review; the CodeRabbit adapter reviews locally, so the
# collect_state tests below drive a copy of it without one.
CODERABBIT_PR = CODERABBIT._replace(local_review=None, local_preflight=None)

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


def test_loader_reads_the_active_reviewer_by_default(tmp_path: Path) -> None:
    assert load_reviewer(_write(tmp_path, _config_with())).key == "x"


def test_loader_null_active_means_no_reviewer(tmp_path: Path) -> None:
    """``"active": null`` is a deliberate "no reviewer", not a config error."""
    config = {**_config_with(), "active": None}
    with pytest.raises(reviewer_state.NoActiveReviewer, match="no PR reviewer"):
        load_reviewer(_write(tmp_path, config))


def test_no_active_reviewer_is_not_a_config_error() -> None:
    """Callers map ValueError to a usage error; no reviewer must not look like one."""
    assert not issubclass(reviewer_state.NoActiveReviewer, ValueError)


def test_loader_reads_a_named_adapter_when_none_is_active(tmp_path: Path) -> None:
    config = {**_config_with(), "active": None}
    assert load_reviewer(_write(tmp_path, config), key="x").key == "x"


def test_loader_missing_active_is_still_an_error(tmp_path: Path) -> None:
    """Only an explicit null turns the reviewer off; a missing key is a typo."""
    config = _config_with()
    del config["active"]
    with pytest.raises(ValueError, match="None"):
        load_reviewer(_write(tmp_path, config))


def test_main_reports_no_active_reviewer(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    path = _write(tmp_path, {**_config_with(), "active": None})
    monkeypatch.setattr(
        sys,
        "argv",
        ["reviewer_state.py", "--repo", "o/r", "--pr", "7", "--config", str(path)],
    )
    reviewer_state.main()
    out = json.loads(capsys.readouterr().out)
    assert out["reviewer"] is None and out["configured"] is False


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
        if any(a.endswith("/statuses") for a in args):
            return []
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
    state = reviewer_state.collect_state("octo/example", 7, CODERABBIT_PR)
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
    state = reviewer_state.collect_state("octo/example", 7, CODERABBIT_PR)
    assert state["verdict"] == "none"
    assert state["needs_trigger"] is True


def test_bugbot_never_takes_its_verdict_from_issue_comments(monkeypatch: Any) -> None:
    """Bugbot's summary is a review, so an issue comment can't supply its verdict.

    The issue comments ARE fetched for every reviewer: trigger comments are issue
    comments, and the incremental outcome and round budget count them. They must
    not be read as reviews.
    """
    calls: list[str] = []
    comments = [_walkthrough(f"{MARKER}\nfound no new issues")]
    monkeypatch.setattr(reviewer_state, "_gh_json", _fake_gh([], comments, calls))
    state = reviewer_state.collect_state("octo/example", 1, BUGBOT)
    assert state["verdict"] == "none"
    assert sum("issues/" in call for call in calls) == 1
    assert state["round_budget"]["incremental_before_final"]["used"] == 0


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
    state = reviewer_state.collect_state("octo/example", 7, CODERABBIT_PR)
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
    state = reviewer_state.collect_state("octo/example", 7, CODERABBIT_PR)
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


def test_an_incremental_command_without_a_reply_yet_blocks_the_rescue() -> None:
    """The incremental command itself, not just its reply, makes the window ambiguous."""
    comments = [
        _command("2026-09-28T10:00:04Z"),
        _command("2026-09-28T10:00:06Z", body="@coderabbitai review"),
        _comment(_NOT_COMPLETED, "2026-09-28T10:00:12Z", "2026-09-28T10:09:00Z"),
    ]
    assert _outcome(comments, [_INCREMENTAL_REVIEW]) == "pending"


def test_a_full_review_aborted_by_a_push_is_failed_not_pending() -> None:
    """A push during the full review aborts it ("Head commit changed").

    No review posts, so it must read `failed` (wait, retry once) rather than sit at
    `pending` forever.
    """
    aborted = _REPLY + (
        "<details>\n<summary>⚠️ Action not completed</summary>\n\n"
        "Head commit changed.\n\n</details>"
    )
    comments = [
        _command("2026-09-28T18:16:56Z"),
        _comment(aborted, "2026-09-28T18:17:05Z", "2026-09-28T18:20:01Z"),
    ]
    assert _outcome(comments) == "failed"


def test_a_full_review_aborted_by_a_base_or_head_change_is_failed() -> None:
    """The same abort, worded "Pull request base or head changed"."""
    aborted = _REPLY + (
        "<details>\n<summary>⚠️ Action not completed</summary>\n\n"
        "Pull request base or head changed.\n\n</details>"
    )
    comments = [
        _command("2026-09-28T18:37:12Z"),
        _comment(aborted, "2026-09-28T18:37:22Z", "2026-09-28T18:45:02Z"),
    ]
    assert _outcome(comments) == "failed"


# --- The rate limit: a refused request is a wait, never a spent review ---------

# Reply bodies as CodeRabbit posts them when it refuses a request for its rate
# limit (observed): the incremental reply states no wait, the full-review one does.
_RATE_LIMITED_INCREMENTAL = _REPLY + (
    "<details>\n<summary>⚠️ Action not completed</summary>\n\nReview rate limited.\n\n"
    "> Note: CodeRabbit is an incremental review system and does not re-review "
    "already reviewed commits. This command is applicable only when automatic "
    "reviews are paused.\n\n</details>"
)
_RATE_LIMITED_FULL = _REPLY + (
    "<details>\n<summary>⚠️ Action not completed</summary>\n\nReview rate limited.\n\n"
    "---\n\nYour included review limit is currently reached under our Fair Usage "
    "Limits Policy. This review may still proceed through usage-based billing if "
    "eligible. Your next included review will be available in 13 minutes.\n\n"
    "</details>"
)
_TRIGGER = "@coderabbitai review"
# A trigger refused for the rate limit, then a final-review request refused too.
_REFUSED_PAIR = [
    _command("2026-10-13T07:25:49Z", _TRIGGER),
    _comment(_RATE_LIMITED_INCREMENTAL, "2026-10-13T07:25:58Z"),
    _command("2026-10-13T07:26:53Z"),
    _comment(_RATE_LIMITED_FULL, "2026-10-13T07:27:03Z"),
]


def test_coderabbit_declares_its_rate_limit_reply() -> None:
    pattern = CODERABBIT.rate_limited_pattern
    assert pattern is not None and pattern.search("Review rate limited.")
    wait = CODERABBIT.rate_limit_wait_pattern
    assert wait is not None
    match = wait.search("Your next included review will be available in 13 minutes.")
    assert match and match.group(1) == "13"
    assert CODERABBIT.rate_limit_default_wait_minutes == 60
    assert CODERABBIT.rate_limit_notice_marker == (
        "<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->"
    )


def test_bugbot_has_no_rate_limit_reply() -> None:
    assert BUGBOT.rate_limited_pattern is None
    assert BUGBOT.rate_limit_wait_pattern is None
    assert BUGBOT.rate_limit_default_wait_minutes is None
    assert BUGBOT.rate_limit_notice_marker is None


def test_a_refusal_that_states_its_wait_is_retried_after_it() -> None:
    state = reviewer_state.rate_limit_state(_REFUSED_PAIR, CODERABBIT)
    assert state == {
        "refused": True,
        "request": "final",
        "replied_at": "2026-10-13T07:27:03Z",
        "retry_after": "2026-10-13T07:40:03Z",
        "wait_source": "reply",
    }


def test_a_refusal_with_no_stated_wait_waits_the_default() -> None:
    state = reviewer_state.rate_limit_state(_REFUSED_PAIR[:2], CODERABBIT)
    assert state["refused"] is True
    assert state["request"] == "trigger"
    assert state["retry_after"] == "2026-10-13T08:25:58Z"
    assert state["wait_source"] == "default"


def test_only_the_newest_request_can_be_refused() -> None:
    """Re-posting the same request after the wait clears the refusal."""
    comments = [*_REFUSED_PAIR, _command("2026-10-13T07:41:00Z")]
    state = reviewer_state.rate_limit_state(comments, CODERABBIT)
    assert state["refused"] is False
    assert state["request"] == "final"
    assert state["retry_after"] is None


def test_no_request_means_nothing_refused() -> None:
    assert reviewer_state.rate_limit_state([], CODERABBIT) == {
        "refused": False,
        "request": None,
        "replied_at": None,
        "retry_after": None,
        "wait_source": None,
    }


def test_a_reviewer_without_a_refusal_pattern_is_never_refused() -> None:
    comments = [
        _command("2026-10-13T07:25:49Z", "bugbot run"),
        _comment(_RATE_LIMITED_FULL, "2026-10-13T07:27:03Z"),
    ]
    state = reviewer_state.rate_limit_state(comments, BUGBOT)
    assert state["refused"] is False
    assert state["request"] == "trigger"


def test_a_refused_full_review_is_never_rescued_by_a_review_in_its_window() -> None:
    """A verdict-bearing review in the window belongs to some other request."""
    reviews = [
        _rabbit(
            "**Actionable comments posted: 1**",
            commit="abc",
            submitted="2026-10-13T07:27:10Z",
        )
    ]
    refused = _comment(
        _RATE_LIMITED_FULL, "2026-10-13T07:27:03Z", "2026-10-13T07:27:15Z"
    )
    comments = [_command("2026-10-13T07:26:53Z"), refused]
    assert _outcome(comments, reviews) == "pending"


# The reviewer's rate-limit notice: posted when the PR opens and edited in place on
# each refused attempt, it states the wait when the command reply doesn't.
_NOTICE = (
    "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
    "<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->\n\n"
    "> [!WARNING]\n> ## Review limit reached\n> \n"
    "> **Next included review available in 57 minutes.**\n"
)


def test_a_refusal_with_no_stated_wait_reads_the_notice_edited_after_it() -> None:
    comments = [
        _comment(_NOTICE, "2026-10-13T17:05:05Z", "2026-10-13T17:06:07Z"),
        _command("2026-10-13T17:05:52Z", _TRIGGER),
        _comment(_RATE_LIMITED_INCREMENTAL, "2026-10-13T17:06:00Z"),
    ]
    state = reviewer_state.rate_limit_state(comments, CODERABBIT)
    assert state["refused"] is True
    assert state["retry_after"] == "2026-10-13T18:03:07Z"
    assert state["wait_source"] == "notice"


def test_a_notice_not_edited_since_the_request_is_stale() -> None:
    """A notice left from an earlier attempt doesn't describe this request."""
    comments = [
        _comment(_NOTICE, "2026-10-13T17:05:05Z"),
        _command("2026-10-13T17:05:52Z", _TRIGGER),
        _comment(_RATE_LIMITED_INCREMENTAL, "2026-10-13T17:06:00Z"),
    ]
    state = reviewer_state.rate_limit_state(comments, CODERABBIT)
    assert state["wait_source"] == "default"
    assert state["retry_after"] == "2026-10-13T18:06:00Z"


def test_collect_state_reports_the_refusal(monkeypatch: Any) -> None:
    calls: list[str] = []
    monkeypatch.setattr(reviewer_state, "_gh_json", _fake_gh([], _REFUSED_PAIR, calls))
    state = reviewer_state.collect_state("octo/example", 7, CODERABBIT)
    assert state["rate_limit"]["refused"] is True
    assert state["rate_limit"]["request"] == "final"
    assert state["rate_limit"]["retry_after"] == "2026-10-13T07:40:03Z"
    # The refused final review never ran: still needed, still before it.
    assert state["final_review_outcome"] == "pending"
    assert state["local_review"]["phase"] == "before_final"


# --- Loader: the rate-limit and local-review fields ---------------------------


def _preflight(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "command": ["reviewer-cli", "usage"],
        "state_path": "pool.state",
        "available_states": ["available"],
        "remaining_path": "pool.remaining",
        "full_capacity_ms_path": "pool.fullMs",
        "max_recheck_seconds": 300,
        "auth_command": ["reviewer-cli", "whoami"],
        "authenticated_path": "ok",
        "remedies": {
            "not_installed": "install it",
            "not_authenticated": "log in",
            "failed": "run the doctor",
        },
    }
    block.update(overrides)
    return block


_LOCAL_REVIEW = {
    "command": ["reviewer-cli", "review", "--base", "{base}"],
    "event_type_path": "kind",
    "finding_event": "issue",
    "complete_event": "done",
    "finding_fields": {
        "severity": "level",
        "path": "file",
        "line": "line",
        "title": "headline",
        "body": "text",
    },
    "timeout_seconds": 900,
}
_MARKED = {
    "final_review_comment": "@bot full review",
    "command_reply_marker": "<!-- reply -->",
    "final_review_done_pattern": "done",
    "final_review_failed_pattern": "failed",
    "final_review_partial_pattern": "partial",
}


def test_loader_reads_a_preflight_block(tmp_path: Path) -> None:
    """The preflight gates local reviews, so it comes with a ``local_review``."""
    adapter = load_reviewer(
        _write(
            tmp_path,
            _config_with(
                local_preflight=_preflight(), local_review=_LOCAL_REVIEW, **_MARKED
            ),
        )
    )
    assert adapter.local_preflight == reviewer_state.PreflightSpec(
        command=("reviewer-cli", "usage"),
        state_path="pool.state",
        available_states=frozenset({"available"}),
        remaining_path="pool.remaining",
        full_capacity_ms_path="pool.fullMs",
        max_recheck_seconds=300,
        auth_command=("reviewer-cli", "whoami"),
        authenticated_path="ok",
        remedies={
            "not_installed": "install it",
            "not_authenticated": "log in",
            "failed": "run the doctor",
        },
    )


@pytest.mark.parametrize(
    "field",
    [
        "local_preflight",
        "local_review",
        "rate_limited_pattern",
        "rate_limit_wait_pattern",
        "rate_limit_default_wait_minutes",
        "rate_limit_notice_marker",
    ],
)
def test_loader_new_fields_must_be_present(tmp_path: Path, field: str) -> None:
    config = _config_with()
    del config["reviewers"]["x"][field]
    with pytest.raises(ValueError, match=field):
        load_reviewer(_write(tmp_path, config))


@pytest.mark.parametrize(
    "overrides",
    [
        {"command": []},
        {"command": "reviewer-cli usage"},
        {"command": ["reviewer-cli", ""]},
        {"state_path": ""},
        {"remaining_path": 3},
        {"full_capacity_ms_path": None},
        {"available_states": []},
        {"available_states": "available"},
        {"max_recheck_seconds": 0},
        {"max_recheck_seconds": True},
        {"surprise": 1},
        {"auth_command": []},
        {"authenticated_path": ""},
        {"remedies": {"not_installed": "install it"}},
        {"remedies": {"not_installed": "", "not_authenticated": "x", "failed": "y"}},
        {"remedies": "install it"},
    ],
)
def test_loader_rejects_a_bad_preflight_block(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    config = _config_with(
        local_preflight=_preflight(**overrides), local_review=_LOCAL_REVIEW, **_MARKED
    )
    with pytest.raises(ValueError, match="local_preflight"):
        load_reviewer(_write(tmp_path, config))


def test_loader_rate_limited_pattern_needs_the_reply_marker(tmp_path: Path) -> None:
    config = _config_with(
        rate_limited_pattern="rate limited",
        rate_limit_wait_pattern=r"in (\d+) minutes",
        rate_limit_default_wait_minutes=60,
    )
    with pytest.raises(ValueError, match="command_reply_marker"):
        load_reviewer(_write(tmp_path, config))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"rate_limited_pattern": "rate limited"},
            "rate_limit_default_wait_minutes",
        ),
        (
            {
                "rate_limited_pattern": "rate limited",
                "rate_limit_wait_pattern": "in (\\d+) (minutes)",
                "rate_limit_default_wait_minutes": 60,
            },
            "capture group",
        ),
        (
            {
                "rate_limited_pattern": "rate limited",
                "rate_limit_wait_pattern": None,
                "rate_limit_default_wait_minutes": 0,
            },
            "rate_limit_default_wait_minutes",
        ),
        (
            {
                "rate_limited_pattern": None,
                "rate_limit_wait_pattern": "in (\\d+) minutes",
                "rate_limit_default_wait_minutes": None,
            },
            "rate_limited_pattern",
        ),
        (
            {
                "rate_limited_pattern": None,
                "rate_limit_wait_pattern": None,
                "rate_limit_default_wait_minutes": 60,
            },
            "rate_limit_default_wait_minutes",
        ),
        (
            {
                "rate_limited_pattern": None,
                "rate_limit_wait_pattern": None,
                "rate_limit_default_wait_minutes": None,
                "rate_limit_notice_marker": "<!-- limited -->",
            },
            "rate_limit_notice_marker",
        ),
    ],
    ids=[
        "no-default-wait",
        "two-groups",
        "zero-default",
        "wait-without-pattern",
        "default-without-pattern",
        "notice-without-pattern",
    ],
)
def test_loader_rejects_inconsistent_rate_limit_fields(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    config = _config_with(**_MARKED, **overrides)
    with pytest.raises(ValueError, match=message):
        load_reviewer(_write(tmp_path, config))


def test_loader_accepts_a_refusal_with_no_stated_wait(tmp_path: Path) -> None:
    config = _config_with(
        **_MARKED,
        rate_limited_pattern="rate limited",
        rate_limit_wait_pattern=None,
        rate_limit_default_wait_minutes=30,
    )
    adapter = load_reviewer(_write(tmp_path, config))
    assert adapter.rate_limit_wait_pattern is None
    assert adapter.rate_limit_default_wait_minutes == 30


# --- List-valued reviewed-SHA patterns ----------------------------------------


def test_loader_accepts_a_reviewed_sha_pattern_list(tmp_path: Path) -> None:
    config = _config_with(reviewed_sha_pattern=["x(a)", "y(b)"])
    adapter = load_reviewer(_write(tmp_path, config))
    assert [p.pattern for p in adapter.reviewed_sha_patterns] == ["x(a)", "y(b)"]


def test_loader_accepts_a_single_reviewed_sha_pattern(tmp_path: Path) -> None:
    adapter = load_reviewer(_write(tmp_path, _config_with(reviewed_sha_pattern="x(a)")))
    assert [p.pattern for p in adapter.reviewed_sha_patterns] == ["x(a)"]


def test_a_null_reviewed_sha_pattern_means_no_patterns(tmp_path: Path) -> None:
    adapter = load_reviewer(_write(tmp_path, _config_with(reviewed_sha_pattern=None)))
    assert adapter.reviewed_sha_patterns == ()


@pytest.mark.parametrize("value", [[], ["(a)(b)"], [""], 3, ""])
def test_loader_rejects_a_bad_reviewed_sha_pattern(tmp_path: Path, value: Any) -> None:
    config = _config_with(reviewed_sha_pattern=value)
    with pytest.raises(ValueError, match="reviewed_sha_pattern"):
        load_reviewer(_write(tmp_path, config))


def test_loader_reviewed_sha_pattern_must_be_present(tmp_path: Path) -> None:
    config = _config_with()
    del config["reviewers"]["x"]["reviewed_sha_pattern"]
    with pytest.raises(ValueError, match="reviewed_sha_pattern"):
        load_reviewer(_write(tmp_path, config))


@pytest.mark.parametrize(
    "body",
    [
        "Reviewing files that changed between 1111111 and abc1234.",
        '{"coveredCommitId": "abc1234"}',
        'change_assessment_commit: "abc1234"',
    ],
    ids=["range", "covered-commit", "change-assessment"],
)
def test_coderabbit_reads_the_reviewed_sha_from_any_summary_format(body: str) -> None:
    """The first matching pattern wins, so a changed summary format still resolves."""
    head = "abc1234" + "0" * 33
    reviews = [_rabbit(f"**Actionable comments posted: 1**\n{body}", commit=None)]
    assert review_verdict(reviews, head, CODERABBIT) == ("findings", 1)


# --- Incremental-review reply patterns ----------------------------------------


def test_loader_incremental_fields_must_be_present(tmp_path: Path) -> None:
    config = _config_with()
    del config["reviewers"]["x"]["incremental_review_failed_pattern"]
    with pytest.raises(ValueError, match="incremental_review_failed_pattern"):
        load_reviewer(_write(tmp_path, config))


def test_loader_incremental_fields_are_all_or_nothing(tmp_path: Path) -> None:
    config = _config_with(**_MARKED, incremental_review_done_pattern="done")
    with pytest.raises(ValueError, match="all be set or all be null"):
        load_reviewer(_write(tmp_path, config))


def test_loader_incremental_fields_need_the_reply_marker(tmp_path: Path) -> None:
    fields = reviewer_state._INCREMENTAL_REVIEW_FIELDS
    config = _config_with(**dict.fromkeys(fields, "x"))
    with pytest.raises(ValueError, match="command_reply_marker"):
        load_reviewer(_write(tmp_path, config))


def test_loader_compiles_the_incremental_patterns(tmp_path: Path) -> None:
    fields = reviewer_state._INCREMENTAL_REVIEW_FIELDS
    adapter = load_reviewer(
        _write(tmp_path, _config_with(**_MARKED, **dict.fromkeys(fields, "x")))
    )
    for field in fields:
        pattern = getattr(adapter, field)
        assert pattern is not None and pattern.search("X")


def test_loader_rejects_an_invalid_incremental_pattern(tmp_path: Path) -> None:
    fields = reviewer_state._INCREMENTAL_REVIEW_FIELDS
    values = dict.fromkeys(fields, "x") | {fields[1]: "(unclosed"}
    with pytest.raises(ValueError, match=fields[1]):
        load_reviewer(_write(tmp_path, _config_with(**_MARKED, **values)))


def test_coderabbit_declares_its_incremental_replies_and_bugbot_none() -> None:
    reply = "I am an incremental review system.\n\nReview finished."
    assert CODERABBIT.incremental_review_done_pattern is not None
    assert CODERABBIT.incremental_review_done_pattern.search(reply)
    assert CODERABBIT.incremental_review_no_changes_pattern is not None
    assert CODERABBIT.incremental_review_failed_pattern is not None
    assert CODERABBIT.incremental_review_partial_pattern is not None
    for field in reviewer_state._INCREMENTAL_REVIEW_FIELDS:
        assert getattr(BUGBOT, field) is None


# --- Unthreaded-finding patterns ----------------------------------------------


@pytest.mark.parametrize("value", [None, "x(a)", ["no group"], [""]])
def test_loader_rejects_bad_unthreaded_finding_patterns(
    tmp_path: Path, value: Any
) -> None:
    config = _config_with(unthreaded_finding_patterns=value)
    with pytest.raises(ValueError, match="unthreaded_finding_patterns"):
        load_reviewer(_write(tmp_path, config))


def test_loader_accepts_an_empty_unthreaded_finding_list(tmp_path: Path) -> None:
    adapter = load_reviewer(
        _write(tmp_path, _config_with(unthreaded_finding_patterns=[]))
    )
    assert adapter.unthreaded_finding_patterns == ()


def test_loader_ignores_unknown_top_level_keys(tmp_path: Path) -> None:
    """A later top-level section (e.g. ``test_speed``) must not break the loaders."""
    config = _config_with() | {"test_speed": {"budget_seconds": 10}}
    config["review_rounds"] = {"max_local_before_final": 2}
    path = _write(tmp_path, config)
    assert load_reviewer(path).key == "x"
    assert reviewer_state.load_round_caps(path).local_before_final == 2


_RISK = "<!-- final_review_risk_start -->{}<!-- final_review_risk_end -->"
_OUTSIDE = (
    "**Outside diff range comments (1)**\n\n<details>\n"
    "<summary>src/app/mod.py (1)</summary>\n\n`10-20`: Handle the empty case.\n\n"
    "</details>\n\n---\n\nOther text"
)
_HEAD = "abc1234" + "0" * 33


def _risk_comment(text: str, comment_id: int = 7) -> dict[str, Any]:
    body = f"Reviewing files between 1111111 and abc1234.\n{_RISK.format(text)}"
    return {
        "id": comment_id,
        "body": body,
        "user": {"login": "coderabbitai[bot]"},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:05:00Z",
    }


def test_unthreaded_findings_carry_merge_risk_and_outside_diff() -> None:
    reviews = [{"id": 5, **_rabbit(_OUTSIDE, commit=_HEAD)}]
    comments = [_risk_comment("**Merge Risk:** the handler can return HTTP 500.")]
    found = reviewer_state.unthreaded_findings(reviews, comments, _HEAD, CODERABBIT)
    assert {(f["source"], f["id"]) for f in found} == {
        ("summary_comment", 7),
        ("review", 5),
    }
    merge_risk = next(f["text"] for f in found if f["source"] == "summary_comment")
    assert merge_risk == "**Merge Risk:** the handler can return HTTP 500."
    outside = next(f["text"] for f in found if f["source"] == "review")
    assert "10-20" in outside and "Other text" not in outside


def test_unthreaded_findings_skip_other_heads() -> None:
    reviews = [{"id": 5, **_rabbit(_OUTSIDE, commit=_HEAD)}]
    comments = [_risk_comment("**Merge Risk:** x")]
    assert (
        reviewer_state.unthreaded_findings(reviews, comments, "0" * 40, CODERABBIT)
        == []
    )


def test_unthreaded_findings_skip_other_authors_and_dismissed_reviews() -> None:
    reviews = [
        {"id": 1, **_rabbit(_OUTSIDE, commit=_HEAD, login="octocat")},
        {"id": 2, **_rabbit(_OUTSIDE, commit=_HEAD), "state": "DISMISSED"},
    ]
    comment = _risk_comment("**Merge Risk:** x")
    comment["user"] = {"login": "octocat"}
    assert (
        reviewer_state.unthreaded_findings(reviews, [comment], _HEAD, CODERABBIT) == []
    )


def test_unthreaded_findings_ignore_empty_matches() -> None:
    comments = [_risk_comment("   ")]
    assert reviewer_state.unthreaded_findings([], comments, _HEAD, CODERABBIT) == []


def test_bugbot_has_no_unthreaded_findings() -> None:
    reviews = [{"id": 5, **_review(_OUTSIDE, commit=_HEAD)}]
    assert reviewer_state.unthreaded_findings(reviews, [], _HEAD, BUGBOT) == []


def test_a_summary_comment_keeps_its_id_when_shaped_as_a_review() -> None:
    shaped = reviewer_state._comment_as_review(_risk_comment("x", comment_id=42))
    assert shaped["id"] == 42


# --- The local preflight's minimum and the start-failure pattern ---------------


def test_min_remaining_is_optional_and_defaults_to_one(tmp_path: Path) -> None:
    config = _config_with(
        local_preflight=_preflight(), local_review=_LOCAL_REVIEW, **_MARKED
    )
    adapter = load_reviewer(_write(tmp_path, config))
    assert adapter.local_preflight is not None
    assert adapter.local_preflight.min_remaining == 1


def test_loader_reads_min_remaining(tmp_path: Path) -> None:
    config = _config_with(
        local_preflight=_preflight(min_remaining=3),
        local_review=_LOCAL_REVIEW,
        **_MARKED,
    )
    adapter = load_reviewer(_write(tmp_path, config))
    assert adapter.local_preflight is not None
    assert adapter.local_preflight.min_remaining == 3


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "2", None])
def test_loader_rejects_a_bad_min_remaining(tmp_path: Path, bad: Any) -> None:
    config = _config_with(
        local_preflight=_preflight(min_remaining=bad),
        local_review=_LOCAL_REVIEW,
        **_MARKED,
    )
    with pytest.raises(ValueError, match="min_remaining"):
        load_reviewer(_write(tmp_path, config))


def test_start_failure_pattern_is_optional(tmp_path: Path) -> None:
    config = _config_with(local_review=_LOCAL_REVIEW, **_MARKED)
    adapter = load_reviewer(_write(tmp_path, config))
    assert adapter.local_review is not None
    assert adapter.local_review.start_failure_pattern is None


def test_loader_reads_start_failure_pattern(tmp_path: Path) -> None:
    review = _LOCAL_REVIEW | {"start_failure_pattern": "Could not start"}
    adapter = load_reviewer(
        _write(tmp_path, _config_with(local_review=review, **_MARKED))
    )
    assert adapter.local_review is not None
    pattern = adapter.local_review.start_failure_pattern
    assert pattern is not None and pattern.search("could not start review")


@pytest.mark.parametrize("bad", ["", 3, "(unclosed"])
def test_loader_rejects_a_bad_start_failure_pattern(tmp_path: Path, bad: Any) -> None:
    review = _LOCAL_REVIEW | {"start_failure_pattern": bad}
    with pytest.raises(ValueError, match="start_failure_pattern"):
        load_reviewer(_write(tmp_path, _config_with(local_review=review, **_MARKED)))


def test_loader_still_rejects_an_unknown_local_review_key(tmp_path: Path) -> None:
    review = _LOCAL_REVIEW | {"surprise": 1}
    with pytest.raises(ValueError, match="local_review"):
        load_reviewer(_write(tmp_path, _config_with(local_review=review, **_MARKED)))


def test_coderabbit_declares_its_capacity_minimum_and_start_failure() -> None:
    assert CODERABBIT.local_preflight is not None
    assert CODERABBIT.local_preflight.min_remaining == 2
    assert CODERABBIT.local_review is not None
    pattern = CODERABBIT.local_review.start_failure_pattern
    assert pattern is not None and pattern.search("Failed to start review")


# --- The refreshed rate-limit pattern -------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "Review rate limited. Your next review will be available in 5 minutes.",
        "Your included review limit is currently reached.",
    ],
)
def test_coderabbit_recognises_both_refusal_wordings(reply: str) -> None:
    pattern = CODERABBIT.rate_limited_pattern
    assert pattern is not None and pattern.search(reply)


def test_coderabbit_refusal_pattern_skips_a_reply_that_performed_an_action() -> None:
    pattern = CODERABBIT.rate_limited_pattern
    assert pattern is not None
    reply = "Action performed: review triggered.\n\nReview rate limited earlier."
    assert pattern.search(reply) is None


# --- Round caps -----------------------------------------------------------------


def test_the_repo_config_sets_the_incremental_and_fix_caps() -> None:
    caps = reviewer_state.load_round_caps()
    assert (caps.before_final, caps.after_final, caps.fix_iterations) == (1, 3, 5)


def test_round_caps_read_incremental_and_fix_keys(tmp_path: Path) -> None:
    config = _config_with()
    config["review_rounds"] = {
        "max_incremental_before_final": 4,
        "max_incremental_after_final": 2,
        "max_fix_iterations": 8,
    }
    caps = reviewer_state.load_round_caps(_write(tmp_path, config))
    assert (caps.before_final, caps.after_final, caps.fix_iterations) == (4, 2, 8)
    assert (caps.local_before_final, caps.local_after_final) == (3, 3)


def test_round_cap_defaults_without_a_config_block(tmp_path: Path) -> None:
    caps = reviewer_state.load_round_caps(_write(tmp_path, _config_with()))
    assert (caps.before_final, caps.after_final, caps.fix_iterations) == (1, 3, 5)


@pytest.mark.parametrize(
    "rounds",
    [
        {"max_incremental_before_final": -1},
        {"max_incremental_after_final": True},
        {"max_fix_iterations": "5"},
    ],
)
def test_bad_incremental_caps_are_rejected(
    tmp_path: Path, rounds: dict[str, Any]
) -> None:
    config = _config_with()
    config["review_rounds"] = rounds
    with pytest.raises(ValueError, match="review_rounds"):
        reviewer_state.load_round_caps(_write(tmp_path, config))


# --- Paginated gh output ----------------------------------------------------------


def _fake_run(stdout: str) -> Any:
    class _Done:
        pass

    done = _Done()
    done.stdout = stdout  # type: ignore[attr-defined]
    return lambda *args, **kwargs: done


def test_gh_json_flattens_one_array_per_page(monkeypatch: Any) -> None:
    """A gh that prints each page's array separately must still parse."""
    monkeypatch.setattr(
        reviewer_state.subprocess,
        "run",
        _fake_run('[{"id": 1}, {"id": 2}]\n[{"id": 3}]\n'),
    )
    assert reviewer_state._gh_json(["api", "x", "--paginate"]) == [
        {"id": 1},
        {"id": 2},
        {"id": 3},
    ]


def test_gh_json_reads_a_single_document(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        reviewer_state.subprocess, "run", _fake_run('{"data": {"a": 1}}')
    )
    assert reviewer_state._gh_json(["api", "graphql"]) == {"data": {"a": 1}}


def test_gh_json_reads_a_single_array(monkeypatch: Any) -> None:
    monkeypatch.setattr(reviewer_state.subprocess, "run", _fake_run('[{"id": 1}]'))
    assert reviewer_state._gh_json(["api", "x"]) == [{"id": 1}]


def test_gh_json_of_empty_output_is_none(monkeypatch: Any) -> None:
    monkeypatch.setattr(reviewer_state.subprocess, "run", _fake_run("\n"))
    assert reviewer_state._gh_json(["api", "x"]) is None


# --- Incremental outcome, finished windows, on-head and running detection -------
# Replayed from a synthetic PR timeline (fixtures/reviewer_state/synthetic-pr.json):
# the reviewer's command replies, its walkthrough comment and every review, with
# placeholder bodies that carry only the markers the adapter reads. A reply is
# edited in place, so a comment whose recorded edit is later than the cutoff is
# dropped: its body is the later state, not what the reviewer had said by then.

_SYNTHETIC = json.loads(
    (
        Path(__file__).parent / "fixtures" / "reviewer_state" / "synthetic-pr.json"
    ).read_text()
)
_REPO = _SYNTHETIC["repo"]
_PR = _SYNTHETIC["pr"]
_WALKTHROUGH_ID = 100
_WALKTHROUGH = next(c for c in _SYNTHETIC["comments"] if c["id"] == _WALKTHROUGH_ID)
_NO_CHANGES_REPLY_ID = 112
_ABORTED_FULL_REPLY_ID = 110
_LAGGING_REPLY_ID = 120
HEAD_FAILED = "2b" * 20  # its review failed, then a re-trigger finished
HEAD_NO_CHANGES = "4d" * 20  # answered "no files to review"
HEAD_WINDOW = "5e" * 20  # only empty and nitpick reviews, inside a finished window
HEAD_OUTSIDE = "6f" * 20  # one outside-diff finding; the walkthrough covers it
HEAD_LAGGING = "7a" * 20  # the review lands before the reply moves


def _at(clock: str) -> str:
    return f"2026-01-15T{clock}Z"


def _as_of(clock: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cutoff = _at(clock)
    comments = [
        c
        for c in _SYNTHETIC["comments"]
        if (c.get("updated_at") or c["created_at"]) <= cutoff
    ]
    reviews = [r for r in _SYNTHETIC["reviews"] if r["submitted_at"] <= cutoff]
    return comments, reviews


def _windows(comments: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return list(reviewer_state.finished_windows(comments, CODERABBIT))


def _reply_body(comment_id: int) -> str:
    return str(next(c for c in _SYNTHETIC["comments"] if c["id"] == comment_id)["body"])


def _committed(head: str) -> str:
    return str(
        next(k for k in _SYNTHETIC["commits"] if k["oid"] == head)["committedDate"]
    )


def _fake_gh_timeline(
    comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    head: str,
    committed: str,
    statuses: list[dict[str, Any]],
    calls: list[str] | None = None,
) -> Any:
    def _gh_json(args: list[str]) -> Any:
        if calls is not None:
            calls.append(" ".join(args))
        if any(a.endswith("/statuses") for a in args):
            assert any(f"commits/{head}/statuses" in a for a in args)
            return statuses
        if "graphql" in args:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "headRefOid": head,
                            "mergeStateStatus": "CLEAN",
                            "commits": {
                                "nodes": [{"commit": {"committedDate": committed}}]
                            },
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


def _state_as_of(
    monkeypatch: Any,
    clock: str,
    head: str,
    committed: str | None = None,
    statuses: list[dict[str, Any]] | None = None,
) -> Any:
    """Replay ``collect_state`` at ``clock`` against the synthetic timeline.

    The head's commit time and statuses come from the timeline unless overridden.
    """
    comments, reviews = _as_of(clock)
    if statuses is None:
        statuses = [
            s
            for s in _SYNTHETIC["statuses"].get(head, [])
            if s["created_at"] <= _at(clock)
        ]
    monkeypatch.setattr(
        reviewer_state,
        "_gh_json",
        _fake_gh_timeline(
            comments, reviews, head, committed or _committed(head), statuses
        ),
    )
    return reviewer_state.collect_state(_REPO, _PR, CODERABBIT_PR)


def test_synthetic_case_1_an_incremental_review_failed() -> None:
    comments, _ = _as_of("10:05:00")
    assert reviewer_state.incremental_review(comments, CODERABBIT) == (
        "failed",
        _at("10:01:00"),
        _at("10:01:10"),
    )


def test_synthetic_empty_reviews_posted_during_a_failed_review_do_not_pass() -> None:
    """Four empty-body thread-reply reviews land on the head as its review fails."""
    comments, reviews = _as_of("10:05:00")
    empty = [
        r
        for r in reviews
        if r["commit_id"] == HEAD_FAILED
        and not r["body"]
        and r["user"]["login"] in CODERABBIT.bot_logins
    ]
    assert len(empty) == 4
    assert review_verdict(reviews, HEAD_FAILED, CODERABBIT, _windows(comments)) == (
        "none",
        0,
    )


def test_synthetic_case_3_no_files_to_review() -> None:
    comments, _ = _as_of("11:40:00")
    outcome = reviewer_state.incremental_review(comments, CODERABBIT)
    assert outcome.outcome == "no_changes"
    assert outcome.replied_at == _at("11:30:00")


def test_synthetic_a_full_review_reply_is_never_an_incremental_outcome() -> None:
    """The "Head commit changed" reply answers `full review`, not `review`."""
    head_changed = _reply_body(_ABORTED_FULL_REPLY_ID)
    patterns = [
        CODERABBIT.incremental_review_done_pattern,
        CODERABBIT.incremental_review_no_changes_pattern,
        CODERABBIT.incremental_review_failed_pattern,
        CODERABBIT.incremental_review_partial_pattern,
    ]
    assert not any(p is not None and p.search(head_changed) for p in patterns)
    comments, _ = _as_of("11:40:00")
    assert reviewer_state.final_review_outcome(comments, [], CODERABBIT) == "failed"


def test_synthetic_case_4_empty_and_nitpick_reviews_in_a_finished_window_pass() -> None:
    """Two empty-body reviews and a nitpick-only one inside `Review finished`."""
    comments, reviews = _as_of("13:15:00")
    assert review_verdict(reviews, HEAD_WINDOW, CODERABBIT, _windows(comments)) == (
        "pass",
        0,
    )
    assert review_verdict(reviews, HEAD_WINDOW, CODERABBIT) == ("none", 0)


def test_synthetic_outside_diff_findings_win_over_window_evidence() -> None:
    comments, reviews = _as_of("14:15:00")
    assert review_verdict(reviews, HEAD_OUTSIDE, CODERABBIT, _windows(comments)) == (
        "findings",
        1,
    )


def test_synthetic_walkthrough_coverage_counts_only_inside_a_finished_window() -> None:
    comments, _ = _as_of("14:15:00")
    walkthrough = [reviewer_state._comment_as_review(_WALKTHROUGH)]
    windows = _windows(comments)
    assert review_verdict(walkthrough, HEAD_OUTSIDE, CODERABBIT, windows) == (
        "pass",
        0,
    )
    assert review_verdict(walkthrough, HEAD_OUTSIDE, CODERABBIT) == ("none", 0)
    assert review_verdict(walkthrough, "0" * 40, CODERABBIT, windows) == ("none", 0)


def test_an_empty_review_alone_is_never_a_clean_verdict() -> None:
    """An empty bot review on the head, outside every finished window, proves nothing.

    The reviewer posts empty reviews to hold its thread replies even while its review
    is failing, so on its own one must never read as a clean pass.
    """
    empty = _rabbit("", commit=SHA, submitted="2026-01-15T10:07:00Z")
    assert review_verdict([empty], SHA, CODERABBIT) == ("none", 0)
    elsewhere = [("2026-01-15T09:00:00Z", "2026-01-15T10:00:00Z")]
    assert review_verdict([empty], SHA, CODERABBIT, elsewhere) == ("none", 0)
    covering = [("2026-01-15T10:00:00Z", "2026-01-15T10:10:00Z")]
    assert review_verdict([empty], SHA, CODERABBIT, covering) == ("pass", 0)


def test_finished_windows_span_request_to_the_replys_last_edit() -> None:
    comments, _ = _as_of("13:15:00")
    windows = _windows(comments)
    assert (_at("13:00:10"), _at("13:10:00")) in windows  # incremental
    assert (_at("12:00:00"), _at("12:20:00")) in windows  # full review
    assert not any(start == _at("10:01:00") for start, _ in windows)  # failed


def test_a_window_ends_at_the_next_request_of_either_kind() -> None:
    """Request A's late reply edit must not stretch its window over request B."""
    comments = [
        _command("2026-01-15T10:00:00Z", body="@coderabbitai review"),
        _comment(_INCREMENTAL_DONE, "2026-01-15T10:00:10Z", "2026-01-15T10:10:00Z"),
        _command("2026-01-15T10:05:00Z"),
        _comment(_FULL_FAILED, "2026-01-15T10:05:10Z", "2026-01-15T10:05:13Z"),
    ]
    windows = reviewer_state.finished_windows(comments, CODERABBIT)
    assert windows == [("2026-01-15T10:00:00Z", "2026-01-15T10:05:00Z")]
    thread_reply = _rabbit("", commit=SHA, submitted="2026-01-15T10:07:00Z")
    assert review_verdict([thread_reply], SHA, CODERABBIT, windows) == ("none", 0)


def test_a_reviewer_without_reply_patterns_has_no_windows() -> None:
    comments = [
        _command("2026-01-15T10:00:00Z", body="bugbot run"),
        _comment(_INCREMENTAL_DONE, "2026-01-15T10:00:10Z", login="cursor[bot]"),
    ]
    assert reviewer_state.finished_windows(comments, BUGBOT) == []


def test_the_newest_trigger_owns_the_outcome() -> None:
    comments, _ = _as_of("10:55:00")  # 10:01 failed, then 10:30 finished
    assert reviewer_state.incremental_review(comments, CODERABBIT).outcome == (
        "finished"
    )


def test_an_unanswered_trigger_is_pending() -> None:
    comments, _ = _as_of("11:29:00")  # the 11:26 trigger, no reply yet
    outcome = reviewer_state.incremental_review(comments, CODERABBIT)
    assert outcome == ("pending", _at("11:26:00"), None)


def test_incremental_outcome_not_requested_and_not_tracked() -> None:
    assert reviewer_state.incremental_review([], CODERABBIT).outcome == "not_requested"
    comments, _ = _as_of("14:15:00")
    assert reviewer_state.incremental_review(comments, BUGBOT).outcome == (
        "not_tracked"
    )


def test_an_incremental_not_completed_reply_is_its_own_outcome() -> None:
    reply = _REPLY + (
        "<details>\n<summary>Action not completed</summary>\n\nSomething new.\n\n"
        "> Note: CodeRabbit is an incremental review system and does not re-review "
        "already reviewed commits.\n\n</details>"
    )
    comments = [
        _command("2026-01-15T10:00:00Z", body="@coderabbitai review"),
        _comment(reply, "2026-01-15T10:00:09Z"),
    ]
    assert reviewer_state.incremental_review(comments, CODERABBIT).outcome == (
        "not_completed"
    )


def test_synthetic_unthreaded_findings_carry_merge_risk_and_outside_diff() -> None:
    _, reviews = _as_of("14:15:00")
    found = reviewer_state.unthreaded_findings(
        reviews, [_WALKTHROUGH], HEAD_OUTSIDE, CODERABBIT
    )
    assert {(f["source"], f["id"]) for f in found} == {
        ("summary_comment", _WALKTHROUGH_ID),
        ("review", 214),
    }
    merge_risk = next(f["text"] for f in found if f["source"] == "summary_comment")
    assert merge_risk.startswith("**Merge Risk:**")
    outside = next(f["text"] for f in found if f["source"] == "review")
    assert "src/app/example.py:10-12" in outside
    assert "Prompt to fix review comments" not in outside


def test_synthetic_unthreaded_findings_skip_other_heads() -> None:
    _, reviews = _as_of("14:15:00")
    assert (
        reviewer_state.unthreaded_findings(
            reviews, [_WALKTHROUGH], "0" * 40, CODERABBIT
        )
        == []
    )


def test_synthetic_collect_state_case_1_a_failed_review_needs_a_retrigger(
    monkeypatch: Any,
) -> None:
    state = _state_as_of(monkeypatch, "10:05:00", HEAD_FAILED)
    assert state["verdict"] == "none"
    assert state["incremental_review_outcome"] == "failed"
    assert state["review_running"] is False
    assert state["needs_trigger"] is True
    assert state["head_committed_at"] == _at("10:00:00")
    assert state["head_first_status_at"] == _at("10:00:30")


def test_synthetic_collect_state_case_3_carries_the_verdict_past_a_no_changes_head(
    monkeypatch: Any,
) -> None:
    """The head had nothing new to review, and the request covered it.

    The commit before it passed inside a finished window, so its verdict carries.
    """
    state = _state_as_of(monkeypatch, "11:40:00", HEAD_NO_CHANGES)
    assert state["incremental_review_outcome"] == "no_changes"
    assert state["incremental_review_on_head"] is True
    assert state["review_running"] is False
    assert (state["verdict"], state["reported_finding_count"]) == ("pass", 0)
    assert state["verdict_carried_from"] == "3c" * 20
    assert state["verdict_carry_reason"] == "no_changes"
    assert state["needs_trigger"] is False
    # The 11:26 trigger follows the 11:25 full-review request; with a verdict
    # carried, its "no files to review" answer spends no round.
    assert state["round_budget"]["phase"] == "after_final"
    assert state["round_budget"]["incremental_after_final"]["used"] == 0


def test_synthetic_collect_state_no_carry_forward_for_a_head_newer_than_the_reply(
    monkeypatch: Any,
) -> None:
    state = _state_as_of(
        monkeypatch, "11:40:00", HEAD_NO_CHANGES, committed=_at("11:35:00")
    )
    assert state["incremental_review_on_head"] is False
    assert state["verdict"] == "none"
    assert state["needs_trigger"] is True


def test_synthetic_collect_state_a_running_review_is_not_retriggered(
    monkeypatch: Any,
) -> None:
    state = _state_as_of(monkeypatch, "11:29:00", HEAD_NO_CHANGES)
    assert state["verdict"] == "none"
    assert state["incremental_review_outcome"] == "pending"
    assert state["review_running"] is True
    assert state["needs_trigger"] is False


def test_synthetic_collect_state_a_trigger_older_than_the_head_is_not_running(
    monkeypatch: Any,
) -> None:
    state = _state_as_of(
        monkeypatch, "11:29:00", HEAD_NO_CHANGES, committed=_at("11:27:00")
    )
    assert state["incremental_review_outcome"] == "pending"
    assert state["review_running"] is False
    assert state["needs_trigger"] is True


def test_synthetic_collect_state_case_4_passes_the_reviewed_head(
    monkeypatch: Any,
) -> None:
    state = _state_as_of(monkeypatch, "13:15:00", HEAD_WINDOW)
    assert state["verdict"] == "pass"
    assert state["incremental_review_outcome"] == "finished"
    assert state["needs_trigger"] is False


def test_synthetic_collect_state_surfaces_unthreaded_findings(
    monkeypatch: Any,
) -> None:
    state = _state_as_of(monkeypatch, "14:15:00", HEAD_OUTSIDE)
    assert (state["verdict"], state["reported_finding_count"]) == ("findings", 1)
    assert state["final_review_outcome"] == "finished"
    assert {f["source"] for f in state["unthreaded_findings"]} == {
        "review",
        "summary_comment",
    }


def test_the_query_requests_the_head_commit_date() -> None:
    requested = {line.strip() for line in reviewer_state._THREADS_QUERY.splitlines()}
    assert "commits(last: 1) {" in requested
    assert "nodes { commit { committedDate } }" in requested


def test_collect_state_without_a_commit_date_reports_none(monkeypatch: Any) -> None:
    calls: list[str] = []
    monkeypatch.setattr(reviewer_state, "_gh_json", _fake_gh([], [], calls))
    state = reviewer_state.collect_state("owner/repo", 1, CODERABBIT_PR)
    assert state["head_committed_at"] is None
    assert state["head_first_status_at"] is None


# The reply lags (or never moves) but the review landed: the 15:00:10 trigger's
# reply reads "Review triggered" until 15:20, although its review (one outside-diff
# finding, on the head) posted at 15:15. Replayed with the reply in that state.


def _lagging_reply_as_of(
    clock: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    comments, reviews = _as_of(clock)
    reply = next(c for c in _SYNTHETIC["comments"] if c["id"] == _LAGGING_REPLY_ID)
    in_progress = {
        **reply,
        "body": _SYNTHETIC["in_progress_reply"]["body"],
        "updated_at": reply["created_at"],
    }
    return [*comments, in_progress], reviews


def test_a_verdict_bearing_review_on_the_head_lands_a_lagging_reply() -> None:
    comments, reviews = _lagging_reply_as_of("15:17:00")
    outcome = reviewer_state.incremental_review(
        comments, CODERABBIT, reviews, HEAD_LAGGING
    )
    assert outcome == ("finished", _at("15:00:10"), _at("15:15:00"))


def test_without_the_review_the_lagging_reply_is_still_pending() -> None:
    comments, reviews = _lagging_reply_as_of("15:10:00")
    outcome = reviewer_state.incremental_review(
        comments, CODERABBIT, reviews, HEAD_LAGGING
    )
    assert outcome.outcome == "pending"


def test_an_empty_review_on_the_head_does_not_land_a_review() -> None:
    """Empty-body reviews also hold thread replies, so they can't mark a landing."""
    comments, _ = _lagging_reply_as_of("15:10:00")
    empty = _rabbit("", commit=HEAD_LAGGING, submitted=_at("15:05:00"))
    outcome = reviewer_state.incremental_review(
        comments, CODERABBIT, [empty], HEAD_LAGGING
    )
    assert outcome.outcome == "pending"


def test_a_review_before_the_trigger_does_not_land_it() -> None:
    comments, _ = _lagging_reply_as_of("15:10:00")
    early = _rabbit(
        "**Actionable comments posted: 1**",
        commit=HEAD_LAGGING,
        submitted=_at("15:00:05"),
    )
    outcome = reviewer_state.incremental_review(
        comments, CODERABBIT, [early], HEAD_LAGGING
    )
    assert outcome.outcome == "pending"


def test_a_review_on_another_head_does_not_land_it() -> None:
    comments, _ = _lagging_reply_as_of("15:10:00")
    other = _rabbit(
        "**Actionable comments posted: 1**",
        commit=HEAD_OUTSIDE,
        submitted=_at("15:05:00"),
    )
    outcome = reviewer_state.incremental_review(
        comments, CODERABBIT, [other], HEAD_LAGGING
    )
    assert outcome.outcome == "pending"


def test_collect_state_a_landed_review_stops_the_wait(monkeypatch: Any) -> None:
    comments, reviews = _lagging_reply_as_of("15:17:00")
    monkeypatch.setattr(
        reviewer_state,
        "_gh_json",
        _fake_gh_timeline(
            comments, reviews, HEAD_LAGGING, _committed(HEAD_LAGGING), []
        ),
    )
    state = reviewer_state.collect_state(_REPO, _PR, CODERABBIT_PR)
    assert state["incremental_review_outcome"] == "finished"
    assert state["review_running"] is False
    assert (state["verdict"], state["reported_finding_count"]) == ("findings", 1)
    assert [f["id"] for f in state["unthreaded_findings"]] == [215]
    assert "Outside diff range" not in state["unthreaded_findings"][0]["text"]


# --- When did the head reach the PR? committedDate is not the push time ----------


def test_collect_state_no_carry_forward_when_the_request_predates_the_head(
    monkeypatch: Any,
) -> None:
    """A head pushed between the trigger (11:26) and its reply (11:30)."""
    state = _state_as_of(
        monkeypatch, "11:40:00", HEAD_NO_CHANGES, committed=_at("11:28:00")
    )
    assert state["incremental_review_outcome"] == "no_changes"
    assert state["incremental_review_on_head"] is False
    assert state["verdict"] == "none"
    assert state["needs_trigger"] is True


def test_the_request_is_on_head_when_it_follows_the_head_commit(
    monkeypatch: Any,
) -> None:
    state = _state_as_of(monkeypatch, "13:15:00", HEAD_WINDOW)
    assert state["incremental_review_on_head"] is True


def test_a_commit_pushed_after_the_request_is_not_on_head(monkeypatch: Any) -> None:
    """Committed 11:21, trigger 11:26, pushed ~11:27: the request predates the push.

    committedDate alone says "on head". The head's first commit status (posted
    after the push) says otherwise.
    """
    state = _state_as_of(
        monkeypatch,
        "11:40:00",
        HEAD_NO_CHANGES,
        committed=_at("11:21:00"),
        statuses=[
            {"created_at": _at("11:29:00"), "context": "gate"},
            {"created_at": _at("11:27:30"), "context": "ci"},
        ],
    )
    assert state["head_first_status_at"] == _at("11:27:30")
    assert state["incremental_review_on_head"] is False
    assert state["verdict"] == "none"
    assert state["needs_trigger"] is True


def test_a_status_before_the_request_keeps_it_on_head(monkeypatch: Any) -> None:
    state = _state_as_of(
        monkeypatch,
        "11:40:00",
        HEAD_NO_CHANGES,
        committed=_at("11:20:00"),
        statuses=[{"created_at": _at("11:21:00"), "context": "ci"}],
    )
    assert state["head_first_status_at"] == _at("11:21:00")
    assert state["incremental_review_on_head"] is True
    assert state["verdict"] == "pass"
    assert state["verdict_carry_reason"] == "no_changes"


def test_carry_forward_skips_an_earlier_commit_with_no_verdict(
    monkeypatch: Any,
) -> None:
    """A passed; B's review failed (only thread-reply reviews); C had no changes."""
    a, b, c = "a" * 40, "b" * 40, "c" * 40
    comments = [
        _command("2026-01-15T10:00:00Z", body="@coderabbitai review"),
        _comment(_INCREMENTAL_DONE, "2026-01-15T10:00:05Z", "2026-01-15T10:03:00Z"),
        _command("2026-01-15T10:10:00Z", body="@coderabbitai review"),
        _comment(_INCREMENTAL_FAILED, "2026-01-15T10:10:05Z", "2026-01-15T10:10:08Z"),
        _command("2026-01-15T10:20:00Z", body="@coderabbitai review"),
        _comment(
            _reply_body(_NO_CHANGES_REPLY_ID),
            "2026-01-15T10:20:05Z",
            "2026-01-15T10:20:09Z",
        ),
    ]
    reviews = [
        _rabbit("", commit=a, submitted="2026-01-15T10:02:00Z"),
        _rabbit("", commit=b, submitted="2026-01-15T10:10:06Z"),
    ]
    monkeypatch.setattr(
        reviewer_state,
        "_gh_json",
        _fake_gh_timeline(comments, reviews, c, "2026-01-15T10:15:00Z", []),
    )
    state = reviewer_state.collect_state("owner/repo", 1, CODERABBIT_PR)
    assert state["incremental_review_outcome"] == "no_changes"
    assert (state["verdict"], state["needs_trigger"]) == ("pass", False)
    assert state["verdict_carried_from"] == a


def test_bugbot_never_reads_head_statuses(monkeypatch: Any) -> None:
    calls: list[str] = []
    monkeypatch.setattr(reviewer_state, "_gh_json", _fake_gh([], [], calls))
    state = reviewer_state.collect_state("owner/repo", 1, BUGBOT)
    assert not any("statuses" in c for c in calls)
    assert state["incremental_review_outcome"] == "not_tracked"
    assert state["incremental_review_on_head"] is False
    assert state["review_running"] is False


def _state_trigger_beats_status(monkeypatch: Any, reviews: list[dict[str, Any]]) -> Any:
    """Trigger 3 s after the push, and 2 s before the head's first status."""
    head = "5" * 40
    comments = [
        _command("2026-01-15T21:37:19Z", body="@coderabbitai review"),
        _comment(_INCREMENTAL_DONE, "2026-01-15T21:37:49Z", "2026-01-15T21:45:35Z"),
    ]
    monkeypatch.setattr(
        reviewer_state,
        "_gh_json",
        _fake_gh_timeline(
            comments,
            reviews,
            head,
            "2026-01-15T21:37:16Z",
            [{"created_at": "2026-01-15T21:37:21Z", "context": "ci"}],
        ),
    )
    return reviewer_state.collect_state("owner/repo", 1, CODERABBIT_PR)


def test_a_verdict_bearing_review_after_the_request_puts_it_on_head(
    monkeypatch: Any,
) -> None:
    """The trigger beat the first status, but its review of the head proves it."""
    findings = _rabbit(
        "**Actionable comments posted: 2**",
        commit="5" * 40,
        submitted="2026-01-15T21:41:39Z",
    )
    state = _state_trigger_beats_status(monkeypatch, [findings])
    assert state["incremental_review_on_head"] is True
    assert (state["verdict"], state["reported_finding_count"]) == ("findings", 2)


def test_an_empty_review_after_the_request_does_not_put_it_on_head(
    monkeypatch: Any,
) -> None:
    """A thread-reply container says nothing about which head the request covered."""
    empty = _rabbit("", commit="5" * 40, submitted="2026-01-15T21:38:03Z")
    state = _state_trigger_beats_status(monkeypatch, [empty])
    assert state["incremental_review_on_head"] is False


# --- The template's own config: no reviewer is active ---------------------------


def test_collect_state_with_the_shipped_config_reports_no_reviewer(
    monkeypatch: Any, capsys: Any
) -> None:
    """The template ships ``"active": null``; every new path must run cleanly on it.

    ``main`` on the shipped config reports no reviewer and calls GitHub not at all,
    and each adapter the shipped config declares runs the incremental, window and
    on-head paths on an untouched PR without raising.
    """

    def _no_gh(args: list[str]) -> Any:
        raise AssertionError(f"no reviewer is active, yet gh was called: {args}")

    monkeypatch.setattr(reviewer_state, "_gh_json", _no_gh)
    monkeypatch.setattr(
        sys, "argv", ["reviewer_state.py", "--repo", "owner/repo", "--pr", "1"]
    )
    reviewer_state.main()
    out = json.loads(capsys.readouterr().out)
    assert out["reviewer"] is None and out["configured"] is False

    shipped = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    assert shipped["active"] is None
    for key in shipped["reviewers"]:
        calls: list[str] = []
        monkeypatch.setattr(reviewer_state, "_gh_json", _fake_gh([], [], calls))
        state = reviewer_state.collect_state("owner/repo", 1, load_reviewer(key=key))
        assert state["incremental_review_outcome"] in ("not_tracked", "not_requested")
        assert state["incremental_review_on_head"] is False
        assert state["review_running"] is False
        assert state["unthreaded_findings"] == []
