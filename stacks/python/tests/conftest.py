"""Shared pytest setup for this test suite.

Re-exports the fixture-timing hooks (``_pytest_fixture_timing.py``) so they load
on every run, the speed guard's solo re-run included: that re-run clears
``addopts``, so registering the plugin there (``-p``) would drop it.

Also installs the network block (``_network_block.py``) and exempts opt-in lanes.
"""

import pytest
from _network_block import exempt_opt_in_lanes, install
from _pytest_fixture_timing import pytest_fixture_setup, pytest_runtest_protocol

__all__ = ["pytest_fixture_setup", "pytest_runtest_protocol"]


def pytest_configure(config: pytest.Config) -> None:
    """Rebind pytest-socket's errors so a blocked connect cannot be swallowed."""
    install()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Let opt-in-lane tests (integration, e2e) keep network access."""
    exempt_opt_in_lanes(items)
