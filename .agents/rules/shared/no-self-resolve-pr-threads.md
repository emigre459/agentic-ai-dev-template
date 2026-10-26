---
description: Never resolve PR review threads (reviewer bot, Copilot or human) or merge a PR on your own. Reply with the fix + commit SHA, then STOP and let a human (or a reviewer bot that clears its own threads) resolve.
alwaysApply: true
---

# Never self-resolve PR threads or self-merge

Do NOT resolve PR review threads or merge a PR on your own. Replying on the thread
with the fix and its commit SHA (or the reasoning for a pushback) is the agent's job;
calling `resolveReviewThread`, clicking Resolve, or posting a reviewer's resolve-all /
approve command is not, unless the user explicitly says so for that PR.

**Why:** thread resolution is a human sign-off, and with a ruleset that requires
conversation resolution it is the final gate before merge. Self-resolving takes that
judgment away from the reviewer and can flip a PR to mergeable before anyone has
checked the fix.

**How to apply**
- `resolve-pr-concerns` Step 4d (reviewer-bot threads) and Step 4e (a human's threads):
  reply, then leave the thread open.
- A reviewer whose adapter sets `self_resolves_threads: true` in
  `.github/pr-reviewer.json` clears its own addressed threads on re-review. That is the
  bot's call, not yours, so never prompt it with a resolve command.
- Same for merging: drive the PR to merge-ready, report it, and leave the merge to a
  human.
