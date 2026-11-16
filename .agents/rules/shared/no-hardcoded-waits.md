---
description: Wait on the real event or state, never on a fixed sleep; raw time is only the bounding timeout, except deliberate pacing through one named helper.
alwaysApply: true
---

# No hard-coded waits: trigger on events, not timing

Everything must wait on a state change, event or post-condition, not on a clock. A
raw time value is acceptable only as the `timeout` that bounds a wait-for-event,
never as the mechanism itself.

**Why:** a fixed `sleep(N)` "settle" is tuned to one machine or server and silently
breaks on a slower or busier one. The same code passes locally and fails in CI, and
the only difference was the hard-coded wait.

## How to apply

- Replace sleep-and-hope with a wait on what you actually need: the element is
  present, the response arrived, the process printed its readiness line, the
  `Event` is set, the file exists, the port accepts connections. Pass the time as
  the timeout of that wait.
- Browser and UI code (TypeScript or Python): use the framework's auto-waiting and
  condition waits (`expect(...)`, `wait_for_*` on a selector, URL or function), not
  a fixed `waitForTimeout` / `wait_for_timeout`. In unit tests use fake timers.
- Polling a system that gives no push signal is a retry/poll, which belongs in the
  one project retry module (see `../python/retries-through-tenacity.md`), not a
  hand-written loop around `sleep`.
- **The one legitimate raw-time exception is deliberate pacing** (rate limiting,
  politeness delays, jitter): an intentional throttle, not a wait for something to
  happen. Route it through one named helper (`pause()`, `Pacer`) so it is visible
  and greppable, distinct from a settle.
- When auditing, grep `sleep(` and `waitForTimeout` / `wait_for_timeout` and
  classify each: fixed settle (convert to a wait on the condition), pacing (the
  named helper), retry/poll (the retry module), or already a bounding timeout (fine).

## Enforcement

Reviewer-enforced. A project may add a repo-scan test that fails on raw `sleep`
outside its pacing/retry module.
