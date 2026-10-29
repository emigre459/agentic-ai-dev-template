"""Shared pytest setup for this test suite.

Re-exports the fixture-timing hooks (``_pytest_fixture_timing.py``) so they load
on every run, the speed guard's solo re-run included: that re-run clears
``addopts``, so registering the plugin there (``-p``) would drop it.
"""

from _pytest_fixture_timing import pytest_fixture_setup, pytest_runtest_protocol

__all__ = ["pytest_fixture_setup", "pytest_runtest_protocol"]
