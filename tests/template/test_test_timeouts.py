"""Each stack fails a hung test with a fixed per-test timeout (#83).

The per-test speed guard (a budget re-measured per run) is retired; a generous,
fixed timeout in each stack's test config is the only time-based block, so every
way of running the tests (Make, an IDE, a bare ``pytest`` / ``vitest``) gets it.
"""

import re
import tomllib
from pathlib import Path

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
    assert re.search(r"^\s*testTimeout:\s*10_000,", text, re.M), text
