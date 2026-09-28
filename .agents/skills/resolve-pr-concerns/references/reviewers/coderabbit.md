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
  an **incremental** review of commits since the last one.
  Reference: https://docs.coderabbit.ai/reference/review-commands
- **Final full review** (`final_review_comment`): once incremental reviews are
  clean, post `@coderabbitai full review` **once** per PR. It re-reviews the whole
  diff, and on a CodeRabbit trial it found the most important bug of the run. Fix
  its findings and re-validate them incrementally; never post a second one for
  coverage (repeated full reviews don't converge, they just sample more, and each
  spends quota).
- **(observed)** **How a full review ends.** CodeRabbit replies to each command with
  a comment starting `<!-- This is an auto-generated reply by CodeRabbit -->`
  (`command_reply_marker`) and edits it in place when the review ends. An
  incremental command's reply carries the same marker, so `reviewer_state.py` reads
  every reply between a full-review request and the next one, and the failed /
  partial patterns skip replies that carry the incremental note ("incremental
  review system"):

  | Reply | Outcome |
  |---|---|
  | `✅ Action performed` / `Full review finished.` | `finished` |
  | `⚠️ Action not completed` / `Deferred architecture/priority summary could not be published.` | `finished` if a CodeRabbit review posted between the request and the reply's last edit (the findings landed; only the summary failed), else `pending` |
  | `❌ Action failed` / `Review failed.` | `failed` (no review ran; the reply was edited seconds after it was created) |
  | `⚠️ Action not completed` / `Head commit changed.` | `failed`: a push during the full review aborted it, so retry once the head is final | observed on a CodeRabbit trial |
  | `Review finished.` + the incremental note | never a full review |

- **A failed full review: wait, retry once, escalate.** Failures are rare and their
  cause is undocumented (CodeRabbit's docs say only "an error occurred, please try
  again later"). It is **not the rate limit**: a rate-limited request gets a
  separate "Review rate limited" notice and a passing check, and doesn't consume
  allowance. A retry about half an hour later succeeded (observed on a CodeRabbit
  trial). So wait at least 20 minutes, re-post `@coderabbitai full review` once, and
  hand a second failure to a human with both reply links. Never retry immediately.
- `@coderabbitai rate limit` reports the remaining hourly allowance.
- Auto-review and auto-incremental-review on push are on by default
  (`reviews.auto_review` in `.coderabbit.yaml`); it pauses after
  `auto_pause_after_reviewed_commits` (default 5) reviewed commits — after that,
  the trigger comment is required even in "automatic" mode, so re-detect the
  trigger mode if reviews stop arriving.
- Commands must be **top-level** PR comments, not thread replies.

## Detecting a result

- **(observed)** Check name: `CodeRabbit` (a check run; legacy commit statuses are
  opt-in).
- **(observed)** **A green `CodeRabbit` check does NOT mean the head was reviewed.**
  On a push it doesn't review (auto-review off, a label gate, or a paused
  auto-review), it posts `success` with "Review skipped: …", and a rate-limited push
  gets a passing "Review rate limited" check on purpose. So never require the
  `CodeRabbit` check: require the vendor-neutral `review-gate` status, which is
  green only when `reviewer_state.py` has a verdict on the current head and the
  final full review finished.
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
- **(observed)** **Outside-diff findings live ONLY in the review body**: a review
  opening `> [!CAUTION] Some comments are outside the diff…` with
  `**⚠️ Outside diff range comments (N)**` and each finding in a `<details>` block —
  no `Actionable comments posted` line and **no inline thread**. `findings_pattern`
  matches either count (`Actionable…` wins when both are present). Because there is
  no thread, enumerating inline comments (Step 2b) finds nothing: **read the review
  body** and treat each outside-diff finding as a concern (reply with the fix SHA as
  a top-level PR comment — there's no thread to reply on or resolve).
- **(observed)** Re-reviews after a fix: CodeRabbit resolved its addressed threads by
  itself, then posted an empty-body review (the verdict is in the walkthrough, above).
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

- Repo config: `.coderabbit.yaml` at the repo root (this template ships none).
  Settings worth considering:
  - **Trigger-only** — `reviews.auto_review.enabled: false` and
    `reviews.auto_review.auto_incremental_review: false`, with no label gate. With
    auto-review on it re-reviews every push, and its threads block merges through
    the conversation-resolution rule. With it off, the ~30s start-check never sees an
    automatic start, so the skill's comment mode is the normal path.
  - `reviews.auto_review.base_branches: [".*"]` so it reviews PRs whose base isn't
    the default branch (stacked PRs); without it those are skipped.
- **Noise filters** live in the vendor-neutral `.github/review-guidelines.md`. Wire
  them in one of two ways:
  - copy its categories into `reviews.path_instructions` (one entry with
    `path: "**"`); or
  - point the code-guidelines knowledge base at the file. A guideline file applies
    only to its own directory tree by default, so map it to the whole repo:
    ```yaml
    knowledge_base:
      code_guidelines:
        filePatterns:
          - files: ".github/review-guidelines.md"
            applyTo: "**"
    ```
    Don't also list the file's path in `path_instructions`: that reviews it as
    changed code instead of reading it as guidelines.

## Switching reviewers

Switching reviewers is `"active": "<key>"` in `.github/pr-reviewer.json`, and
nothing else: `review-gate` follows `active`. **Never make a vendor's own check a
required status.** A vendor check that can't report on merge-group commits
(Bugbot's can't) wedges a merge queue, and keeping it required would mean dropping
the queue and requiring up-to-date branches instead. Switching to Bugbot also means
restoring its noise-filter file (see `bugbot.md`).
