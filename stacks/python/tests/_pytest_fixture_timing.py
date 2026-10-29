"""Record shared-fixture setup time on each test's JUnit record.

The speed guard (``scripts/check_test_speed.py``) reads JUnit times, which pytest
reports as setup + call + teardown. A session/package/module/class-scoped fixture
is set up once, inside whichever test first requests it, so that one test's time
carries a cost the whole suite shares (booting a local server, building a large
in-memory dataset), and the guard would fail that test for it.

This plugin times every fixture setup and, for a shared scope, appends a
``shared_fixture_setup`` user property (``"name|scope|seconds|where"``) to the
test that paid it. ``seconds`` is the fixture's OWN setup: a shared fixture that
requests another shared fixture during its setup excludes that nested setup,
which is recorded separately, so the guard never subtracts it twice. ``where``
is the fixture's definition location (its node-id prefix), so two modules'
same-named fixtures are judged separately. JUnit writes user properties into the
XML (xdist carries them from the workers), so the guard can subtract shared
setup from a test's time and judge it against its own budget. Function-scoped
setup is not recorded: every test pays it, so it stays in the per-test number.

Shared teardown is not attributed (it runs in the last test's teardown and is
usually small); a fixture slow to tear down still shows on that test.

Registered for every run, including the guard's solo re-run (which clears
``addopts``), through the hook re-exports in ``tests/conftest.py``.
"""

from __future__ import annotations

import time
from collections.abc import Generator

import pytest

#: The JUnit user-property name the speed guard reads.
SHARED_FIXTURE_PROPERTY = "shared_fixture_setup"

_SHARED_SCOPES = frozenset({"session", "package", "module", "class"})

#: Shared setups faster than this are not recorded: they cannot move a test's
#: number, and pytest's own session fixtures would otherwise fill the XML.
_MIN_RECORDED_SECONDS = 0.01

#: Per in-progress fixture setup, the time spent in setups nested inside it.
_nested: list[float] = []

#: The test whose protocol is running, so a shared fixture's setup (whose own
#: ``request.node`` is the session or module) is charged to the test that paid it.
_running: list[pytest.Item] = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Generator[None]:
    """Track the running test for the length of its setup, call and teardown.

    Parameters
    ----------
    item : pytest.Item
        The test about to run.
    nextitem : pytest.Item | None
        The next test, unused.

    Yields
    ------
    None
        Control to pytest's own protocol.
    """
    _running.append(item)
    try:
        yield
    finally:
        _running.pop()


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(
    fixturedef: pytest.FixtureDef[object], request: pytest.FixtureRequest
) -> Generator[None]:
    """Time one fixture setup; record it on the running test if its scope is shared.

    Parameters
    ----------
    fixturedef : pytest.FixtureDef[object]
        The fixture being set up.
    request : pytest.FixtureRequest
        The requesting context, unused.

    Yields
    ------
    None
        Control to the fixture's own setup.
    """
    _nested.append(0.0)
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    own = elapsed - _nested.pop()
    if _nested:
        _nested[-1] += elapsed
    if fixturedef.scope in _SHARED_SCOPES and own >= _MIN_RECORDED_SECONDS and _running:
        where = fixturedef.baseid
        value = f"{fixturedef.argname}|{fixturedef.scope}|{own:.3f}|{where}"
        _running[-1].user_properties.append((SHARED_FIXTURE_PROPERTY, value))
