"""Tests for resolve-pr-concerns' ``merge_gate.py`` (the merge-readiness report).

The script lives beside ``reviewer_state.py`` and ``human_review.py`` outside the
importable package tree, so all three are loaded by path (``reviewer_state`` first:
the others import it by name). ``evaluate_gate`` is a pure function over
already-fetched PR state, which is what lets the whole policy be tested without a
live PR. The collectors are driven with ``reviewer_state._gh_json`` stubbed out
(``merge_gate`` and ``human_review`` both read GitHub through it).

Required checks are never hard-coded: the tests inject neutral names
(``ci / lint``, ``ci / tests``) through ``required_checks`` or a faked
``rules/branches/{base}`` response.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess  # nosec B404 -- fixed argv, in-repo script, no shell
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / ".agents" / "skills" / "resolve-pr-concerns" / "scripts"
_SCRIPT = _SCRIPTS / "merge_gate.py"


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
merge_gate = _load("merge_gate")
evaluate_gate = merge_gate.evaluate_gate

REPO = "owner/repo"
REQUIRED = ("ci / lint", "ci / tests")
GREEN = {name: "success" for name in REQUIRED}
HEAD = "a" * 40
NOT_FOUND = "gh: Not Found (HTTP 404)"
THREADS_BLOCKER = (
    "{n} unresolved review thread(s); the base branch's rules require resolution: "
    "a human resolves answered threads (the agent never resolves them)"
)


def _gate(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "check_conclusions": dict(GREEN),
        "required_checks": REQUIRED,
        "reviewer_verdict": "pass",
        "open_reviewer_findings": 0,
        "unresolved_thread_total": 0,
        "merge_state_status": "CLEAN",
        "pr_state": "OPEN",
        "reviewer_name": "Review Bot",
        "trigger_comment": "review-bot run",
    }
    kwargs.update(overrides)
    return evaluate_gate(**kwargs)


# --- required checks -----------------------------------------------------------


def test_all_green_passes() -> None:
    verdict = _gate()
    assert verdict["mergeable"] is True
    assert verdict["blockers"] == []


def test_unstable_is_mergeable_because_only_the_required_checks_count() -> None:
    """UNSTABLE means a non-required check is failing; GitHub still allows it."""
    assert _gate(merge_state_status="UNSTABLE")["mergeable"] is True


def test_dirty_blocks() -> None:
    assert _gate(merge_state_status="DIRTY")["mergeable"] is False


def test_missing_required_check_blocks() -> None:
    conclusions = dict(GREEN)
    del conclusions["ci / lint"]
    verdict = _gate(check_conclusions=conclusions)
    assert verdict["mergeable"] is False
    assert verdict["blockers"] == ["required check has not reported: ci / lint"]


def test_every_required_check_is_checked_not_just_the_first() -> None:
    conclusions = dict(GREEN)
    del conclusions["ci / tests"]
    assert _gate(check_conclusions=conclusions)["mergeable"] is False


def test_failing_required_check_blocks() -> None:
    conclusions = dict(GREEN)
    conclusions["ci / tests"] = "failure"
    verdict = _gate(check_conclusions=conclusions)
    assert verdict["mergeable"] is False
    assert verdict["blockers"] == ["required check not green: ci / tests = failure"]


def test_skipped_required_check_is_allowed() -> None:
    """GitHub counts neutral/skipped as satisfying a required check."""
    conclusions = dict(GREEN)
    conclusions["ci / lint"] = "skipped"
    conclusions["ci / tests"] = "NEUTRAL"
    assert _gate(check_conclusions=conclusions)["mergeable"] is True


def test_a_failing_check_outside_the_required_set_is_info_only() -> None:
    conclusions = {**GREEN, "docs / spelling": "failure"}
    verdict = _gate(check_conclusions=conclusions)
    assert verdict["mergeable"] is True
    assert any("docs / spelling" in note for note in verdict["info"]["notes"])


def test_no_rules_means_every_reported_check_must_pass() -> None:
    """With no required-checks rule, every reported check must be non-failing."""
    green = _gate(required_checks=None, check_conclusions={"anything": "success"})
    assert green["mergeable"] is True
    red = _gate(
        required_checks=None,
        check_conclusions={"ci / lint": "success", "ci / build": "timed_out"},
    )
    assert red["mergeable"] is False
    assert red["blockers"] == ["check not green: ci / build = timed_out"]
    assert red["info"]["required_checks"] is None


def test_without_rules_a_running_check_has_not_finished() -> None:
    verdict = _gate(required_checks=None, check_conclusions={"ci / e2e": "in_progress"})
    assert verdict["blockers"] == ["check has not finished: ci / e2e = in_progress"]


def test_an_empty_required_set_still_needs_the_reported_checks() -> None:
    """A rule naming no contexts reads as no rule: the stricter reading wins."""
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": []},
        }
    ]
    assert merge_gate.required_checks_from_rules(rules) is None


# --- merge state ---------------------------------------------------------------


def test_behind_blocks_only_under_a_strict_policy() -> None:
    """A merge queue replaces the strict policy: BEHIND is then not a blocker."""
    strict = _gate(merge_state_status="BEHIND", strict_up_to_date=True)
    assert strict["mergeable"] is False
    assert any("BEHIND" in b for b in strict["blockers"])
    loose = _gate(merge_state_status="BEHIND", strict_up_to_date=False)
    assert loose["mergeable"] is True
    assert any("BEHIND" in note for note in loose["info"]["notes"])
    assert _gate(merge_state_status="BEHIND")["mergeable"] is True  # default: loose


def test_unknown_merge_state_blocks() -> None:
    """UNKNOWN means not computed yet -- never read as ready."""
    assert _gate(merge_state_status="UNKNOWN")["mergeable"] is False


# --- reviewer half -------------------------------------------------------------


def test_reviewer_never_reviewed_blocks_even_with_green_checks() -> None:
    verdict = _gate(reviewer_verdict="none")
    assert verdict["mergeable"] is False
    assert any("Review Bot" in b for b in verdict["blockers"])


def test_never_reviewed_blocker_text() -> None:
    assert _gate(reviewer_verdict="none")["blockers"] == [
        "Review Bot has not reviewed the current head SHA — trigger `review-bot run`"
    ]


def test_never_reviewed_blocker_names_the_configured_reviewer_and_trigger() -> None:
    verdict = _gate(
        reviewer_verdict="none",
        reviewer_name="Other Reviewer",
        trigger_comment="@other-reviewer review",
    )
    assert any(
        "Other Reviewer" in b and "`@other-reviewer review`" in b
        for b in verdict["blockers"]
    )


def test_unrecognized_verdict_blocks() -> None:
    verdict = _gate(reviewer_verdict="weird")
    assert any("unrecognized Review Bot verdict" in b for b in verdict["blockers"])


def test_open_reviewer_finding_blocks() -> None:
    verdict = _gate(reviewer_verdict="findings", open_reviewer_findings=2)
    assert verdict["mergeable"] is False
    assert "2 unresolved Review Bot finding(s)" in verdict["blockers"]


def test_reviewer_findings_all_resolved_passes() -> None:
    """Addressed findings leave zero open threads; zero open means addressed."""
    assert (
        _gate(reviewer_verdict="findings", open_reviewer_findings=0)["mergeable"]
        is True
    )


def test_no_reviewer_skips_the_reviewer_half() -> None:
    """``reviewer_verdict=None`` (``"active": null``): no reviewer blockers at all."""
    verdict = _gate(
        reviewer_verdict=None,
        open_reviewer_findings=3,
        final_review_outcome="not_requested",
    )
    assert verdict["mergeable"] is True
    assert verdict["info"]["reviewer"] is None
    assert any("no PR reviewer" in note for note in verdict["info"]["notes"])


def test_no_reviewer_keeps_the_other_halves() -> None:
    verdict = _gate(
        reviewer_verdict=None,
        unresolved_thread_total=1,
        threads_must_resolve=True,
        changes_requested_by=["octocat"],
        check_conclusions={"ci / lint": "failure", "ci / tests": "success"},
    )
    assert len(verdict["blockers"]) == 3


def test_unthreaded_findings_are_info_not_blockers() -> None:
    found = [{"source": "review", "id": 9, "text": "Consider a guard here"}]
    verdict = _gate(unthreaded_findings=found)
    assert verdict["mergeable"] is True
    assert verdict["blockers"] == []
    assert verdict["info"]["unthreaded_findings"] == found
    assert any("disposition" in note for note in verdict["info"]["notes"])


def test_unresolved_non_reviewer_thread_blocks() -> None:
    """With thread resolution required by the rules, any open thread blocks."""
    verdict = _gate(unresolved_thread_total=1, threads_must_resolve=True)
    assert verdict["mergeable"] is False
    assert verdict["blockers"] == [THREADS_BLOCKER.format(n=1)]
    assert verdict["info"]["unresolved_threads"] == 1


def test_unresolved_threads_block_only_when_the_rules_require_resolution() -> None:
    required = _gate(unresolved_thread_total=2, threads_must_resolve=True)
    assert required["mergeable"] is False
    not_required = _gate(unresolved_thread_total=2, threads_must_resolve=False)
    assert not_required["mergeable"] is True
    assert not_required["blockers"] == []
    assert not_required["info"]["unresolved_threads"] == 2
    assert any(
        "2 unresolved review thread(s)" in note and "not a blocker" in note
        for note in not_required["info"]["notes"]
    )
    assert _gate(unresolved_thread_total=2)["mergeable"] is True  # default: not


def test_the_threads_blocker_leaves_resolution_to_a_human() -> None:
    (blocker,) = _gate(unresolved_thread_total=1, threads_must_resolve=True)["blockers"]
    assert "human" in blocker and "never resolves" in blocker


def test_threads_must_resolve_reads_the_pull_request_rule() -> None:
    assert merge_gate.threads_must_resolve(_RULES) is True
    off = [
        {
            "type": "pull_request",
            "parameters": {"required_review_thread_resolution": False},
        }
    ]
    assert merge_gate.threads_must_resolve(off) is False
    assert merge_gate.threads_must_resolve([{"type": "deletion"}]) is False
    assert merge_gate.threads_must_resolve(None) is False  # a 404


def _final_gate(outcome: str) -> Any:
    return _gate(
        reviewer_name="Review Bot",
        trigger_comment="review-bot run",
        final_review_outcome=outcome,
        final_review_comment="review-bot full review",
    )


def test_final_review_not_requested_blocks_with_the_command() -> None:
    assert _final_gate("not_requested")["blockers"] == [
        (
            "Review Bot has not run its final full review — post "
            "`review-bot full review` once incremental reviews are clean"
        )
    ]


def test_final_review_pending_blocks() -> None:
    assert _final_gate("pending")["blockers"] == [
        "Review Bot's final full review has not finished"
    ]


def test_final_review_failed_blocks_with_the_retry_rule() -> None:
    (blocker,) = _final_gate("failed")["blockers"]
    assert "20" in blocker and "once" in blocker and "escalate" in blocker


def test_final_review_finished_or_not_required_passes() -> None:
    assert _final_gate("finished")["mergeable"] is True
    assert _gate()["mergeable"] is True  # default: not_required


def test_unrecognized_final_review_outcome_blocks() -> None:
    assert _final_gate("weird")["mergeable"] is False


def _local(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "reviewer_verdict": "none",
        "local_review": True,
        "final_review_outcome": "finished",
        "final_review_comment": "review-bot full review",
        "reviewer_name": "Review Bot",
        "trigger_comment": "review-bot run",
    }
    kwargs.update(overrides)
    return _gate(**kwargs)


def test_a_locally_reviewed_pr_needs_no_verdict_on_the_moved_head() -> None:
    assert _local()["mergeable"] is True


def test_a_locally_reviewed_pr_still_needs_its_full_review() -> None:
    verdict = _local(final_review_outcome="not_requested")
    assert verdict["mergeable"] is False
    assert not any("review-bot run`" in b for b in verdict["blockers"])
    assert any("`review-bot full review`" in b for b in verdict["blockers"])
    assert any("once local reviews" in b for b in verdict["blockers"])


def test_a_locally_reviewed_pr_with_an_open_finding_is_blocked() -> None:
    assert _local(open_reviewer_findings=1)["mergeable"] is False


# --- human half ----------------------------------------------------------------


def test_a_human_request_for_changes_blocks() -> None:
    verdict = _gate(changes_requested_by=["octocat"])
    assert verdict["mergeable"] is False
    assert verdict["blockers"] == [
        (
            "@octocat requested changes — answer every item (human_review.py), then "
            "re-request their review; only their new review clears it"
        ),
    ]


def test_each_requesting_reviewer_gets_a_blocker() -> None:
    blockers = _gate(changes_requested_by=["reviewer-a", "reviewer-b"])["blockers"]
    assert [b.split()[0] for b in blockers] == ["@reviewer-a", "@reviewer-b"]


def test_no_request_for_changes_leaves_the_gate_as_before() -> None:
    assert _gate(changes_requested_by=[]) == _gate()


# --- PR state, audit, accumulation ---------------------------------------------


def test_a_false_audit_adds_a_blocker_and_absence_does_not() -> None:
    assert _gate()["mergeable"] is True  # absent: no blocker
    assert _gate(acceptance_audit_clean=True)["mergeable"] is True
    verdict = _gate(acceptance_audit_clean=False)
    assert verdict["mergeable"] is False
    assert verdict["blockers"] == ["acceptance-criteria audit is not clean"]


def test_already_merged_pr_is_terminal_success_not_a_blocker() -> None:
    """A MERGED PR stops having a computable mergeStateStatus (it goes UNKNOWN).

    Without this, the report would list a blocker on work that already landed.
    """
    verdict = _gate(pr_state="MERGED", merge_state_status="UNKNOWN")
    assert verdict["already_merged"] is True
    assert verdict["mergeable"] is False
    assert verdict["blockers"] == []


def test_closed_unmerged_pr_blocks_with_a_clear_reason() -> None:
    verdict = _gate(pr_state="CLOSED", merge_state_status="UNKNOWN")
    assert verdict["mergeable"] is False
    assert any("CLOSED" in b for b in verdict["blockers"])


def test_open_pr_defaults_to_not_already_merged() -> None:
    assert _gate()["already_merged"] is False


def test_blockers_are_accumulated_not_short_circuited() -> None:
    verdict = _gate(
        merge_state_status="BEHIND",
        strict_up_to_date=True,
        reviewer_verdict="none",
        acceptance_audit_clean=False,
        check_conclusions={"ci / lint": "failure"},
    )
    assert len(verdict["blockers"]) >= 5


# --- the output is a report, never permission ----------------------------------

# Imperative or permission-granting wording a report must never use. ``\bmerge\b``
# leaves ``merged``, ``mergeable`` and ``mergeStateStatus`` alone.
_PERMISSION_RE = re.compile(
    r"\bmerge\b|\bsquash\b|\bgo ahead\b|\bok to\b|\bsafe to\b|\bpermission\b"
    r"|\bauthori[sz]",
    re.IGNORECASE,
)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def test_the_output_never_says_merge() -> None:
    """Merge-ready is a report to the human: no text in it tells anyone to merge."""
    verdicts = [
        _gate(),
        _gate(pr_state="MERGED"),
        _gate(
            pr_state="CLOSED",
            merge_state_status="BEHIND",
            strict_up_to_date=True,
            reviewer_verdict="weird",
            open_reviewer_findings=1,
            unresolved_thread_total=2,
            threads_must_resolve=True,
            acceptance_audit_clean=False,
            changes_requested_by=["octocat"],
            final_review_outcome="failed",
            final_review_comment="review-bot full review",
            check_conclusions={"ci / lint": "failure", "docs": "failure"},
        ),
        _gate(merge_state_status="BEHIND", reviewer_verdict="none"),
        _gate(reviewer_verdict=None, required_checks=None),
        _gate(final_review_outcome="not_requested", final_review_comment="x"),
        _gate(final_review_outcome="pending", final_review_comment="x"),
        _gate(final_review_outcome="odd", final_review_comment="x"),
        _gate(
            required_checks=None,
            check_conclusions={"a": "queued", "b": "failure"},
            unthreaded_findings=[{"source": "review", "id": 1, "text": "t"}],
        ),
        _gate(merge_state_status="DIRTY"),
        _gate(unresolved_thread_total=3, threads_must_resolve=False),
    ]
    verdicts.append(merge_gate.evaluate_stack([(1, verdicts[0]), (2, verdicts[2])]))
    verdicts.append(merge_gate.single_layer_stack(5, verdicts[0]))
    for verdict in verdicts:
        texts = [*verdict["blockers"], *_strings(verdict["info"])]
        offending = [t for t in texts if _PERMISSION_RE.search(t)]
        assert not offending, offending


_MERGE_PATH_RE = re.compile(r"/merges?(?:/|\?|$)")


def _assert_reads_only(calls: list[list[str]]) -> None:
    """Every ``gh`` call is a read: no ``pr merge``, no merge API, no write method."""
    assert calls
    for call in calls:
        assert call[:2] != ["pr", "merge"], call
        assert "merge" not in call[:3], call
        assert not any(flag in call for flag in ("-X", "--method")), call
        if call[0] == "pr":
            assert call[1] == "view", call
            continue
        assert call[0] == "api", call
        if call[1] == "graphql":
            query = next(a for a in call if a.startswith("query="))
            assert "mutation" not in query, call
        else:
            assert not _MERGE_PATH_RE.search(call[1]), call


def test_the_script_never_runs_a_merge(
    fake_gh: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Drive every collection path and check the argv of each ``gh`` call."""
    source = _SCRIPT.read_text()
    assert '"merge"' not in source and "'merge'" not in source
    assert "pr merge" not in source
    stacks = [{"number": 1, "pull_requests": [{"number": 3}, {"number": 4}]}]
    fake = fake_gh(stacks=stacks, threads=[[False]])
    merge_gate.collect_and_evaluate(REPO, 3)
    merge_gate.collect_and_evaluate(
        REPO, 3, config_path=_config(tmp_path, _local_reviewer_key())
    )
    merge_gate.collect_and_evaluate_stack(REPO, 4)
    _assert_reads_only(fake.calls)
    for argv in (["--pr", "4", "--stack"], ["--pr", "3"]):
        fake = fake_gh(stacks=stacks)
        _main(monkeypatch, *argv)
        _assert_reads_only(fake.calls)
    capsys.readouterr()


# --- stacks --------------------------------------------------------------------


def _verdict(*blockers: str, merged: bool = False) -> dict[str, Any]:
    return {
        "mergeable": not merged and not blockers,
        "already_merged": merged,
        "blockers": list(blockers),
        "info": {"unthreaded_findings": [], "notes": []},
    }


def test_stack_is_mergeable_only_when_every_layer_is() -> None:
    green = merge_gate.evaluate_stack(
        [(1, _verdict()), (2, _verdict()), (3, _verdict())]
    )
    assert green["mergeable"] is True and green["blockers"] == []
    red = merge_gate.evaluate_stack(
        [
            (1, _verdict()),
            (2, _verdict("required check not green: ci / tests = failure")),
            (3, _verdict()),
        ]
    )
    assert red["mergeable"] is False
    assert red["blockers"] == ["#2: required check not green: ci / tests = failure"]
    assert [layer["pr"] for layer in red["layers"]] == [1, 2, 3]


def test_stack_ignores_a_lower_layer_that_already_landed() -> None:
    verdict = merge_gate.evaluate_stack([(1, _verdict(merged=True)), (2, _verdict())])
    assert verdict["mergeable"] is True
    assert verdict["already_merged"] is False


def test_fully_merged_stack_is_terminal_success() -> None:
    verdict = merge_gate.evaluate_stack(
        [(1, _verdict(merged=True)), (2, _verdict(merged=True))]
    )
    assert verdict["mergeable"] is False
    assert verdict["already_merged"] is True
    assert verdict["blockers"] == []
    assert verdict["layers"] == [
        {"pr": 1, **_verdict(merged=True)},
        {"pr": 2, **_verdict(merged=True)},
    ]


def test_stack_info_tags_unthreaded_findings_with_their_layer() -> None:
    finding = {"source": "review", "id": 4, "text": "t"}
    layer = _verdict()
    layer["info"]["unthreaded_findings"] = [finding]
    verdict = merge_gate.evaluate_stack([(1, _verdict()), (2, layer)])
    assert verdict["info"]["unthreaded_findings"] == [{"pr": 2, **finding}]
    assert verdict["mergeable"] is True


def test_empty_stack_fails_loudly() -> None:
    with pytest.raises(ValueError, match="no pull requests"):
        merge_gate.evaluate_stack([])


def test_a_single_layer_stack_says_so_in_info() -> None:
    verdict = merge_gate.single_layer_stack(12, _verdict())
    assert verdict["mergeable"] is True
    assert [layer["pr"] for layer in verdict["layers"]] == [12]
    assert any("not in a stack" in note for note in verdict["info"]["notes"])


# --- live collection -----------------------------------------------------------

_RULES = [
    {
        "type": "pull_request",
        "parameters": {"required_review_thread_resolution": True},
    },
    {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": False,
            "required_status_checks": [
                {"context": "ci / lint", "integration_id": 1},
                {"context": "ci / tests"},
            ],
        },
    },
]


class _FakeGh:
    """Answer every ``gh`` read the collectors make, by endpoint."""

    def __init__(
        self,
        *,
        state: str = "OPEN",
        merge_state: str = "CLEAN",
        base: str = "main",
        rules: Any = _RULES,
        check_runs: list[dict[str, Any]] | None = None,
        statuses: list[dict[str, Any]] | None = None,
        threads: list[list[bool]] | None = None,
        reviews: list[dict[str, Any]] | None = None,
        stacks: Any = (),
    ) -> None:
        self.pull = {
            "state": state,
            "mergeStateStatus": merge_state,
            "headRefOid": HEAD,
            "baseRefName": base,
        }
        self.rules = rules
        self.check_runs = (
            check_runs
            if check_runs is not None
            else [{"name": n, "conclusion": "success"} for n in REQUIRED]
        )
        self.statuses = statuses or []
        self.threads = threads or [[]]
        self.reviews = reviews or []
        self.stacks = stacks
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> Any:
        self.calls.append(list(args))
        if args[:2] == ["pr", "view"]:
            return dict(self.pull)
        if args[:2] == ["api", "graphql"]:
            query = next(a for a in args if a.startswith("query="))
            if "MergeGateThreads" in query:
                pages = [
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [{"isResolved": r} for r in page]
                                    }
                                }
                            }
                        }
                    }
                    for page in self.threads
                ]
                return pages if len(pages) > 1 else pages[0]
            return {  # reviewer_state's own query
                "data": {
                    "repository": {
                        "pullRequest": {
                            "headRefOid": HEAD,
                            "mergeStateStatus": self.pull["mergeStateStatus"],
                            "reviewThreads": {"nodes": []},
                        }
                    }
                }
            }
        path = args[1]
        if "/rules/branches/" in path:
            if self.rules == "404":
                raise subprocess.CalledProcessError(1, ["gh", *args], stderr=NOT_FOUND)
            return self.rules
        if path.endswith("/check-runs"):
            return {"total_count": len(self.check_runs), "check_runs": self.check_runs}
        if path.endswith("/statuses"):
            return self.statuses
        if path.endswith("/reviews"):
            return self.reviews
        if "/stacks?" in path:
            if self.stacks == "404":
                raise subprocess.CalledProcessError(1, ["gh", *args], stderr=NOT_FOUND)
            return list(self.stacks)
        return []


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(**kwargs: Any) -> _FakeGh:
        fake = _FakeGh(**kwargs)
        monkeypatch.setattr(reviewer_state, "_gh_json", fake)
        return fake

    return install


def _config(tmp_path: Path, active: str | None) -> Path:
    config = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    config["active"] = active
    path = tmp_path / "pr-reviewer.json"
    path.write_text(json.dumps(config))
    return path


def _local_reviewer_key() -> str:
    config = json.loads(reviewer_state.DEFAULT_CONFIG_PATH.read_text())
    return next(k for k, v in config["reviewers"].items() if v.get("local_review"))


def test_required_checks_come_from_the_branch_rules(fake_gh: Any) -> None:
    fake = fake_gh()
    assert merge_gate.required_checks(REPO, "main") == ("ci / lint", "ci / tests")
    assert fake.calls == [["api", "repos/owner/repo/rules/branches/main", "--paginate"]]


def test_contexts_from_several_rulesets_are_combined(fake_gh: Any) -> None:
    second = {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": True,
            "required_status_checks": [
                {"context": "ci / e2e"},
                {"context": "ci / lint"},
            ],
        },
    }
    fake_gh(rules=[*_RULES, second])
    assert merge_gate.required_checks(REPO, "main") == (
        "ci / lint",
        "ci / tests",
        "ci / e2e",
    )
    assert merge_gate.strict_up_to_date([*_RULES, second]) is True
    assert merge_gate.strict_up_to_date(_RULES) is False
    assert merge_gate.strict_up_to_date(None) is False


def test_no_required_checks_rule_reads_as_none(fake_gh: Any) -> None:
    fake_gh(rules=[{"type": "deletion"}])
    assert merge_gate.required_checks(REPO, "main") is None


def test_a_rules_404_is_not_an_error(fake_gh: Any) -> None:
    fake_gh(rules="404")
    assert merge_gate.required_checks(REPO, "main") is None


def test_another_gh_failure_on_the_rules_is_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(args: list[str]) -> Any:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="HTTP 500")

    monkeypatch.setattr(reviewer_state, "_gh_json", boom)
    with pytest.raises(subprocess.CalledProcessError):
        merge_gate.required_checks(REPO, "main")


def test_no_check_name_or_ruleset_id_is_hard_coded() -> None:
    """The template's own ruleset contexts and ids never leak into the script."""
    ruleset = json.loads(
        (_REPO_ROOT / ".github" / "repo-settings" / "ruleset.json").read_text()
    )
    source = _SCRIPT.read_text()
    for rule in ruleset["rules"]:
        for check in (rule.get("parameters") or {}).get("required_status_checks", []):
            assert check["context"] not in source
            assert str(check["integration_id"]) not in source


def test_the_shipped_config_skips_the_reviewer_half(
    fake_gh: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.github/pr-reviewer.json`` ships ``"active": null``."""

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the reviewer half must be skipped")

    monkeypatch.setattr(reviewer_state, "collect_state", never)
    fake_gh()
    verdict = merge_gate.collect_and_evaluate(REPO, 1)
    assert verdict["mergeable"] is True
    assert verdict["blockers"] == []
    assert verdict["info"]["reviewer"] is None
    assert verdict["info"]["required_checks"] == ["ci / lint", "ci / tests"]


def test_collect_reads_threads_checks_and_statuses_on_every_page(
    fake_gh: Any,
) -> None:
    fake = fake_gh(
        threads=[[True, False], [False]],
        statuses=[
            {"context": "ci / tests", "state": "failure"},  # newest first
            {"context": "ci / tests", "state": "success"},
        ],
        check_runs=[{"name": "ci / lint", "conclusion": "success"}],
    )
    verdict = merge_gate.collect_and_evaluate(REPO, 1)
    assert verdict["blockers"] == [
        "required check not green: ci / tests = failure",
        THREADS_BLOCKER.format(n=2),
    ]
    lists = [c for c in fake.calls if c[0] == "api" and c[1] != "graphql"]
    assert lists and all("--paginate" in c for c in lists)
    graphql = [c for c in fake.calls if c[:2] == ["api", "graphql"]]
    assert graphql and all("--paginate" in c for c in graphql)


def test_a_pending_check_run_reads_its_status(fake_gh: Any) -> None:
    fake_gh(
        check_runs=[
            {"name": "ci / lint", "conclusion": "success"},
            {"name": "ci / tests", "conclusion": None, "status": "in_progress"},
        ]
    )
    verdict = merge_gate.collect_and_evaluate(REPO, 1)
    assert verdict["blockers"] == ["required check not green: ci / tests = in_progress"]


def test_collect_without_rules_needs_every_reported_check(fake_gh: Any) -> None:
    fake_gh(
        rules="404",
        check_runs=[
            {"name": "ci / lint", "conclusion": "success"},
            {"name": "ci / build", "conclusion": "failure"},
        ],
    )
    verdict = merge_gate.collect_and_evaluate(REPO, 1)
    assert verdict["blockers"] == ["check not green: ci / build = failure"]
    assert verdict["info"]["required_checks"] is None


def test_collect_reads_the_strict_policy_from_the_rules(fake_gh: Any) -> None:
    strict = json.loads(json.dumps(_RULES))
    strict[1]["parameters"]["strict_required_status_checks_policy"] = True
    fake_gh(rules=strict, merge_state="BEHIND")
    assert merge_gate.collect_and_evaluate(REPO, 1)["mergeable"] is False
    fake_gh(merge_state="BEHIND")
    assert merge_gate.collect_and_evaluate(REPO, 1)["mergeable"] is True


def test_collect_with_a_rules_404_lists_threads_as_info(fake_gh: Any) -> None:
    fake_gh(rules="404", threads=[[False, False]])
    verdict = merge_gate.collect_and_evaluate(REPO, 1)
    assert verdict["blockers"] == []
    assert verdict["info"]["unresolved_threads"] == 2
    assert verdict["info"]["threads_must_resolve"] is False


def test_collect_blocks_on_threads_the_rules_require_resolved(fake_gh: Any) -> None:
    fake_gh(threads=[[False]])
    verdict = merge_gate.collect_and_evaluate(REPO, 1)
    assert verdict["blockers"] == [THREADS_BLOCKER.format(n=1)]
    loose = json.loads(json.dumps(_RULES))
    loose[0]["parameters"]["required_review_thread_resolution"] = False
    fake_gh(rules=loose, threads=[[False]])
    assert merge_gate.collect_and_evaluate(REPO, 1)["blockers"] == []


def test_a_non_list_rules_payload_is_an_error(fake_gh: Any) -> None:
    fake_gh(rules={"message": "unexpected"})
    with pytest.raises(ValueError, match="expected a list of rules"):
        merge_gate.branch_rules(REPO, "main")


def test_an_empty_rules_payload_is_no_rules(fake_gh: Any) -> None:
    fake_gh(rules=None)
    assert merge_gate.branch_rules(REPO, "main") == []


def test_no_required_checks_rule_notes_classic_protection_is_not_read() -> None:
    verdict = _gate(required_checks=None)
    assert any(
        "classic branch protection is not" in note for note in verdict["info"]["notes"]
    )


def test_the_merge_state_blocker_lists_the_passing_states_plainly() -> None:
    (blocker,) = _gate(merge_state_status="DIRTY", strict_up_to_date=True)["blockers"]
    assert blocker == (
        "mergeStateStatus is DIRTY (need one of CLEAN, HAS_HOOKS, UNSTABLE)"
    )
    (loose,) = _gate(merge_state_status="DIRTY")["blockers"]
    assert loose.endswith("(need one of BEHIND, CLEAN, HAS_HOOKS, UNSTABLE)")


def test_collect_on_a_merged_pr_reads_nothing_else(fake_gh: Any) -> None:
    fake = fake_gh(state="MERGED", merge_state="UNKNOWN")
    verdict = merge_gate.collect_and_evaluate(REPO, 1)
    assert verdict["already_merged"] is True
    assert verdict["blockers"] == []
    assert len(fake.calls) == 1


def test_collect_and_evaluate_uses_the_configured_reviewer(
    fake_gh: Any, tmp_path: Path
) -> None:
    """The live path loads the adapter from the config and names it in blockers."""
    config_path = _config(tmp_path, _local_reviewer_key())
    adapter = reviewer_state.load_reviewer(config_path)
    fake_gh()
    verdict = merge_gate.collect_and_evaluate(REPO, 1, config_path=config_path)
    assert verdict["mergeable"] is False
    # A reviewer with a local review: the PR's one review is the full review, so the
    # only blocker asks for it, never for an incremental trigger.
    assert verdict["blockers"] == [
        (
            f"{adapter.display_name} has not run its final full review — post "
            f"`{adapter.final_review_comment}` once local reviews are clean"
        ),
    ]
    assert verdict["info"]["reviewer"] == adapter.key


def _clean_state(**extra: Any) -> Any:
    def state(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "verdict": "pass",
            "open_findings": [],
            "unresolved_thread_total": 0,
            "final_review_outcome": "finished",
            "verdict_carried_from": None,
            "unthreaded_findings": [],
            **extra,
        }

    return state


def test_collect_and_evaluate_accepts_a_carried_verdict(
    fake_gh: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rebase-only head's carried verdict satisfies the gate."""
    monkeypatch.setattr(
        reviewer_state,
        "collect_state",
        _clean_state(verdict_carried_from="r" * 40, verdict_carry_reason="same_patch"),
    )
    fake_gh()
    verdict = merge_gate.collect_and_evaluate(
        REPO, 1, config_path=_config(tmp_path, _local_reviewer_key())
    )
    assert verdict["blockers"] == []
    assert verdict["mergeable"] is True
    assert verdict["info"]["verdict_carried_from"] == "r" * 40


def test_collect_lists_unthreaded_findings_as_info(
    fake_gh: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    found = [{"source": "review", "id": 3, "text": "Possible race"}]
    monkeypatch.setattr(
        reviewer_state, "collect_state", _clean_state(unthreaded_findings=found)
    )
    fake_gh()
    verdict = merge_gate.collect_and_evaluate(
        REPO, 1, config_path=_config(tmp_path, _local_reviewer_key())
    )
    assert verdict["mergeable"] is True
    assert verdict["info"]["unthreaded_findings"] == found


def test_collect_and_evaluate_blocks_on_a_standing_request(
    fake_gh: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(reviewer_state, "collect_state", _clean_state())
    seen: list[tuple[str, int]] = []

    def _standing(repo: str, pr: int, bots: frozenset[str]) -> list[str]:
        seen.append((repo, pr))
        assert bots  # every configured reviewer's logins are excluded
        return ["octocat"]

    monkeypatch.setattr(human_review, "standing_requests", _standing)
    fake_gh()
    verdict = merge_gate.collect_and_evaluate(
        REPO, 7, config_path=_config(tmp_path, _local_reviewer_key())
    )
    assert seen == [(REPO, 7)]
    assert verdict["mergeable"] is False
    assert verdict["blockers"][0].startswith("@octocat requested changes")


def test_a_human_request_blocks_with_no_reviewer_configured(fake_gh: Any) -> None:
    fake_gh(
        reviews=[
            {
                "id": 1,
                "user": {"login": "octocat", "type": "User"},
                "state": "CHANGES_REQUESTED",
                "body": "please fix",
                "submitted_at": "2026-10-26T17:00:00Z",
            }
        ]
    )
    verdict = merge_gate.collect_and_evaluate(REPO, 1)
    assert verdict["blockers"][0].startswith("@octocat requested changes")


def test_stack_prs_reads_bottom_to_top(fake_gh: Any) -> None:
    fake = fake_gh(
        stacks=[{"number": 7, "pull_requests": [{"number": n} for n in (11, 12, 13)]}]
    )
    assert merge_gate.stack_prs(REPO, 12) == [11, 12, 13]
    assert fake.calls == [
        ["api", "repos/owner/repo/stacks?pull_request=12", "--paginate"]
    ]


def test_stack_prs_is_none_for_a_pr_outside_any_stack(fake_gh: Any) -> None:
    fake_gh(stacks=[])
    assert merge_gate.stack_prs(REPO, 12) is None


def test_stack_prs_skips_a_stack_without_the_pr(fake_gh: Any) -> None:
    fake_gh(
        stacks=[
            {"number": 1, "pull_requests": [{"number": 3}]},
            {"number": 2, "pull_requests": [{"number": 12}, {"number": 13}]},
        ]
    )
    assert merge_gate.stack_prs(REPO, 13) == [12, 13]


def test_stack_prs_is_none_without_a_stacks_api(fake_gh: Any) -> None:
    fake_gh(stacks="404")
    assert merge_gate.stack_prs(REPO, 12) is None


def test_collect_and_evaluate_stack_gates_every_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(merge_gate, "stack_prs", lambda repo, pr: [11, 12])
    seen: list[int] = []

    def _single(repo: str, pr: int, **kwargs: Any) -> dict[str, Any]:
        seen.append(pr)
        assert kwargs["acceptance_audit_clean"] is True
        configs.append(kwargs["config"])
        blocker = THREADS_BLOCKER.format(n=1)
        return _verdict() if pr == 11 else _verdict(blocker)

    configs: list[Any] = []
    monkeypatch.setattr(merge_gate, "collect_and_evaluate", _single)
    verdict = merge_gate.collect_and_evaluate_stack(
        REPO, 12, acceptance_audit_clean=True
    )
    assert seen == [11, 12]
    assert verdict["blockers"] == [f"#12: {THREADS_BLOCKER.format(n=1)}"]
    assert configs[0] is configs[1]  # loaded once, shared by every layer


def test_a_pr_outside_any_stack_is_a_single_layer_stack(fake_gh: Any) -> None:
    fake_gh(stacks="404")
    verdict = merge_gate.collect_and_evaluate_stack(REPO, 5)
    assert verdict["mergeable"] is True
    assert [layer["pr"] for layer in verdict["layers"]] == [5]
    assert any("not in a stack" in note for note in verdict["info"]["notes"])


# --- command line --------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 -- fixed argv, in-repo script, no shell
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_the_script_is_an_executable_python3_script() -> None:
    assert _SCRIPT.read_text().startswith("#!/usr/bin/env python3\n")
    assert os.access(_SCRIPT, os.X_OK)


def test_help_exits_zero() -> None:
    result = _run_cli("--help")
    assert result.returncode == 0
    assert "--stack" in result.stdout and "--acceptance-audit-clean" in result.stdout
    assert "--has-open-park" not in result.stdout


def test_usage_errors_exit_64(tmp_path: Path) -> None:
    assert _run_cli().returncode == 64
    assert _run_cli("--repo", REPO).returncode == 64
    assert _run_cli("--repo", "no-slash", "--pr", "7").returncode == 64
    assert (
        _run_cli("--repo", REPO, "--pr", "7", "--acceptance-audit-clean", "maybe")
    ).returncode == 64
    missing = _run_cli(
        "--repo", REPO, "--pr", "7", "--config", str(tmp_path / "nope.json")
    )
    assert missing.returncode == 64
    assert "not found" in missing.stderr
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    invalid = _run_cli("--repo", REPO, "--pr", "7", "--config", str(bad))
    assert invalid.returncode == 64
    assert "not valid JSON" in invalid.stderr


def _main(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["merge_gate.py", "--repo", REPO, *args])
    with pytest.raises(SystemExit) as exc:
        merge_gate.main()
    code = exc.value.code
    assert isinstance(code, int)
    return code


def test_main_exits_zero_when_nothing_blocks(
    fake_gh: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_gh()
    assert _main(monkeypatch, "--pr", "1") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mergeable"] is True
    assert "layers" not in report


def test_main_exits_one_when_blocked(
    fake_gh: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_gh()
    assert _main(monkeypatch, "--pr", "1", "--acceptance-audit-clean", "false") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["blockers"] == ["acceptance-criteria audit is not clean"]


def test_main_accepts_a_clean_audit(
    fake_gh: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_gh()
    assert _main(monkeypatch, "--pr", "1", "--acceptance-audit-clean", "true") == 0


def test_main_reports_layers_with_stack(
    fake_gh: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_gh(stacks=[{"number": 1, "pull_requests": [{"number": 3}, {"number": 4}]}])
    assert _main(monkeypatch, "--pr", "4", "--stack") == 0
    report = json.loads(capsys.readouterr().out)
    assert [layer["pr"] for layer in report["layers"]] == [3, 4]


def test_main_exits_zero_on_an_already_merged_pr(
    fake_gh: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_gh(state="MERGED", merge_state="UNKNOWN")
    assert _main(monkeypatch, "--pr", "1") == 0
    assert json.loads(capsys.readouterr().out)["already_merged"] is True


def test_a_gh_failure_is_reported_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(args: list[str]) -> Any:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="HTTP 502")

    monkeypatch.setattr(reviewer_state, "_gh_json", boom)
    assert _main(monkeypatch, "--pr", "1") == 2
    assert "HTTP 502" in capsys.readouterr().err


def test_a_non_list_rules_payload_exits_two(
    fake_gh: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_gh(rules={"message": "unexpected"})
    assert _main(monkeypatch, "--pr", "1") == 2
    assert "expected a list of rules" in capsys.readouterr().err


def test_an_unexpected_payload_shape_exits_two(
    fake_gh: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = fake_gh()
    fake.pull = {}  # pr view answered without the fields asked for
    assert _main(monkeypatch, "--pr", "1") == 2
    captured = capsys.readouterr()
    assert "unexpected GitHub payload" in captured.err
    assert captured.out == ""


def test_main_loads_the_config_once_for_a_stack(
    fake_gh: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    loads: list[Any] = []
    real = merge_gate.load_config

    def counting(path: Any) -> Any:
        loads.append(path)
        return real(path)

    monkeypatch.setattr(merge_gate, "load_config", counting)
    fake_gh(stacks=[{"number": 1, "pull_requests": [{"number": n} for n in (3, 4, 5)]}])
    assert _main(monkeypatch, "--pr", "4", "--stack") == 0
    assert len(loads) == 1
    capsys.readouterr()
