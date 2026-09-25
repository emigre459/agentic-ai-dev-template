---
description: How the configured PR reviewer (.github/pr-reviewer.json) is triggered on PRs, and how that interacts with the explicit trigger comment in resolve-pr-concerns — detect the trigger mode, then post the literal trigger comment only when it's needed.
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
