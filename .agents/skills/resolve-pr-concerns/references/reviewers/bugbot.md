# Reviewer adapter notes: Cursor Bugbot (`bugbot`)

Vendor quirks for the `bugbot` entry in `.github/pr-reviewer.json`. Skill prose
names no vendor; read this file before interpreting a review when
`.active == "bugbot"`. Config fields (check name, trigger, logins, marker,
verdict patterns) live in the config — this file covers what a field can't say.

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

- `self_resolves_threads: false` — Bugbot does not resolve its own threads once a
  later push addresses them; `resolve-pr-concerns` Step 4d closes them.

## Config

- Noise filters: `.cursor/BUGBOT.md` (categories of finding to skip).
