"""Shared pytest setup for this test suite.

Installs the network block (``_network_block.py``) and exempts opt-in lanes.
"""

import pytest
from _network_block import exempt_opt_in_lanes, install


def pytest_configure(config: pytest.Config) -> None:
    """Rebind pytest-socket's errors so a blocked connect cannot be swallowed."""
    install()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Let opt-in-lane tests (integration, e2e) keep network access."""
    exempt_opt_in_lanes(items)
