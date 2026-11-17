"""Guard the Dependabot policy: security updates always, version updates for majors only.

`allow` with `update-types` limits *version* updates; GitHub documents that
security updates are created regardless. The one exception is uv-build minors
in the python stack: uv ships breaking changes in 0.x minor releases.

uv rules are limited to direct dependencies: a major of an indirect dependency
is its parent package's call, and arrives with a deliberate `uv lock --upgrade`.
dependabot-core skips the dependency-type filter for security updates, so a
vulnerable indirect dependency still gets a security PR.
"""

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"

MAJOR_RULE: dict[str, Any] = {
    "dependency-name": "*",
    "update-types": ["version-update:semver-major"],
}
UV_MAJOR_RULE: dict[str, Any] = {**MAJOR_RULE, "dependency-type": "direct"}
UV_BUILD_MINOR_RULE: dict[str, Any] = {
    "dependency-name": "uv-build",
    "dependency-type": "direct",
    "update-types": ["version-update:semver-minor"],
}


def load_updates(path: Path) -> list[dict[str, Any]]:
    """Return the `updates` entries of a dependabot.yml."""
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    updates: list[dict[str, Any]] = config["updates"]
    return updates


def _label(entry: dict[str, Any]) -> str:
    return f"{entry['package-ecosystem']} {entry['directory']}"


# The one entry that takes every release: the template's pinned skills CLI. Each
# bump regenerates golden.json by hand, so one PR at a time.
SKILLS_CLI = "npm /.github/actions/skills-cli"


def test_every_entry_allows_only_majors() -> None:
    for entry in load_updates(DEPENDABOT):
        if _label(entry) == SKILLS_CLI:
            continue
        allow = entry.get("allow", [])
        major = UV_MAJOR_RULE if entry["package-ecosystem"] == "uv" else MAJOR_RULE
        assert major in allow, f"{_label(entry)}: missing the semver-major rule"
        extra = [rule for rule in allow if rule != major]
        assert all(
            rule == UV_BUILD_MINOR_RULE for rule in extra
        ), f"{_label(entry)}: unexpected allow rules {extra}"


def test_skills_cli_takes_every_release_one_pr_at_a_time() -> None:
    (entry,) = [e for e in load_updates(DEPENDABOT) if _label(e) == SKILLS_CLI]
    assert "allow" not in entry, "skills CLI: every release, so no allow rule"
    assert entry.get("open-pull-requests-limit") == 1


def test_uv_build_minor_exception_only_on_python_stack() -> None:
    with_exception = [
        _label(entry)
        for entry in load_updates(DEPENDABOT)
        if UV_BUILD_MINOR_RULE in entry.get("allow", [])
    ]
    assert with_exception == ["uv /stacks/python"]


def test_no_minor_patch_groups() -> None:
    for entry in load_updates(DEPENDABOT):
        groups = entry.get("groups", {})
        for name, group in groups.items():
            assert (
                "update-types" not in group
            ), f"{_label(entry)}: group {name} filters update-types"
        if entry["package-ecosystem"] == "bun":
            # peer-coupled packages keep moving as a set
            assert set(groups) == {"vite", "vitest"}
        else:
            assert not groups, f"{_label(entry)}: unexpected groups {list(groups)}"


def test_stack_markers_intact() -> None:
    text = DEPENDABOT.read_text(encoding="utf-8")
    for stack in ("python", "react", "template"):
        assert f"# >>> STACK:{stack}" in text
        assert f"# <<< STACK:{stack}" in text
