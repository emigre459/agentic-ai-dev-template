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
- **Fix rounds run locally**: this adapter declares `local_review` (the CLI,
  below), so agents never post the trigger on a PR; `reviewer_state.py` keeps
  `needs_trigger` and `round_budget.trigger_allowed` false. The PR's one request is
  the final full review. Set
  `local_review` and `local_preflight` to `null` to go back to PR incremental
  rounds (e.g. for a developer without the CLI).
- Trigger: a top-level PR comment `@coderabbitai review` (`trigger_comment`) —
  an **incremental** review of commits since the last one. Kept in the adapter for
  a run with `local_review` set to null.
  Reference: https://docs.coderabbit.ai/reference/review-commands
- **Final full review** (`final_review_comment`): once the local rounds are clean,
  post `@coderabbitai full review` **once** per PR. It re-reviews the whole diff,
  and on a CodeRabbit trial it found the most important bug of the run. Fix its
  findings, reply on each thread (CodeRabbit re-checks the fix in its reply, a chat
  message on its own limit, not a review), and run one local round on the final
  head; never post a second one for coverage (repeated full reviews don't converge,
  they just sample more, and each spends quota).
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
  | `⚠️ Action not completed` / `Pull request base or head changed.` | `failed`: the same abort (a push, or `main` moving under it) | observed on a CodeRabbit trial |
  | `Review finished.` + the incremental note | never a full review |

- **(observed)** **How an incremental review ends.** The reply to
  `@coderabbitai review` carries the incremental note ("CodeRabbit is an
  incremental review system…"), and the `incremental_review_*_pattern` fields
  require it. `reviewer_state.py` reports the reply to the newest trigger as
  `incremental_review_outcome`:

  | Reply | Outcome |
  |---|---|
  | `✅ Action performed` / `Review finished.` | `finished` |
  | `❌ Action failed` / `Review failed.` | `failed`: re-post the trigger once |
  | `⚠️ Action not completed` / `No files to review.` | `no_changes`: nothing new since the last reviewed commit (e.g. a merge-only push); an earlier verdict carries forward (`verdict_carried_from`) and the trigger spends no round; with no verdict to carry it spends one and `needs_trigger` stays true |
  | any other `⚠️ Action not completed` | `not_completed`: re-post once |
  | still `Action performed` / `Review triggered.`, but a verdict-bearing review of the head posted after the trigger | `finished`: the reply can lag the review by minutes |

  While a review fails, CodeRabbit can still post **empty-body reviews** on the head
  in the same seconds, as containers for its thread replies, so an empty review
  alone is never a clean verdict (see "Detecting a result").

- **Don't push while a full review is running.** A push aborts it ("Head commit changed" /
  "Pull request base or head changed") and spends a review for nothing; batch the fixes
  and push after it finishes.
- **A failed full review: wait, retry once, escalate.** Failures are rare and their
  cause is undocumented (CodeRabbit's docs say only "an error occurred, please try
  again later"). It is **not the rate limit**: a rate-limited request gets a
  separate "Review rate limited" notice and a passing check, and doesn't consume
  allowance — except that a refused full review can also arrive as `Review failed.`
  with the "included review limit" sentence, which is a refusal (see "The refusal
  replies" below), not a failure. A retry about half an hour later succeeded (observed on a CodeRabbit
  trial). So wait at least 20 minutes, re-post `@coderabbitai full review` once, and
  hand a second failure to a human with both reply links. Never retry immediately.
- `@coderabbitai rate limit` reports the remaining hourly PR allowance, but it is a
  PR comment. The local CLI can't answer it: its usage report covers the CLI's own
  pool (below).
- Auto-review and auto-incremental-review on push are on by default
  (`reviews.auto_review` in `.coderabbit.yaml`); it pauses after
  `auto_pause_after_reviewed_commits` (default 5) reviewed commits — after that,
  the trigger comment is required even in "automatic" mode, so re-detect the
  trigger mode if reviews stop arriving.
- Commands must be **top-level** PR comments, not thread replies.
- When CodeRabbit is installed but is **not** the `active` reviewer, don't trigger
  it at all.

## Rate limits, the local review and the allowance check

- **(observed)** **Two allowances per developer, and nothing local can see the PR
  one.** PR reviews (every `@coderabbitai review` / `full review`) and CLI reviews
  draw on separate pools: "Reviews from the CLI and IDE extensions draw on separate
  allowances" ([review usage dashboard](https://docs.coderabbit.ai/management/review-usage-dashboard)).
  We have seen `coderabbit usage --agent` read 10 of 10 left seconds before a PR
  request was refused. So `local_preflight` gates **local reviews** only; a PR
  request is never preflighted, and its refusal reply (below) decides the wait.
- **The PR pool shrinks with use** (Team plan, [rate limits](https://docs.coderabbit.ai/management/rate-limits)):

  | Included PR reviews in the last 7 days | Refill rate |
  |---|---|
  | 0–39 | 8 per hour |
  | 40–49 | 6 per hour |
  | 50–59 | 4 per hour |
  | 60–69 | 2 per hour |
  | 70+ | 1 per hour, one at a time |

  The slowdown "does not change trial, CLI, IDE, or self-hosted limits". It counts
  per developer identity: agents post under the developer's login, so their
  requests land in that developer's pool.
- **Three counters; only one sets the rate** ([review usage dashboard](https://docs.coderabbit.ai/management/review-usage-dashboard)).
  *Review events* count every attempt, including blocked ones and ones that
  continued on credits; *included reviews* draw the hourly allowance; the *rolling
  7-day activity* that sets the refill rate counts only included reviews. Blocked
  attempts and paid (credit) continuations don't raise it. **(observed)** Opening a
  PR doesn't either when auto-review is off: the 7-day count matched the explicit
  requests that ran.
- **(observed)** **How a team gets throttled.** After a trial with a higher limit
  ends, a team that asks for several PR reviews per PR (an incremental review per
  fix round plus the full review) climbs the table within days: we watched one
  developer's 7-day count go from under 10 to 70 (1 per hour) in three days. The
  count falls only as old reviews age out, and every burst pushes it back up, so
  staying under 40 (8 per hour) takes fewer requests, not waiting. That is why fix
  rounds run locally and each PR spends one PR review.
- **Overage.** An over-limit review can continue on credits: $1 per credit, 4 files
  per credit, i.e. $0.25 per reviewed file ([usage add-on](https://docs.coderabbit.ai/management/usage-based-addon)).
  PR and CLI overage share one organization billing path and its monthly spending
  cap (an admin sets it, or "Unlimited"); past the cap an over-limit request is
  refused until the cap is raised or the month resets. The CLI spends credits only
  with `--use-credits`, which `local_review` never passes.
- **A review covers at most 300 files.** A PR (or stack layer) larger than that
  can't be fully reviewed, so `review-gate` can't go green on it: split it, or land
  the mechanical bulk as its own PR first.
- **(observed)** **The local review** (`local_review`):
  `coderabbit review --agent --deep --committed --base-commit <merge base>`.
  - `--deep` is "the full pull request review policy". Replayed on a handful of
    already-reviewed PRs, it found about as many issues as the PR full review but
    mostly different ones (a small minority overlapped), while the default mode
    missed issues the full review had caught. So local rounds use `--deep`, and the
    PR keeps its one full review; neither replaces the other.
  - Output is one JSON event per line: `review_context`, `heartbeat`, `finding`
    (`severity`, `fileName`, `startLine`, `endLine`, `title`, `comment`,
    `suggestions`), then `complete` (`findings`, `reviewedFiles`). A review takes
    2–6 minutes and can take 15 (`timeout_seconds` 1200).
  - Each run takes one review from the CLI's hourly pool (we read 10 per hour; the
    docs' figure for Team may differ). The first usage reading after a run can lag.
  - **(observed)** A `--deep` round can cost 1–2 reviews of the pool, so the adapter
    sets `local_preflight.min_remaining: 2`: with 1 left the preflight reads
    `exhausted`, rather than starting a round that dies with `Failed to start
    review` (`local_review.start_failure_pattern`). `local_review.py` records the
    reviews left before and after each round (`remaining_before` /
    `remaining_after`; the after-read is best effort and may be `null`). A start
    failure reads `exhausted` only when the re-read after it is under
    `min_remaining`; otherwise it stays `failed`, so a real CLI or network failure
    is never masked as a wait.
  - It reasons with CodeRabbit's index of the default branch, so reviewing an old
    commit can flag code the default branch has since changed. On a branch cut from
    the current default branch that doesn't arise.
- **(observed)** **The allowance check is the CLI's usage report**
  (`local_preflight`): `coderabbit usage --agent` prints one JSON line. With a full
  pool (identifying values replaced):

  ```json
  {"type":"status","phase":"usage","status":"complete","organization":"<org>",
   "includedReviews":{"state":"available","repository":"<owner>/<repo>",
   "fullCapacityInMs":0,"limit":10,"remaining":10,"rollingWindowMs":3600000},
   "billingPeriod":{"state":"available","scope":"user","usageBillingStatus":"active",
   "user":"<login>","reviewsCount":12,"spendCents":0,
   "resetsAt":"<ISO time>"}}
  ```

  `fullCapacityInMs` is the time until the WHOLE pool is back; one review frees
  sooner, so the check re-runs every 5 minutes or less (`max_recheck_seconds`)
  rather than sleeping it out. The empty-pool output hasn't been observed yet; if
  its shape differs, fix the paths in `pr-reviewer.json`, not the scripts.
  `billingPeriod.reviewsCount` counts PR and CLI reviews together.
- **Setting up the CLI.** Install: https://docs.coderabbit.ai/cli#getting-started
  (Homebrew works). Log in: `coderabbit auth login`; check with
  `coderabbit auth status --agent` (`"authenticated": true`). Diagnose:
  `coderabbit doctor`. Without it the check reads `unknown` and `local_review.py`
  fails with the matching remedy.
- **(observed)** **The refusal replies** (`rate_limited_pattern`), both under
  `⚠️ Action not completed`:
  - to `@coderabbitai review`: `Review rate limited.` plus the incremental note,
    with no wait stated;
  - to `@coderabbitai full review`: `Review rate limited.` plus "Your next included
    review will be available in 13 minutes." (`rate_limit_wait_pattern`);
  - to `@coderabbitai full review`, under `❌ Action failed`: `Review failed.` with no
    "Review rate limited" line, plus "Your included review limit is currently
    reached …" and the same "available in N minutes". `rate_limited_pattern`
    matches that sentence too, so it reads as a refusal to wait out, not a failed
    review to re-post.

  The same allowance sentence also follows `Action performed` / `Full review
  triggered.` when the review ran anyway on usage-based billing; that review ran,
  so the pattern skips any reply that says `Action performed`.

  The full-review one also matches the partial ("not completed") pattern, so
  `reviewer_state.py` never rescues a refused full review with a review that
  happens to land in its window, and reports the refusal as `rate_limit.refused`.
  Answering a refused incremental review with a full review only spends the quota
  faster: the full review was refused too.
- **(observed)** **The rate-limit notice** (`rate_limit_notice_marker`). When the
  reply states no wait, the summary comment does: it carries
  `<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->` and
  "**Next included review available in N minutes.**", and CodeRabbit edits it in
  place on each refused attempt (it is also posted when the PR opens). So
  `reviewer_state.py` reads the wait from it only when it was edited at or after the
  request, counting from the edit; the notice's wait matched the moment an earlier
  review aged out of the hourly window. With neither, the adapter's 60-minute
  default applies. (A re-post a couple of minutes before the stated wait was
  refused.)
- The review usage dashboard's Review log lists every PR attempt with its outcome,
  files, refill rate and 7-day count: the only view of the PR pool (it needs a
  login). Its IDE/CLI page has shown 0 reviews while CLI reviews had run.
- A refused request still gets a passing "Review rate limited" `CodeRabbit` check;
  never read that check (below).

## Detecting a result

- **(observed)** Check name: `CodeRabbit`. On the repos we've seen it is a **commit
  status** posted by the CodeRabbit app, not a check run: query it with
  `repos/<owner>/<repo>/commits/<sha>/statuses`, not `check-runs`. CodeRabbit's docs
  describe a check run as well, so it can be either; `gh pr checks` lists both
  kinds.
- **(observed)** **A green `CodeRabbit` check does NOT mean the head was reviewed.**
  On a push it doesn't review (auto-review off, a label gate, or a paused
  auto-review), it posts `success` with "Review skipped: …", and a rate-limited push
  gets a passing "Review rate limited" check on purpose. So never require the
  `CodeRabbit` check: require the vendor-neutral `review-gate` status. Because this
  adapter declares a local review, that status is green once the final full review
  finished and every CodeRabbit thread is resolved (later fix commits are re-checked
  locally and on the threads); with `local_review` null it also needs a verdict on
  the current head.
- **(observed)** Logins: `coderabbitai[bot]` on REST reviews/comments,
  `coderabbitai` as the GraphQL thread author — both listed in `bot_logins`.
- **(observed)** Findings: a review whose body starts
  `**Actionable comments posted: N**`, with `commit_id` = the reviewed head, plus N
  inline threads.
- **(observed)** **A clean incremental re-review posts an EMPTY-body review** (or a
  nitpick-only one, with no `Actionable comments posted` line). The walkthrough
  issue-comment (starts
  `<!-- This is an auto-generated comment: summarize by coderabbit.ai -->`, edited
  in place) used to carry the clean verdict (`No actionable comments were generated
  in the recent review.`, still the adapter's `clean_pattern`). Hence
  `summary_in_issue_comments: true`: judge that comment like a review, with recency
  = its `updated_at` (not `created_at`).
- **(observed)** **The walkthrough format changed in late September 2026**: it
  dropped both the `No actionable comments` line and the reviewed-range line. So a
  verdict-less review of the head, or a walkthrough naming the head as reviewed,
  counts as `pass` **only inside a finished request window** — from a trigger or
  full-review comment to the last edit of its `Review finished` / `Full review
  finished` reply — and only when no findings review is on the head.
  `reviewer_state.py` applies that rule; an empty review outside such a window
  proves nothing.
- **(observed)** Reviewed SHA in the walkthrough, in the order the
  `reviewed_sha_pattern` list tries them:
  1. the older `Reviewing files that changed from the base of the PR and between
     <base-sha> and <head-sha>.` (the second SHA);
  2. `<!-- final_review_risk_coverage:{…"coveredCommitId":"<sha>"…} -->`;
  3. `<!-- change_assessment_commit:"<sha>" -->`.

  A walkthrough still naming an older head does not vouch for the new one, so after
  a push the verdict reads `none` until it re-reviews.
- **(observed)** **Merge Risk.** A newer walkthrough section between the
  `final_review_risk_start` / `_end` markers can state a finding with no thread.
  `unthreaded_finding_patterns` puts it in `unthreaded_findings`. The walkthrough's
  "Reviews paused" note (`auto_pause_after_reviewed_commits`) doesn't matter in
  trigger-only mode, where reviews run only on request.
- **(observed)** **Outside-diff findings live ONLY in the review body**: a review
  opening `> [!CAUTION] Some comments are outside the diff…` with
  `**⚠️ Outside diff range comments (N)**` and each finding in a `<details>` block —
  no `Actionable comments posted` line and **no inline thread**. `findings_pattern`
  matches either count (`Actionable…` wins when both are present). Because there is
  no thread, `open_findings` and Step 2b find nothing; the block lands in
  `unthreaded_findings` instead. Treat each outside-diff finding as a concern and
  record its disposition in the one top-level disposition comment (Step 2a) —
  there's no thread to reply on or resolve.
- **(observed)** Re-reviews after a fix: CodeRabbit resolved its addressed threads by
  itself, then posted an empty-body review (the verdict is in the walkthrough, above).
- No summary marker (`summary_marker: null`): a review or comment counts as the
  summary only when its body matches a verdict pattern, so empty/reply-style
  reviews can't shadow it.
- The walkthrough also carries pre-merge "checks" warnings (e.g. out-of-scope
  changes) — those are not inline findings and don't affect the verdict; read them
  as ordinary PR feedback.

## CODEOWNERS and `review-gate`

If the default branch has a CODEOWNERS file (`.github/CODEOWNERS`, `CODEOWNERS` or
`docs/CODEOWNERS`), `review-gate` also fails while a changed path's owner hasn't
approved the head (`@<owner> must approve: …`), whatever CodeRabbit's verdict. It
resolves user logins only: a CODEOWNERS line naming a team (`@org/team`) or an email
owner, alone or beside users, is skipped by this gate (as is a line with `!`, `[...]`
or `\` in its pattern) and its paths are left to GitHub's own "Require review from
Code Owners" rule, so turn that rule on if you rely on team owners. Without a
CODEOWNERS file, or with one naming no user owner, the ownership half does nothing.

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
    automatic start, so the skill's comment mode is the normal path. **Required with
    `local_review`**: an automatic review spends a PR review on every push, which is
    exactly what the local rounds avoid.
  - `reviews.auto_review.base_branches: [".*"]` so it reviews PRs whose base isn't
    the default branch (stacked PRs); without it those are skipped.
  - Check a review body's "Run configuration → Configuration used" line: it should
    read the repository file, not "Organization UI", or the repo's
    `.coderabbit.yaml` isn't being applied.
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
