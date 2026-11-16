---
description: Retries and polls go through tenacity configured in ONE project module (shared never-retry and retry-on lists); no hand-written retry or poll loops. Routine retries log at DEBUG; giving up is loud.
appliesTo: "**/*.py"
---

# Retries go through one tenacity module

Scattered `for attempt in range(3)` loops and per-call `try/except` retries each
invent their own rules: one retries a `TypeError`, one hammers a 400, one never
backs off. Put the policy in **one project module** built on
[tenacity](https://tenacity.readthedocs.io/) and have every retry or poll call
site use it. Changing what is retried then means editing that module's lists, not a
loop at a call site. No hand-written retry or poll loops.

## The shape of that module

- **A never-retry list**: programming errors (`TypeError`, `ValueError`,
  `AssertionError`, ...) and the module's own "gave up polling" timeout. Retrying
  these repeats the same bug behind a delay. Anything that is a `BaseException` but
  not an `Exception` (`KeyboardInterrupt`, `asyncio.CancelledError`, a test-lane
  `NetworkBlocked`) is never retried, structurally, and no per-call option
  overrides that.
- **A retry-on list**: connection, timeout and transport errors (reset, refused,
  `TimeoutError`, your HTTP client's transport errors). Not the rest of `OSError`
  (`FileNotFoundError`, `PermissionError` are not transient).
- **HTTP statuses** are read from the exception chain: 408, 429 and every 5xx
  retry; any other status does not, whatever the exception type.
- **Per-call overrides** (attempts, wait, extra types) are arguments to the
  helper, not a new loop.
- **Honour `Retry-After`** on 429/503, with a give-up cap so a hostile or buggy
  server cannot park the caller for an hour.
- A site that must retry an **exact set of types regardless of HTTP status** passes
  a predicate, not the type list.
- Sync and async entry points, plus a **poll-until-condition** helper for remote
  state with no push signal, and a named pacing helper (see
  `../shared/no-hardcoded-waits.md`).

## Logging

A retry that recovers is normal operation: log it at DEBUG (see "Log levels" in
`python-best-practices.md`). Giving up is the failure: raise, and log it at
WARNING or ERROR with the cause so the operator can act.

## Enforcement

Reviewer-enforced. A project may add a repo-scan test that fails on a `tenacity`
import or raw `sleep` outside that one module.
