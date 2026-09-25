# Reviewer adapter notes: CodeRabbit (`coderabbit`)

Vendor quirks for the `coderabbit` entry in `.github/pr-reviewer.json`. Skill
prose names no vendor; read this file before interpreting a review when
`.active == "coderabbit"`.

> Items marked **(observed)** have been seen on real PRs; the rest come from
> CodeRabbit's docs. Confirm the unmarked ones (especially multi-finding and
> re-review-after-fix shapes) on a trial PR before making this adapter `active`.

## Installing and triggering

- Install the CodeRabbit GitHub App for the repo; per-repo behavior lives in
  `.coderabbit.yaml` at the repo root.
- Trigger: a top-level PR comment `@coderabbitai review` (`trigger_comment`) —
  an **incremental** review of commits since the last one. `@coderabbitai full
  review` re-reviews from scratch; use it only when an incremental review is stale
  or confused.
- Auto-review and auto-incremental-review on push are on by default
  (`reviews.auto_review` in `.coderabbit.yaml`); it pauses after
  `auto_pause_after_reviewed_commits` (default 5) reviewed commits — after that,
  the trigger comment is required even in "automatic" mode, so re-detect the
  trigger mode if reviews stop arriving.
- Commands must be **top-level** PR comments, not thread replies.

## Detecting a result

- **(observed)** Check name: `CodeRabbit` (a check run; legacy commit statuses are
  opt-in).
- **(observed)** Logins: `coderabbitai[bot]` on REST reviews/comments,
  `coderabbitai` as the GraphQL thread author — both listed in `bot_logins`.
- **(observed)** Findings: a review whose body starts
  `**Actionable comments posted: N**`, with `commit_id` = the reviewed head, plus N
  inline threads.
- **(observed)** **A clean incremental re-review posts an EMPTY-body review.** The
  clean verdict (`No actionable comments were generated in the recent review.`)
  lives only in CodeRabbit's **walkthrough issue-comment** (starts
  `<!-- This is an auto-generated comment: summarize by coderabbit.ai -->`), which
  it edits in place. Hence `summary_in_issue_comments: true`: judge that comment
  like a review, with recency = its `updated_at` (not `created_at`).
- **(observed)** Reviewed SHA in the walkthrough: `Reviewing files that changed from
  the base of the PR and between <base-sha> and <head-sha>.` (full SHAs) —
  `reviewed_sha_pattern` captures the second. A walkthrough still naming an older
  head does not vouch for the new one, so after a push the verdict is unknown until
  it re-reviews.
- No summary marker (`summary_marker: null`): a review or comment counts as the
  summary only when its body matches a verdict pattern, so empty/reply-style
  reviews can't shadow it.
- The walkthrough also carries pre-merge "checks" warnings (e.g. out-of-scope
  changes) — those are not inline findings and don't affect the verdict; read them
  as ordinary PR feedback.

## Threads

- **(observed)** `self_resolves_threads: true` — CodeRabbit resolves its own thread
  once a later push addresses it. `resolve-pr-concerns` Step 4d still replies with
  the fix SHA; if the bot already resolved the thread, don't re-open it.
- **Never post `@coderabbitai resolve` or `@coderabbitai approve`.** They resolve
  EVERY CodeRabbit thread on the PR at once, including ones nobody has addressed.
  Close threads one at a time, per Step 4d.

## Config

- Repo config: `.coderabbit.yaml`. When switching to this adapter, port the noise
  filters from the previous reviewer's config (e.g. `.cursor/BUGBOT.md`) into it.
