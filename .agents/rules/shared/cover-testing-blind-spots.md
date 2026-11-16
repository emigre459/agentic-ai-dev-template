---
description: When you notice a path that neither the tests nor the live test kit exercise, cover it before calling the work done; never just mention it as a caveat.
alwaysApply: true
---

# Cover every testing blind spot you find

When you notice that something the change relies on is exercised by **neither** the
automated tests **nor** the live test kit or runbook, close that gap in the same
change: add a test, or, when only a live run can prove it, add a step to the kit or
runbook with its expected output and what a failure means. Don't leave it as a
caveat in a summary or a PR comment.

**Why:** a gap that is only mentioned gets read once and forgotten, and the path ships
untested. The usual cause is a test setup that differs from how the code really
runs: a runbook that copies actions locally while consumers call them remotely, or a
fake that stands in for the real CLI. A failure there reaches users first.

**How to apply**
- Name the gap concretely: which path, why the current tests and kit miss it.
- Prefer an automated test. Use a kit or runbook step only when the path needs real
  infrastructure (GitHub Actions, a scratch repo, the network), and mark the step as
  required, not optional.
- Guard the new step with a test where you can (for example, a test that the runbook
  still contains it), so later edits can't drop it silently.
- If closing the gap needs something you can't do (a reviewer-run step, credentials),
  add the step anyway and say who runs it.
