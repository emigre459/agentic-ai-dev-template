# `upgrade-deps` + first refresh — implementation plan

Spec: `docs/superpowers/specs/2026-10-29-upgrade-deps-design.md`. Issue: #52.
Branch: `52-upgrade-deps-skill-and-refresh`. Execution: orchestrator-driven TDD (tasks
are sequential and tightly coupled — the refresh uses the skill). Push after every commit.

## Task 1 — `alert_status.py` (TDD)

Files: `.agents/skills/upgrade-deps/scripts/alert_status.py`,
`tests/template/test_upgrade_deps_alert_status.py`.

1. RED: tests for
   - range parsing/matching: `< 26.10.1`, `>= 7.0.0, < 7.30.1`, `<= 1.2`, `= 2.0.0`,
     pre-release (`7.30.1-rc.1` is `< 7.30.1`);
   - uv.lock reading (tomllib fixture with two versions of a package);
   - bun.lock reading (fixture with trailing commas, a top-level and a nested
     `jsdom/undici` entry);
   - manifest → lockfile mapping (`stacks/python/uv.lock` as-is; `pyproject.toml` /
     `package.json` → sibling lockfile);
   - per-alert status `cleared` / `open` / `unknown`, exit codes 0 / 1 / 64, JSON shape;
   - `--alerts-file` path (no `gh`), and `gh` invocation shape via a stub runner.
2. Verify RED, implement (stdlib only), verify GREEN, `make machinery_lint`.
3. Commit + push.

## Task 2 — `SKILL.md` + attribution

Files: `.agents/skills/upgrade-deps/SKILL.md`,
`.agents/skills/upgrade-deps/references/attribution.md`.

- Frontmatter: portable fields only (`name`, `description`).
- Sections: when to use; preconditions (branch linked to an issue via build-from-issue,
  or a fresh `deps-refresh-<date>` branch off `main`; clean tree); inventory (bot PRs,
  `alert_status.py`, ecosystems from `.github/dependabot.yml`); security first + fix
  ladder; fold bot PRs (manifest changes only, regenerate locks); bump the rest (uv,
  bun `--latest` keeping Dependabot's coupled groups together, Actions pins); fix
  breakage (fix, don't suppress); per-bump rollback for non-security majors; verify;
  commit slices; PR body template (supersedes / alerts cleared / deferred majors);
  hand-off to `resolve-pr-concerns`; closing leftover bot PRs after merge; red flags.
- Skill-root-relative script paths; `.agents/skills/` named as canonical.
- Validate with the `review-skill` skill; fix what it finds.
- Commit + push.

## Task 3 — Run the skill on this repo

Follow the new SKILL.md verbatim (dogfood); any gap found in the skill is fixed in the
skill first.

1. Inventory: PRs #16, #19, #38, #42, #43; `alert_status.py --repo` shows both `black`
   alerts `open`.
2. Security slice: `uv lock --upgrade-package black` (root and `stacks/python`);
   `alert_status.py` → `cleared`. Commit + push.
3. Fold bot PRs: #16's `uv_build` floor in `stacks/python/pyproject.toml`; #19 jsdom 30;
   #38 react-dev group; #42/#43 python-dev groups — via `uv lock --upgrade` and
   `bun update --latest`, never their lockfiles. Commit + push.
4. Routine bumps not covered by a bot PR (trial run: TypeScript 7, vitest 5,
   `@vitest/coverage-istanbul` 5, `@testing-library/jest-dom` 7). Commit + push.
5. Breakage fixes: ruff 0.17's new findings in `scripts/` and `tests/` (fix each;
   executable bit for shebanged scripts), `biome migrate`. Commit + push.

## Task 4 — Verify (E2E)

`make deps`, `make pr_check`, `make coverage`, `make security`, `alert_status.py --repo`
(exit 0). Read the outputs, not just exit codes.

## Task 5 — Finalize

Acceptance audit on #52 (tick boxes with evidence); `finishing-a-development-branch`
→ PR (`Closes #52`, supersedes #16 #19 #38 #42 #43); momentum hook; `resolve-pr-concerns`
(no reviewer active → its Step 1b pre-pass review). After merge, close any bot PR
Dependabot didn't auto-close, with a comment linking the PR.
