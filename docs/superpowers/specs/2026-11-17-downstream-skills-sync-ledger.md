# Downstream skills sync — reconciliation record (#66)

The per-commit record that spec §1 of
`2026-11-17-downstream-skills-sync-design.md` asks for. Every downstream commit that
touched a shared skill since that skill last synced has one row here, with its
verdict and the task in the plan (`docs/superpowers/plans/2026-11-17-downstream-skills-sync.md`)
that carries it. Later tasks cite rows by downstream PR number; Task 11 fills in the
residual diff at the end.

The downstream repos are **A** (backend, leads), **B** (frontend) and **C** (internal
starter). PR numbers without a prefix are A's; B's and C's carry `B` / `C`.

**Sync points.** The six skills #56 covered: five in A (build-from-issue,
plan-issues, clean-up-kanban, resolve-pr-concerns, review-skill; A has no pr-check)
and pr-check, which only B and C carry. For A's five, plus the seven review helpers in
A's `work-the-backlog/scripts/`, `.agentrules/pr-check-scope.md` and
`scripts/single_flight.sh`: since 2026-10-20. The explain-diff pair: since
2026-08-11. B and C: every commit touching `.agents/skills` since 2026-10-01. A's
generic-only skills: their whole history. Taken from full-history clones; A at
`main` 2026-11-17.

**Verdicts:** `ported` · `generalized` · `left out: product-specific` ·
`already in template (#PR)` · `declined in #47, ported now` · `template convention`
(docstring style or formatter output; the template uses numpy docstrings).
**Task** is the plan task that carries the row; `—` means nothing to do.

## A: shared skills and review helpers (since 2026-10-20)

Each commit from 2026-10-20 to 2026-10-29 was checked against what #56 carried.

| Date | PR | Paths | Change | Verdict | Task |
|---|---|---|---|---|---|
| 2026-10-22 | #2496 | resolve-pr-concerns SKILL.md (5b); `path_owners.py`, `review_gate.py` | CODEOWNERS path-owner approval half of `review-gate`; `@<owner> must approve` is a wait, not a finding | already in template (#56) | — |
| 2026-10-22 | #2505 | build-from-issue SKILL.md (Step 6) | E2E discovery example renamed to a product make target | left out: product-specific (template keeps its generic per-stack list) | — |
| 2026-10-22 | #2530 | `.agentrules/pr-check-scope.md`; build-from-issue SKILL.md (Step 11 gate, red flag, mistakes row); resolve-pr-concerns SKILL.md (gate paragraph) | Light check: a scoped pre-PR check for a diff outside code, config and shared test fixtures; CI stays the full gate | declined in #47, ported now (generalized: rule `.agents/rules/shared/pr-check-scope.md`, new hard-stop wording, full-check fallback when the rule is absent) | T4 (rule, hard stop), T5 (prose) |
| 2026-10-26 | #2573 | resolve-pr-concerns SKILL.md (2g) | Speed-guard budgets scaled by a measured runner-speed factor | already in template (#56: dynamic budget in `.github/pr-reviewer.json` `test_speed`) | — |
| 2026-10-26 | #2603 | resolve-pr-concerns SKILL.md (2h, 4e); `human_review.py`; `merge_gate.py` | Answer a human reviewer's review (threads + one body comment, re-request); a standing change request blocks the merge report | already in template (#56) | — |
| 2026-10-27 | #2623 (lock) | `scripts/single_flight.sh`; resolve-pr-concerns SKILL.md | Single-flight lock: one `pr_check` per machine | declined in #47, ported now (generalized: stdlib `resolve-pr-concerns/scripts/single_flight.py`, same lock path and nesting marker) | T3 (script), T5 (prose) |
| 2026-10-27 | #2623 (stack) | build-from-issue SKILL.md (stacked finalize); resolve-pr-concerns SKILL.md (modes, 4d "Stacked layers") | `pr_check` on the top layer only, `make lint` on lower layers | already in template (#56, `references/stacked-prs.md` "Finalize") | — |
| 2026-11-01 | #2745 | `.agentrules/pr-check-scope.md`; build-from-issue SKILL.md (stacked finalize) | Names the merge-queue-only slow/pdf/DB lanes | left out: product-specific (lane names); the generic half (CI gates each layer, the queue tests the combined state) is kept | T4 (rule stays lane-free), T5 |
| 2026-11-03 | #2805 | clean-up-kanban SKILL.md (§6) | Data-layer labels on parity cards; re-import rule | left out: product-specific | T10 (no change) |
| 2026-11-05 | #2845 | `review_gate.py`; resolve-pr-concerns SKILL.md (5b) | Docs-only PRs pass `review-gate` without a review request | generalized (opt-in: example ships as `references/docs-only-paths.example.json`, no repo-level file; README interview asks; maintainer unnamed) | T2 (script), T5 (prose, README) |
| 2026-11-05 | #2847 | build-from-issue SKILL.md (7b) | Ship a teaching notebook with every live kit | generalized (two-way tooling check; local-stack JupyterLab and its env var left out) | T6 |
| 2026-11-10 | #2903 (gate) | `review_gate.py`; resolve-pr-concerns SKILL.md (overview, "Local review rounds"); `references/reviewers/coderabbit.md` | `review-gate` stops counting a local reviewer's open threads; the ruleset's conversation resolution blocks the merge | generalized | T2 (script), T5 (prose, adapter) |
| 2026-11-10 | #2903 (2e) | resolve-pr-concerns SKILL.md (2e) | Pointer to a repo-wide update-PR sweep skill | generalized (points at `upgrade-deps` sweep mode) | T5 (T7 builds the mode) |
| 2026-11-10 | #2917 | build-from-issue SKILL.md (Step 1a) | Rename launcher-named branches to the repo pattern | already in template (#67) | — |

No commit since 2026-10-20 touched plan-issues, review-skill, `reviewer_state.py`,
`local_review.py` or `review_preflight.py`. A has no pr-check skill.

## A: explain-diff and explain-diff-merge-review (since 2026-08-11)

| Date | PR | Paths | Change | Verdict | Task |
|---|---|---|---|---|---|
| 2026-08-12 | #1044 | explain-diff-merge-review (SKILL.md, `render.py`, `flag_sensitive_paths.py`) | Add the skill; wire it into build-from-issue 7b | already in template (template-originated, 2026-08-11; the template adds the preflight) | — |
| 2026-08-13 | #1084 | explain-diff (SKILL.md, `render.py`) | Sync the skill in from the template | already in template | — |
| 2026-10-08 | #2012 | explain-diff `render.py`; explain-diff-merge-review `render.py`, `flag_sensitive_paths.py` | Google-style docstrings on every function; no behavior change | template convention | T10 (no behavior or lint fix to port, so no regression test owed) |

The rest of A's diff in these scripts is A's formatter output (line wrapping) and
`-> None` / parameter annotations; the template's `machinery_lint` covers `scripts/`
and `tests/`, not skill scripts, so it is not a lint fix. SKILL.md bodies match
apart from the template's preflight.

**Correction to the spec's known table:** #1837 touched no skill path (only repo
`scripts/` and tests), and #2896 touched only `write-teaching-notebook`, not
build-from-issue.

## B and C (since 2026-10-01)

| Date | PR | Paths | Change | Verdict | Task |
|---|---|---|---|---|---|
| 2026-10-19 | C #69 | clean-up-kanban SKILL.md; plan-issues SKILL.md | Same change as B #31 | already in template (#56) | — |
| 2026-10-21 | B #31 | clean-up-kanban SKILL.md (§1a, epic template); plan-issues SKILL.md | No evergreen epics: prefer a new, drainable epic; split past ~60 children | already in template (#56) | — |
| 2026-10-29 | B #36 | build-from-issue SKILL.md; resolve-pr-concerns SKILL.md; `wait_for_pr_checks.sh` | Hard-codes CodeRabbit in place of Bugbot; a pre-PR CLI review; the wait script requires the vendor's check | already in template (#56, vendor-neutral: reviewer adapter in `.github/pr-reviewer.json`, `references/reviewers/coderabbit.md`, A's local review rounds, the required `review-gate`); vendor hard-coding not ported. Re-checked in Task 11: B's pre-PR CLI review is covered by the local review rounds, which run the same local CLI review once the PR is open (so each round is recorded on the PR and `review-gate` can see it) and keep the paid PR review for the final full one | — |
| 2026-10-29 | B #40 | resolve-pr-concerns SKILL.md | HEAD-review evidence is a review on HEAD or HEAD in the summary's reviewed commits, never an inline thread; bot-login regex for both API spellings | already in template (#56, `reviewer_state.py` reads `commit_id` and the summary's reviewed SHA) | — |

B's `pr-check` had no commit in the window; Task 11 diffs it.

## A: generic-only skills (whole history)

| Date | PR | Skill (paths) | Change | Verdict | Task |
|---|---|---|---|---|---|
| 2026-11-10 | #2903 | cleanup-update-prs (SKILL.md, `scripts/update_prs.py`) | New skill: plan, judgment pass, one approval, close, fold, post-merge recheck | generalized (folded into `upgrade-deps` sweep mode; stdlib; bun support) | T7 |
| 2026-11-12 | #2969 | cleanup-update-prs (SKILL.md, `update_prs.py`) | Dependabot policy becomes security + majors; a major with no advisory is `REVIEW` | generalized (both Dependabot policies) | T7 |
| 2026-11-16 | #3053 | cleanup-update-prs (SKILL.md) | uv majors for direct deps only; an indirect major is a leftover to close | generalized (applies under the major-only policy) | T7 |
| 2026-11-05 | #2847 | write-teaching-notebook (SKILL.md) | New skill: structure, before/after, never write to a shared environment | generalized (§3: tooling via skill-config or a `notebook` group; "before" from a named read-only path or `main` in a temp worktree; starter and check script bundled) | T6 |
| 2026-11-08 | #2896 | write-teaching-notebook (SKILL.md) | Visual output: `rich`, rendered tables, Plotly, WebGL or aggregation for big data, seaborn for raw points | ported | T6 |
| 2026-07-31 | #898 | update-project-memory (SKILL.md, `references/decision-tree.md`, `scripts/check_budget.py`) | AGENTS.md as a budget-governed router plus a regrowth gate | generalized (§5: `.agents/rules/` homes, caps as CLI args, defaults 120/150 lines and 9,000 chars) | T8 |
| 2026-08-07 | #975 | update-project-memory (`check_budget.py`) | Line wrap for A's lint scope | template convention | T8 |
| 2026-10-08 | #2012 | update-project-memory (`check_budget.py`) | Docstrings | template convention | T8 |
| 2026-05-28 | #151 | daily-update (SKILL.md, `evals/evals.json`, `scripts/daily_update.py`) | New skill: daily status from git, PRs and the board | generalized (§6: repo from `gh`, board from skill-config, per-user state file, no default channel) | T9 |
| 2026-06-01 | #152 | daily-update (all three) | Surface unmerged-branch activity under "In flight" | ported | T9 |
| 2026-06-08 | #174 | daily-update (SKILL.md, `daily_update.py`) | Always render "In flight", with a between-tasks placeholder | ported | T9 |
| 2026-10-06 | #2009 | daily-update (`daily_update.py`) | numpy docstrings converted to Google style | template convention | T9 |
| 2026-10-08 | #2012 | daily-update (`daily_update.py`) | More docstrings | template convention | T9 |
| 2026-05-28 | #151 | daily-update-workspace (`.gitignore` only) | No committed skill | stays repo-local | — |

## Not synced

- **A #1851** is open, not merged. Not synced.
- **A #2996** (retire A's per-test speed guard) is still open, and so is its PR
  (#3081), at A's `main` 2026-11-17 (re-fetched in Task 11). Nothing to port: the
  skills' `pr_check` prose is already gate-agnostic ("including the per-test speed
  check where the repo has one"), and the template keeps its own speed guard.
- `work-the-backlog` is deferred (follow-on card), though its seven review helpers
  are in this record. The 2026-11-11 amendment to #49 is not built.

## Skill inventory (for the PR body)

- **A** (38 folders): 7 shared, synced above (build-from-issue, plan-issues,
  clean-up-kanban, resolve-pr-concerns, review-skill, explain-diff,
  explain-diff-merge-review); `write-teaching-notebook`, `update-project-memory`,
  `daily-update` ported; `cleanup-update-prs` folded into `upgrade-deps`;
  `work-the-backlog`, `standup-kanban-sync`, `langfuse-check-in`,
  `daily-update-workspace` stay repo-local; 2 third-party (`gh-stack`, `langfuse`,
  in A's `skills-lock.json`); 21 product-specific skills stay repo-local.
- **B** (6): build-from-issue, clean-up-kanban, plan-issues, pr-check,
  resolve-pr-concerns, review-skill, all in the template.
- **C** (7): B's six plus run-tests, all in the template.

## Residual diff

Task 11, against A's `main` at `7a2ce99` (2026-11-17, re-fetched): the only commit
since the record was taken (#3001) touches no skill path. Commits after 2026-11-10
that changed A's `.agentrules/` only (#3018, #3038, #3039, #3041) are rule changes,
outside this record; #3057 adds a product-specific skill (counted in the inventory).
B and C have no new skill commits.

Each shared skill diffed against A (B's `pr-check` too; the seven helpers against
A's `work-the-backlog/scripts/`). Scripts were compared on their AST with docstrings
and annotations stripped, so style and the docstring convention drop out; prose was
compared paragraph by paragraph. Path rewrites (`.claude/skills/` and `.agentrules/`
to `.agents/`) and the template's preflight paragraph are generalization, not drift,
and are not repeated below. **No unexplained hunk remained, so Task 11 ported
nothing.** "This PR" is the #66 PR (its task in brackets).

**build-from-issue**
- template ahead (#56): stacked delivery moved to `resolve-pr-concerns/references/stacked-prs.md` (offer, build, finalize, don't-stack list); Step 7 audits prose "done" criteria; the conflicting-PR exit of `wait_for_pr_checks.py`.
- template ahead (initial template): the momentum hook's second Slack clause ("Other tracks ready next").
- template ahead (#67): Step 1a resume and rename wording, fresh-workspace and open-PR-head guards.
- template ahead (this PR, T5/T6): light check and single-flight gate wording, gate-agnostic `pr_check` prose, teaching notebook through `write-teaching-notebook`.
- template ahead (#56): the E2E discovery list is per stack; staleness tells are generic.
- product-specific: E2E make targets, the staleness tells naming A's removed modules, the observability check-in step, the connector-coverage and graph-diagram steps (9b), the team Slack channel and its rationale, A's issue numbers.

**plan-issues**
- template ahead (#56): a GraphQL-refused REST fallback for any harness (A: cloud sessions only); unlinking is not deleting; one card per mergeable PR in the scope question and the Why bullet; epic-narrative refresh.
- product-specific: the second-tracker routing reference and every routing step, A's fixed board and its auto-add rule.

**clean-up-kanban**
- template ahead (#56): generic origin case study, branch-pattern-aware classifier, upstream-sync pointer to the shared rule; (initial template): Stage-field setup.
- product-specific: the frontend/eval-app repo routing, data-layer labels on parity cards and the re-import rule (#2805), A's issue numbers.

**resolve-pr-concerns** (SKILL.md, reviewer adapters)
- template ahead (#56): parallel fixes in `references/parallel-fixes.md`, stacks in `references/stacked-prs.md`, `wait_for_pr_checks.py` (A: `.sh`), the up-to-date rule read from the branch rules, the reviewer-poll contract and between-rounds polling restated, the speed-guard coaching in 2g.
- template ahead (#58): per-adapter repo-owned `notes` file instead of vendor facts in the adapter docs.
- template ahead (this PR, T2/T5): opt-in docs-only lane, local-thread gate change, single-flight gate, light check, sweep pointer in 2e.
- product-specific: vendor trial history, billing and pricing, the org's app id and usage JSON, the named bypass holder, A's required-check names and issue numbers.

**Review helpers** (`resolve-pr-concerns/scripts/` vs A's `work-the-backlog/scripts/`)
- `review_gate.py`: template ahead (#56: CODEOWNERS-only gate with no active reviewer, three CODEOWNERS locations, `incremental_review_on_head` guard; this PR T2: docs-only lane refuses a moved head instead of A's whole-pass retry).
- `reviewer_state.py`: template ahead (#46: `NoActiveReviewer`; #56: `start_failure_pattern`, optional `min_remaining` default 1; #40: `_classify` raises on unset final-review patterns instead of asserting; #58: `notes` / `notes_file`).
- `merge_gate.py`: template ahead (#56: required checks and thread resolution read from the branch rules, exit codes, paginated threads); product-specific: A's hard-coded required-check names.
- `human_review.py`: template ahead (#56: config errors and `gh` failures exit with a message, `owner/repo` validation).
- `path_owners.py`: template ahead (#56: unsupported CODEOWNERS lines are skipped, not fatal; three CODEOWNERS locations).
- `local_review.py`, `review_preflight.py`: template ahead (#46: `not_configured` with no active reviewer; #56: configurable start-failure pattern, `min_remaining`); product-specific: A's issue number in the round record.

**review-skill**
- template ahead (#56): canonical `.agents/skills/` lookup, `--json` report path instead of writing into the skill folder, executable-bit check only for scripts with a shebang, script-path checks.

**explain-diff, explain-diff-merge-review**
- Scripts identical on the AST; SKILL.md bodies identical. Nothing beyond the record's #2012 row.

**pr-check** (B; A has none)
- template ahead (#56, #58, this PR T4/T5): stack-neutral (B: React-only), single-flight lock, gate-agnostic speed check, light-check note, report adds speed offenders.

**write-teaching-notebook, update-project-memory, daily-update** (ported in this PR, T6/T8/T9)
- generalized per §3, §5, §6: bundled starter and check script, tooling from skill-config or the `notebook` group, `main` as "before"; caps as CLI args with a soft cap; repo and board from `gh` or skill-config, injected `gh` runner, per-repo state path.
- product-specific: A's local-stack JupyterLab, seed and data endpoints; A's AGENTS.md caps and test path; A's repo, board, user map, default channel and permission-error wording tied to its board name.

**upgrade-deps** (A's `cleanup-update-prs`, folded in T7): no file-level diff; the record's #2903, #2969 and #3053 rows cover it.
