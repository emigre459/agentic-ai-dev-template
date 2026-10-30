---
description: How the configured PR reviewer (.github/pr-reviewer.json) is triggered on PRs, and how that interacts with the explicit trigger comment in resolve-pr-concerns — detect the trigger mode, post the literal trigger comment only when it's needed (or, for a reviewer with a local review, run local_review.py instead), then its final-review comment once; wait out a refused request and re-post it unchanged. The review gate is the vendor-neutral review-gate status, never the vendor's own check.
alwaysApply: true
---

# Triggering the configured PR reviewer

The automated PR reviewer is whichever one `.github/pr-reviewer.json` names as
`active`; the config declares its `check_name`, `trigger_comment`, and `bot_logins`.
Swapping reviewers is a config edit — never hardcode a vendor in skills or rules.
**`"active": null` means the repo has no automated reviewer** (no subscription, or
not installed): post no trigger or final-review comment, run no local review, and
don't wait for a review — `resolve-pr-concerns` falls back to its Step 1b pre-pass.
Vendor quirks live in
`.agents/skills/resolve-pr-concerns/references/reviewers/<active>.md` and, for a
reviewer the repo set up itself, in the repo-owned file its adapter's `notes` names
(read both when both exist).

The reviewer's trigger behavior is usually a **per-repo setting on the vendor's
side**, not something this repo controls:

- **Automatic mode** — the reviewer reviews on every PR create/update (push).
- **Manual mode** — it reviews only when someone posts its `trigger_comment` on the PR.
  Some reviewers also fall back to manual after N reviewed commits (the notes file
  says which).

The reviewer must also be **installed** (its GitHub App) and **enabled** for the repo
before it reviews at all. A fresh repo created from this template has no automated
review until someone installs + enables it **and** sets `active` to its adapter key
(the template ships with `"active": null`).

**How to apply:** after every push, watch about 30 seconds, and at least until the
new head's first commit status appears (the reviewer state dates the head's arrival by
it, so a trigger posted earlier can read as "not on this head"). Then:

- **A review started** (verdict leaves `none`, `review_running`, or an in-progress
  signal): auto-run is on. **Do NOT post the trigger comment** — it's a redundant
  double-trigger that just clutters the PR. Wait for the review. The reviewer's check
  merely registering is not evidence one started; it can register as "skipped".
- **No review started:** post the trigger comment only if `round_budget.trigger_allowed`
  is `true`. Read the literal value with
  `jq -r '.reviewers[.active].trigger_comment' .github/pr-reviewer.json`, then post
  exactly that string (`gh pr comment <num> --body "<trigger_comment>"`) — a literal
  body matches the `.claude/settings.json` allow-list entry; a `$(...)` substitution
  doesn't.
- **Comment mode is sticky.** Once you have had to comment on a PR, stay in comment
  mode for the rest of it: every later push, watch, confirm nothing started, then post
  the trigger (budget permitting).

Don't blindly post the trigger comment "to be safe" — on an auto-on-push repo that
double-triggers every round. Confirm nothing started first, then send.

**Unless the reviewer reviews locally.** When the adapter declares `local_review`
(`reviewer_state.py` reports `local_review.configured: true`), never post the
trigger: run each fix round with the resolve-pr-concerns skill's `local_review.py`
(it checks the local allowance with `review_preflight.py`, then records the round on
the PR), and the PR's one review request is the final full review
(`resolve-pr-concerns`, "Local review rounds"). The PR quota is small, so each extra
request on a PR costs real capacity.

**A refused request is a wait, never an escalation.** When `reviewer_state.py`
reports `rate_limit.refused`, the request never ran: wait until
`rate_limit.retry_after` and re-post the SAME request (`rate_limit.request`). Never
answer a refused incremental review with a full review, and never re-post early. No
local check can see the PR quota (`review_preflight.py` reads the local review's
separate allowance), so PR requests are never preflighted
(`resolve-pr-concerns`, "Review requests and the rate limit").

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
review (for a reviewer with a local review: when its final full review finished and
every reviewer thread is resolved, since later fixes are re-checked locally). Require `review-gate` in the `main` ruleset once a reviewer is installed —
never the vendor's check.
