---
description: How the configured PR reviewer (.github/pr-reviewer.json) is triggered on PRs, and how that interacts with the explicit trigger comment in resolve-pr-concerns — detect the trigger mode, post the literal trigger comment only when it's needed, then its final-review comment once. The review gate is the vendor-neutral review-gate status, never the vendor's own check.
alwaysApply: true
---

# Triggering the configured PR reviewer

The automated PR reviewer is whichever one `.github/pr-reviewer.json` names as
`active`; the config declares its `check_name`, `trigger_comment`, and `bot_logins`.
Swapping reviewers is a config edit — never hardcode a vendor in skills or rules.
Vendor quirks live in
`.agents/skills/resolve-pr-concerns/references/reviewers/<active>.md`.

The reviewer's trigger behavior is usually a **per-repo setting on the vendor's
side**, not something this repo controls:

- **Automatic mode** — the reviewer reviews on every PR create/update (push).
- **Manual mode** — it reviews only when someone posts its `trigger_comment` on the PR.
  Some reviewers also fall back to manual after N reviewed commits (the notes file
  says which).

The reviewer must also be **installed** (its GitHub App) and **enabled** for the repo
before it reviews at all. A fresh repo created from this template has no automated
review until someone installs + enables it.

**How to apply:** Detect the mode once per PR (see `resolve-pr-concerns` Step 1a)
and act accordingly:

- **Automatic mode:** the push already triggered a fresh review. **Do NOT post the
  trigger comment** — it's a redundant double-trigger that just clutters the PR.
  Wait for the auto-review.
- **Manual mode:** post the trigger comment after pushing fixes, since nothing else
  will trigger a re-review. Read the literal value with
  `jq -r '.reviewers[.active].trigger_comment' .github/pr-reviewer.json`, then post
  exactly that string (`gh pr comment <num> --body "<trigger_comment>"`) — a literal
  body matches the `.claude/settings.json` allow-list entry; a `$(...)` substitution
  doesn't.

Don't blindly post the trigger comment "to be safe" — on an auto-on-push repo that
double-triggers every round. Confirm the mode first, then skip or send accordingly.

**Then the final full review, once, in either mode.** If the adapter's
`final_review_comment` is non-null, post it once every finding on the head is
fixed or answered (verdict `pass`, or `findings` you have replied to), and never
again for coverage — no push triggers it. On a failure, wait 20+ minutes and post it once
more, then escalate. `reviewer_state.py` reports `final_review_outcome`;
`resolve-pr-concerns` Step 5a has the details.

**What counts as reviewed.** The reviewer's own `check_name` status is not evidence:
it can pass on a head it skipped or was rate-limited on. The vendor-neutral
`review-gate` status (posted by `.github/workflows/review-gate.yml`) is green only
when the reviewer has reviewed the current head **and** finished its final full
review. Require `review-gate` in the `main` ruleset once a reviewer is installed —
never the vendor's check.
