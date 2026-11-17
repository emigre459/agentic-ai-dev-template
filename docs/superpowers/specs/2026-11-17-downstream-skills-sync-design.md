# Downstream skills sync — design (#66)

Issue: #66 · Epic: #51 · Builds on: #47 (PR #56, the last catch-up, merged
2026-10-29), #48 (extension points), #49 (consumer kit).

## Intent and why

Three downstream repos are about to stop hand-maintaining their dev-workflow skills
and install them from this template instead (lockfile, nightly update PR, drift
check). They still carry skills and skill changes this template lacks, so switching
today would roll them back. This change brings every downstream skill change into
the template, generalized, keeps everything the template added since, and ends with
a release (`v0.1.0`) the downstream repos pin.

In this document the downstream repos are **downstream A** (the backend repo; its
skills lead every other copy), **downstream B** (a frontend repo) and **downstream C**
(an internal starter template). They are never named in tracked files
(`tests/template/test_no_org_names.py`).

## Goals

1. Every downstream change to a shared skill since that skill last synced is in the
   template, generalized — or recorded as left out because it is product-specific.
2. Downstream A's generic-only skills are ported, folded, or recorded as staying
   local with a reason.
3. Every skill in all three downstream repos is classified in the PR body: ported,
   stays repo-local, or third-party. Product-specific skills are counted, not named.
4. No organization, product or private-repo name in any tracked file;
   `make pr_check` green.
5. After merge, `v0.1.0` is published (the `release` skill, with the user's OK).

## Non-goals

- Switching the downstream repos over (their own cards, each blocked by #66). Their
  cards were amended on 2026-11-17 to adopt the vendored-skills rule and to make
  `skills drift` a required check.
- `work-the-backlog` (deferred by the user on 2026-11-17; a follow-on card is filed
  at the end of the build).
- The 2026-11-11 amendment to #49 (App/PAT token for the update PR, a skills-only
  `review-gate` lane, auto-merge): downstream A has not built it either. It is
  noted at the momentum step as a possible follow-on, not built here.

## Anti-goals (failure even if it "works")

- Any template-only advance removed or weakened (stacked PRs, the review helpers in
  `resolve-pr-concerns/scripts/`, the consumer kit, the branch-name gate, the
  `**Repo config first:**` preflights, the harness-agnostic paths).
- A downstream product rule copied into a shared skill (tracker routing to a second
  tracker, data-layer card labels, merge-queue lane names, named people or channels).
- A skill that only works in one harness, or that runs a script outside its own
  folder (`review-skill`'s validator fails that).
- New `make` targets. The user wants scripts instead (2026-11-17).

## Approaches considered

**Reconciliation method** (user picked 1):

1. **Per-commit record plus a final diff check (chosen).** For each shared skill,
   list every downstream commit since that skill last synced and mark each one
   ported / generalized / left out (with the reason). Then diff the result against
   downstream A's `main` and account for every remaining difference as either
   "template ahead" or "product-specific". The record proves "matches downstream
   A's `main`" without demanding file equality.
2. Today's diff only, deciding direction per hunk with `git blame`. Faster to start,
   but easy to get the direction wrong wherever both sides edited one paragraph.
3. Take downstream wholesale, then re-apply template additions. Contradicts the
   union reading the user approved; churns every file and risks losing template work.

**Notebook tooling** (user picked A, with scripts instead of make targets): the
template's Python stack owns the notebook tooling (an opt-in dependency group), and
the check script and starter notebook ship inside the skill. The rejected
alternative (B) was skill prose only, relying on whatever a repo already has, which
leaves a fresh repo unable to follow the skill.

**Folding `cleanup-update-prs` into `upgrade-deps`** (user picked 1):

1. **Two modes in upgrade-deps (chosen):** *sweep* (the downstream policy) and
   *refresh* (today's bump-everything).
2. Downstream policy only, dropping bump-everything.
3. Keep upgrade-deps, port only the post-merge closing — rejected: downstream A would
   lose its close-routine-bumps behavior on switch-over.

**Downstream changes #47 declined** (user, 2026-11-17: port both now). #47 left out
downstream #2530 (the "light check": a scoped pre-PR check for low-risk diffs) and
did not carry #2623 (a machine-wide single-flight lock on `pr_check`). Neither is
product-specific, so leaving them out would roll downstream A back on switch-over.
Alternatives were keeping them declined, or porting only the light check.

**Docs-only review lane default** (user, 2026-11-17: opt-in, and the README setup
interview asks). Shipping the allowlist enabled was rejected: it would skip review
on README and `docs/` edits in every new repo by default, which weakens a gate.

## Design

### 1. The reconciliation record (approach 1)

Built first, before any port, as `docs/superpowers/specs/2026-11-17-downstream-skills-sync-ledger.md`
(committed; the PR body summarizes it).

- **Sync points.** For the six skills #56 covered (build-from-issue, plan-issues,
  clean-up-kanban, resolve-pr-concerns, review-skill, pr-check): downstream commits
  since **2026-10-20**, checking that each commit from 2026-10-20 to 2026-10-29 was
  already carried by #56 (it may not have been). For explain-diff and
  explain-diff-merge-review (#56 didn't cover them): since **2026-08-11**, when the
  template added them. For downstream B and C: every commit touching their skill
  folders since **2026-10-01**.
- **Deepen the downstream clones first.** The scratch clones are shallow (A's
  history starts 2026-09-30; C has one commit). Fetch full history for the skill
  paths before building the record.
- **One row per downstream commit:** downstream PR number, skill, a one-line
  summary, the verdict (`ported` / `generalized` / `left out: product-specific` /
  `already in template` / `declined in #47, ported now`), and the template commit
  that carries it.
- **resolve-pr-concerns' helpers count as part of that skill.** In downstream A
  they live in `.claude/skills/work-the-backlog/scripts/` (`review_gate.py`,
  `reviewer_state.py`, `merge_gate.py`, `human_review.py`, `path_owners.py`,
  `local_review.py`, `review_preflight.py`); the template keeps them in
  `resolve-pr-concerns/scripts/`. All seven are in the record and the residual
  diff (e.g. downstream #2603, #2496 as well as #2845, #2903), even though
  `work-the-backlog` itself is deferred.
- **Residual diff check:** after the ports, diff each shared skill against
  downstream A's `main` (and downstream B's `pr-check`, which A lacks). Every
  remaining hunk is labeled `template ahead` (cite the template PR) or
  `product-specific`. An unexplained hunk is a gap: port it.
- Downstream A's skills live under `.claude/skills/`; the template's under
  `.agents/skills/`. Path rewrites (`.claude/skills/x` → the skill-root-relative
  form, `.agentrules/` → `.agents/rules/`) count as generalization, not drift.

Known post-sync downstream changes to shared skills (the record must confirm the
list is complete):

| Downstream PR | Skill | Change | Expected verdict |
|---|---|---|---|
| #2847, #2896 | build-from-issue | Step 7b ships a teaching notebook with the live kit | generalized (§3) |
| #2745 | build-from-issue | Stacked finalize: names the merge-queue's slow/pdf/DB lanes | left out: product lane names; keep the generic sentence |
| #2917 | build-from-issue | Rename launcher-named branches | already in template (#67) |
| #2845 | resolve-pr-concerns | Docs-only PRs pass `review-gate` without a review | generalized (§2) |
| #2903 | resolve-pr-concerns | `review-gate` stops counting a local reviewer's open threads; pointer to the sweep skill | generalized (§2) |
| #2530 (pre-sync) | build-from-issue, resolve-pr-concerns | Light check: scoped pre-PR check for low-risk diffs | declined in #47, ported now (§2b) |
| #2623 (pre-sync) | build-from-issue, resolve-pr-concerns | Single-flight lock: one `pr_check` per machine | declined in #47, ported now (§2b) |
| #2603, #2496 (pre-sync) | resolve-pr-concerns helpers | Human-review answering, path owners | expected: already in template (#46, #56); the record confirms |
| #2805 | clean-up-kanban | Data-layer labels on parity cards; re-import rule | left out: product-specific |
| #1837, #2012 (pre-sync) | explain-diff, explain-diff-merge-review | Lint and docstring fixes in `render.py` / `flag_sensitive_paths.py` | ported where they fix behavior or lint; docstring style follows the template's convention |

### 2. resolve-pr-concerns

- **Docs-only lane, opt-in.** `scripts/review_gate.py` gains `load_docs_allowlist`
  and `docs_only_status`: once no path owner's approval is missing, a PR whose every
  changed and renamed-from path is in the allowlist passes as
  `docs-only: no review required`. Fail closed: a missing or malformed
  `.github/docs-only-paths.json`, or a capped file list, means no PR is docs-only.
  The allowlist is located as `parents[4] / ".github/docs-only-paths.json"` from
  the script and read from the default-branch checkout that
  `.github/workflows/review-gate.yml:60-73` already makes.
  - **The template ships no `.github/docs-only-paths.json`.** The example ships in
    the skill as `references/docs-only-paths.example.json` (downstream's schema:
    `dirs`, `dir_suffixes`, `top_level_suffix`, `excluded_names`; docs/ prose,
    tables and raster images, top-level `*.md`, never `AGENTS.md`/`CLAUDE.md`; its
    `description` rewritten without downstream paths), so skills-only consumers
    get it too.
  - **README setup interview** gains a question (after the stack questions):
    "Should a PR that only touches docs (docs/ prose, tables and images, top-level
    Markdown other than AGENTS.md/CLAUDE.md) pass review-gate without a PR review?
    Default: no." On yes, the setup steps copy the example to
    `.github/docs-only-paths.json` before the initialization commit. The README's
    "Customizing the skills" section documents the file for later opt-in.
    `tests/template/test_readme_workflow.py` pins the question.
  - SKILL.md Step 5b gets the docs-only paragraph ("when the repo has opted in"),
    without the downstream maintainer's name: "the maintainer's ruleset bypass".
- **Local reviewer threads.** `review_gate.py` no longer requires a local reviewer's
  threads to be resolved. The ruleset's required conversation resolution blocks the
  merge instead (resolving a thread fires no Actions event, so the gate lagged).
  The template's ruleset already requires it (`.github/repo-settings/ruleset.json:18`,
  applied at bootstrap). Update SKILL.md's overview bullet, "Local review rounds",
  and the CodeRabbit adapter's matching paragraph.
- **Step 2e** points at `upgrade-deps` (sweep mode) for a repo-wide sweep of update PRs.
- Tests: extend `tests/template/test_review_gate.py` (docs-only pass, each
  fail-closed case including the file being absent, renamed-from path outside the
  list, `AGENTS.md`/`CLAUDE.md` at any depth, path-owner approval still required
  first, local-reviewer threads no longer counted). A test asserts the template
  does not ship `.github/docs-only-paths.json` and that the example parses.

### 2b. The light check and the single-flight lock (declined in #47, ported now)

**Light check** — new rule `.agents/rules/shared/pr-check-scope.md`
(`alwaysApply: true`), generalized from downstream #2530:

- **Full `make pr_check` before the PR** when the diff touches the stack's source
  (`src/`), a manifest or lockfile (`pyproject.toml`, `uv.lock`, `package.json`,
  `bun.lock`), the `Makefile`, CI (`.github/workflows/`, `.github/actions/`), the
  test or lint configuration, or shared test infrastructure (any `conftest.py`, a
  test helper other tests import, a Vitest setup file). In the template repo, also
  `scripts/` and `tests/template/conftest.py`.
- **Otherwise the light check:** `make lint`, then the test files covering the
  change; add `make security` when the diff changes Python under `scripts/` or a
  skill's `scripts/`. Then push, open the PR, and wait for every required status
  with `resolve-pr-concerns`' `wait_for_pr_checks.py`. A "ready" claim cites those
  actual results. When unsure, run the full check.
- **The hard-stop wording changes with it.** Every skill's preflight says no file
  can lift "opening or calling ready a PR without a green `make pr_check` and CI".
  It becomes "…without the pre-PR check `pr-check-scope` requires (the full
  `make pr_check` unless the diff qualifies for the light check) and green CI".
  Same edit in `sync-core-dev-skills-upstream.md`, build-from-issue Step 11 (gate,
  red flag, common-mistakes row) and resolve-pr-concerns' re-run step.
- AGENTS.md: one Rules-table row, and the Quality Gates sentence names the light
  check.

**Single-flight lock** — from downstream #2623, as a script bundled in
`resolve-pr-concerns/scripts/single_flight.py` (stdlib; no Makefile change, no new
target):

- `python3 <skill-dir>/scripts/single_flight.py <name> -- <command…>` runs the
  command under an exclusive lock on `${SINGLE_FLIGHT_DIR:-/tmp}/<repo>-<name>.lock`
  (`fcntl.flock`; where `fcntl` is missing, it runs unlocked and warns). While
  waiting it prints who holds the lock (a sidecar holder file) and propagates the
  command's exit status.
- `CI=true` or `SINGLE_FLIGHT=0` bypasses it. A nested call on a lock its parent
  already holds runs directly (an env marker). The lock lives in a fixed directory,
  not `$TMPDIR`, because sandboxed sessions get their own `$TMPDIR`.
- Use the same lock path and marker as downstream's `single_flight.sh`, so a
  downstream Makefile that already locks and a skill that wraps it don't deadlock.
- build-from-issue Step 11 and resolve-pr-concerns run the full check as
  `single_flight.py suite -- make pr_check`.
- The "top layer only" half of #2623 is already in the template (stacked finalize).
- Tests: `tests/template/test_single_flight.py`: serialization across two
  processes, bypasses, nested run, exit-status propagation, the holder message.
  It uses a tmp `SINGLE_FLIGHT_DIR` and stays fast.

### 3. Teaching notebook (Python stack only)

**New skill `write-teaching-notebook`** (`.agents/skills/write-teaching-notebook/`):

- `SKILL.md`: the `**Repo config first:**` preflight; then a first step that finds
  the repo's notebook tooling, in order: (1) `.agents/skill-config/write-teaching-notebook.md`
  names a `check command` and an `open command` (downstream A keeps its own
  `make notebook_check` and its stack's JupyterLab this way, so switching over
  rolls nothing back); (2) a `[dependency-groups] notebook` in the root
  `pyproject.toml` (the template default). With neither, it stops with a clear
  message. The structure is downstream's
  (what was asked; setup that prints every target; before and after first; one
  section per stage; calls we made; self-checks; what this doesn't show), the
  "never writes to a shared environment" rule, the explicit-code-style rules and the
  visual-output rules (`rich`, rendered tables, Plotly by default, WebGL or
  aggregation for big data, seaborn/matplotlib only for raw-point plots).
- **"Before" generalized:** `main`'s behavior on the same input, from the first of:
  (1) a read-only path the repo names in `.agents/skill-config/write-teaching-notebook.md`
  (e.g. a dev API or stored outputs); (2) `main`'s code run on the same input from a
  temporary `git worktree` of `origin/main`. Same-input assertion first; premise
  check (main still shows the problem); a cell that can't reach "before" raises,
  never falls back to synthetic data. Exceptions are stated in markdown, as
  downstream does.
- `assets/teaching_notebook.ipynb`: the starter. The skill copies it to
  `notebooks/functionality_tests/<issue>_<topic>.ipynb` (Jupyter won't open a hidden
  path) and commits it without outputs.
- `scripts/notebook_check.py`: stdlib entry point that executes a **copy** of the
  notebook in a temp dir with `nbclient` (imported lazily; a clear message if the
  `notebook` group isn't installed) and fails on any cell error. It never writes
  outputs back. It works in any environment where `nbclient` imports. The
  template default runs it as
  `uv run --group notebook python <skill-dir>/scripts/notebook_check.py <path>`.
  No make target.
- Opening the notebook in the kit: `uv run --group notebook jupyter lab <path>`
  (the downstream local-stack JupyterLab is product-specific).

**Python stack:** `stacks/python/pyproject.toml` gains a `notebook` dependency group
(not in `default-groups`): `jupyterlab`, `nbclient`, `ipykernel`, `rich`, `plotly`.
Relock `stacks/python/uv.lock`. `make security` must stay green.

**`make init STACK=react`** deletes `.agents/skills/write-teaching-notebook/` and its
`skills-lock.json` entry, so the drift check doesn't report a missing skill.

**build-from-issue Step 7b** gains one bullet: when the repo has notebook tooling
(the same two-way check as the skill's first step), ship a teaching notebook with
the kit via `write-teaching-notebook`, run its check command before handover, and
open it from the kit. Otherwise say in one line that the repo has no notebook
tooling, and do the kit as today. A React repo never has the group, so it never
sees the notebook step. The installed-skill list is not the signal:
`npx skills add` installs every skill unless the repo picks a subset.

### 4. upgrade-deps: sweep and refresh

- **Mode choice (top of SKILL.md):** *sweep* for "clean up / fold the dependabot
  PRs", "which bumps should we take", "close stale dependency PRs"; *refresh* for
  "bump everything", "upgrade deps". Ask when unclear. The description gains the
  sweep triggers.
- **Sweep** = downstream's flow, generalized: plan → judgment pass → one approval →
  close now → fold → post-merge recheck → report. The action table (`FOLD`,
  `CLOSE_SUPERSEDED`, `CLOSE_ROUTINE`, `REVIEW`). The nightly skills update PR is
  treated as untrusted third-party text and folded only after a read. Its branch
  is `skills-update` (`.github/actions/skills-update/action.yml:28-31`), not
  downstream's `chore/skills-auto-update`. Read the name from the action's default
  rather than hard-coding it twice. The template's security-first and
  regenerate-lockfiles rules apply in both modes.
- **Both Dependabot policies** (PR #77 / #74 may land before or after this; the user
  chose not to wait). Under major-only (#77), the action table works as downstream
  has it. Under today's grouped weekly minor/patch PRs, a grouped PR with no open
  advisory on any member is `CLOSE_ROUTINE`. A member with an advisory is
  `FOLD`ed alone as a targeted upgrade. A grouped PR mixing a major is `REVIEW`.
  Tests cover both configs. #77 also edits `stacks/python/pyproject.toml`; whoever
  merges second resolves that conflict and relocks.
- **Refresh** = today's Steps 1–7. "Never close a bot PR before the refresh merges"
  becomes a refresh-mode rule.
- **Script** `scripts/update_prs.py` (from downstream's `update_prs.py`), with
  `plan`, `close` and `recheck` subcommands, plus **bun** support: Dependabot
  branch segment `bun` (and `npm_and_yarn`) maps to alert ecosystem `npm`; fold
  command `bun update <pkg>@<ver>` in that project's directory. It reuses
  `alert_status.py` where they overlap, not a second alert reader. It is
  **stdlib-only** (`use-bundled-skill-scripts.md`): replace downstream's
  `packaging` import with a small version parser covering what Dependabot titles
  and alerts carry, and `canonicalize_name` with PEP 503 normalization. Projects
  come from each `directory` in `.github/dependabot.yml`, not from a root
  `pyproject.toml` (a bun repo has none). Downstream's floor-bump rule has no
  template equivalent: inline it as a sentence (raise the manifest floor to the
  fixed version when the lock alone can't hold it, as Step 2.2 already says).
  Other paths are generalized (`.agentrules/…` → `.agents/rules/…`; the issue
  search uses the current repo). The background-wait recheck stays (no hard-coded sleeps beyond the
  timeout and grace, per `no-hardcoded-waits`).
- **Close rules:** closing is the user's call (one approval covers the plan's
  closes, the fold PR and post-merge closes), and it is always close with a
  comment, never delete.
- Tests: `tests/template/test_upgrade_deps_update_prs.py` (classification per
  action, the bun mapping, `close` refusing a PR whose state changed, `recheck`
  exit codes 0/2/3/4), with `gh` mocked. No network.

### 5. update-project-memory (ported)

- Generalized: `.agentrules/` → `.agents/rules/`; the routing list names this
  template's homes (a rule in `.agents/rules/`, a skill's `references/`, `docs/`).
- `scripts/check_budget.py` (stdlib; caps are CLI args). Default caps leave room
  above the template's own `AGENTS.md` (109 lines and 6.3k chars before
  `make init`, plus §2b's routing row): **soft 120 lines, hard 150 lines / 9,000
  chars** (my call). A repo sets its own
  caps in `.agents/skill-config/update-project-memory.md` (downstream A: 50/75/4,200).
- `tests/template/test_agents_md_budget.py` runs the script over `AGENTS.md` with
  the defaults. build-from-issue Step 9 points at the skill for any `AGENTS.md` edit.
- Step 1 (re-read the agents.md standard) stays, as an agent action, not a test.

### 6. daily-update (ported)

- Generalized: the repo from `gh repo view`; the board from
  `.agents/skill-config/daily-update.md` (project owner/number), otherwise the
  repo's linked project, otherwise no board section. Display names from
  `gh api users/<login>`, no hard-coded people.
- State file per user: `${XDG_STATE_HOME:-~/.local/state}/daily-update/<owner>-<repo>/<login>.json`.
- Slack posting only when the user asks this session, to a channel they name or the
  one in skill-config. No default channel.
- `scripts/daily_update.py` generalized with the same contracts. `evals/evals.json`
  ported if its cases are generic, rewritten otherwise.
- Tests: `tests/template/test_daily_update.py` (window from state, user detection,
  classification), `gh` mocked.

### 7. The vendored-skills rule

`.agents/rules/shared/sync-core-dev-skills-upstream.md`: `appliesTo` widened to
`.agents/skills/**` and `.claude/skills/**`. The opening is sharpened: a vendored
skill is consumed, pinned in `skills-lock.json` and replaced on update, so stop and
send the change upstream as a PR; repo needs go in the extension points. The
consumer cards copy this file.

### 8. Shared plumbing

- Every new skill opens with the `**Repo config first:**` preflight
  (`test_skill_extension_points.py`) and passes `review-skill`'s validator.
- `skills-lock.json` regenerated (`node .github/actions/skills-drift/skills-lock.mjs write --root .`).
- README: the skills list and "Customizing the skills" mention the new skills and
  their config files. AGENTS.md: only if a routing line is needed, within budget.
- `test_no_org_names.py` must pass over every new file. Downstream PR numbers may
  appear; downstream names may not.

### 9. PR body: classification

One table per downstream repo: each skill → `ported` / `generalized into <skill>` /
`stays repo-local (<reason>)` / `third-party (installed from its own upstream)`.
Product-specific skills are one counted line per repo ("N product-specific skills
stay repo-local"). Expected for downstream A: 7 shared synced;
`write-teaching-notebook`, `update-project-memory` and `daily-update` ported;
`cleanup-update-prs` folded into `upgrade-deps`; `work-the-backlog` stays local
(deferred, follow-on card); `standup-kanban-sync` stays local (bound to one team's
standup, a meeting-notes service and a chat channel); `langfuse-check-in` stays
local (one observability vendor and its schema); `daily-update-workspace` stays
local (no committed skill, only a `.gitignore`); 2 third-party; the rest counted as
product-specific. Downstream B and C: every skill already in the template.

### 10. Release after merge

The template has no published release (a `v0.0.1` draft only) and no
`major`/`minor`/`patch` labels in the repo yet (they are defined in `labels.json`).
release-drafter defaults to a patch bump and, with no baseline, drafts `v0.0.1`.
So:

- Before the PR opens, create the labels from `labels.json` (`gh label create`,
  with the user's OK) and label the PR `minor`.
- After merge, the `release` skill publishes it, only on the user's explicit OK.
  If the draft still reads `v0.0.1`, ask whether to set a `v0.0.0` baseline or
  edit the draft to `v0.1.0`. Then offer the template's own re-pin PR (the
  `release` skill does this in the template repo).

## Left to the builder

- Task order and slicing (the record first; then one commit per area above).
- Exact prose, as long as it follows `harness-agnostic-skills.md` and the anti-goals.
- How `update_prs.py` shares code with `alert_status.py`.
- Notebook starter cell contents, within §3's structure.

## Calls made without the user (check these)

- Sync-point dates (2026-10-20 for #56's skills, 2026-08-11 for the explain-diff
  pair, 2026-10-01 for B and C).
- `update-project-memory` default caps of 120/150 lines and 9,000 chars, so the
  template's own `AGENTS.md` passes with the new routing row.
- Light-check scope for this template (§2b), and the hard-stop wording changing
  from "a green `make pr_check`" to "the check `pr-check-scope` requires".
- The single-flight lock as a skill-bundled Python script that matches downstream's
  lock path, so the two interoperate.
- Sweep mode closing a grouped minor/patch PR with no advisory as `CLOSE_ROUTINE`
  while the template still groups them (pre-#77).
- Notebook tooling found through skill-config first, then the `notebook` group.
- "Before" in a teaching notebook falls back to `main`'s code in a temporary
  worktree when the repo names no read-only path.
- The `notebook` group's contents (`jupyterlab`, `nbclient`, `ipykernel`, `rich`,
  `plotly`).
- Docs-only allowlist copied as-is (docs/ prose, tables and raster images; top-level
  `*.md`; never `AGENTS.md`/`CLAUDE.md`).
