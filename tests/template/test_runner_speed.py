"""Tests for ``scripts/runner_speed.py``, the speed guard's runner calibrator.

The script is loaded by path (it is not imported as ``scripts.runner_speed``)
because a repo initialized for one stack keeps ``scripts/`` without a package
``__init__.py``; the by-path tests prove it runs under plain ``python3``.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess  # nosec B404 -- runs our own script with a fixed argv
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "runner_speed.py"
_CHECKER = _REPO / "scripts" / "check_test_speed.py"
_CONFIG = _REPO / ".github" / "pr-reviewer.json"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runner_speed_under_test", _SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rs = _load()


def _settings(**overrides: Any) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "budget_seconds": 10,
        "recheck_fraction": 0.6,
        "factor_low": 0.5,
        "factor_high": 2.0,
        "shared_setup_budget_seconds": 30,
        "default_runner_class": "runner-a",
        "reference_seconds": {"runner-a": 0.5, "runner-b": 1.0},
    }
    settings.update(overrides)
    return settings


def _write_config(tmp_path: Path, section: Any) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"active": None, "test_speed": section}))
    return path


# --- factor -----------------------------------------------------------------


def test_factor_is_measured_over_reference() -> None:
    assert rs.factor(1.2, 1.0, low=0.5, high=2.0) == pytest.approx(1.2)


@pytest.mark.parametrize(
    ("measured", "expected"), [(0.1, 0.5), (0.5, 0.5), (2.0, 2.0), (9.0, 2.0)]
)
def test_factor_clamps_to_the_range(measured: float, expected: float) -> None:
    assert rs.factor(measured, 1.0, low=0.5, high=2.0) == expected


def test_factor_rejects_an_inverted_clamp() -> None:
    with pytest.raises(ValueError, match="low"):
        rs.factor(1.0, 1.0, low=2.0, high=0.5)


@pytest.mark.parametrize("reference", [0.0, -1.0])
def test_factor_rejects_a_non_positive_reference(reference: float) -> None:
    with pytest.raises(ValueError, match="reference_s"):
        rs.factor(1.0, reference, low=0.5, high=2.0)


# --- calibrate --------------------------------------------------------------


def test_calibrate_rejects_zero_repeats() -> None:
    with pytest.raises(ValueError, match="repeats"):
        rs.calibrate(0)


def test_calibrate_returns_the_fastest_run(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 3.0, 10.0, 11.0, 20.0, 22.0])
    monkeypatch.setattr(rs, "_workload", lambda: 0)
    monkeypatch.setattr(rs.time, "perf_counter", lambda: next(ticks))
    assert rs.calibrate(3) == 1.0


def test_the_workload_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs, "_WORKLOAD_ROUNDS", 50)
    assert rs._workload() == rs._workload() != 0


# --- runner class and references --------------------------------------------


def test_runner_class_defaults_to_the_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(rs.RUNNER_CLASS_ENV, raising=False)
    assert rs.runner_class(_settings()) == "runner-a"


def test_runner_class_env_overrides_the_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(rs.RUNNER_CLASS_ENV, "runner-b")
    assert rs.runner_class(_settings()) == "runner-b"


def test_reference_for_a_known_class() -> None:
    assert rs.reference_for("runner-b", _settings()) == 1.0


def test_unknown_runner_class_says_how_to_add_it() -> None:
    with pytest.raises(SystemExit) as exc:
        rs.reference_for("runner-z", _settings(), measured=0.9123)
    message = str(exc.value)
    assert "runner-z" in message
    assert "test_speed.reference_seconds" in message
    assert ".github/pr-reviewer.json" in message
    assert '"runner-z": 0.912' in message
    assert rs.RUNNER_CLASS_ENV in message
    assert "runner-a, runner-b" in message


def test_unknown_runner_class_without_a_measurement_says_how_to_get_one() -> None:
    with pytest.raises(SystemExit) as exc:
        rs.reference_for("runner-z", _settings())
    assert "runner_speed.py" in str(exc.value)


# --- the config section -----------------------------------------------------


def test_the_shipped_config_section_loads() -> None:
    settings = rs.load_settings()
    assert settings["budget_seconds"] == 10
    assert settings["recheck_fraction"] == 0.6
    assert (settings["factor_low"], settings["factor_high"]) == (0.5, 2.0)
    assert settings["shared_setup_budget_seconds"] == 30
    assert settings["default_runner_class"] == "ubuntu-latest"
    assert settings["reference_seconds"] == {"ubuntu-latest": 0.874}
    assert rs.DEFAULT_CONFIG == _CONFIG


def test_load_settings_reads_a_given_file(tmp_path: Path) -> None:
    assert rs.load_settings(_write_config(tmp_path, _settings())) == _settings()


@pytest.mark.parametrize(
    "section",
    [
        None,
        [],
        _settings(budget_seconds=0),
        _settings(budget_seconds=True),
        _settings(budget_seconds="10"),
        _settings(recheck_fraction=0),
        _settings(recheck_fraction=1.5),
        _settings(factor_low=0),
        _settings(factor_low=3.0),
        _settings(shared_setup_budget_seconds=-1),
        _settings(default_runner_class=""),
        _settings(default_runner_class="runner-z"),
        _settings(reference_seconds={}),
        _settings(reference_seconds={"runner-a": 0}),
        _settings(reference_seconds={"runner-a": True}),
    ],
    ids=lambda s: "bad",
)
def test_load_settings_rejects_a_bad_section(tmp_path: Path, section: Any) -> None:
    with pytest.raises(ValueError, match="test_speed"):
        rs.load_settings(_write_config(tmp_path, section))


def test_load_settings_rejects_a_missing_section(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"active": None}))
    with pytest.raises(ValueError, match="test_speed"):
        rs.load_settings(path)


@pytest.mark.parametrize("content", [None, "{not json"])
def test_load_settings_rejects_a_missing_or_broken_file(
    tmp_path: Path, content: str | None
) -> None:
    path = tmp_path / "config.json"
    if content is not None:
        path.write_text(content)
    with pytest.raises(ValueError, match=str(path)):
        rs.load_settings(path)


@pytest.mark.parametrize("key", rs.REQUIRED_KEYS)
def test_load_settings_rejects_a_missing_key(tmp_path: Path, key: str) -> None:
    section = _settings()
    del section[key]
    with pytest.raises(ValueError, match=key):
        rs.load_settings(_write_config(tmp_path, section))


# --- one config, stdlib only, runs by path ----------------------------------


def test_the_guard_has_one_config_file() -> None:
    """Budgets and references live only in ``.github/pr-reviewer.json``."""
    assert not list(_REPO.glob("conf/*speed*"))
    for script in (_SCRIPT, _CHECKER):
        text = script.read_text()
        assert "tomllib" not in text
        assert "pr-reviewer.json" in text


@pytest.mark.parametrize("script", [_SCRIPT, _CHECKER], ids=lambda p: p.name)
def test_the_scripts_import_only_the_standard_library(script: Path) -> None:
    tree = ast.parse(script.read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names), roots - sys.stdlib_module_names


def test_runner_speed_runs_by_path(tmp_path: Path) -> None:
    out = subprocess.run(  # nosec B603 -- fixed argv, our own script
        [sys.executable, str(_SCRIPT), "--repeats", "1"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin"},
    ).stdout
    assert out.startswith("runner_class=ubuntu-latest calibrate_s=")
