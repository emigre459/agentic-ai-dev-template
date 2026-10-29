# `upgrade-deps` skill + first dependency refresh — design

Issue: #52. Status: proposed.

## Intent

Dependency PRs pile up (five Dependabot PRs open on 2026-10-29, plus two high
`black` alerts). Landing them one at a time costs a catch-up round each, and nothing
makes a "bump everything, security first" refresh repeatable with the same rigor as
feature work. Add an `upgrade-deps` skill that does the refresh as ONE PR, then use
it for this repo's backlog.

## Goals

- One skill, harness-agnostic, in `.agents/skills/upgrade-deps/`, that:
  1. inventories open dependency PRs (Dependabot / Renovate) and open security alerts;
  2. clears security alerts first, with targeted upgrades;
  3. folds every open bot PR's *manifest* change in, but regenerates lockfiles with the
     ecosystem's own tool (`uv lock`, `bun install` / `bun update`) — never takes a bot's
     lockfile diff or hand-edits a lockfile;
  4. bumps everything else (majors included) and fixes what the bumps break;
  5. verifies (`make pr_check`, `make coverage`, `make security`, plus an alert check),
     opens one PR, and hands off to `resolve-pr-concerns`.
- A bundled, tested `scripts/alert_status.py` that answers "is each open alert actually
  fixed on this tree?" by reading the alert's vulnerable range and the versions locked in
  its manifest's lockfile, instead of trusting the Dependabot UI.
- Use the skill for #52: fold #16, #19, #38, #42, #43; clear both `black` alerts.

## Non-goals

- Scheduling / automation (a cron or Action that runs the skill). Dependabot already
  schedules; the skill is run on demand.
- Changing `.github/dependabot.yml`.
- New ecosystems beyond what the template ships (uv, bun, GitHub Actions pins).

## Anti-goals (a failure even if it "works")

- Getting green by disabling a check, skipping a test, or ignoring a new lint rule
  without the user's approval.
- Deferring or downgrading a security fix to get green.
- Hand-merged or bot-copied lockfiles.
- Auto-merging, force-pushing, or closing bot PRs before the refresh merges.

## Decisions (the user's)

- Build our own skill unless a public one is consumable as-is (user, 2026-10-29).
  Catalog search found none (anthropics/skills, superpowers, claude-plugins-official,
  skills.sh, GitHub code search); the closest are project-specific or npm/pip-only.
- Questions skipped: the user judged this mechanical.

## Calls left to the builder (mine)

- **Majors policy:** attempt every major. A non-security major that breaks beyond a
  mechanical fix is rolled back on its own (pinned to the previous major) and listed as
  deferred in the PR body; the rest of the run continues. Security fixes are never
  deferred — if one can't land, stop and ask the user.
- **Fix ladder for an alert:** remove the dependency if unused → raise the direct floor
  → targeted transitive upgrade (`uv lock --upgrade-package`, `bun update <pkg>`) → an
  override (`overrides` in package.json, `constraint-dependencies` in uv) only as a last
  resort, with the reason in the PR body.
- **Mechanical breakage is fixed, not suppressed:** new lint rules (ruff 0.17 adds ~22
  findings here), config migrations (`biome migrate` for Biome 2.6).
- **State between runs** lives in the previous refresh PR's body (deferred majors and
  why), not in a state file.
- **Commit slices:** security → folded bot PRs → routine bumps → breakage fixes, pushed
  per commit (push-every-commit rule).
- **Attribution:** ideas borrowed from MIT/Apache-licensed public skills are credited in
  `references/attribution.md` (JoshuaC215/agent-service-toolkit `dependency-refresh` —
  PR-body state, rollback-and-defer; mflux-community/mflux `remediating-dependabot` —
  verify alerts against the lock, fix ladder; rshade/agent-skills `dep-upgrade`,
  Apache-2.0 — per-bump rollback). Nothing is copied verbatim.

## `alert_status.py` contract

- Input: `--repo owner/name` (reads open alerts via `gh api …/dependabot/alerts
  --paginate`), or `--alerts-file` (JSON, for tests); `--root` (repo root, default cwd).
- For each open alert: map `dependency.manifest_path` to its lockfile (`uv.lock` /
  `bun.lock`, the manifest itself if it is one, else the sibling lockfile), collect every
  locked version of the package (uv.lock via `tomllib`; bun.lock's `packages` entries,
  including nested `parent/child` keys), and test each against
  `security_vulnerability.vulnerable_version_range` (comma-joined `<`, `<=`, `>`, `>=`,
  `=` clauses; numeric dotted versions, pre-release suffix sorts before release).
- Output JSON: per alert `number`, `severity`, `package`, `lockfile`, `locked_versions`,
  `vulnerable_range`, `first_patched`, `status` (`cleared` / `open` / `unknown` when the
  lockfile or package can't be read). Exit 0 when nothing is `open` or `unknown`, 1
  otherwise, 64 on usage errors.
- Stdlib only (runs with `python3`, like the other bundled scripts).

## Done looks like

- `.agents/skills/upgrade-deps/` (SKILL.md, `scripts/alert_status.py`,
  `references/attribution.md`) with tests in `tests/template/`.
- One PR from `52-upgrade-deps-skill-and-refresh` with all five bot PRs' updates, both
  `black` alerts cleared per `alert_status.py`, `make pr_check` / `coverage` /
  `security` green, CI green.
