---
description: Default-lane tests cannot reach the network (non-loopback connects fail the test); mock it, move the test to an opt-in lane, or mark it enable_socket with a reason.
appliesTo: "**/tests/**/*.py"
---

# Default-lane tests don't reach the network

A test that silently reaches a real host is slow, flaky and depends on the machine
it runs on. Worse, the failure is often swallowed: a broad `except Exception` or a
retry loop turns "the network was unreachable" into a pass or a long delay.

In the default lane (`make tests`, `make coverage`) a connect to anything but
loopback or a Unix socket fails the test with `NetworkBlocked`, naming the host. It
is a `BaseException`, so `except Exception:` and retry loops cannot swallow or retry
it. When a test needs a remote:

1. **Mock it** (preferred), or run a local server on loopback.
2. Move the test to an opt-in lane (`integration`, `e2e`): those tests keep network
   access automatically.
3. Mark a single test `@pytest.mark.enable_socket` and say why in a comment.

## Enforcement

`pytest-socket` plus `stacks/python/tests/_network_block.py`, wired in
`tests/conftest.py` and the `addopts` / `filterwarnings` in `pyproject.toml`, so CI
fails on a violation. The module docstring lists the limits (an unread executor
future, connects between tests). Adding `enable_socket` without a reason is
reviewer-enforced.
