"""Every required check must also run on merge-queue commits.

A merge queue tests each PR on a temporary ``gh-readonly-queue/main/*`` commit and
merges only when every required status reports there. A required check whose
workflow doesn't trigger on ``merge_group`` never reports, so the queue waits until
its timeout and drops the PR. Guarded for the template as shipped and for each stack
after ``make init`` rewrites the required contexts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.init_template import initialize

_REPO = Path(__file__).resolve().parents[2]


def _workflows(root: Path) -> list[dict[Any, Any]]:
    flows = []
    for path in sorted((root / ".github" / "workflows").glob("*.yml")):
        data = yaml.safe_load(path.read_text())
        assert isinstance(data, dict), path
        data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads `on:` as True
        flows.append(data)
    return flows


def _triggers(flow: dict[Any, Any]) -> set[str]:
    on = flow["on"]
    if isinstance(on, str):
        return {on}
    return set(on)


def merge_group_contexts(root: Path) -> set[str]:
    """Check contexts reported on merge-group commits: each job's name, else its id."""
    contexts: set[str] = set()
    for flow in _workflows(root):
        if "merge_group" not in _triggers(flow):
            continue
        for job_id, job in flow.get("jobs", {}).items():
            contexts.add(job.get("name") or job_id)
    return contexts


def required_contexts(root: Path) -> set[str]:
    ruleset = json.loads(
        (root / ".github" / "repo-settings" / "ruleset.json").read_text()
    )
    return {
        check["context"]
        for rule in ruleset["rules"]
        if rule["type"] == "required_status_checks"
        for check in rule["parameters"]["required_status_checks"]
    }


def _has_merge_queue(root: Path) -> bool:
    ruleset = json.loads(
        (root / ".github" / "repo-settings" / "ruleset.json").read_text()
    )
    return any(rule["type"] == "merge_queue" for rule in ruleset["rules"])


def test_every_required_check_runs_on_merge_group() -> None:
    required = required_contexts(_REPO)
    assert required, "the ruleset requires no checks; the queue would merge untested"
    missing = required - merge_group_contexts(_REPO)
    assert not missing, f"required but never reported on merge_group: {missing}"


def test_the_guard_catches_a_check_that_skips_merge_group(tmp_path: Path) -> None:
    """Mutation check: drop ci.yml's merge_group trigger and the guard must fail."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    ci = (_REPO / ".github" / "workflows" / "ci.yml").read_text()
    stripped = ci.replace("  merge_group:\n    types: [checks_requested]\n", "")
    assert stripped != ci, "ci.yml's merge_group trigger moved; update this test"
    (workflows / "ci.yml").write_text(stripped)
    assert required_contexts(_REPO) - merge_group_contexts(tmp_path)


@pytest.mark.parametrize("stack", ["python", "react"])
def test_after_init_the_queue_and_its_checks_still_line_up(
    template_tree: Path, stack: str
) -> None:
    initialize(template_tree, stack, "acme-app", "app", apply_settings=False)
    assert _has_merge_queue(template_tree)
    required = required_contexts(template_tree)
    assert required == {"lint", "tests", "security"}
    assert not required - merge_group_contexts(template_tree)
