"""The network block fails default-lane tests that reach a non-loopback host.

Behaviour is checked by running tiny suites in-process with ``pytester``, each
with a conftest that installs the block the way ``tests/conftest.py`` does and
the same ``--allow-hosts`` / ``--allow-unix-socket`` flags as ``pyproject.toml``.
192.0.2.1 is TEST-NET-1 (RFC 5737): never routable, and the block fires before any
packet is sent.
"""

import _network_block
import pytest
import pytest_socket

pytest_plugins = ("pytester",)

_CONFTEST = """
import pytest
import _network_block as nb

def pytest_configure(config):
    # The enclosing test is itself guarded; lift its guard so this session starts clean.
    import pytest_socket
    pytest_socket._remove_restrictions()
    nb.install()

@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    nb.exempt_opt_in_lanes(items)
"""

_INI = """
[pytest]
markers =
    integration: live tests
filterwarnings =
    error:(?s).*NetworkBlocked:pytest.PytestUnhandledThreadExceptionWarning
"""

_FLAGS = ("--allow-hosts=127.0.0.0/8,::1,localhost", "--allow-unix-socket")


def _run(pytester: pytest.Pytester, body: str, *args: str) -> pytest.RunResult:
    """Run ``body`` as an inner test file with the block installed."""
    pytester.makeini(_INI)
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(test_inner=body)
    return pytester.runpytest_inprocess("-p", "no:cacheprovider", "-n0", *_FLAGS, *args)


def test_blocked_connect_fails_naming_block_and_address(
    pytester: pytest.Pytester,
) -> None:
    """A non-loopback connect fails the test and names NetworkBlocked and the host."""
    result = _run(
        pytester,
        """
import socket

def test_reaches_out():
    socket.create_connection(("192.0.2.1", 80), timeout=1)
""",
    )
    result.assert_outcomes(failed=1)
    out = result.stdout.str()
    assert "NetworkBlocked" in out
    assert "192.0.2.1" in out


def test_blocked_connect_escapes_except_exception(pytester: pytest.Pytester) -> None:
    """``except Exception`` cannot swallow the block."""
    result = _run(
        pytester,
        """
import socket

def test_swallows():
    try:
        socket.create_connection(("192.0.2.1", 80), timeout=1)
    except Exception:
        pass
""",
    )
    result.assert_outcomes(failed=1)
    assert "NetworkBlocked" in result.stdout.str()


def test_loopback_connect_passes(pytester: pytest.Pytester) -> None:
    """Loopback stays reachable, so tests can run local servers."""
    result = _run(
        pytester,
        """
import socket

def test_loopback():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        with socket.create_connection(server.getsockname(), timeout=5):
            pass
""",
    )
    result.assert_outcomes(passed=1)


_UNGUARDED = """
import socket

import pytest
import pytest_socket

{decorator}
def test_unguarded():
    assert socket.socket.connect is pytest_socket._true_connect
"""


def test_opt_in_marked_test_is_not_guarded(pytester: pytest.Pytester) -> None:
    """A test carrying an opt-in marker keeps real network access."""
    result = _run(
        pytester,
        _UNGUARDED.format(decorator="@pytest.mark.integration"),
        "-m",
        "integration",
    )
    result.assert_outcomes(passed=1)


def test_enable_socket_marker_exempts(pytester: pytest.Pytester) -> None:
    """``enable_socket`` lifts the guard for one test."""
    result = _run(pytester, _UNGUARDED.format(decorator="@pytest.mark.enable_socket"))
    result.assert_outcomes(passed=1)


def test_block_is_base_exception_not_exception() -> None:
    """The block must not be an ``Exception``, so retry loops cannot retry it."""
    _network_block.install()
    blocked = pytest_socket.SocketConnectBlockedError
    assert issubclass(blocked, BaseException)
    assert not issubclass(blocked, Exception)
    assert pytest_socket.SocketBlockedError is _network_block.NetworkBlocked


def test_blocked_connect_ex_fails_naming_address(pytester: pytest.Pytester) -> None:
    """``connect_ex`` is guarded too (``pytest-socket`` only guards ``connect``)."""
    result = _run(
        pytester,
        """
import socket

def test_connect_ex_reaches_out():
    with socket.socket() as s:
        s.settimeout(1)
        s.connect_ex(("192.0.2.1", 80))
""",
    )
    result.assert_outcomes(failed=1)
    assert "192.0.2.1" in result.stdout.str()


def test_block_survives_warnings_as_errors(pytester: pytest.Pytester) -> None:
    """A warnings-as-errors filter cannot replace the block with a UserWarning."""
    result = _run(
        pytester,
        """
import socket

def test_swallows_under_warnings_as_errors():
    try:
        socket.create_connection(("192.0.2.1", 80), timeout=1)
    except Exception:
        pass
""",
        "-W",
        "error::UserWarning",
    )
    result.assert_outcomes(failed=1)
    assert "NetworkBlocked" in result.stdout.str()
