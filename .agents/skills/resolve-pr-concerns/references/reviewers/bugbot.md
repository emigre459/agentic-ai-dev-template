# Reviewer adapter notes: Cursor Bugbot (`bugbot`)

Vendor quirks for the `bugbot` entry in `.github/pr-reviewer.json`. Skill prose
names no vendor; read this file before interpreting a review when
`.active == "bugbot"`. Config fields (check name, trigger, logins, marker,
verdict patterns) live in the config — this file covers what a field can't say.

> **Before switching to (or back to) Bugbot, read this caveat:** require the
> vendor-neutral `review-gate` status in the `main` ruleset (it follows `active`, so
> nothing else needs to change), and **never make the `Cursor Bugbot` check itself
> required.** Bugbot can't post its check on merge-group commits, so a required
> Bugbot check wedges a merge queue; keeping it required would mean dropping the
> merge queue and requiring up-to-date branches instead, which brings back manual
> catch-up merges. Also restore the noise filters: copy
> `.github/review-guidelines.md` to `.cursor/BUGBOT.md` (see "Config" below).
>
> **No final full review.** Bugbot's scope (incremental vs comprehensive) is set in
> the Bugbot admin UI, and no comment asks for a full review, so its five
> final-review fields are `null` and the gate requires none.

## Installing and triggering

- Bugbot reviews nothing until it is **installed** (the Cursor GitHub App,
  https://github.com/apps/cursor/installations) and **enabled** for the repo on the
  Cursor side (https://cursor.com/dashboard/bugbot/installation). A repo created
  from this template starts with neither.
- Trigger mode is a **per-repo Cursor dashboard setting**, not something the repo
  controls:
  - **Automatic** — reviews on every PR create/update (push).
  - **Manual** — reviews only when someone comments `bugbot run` (the config's
    `trigger_comment`; `cursor review` also works) on the PR.
  Teams sometimes switch automatic mode off to control usage-based cost, so detect
  the mode per PR (`resolve-pr-concerns` Step 1a) rather than assuming it.
- Reviews typically land within a couple of minutes of the trigger.

## Detecting a result — do NOT trust the status check alone

- The verdict and findings live in the **review** + **inline review comments**,
  decoupled from the `Cursor Bugbot` **status check**. The check can read
  `skipping` / `neutral` / `pending` while a complete review with findings exists
  (observed in practice). A green check is not proof you've read its findings.
- **Author logins vary.** The summary review has been observed authored by both
  `cursor` and `cursor[bot]`, and inline finding threads likewise. Filtering on one
  login can report "never reviewed" for a clean review and hide a real finding.
  `bot_logins` lists both; key on the summary marker.
- **Summary marker:** `<!-- BUGBOT_REVIEW -->`. Verdict: `found no new issues` =
  pass; `found N potential issue(s)` = N inline findings to enumerate.
- **Findings are NEVER in issue-comments** — the PR's issue-comments hold only the
  `bugbot run` trigger comments.
- **`commit_id` is `null` on `gh pr view --json reviews`**, so a monitor filtering
  that output by head SHA never matches. Identify a fresh review by author +
  `submittedAt` newer than your trigger + the marker; the reviewed SHA is in the
  body's `Reviewed by Cursor Bugbot for commit <sha>` footer. The REST
  `repos/<owner>/<repo>/pulls/<n>/reviews` endpoint (which `wait_for_pr_checks.sh`
  reads) does carry `commit_id`, which is why the adapter's `reviewed_sha_pattern`
  is `null`.
- **Re-anchored findings:** a prior finding re-posted on a new head keeps its
  `BUGBOT_BUG_ID` while the footer references an older SHA — that is the
  already-addressed finding, not a new one.

## Threads

- `self_resolves_threads: true` — Bugbot resolves its own addressed threads once a
  later push fixes them (observed on real PRs). `resolve-pr-concerns` Step 4d still
  replies with the fix SHA; if the bot already resolved the thread, don't re-open it.

## Config

- Noise filters: Bugbot reads `.cursor/BUGBOT.md`, which this template does not
  ship (it keeps no vendor directory). The filters live in the vendor-neutral
  `.github/review-guidelines.md`; to wire them in, restore them as
  `.cursor/BUGBOT.md` (a copy of that file).
