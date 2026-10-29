"""The per-test speed guard is wired into both stacks, the root, CI and ``make init``.

``scripts/check_test_speed.py`` is tested on its own in ``test_check_test_speed.py``;
this module checks the wiring around it: each stack's coverage run writes the JUnit
file the guard reads, ``pr_check`` runs coverage THEN the guard (never as sibling
prerequisites, which ``make -j`` may reorder), CI runs it after coverage on pull
requests and merge-queue runs only, the budget lives in one place, and ``make init``
keeps a working guard for either stack.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess  # nosec B404 -- runs our own scripts and git with fixed argvs
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from scripts.init_template import initialize

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "scripts" / "check_test_speed.py"
CALIBRATOR = REPO_ROOT / "scripts" / "runner_speed.py"
CONFIG = ".github/pr-reviewer.json"

#: Each stack's slow-test allowlist, relative to the stack directory (which
#: ``make init`` promotes to the repo root).
ALLOWLISTS = {
    "python": "tests/slow_test_allowlist.txt",
    "react": "slow_test_allowlist.txt",
}

#: The file the coverage run writes and the guard reads, in the stack directory.
JUNIT = "test-results.xml"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("speed_guard_wiring", CHECKER)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {CHECKER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rule(makefile: Path, target: str) -> tuple[list[str], list[str]]:
    """Return a Makefile target's prerequisites and recipe lines.

    Only the simple ``target: prereqs ## help`` form this repo uses is parsed.
    """
    lines = makefile.read_text().splitlines()
    header = re.compile(rf"^{re.escape(target)}:(?!=)([^#]*)(?:##.*)?$")
    for index, line in enumerate(lines):
        match = header.match(line)
        if match is None:
            continue
        recipe = []
        for follower in lines[index + 1 :]:
            if not follower.startswith("\t"):
                break
            recipe.append(follower.strip())
        return match.group(1).split(), recipe
    raise AssertionError(f"{makefile} has no {target!r} target")


def _runs_coverage_then_speed_check(recipe: list[str]) -> bool:
    """Return whether ONE recipe line runs coverage, then (if it passed) the guard."""
    chained = re.compile(
        r"\$\(MAKE\)[^&;]*\bcoverage\b[^&;]*&&\s*\$\(MAKE\)[^&;]*\btest_speed_check\b"
    )
    return any(chained.search(line) for line in recipe)


@pytest.mark.parametrize("stack", ["python", "react"])
def test_each_stack_pr_check_runs_lint_security_then_coverage_then_speed_check(
    stack: str,
) -> None:
    prereqs, recipe = _rule(REPO_ROOT / "stacks" / stack / "Makefile", "pr_check")
    assert prereqs == ["lint", "security"]
    assert _runs_coverage_then_speed_check(recipe), recipe


@pytest.mark.parametrize("stack", ["python", "react"])
def test_each_stack_speed_check_reads_coverage_junit_and_times_solo_reruns(
    stack: str,
) -> None:
    makefile = REPO_ROOT / "stacks" / stack / "Makefile"
    _, coverage = _rule(makefile, "coverage")
    assert any(JUNIT in line for line in coverage), coverage

    _, recipe = _rule(makefile, "test_speed_check")
    command = " ".join(recipe)
    assert "python3 $(REPO_ROOT)/scripts/check_test_speed.py" in command
    assert f"--junit {JUNIT}" in command
    assert f"--allowlist {ALLOWLISTS[stack]}" in command
    # The stack Makefile runs in its own directory, so the checker's default
    # (cwd-relative) config path would miss the repo-root settings file.
    assert f"--config $(REPO_ROOT)/{CONFIG}" in command
    rerun = re.search(r"--rerun '([^']*)'", command)
    assert rerun is not None, command
    # {junit}: the solo re-run is timed from its own JUnit entry, not wall-clock.
    assert "{test}" in rerun.group(1) and "{junit}" in rerun.group(1)


@pytest.mark.parametrize("stack", ["python", "react"])
def test_each_stack_allowlist_starts_empty_with_a_header(stack: str) -> None:
    allowlist = REPO_ROOT / "stacks" / stack / ALLOWLISTS[stack]
    text = allowlist.read_text()
    assert text.startswith("#")
    assert _load_checker().parse_allowlist(allowlist) == set()


def test_root_pr_check_includes_machinery_tests_and_the_speed_check() -> None:
    makefile = REPO_ROOT / "Makefile"
    prereqs, recipe = _rule(makefile, "pr_check")
    assert prereqs == ["lint", "machinery_tests", "security"]
    assert _runs_coverage_then_speed_check(recipe), recipe

    _, delegated = _rule(makefile, "test_speed_check")
    assert any(
        "-C stacks/python test_speed_check" in line for line in delegated
    ), delegated
    assert any(
        "-C stacks/react test_speed_check" in line for line in delegated
    ), delegated


def _steps(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = workflow["jobs"][job]["steps"]
    return steps


@pytest.mark.parametrize("job", ["python-tests", "react-tests"])
def test_ci_runs_the_speed_check_after_coverage_except_on_push(job: str) -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    runs = [step.get("run") for step in _steps(workflow, job)]
    assert "make coverage" in runs and "make test_speed_check" in runs
    assert runs.index("make test_speed_check") == runs.index("make coverage") + 1
    guard = _steps(workflow, job)[runs.index("make test_speed_check")]
    assert guard.get("if") == "github.event_name != 'push'"


def test_react_ci_times_the_guard_on_the_python_stacks_interpreter() -> None:
    """The calibration benchmark is Python, so its speed depends on the interpreter.

    Both stacks' guards must calibrate on the same Python version, or one
    ``ubuntu-latest`` reference would scale their budgets differently.
    """
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    steps = _steps(workflow, "react-tests")
    runs = [step.get("run") for step in steps]
    setups = [
        i
        for i, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    ]
    assert setups and setups[0] < runs.index("make test_speed_check")
    pinned = str(steps[setups[0]]["with"]["python-version"])
    stack_python = (REPO_ROOT / "stacks/python/.python-version").read_text().strip()
    assert stack_python.startswith(pinned), (pinned, stack_python)


def test_ci_keeps_the_speed_check_inside_each_stack_marker_block() -> None:
    """``make init`` strips the other stack's block, so each step must sit in its own."""
    text = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    for stack in ("python", "react"):
        block = re.search(
            rf"^# >>> STACK:{stack}\n(.*?)^# <<< STACK:{stack}\n",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
        assert block is not None, f"STACK:{stack} markers missing"
        assert block.group(1).count("make test_speed_check") == 1


def _tracked_files() -> list[str]:
    listed = subprocess.run(  # nosec B603 B607 -- fixed git argv
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return listed.stdout.splitlines()


def test_the_budget_is_configured_in_exactly_one_place() -> None:
    # A budget key assigned a number. docs/ quotes the design and tests/template
    # builds its own settings fixtures; neither configures the guard.
    assigned = re.compile(r"budget_seconds[\"']?\s*[:=]\s*\d")
    configured = set()
    for rel in _tracked_files():
        if rel.startswith(("docs/", "tests/template/")):
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if assigned.search(text):
            configured.add(rel)
    assert configured == {CONFIG}

    # Nothing in the wiring overrides it: no pinned factor, no budget of its own.
    override = re.compile(r"--factor\b|budget\w*\s*[:=]\s*\d", re.IGNORECASE)
    wiring = [
        "Makefile",
        "stacks/python/Makefile",
        "stacks/react/Makefile",
        ".github/workflows/ci.yml",
        "stacks/python/pyproject.toml",
        "stacks/react/vitest.config.ts",
        "stacks/react/package.json",
    ]
    for rel in wiring:
        assert not override.search((REPO_ROOT / rel).read_text()), rel


_MINIMAL_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="1">
<testcase classname="tests.test_greeting" name="test_greet" time="0.010"/>
</testsuite></testsuites>
"""


@pytest.mark.parametrize("stack", ["python", "react"])
def test_init_keeps_the_speed_guard_for_either_stack(
    template_tree: Path, stack: str
) -> None:
    initialize(template_tree, stack, "acme-app", "An app", apply_settings=False)

    for rel in ("scripts/check_test_speed.py", "scripts/runner_speed.py", CONFIG):
        assert (template_tree / rel).is_file(), rel
    assert (template_tree / ALLOWLISTS[stack]).is_file()
    _, recipe = _rule(template_tree / "Makefile", "test_speed_check")
    assert any("check_test_speed.py" in line for line in recipe)
    ci = yaml.safe_load((template_tree / ".github/workflows/ci.yml").read_text())
    assert "make test_speed_check" in [step.get("run") for step in _steps(ci, "tests")]

    # The kept guard runs in the initialized tree (react drops scripts/__init__.py).
    junit = template_tree / JUNIT
    junit.write_text(_MINIMAL_JUNIT)
    done = subprocess.run(  # nosec B603 -- our own script, fixed argv
        [
            sys.executable,
            "-I",
            str(template_tree / "scripts" / "check_test_speed.py"),
            "--junit",
            str(junit),
            "--allowlist",
            str(template_tree / ALLOWLISTS[stack]),
            "--config",
            str(template_tree / CONFIG),
            "--rerun",
            "true {test} {junit}",
            "--factor",
            "1",
        ],
        cwd=template_tree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr


def test_the_checker_runs_without_the_package_marker(tmp_path: Path) -> None:
    """A react-initialized repo has no ``scripts/__init__.py``; the guard must still run."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(CHECKER, scripts / CHECKER.name)
    shutil.copy2(CALIBRATOR, scripts / CALIBRATOR.name)
    assert not (scripts / "__init__.py").exists()

    done = subprocess.run(  # nosec B603 -- our own script, fixed argv
        [sys.executable, "-I", str(scripts / CHECKER.name), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "--rerun" in done.stdout
