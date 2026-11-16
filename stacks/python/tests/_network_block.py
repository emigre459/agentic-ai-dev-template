"""Fail a default-lane test that connects to a non-loopback address.

Default-lane tests must not reach the network: a connect to a real host makes a
test slow, flaky and dependent on the machine it runs on. ``pytest-socket`` does
the blocking; this module makes the block hard to hide and keeps opt-in lanes free.

- ``NetworkBlocked`` is the error a blocked connect raises. It derives from
  ``pytest.fail.Exception`` (a ``BaseException``), so ``except Exception:`` and
  retry loops cannot swallow or retry it, and the test still fails naming the
  host. ``pytest-socket`` raises ``RuntimeError`` subclasses, which a broad
  ``except Exception`` would hide.
- ``install()`` rebinds ``pytest_socket.SocketConnectBlockedError`` and
  ``pytest_socket.SocketBlockedError`` to it (``pytest-socket`` looks both up as
  module globals when it raises). It also wraps ``socket.socket.connect_ex`` once,
  because ``pytest-socket`` guards only ``connect``: while the guard is active the
  wrapper routes through the guarded ``connect`` and returns 0 or the errno.
- ``exempt_opt_in_lanes`` marks every test carrying an opt-in marker
  (``integration``, ``e2e``) with ``enable_socket``, so only default-lane tests
  are guarded.

Loopback and Unix sockets are allowed (``--allow-hosts`` and
``--allow-unix-socket`` in ``pyproject.toml``'s ``addopts``). To use a real host,
mock it, move the test to an opt-in lane, or mark it ``enable_socket`` with a reason.

Limits:

- A connect from a plain background thread raises ``NetworkBlocked`` in that
  thread; a ``filterwarnings`` entry in ``pyproject.toml`` promotes pytest's
  unhandled-thread-exception warning to an error when it names ``NetworkBlocked``.
  A ``ThreadPoolExecutor`` worker stores the error on its future instead: a future
  the test never reads (``result()`` / ``exception()``) leaves only a warning.
- ``pytest-socket`` restores the real ``connect`` at each test's teardown, so
  connects made between tests (collection, a session fixture) are not checked.
"""

from __future__ import annotations

import errno
import socket
import warnings
from typing import Any

import pytest
import pytest_socket

_OPT_IN_MARKERS = frozenset({"integration", "e2e"})
_HINT = (
    "Mock it, move the test to an opt-in lane, or mark it enable_socket with a reason."
)


class NetworkBlocked(pytest.fail.Exception):  # type: ignore[name-defined]
    """A default-lane test tried to reach a non-loopback address.

    Built as ``NetworkBlocked(allowed, host)`` (a blocked connect) or with no
    arguments (``pytest-socket``'s ``--disable-socket`` mode raises it bare).

    Parameters
    ----------
    allowed : list[str] | str
        The allowed hosts, or a ready-made message (defaults to a generic one).
    host : str | None
        The host the test tried to connect to.
    *args : Any
        Ignored; accepted for ``pytest-socket`` compatibility.
    **kwargs : Any
        Ignored; accepted for ``pytest-socket`` compatibility.
    """

    def __init__(
        self,
        allowed: list[str] | str = f"A test tried to use the network. {_HINT}",
        host: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Build the message, warn once, and initialise the failure."""
        if isinstance(allowed, str):
            msg = allowed
        else:
            msg = (
                f'A test tried to connect to "{host}" '
                f'(allowed: "{",".join(allowed)}"). {_HINT}'
            )
        # The warning surfaces swallowed blocks in the summary. Under -W error it
        # would raise instead of the block, so drop it rather than replace the block.
        try:
            warnings.warn(msg, stacklevel=2)
        except Warning:
            pass
        super().__init__(msg, pytrace=True)


def _install_connect_ex() -> None:
    """Make ``socket.socket.connect_ex`` obey the guarded ``connect`` (once)."""
    if getattr(socket.socket.connect_ex, "_network_block", False):
        return
    real_connect_ex = socket.socket.connect_ex

    def connect_ex(self: socket.socket, address: Any) -> int:
        if socket.socket.connect is pytest_socket._true_connect:
            return real_connect_ex(self, address)  # type: ignore[no-any-return]
        try:
            socket.socket.connect(self, address)
        except OSError as exc:  # NetworkBlocked is a BaseException and propagates
            # A socket timeout carries no errno; native connect_ex reports EWOULDBLOCK.
            return exc.errno if exc.errno is not None else errno.EWOULDBLOCK
        return 0

    connect_ex._network_block = True  # type: ignore[attr-defined]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]


def install() -> None:
    """Rebind ``pytest-socket``'s error names to ``NetworkBlocked``; guard ``connect_ex``."""
    _install_connect_ex()
    pytest_socket.SocketConnectBlockedError = NetworkBlocked  # type: ignore[misc,assignment]
    pytest_socket.SocketBlockedError = NetworkBlocked  # type: ignore[misc,assignment]


def exempt_opt_in_lanes(items: list[pytest.Item]) -> None:
    """Mark every opt-in-lane test ``enable_socket`` so it keeps real network access.

    Parameters
    ----------
    items : list[pytest.Item]
        The collected tests.
    """
    for item in items:
        if any(marker.name in _OPT_IN_MARKERS for marker in item.iter_markers()):
            item.add_marker(pytest.mark.enable_socket)
