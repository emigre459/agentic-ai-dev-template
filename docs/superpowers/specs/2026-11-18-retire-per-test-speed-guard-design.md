# Retire the per-test speed guard for a fixed per-test timeout — design

Issue: #83 (epic #51). Status: approved 2026-11-18. Size: small change.

## Intent

The template ships a per-test speed guard (`scripts/check_test_speed.py`, `make
test_speed_check`) on both stacks. Its leading downstream repo retired the same guard
on 2026-11-17 (the leading downstream repo's PR #3081, closing its #2996): the solo re-measure cost
minutes on every run and most failures were machine-contention flakes. Consumers of
this template should not inherit a gate their leading repo dropped. A fixed, generous
per-test timeout replaces it as the only time-based block: hitting it means a test is
hung or badly broken, never "a bit slow".

## Goals

- `test_speed_check` leaves `make pr_check` and CI on both stacks, with its allowlists,
  re-measure machinery and runner calibration.
- A fixed per-test timeout fails a hung test on each stack.
- Every skill, rule and doc that names the speed check is updated to match.

## Non-goals

- Porting downstream's non-blocking "slowest tests" CI job summary
  (`rank_test_wallclock.py`). Possible follow-up card.
- A timeout for the root template-machinery tests (`tests/template/`); the guard never
  covered them.
- Shared-fixture start-up allowances (downstream's `SHARED_FIXTURE_STARTUP_S`): the
  template's stacks have no shared slow fixtures.

## Anti-goals

- A timeout tight enough to fail healthy-but-slow tests on a loaded runner. That is the
  flake the guard is being retired for.

## Approaches

1. **Timeout in each stack's test config (chosen).** `timeout` in the Python stack's
   `[tool.pytest.ini_options]`, `testTimeout` in `vitest.config.ts`. Applies to every
   way a test runs: `make tests`, `make coverage`, an IDE, a bare `pytest`/`vitest`.
2. **`--timeout` passed only from the Make targets.** Rejected: a test run any other way
   has no limit.
3. **Keep the guard, loosen its budget.** Rejected: keeps the re-measure cost and the
   contention flakes the card exists to remove.

## Design (decided)

- **Python stack:** add `pytest-timeout` to the dev dependency group; set
  `timeout = 60` in `[tool.pytest.ini_options]`. Method left at the plugin default
  (`signal` on POSIX, `thread` elsewhere): on expiry the test fails on its own and the
  run continues, keeping JUnit and coverage. The comment says to switch to
  `timeout_method = "thread"` if the code under test uses SIGALRM (downstream's reason).
- **React stack:** `testTimeout: 10_000` in `vitest.config.ts`'s `test` block.
- **Values (user-decided at read-back):** the template's own suites cannot set them by
  measured maxima (slowest Python test 0.066 s, slowest React test 0.037 s, measured
  2026-11-17). Python takes downstream's proven 60 s, about 3x its measured 20.5 s
  slowest default-lane test (#3081). React has no downstream measurement; 10 s is twice
  Vitest's 5 s default, leaving room for slow jsdom/user-event tests in consumers. Each
  value carries a comment with this derivation.
- **Removed:** `scripts/check_test_speed.py`, `scripts/runner_speed.py` (only the guard
  uses it here; downstream kept theirs for a lane-total check the template lacks), their
  tests (`tests/template/test_check_test_speed.py`, `test_runner_speed.py`,
  `test_speed_guard_wiring.py`), both `slow_test_allowlist.txt` files,
  `stacks/python/tests/_pytest_fixture_timing.py` and its `conftest.py` re-export, the
  `test_speed_check` Make targets (root and both stacks) and their place in `pr_check`,
  both CI "Per-test speed guard" steps, the Python setup in the `react-tests` job (it
  existed only for the guard's calibration), and the `test_speed` block in
  `.github/pr-reviewer.json`.
- **Tests:** a new wiring test file asserts each stack sets its timeout, `pytest-timeout`
  is a Python dev dependency, and no Make target, CI step, config or shipped file still
  names the speed check. The `test_speed` cases in `test_pr_reviewer_seam.py` go;
  `test_reviewer_state.py`'s "later top-level section" example is renamed off
  `test_speed`.
- **Prose (user-decided: strip every mention):** `resolve-pr-concerns` §2g becomes
  timeout coaching (fix what it waited on → reclassify to a slower lane → an explicit
  per-test timeout only with a derivation comment); its intro lines lose the speed
  check. `pr-check/SKILL.md`, `build-from-issue/SKILL.md`,
  `build-from-issue/references/stacked-prs.md`, `upgrade-deps/SKILL.md`, the
  `pr-check-scope` rule, `AGENTS.md` (through `update-project-memory`), the README and
  `.gitignore`'s comment drop it. `skills-lock.json` is unchanged: pinned entries verify
  against the `v0.1.0` tag, not the working tree.

## Left to the builder

- Exact wording of the rewritten prose, kept short and matching downstream's §2g where
  it applies.
- Whether the `.gitignore` entry for `test-results.xml` stays (coverage still writes it).
