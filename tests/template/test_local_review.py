"""The local review path: fix rounds reviewed on the developer's machine.

A PR review request spends the reviewer's scarce per-developer PR quota, so the
adapter can declare a ``local_review`` command (a CLI that reviews committed changes
against a base). Its rounds are recorded as plain PR comments carrying a marker, so
``reviewer_state`` budgets them from the comment stream, and the PR itself gets one
final full review. These tests drive a made-up reviewer CLI so the seam stays
vendor-neutral; the scripts are loaded by path (they live outside any package).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess  # nosec B404 -- only CalledProcessError, nothing is run
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
BUGBOT = reviewer_state.load_reviewer(key="bugbot")
CODERABBIT = reviewer_state.load_reviewer(key="coderabbit")

HEAD = "1111111111111111111111111111111111111111"
OLD = "2222222222222222222222222222222222222222"
BASE = "3333333333333333333333333333333333333333"


def _local_review(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "command": ["reviewer-cli", "review", "--json", "--base", "{base}"],
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
    block.update(overrides)
    return block


def _preflight(min_remaining: int | None = None) -> dict[str, Any]:
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
    if min_remaining is not None:
        block["min_remaining"] = min_remaining
    return block


def _config(**entry: Any) -> dict[str, Any]:
    config = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    base = config["reviewers"]["coderabbit"]
    base.update(entry)
    return {"active": "x", "reviewers": {"x": base}}


def _write(tmp_path: Path, config: dict[str, Any]) -> Path:
    path = tmp_path / "pr-reviewer.json"
    path.write_text(json.dumps(config))
    return path


def _adapter(tmp_path: Path, **entry: Any) -> Any:
    entry.setdefault("local_review", _local_review())
    entry.setdefault("local_preflight", _preflight())
    return reviewer_state.load_reviewer(_write(tmp_path, _config(**entry)))


def _capacity_adapter(tmp_path: Path, min_remaining: int = 2) -> Any:
    """Return an adapter that names its start failure and asks for a minimum."""
    return _adapter(
        tmp_path,
        local_review=_local_review(start_failure_pattern="Failed to start review"),
        local_preflight=_preflight(min_remaining=min_remaining),
    )


# --- Loader --------------------------------------------------------------------


def test_the_loader_reads_a_local_review_block(tmp_path: Path) -> None:
    spec = _adapter(tmp_path).local_review
    assert spec == reviewer_state.LocalReviewSpec(
        command=("reviewer-cli", "review", "--json", "--base", "{base}"),
        event_type_path="kind",
        finding_event="issue",
        complete_event="done",
        finding_fields={
            "severity": "level",
            "path": "file",
            "line": "line",
            "title": "headline",
            "body": "text",
        },
        timeout_seconds=900,
    )


def test_local_review_must_be_present(tmp_path: Path) -> None:
    config = _config(local_preflight=None)
    del config["reviewers"]["x"]["local_review"]
    with pytest.raises(ValueError, match="local_review"):
        reviewer_state.load_reviewer(_write(tmp_path, config))


def test_a_reviewer_may_have_no_local_review(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, local_review=None, local_preflight=None)
    assert adapter.local_review is None


def test_a_capacity_check_needs_a_local_review_to_gate(tmp_path: Path) -> None:
    """The preflight reads the local review's pool; alone it gates nothing."""
    with pytest.raises(ValueError, match="local_preflight"):
        _adapter(tmp_path, local_review=None)


@pytest.mark.parametrize(
    "overrides",
    [
        {"command": []},
        {"command": "reviewer-cli review"},
        {"command": ["reviewer-cli", "review"]},  # no {base} placeholder
        {"command": ["reviewer-cli", ""]},
        {"event_type_path": ""},
        {"finding_event": 3},
        {"complete_event": None},
        {"finding_fields": {"severity": "level"}},
        {"finding_fields": "level"},
        {"timeout_seconds": 0},
        {"timeout_seconds": True},
        {"surprise": 1},
    ],
)
def test_the_loader_rejects_a_bad_local_review_block(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="local_review"):
        _adapter(tmp_path, local_review=_local_review(**overrides))


def test_a_local_review_needs_a_final_full_review(tmp_path: Path) -> None:
    """With no final review, the gate for a local reviewer would pass on no review."""
    # A self-consistent adapter with no final review and no command replies at all,
    # so only the local-review rule can reject it.
    nulls = {
        field: None
        for field in (
            "final_review_comment",
            "command_reply_marker",
            "final_review_done_pattern",
            "final_review_failed_pattern",
            "final_review_partial_pattern",
            *reviewer_state._INCREMENTAL_REVIEW_FIELDS,
            "rate_limited_pattern",
            "rate_limit_wait_pattern",
            "rate_limit_default_wait_minutes",
            "rate_limit_notice_marker",
        )
    }
    with pytest.raises(ValueError, match="local_review needs a final full review"):
        _adapter(tmp_path, **nulls)


def test_coderabbit_reviews_locally_with_its_cli() -> None:
    spec = CODERABBIT.local_review
    assert spec is not None
    assert spec.command[:2] == ("coderabbit", "review")
    assert "--agent" in spec.command and "--deep" in spec.command
    assert "{base}" in spec.command
    assert spec.finding_event == "finding" and spec.complete_event == "complete"
    preflight = CODERABBIT.local_preflight
    assert preflight is not None
    assert preflight.command == ("coderabbit", "usage", "--agent")
    assert preflight.auth_command == ("coderabbit", "auth", "status", "--agent")


def test_bugbot_has_no_local_review() -> None:
    """Its local review runs only inside the editor; there is no CLI to wrap yet."""
    assert BUGBOT.local_review is None
    assert BUGBOT.local_preflight is None


# --- Round caps ----------------------------------------------------------------


def test_the_repo_config_caps_local_rounds() -> None:
    caps = reviewer_state.load_round_caps()
    assert (caps.local_before_final, caps.local_after_final) == (3, 3)


def test_local_caps_are_read_from_the_config(tmp_path: Path) -> None:
    config = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    config["review_rounds"].update(max_local_before_final=5, max_local_after_final=2)
    caps = reviewer_state.load_round_caps(_write(tmp_path, config))
    assert (caps.local_before_final, caps.local_after_final) == (5, 2)


def test_no_review_rounds_block_means_the_defaults(tmp_path: Path) -> None:
    config = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    del config["review_rounds"]
    caps = reviewer_state.load_round_caps(_write(tmp_path, config))
    assert caps == reviewer_state.DEFAULT_ROUND_CAPS


@pytest.mark.parametrize(
    "rounds",
    [
        {"max_local_before_finale": 3},  # a misspelled knob must not fall back
        {"max_local_before_final": -1},
        {"max_local_after_final": True},
        {"max_local_after_final": "3"},
    ],
)
def test_bad_round_caps_are_rejected(tmp_path: Path, rounds: dict[str, Any]) -> None:
    config = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    config["review_rounds"] = rounds
    with pytest.raises(ValueError, match="review_rounds"):
        reviewer_state.load_round_caps(_write(tmp_path, config))


# --- Records and the local budget ---------------------------------------------


def _comment(body: str, at: str, login: str = "a-developer") -> dict[str, Any]:
    return {"body": body, "created_at": at, "updated_at": at, "user": {"login": login}}


def _record(head: str, at: str, round_: int = 1, findings: int = 0) -> dict[str, Any]:
    return _comment(
        reviewer_state.local_review_marker(
            {
                "reviewer": "x",
                "head": head,
                "base": BASE,
                "round": round_,
                "findings": findings,
            }
        )
        + "\nLocal review: 0 findings.",
        at,
    )


FINAL = "@coderabbitai full review"
_REPLY = "<!-- This is an auto-generated reply by CodeRabbit -->\n"
_FULL_DONE = _REPLY + "<summary>✅ Action performed</summary>\n\nFull review finished."
_FULL_REFUSED = _REPLY + (
    "<summary>⚠️ Action not completed</summary>\n\nReview rate limited.\n\n"
    "Your next included review will be available in 13 minutes."
)
_CAPS = RoundCaps(local_before_final=2, local_after_final=1)


def _state(comments: list[dict[str, Any]], head: str = HEAD) -> Any:
    return reviewer_state.local_review_state(comments, CODERABBIT, _CAPS, head)


def test_a_record_round_trips_through_its_marker() -> None:
    record = {"reviewer": "x", "head": HEAD, "base": BASE, "round": 2, "findings": 4}
    parsed = reviewer_state.local_review_records(
        [
            _comment(
                reviewer_state.local_review_marker(record) + "\nbody",
                "2026-10-13T01:00:00Z",
            )
        ]
    )
    assert parsed == [dict(record, at="2026-10-13T01:00:00Z")]


def test_a_malformed_marker_is_ignored() -> None:
    body = "<!-- local-review:v1 {not json -->"
    assert (
        reviewer_state.local_review_records([_comment(body, "2026-10-13T01:00:00Z")])
        == []
    )


def test_a_fresh_pr_may_run_a_local_review() -> None:
    state = _state([])
    assert state["configured"] is True
    assert state["phase"] == "before_final"
    assert state["before_final"] == {"used": 0, "cap": 2}
    assert state["allowed"] is True
    assert state["reviewed_head"] is False


def test_local_rounds_before_the_final_review_spend_its_budget() -> None:
    state = _state(
        [_record(OLD, "2026-10-13T01:00:00Z"), _record(OLD, "2026-10-13T02:00:00Z", 2)]
    )
    assert state["before_final"] == {"used": 2, "cap": 2}
    assert state["allowed"] is False


def test_local_rounds_after_the_final_review_count_separately() -> None:
    state = _state(
        [
            _record(OLD, "2026-10-13T01:00:00Z"),
            _comment(FINAL, "2026-10-13T02:00:00Z"),
            _comment(_FULL_DONE, "2026-10-13T02:10:00Z", "coderabbitai[bot]"),
            _record(OLD, "2026-10-13T03:00:00Z", 2),
        ]
    )
    assert state["phase"] == "after_final"
    assert state["before_final"]["used"] == 1
    assert state["after_final"] == {"used": 1, "cap": 1}
    assert state["allowed"] is False


def test_a_refused_final_request_does_not_start_the_after_final_phase() -> None:
    """The final review never ran, so the PR is still before it."""
    state = _state(
        [
            _record(OLD, "2026-10-13T01:00:00Z"),
            _comment(FINAL, "2026-10-13T02:00:00Z"),
            _comment(_FULL_REFUSED, "2026-10-13T02:00:09Z", "coderabbitai[bot]"),
        ]
    )
    assert state["phase"] == "before_final"
    assert state["before_final"] == {"used": 1, "cap": 2}
    assert state["allowed"] is True


def test_a_recorded_head_needs_no_second_local_review() -> None:
    state = _state([_record(HEAD, "2026-10-13T01:00:00Z", findings=3)])
    assert state["reviewed_head"] is True
    assert state["head_findings"] == 3


def test_a_reviewer_without_a_local_review_reports_none() -> None:
    state = reviewer_state.local_review_state([], BUGBOT, _CAPS, HEAD)
    assert state == {"configured": False}


# --- collect_state carries the local budget -----------------------------------


def _fake_gh(comments: list[dict[str, Any]]) -> Any:
    def _fake(args: list[str]) -> Any:
        if "graphql" in args:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "headRefOid": HEAD,
                            "mergeStateStatus": "CLEAN",
                            "reviewThreads": {"nodes": []},
                        }
                    }
                }
            }
        if any("/comments" in a for a in args):
            return comments
        return []

    return _fake


def test_collect_state_reports_the_local_budget(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        reviewer_state, "_gh_json", _fake_gh([_record(HEAD, "2026-10-13T01:00:00Z")])
    )
    state = reviewer_state.collect_state("o/r", 1, CODERABBIT, _CAPS)
    assert state["local_review"]["reviewed_head"] is True
    assert state["local_review"]["before_final"] == {"used": 1, "cap": 2}
    # The PR's one review request is the final full review: never a trigger.
    assert state["verdict"] == "none"
    assert state["needs_trigger"] is False
    assert state["needs_final_review"] is True


def test_collect_state_still_asks_a_pr_only_reviewer_to_trigger(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(reviewer_state, "_gh_json", _fake_gh([]))
    state = reviewer_state.collect_state("o/r", 1, BUGBOT, _CAPS)
    assert state["local_review"] == {"configured": False}
    assert state["needs_trigger"] is True


# --- local_review.py: run one local round and record it on the PR ---------------

_load("review_preflight")  # local_review imports it by name
local_review = _load("local_review")

_FINDING = {
    "kind": "issue",
    "level": "major",
    "file": "src/a.py",
    "line": 12,
    "headline": "Guard the empty case.",
    "text": "**Guard the empty case.** It raises on [].",
}
_DONE = {"kind": "done", "status": "ok"}


class _Fake:
    """Records every call so a test can assert what ran and what was posted."""

    def __init__(
        self,
        *,
        comments: list[dict[str, Any]] | None = None,
        local_head: str = HEAD,
        review: tuple[int, str, str] | BaseException = (0, "", ""),
        preflight: str = "available",
        remaining: list[int | None] | None = None,
        after_status: str | None = None,
    ) -> None:
        self.comments = comments or []
        self.local_head = local_head
        self.review = review
        self.preflight_status = preflight
        # Preflight readings, call by call (before the round, then after it).
        self.remaining = remaining if remaining is not None else [None]
        self.preflights = 0
        self.after_status = after_status  # status of the re-read after the round
        self.ran: list[list[str]] = []
        self.posted: list[str] = []
        self.pr_heads: list[str] = [HEAD]  # what the API reports, call by call
        self.remote_head = HEAD
        self.slept: list[float] = []

    def gh(self, args: Any) -> Any:
        if args[1].endswith("/pulls/7"):
            sha = self.pr_heads.pop(0) if len(self.pr_heads) > 1 else self.pr_heads[0]
            return {"head": {"sha": sha, "ref": "feat"}, "base": {"ref": "main"}}
        if args[1].endswith("/issues/7/comments"):
            return self.comments
        raise AssertionError(args)

    def git(self, args: Any) -> str:
        if list(args) == ["rev-parse", "HEAD"]:
            return self.local_head
        if args[0] == "merge-base":
            return BASE
        if args[0] == "ls-remote":
            return f"{self.remote_head}\trefs/heads/feat"
        return ""

    def run(self, argv: Any, timeout: float) -> tuple[int, str, str]:
        self.ran.append(list(argv))
        if isinstance(self.review, BaseException):
            raise self.review
        return self.review

    def post(self, repo: str, pr: int, body: str) -> str:
        self.posted.append(body)
        return "https://github.com/o/r/pull/7#issuecomment-1"

    def preflight(self, adapter: Any) -> dict[str, Any]:
        left = self.remaining[min(self.preflights, len(self.remaining) - 1)]
        status = self.after_status if self.preflights and self.after_status else None
        self.preflights += 1
        return {
            "status": status or self.preflight_status,
            "remaining": left,
            "recheck_after_seconds": 120,
            "remedy": None,
            "detail": "x",
        }

    def deps(self) -> Any:
        return local_review.Deps(
            gh=self.gh,
            git=self.git,
            run=self.run,
            post=self.post,
            preflight=self.preflight,
            sleep=self.slept.append,
        )


def _ndjson(*events: Any) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _run(
    tmp_path: Path, fake: _Fake, adapter: Any = None
) -> tuple[dict[str, Any], int]:
    result: tuple[dict[str, Any], int] = local_review.review(
        "o/r", 7, adapter or _adapter(tmp_path), _CAPS, fake.deps()
    )
    return result


def test_a_clean_round_reviews_against_the_merge_base_and_records_itself(
    tmp_path: Path,
) -> None:
    fake = _Fake(review=(0, _ndjson({"kind": "progress"}, _FINDING, _DONE), ""))
    result, code = _run(tmp_path, fake)
    assert code == 0
    assert result["status"] == "reviewed"
    assert fake.ran == [["reviewer-cli", "review", "--json", "--base", BASE]]
    assert result["findings"] == [
        {
            "severity": "major",
            "path": "src/a.py",
            "line": 12,
            "title": "Guard the empty case.",
            "body": "**Guard the empty case.** It raises on [].",
        }
    ]
    assert (result["phase"], result["round"], result["cap"]) == ("before_final", 1, 2)
    [body] = fake.posted
    [record] = reviewer_state.local_review_records(
        [{"body": body, "created_at": "2026-10-13T05:00:00Z"}]
    )
    assert record["head"] == HEAD and record["base"] == BASE
    assert record["findings"] == 1 and record["round"] == 1
    assert "@" not in body  # mentions nobody, so it is not a review request
    assert "`src/a.py:12`" in body


def test_a_reviewer_without_a_local_review_falls_back_to_pr_rounds(
    tmp_path: Path,
) -> None:
    fake = _Fake()
    result, code = _run(tmp_path, fake, BUGBOT)
    assert (result["status"], code) == ("not_configured", 0)
    assert fake.ran == [] and fake.posted == []


def test_an_unpushed_head_is_refused_without_waiting(tmp_path: Path) -> None:
    fake = _Fake(local_head=OLD)
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("head_mismatch", 65)
    assert fake.ran == [] and fake.slept == []


def test_a_just_pushed_head_waits_for_the_api_to_catch_up(tmp_path: Path) -> None:
    """Right after a push the PR API can still name the old head."""
    fake = _Fake(review=(0, _ndjson(_DONE), ""))
    fake.pr_heads = [OLD, OLD, HEAD]
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("reviewed", 0)
    assert len(fake.slept) == 2


def test_a_head_the_api_never_catches_up_with_is_a_mismatch(tmp_path: Path) -> None:
    fake = _Fake()
    fake.pr_heads = [OLD]
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("head_mismatch", 65)
    assert len(fake.slept) == local_review._API_CATCH_UP_TRIES
    assert fake.ran == []


def test_a_head_already_reviewed_locally_is_not_reviewed_again(tmp_path: Path) -> None:
    fake = _Fake(comments=[_record(HEAD, "2026-10-13T01:00:00Z", findings=2)])
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("already_reviewed", 0)
    assert result["head_findings"] == 2
    assert fake.ran == []


def test_a_spent_budget_runs_nothing(tmp_path: Path) -> None:
    fake = _Fake(
        comments=[
            _record(OLD, "2026-10-13T01:00:00Z"),
            _record(OLD, "2026-10-13T02:00:00Z", 2),
        ]
    )
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("budget_spent", 65)
    assert result["used"] == 2 and result["cap"] == 2
    assert fake.ran == []


def test_an_exhausted_allowance_waits_without_reviewing(tmp_path: Path) -> None:
    fake = _Fake(preflight="exhausted")
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("exhausted", 75)
    assert result["recheck_after_seconds"] == 120
    assert fake.ran == [] and fake.posted == []


def test_a_review_that_never_completes_is_a_failure_and_records_nothing(
    tmp_path: Path,
) -> None:
    fake = _Fake(review=(0, _ndjson(_FINDING), ""))
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("failed", 1)
    assert fake.posted == []


def test_a_failing_command_reports_its_stderr(tmp_path: Path) -> None:
    fake = _Fake(review=(2, "", "boom: not logged in"))
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("failed", 1)
    assert "not logged in" in result["detail"]
    assert result["remedy"] == "run the doctor"


def test_a_missing_cli_names_the_install_remedy(tmp_path: Path) -> None:
    fake = _Fake(review=FileNotFoundError("reviewer-cli"))
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("failed", 1)
    assert result["remedy"] == "install it"


def test_a_hung_review_is_a_failure(tmp_path: Path) -> None:
    fake = _Fake(review=subprocess.TimeoutExpired("reviewer-cli", 900))
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("failed", 1)
    assert "timed out" in result["detail"]
    assert fake.posted == []


def test_a_git_failure_finding_the_base_is_reported_not_raised(tmp_path: Path) -> None:
    """An unreachable remote must yield ``failed``, not a traceback."""
    fake = _Fake()
    real_git = fake.git

    def git(args: Any) -> str:
        if args[0] == "fetch":
            raise subprocess.CalledProcessError(
                128, ["git", *args], stderr="fatal: could not read from remote"
            )
        return real_git(args)

    fake.git = git  # type: ignore[method-assign]
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("failed", 1)
    assert "could not read from remote" in result["detail"]
    assert fake.ran == [] and fake.posted == []


def test_a_github_failure_is_reported_not_raised(tmp_path: Path) -> None:
    """An auth or network failure reading the PR must yield ``failed``."""
    fake = _Fake()

    def gh(args: Any) -> Any:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="HTTP 401")

    fake.gh = gh  # type: ignore[method-assign]
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("failed", 1)
    assert "HTTP 401" in result["detail"]
    assert fake.ran == []


def test_a_failed_record_post_still_returns_the_findings(tmp_path: Path) -> None:
    """The review already ran and spent its allowance: don't lose its findings."""
    fake = _Fake(review=(0, _ndjson(_FINDING, _DONE), ""))

    def post(repo: str, pr: int, body: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")

    fake.post = post  # type: ignore[method-assign]
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("failed", 1)
    assert "HTTP 502" in result["detail"]
    assert len(result["findings"]) == 1
    # Re-running would spend a second review on the same head, so hand back the
    # record to post instead.
    [record] = reviewer_state.local_review_records(
        [{"body": result["record_body"], "created_at": "2026-10-13T05:00:00Z"}]
    )
    assert record["head"] == HEAD
    assert "don't re-run" in result["detail"]


def test_main_reports_a_bad_config_as_a_usage_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert local_review.main(["--repo", "o/r", "--pr", "7", "--config", str(bad)]) == 64


def test_main_with_no_active_reviewer_is_not_configured(
    tmp_path: Path, capsys: Any
) -> None:
    config = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    config["active"] = None
    path = _write(tmp_path, config)
    assert local_review.main(["--repo", "o/r", "--pr", "7", "--config", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["reviewer"], result["status"]) == (None, "not_configured")


# --- Remaining reviews before and after a round --------------------------------

_START_FAILURE = "Error: Failed to start review. Please try again."


def test_a_round_records_remaining_before_and_after(tmp_path: Path) -> None:
    fake = _Fake(review=(0, _ndjson(_FINDING, _DONE), ""), remaining=[4, 2])
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("reviewed", 0)
    assert result["remaining_before"] == 4
    assert result["remaining_after"] == 2
    [body] = fake.posted
    [record] = reviewer_state.local_review_records(
        [{"body": body, "created_at": "2026-10-14T05:00:00Z"}]
    )
    assert record["remaining_before"] == 4 and record["remaining_after"] == 2
    assert record["head"] == HEAD and record["findings"] == 1
    assert "Local allowance: 4 -> 2 reviews left." in body


def test_an_unreadable_remaining_is_recorded_as_null(tmp_path: Path) -> None:
    fake = _Fake(review=(0, _ndjson(_DONE), ""))
    result, _ = _run(tmp_path, fake)
    assert result["remaining_before"] is None and result["remaining_after"] is None
    [body] = fake.posted
    [record] = reviewer_state.local_review_records(
        [{"body": body, "created_at": "2026-10-14T05:00:00Z"}]
    )
    assert record["remaining_before"] is None and record["remaining_after"] is None
    assert "reviews left" not in body


def test_a_half_readable_allowance_is_shown_with_a_question_mark(
    tmp_path: Path,
) -> None:
    fake = _Fake(review=(0, _ndjson(_DONE), ""), remaining=[4, None])
    _run(tmp_path, fake)
    [body] = fake.posted
    assert "Local allowance: 4 -> ? reviews left." in body


def test_a_record_without_the_remaining_keys_still_parses_and_budgets() -> None:
    """Records from older rounds on a PR keep counting."""
    state = _state([_record(HEAD, "2026-10-14T01:00:00Z", findings=1)])
    assert state["reviewed_head"] is True and state["before_final"]["used"] == 1


def test_a_start_failure_at_low_remaining_is_exhausted(tmp_path: Path) -> None:
    """One left cannot pay a two-review deep round: a wait, not a CLI failure."""
    fake = _Fake(
        review=(1, "", _START_FAILURE), remaining=[3, 1], after_status="exhausted"
    )
    result, code = _run(tmp_path, fake, _capacity_adapter(tmp_path))
    assert (result["status"], code) == ("exhausted", 75)
    assert result["remaining_before"] == 3
    assert result["remaining_after"] == 1
    assert result["recheck_after_seconds"] == 120  # the post-failure preflight's wait
    assert "failed to start" in result["detail"]
    assert fake.posted == []


def test_a_start_failure_with_a_pool_still_above_the_minimum_stays_failed(
    tmp_path: Path,
) -> None:
    """A real CLI or network failure must not be masked as a wait."""
    fake = _Fake(review=(1, "", _START_FAILURE), remaining=[3, 3])
    result, code = _run(tmp_path, fake, _capacity_adapter(tmp_path))
    assert (result["status"], code) == ("failed", 1)
    assert result["remaining_before"] == 3 and result["remaining_after"] == 3


def test_a_start_failure_is_read_from_stdout_too(tmp_path: Path) -> None:
    fake = _Fake(review=(1, _START_FAILURE, ""), remaining=[3, 1])
    result, _ = _run(tmp_path, fake, _capacity_adapter(tmp_path))
    assert result["status"] == "exhausted"


def test_a_start_failure_at_high_remaining_stays_failed(tmp_path: Path) -> None:
    fake = _Fake(review=(1, "", _START_FAILURE), remaining=[10, 10])
    result, code = _run(tmp_path, fake, _capacity_adapter(tmp_path))
    assert (result["status"], code) == ("failed", 1)
    assert result["remaining_before"] == 10


def test_a_start_failure_reread_as_exhausted_waits_its_recheck(
    tmp_path: Path,
) -> None:
    fake = _Fake(
        review=(1, "", _START_FAILURE), remaining=[10, 1], after_status="exhausted"
    )
    result, _ = _run(tmp_path, fake, _capacity_adapter(tmp_path))
    assert result["status"] == "exhausted"
    assert result["recheck_after_seconds"] == 120


def test_a_capacity_wait_without_a_reread_wait_uses_the_adapters_cap(
    tmp_path: Path,
) -> None:
    """An ``unknown`` re-read has no wait of its own; the adapter's cap applies."""
    fake = _Fake(
        review=(1, "", _START_FAILURE), remaining=[3, 1], after_status="unknown"
    )
    result, _ = _run(tmp_path, fake, _capacity_adapter(tmp_path))
    assert result["status"] == "exhausted"
    assert result["recheck_after_seconds"] == 300


@pytest.mark.parametrize("after_remaining", [10, 2, None])
def test_an_exhausted_state_without_a_low_count_stays_failed(
    tmp_path: Path, after_remaining: int | None
) -> None:
    """The state can be unavailable with plenty left (or the count unreadable)."""
    fake = _Fake(
        review=(1, "", _START_FAILURE),
        remaining=[10, after_remaining],
        after_status="exhausted",
    )
    result, code = _run(tmp_path, fake, _capacity_adapter(tmp_path))
    assert (result["status"], code) == ("failed", 1)


def test_a_start_failure_that_drained_the_pool_is_exhausted(tmp_path: Path) -> None:
    """The pool is shared: a re-read below the minimum proves it ran dry."""
    fake = _Fake(review=(1, "", _START_FAILURE), remaining=[10, 1])
    result, _ = _run(tmp_path, fake, _capacity_adapter(tmp_path))
    assert result["status"] == "exhausted"


def test_a_start_failure_with_no_readable_remaining_stays_failed(
    tmp_path: Path,
) -> None:
    fake = _Fake(review=(1, "", _START_FAILURE))
    result, _ = _run(tmp_path, fake, _capacity_adapter(tmp_path))
    assert result["status"] == "failed"


def test_another_failure_at_low_remaining_stays_failed(tmp_path: Path) -> None:
    fake = _Fake(review=(1, "", "Error: boom"), remaining=[3, 1])
    result, _ = _run(tmp_path, fake, _capacity_adapter(tmp_path))
    assert result["status"] == "failed"


def test_the_start_failure_is_matched_on_the_adapters_pattern(
    tmp_path: Path,
) -> None:
    adapter = _adapter(
        tmp_path,
        local_review=_local_review(start_failure_pattern="could not begin"),
        local_preflight=_preflight(min_remaining=2),
    )
    pool_short = _Fake(review=(1, "", "Error: could not begin"), remaining=[3, 1])
    assert _run(tmp_path, pool_short, adapter)[0]["status"] == "exhausted"
    other_text = _Fake(review=(1, "", _START_FAILURE), remaining=[3, 1])
    assert _run(tmp_path, other_text, adapter)[0]["status"] == "failed"


def test_a_start_failure_with_the_default_minimum_is_exhausted_only_when_empty(
    tmp_path: Path,
) -> None:
    adapter = _capacity_adapter(tmp_path, min_remaining=1)
    empty = _Fake(review=(1, "", _START_FAILURE), remaining=[1, 0])
    assert _run(tmp_path, empty, adapter)[0]["status"] == "exhausted"
    one_left = _Fake(review=(1, "", _START_FAILURE), remaining=[2, 1])
    assert _run(tmp_path, one_left, adapter)[0]["status"] == "failed"


def test_no_start_failure_pattern_never_reads_exhausted(tmp_path: Path) -> None:
    """Without an adapter pattern a start failure is a plain failure, however low."""
    adapter = _adapter(tmp_path, local_preflight=_preflight(min_remaining=2))
    assert adapter.local_review.start_failure_pattern is None
    fake = _Fake(review=(1, "", _START_FAILURE), remaining=[1, 0])
    result, code = _run(tmp_path, fake, adapter)
    assert (result["status"], code) == ("failed", 1)


def test_a_failing_post_round_read_still_records_the_round(tmp_path: Path) -> None:
    """The round already ran: a usage read that raises must not fail it."""
    fake = _Fake(review=(0, _ndjson(_FINDING, _DONE), ""), remaining=[4])
    reads = {"n": 0}
    original = fake.preflight

    def flaky(adapter: Any) -> dict[str, Any]:
        reads["n"] += 1
        if reads["n"] > 1:
            raise PermissionError("usage is not readable")
        return original(adapter)

    fake.preflight = flaky  # type: ignore[method-assign]
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("reviewed", 0)
    assert result["remaining_before"] == 4 and result["remaining_after"] is None
    [body] = fake.posted
    [record] = reviewer_state.local_review_records(
        [{"body": body, "created_at": "2026-10-14T05:00:00Z"}]
    )
    assert record["remaining_after"] is None and record["findings"] == 1


@pytest.mark.parametrize(
    ("after_status", "after_remaining"), [("unknown", None), ("exhausted", 0)]
)
def test_an_unreadable_or_empty_post_round_read_still_reviews(
    tmp_path: Path, after_status: str, after_remaining: int | None
) -> None:
    """``unknown`` carries no count (the real preflight returns None); empty is 0."""
    fake = _Fake(
        review=(0, _ndjson(_DONE), ""),
        remaining=[4, after_remaining],
        after_status=after_status,
    )
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("reviewed", 0)
    assert result["remaining_after"] == after_remaining
    assert len(fake.posted) == 1


def test_a_failing_post_failure_read_stays_failed(tmp_path: Path) -> None:
    fake = _Fake(review=(1, "", _START_FAILURE), remaining=[3])

    def boom(adapter: Any) -> dict[str, Any]:
        if fake.preflights:
            raise OSError("no usage")
        fake.preflights += 1
        return {"status": "available", "remaining": 3, "remedy": None, "detail": "x"}

    fake.preflight = boom  # type: ignore[method-assign]
    result, code = _run(tmp_path, fake, _capacity_adapter(tmp_path))
    assert (result["status"], code) == ("failed", 1)
    assert result["remaining_after"] is None


def test_the_early_exhausted_result_carries_remaining_before(tmp_path: Path) -> None:
    fake = _Fake(preflight="exhausted", remaining=[1])
    result, code = _run(tmp_path, fake)
    assert (result["status"], code) == ("exhausted", 75)
    assert result["remaining_before"] == 1
    assert fake.ran == []


def test_a_capacity_wait_needs_a_preflight_to_say_how_long() -> None:
    """A capacity failure with no preflight is a bug: it raises, never asserts."""
    adapter = BUGBOT._replace(local_preflight=None)
    with pytest.raises(ValueError, match="preflight"):
        local_review._recheck_after(adapter, {"status": "unknown"})
