"""Each stack fails a hung test with a fixed per-test timeout (#83).

The per-test speed guard (a budget re-measured per run) is retired; a generous,
fixed timeout in each stack's test config is the only time-based block, so every
way of running the tests (Make, an IDE, a bare ``pytest`` / ``vitest``) gets it.
"""

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PY_PROJECT = REPO_ROOT / "stacks" / "python" / "pyproject.toml"
VITEST_CONFIG = REPO_ROOT / "stacks" / "react" / "vitest.config.ts"


def _py_project() -> dict:
    return tomllib.loads(PY_PROJECT.read_text())


def test_python_stack_sets_a_60s_per_test_timeout() -> None:
    ini = _py_project()["tool"]["pytest"]["ini_options"]
    assert ini["timeout"] == 60
    # The plugin's default method (signal on POSIX) fails just the hung test.
    assert "timeout_method" not in ini


def test_python_stack_has_pytest_timeout_as_a_dev_dependency() -> None:
    dev = _py_project()["dependency-groups"]["dev"]
    assert any(re.match(r"pytest-timeout\b", dep) for dep in dev), dev


def test_react_stack_sets_a_10s_test_timeout() -> None:
    text = VITEST_CONFIG.read_text()
    assert re.search(r"^\s*testTimeout:\s*10_000,", text, re.MULTILINE), text


# --- The retired speed guard leaves no trace in the build (Make, CI, config) ---

_GUARD_TERMS = re.compile(
    r"test_speed|check_test_speed|runner_speed|slow_test_allowlist"
    r"|_pytest_fixture_timing|speed (?:check|guard|budget)",
    re.IGNORECASE,
)
_GUARD_FILES = [
    "scripts/check_test_speed.py",
    "scripts/runner_speed.py",
    "stacks/python/tests/_pytest_fixture_timing.py",
    "stacks/python/tests/slow_test_allowlist.txt",
    "stacks/react/slow_test_allowlist.txt",
]


def test_the_guard_files_are_gone() -> None:
    assert [p for p in _GUARD_FILES if (REPO_ROOT / p).exists()] == []


@pytest.mark.parametrize(
    "rel",
    [
        "Makefile",
        "stacks/python/Makefile",
        "stacks/react/Makefile",
        ".github/workflows/ci.yml",
        ".github/pr-reviewer.json",
        ".gitignore",
        "stacks/python/tests/conftest.py",
    ],
)
def test_no_build_file_names_the_speed_guard(rel: str) -> None:
    match = _GUARD_TERMS.search((REPO_ROOT / rel).read_text())
    assert match is None, f"{rel}: {match.group(0) if match else ''}"


@pytest.mark.parametrize("stack", ["python", "react"])
def test_each_stack_pr_check_runs_lint_security_then_coverage(stack: str) -> None:
    text = (REPO_ROOT / "stacks" / stack / "Makefile").read_text()
    assert re.search(r"^pr_check: lint security\b", text, re.MULTILINE)
    assert "$(MAKE) --no-print-directory coverage" in text


def test_react_tests_job_sets_up_no_python() -> None:
    """Python was there only for the guard's calibration benchmark."""
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    job = ci.split("  react-tests:")[1].split("  react-security:")[0]
    assert "setup-python" not in job


# --- ...and no trace in the prose agents follow (skills, rules, README) ---


def _prose_files() -> list[Path]:
    return [*sorted((REPO_ROOT / ".agents").rglob("*.md")), REPO_ROOT / "README.md"]


def test_no_skill_rule_or_readme_names_the_speed_guard() -> None:
    hits = [
        f"{p.relative_to(REPO_ROOT)}: {m.group(0)}"
        for p in _prose_files()
        if (m := _GUARD_TERMS.search(p.read_text()))
    ]
    assert hits == []


def test_resolve_pr_concerns_coaches_the_per_test_timeout() -> None:
    text = (REPO_ROOT / ".agents/skills/resolve-pr-concerns/SKILL.md").read_text()
    section = text.split("### 2g. ")[1].split("\n### ")[0]
    assert section.lower().startswith("per-test timeout"), section[:80]
    for phrase in ("pytest-timeout", "testTimeout", "hung"):
        assert phrase in section, phrase
