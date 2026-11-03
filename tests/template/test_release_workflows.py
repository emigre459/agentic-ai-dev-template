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

import pytest
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


def _drafter_step(
    workflow: dict[Any, Any], step_id: str | None = None
) -> dict[str, Any]:
    """Return the one release-drafter step, or the one with ``step_id``."""
    (job,) = workflow["jobs"].values()
    (step,) = [
        s
        for s in job["steps"]
        if s.get("uses") == _DRAFTER_ACTION and (step_id is None or s["id"] == step_id)
    ]
    return step


def test_release_drafter_defaults_to_patch_with_label_overrides() -> None:
    config = yaml.safe_load((_GITHUB / "release-drafter.yml").read_text())
    assert config["name-template"] == "v$RESOLVED_VERSION"
    assert config["tag-template"] == "v$RESOLVED_VERSION"
    # The publish workflow's `header` is glued to the template with no separator,
    # so the template itself must open with a blank line.
    assert config["template"].startswith("\n\n")
    assert "$CHANGES" in config["template"]
    resolver = config["version-resolver"]
    assert resolver["major"] == {"labels": ["major"]}
    assert resolver["minor"] == {"labels": ["minor"]}
    assert resolver["patch"] == {"labels": ["patch"]}
    assert resolver["default"] == "patch"


def test_summary_header_leaves_first_change_on_its_own_line() -> None:
    """release-drafter renders header + template + footer with no separator."""
    config = yaml.safe_load((_GITHUB / "release-drafter.yml").read_text())
    summary = "A one-paragraph summary."  # no trailing newline, like the input
    body = summary + config["template"].replace("$CHANGES", "* Fix foo (#12)")
    lines = body.split("\n")
    assert lines[0] == summary
    assert lines[1] == ""
    assert lines[2] == "* Fix foo (#12)"


def test_drafter_runs_on_push_to_main_and_by_hand() -> None:
    workflow = _load_workflow("release-drafter.yml")
    # workflow_dispatch lets the release skill re-draft after relabeling a merged
    # PR (a label change is not a push, so nothing else would re-run it).
    assert workflow["on"] == {"push": {"branches": ["main"]}, "workflow_dispatch": None}
    # A dispatch from another branch would draft from that branch: main only.
    (job,) = workflow["jobs"].values()
    assert job["if"] == "github.ref == 'refs/heads/main'"
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

    # Publishing tags the release target: only ever from main, never from a
    # branch run that would pin consumers to unmerged code.
    (job,) = workflow["jobs"].values()
    assert job["if"] == "github.ref == 'refs/heads/main'"

    step = _drafter_step(workflow, "drafter")
    assert step["with"]["commitish"] == "main"
    assert step["with"]["publish"] is True
    assert step["with"]["header"] == "${{ inputs.summary }}"
    assert step["id"]

    # The release is reported in the job summary, and the free-text summary input
    # is never interpolated into a shell script (script injection).
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


# --- release.yml publishes only the version the user approved -------------------


def _release_steps() -> list[dict[str, Any]]:
    (job,) = _load_workflow("release.yml")["jobs"].values()
    steps: list[dict[str, Any]] = job["steps"]
    return steps


def _version_check() -> dict[str, Any]:
    (step,) = [s for s in _release_steps() if "inputs.version" in str(s.get("env"))]
    return step


def test_release_requires_the_approved_version() -> None:
    inputs = _load_workflow("release.yml")["on"]["workflow_dispatch"]["inputs"]
    version = inputs["version"]
    assert version["required"] is True
    assert version["type"] == "string"
    assert "v0.2.0" in version["description"]


def test_release_checks_the_version_before_publishing() -> None:
    steps = _release_steps()
    workflow = _load_workflow("release.yml")
    preview = _drafter_step(workflow, "preview")
    publish = _drafter_step(workflow, "drafter")
    check = _version_check()
    # A dry run resolves the version, the check compares it, then the publish.
    assert steps.index(preview) < steps.index(check) < steps.index(publish)
    assert preview["with"]["dry-run"] is True
    assert "publish" not in preview["with"]
    assert preview["with"]["commitish"] == publish["with"]["commitish"] == "main"
    assert check["env"]["WOULD_PUBLISH"] == "${{ steps.preview.outputs.tag_name }}"


def test_the_version_input_reaches_run_steps_only_through_env() -> None:
    check = _version_check()
    assert check["env"]["APPROVED_VERSION"] == "${{ inputs.version }}"
    for step in _release_steps():
        assert "inputs.version" not in step.get("run", "")
        assert "${{" not in step.get("run", "")
        if "uses" in step:
            assert "inputs.version" not in str(step.get("with", {}))


def _run_check(
    tmp_path: Path, approved: str, would: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 B607 -- the workflow's own step script
        ["bash", "-euo", "pipefail", "-c", _version_check()["run"]],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "APPROVED_VERSION": approved,
            "WOULD_PUBLISH": would,
        },
    )


def test_version_check_passes_on_a_match(tmp_path: Path) -> None:
    result = _run_check(tmp_path, "v0.2.0", "v0.2.0")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("approved", "would"),
    [("v0.2.0", "v0.3.0"), ("0.2.0", "v0.2.0"), ("v0.2.0", "")],
    ids=["other-version", "missing-v", "nothing-resolved"],
)
def test_version_check_fails_without_publishing_on_a_mismatch(
    tmp_path: Path, approved: str, would: str
) -> None:
    result = _run_check(tmp_path, approved, would)
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert approved in result.stdout


def _skill_text(name: str) -> str:
    return (_REPO / ".agents" / "skills" / name / "SKILL.md").read_text()


def test_build_from_issue_mentions_release_check() -> None:
    text = _skill_text("build-from-issue")
    assert "gh release list" in text
    assert "--add-label" in text
    for bump in ("patch", "minor", "major"):
        assert bump in text
    # With no published release the first one is suggested; the user can correct it.
    assert "v0.1.0" in text


def test_release_skill_gates_publish_on_user_ok() -> None:
    text = _skill_text("release")
    assert "AskUserQuestion" in text
    assert "release.yml" in text
    assert "make skills_lock TAG=" in text
    assert "at most one paragraph" in text
    assert "release-pin-v" in text
    # The skill never merges and never publishes without the user's OK.
    assert "never publish without the user's explicit OK for that release" in text
    assert "never merges anything" in text
    # The summary travels as JSON on stdin through a single-quoted heredoc, never
    # inside double quotes where the shell would expand it.
    assert "--json <<'JSON'" in text
    assert 'summary="' not in text
    # The new run is found by creation time; Step 6 verifies the published tag.
    assert "--event workflow_dispatch --json databaseId,createdAt" in text
    assert "gh release list --exclude-drafts --limit 1 --json tagName" in text
    assert "gh run watch" in text
    assert "gh release view" in text


def _readme() -> str:
    return (_REPO / "README.md").read_text()


def test_readme_covers_install_pin_update_telemetry_release() -> None:
    readme = _readme()
    for needle in (
        "DISABLE_TELEMETRY=1",
        "#v",
        "SKILLS_UPDATE_TOKEN",
        "SKILLS_UPDATE_APP_ID",
        "skills drift",
        "release.yml",
        "skills-update",
        "release-pin-v",
        "60 days",
        "__pycache__",
    ):
        assert needle in readme, f"README does not mention {needle!r}"
    for heading in (
        "## Installing the skills",
        "## Staying up to date",
        "## The drift check",
        "## Cutting a release",
    ):
        assert heading in readme, f"README lacks {heading!r}"


def test_readme_release_sections_follow_customizing_and_keep_it() -> None:
    readme = _readme()
    customizing = readme.index("## Customizing the skills in your repo")
    assert readme.index(
        "### `.agents/skill-config/<skill>.md`: notes for one skill"
    ) > (customizing)
    order = [
        readme.index(h)
        for h in (
            "## Installing the skills",
            "## Staying up to date",
            "## The drift check",
            "## Cutting a release",
            "## Prerequisites",
        )
    ]
    assert customizing < order[0]
    assert order == sorted(order)


def test_readme_new_code_blocks_are_plain_ascii() -> None:
    readme = _readme()
    new = readme[
        readme.index("## Installing the skills") : readme.index("## Prerequisites")
    ]
    blocks = re.findall(r"```[^\n]*\n(.*?)```", new, flags=re.DOTALL)
    assert blocks, "expected code blocks in the new sections"
    for block in blocks:
        assert block.isascii(), f"non-ASCII code block: {block[:60]!r}"


def test_agents_md_mentions_release_skill_and_labels() -> None:
    text = (_REPO / "AGENTS.md").read_text()
    skills = text.split("## Skills")[1].split("## Hooks")[0]
    assert "`release` skill" in skills
    assert "`minor`" in skills
    assert "`major`" in skills


def test_build_from_issue_has_no_no_release_bump_option() -> None:
    text = _skill_text("build-from-issue")
    assert "no release" not in text
    assert "still counts as a patch" in text


def test_readme_states_the_adoption_and_schedule_caveats() -> None:
    readme = " ".join(_readme().split())
    assert "On public repositories, GitHub disables scheduled workflows" in readme
    assert "not necessarily the newest release" in readme
    assert "`workflow_dispatch:` trigger" in readme
    assert "`ci-workflow:`" in readme
    assert "`merge_group`" in readme
