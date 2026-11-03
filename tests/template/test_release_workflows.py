"""Guard the release tooling and the retirement of ``CHANGELOG.md``.

Covers the release-drafter config and its two workflows.

Releases are drafted from merged PRs on every push to ``main`` and published by
hand through ``release.yml``. The semver bump is read from a PR label
(``major`` / ``minor`` / ``patch``); no label means patch.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 -- fixed argv, in-repo git, no shell
from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parents[2]
_GITHUB = _REPO / ".github"
_DRAFTER_ACTION = "release-drafter/release-drafter@v7"
_SELF = "tests/template/test_release_workflows.py"


def _load_workflow(name: str) -> dict[Any, Any]:
    data = yaml.safe_load((_GITHUB / "workflows" / name).read_text())
    assert isinstance(data, dict)
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads a bare `on:` as True
    return data


def _drafter_step(workflow: dict[Any, Any]) -> dict[str, Any]:
    (job,) = workflow["jobs"].values()
    (step,) = [s for s in job["steps"] if s.get("uses") == _DRAFTER_ACTION]
    return step


def test_release_drafter_defaults_to_patch_with_label_overrides() -> None:
    config = yaml.safe_load((_GITHUB / "release-drafter.yml").read_text())
    assert config["name-template"] == "v$RESOLVED_VERSION"
    assert config["tag-template"] == "v$RESOLVED_VERSION"
    assert config["template"] == "$CHANGES"
    resolver = config["version-resolver"]
    assert resolver["major"] == {"labels": ["major"]}
    assert resolver["minor"] == {"labels": ["minor"]}
    assert resolver["patch"] == {"labels": ["patch"]}
    assert resolver["default"] == "patch"


def test_drafter_runs_only_on_push_to_main() -> None:
    workflow = _load_workflow("release-drafter.yml")
    assert workflow["on"] == {"push": {"branches": ["main"]}}
    assert workflow["permissions"] == {"contents": "write", "pull-requests": "read"}
    assert workflow["concurrency"]["group"]
    assert workflow["concurrency"]["cancel-in-progress"] is False
    _drafter_step(workflow)


def test_release_requires_summary_and_publishes() -> None:
    workflow = _load_workflow("release.yml")
    assert set(workflow["on"]) == {"workflow_dispatch"}
    summary = workflow["on"]["workflow_dispatch"]["inputs"]["summary"]
    assert summary["required"] is True
    assert summary["type"] == "string"
    assert "one paragraph" in summary["description"]
    assert workflow["permissions"] == {"contents": "write", "pull-requests": "read"}

    step = _drafter_step(workflow)
    assert step["with"]["publish"] is True
    assert step["with"]["header"] == "${{ inputs.summary }}"
    assert step["id"]

    # The release is reported in the job summary, and the free-text summary input
    # is never interpolated into a shell script (script injection).
    (job,) = workflow["jobs"].values()
    runs = [s for s in job["steps"] if "run" in s]
    assert runs, "expected a run step that writes the job summary"
    report = "\n".join(s["run"] for s in runs)
    env = "\n".join(str(s.get("env", "")) for s in runs)
    assert "$GITHUB_STEP_SUMMARY" in report
    assert f"steps.{step['id']}.outputs.tag_name" in env
    assert f"steps.{step['id']}.outputs.html_url" in env
    assert "${{" not in report


def _tracked_files() -> list[str]:
    out = subprocess.run(  # nosec B603 B607 -- fixed argv, in-repo git, no shell
        ["git", "ls-files", "-z"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def test_no_changelog_remains() -> None:
    """No tracked file keeps CHANGELOG.md alive.

    The file itself must be gone, and no tracked file may mention the filename
    ``CHANGELOG.md`` or list the bare word ``CHANGELOG`` as an entry (a line that
    is just ``CHANGELOG`` or a comma-separated list item such as
    ``README, CHANGELOG, lockfiles``). Exempt: everything under
    ``docs/superpowers/`` (dated historical specs, plans and test kits) and this
    test file. Prose that merely says "changelog" is allowed.
    """
    assert not (_REPO / "CHANGELOG.md").exists()
    bare_entry = re.compile(r"(^|[\s,(])CHANGELOG([\s,).]|$)")
    offenders = []
    for rel in _tracked_files():
        if rel.startswith("docs/superpowers/") or rel == _SELF:
            continue
        path = _REPO / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if "CHANGELOG.md" in text or bare_entry.search(text):
            offenders.append(rel)
    assert not offenders, f"still mention CHANGELOG: {offenders}"
