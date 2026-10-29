r"""Fail if any test's own time exceeds a per-test budget (stack-neutral speed guard).

Guards against test-speed regressions such as a "unit" test quietly doing a minute
of real network I/O. Reads the JUnit XML the coverage run wrote (pytest's or
Vitest's) and fails when a test not on the committed allowlist took longer than
the budget. Settings live in the ``test_speed`` section of
``.github/pr-reviewer.json``, shared by both stacks.

Why a per-test budget rather than a total wall-clock baseline: it names the exact
offender and needs no stored cross-run state.

**The JUnit number is wall-clock, and wall-clock is contended.** A parallel suite
run has its workers compete for cores, so every test's reported time inflates with
machine load while its work is unchanged; a guard that trusts that number accuses
a different innocent test on every busy run. Switching to CPU time is not the fix:
the pathology being guarded (real network I/O, a sleep, a subprocess) burns almost
no CPU, so a CPU-time budget would be blind to exactly what this exists to catch.
Wall-clock is the right metric; the problem is measuring it under contention. So a
suspect test is **re-run alone** with the stack's re-run command and fails only if
it is still over the budget by itself. Pass ``--no-recheck`` to judge the suite
numbers as they are.

**Contention also cuts the other way, so near-budget tests are re-measured too.**
A test can read under budget in the suite yet be over it alone, because a sibling
already paid for a cache it needs. Re-measuring only over-budget tests would make
the verdict depend on which runner a PR drew, so every un-allowlisted test above
``budget * recheck_fraction`` is re-measured alone.

**Shared-fixture setup has its own budget (pytest only).** JUnit's time is setup +
call + teardown, and a session/package/module/class-scoped fixture is set up inside
whichever test first requests it, so that test carries a cost the whole suite
shares. A pytest plugin records each shared setup on the test that paid it, as a
``shared_fixture_setup`` JUnit property (``"name|scope|seconds|where"``). A test is
charged its JUnit time minus that, so function-scoped setup (paid by every test)
still counts, and each shared fixture is judged against
``shared_setup_budget_seconds`` on its **fastest** setup in the run: each parallel
worker sets it up once, and the least contended of those is the honest cost.
Vitest has no such hook, so its tests are judged on their full time.

**Both budgets are seconds on a typical runner of a class, scaled by measured
runner speed.** A slower runner makes every test slower, alone or not. So at start,
before any re-measure, the guard times a fixed workload (``runner_speed.py``),
divides it by the reference for this runner class (``TEST_SPEED_RUNNER_CLASS``, else
``default_runner_class``) and scales both budgets by the result, clamped to
``factor_low``-``factor_high`` so a pathological runner can neither switch the guard
off nor make it impossible. A laptop is just a faster runner: it gets a stricter
budget than CI, not a more lenient one. ``--factor`` pins the factor instead.

**The allowlist is a ratchet.** It names today's legitimately slow tests (one per
line, ``# reason`` after it) so the guard blocks *new* slow tests; an entry that has
become comfortably fast is reported so the list shrinks.

A test id is ``"{classname}::{name}"`` as JUnit reports it, e.g.
``tests.pkg.test_mod.TestX::test_a`` (pytest) or ``src/app/view.test.tsx::view >
renders`` (Vitest). In ``--rerun``, ``{test}`` expands to the pytest node id or to
``<file> -t "<name as an anchored, escaped regex>"`` for Vitest, and an optional
``{junit}`` to a temp file the re-run writes its JUnit to. With ``{junit}`` the
re-run is judged on the test's own JUnit time, and a re-run whose match was
skipped or that ran no test is unmeasurable (fails closed). Without it the
re-run is judged on the whole command's wall-clock when it exits 0: start-up is
included, so a test that ran reads slower than it is, but a command that exits 0
without running the test reads fast. Always pass ``{junit}``.

Stdlib only and runnable by path::

    python3 scripts/check_test_speed.py --junit <path> --allowlist <path> \
        --rerun '<cmd with {test}>' [--config .github/pr-reviewer.json] \
        [--factor F] [--no-recheck]

Exit codes: 0 within budget, 1 an un-allowlisted offender (or a shared fixture over
its budget), 2 bad input (JUnit missing, empty, unparseable or without testcases;
bad config, runner class or ``--factor``; a ``--rerun`` without ``{test}``).
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import os
import re
import shlex
import subprocess  # nosec B404 - runs the stack's own test command, no shell
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple
from xml.etree import ElementTree  # nosec B405 - parses our own test run's JUnit XML


def _load_runner_speed() -> ModuleType:
    """Load the sibling ``runner_speed.py`` by path.

    ``scripts/`` is not always a package (a repo initialized for one stack drops
    its ``__init__.py``), so the sibling is loaded by file path, which works both
    for ``python3 scripts/check_test_speed.py`` and for a test loading this file.

    Returns
    -------
    ModuleType
        The ``runner_speed`` module.

    Raises
    ------
    ImportError
        If the file cannot be loaded.
    """
    path = Path(__file__).resolve().with_name("runner_speed.py")
    spec = importlib.util.spec_from_file_location("runner_speed", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner_speed = _load_runner_speed()

#: The JUnit user property the pytest fixture-timing plugin writes, one per
#: shared-fixture setup a test paid for: ``"name|scope|seconds|where"``.
SHARED_FIXTURE_PROPERTY = "shared_fixture_setup"

#: An allowlisted test is reported stale (prune it) only when it is COMFORTABLY
#: under budget, so a test hovering near the line does not flap between "over"
#: and "stale" from run to run.
STALE_FRACTION = 0.5

#: Wall-clock cap on one re-measure: generous so a slow test is measured rather
#: than killed, bounded so a hung test cannot stall the gate. A timeout reads as
#: "unmeasurable", which fails closed.
ISOLATION_TIMEOUT_SECONDS = 600.0

#: Most re-measures in flight at once, each in its own process. One process per
#: test keeps its cost from hiding behind a cache a sibling filled; a few at a
#: time stops the re-check costing more than the suite. Every re-measure that
#: still reads over budget is confirmed serially, so contention between them can
#: never fail a test.
MAX_RECHECK_WORKERS = 4

TEST_PLACEHOLDER = "{test}"
JUNIT_PLACEHOLDER = "{junit}"

#: JavaScript regex syntax characters (``-t`` is a JS ``RegExp``). Python's
#: ``re.escape`` also escapes spaces, ``-``, ``#`` and others, which a ``u``-flag
#: JS regex rejects, so only these are escaped.
_JS_REGEX_SPECIAL = re.compile(r"[.*+?^${}()|[\]\\]")


class BadInput(ValueError):
    """The guard was given input it cannot judge (exit 2)."""


class SharedSetup(NamedTuple):
    """One shared-scope fixture setup recorded on a test.

    Attributes
    ----------
    name : str
        The fixture's name.
    scope : str
        Its scope (``session``, ``package``, ``module`` or ``class``).
    seconds : float
        Its own setup time (nested shared setup excluded).
    where : str
        Its definition location; two modules' same-named fixtures differ here.
    """

    name: str
    scope: str
    seconds: float
    where: str = ""


class CaseTiming(NamedTuple):
    """One JUnit testcase's time and the shared setup inside it.

    Attributes
    ----------
    classname : str
        JUnit ``classname``: a dotted module (pytest) or a file path (Vitest).
    name : str
        JUnit ``name``: the test function (pytest) or ``"suite > test"`` (Vitest).
    total : float
        JUnit's time: setup + call + teardown.
    shared : tuple[SharedSetup, ...]
        Shared-fixture setups this test paid for.
    dialect : str
        ``"pytest"`` or ``"vitest"``.
    skipped : bool
        Whether the test was skipped.
    """

    classname: str
    name: str
    total: float
    shared: tuple[SharedSetup, ...] = ()
    dialect: str = "pytest"
    skipped: bool = False

    @property
    def test_id(self) -> str:
        """str: The ``"{classname}::{name}"`` id the allowlist uses."""
        return f"{self.classname}::{self.name}"

    @property
    def charged(self) -> float:
        """float: The test's own time: ``total`` less its shared-fixture setup."""
        return max(self.total - sum(s.seconds for s in self.shared), 0.0)


def _seconds(raw: str | None, where: str) -> float:
    """Parse a JUnit ``time`` attribute.

    A missing or unreadable time is bad input, never 0 s: reading it as 0 would
    pass the very test it belongs to.

    Parameters
    ----------
    raw : str | None
        The attribute value.
    where : str
        The testcase and file, for the message.

    Returns
    -------
    float
        Seconds.

    Raises
    ------
    BadInput
        If the attribute is missing, not a number, negative or not finite.
    """
    if raw is None:
        raise BadInput(f"{where} has no time attribute.")
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise BadInput(f"{where} has an unreadable time {raw!r}.")
    return value


def _shared_setups(testcase: ElementTree.Element) -> tuple[SharedSetup, ...]:
    """Read the shared-fixture setups recorded on one testcase.

    Parameters
    ----------
    testcase : ElementTree.Element
        A ``<testcase>`` element.

    Returns
    -------
    tuple[SharedSetup, ...]
        One entry per well-formed ``shared_fixture_setup`` property.
    """
    found: list[SharedSetup] = []
    for prop in testcase.iter("property"):
        if prop.get("name") != SHARED_FIXTURE_PROPERTY:
            continue
        fixture, scope, secs, where = ((prop.get("value") or "").split("|", 3) + [""])[
            :4
        ]
        try:
            found.append(SharedSetup(fixture, scope, float(secs), where))
        except ValueError:
            continue
    return tuple(found)


def _dialect(root: ElementTree.Element, classnames: Iterable[str]) -> str:
    """Tell a Vitest JUnit file from a pytest one.

    Each runner names the root ``<testsuites>`` after itself by default; failing
    that, a Vitest ``classname`` is a file path while a pytest one is dotted.

    Parameters
    ----------
    root : ElementTree.Element
        The document root.
    classnames : Iterable[str]
        Every testcase's ``classname``.

    Returns
    -------
    str
        ``"vitest"`` or ``"pytest"``.
    """
    suite_name = (root.get("name") or "").lower()
    if "vitest" in suite_name:
        return "vitest"
    if "pytest" in suite_name:
        return "pytest"
    if any("/" in name or "\\" in name for name in classnames):
        return "vitest"
    return "pytest"


def parse_junit(path: Path) -> list[CaseTiming]:
    """Return every testcase in a pytest or Vitest JUnit file.

    Parameters
    ----------
    path : Path
        The JUnit XML file.

    Returns
    -------
    list[CaseTiming]
        One entry per ``<testcase>``.

    Raises
    ------
    BadInput
        If the file is missing, unreadable, empty, unparseable, has no testcases
        or has a testcase without a usable time: the gate measured nothing (or
        nothing it can trust), which must fail loudly.
    """
    if not path.is_file():
        raise BadInput(
            f"JUnit XML not found at {path}; the coverage run must write it, or "
            "this gate measured nothing."
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BadInput(f"JUnit XML at {path} is unreadable: {exc}") from exc
    if not data.strip():
        raise BadInput(f"JUnit XML at {path} is empty.")
    try:
        # Our own test run's report, not untrusted input.
        root = ElementTree.fromstring(data)  # nosec B314
    except (ElementTree.ParseError, ValueError) as exc:
        raise BadInput(f"JUnit XML at {path} is unparseable: {exc}") from exc
    testcases = list(root.iter("testcase"))
    if not testcases:
        raise BadInput(f"JUnit XML at {path} has no testcases.")
    dialect = _dialect(root, (tc.get("classname", "") for tc in testcases))
    return [
        CaseTiming(
            tc.get("classname", ""),
            tc.get("name", ""),
            _seconds(
                tc.get("time"),
                f"{path}: testcase {tc.get('classname', '')}::{tc.get('name', '')}",
            ),
            _shared_setups(tc),
            dialect,
            tc.find("skipped") is not None,
        )
        for tc in testcases
    ]


def parse_allowlist(path: Path) -> set[str]:
    """Return the allowlisted test ids in ``path``.

    Blank lines and ``#`` comments (whole-line or trailing) are ignored, so each
    entry carries its reason inline. A missing file is an empty allowlist.

    Parameters
    ----------
    path : Path
        The allowlist file.

    Returns
    -------
    set[str]
        Allowlisted ``"{classname}::{name}"`` ids.

    Raises
    ------
    BadInput
        If the file exists but cannot be read as text.
    """
    if not path.exists():
        return set()
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise BadInput(f"allowlist {path} is unreadable: {exc}") from exc
    entries = (line.split("#", 1)[0].strip() for line in text.splitlines())
    return {entry for entry in entries if entry}


def fastest_shared_setups(
    timings: Iterable[CaseTiming],
) -> dict[tuple[str, str, str], float]:
    """Return each shared fixture's FASTEST setup in the run.

    Every parallel worker sets a shared fixture up once, under varying load, so
    the least contended of those setups is its honest cost.

    Parameters
    ----------
    timings : Iterable[CaseTiming]
        The run's testcases.

    Returns
    -------
    dict[tuple[str, str, str], float]
        ``(name, scope, where)`` -> fastest setup seconds.
    """
    fastest: dict[tuple[str, str, str], float] = {}
    for timing in timings:
        for setup in timing.shared:
            key = (setup.name, setup.scope, setup.where)
            fastest[key] = min(setup.seconds, fastest.get(key, setup.seconds))
    return fastest


def pytest_nodeid(classname: str, name: str) -> str:
    """Convert a pytest JUnit ``classname`` and ``name`` into a node id.

    JUnit reports the classname dotted (``tests.pkg.test_mod`` or
    ``tests.pkg.test_mod.TestX``); pytest selects by path
    (``tests/pkg/test_mod.py::TestX::test_a``). Module and class parts are told
    apart by convention: modules are lowercase ``test_*``, classes CapWords.

    Parameters
    ----------
    classname : str
        The dotted JUnit classname.
    name : str
        The test name, parameters included.

    Returns
    -------
    str
        A pytest node id for command-line selection.
    """
    parts = [part for part in classname.split(".") if part]
    if not parts:
        return name
    split = next(
        (i for i in range(1, len(parts)) if parts[i][:1].isupper()), len(parts)
    )
    classes = "".join(f"::{part}" for part in parts[split:])
    return f"{'/'.join(parts[:split])}.py{classes}::{name}"


def rerun_selector(case: CaseTiming) -> str:
    """Return the shell-quoted words that select one test in its runner.

    Parameters
    ----------
    case : CaseTiming
        The test.

    Returns
    -------
    str
        The pytest node id, or for Vitest ``<file> -t "<pattern>"`` where the
        pattern is the full name with regex characters escaped and anchored, so
        it matches that test and no other.
    """
    if case.dialect != "vitest":
        return shlex.quote(pytest_nodeid(case.classname, case.name))
    pattern = "^" + _JS_REGEX_SPECIAL.sub(r"\\\g<0>", case.name) + "$"
    quoted = pattern.replace("\\", "\\\\").replace('"', '\\"')
    return f'{shlex.quote(case.classname)} -t "{quoted}"'


def rerun_argv(template: str, case: CaseTiming, junit: Path) -> list[str]:
    """Expand the ``--rerun`` template for one test into an argv.

    Parameters
    ----------
    template : str
        The command, with ``{test}`` and optionally ``{junit}``.
    case : CaseTiming
        The test to run alone.
    junit : Path
        Where the re-run should write its JUnit XML.

    Returns
    -------
    list[str]
        The argv (no shell is involved).
    """
    text = template.replace(TEST_PLACEHOLDER, rerun_selector(case))
    return shlex.split(text.replace(JUNIT_PLACEHOLDER, shlex.quote(str(junit))))


def _check_template(template: str) -> str:
    """Validate the ``--rerun`` template.

    Parameters
    ----------
    template : str
        The command.

    Returns
    -------
    str
        The template, unchanged.

    Raises
    ------
    BadInput
        If it lacks ``{test}`` or does not split into words.
    """
    if TEST_PLACEHOLDER not in template:
        raise BadInput(
            f"--rerun must contain {TEST_PLACEHOLDER}, where the test to re-run "
            f"alone goes; got {template!r}"
        )
    try:
        shlex.split(template)
    except ValueError as exc:
        raise BadInput(f"--rerun does not split into words: {exc}") from exc
    return template


def measure_alone(case: CaseTiming, template: str) -> float | None:
    """Re-run one test by itself and return its time in seconds.

    Parameters
    ----------
    case : CaseTiming
        The test to re-measure.
    template : str
        The ``--rerun`` command.

    Returns
    -------
    float | None
        With ``{junit}`` in the template, the test's charged time from the
        re-run's JUnit; otherwise the command's wall-clock, which counts only
        when it exits 0. ``None`` when it could not be measured (did not start,
        timed out, failed, wrote no usable JUnit, or ran no test: a skipped
        match is not a measurement): deliberately NOT "fast", the caller fails
        closed on it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "alone.xml"
        try:
            argv = rerun_argv(template, case, xml)
            start = time.perf_counter()
            # No shell; the argv is the stack's own test command plus a test id
            # taken from our own JUnit report.
            done = subprocess.run(  # nosec B603
                argv,
                capture_output=True,
                timeout=ISOLATION_TIMEOUT_SECONDS,
                check=False,
            )
            elapsed = time.perf_counter() - start
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return None
        if JUNIT_PLACEHOLDER not in template:
            return elapsed if done.returncode == 0 else None
        try:
            timings = parse_junit(xml)
        except BadInput:
            return None
    ran = [t for t in timings if not t.skipped]
    exact = [t.charged for t in ran if t.test_id == case.test_id]
    if exact:
        return max(exact)
    if any(t.test_id == case.test_id for t in timings):
        return None  # found, but skipped alone: no measurement
    # A parametrized id can differ in the re-run; take the slowest test that ran
    # rather than reporting "unmeasurable" (None when nothing ran at all).
    return max((t.charged for t in ran), default=None)


def default_recheck_workers() -> int:
    """Return how many re-measures to run at once: a quarter of the cores.

    Returns
    -------
    int
        Between 1 and :data:`MAX_RECHECK_WORKERS`.
    """
    return max(1, min(MAX_RECHECK_WORKERS, (os.cpu_count() or 1) // 4))


def measure_all(
    cases: list[CaseTiming], template: str, workers: int
) -> dict[str, float | None]:
    """Re-measure each test alone, ``workers`` processes at a time.

    Parameters
    ----------
    cases : list[CaseTiming]
        The tests to re-measure.
    template : str
        The ``--rerun`` command.
    workers : int
        How many re-runs to have in flight.

    Returns
    -------
    dict[str, float | None]
        Each test id's time alone (``None``: unmeasurable).
    """
    if not cases:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        times = pool.map(lambda case: measure_alone(case, template), cases)
        return {case.test_id: secs for case, secs in zip(cases, times)}


def budget_factor(settings: Mapping[str, Any], pinned: float | None) -> float:
    """Return what to scale this run's budgets by, and print how it was found.

    Unless pinned, this calibrates the machine and divides by the reference for
    its runner class, clamped to the configured range. Call it once, before any
    re-measure: a re-measure running beside the calibration would slow it.

    Parameters
    ----------
    settings : Mapping[str, Any]
        The ``test_speed`` section.
    pinned : float | None
        A factor to use as is, skipping calibration.

    Returns
    -------
    float
        The pinned factor, else the clamped measured one.

    Raises
    ------
    BadInput
        When ``pinned`` is not a positive finite number (``nan`` would make every
        budget comparison false and pass every test).
    SystemExit
        When the runner class has no reference (from ``runner_speed``).
    """
    name = runner_speed.runner_class(settings)
    low, high = float(settings["factor_low"]), float(settings["factor_high"])
    if pinned is not None:
        if not math.isfinite(pinned) or pinned <= 0:
            raise BadInput(f"--factor {pinned!r} is not a positive finite number.")
        print(
            f"test-speed guard: runner class {name}: runner-speed factor "
            f"{pinned:.2f}, pinned with --factor (not calibrated; a pin bypasses "
            f"the {low:g}-{high:g} clamp).",
            file=sys.stderr,
        )
        if not low <= pinned <= high:
            print(
                f"test-speed guard: note: the pinned factor is outside the "
                f"{low:g}-{high:g} clamp; using it as given.",
                file=sys.stderr,
            )
        return pinned
    measured = float(runner_speed.calibrate())
    reference = float(runner_speed.reference_for(name, settings, measured=measured))
    scale = float(runner_speed.factor(measured, reference, low=low, high=high))
    print(
        f"test-speed guard: runner class {name}: calibration {measured:.3f}s vs "
        f"reference {reference:.3f}s -> runner-speed factor {scale:.2f} "
        f"(clamped to {low:g}-{high:g}).",
        file=sys.stderr,
    )
    return scale


def _build_parser() -> argparse.ArgumentParser:
    """Return the argument parser.

    Returns
    -------
    argparse.ArgumentParser
        The configured parser.
    """
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--junit", type=Path, required=True, help="The coverage run's JUnit XML."
    )
    p.add_argument(
        "--allowlist",
        type=Path,
        required=True,
        help="The stack's slow-test allowlist: one test id per line, '# reason'.",
    )
    p.add_argument(
        "--rerun",
        required=True,
        help="Command that runs one test alone; {test} selects it, the optional "
        "{junit} is where it writes its JUnit XML.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=runner_speed.DEFAULT_CONFIG,
        help="Settings file with the test_speed section "
        "(default: .github/pr-reviewer.json).",
    )
    p.add_argument(
        "--factor",
        type=float,
        default=None,
        help="Escape hatch: pin the runner-speed factor both budgets are scaled "
        "by instead of calibrating this machine. A pin bypasses the configured "
        "factor_low-factor_high clamp. Must be positive and finite.",
    )
    p.add_argument(
        "--no-recheck",
        dest="recheck",
        action="store_false",
        help="Judge the suite-run numbers as they are, without re-running suspects "
        "alone (contention-sensitive).",
    )
    return p


def _remeasure(
    suspects: list[CaseTiming], template: str, budget: float
) -> dict[str, float | None]:
    """Re-measure the suspects alone, confirming every overage serially.

    One solo sample can catch a cold import or a busy moment, and the batched
    pass runs a few re-measures at once, so a test that reads over budget is run
    again with nothing else running and the faster of its two runs is the
    verdict. Contention only inflates a time, so a pass needs no confirmation.

    Parameters
    ----------
    suspects : list[CaseTiming]
        Tests to re-measure.
    template : str
        The ``--rerun`` command.
    budget : float
        The scaled per-test budget.

    Returns
    -------
    dict[str, float | None]
        Each suspect's time alone (``None``: unmeasurable).
    """
    solo = measure_all(suspects, template, default_recheck_workers())
    over = [c for c in suspects if (solo.get(c.test_id) or 0.0) > budget]
    for tid, again in measure_all(over, template, 1).items():
        first = solo.get(tid)
        if again is not None and first is not None:
            solo[tid] = min(first, again)
    return solo


def _report_shared(
    fastest: dict[tuple[str, str, str], float], budget: float, config: Path
) -> bool:
    """Print the slowest shared-fixture setups and any over their budget.

    Parameters
    ----------
    fastest : dict[tuple[str, str, str], float]
        Each shared fixture's fastest setup.
    budget : float
        The scaled shared-setup budget.
    config : Path
        The settings file, for the remedy.

    Returns
    -------
    bool
        Whether any shared fixture is over its budget.
    """
    if not fastest:
        return False
    ranked = sorted(fastest.items(), key=lambda kv: kv[1], reverse=True)
    print(
        "test-speed guard: slowest shared-fixture setups (fastest in the run): "
        + ", ".join(
            f"{name} [{scope}] {secs:.2f}s" for (name, scope, _), secs in ranked[:5]
        ),
        file=sys.stderr,
    )
    over = [row for row in ranked if row[1] > budget]
    if not over:
        return False
    print(
        f"test-speed guard: {len(over)} shared fixture(s) exceed the {budget:.2f}s "
        "shared-setup budget even on their fastest setup in the run:",
        file=sys.stderr,
    )
    for (name, scope, where), secs in over:
        print(
            f"  {secs:7.2f}s  {name}  ({scope}, defined in {where or '<root>'})",
            file=sys.stderr,
        )
    print(
        "\nA shared fixture is set up once per parallel worker. Make its setup "
        "cheaper or narrow what it builds; only then raise "
        f"test_speed.shared_setup_budget_seconds in {config}, with a reason.",
        file=sys.stderr,
    )
    return True


def _remedy(allowlist: Path) -> str:
    """Return the remedy text printed with the offenders, in its fixed order.

    Parameters
    ----------
    allowlist : Path
        The stack's allowlist file.

    Returns
    -------
    str
        The remedies: speed it up, move it, and only then allowlist it.
    """
    return (
        "\nFix each one, in this order:\n"
        "  1. Speed it up, usually by mocking the slow dependency (network,\n"
        "     subprocess, sleep, a heavy real object). Most unit-test overages\n"
        "     are an unmocked call.\n"
        "  2. Move it to a slower test group, out of the default suite.\n"
        f"  3. Only then allowlist it in {allowlist}: its test id, then\n"
        "     '# <reason>, <tracking issue>'.\n"
    )


def main(argv: list[str] | None = None) -> int:
    """Check per-test times against the budget; return a process exit code.

    Parameters
    ----------
    argv : list[str] | None
        Argument vector; ``None`` uses ``sys.argv``.

    Returns
    -------
    int
        0 within budget, 1 on an offender, 2 on bad input.
    """
    args = _build_parser().parse_args(argv)
    try:
        settings = runner_speed.load_settings(args.config)
        template = _check_template(args.rerun)
        timings = parse_junit(args.junit)
        allow = parse_allowlist(args.allowlist)
        # Calibrate first, alone: every re-measure below runs after it.
        scale = budget_factor(settings, args.factor)
    except (ValueError, SystemExit) as exc:
        print(f"test-speed guard: {exc}", file=sys.stderr)
        return 2

    budget = float(settings["budget_seconds"]) * scale
    shared_budget = float(settings["shared_setup_budget_seconds"]) * scale
    print(
        f"test-speed guard: budgets scaled by {scale:.2f}: {budget:.2f}s per test "
        f"(reference {settings['budget_seconds']:g}s), {shared_budget:.2f}s per "
        f"shared fixture (reference {settings['shared_setup_budget_seconds']:g}s).",
        file=sys.stderr,
    )

    # With --no-recheck there is no solo time, so only the budget itself applies.
    line = budget * float(settings["recheck_fraction"]) if args.recheck else budget
    suspects = sorted(
        (t for t in timings if t.charged > line and t.test_id not in allow),
        key=lambda t: t.charged,
        reverse=True,
    )
    over_budget = [t for t in suspects if t.charged > budget]
    solo: dict[str, float | None] = {}
    if args.recheck and suspects:
        print(
            f"test-speed guard: {len(over_budget)} test(s) over budget and "
            f"{len(suspects) - len(over_budget)} near it in the suite run; "
            "re-measuring each one alone (the suite number is wall-clock and moves "
            "with machine load)...",
            file=sys.stderr,
        )
        solo = _remeasure(suspects, template, budget)

    # (case, how it was judged). An unmeasurable test fails closed: the suite
    # number is the one we just declined to trust, so never silently absolve.
    offenders: list[tuple[CaseTiming, str]] = []
    dismissed: list[tuple[CaseTiming, float]] = []
    for case in suspects:
        if not args.recheck:
            offenders.append((case, "not re-measured (--no-recheck)"))
            continue
        alone = solo.get(case.test_id)
        if alone is None:
            print(
                f"  ? {case.test_id}: could not be re-measured alone; failing closed.",
                file=sys.stderr,
            )
            offenders.append((case, "alone: could not be measured"))
        elif alone > budget:
            offenders.append((case, f"alone {alone:.2f}s"))
        elif case.charged > budget:
            dismissed.append((case, alone))

    if dismissed:
        print(
            f"test-speed guard: {len(dismissed)} suite-run overage(s) were "
            "contention, not regressions (fast when run alone):",
            file=sys.stderr,
        )
        for case, alone in dismissed:
            print(
                f"  {case.charged:7.2f}s -> {alone:6.2f}s alone   {case.test_id}",
                file=sys.stderr,
            )

    # Advise pruning only from a run we trust: on a contended run (any test over
    # budget in the suite) allowlisted tests read slow and innocents read as
    # offenders, so "prune these" would delete entries the next busy run needs.
    fastest: dict[str, float] = {}
    for timing in timings:
        fastest[timing.test_id] = min(
            timing.charged, fastest.get(timing.test_id, timing.charged)
        )
    stale = sorted(
        tid for tid in allow if fastest.get(tid, 0.0) < budget * STALE_FRACTION
    )
    if stale and not over_budget:
        print(
            f"test-speed guard: warning: these allowlisted tests are fast now (or "
            f"gone); prune them from {args.allowlist}:",
            file=sys.stderr,
        )
        for tid in stale:
            print(f"  - {tid}", file=sys.stderr)

    shared_over = _report_shared(
        fastest_shared_setups(timings), shared_budget, args.config
    )

    if offenders:
        print(
            f"test-speed guard: {len(offenders)} test(s) fail the {budget:.2f}s "
            "per-test budget and are not allowlisted:",
            file=sys.stderr,
        )
        for case, how in offenders:
            rerun = shlex.join(rerun_argv(template, case, Path("alone-junit.xml")))
            print(
                f"  suite {case.charged:.2f}s, {how}  {case.test_id}\n"
                f"    re-run alone: {rerun}",
                file=sys.stderr,
            )
        print(_remedy(args.allowlist), file=sys.stderr)
    if offenders or shared_over:
        return 1

    print(
        f"test-speed guard: OK, all {len(timings)} tests within the {budget:.2f}s "
        f"budget ({len(allow)} allowlisted)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
