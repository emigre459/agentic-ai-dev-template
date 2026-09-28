"""Guard the ``review-gate`` workflow and the merge-queue CI trigger.

``review-gate`` is a required status, so its workflow must fire on every event that
can change the reviewer's state, must never run PR code (it holds
``statuses: write``), and no job may share its context name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_WF = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _load(name: str) -> dict[Any, Any]:
    data = yaml.safe_load((_WF / name).read_text())
    assert isinstance(data, dict)
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads a bare `on:` as True
    return data


def _gate_job() -> dict[str, Any]:
    (job,) = _load("review-gate.yml")["jobs"].values()
    assert isinstance(job, dict)
    return job


def test_ci_runs_on_merge_group_commits() -> None:
    assert _load("ci.yml")["on"]["merge_group"] == {"types": ["checks_requested"]}


def test_review_gate_triggers_and_permissions() -> None:
    wf = _load("review-gate.yml")
    assert set(wf["on"]) == {
        "pull_request_target",
        "issue_comment",
        "pull_request_review",
        "merge_group",
        "workflow_dispatch",
    }
    assert wf["on"]["issue_comment"] == {"types": ["created", "edited"]}
    assert wf["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
        "issues": "read",
        "statuses": "write",
    }


def test_review_gate_never_runs_pr_code() -> None:
    checkout = next(
        step
        for step in _gate_job()["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["with"]["persist-credentials"] is False


def test_no_job_shares_the_status_context() -> None:
    """No job may be named after the status context.

    A job named `review-gate` posts a check run with that name that passes whenever
    the job succeeds, which would satisfy the required context whatever the status says.
    """
    for name in ("review-gate.yml", "ci.yml"):
        for key, job in _load(name)["jobs"].items():
            assert "review-gate" not in (key, job.get("name")), (name, key)


def test_issue_comments_on_plain_issues_are_skipped() -> None:
    assert "github.event.issue.pull_request" in _gate_job()["if"]


def test_run_steps_take_event_data_only_through_env() -> None:
    for step in _gate_job()["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")


def test_the_wait_script_ignores_this_jobs_cancelled_runs_by_name() -> None:
    """wait_for_pr_checks.sh names the job to skip its superseded (cancelled) runs."""
    wait_script = (
        _WF.parents[1]
        / ".agents"
        / "skills"
        / "resolve-pr-concerns"
        / "scripts"
        / "wait_for_pr_checks.sh"
    ).read_text()
    assert f'REVIEW_GATE_JOB="{_gate_job()["name"]}"' in wait_script


def test_a_default_branch_without_the_script_skips_instead_of_failing() -> None:
    """Before the gate's own PR merges, `main` has no review_gate.py.

    The run must skip with a notice and post nothing: a missing required status still
    blocks the merge, so skipping is safe, while a red run is noise on every PR event.
    """
    (step,) = [s for s in _gate_job()["steps"] if "run" in s]
    assert '[ ! -f "$script" ]' in step["run"]
    assert "exit 0" in step["run"]
