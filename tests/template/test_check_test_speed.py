"""Tests for ``scripts/check_test_speed.py``, the stack-neutral per-test speed guard.

The checker is loaded by path, as plain ``python3`` runs it in a repo with no
``scripts`` package. Most tests drive :func:`main` with a pinned ``--factor`` and
a fake :func:`measure_all`, so the re-measure verdicts are tested without running
a real test suite; two tests run a real (tiny) re-run command.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import subprocess  # nosec B404 -- runs our own script with a fixed argv
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any
from xml.sax.saxutils import quoteattr

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "check_test_speed.py"
_RERUN = "runner {test}"
_PY_MOD = "tests.pkg.test_mod"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_test_speed_under_test", _SCRIPT
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cts = _load()

# (classname, name, seconds, shared setups as (fixture, scope, seconds, where))
Case = tuple[str, str, float, Sequence[tuple[str, str, float, str]]]


def _case(
    name: str, seconds: float, shared: Sequence[tuple[str, str, float, str]] = ()
) -> Case:
    return (_PY_MOD, name, seconds, shared)


def _tid(name: str) -> str:
    return f"{_PY_MOD}::{name}"


def _junit(
    path: Path, cases: Iterable[Case], suites_name: str = "pytest tests"
) -> Path:
    rows = []
    for classname, name, seconds, shared in cases:
        props = "".join(
            f'<property name="{cts.SHARED_FIXTURE_PROPERTY}" '
            f'value="{fixture}|{scope}|{secs}|{where}" />'
            for fixture, scope, secs, where in shared
        )
        body = f"<properties>{props}</properties>" if props else ""
        rows.append(
            f"<testcase classname={quoteattr(classname)} name={quoteattr(name)} "
            f'time="{seconds}">{body}</testcase>'
        )
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites name="{suites_name}"><testsuite name="suite">'
        + "".join(rows)
        + "</testsuite></testsuites>"
    )
    return path


def _config(tmp_path: Path, **overrides: Any) -> Path:
    section: dict[str, Any] = {
        "budget_seconds": 10,
        "recheck_fraction": 0.6,
        "factor_low": 0.5,
        "factor_high": 2.0,
        "shared_setup_budget_seconds": 30,
        "default_runner_class": "runner-a",
        "reference_seconds": {"runner-a": 0.5},
    }
    section.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"active": None, "test_speed": section}))
    return path


class _FakeMeasure:
    """Stand-in for :func:`measure_all`: answers from per-pass tables."""

    def __init__(self, *passes: dict[str, float | None]) -> None:
        self.passes = list(passes)
        self.calls: list[tuple[list[str], str, int]] = []

    def __call__(
        self, cases: list[Any], template: str, workers: int
    ) -> dict[str, float | None]:
        ids = [case.test_id for case in cases]
        self.calls.append((ids, template, workers))
        table = self.passes[min(len(self.calls), len(self.passes)) - 1]
        return {tid: table.get(tid) for tid in ids}


def _run(
    tmp_path: Path,
    cases: Iterable[Case],
    *extra: str,
    allow: str = "",
    factor: str | None = "1",
    measure: Callable[..., dict[str, float | None]] | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> int:
    junit = _junit(tmp_path / "junit.xml", cases)
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text(allow)
    if measure is not None and monkeypatch is not None:
        monkeypatch.setattr(cts, "measure_all", measure)
    argv = [
        "--junit",
        str(junit),
        "--allowlist",
        str(allowlist),
        "--rerun",
        _RERUN,
        "--config",
        str(_config(tmp_path)),
    ]
    if factor is not None:
        argv += ["--factor", factor]
    result: int = cts.main([*argv, *extra])
    return result


# --- bad input is exit 2 ----------------------------------------------------


def test_missing_junit_is_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cts.main(
        [
            "--junit",
            str(tmp_path / "absent.xml"),
            "--allowlist",
            str(tmp_path / "allow.txt"),
            "--rerun",
            _RERUN,
            "--config",
            str(_config(tmp_path)),
            "--factor",
            "1",
        ]
    )
    assert code == 2
    assert "absent.xml" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    ["", "   \n", "<testsuites><testsuite", '<testsuites name="x"></testsuites>'],
    ids=["empty", "blank", "unparseable", "no-testcases"],
)
def test_empty_junit_is_exit_2(
    tmp_path: Path, content: str, capsys: pytest.CaptureFixture[str]
) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(content)
    code = cts.main(
        [
            "--junit",
            str(junit),
            "--allowlist",
            str(tmp_path / "allow.txt"),
            "--rerun",
            _RERUN,
            "--config",
            str(_config(tmp_path)),
            "--factor",
            "1",
        ]
    )
    assert code == 2
    assert "junit.xml" in capsys.readouterr().err


@pytest.mark.parametrize("time_attr", ["", 'time="abc"', 'time="nan"', 'time="-1"'])
def test_a_testcase_without_a_usable_time_is_exit_2(
    tmp_path: Path, time_attr: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing time must not read as 0 s, which would pass that test."""
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites name="pytest tests"><testsuite>'
        f'<testcase classname="{_PY_MOD}" name="test_a" {time_attr} />'
        "</testsuite></testsuites>"
    )
    code = cts.main(
        [
            "--junit",
            str(junit),
            "--allowlist",
            str(tmp_path / "allow.txt"),
            "--rerun",
            _RERUN,
            "--config",
            str(_config(tmp_path)),
            "--factor",
            "1",
        ]
    )
    assert code == 2
    assert "test_a" in capsys.readouterr().err


def test_an_unreadable_junit_is_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    junit = _junit(tmp_path / "junit.xml", [_case("test_a", 1.0)])

    def _deny(self: Path) -> bytes:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "read_bytes", _deny)
    code = cts.main(
        [
            "--junit",
            str(junit),
            "--allowlist",
            str(tmp_path / "allow.txt"),
            "--rerun",
            _RERUN,
            "--config",
            str(_config(tmp_path)),
            "--factor",
            "1",
        ]
    )
    assert code == 2
    assert "unreadable" in capsys.readouterr().err


def test_an_undecodable_junit_is_exit_2(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_bytes(
        b'<?xml version="1.0" encoding="utf-8"?><testsuites name="\xff\xfe">'
        b'<testcase classname="m" name="t" time="1" /></testsuites>'
    )
    with pytest.raises(cts.BadInput, match="unparseable"):
        cts.parse_junit(junit)


@pytest.mark.parametrize("kind", ["undecodable", "directory"])
def test_an_unreadable_allowlist_is_exit_2(
    tmp_path: Path,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    allowlist = tmp_path / "allow"
    if kind == "directory":
        allowlist.mkdir()
    else:
        allowlist.write_bytes(b"\xff\xfe\x00bad")
    junit = _junit(tmp_path / "junit.xml", [_case("test_a", 1.0)])
    code = cts.main(
        [
            "--junit",
            str(junit),
            "--allowlist",
            str(allowlist),
            "--rerun",
            _RERUN,
            "--config",
            str(_config(tmp_path)),
            "--factor",
            "1",
        ]
    )
    assert code == 2
    assert "allowlist" in capsys.readouterr().err


def test_a_pinned_factor_says_it_bypasses_the_clamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run(
        tmp_path,
        [_case("test_a", 1.0)],
        factor="3",
        measure=_FakeMeasure({}),
        monkeypatch=monkeypatch,
    )
    err = capsys.readouterr().err
    assert code == 0
    assert "bypasses the 0.5-2 clamp" in err
    assert "30.00s per test" in err
    assert "bypasses" in cts._build_parser().format_help()


def test_bad_config_is_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    junit = _junit(tmp_path / "junit.xml", [_case("test_a", 1.0)])
    code = cts.main(
        [
            "--junit",
            str(junit),
            "--allowlist",
            str(tmp_path / "allow.txt"),
            "--rerun",
            _RERUN,
            "--config",
            str(_config(tmp_path, budget_seconds=0)),
        ]
    )
    assert code == 2
    assert "budget_seconds" in capsys.readouterr().err


def test_unknown_runner_class_is_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TEST_SPEED_RUNNER_CLASS", "runner-z")
    monkeypatch.setattr(cts.runner_speed, "calibrate", lambda: 0.777)
    code = _run(tmp_path, [_case("test_a", 1.0)], factor=None)
    err = capsys.readouterr().err
    assert code == 2
    assert "test_speed.reference_seconds" in err
    assert '"runner-z": 0.777' in err


@pytest.mark.parametrize("pinned", ["nan", "0", "-1", "inf"])
def test_pinned_factor_must_be_positive_and_finite(
    tmp_path: Path,
    pinned: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cts.runner_speed, "calibrate", lambda: 0.5)
    code = _run(tmp_path, [_case("test_a", 1.0)], factor=pinned)
    assert code == 2
    assert "--factor" in capsys.readouterr().err


def test_rerun_without_the_test_placeholder_is_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    junit = _junit(tmp_path / "junit.xml", [_case("test_a", 1.0)])
    code = cts.main(
        [
            "--junit",
            str(junit),
            "--allowlist",
            str(tmp_path / "allow.txt"),
            "--rerun",
            "runner --all",
            "--config",
            str(_config(tmp_path)),
            "--factor",
            "1",
        ]
    )
    assert code == 2
    assert "{test}" in capsys.readouterr().err


# --- verdicts -----------------------------------------------------------------


def test_within_budget_passes_without_remeasuring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    measure = _FakeMeasure({})
    code = _run(
        tmp_path,
        [_case("test_a", 1.0), _case("test_b", 5.9)],
        measure=measure,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert measure.calls == []
    assert "OK" in capsys.readouterr().out


def test_contended_overage_is_dismissed_when_fast_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    measure = _FakeMeasure({_tid("test_a"): 3.9})
    code = _run(
        tmp_path, [_case("test_a", 25.0)], measure=measure, monkeypatch=monkeypatch
    )
    err = capsys.readouterr().err
    assert code == 0
    assert "contention" in err
    assert "25.00s -> " in err and "3.90s alone" in err


def test_still_slow_alone_fails_with_both_times_and_the_remedy_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    measure = _FakeMeasure({_tid("test_a"): 14.0})
    code = _run(
        tmp_path, [_case("test_a", 20.0)], measure=measure, monkeypatch=monkeypatch
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "suite 20.00s" in err and "alone 14.00s" in err
    assert _tid("test_a") in err
    speed, move, allow = (
        err.index("Speed it up"),
        err.index("slower test group"),
        err.index("allowlist it"),
    )
    assert speed < move < allow
    assert "mock" in err[speed:move]
    assert "reason" in err[allow:] and "tracking issue" in err[allow:]
    # The re-run command a developer can paste is printed too.
    assert "runner tests/pkg/test_mod.py::test_a" in err


def test_a_confirmed_overage_takes_the_faster_of_two_solo_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    measure = _FakeMeasure({_tid("test_a"): 12.0}, {_tid("test_a"): 9.0})
    code = _run(
        tmp_path, [_case("test_a", 20.0)], measure=measure, monkeypatch=monkeypatch
    )
    assert code == 0
    assert [call[0] for call in measure.calls] == [[_tid("test_a")]] * 2
    assert measure.calls[1][2] == 1  # the confirming pass is serial


def test_unmeasurable_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    measure = _FakeMeasure({_tid("test_a"): None})
    code = _run(
        tmp_path, [_case("test_a", 8.0)], measure=measure, monkeypatch=monkeypatch
    )
    assert code == 1
    assert "could not be re-measured" in capsys.readouterr().err


def test_a_near_budget_test_is_remeasured_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared work can hide a slow test in the suite; recheck_fraction catches it."""
    measure = _FakeMeasure({_tid("test_a"): 16.0})
    code = _run(
        tmp_path,
        [_case("test_a", 6.5), _case("test_b", 5.9)],
        measure=measure,
        monkeypatch=monkeypatch,
    )
    assert code == 1
    assert measure.calls[0][0] == [_tid("test_a")]


def test_no_recheck_fails_on_the_suite_time_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    measure = _FakeMeasure({})
    code = _run(
        tmp_path,
        [_case("test_a", 11.0), _case("test_b", 8.0)],
        "--no-recheck",
        measure=measure,
        monkeypatch=monkeypatch,
    )
    err = capsys.readouterr().err
    assert code == 1
    assert measure.calls == []
    assert _tid("test_a") in err and _tid("test_b") not in err


def test_the_rerun_template_reaches_the_measure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    measure = _FakeMeasure({_tid("test_a"): 1.0})
    _run(tmp_path, [_case("test_a", 11.0)], measure=measure, monkeypatch=monkeypatch)
    assert measure.calls[0][1] == _RERUN


def test_budget_scales_with_the_factor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    measure = _FakeMeasure({_tid("test_a"): 15.0})
    cases = [_case("test_a", 15.0)]
    assert _run(tmp_path, cases, measure=measure, monkeypatch=monkeypatch) == 1
    assert (
        _run(tmp_path, cases, factor="2", measure=measure, monkeypatch=monkeypatch) == 0
    )
    assert "20.00s per test" in capsys.readouterr().err


def test_a_calibrated_factor_is_printed_and_clamped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("TEST_SPEED_RUNNER_CLASS", raising=False)
    monkeypatch.setattr(cts.runner_speed, "calibrate", lambda: 5.0)
    code = _run(tmp_path, [_case("test_a", 1.0)], factor=None)
    err = capsys.readouterr().err
    assert code == 0
    assert "runner class runner-a" in err
    assert "calibration 5.000s" in err
    assert "reference 0.500s" in err
    assert "factor 2.00" in err
    assert "20.00s per test" in err


# --- allowlist ------------------------------------------------------------------


def test_parse_allowlist_ignores_comments_and_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "allow.txt"
    path.write_text(
        "# header comment\n\n"
        "tests.pkg.test_mod::test_a  # slow parser, tracked in owner/repo#1\n"
        "   src/app/view.test.tsx::view > renders   \n"
    )
    assert cts.parse_allowlist(path) == {
        "tests.pkg.test_mod::test_a",
        "src/app/view.test.tsx::view > renders",
    }
    assert cts.parse_allowlist(tmp_path / "absent.txt") == set()


def test_an_allowlisted_slow_test_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    measure = _FakeMeasure({})
    code = _run(
        tmp_path,
        [_case("test_a", 30.0)],
        allow=f"{_tid('test_a')}  # reason, owner/repo#1\n",
        measure=measure,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert measure.calls == []


def test_a_stale_allowlist_entry_is_a_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run(
        tmp_path,
        [_case("test_a", 1.0), _case("test_b", 7.0)],
        allow=f"{_tid('test_a')}\n{_tid('test_b')}\n{_tid('test_gone')}\n",
        measure=_FakeMeasure({}),
        monkeypatch=monkeypatch,
    )
    err = capsys.readouterr().err
    assert code == 0
    assert "prune" in err
    assert _tid("test_a") in err and _tid("test_gone") in err
    assert _tid("test_b") not in err  # near the line: kept, so it cannot flap


def test_no_stale_warning_from_a_contended_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(
        tmp_path,
        [_case("test_a", 1.0), _case("test_b", 25.0)],
        allow=f"{_tid('test_a')}\n",
        measure=_FakeMeasure({_tid("test_b"): 3.0}),
        monkeypatch=monkeypatch,
    )
    assert "prune" not in capsys.readouterr().err


# --- shared-fixture setup -----------------------------------------------------------


def test_shared_setup_is_subtracted_from_the_test_that_paid_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    measure = _FakeMeasure({})
    code = _run(
        tmp_path,
        [_case("test_a", 14.0, [("db", "session", 9.0, "tests/")])],
        measure=measure,
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert measure.calls == []


@pytest.mark.parametrize(("other", "expected"), [(31.0, 1), (20.0, 0)])
def test_the_shared_budget_judges_the_fastest_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    other: float,
    expected: int,
) -> None:
    code = _run(
        tmp_path,
        [
            _case("test_a", 36.0, [("db", "session", 35.0, "tests/")]),
            _case("test_b", other + 1, [("db", "session", other, "tests/")]),
        ],
        measure=_FakeMeasure({}),
        monkeypatch=monkeypatch,
    )
    assert code == expected
    if expected:
        err = capsys.readouterr().err
        assert "db" in err and "31.00s" in err
        assert "shared_setup_budget_seconds" in err


def test_same_named_fixtures_in_different_modules_are_judged_apart() -> None:
    timings = [
        cts.CaseTiming("m", "a", 5.0, (cts.SharedSetup("db", "module", 4.0, "a/"),)),
        cts.CaseTiming("m", "b", 5.0, (cts.SharedSetup("db", "module", 2.0, "b/"),)),
    ]
    assert cts.fastest_shared_setups(timings) == {
        ("db", "module", "a/"): 4.0,
        ("db", "module", "b/"): 2.0,
    }


def test_a_malformed_shared_property_is_ignored(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites name="pytest tests"><testsuite>'
        '<testcase classname="tests.test_m" name="test_a" time="3.0"><properties>'
        f'<property name="{cts.SHARED_FIXTURE_PROPERTY}" value="db|session|x|" />'
        '<property name="other" value="1" />'
        "</properties></testcase></testsuite></testsuites>"
    )
    (timing,) = cts.parse_junit(junit)
    assert timing.shared == ()
    assert timing.charged == 3.0


# --- JUnit dialects and re-run selectors ---------------------------------------------


@pytest.mark.parametrize(
    ("classname", "name", "nodeid"),
    [
        ("tests.pkg.test_mod", "test_a", "tests/pkg/test_mod.py::test_a"),
        ("tests.pkg.test_mod.TestX", "test_a", "tests/pkg/test_mod.py::TestX::test_a"),
        (
            "tests.test_mod.TestX.TestY",
            "test_a[1-2]",
            "tests/test_mod.py::TestX::TestY::test_a[1-2]",
        ),
        ("test_mod", "test_a", "test_mod.py::test_a"),
    ],
)
def test_pytest_node_ids(classname: str, name: str, nodeid: str) -> None:
    assert cts.pytest_nodeid(classname, name) == nodeid


def test_pytest_junit_is_parsed(tmp_path: Path) -> None:
    junit = _junit(
        tmp_path / "junit.xml",
        [_case("test_a", 2.5, [("db", "session", 1.5, "tests/")])],
    )
    (timing,) = cts.parse_junit(junit)
    assert timing.dialect == "pytest"
    assert timing.test_id == _tid("test_a")
    assert timing.total == 2.5
    assert timing.shared == (cts.SharedSetup("db", "session", 1.5, "tests/"),)
    assert timing.charged == 1.0
    assert cts.rerun_selector(timing) == "tests/pkg/test_mod.py::test_a"


def test_vitest_junit_is_parsed(tmp_path: Path) -> None:
    junit = _junit(
        tmp_path / "junit.xml",
        [
            ("src/app/view.test.tsx", "view > renders the list", 1.25, ()),
            ("src/util.test.ts", "adds", 0.5, ()),
        ],
        suites_name="vitest tests",
    )
    first, second = cts.parse_junit(junit)
    assert first.dialect == second.dialect == "vitest"
    assert first.test_id == "src/app/view.test.tsx::view > renders the list"
    assert first.charged == 1.25
    assert shlex.split(cts.rerun_selector(first)) == [
        "src/app/view.test.tsx",
        "-t",
        "^view > renders the list$",
    ]


def test_vitest_is_recognized_by_a_file_path_classname(tmp_path: Path) -> None:
    junit = _junit(
        tmp_path / "junit.xml",
        [("src/app/view.test.tsx", "renders", 1.0, ())],
        suites_name="custom",
    )
    assert cts.parse_junit(junit)[0].dialect == "vitest"


@pytest.mark.parametrize(
    "name",
    [
        "math > adds (a+b) [x]? {1,2} ^$ |pipe| *star* .dot",
        'says "hello" and \\backslash\\',
        "it's $HOME and `cmd`",
    ],
)
def test_vitest_rerun_filter_escapes_regex_characters(name: str) -> None:
    timing = cts.CaseTiming("src/app/a b.test.ts", name, 1.0, (), "vitest")
    argv = shlex.split(f"runner {cts.rerun_selector(timing)} --flag")
    assert argv[:3] == ["runner", "src/app/a b.test.ts", "-t"]
    assert argv[4] == "--flag"
    pattern = argv[3]
    assert re.fullmatch(pattern, name)
    assert not re.search(pattern, name + " and more")
    assert not re.search(pattern, "x" + name)


def test_rerun_argv_expands_the_placeholders(tmp_path: Path) -> None:
    timing = cts.CaseTiming(_PY_MOD, "test_a[x y]", 1.0)
    argv = cts.rerun_argv(
        "uv run pytest {test} -q --junitxml={junit}", timing, tmp_path / "o.xml"
    )
    assert argv == [
        "uv",
        "run",
        "pytest",
        "tests/pkg/test_mod.py::test_a[x y]",
        "-q",
        f"--junitxml={tmp_path / 'o.xml'}",
    ]


# --- a real re-run ----------------------------------------------------------------------

_FAKE_RUNNER = """
import sys
from pathlib import Path
out, selector = sys.argv[1], sys.argv[2]
classname, name = "tests.pkg.test_mod", selector.rsplit("::", 1)[1]
Path(out).write_text(
    '<testsuites name="pytest tests"><testsuite>'
    f'<testcase classname="{classname}" name="{name}" time="12.5" />'
    f'<testcase classname="{classname}" name="other" time="99" />'
    '</testsuite></testsuites>'
)
"""


def test_measure_reads_the_test_from_the_rerun_junit(tmp_path: Path) -> None:
    runner = tmp_path / "fake_runner.py"
    runner.write_text(_FAKE_RUNNER)
    template = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(runner))} {{junit}} {{test}}"
    )
    timing = cts.CaseTiming(_PY_MOD, "test_a", 20.0)
    assert cts.measure_alone(timing, template) == 12.5


def _writer(tmp_path: Path, xml: str) -> str:
    """Return a ``--rerun`` template whose command writes ``xml`` to ``{junit}``."""
    runner = tmp_path / "writer.py"
    runner.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"Path(sys.argv[1]).write_text({xml!r})\n"
    )
    return (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(runner))} {{junit}} {{test}}"
    )


def test_a_rerun_whose_exact_match_is_skipped_is_unmeasurable(tmp_path: Path) -> None:
    """A skipped re-run is 0 s, which must not pass an over-budget test."""
    template = _writer(
        tmp_path,
        '<testsuites name="pytest tests"><testsuite>'
        f'<testcase classname="{_PY_MOD}" name="test_a" time="0"><skipped /></testcase>'
        f'<testcase classname="{_PY_MOD}" name="other" time="0.1" />'
        "</testsuite></testsuites>",
    )
    assert cts.measure_alone(cts.CaseTiming(_PY_MOD, "test_a", 20.0), template) is None


def test_a_rerun_with_no_tests_is_unmeasurable(tmp_path: Path) -> None:
    template = _writer(
        tmp_path,
        '<testsuites name="pytest tests"><testsuite>'
        f'<testcase classname="{_PY_MOD}" name="other" time="0"><skipped /></testcase>'
        "</testsuite></testsuites>",
    )
    assert cts.measure_alone(cts.CaseTiming(_PY_MOD, "test_a", 20.0), template) is None
    empty = _writer(tmp_path, '<testsuites name="pytest tests"></testsuites>')
    assert cts.measure_alone(cts.CaseTiming(_PY_MOD, "test_a", 20.0), empty) is None


def test_an_unmeasurable_skip_fails_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = _writer(
        tmp_path,
        '<testsuites name="pytest tests"><testsuite>'
        f'<testcase classname="{_PY_MOD}" name="test_a" time="0"><skipped /></testcase>'
        "</testsuite></testsuites>",
    )
    junit = _junit(tmp_path / "junit.xml", [_case("test_a", 20.0)])
    code = cts.main(
        [
            "--junit",
            str(junit),
            "--allowlist",
            str(tmp_path / "allow.txt"),
            "--rerun",
            template,
            "--config",
            str(_config(tmp_path)),
            "--factor",
            "1",
        ]
    )
    assert code == 1


def test_measure_without_a_junit_placeholder_times_the_command() -> None:
    template = (
        f'{shlex.quote(sys.executable)} -c "import time; time.sleep(0.2)" {{test}}'
    )
    timing = cts.CaseTiming(_PY_MOD, "test_a", 20.0)
    seconds = cts.measure_alone(timing, template)
    assert seconds is not None and 0.2 <= seconds < 10


def test_measure_of_a_missing_command_is_unmeasurable() -> None:
    timing = cts.CaseTiming(_PY_MOD, "test_a", 20.0)
    assert cts.measure_alone(timing, "/nonexistent/runner {test}") is None


def test_measure_all_runs_every_case(tmp_path: Path) -> None:
    runner = tmp_path / "fake_runner.py"
    runner.write_text(_FAKE_RUNNER)
    template = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(runner))} {{junit}} {{test}}"
    )
    cases = [cts.CaseTiming(_PY_MOD, f"test_{c}", 20.0) for c in "ab"]
    assert cts.measure_all(cases, template, 2) == {
        _tid("test_a"): 12.5,
        _tid("test_b"): 12.5,
    }


def test_the_checker_runs_by_path_with_plain_python(tmp_path: Path) -> None:
    junit = _junit(tmp_path / "junit.xml", [_case("test_a", 1.0)])
    proc = subprocess.run(  # nosec B603 -- fixed argv, our own script
        [
            sys.executable,
            str(_SCRIPT),
            "--junit",
            str(junit),
            "--allowlist",
            str(tmp_path / "allow.txt"),
            "--rerun",
            _RERUN,
            "--factor",
            "1",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
    assert "10.00s per test" in proc.stderr  # the shipped config's budget
