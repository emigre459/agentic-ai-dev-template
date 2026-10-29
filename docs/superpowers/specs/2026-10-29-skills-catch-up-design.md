# Catch the dev-workflow skills up to downstream, org-neutral (#47)

Issue: #47 · Parent epic: #51 (publish the dev-workflow skills as a versioned,
installable source) · Date: 2026-10-29

## Intent

The most advanced copies of this template's dev-workflow skills live in downstream
repos, and every improvement made there is re-ported here by hand. Epic #51 makes
this repo the single installable source (`npx skills add`), so before #48 (extension
points) and #49 (consumer kit) the canonical skills must first be *at least as good
as the best downstream copy*, with nothing org-specific in them. After this card,
downstream repos can migrate onto the template's skills without losing behavior.

## Sources surveyed

Six downstream repos carry copies. The most advanced, per skill:

| Skill | Most advanced source | Notes |
|---|---|---|
| build-from-issue | downstream A (backend) + B (frontend) | B newer by date, A ahead on review handling and stacks |
| plan-issues | A | B is A minus one section, with a stale reference |
| clean-up-kanban | B, plus one fix from A | canonical already nearly current |
| resolve-pr-concerns | A, whose helpers live in **another skill's** `scripts/` and the repo root | B and an older template-derived repo are behind canonical |
| review-skill | a tooling repo's variant (external-reference check), A (type hints) | |
| pr-check | B | `pr_check` mirrors the CI gate |

Line-level delta inventories (every hunk classified general / org-specific /
out-of-skill dependency) were produced while building this card and are the
builder's working notes; this document carries every decision they fed.

## Goals

1. Each of the six skills does everything the most advanced downstream copy does
   that is general, and keeps everything canonical already does better.
2. No organization, product, private repo, person, internal tool, Slack channel,
   CI check name, ruleset id, or downstream issue/PR number appears in any skill
   (prose, scripts, references) or in the tests/fixtures this card adds.
3. Every helper a skill calls lives in that skill's own folder and runs on stdlib
   `python3` (or POSIX `sh`); no skill invokes another skill's scripts.
4. `make pr_check` passes, including the skill tests, and `pr_check` now mirrors CI.
5. Every repo built from the template ships a per-test speed guard, so a high-coverage
   suite can't quietly grow slow tests that make CI and development painful.

## Non-goals

- Porting the downstream autonomous backlog skill (`work-the-backlog`) or its
  issue-selection / run-manifest helpers.
- Cross-tracker routing in plan-issues (twin cards in a second tracker). That is a
  per-repo customization and belongs to #48. This card only removes the orphaned
  tracker wording canonical still carries.
- The downstream `review-gate.yml` comment-edit filter (it hard-codes one vendor's
  phrases into a vendor-neutral workflow).
- The downstream "light check" scope rule for doc-only diffs (no such rule here).

## Anti-goals (a failure even if everything "works")

- **Regressing canonical.** Canonical is ahead in places: `"active": null` (no
  reviewer) everywhere, no changelog step, vendor-neutral review handling, waiting
  out refused review requests until `retry_after`, the bundled human-review flow,
  local-round `record_body` recovery, the CONFLICTING/DIRTY/merge-queue merge-ready
  bar, the 11b two-bracket Slack message format, generic staleness tells,
  harness-neutral wording. Every one survives.
- **An agent that merges.** Merge-ready is a *report to the human*. A human presses
  the merge button, or explicitly grants the agent a one-off permission for that
  specific merge. Nothing in the skills or the merge gate's output counts as
  permission, and permission never carries over to another PR.
- **A gate that surprises a repo that didn't ask for it.** The CODEOWNERS gate is
  invisible until a repo adds a CODEOWNERS file.
- **Private data in fixtures.** The downstream reviewer-state fixture is a verbatim
  private PR (it quotes private source); it is replaced by a synthetic fixture,
  never sanitized by search-and-replace.

## Decisions made with the user

1. **Scope** includes stacked PRs and the merge gate (beyond the initial proposal).
2. **CODEOWNERS ownership gate** ships only in a form where a repo without a
   CODEOWNERS file never sees it.
3. **`make pr_check` = lint + coverage + security** (plus the speed check, decision
   13), matching CI's required jobs;
   the pr-check skill then truthfully says it mirrors CI.
4. **`wait_for_pr_checks` and `human_review` move to stdlib python3** (both are
   bash + `jq` today, in canonical and downstream alike).
5. Open Dependabot PRs/alerts are unrelated (stack dependencies) and are handled
   separately.
6. **Stacking policy** (user's words, paraphrased): stack when a single PR is
   complex enough that even an AI reviewer would likely miss related changes; the
   point is review quality (the bot finds more bugs on smaller coherent layers).
   **At most 4 layers. All layers conceptually related** (normally true since they
   come from one planned card). **Total files changed across the whole stack < 300**.
7. **Merge-ready is report-only** (see anti-goals).
8. Tracker routing → #48.
9. **Stacks work with or without a merge queue; this card changes no ruleset or CI.**
   PR #55 (issue #53) makes a merge queue the template default wherever GitHub
   offers one (org-owned repos); user-owned repos, this template included, get none.
   So the stacking references describe both paths: with a queue, `gh stack merge`
   enqueues the layers and the queue tests and lands them bottom-up against the
   latest `main`; without one, every layer's PR runs full CI and is gated on its own,
   and landing follows the no-queue retarget steps. Per-layer CI gating (heavy jobs
   only on the bottom layer) is not added.
10. **CODEOWNERS alone turns the ownership status on**, with or without a configured
    reviewer. A repo with neither a reviewer nor a CODEOWNERS file still gets no
    `review-gate` status (today's behavior).
11. **The org-name guard stores hashed names**, never plain text (the repo is
    public). Also scrub the one existing org mention (a `pyproject.toml` comment).
12. **`gh stack` installation stays manual.** The template installs no tools today, so
    the skills detect a missing extension and show the user the commands; they never
    install it themselves. (Had install machinery existed, it would have needed a
    loud, explicit human Y/N before installing anything locally.)
13. **The per-test speed guard is folded into this card.** Every test gets a budget of
    **10 seconds**, kept **dynamic**: it means "10 s on a typical runner of this
    class" and is scaled by the measured speed of the machine running it. The budget
    and the other knobs live in **one shared config file** that both stacks read, so
    the Python and React stacks stay in sync, including in a repo initialized with
    only one of them.

## Design, per skill

### resolve-pr-concerns — helpers (`scripts/`)

All helpers live in `.agents/skills/resolve-pr-concerns/scripts/`, run as
`python3 <skill-dir>/scripts/<name>.py`, import only the stdlib and each other, and
keep canonical's conventions (NumPy docstrings, `# nosec` annotations on
subprocess, explicit raises instead of control-flow `assert`, `NoActiveReviewer`).

| Helper | Change |
|---|---|
| `reviewer_state.py` | Port downstream's additions onto canonical: incremental-review outcome (`not_tracked/not_requested/pending/finished/no_changes/failed/not_completed`, lagging-reply rule), `review_running` and on-head dating by the head's first commit status, finished-request-window verdicts (a verdict-less review passes only inside a finished window), verdict carry (`no_changes` and `same_patch` via a patch fingerprint and base-at-review resolution), PR round budget (`round_budget`: before/after-final `{used,cap}`, `trigger_allowed`, `final_review_due`, `max_fix_iterations`, CLI overrides), `unthreaded_findings`, list-valued `reviewed_sha_pattern`, `min_remaining`, and the paginated `_gh_json`. Keep `"active": null` and `load_round_caps`' type guard. |
| `local_review.py` | Capacity-aware start failure (`exhausted` only when a re-read is under `min_remaining`), recheck wait, best-effort post-round re-read, `remaining_before/after` in the round record. The vendor start-failure string becomes an optional adapter key (`local_review.start_failure_pattern`), not a literal. |
| `review_preflight.py` | `min_remaining` gating and the unreadable-count note. Keep canonical's "never gates a PR request" docstring and "round" wording. |
| `review_gate.py` | Running review → `pending`; failed/not-completed request text; carried verdict → success. Plus the CODEOWNERS ownership half (below). |
| `path_owners.py` (new) | CODEOWNERS parser + ownership status, from downstream. |
| `human_review.py` (replaces `.sh`) | Downstream's python port (multi-hop reply chains, importable API), plus canonical's config behavior: no default config → only GitHub Bot accounts excluded; explicit missing/invalid `--config` or usage error → exit 64; `-h`. CLI becomes `--repo <owner>/<repo> --pr <n> [--config]`. |
| `wait_for_pr_checks.py` (replaces `.sh`) | Same contract and exit codes as today (0/1/2/64 + the `"active": null` CI-only wait, `--config`, soft reviewer checks, post-settle review-of-HEAD poll, the fail-loud UNRESOLVED REVIEW FINDINGS list, cancelled review-gate exemption), plus downstream's merge-state preflight (**exit 3** on CONFLICTING/DIRTY, BEHIND warns, UNKNOWN polled), the reviewer_state soft-wait cutoff and the `REVIEWER:` report line, and rejecting an active adapter with empty `bot_logins` (exit 64). Calls `reviewer_state` in-process or as a sibling, never via another skill. |
| `merge_gate.py` (new) | All-blockers merge-readiness report, generalized (below). |

**CODEOWNERS gate (invisible until needed).** `review_gate.py` reads CODEOWNERS
from the default branch at the standard GitHub locations (`.github/CODEOWNERS`,
`CODEOWNERS`, `docs/CODEOWNERS`). No file → the ownership half does nothing and the
status is exactly today's (including *no status at all* when no reviewer is
configured). With a file: a PR touching a path its author doesn't own needs that
path's owner to approve the head; the status reads `@<owner> must approve: <path>`.
This applies whether or not a reviewer is configured: with `"active": null` and a
CODEOWNERS file, `review-gate` posts an ownership-only status. Team (`@org/team`)
and email owners can't be resolved with the workflow token, and the gate must never
crash on them. *Builder's call (marked):* any CODEOWNERS line that names a team or
email owner (alone or mixed with users) is left to GitHub's own "Require review from
Code Owners" rule and skipped by this gate, so a team member's approval is never
wrongly rejected; the reviewer notes say so.

**Merge gate, generalized.** Reports every blocker at once: required checks not
green, reviewer verdict/final review/open findings, a human's standing changes-requested, unresolved threads, a blocking `mergeStateStatus` (BEHIND only while the ruleset requires up-to-date branches),
and, with `--stack`, every layer of the stack. *Builder's call (marked):* unthreaded
findings are **listed for information**, not a blocker, because they have no stable
per-finding id to key a disposition on; the single disposition comment remains the
agent's Step 7 check. Required checks are read live from
the base branch's rules (`repos/{o}/{r}/rules/branches/{base}`); with no
required-checks rule, every reported check must be non-failing. No hard-coded check
names or ruleset ids. `"active": null` → the reviewer half is skipped. Output is a
report; nothing in it merges or authorizes a merge. *Builder's call (marked):* the
downstream `--has-open-park` flag is dropped (autonomous-loop concept); the
acceptance audit stays build-from-issue's gate, so `--acceptance-audit-clean` is
optional and, when given false, adds a blocker.

**Config** (`.github/pr-reviewer.json`, still `"active": null`): add the
incremental-review patterns, `unthreaded_finding_patterns`, `reviewed_sha_pattern`
as a list, `local_preflight.min_remaining`, `local_review.start_failure_pattern`,
and `review_rounds.max_incremental_before_final` / `max_incremental_after_final` /
`max_fix_iterations`, with values for both shipped adapters. Refresh the CodeRabbit
adapter for its current output: refused full reviews ("included review limit…"
without "Action performed") are refusals, not failures; the post-2026-09
walkthrough format (no clean line, new reviewed-commit markers); the `CodeRabbit`
signal is a commit status, not a check run (hedge: `gh pr checks` lists both).
Bugbot: the `BUGBOT_REVIEW_STALE` superseded-summary note.

### resolve-pr-concerns — prose (`SKILL.md`, `references/`)

Port, vendor-neutral: the reviewer poll-loop contract (poll `reviewer_state`, stop
on any terminal reply, 60-minute `pending` timeout handed to the user); act on
`incremental_review_outcome`; PR review-round budget (fix after the before-final
budget is validated by the final review; budget spent → answer remaining findings,
never trigger past the cap); "an empty live set is not proof the reviewer ran";
"inline threads are not evidence HEAD was reviewed"; unthreaded findings answered
in **one** top-level disposition comment, checked in Step 7; check the merge state
before any wait (exit 3); merge, don't rebase, on a PR with review history; verdict
carry → act on `needs_trigger`, not "the SHA changed"; per-push trigger check waits
for the new head's first status and stays in comment mode once a comment was
needed; structural enforcement (`review-gate` is required, so the trigger comment is
a speed optimization); no silent review skip on trivial PRs (the only skip is a
human's audited ruleset bypass); don't push between posting the full review and its
reply's final edit; poll-filter discipline; `min_remaining`; merge-ready requires
actual terminal check results, never a prediction from a local run; a conditional
coaching paragraph for repos that have a per-test speed guard; stacked-PR mode
(below); the merge gate in Step 7's summary.

Keep canonical where downstream conflicts: one reviewer trigger per push covering
every fix (not one per finding), with the principle "a fix is verified once a review
of a head containing it is clean, or the final review / a local round covered it";
the hidden answer-marker model for the agent's own replies (not login filtering);
never require the vendor's own check; the final full review exactly once per PR.

All helper invocations change to the bundled python paths; `human_review.sh` and
`wait_for_pr_checks.sh` references are updated everywhere (skills, rules, settings
allow-lists, README, tests).

### Stacked PRs (build-from-issue + resolve-pr-concerns)

- **Policy** (one PR is the default; the author decides): build-from-issue offers a
  stack at plan review when the plan is complex enough that one review would likely
  miss related changes. Rough signals, not rules: several separately reviewable
  concerns, or a large expected diff. Hard limits: ≤ 4 layers, every layer from the
  same planned card, < 300 files changed across the whole stack (beyond that, split
  the card via `/plan-issues`, don't stack harder). Not for docs/config-only work,
  hotfixes, or a change that can't be split into layers that are each green.
- **Tooling:** GitHub's `gh stack` CLI extension (`github/gh-stack`, MIT), plus its
  agent skill (`npx skills add github/gh-stack`). The template has no tool-install
  machinery (its init preflight only checks that `gh` is on PATH and tells the user
  to install it), so installing `gh stack` stays **manual** (decision 12): when
  build-from-issue is about to offer a stack it checks `gh stack --help`; if the
  extension is missing it says so plainly and prints the exact install commands for
  the user to run. The agent never installs anything on the user's machine itself.
  The third-party skill is not vendored.
- **References, each skill owning its own copy of what it needs** (no cross-skill
  paths): `build-from-issue/references/stacked-prs.md` (when to stack, splitting,
  the plan's `## Stack` section, building, finalize: `make pr_check` on the top
  layer + `make lint` per lower layer, `gh stack submit`, per-layer PR bodies with
  Refs/Closes) and `resolve-pr-concerns/references/stacked-prs.md` (detecting a
  stack, driving each layer bottom-up with its own final review, fixes on the
  owning layer, never merging `main` into a layer and never `gh pr merge` on a
  layer, CI on a stack, `merge_gate.py --stack`, landing with `gh stack merge` by a
  human or with one-off permission). Generalized from the downstream reference;
  its pilot data and issue numbers are dropped, keeping the reasoning.
- **Merge queue optional** (decision 9). Local finalize runs `make pr_check` on the
  top layer (it contains every change); each layer's PR runs full CI. Landing: with
  a queue, `gh stack merge` enqueues the stack and the queue tests each layer
  against the latest `main`; without one, the reference covers the retarget gotchas
  (a merged layer's successor must be rebased onto the new base before it lands).
  `merge_gate.py` treats `BEHIND` as a blocker only while the ruleset requires
  up-to-date branches, which a queue replaces.

### build-from-issue (`SKILL.md`)

Port: define current branch / branch-derived / **target** issue number (a number
the user names wins); always push + open a PR instead of the finishing menu, with
two narrow exceptions (user explicitly asks otherwise for this branch; a green suite
is unobtainable → stop and report), and fix the diagram edge that contradicts it;
`Refs #N` (not `Closes #N`) for a prerequisite or partial PR; local CI-mirror gate
(`make pr_check`) before the PR exists; merge-ready only from actual check results;
actually invoke `resolve-pr-concerns`; list the spec's 3–5 judgment calls with the
review link; pick 5a vs 5b yourself and say why the other was rejected; commit the
live-E2E test kit to the repo (`docs/superpowers/test-kits/…`), publishing it as a
rendered artifact optional; offer `explain-diff-merge-review` alongside the kit;
Slack momentum post off by default (only when the user asks this session and the
repo has a channel convention), keeping canonical's two-bracket format; audit prose
acceptance criteria too and post the audit table as an issue comment rather than
rewriting the card into checkboxes; check whether the fix is already on
`origin/main` by reading code, not commit ancestry; surface an open question the
card poses instead of silently picking; dual-stack E2E discovery (drop the leftover
product target name); AGENTS.md stays lean (prefer a rule or reference); canonical
`.agents/skills/` paths; checkpoint-commits citation; new red flags / common
mistakes for the above; stacked-PR steps (offer at plan review, build per layer,
finalize per layer, resolve in stack mode).

Drop: product-specific steps (schema diagrams, observability check-ins, connector
coverage), the changelog step, vendor-specific pre-PR CLI review, real issue numbers
and logins.

### plan-issues (`SKILL.md`) and clean-up-kanban (`SKILL.md`)

plan-issues: add the missing **Phase 3 step 0** existing-card overlap gate
(canonical already cites it); the existing-card-first and epic-body-is-the-map core
rules; refresh the epic narrative after nesting (un-nest first if the child doesn't
cohere); the "self-contained story" sentence while keeping canonical's conditional
`Stage` field; a REST-only fallback when `gh` GraphQL is refused (detect once, swap
in a substitution table); the dependency-cycle gotcha (delete the old blocked-by
edge before wiring the reverse); board auto-add awareness; "nested J of N" added to
the partial-success line (keeping "attached L of N"); one card = one independently
mergeable PR, and don't decompose a one-PR project; never move a card to In
Progress (build-from-issue owns that); fix the harness-specific clean-up-kanban
path. Remove the orphaned tracker-routing remnants (→ #48).

clean-up-kanban: the paginated child-count fix (`sub_issues_summary.total`), links
to the push-every-commit and skill-sync rule files.

### review-skill

`python3 scripts/validate.py` (stdlib; `allowed-tools: Bash(python3:*)`). Add to the
validator: an **external-reference check** that fails when SKILL.md tells the agent
to run a script outside the skill's folder (this mechanically enforces goal 3;
*builder's call, marked:* prose pointers to a sibling skill's `SKILL.md`, as in
"read clean-up-kanban's SKILL.md", stay allowed); shebang-aware executable checks
(importable sibling modules need no `+x`); `--json` report output; type hints and
NumPy docstrings. New tests cover these, plus a smoke test that every skill under
`.agents/skills/` passes with no FAIL.

### pr-check, Makefiles, docs

- Root `Makefile`: `pr_check: lint machinery_tests coverage test_speed_check security`.
  Each stack `Makefile`: `pr_check: lint coverage test_speed_check security`, where
  the speed check reads the JUnit file coverage just wrote. `make tests` stays the
  fast iteration target (not the gate).
- pr-check skill: describes the components and says it mirrors CI; reports
  coverage shortfalls, speed-guard offenders (with the remedy order) and security
  findings.
- AGENTS.md quality-gates table, README, and any init-template text that describes
  `pr_check` are updated to match.

### Per-test speed guard (both stacks)

Downstream's guard, generalized to both stacks and to a fresh repo.

- **What it does.** After the coverage run, read the suite's JUnit XML and fail if any
  test not on the allowlist took longer than the budget. To stop false alarms from a
  busy, parallel run, every test over the budget (or within `recheck_fraction` of it,
  since sharing work can hide a slow test) is **re-run alone, one at a time**, and
  fails only if it is still over the budget when run alone. An allowlisted test that has become fast gets a
  warning so the list shrinks over time (a ratchet). On a failure, print the
  remedies in order: speed the test up (usually by mocking a slow dependency), move
  it to a slower test group, and only then add it to the allowlist with a reason and a
  tracking issue.
- **Dynamic budget.** The budget means "seconds on a typical runner of this class".
  At start, the guard times a fixed pure-Python benchmark (best of 5) and divides
  by the committed typical time for its runner class (`TEST_SPEED_RUNNER_CLASS`,
  default `ubuntu-latest`, the template's CI runner), clamped to 0.5–2×. A faster
  machine (a laptop) gets a proportionally tighter budget, and a slow CI instance a
  looser one. An unknown runner class fails loudly and says how to add it (the
  guard prints its own measurement every run, so seeding a class is one line). The
  `ubuntu-latest` value is seeded from this PR's own CI run. `--factor` pins the
  factor for debugging.
- **One config, both stacks.** A single shared file holds `budget_seconds = 10`,
  `recheck_fraction`, the factor clamp, and the per-runner-class typical times. It
  lives outside `stacks/` (*builder's call, marked:* a `conf/test-speed.toml` that
  `make init` keeps for either stack), so changing the budget is a one-line edit that
  applies to Python and React alike, and survives initialization with one stack.
- **Where the code lives.** One stack-neutral, stdlib-`python3` script pair (the
  checker and the runner-speed calibrator) outside `stacks/`, kept by `make init`
  for either stack (as `scripts/apply_repo_settings.py` is today). It parses either
  test runner's JUnit XML. Each stack supplies only what is genuinely stack-specific
  through its Makefile: how to emit JUnit from the coverage run (pytest
  `--junitxml`, Vitest's JUnit reporter), the command that re-runs one test alone,
  and its own allowlist file (empty at first).
- **Wiring.** Each stack gets `make test_speed_check`, which reads the last coverage
  run's JUnit file. `make pr_check` becomes lint + coverage + speed check + security,
  and CI runs the speed check right after coverage in each stack's tests job on PRs
  and merge-queue runs, but not on pushes to `main`, where it would only re-run
  flakes. The template's own CI checks that the speed guard is wired into both
  stacks.
- **Python extra.** Downstream's pytest plugin that records shared-fixture setup on
  the test that paid for it comes along for the Python stack, so a session-scoped
  fixture's one-time cost isn't blamed on one unlucky test. Shared setup has its own
  budget in the same config. Vitest has no equivalent hook, so React tests are judged
  on their full time (noted in the config).
- **Not ported:** the downstream "slow lane" (a second 90-second budget for
  slow-marked tests) and its wall-clock ranking report. The template has no slow lane.
- **Known approximation.** The benchmark is Python, and for a React repo it stands in
  for single-thread CPU speed. That is good enough to scale a budget, and the config
  notes it.
- resolve-pr-concerns' coaching for a failing speed check is ported unconditionally,
  since every repo built from the template now has the guard.

### Tests (`tests/template/`)

Port downstream's tests for every new capability, rewritten against neutral data
(`owner/repo`, neutral logins, tmp CODEOWNERS fixtures, a synthetic reviewer-state
timeline that reproduces the orderings the replay tests assert). Keep every
canonical-only test (no-reviewer, rate-limit, local-round recovery). Port the
existing bash-script tests to the python helpers. Add tests for the speed guard (budget scaling and clamp, re-measure verdicts,
allowlist ratchet, both JUnit dialects, unknown runner class, the one-config
invariant), exit 3, the merge
gate (configurable required checks, `--stack`), path owners (incl. team owners
skipped, no file → no gate), and the validator. Extend the seam test's field list.
Add a guard test that scans `.agents/skills/` and the new fixtures for the
downstream org/product/repo names and logins, so goal 2 stays true. The denylist is
stored as **SHA-256 hashes** of lower-cased tokens (decision 11); the test
tokenizes each file (splitting on non-alphanumerics, and also checking adjacent
token pairs joined by `-`/`_`/nothing so multi-word names match) and compares
hashes, so no name is published in plain text.

## Constraints

- PR #55 (merge queue default) touches `AGENTS.md`, `README.md` and
  `.github/repo-settings/ruleset.json`; this branch merges `main` in once #55 lands
  and reconciles the `AGENTS.md` quality-gates and PR-guidelines text.
- Harness-agnostic skills (`.agents/rules/shared/harness-agnostic-skills.md`):
  skill-root-relative bundled paths, `.agents/skills/` as canonical, no mandatory
  Claude-only tool.
- The seam test forbids vendor names in build-from-issue and in the seam files, and
  requires build-from-issue to keep naming `local_review.py` / `review_preflight.py`.
- Repo root allowlist (`test_root_hygiene.py`); squash-merge only; push every commit.

## Left to the builder

Task ordering, how to split the reviewer_state port into reviewable commits, exact
wording of ported prose, test names, the synthetic fixture's content (structure must
cover the markers listed in the inventory), and anything marked *builder's call*
above.
