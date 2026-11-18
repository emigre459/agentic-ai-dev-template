---
description: Before opening a PR, run the full `make pr_check` only when the diff can change what the whole suite sees; a docs, config or single-test diff runs lint plus its own tests and lets CI be the full gate
alwaysApply: true
---

# Scope the pre-PR check to what the diff can break

`make pr_check` is the local mirror of CI: lint, security and the full coverage run.
It can take many minutes, and CI runs the required checks again on every push. For a
diff that can't change what the full suite sees, the local run only repeats CI.

This rule says which check to run before opening a PR. It never lowers the bar for
merging: the required CI checks and review still gate every PR.

## Full `make pr_check` before the PR when the diff touches any of

- the stack's source: `src/`
- a manifest or lockfile: `pyproject.toml`, `uv.lock`, `package.json`, `bun.lock`
- the `Makefile`, or CI: `.github/workflows/`, `.github/actions/`
- test or lint configuration (pytest, ruff, mypy, Biome, Vitest, tsconfig settings)
- shared test infrastructure: any `conftest.py`, a test helper module other tests
  import, a Vitest setup file. A fixture change reaches every test that uses it.
- in the template repo itself, also `scripts/` and `tests/template/conftest.py`

Run it through the single-flight lock so parallel sessions queue instead of
competing:

```bash
python3 <resolve-pr-concerns skill dir>/scripts/single_flight.py suite -- make pr_check
```

## Otherwise, the light check

Docs, `references/`, `.agents/rules/`, `.github/CODEOWNERS`, skill markdown, and test
or script changes that nothing else imports:

```bash
make lint
# the stack's test runner on the test files covering the change:
uv run pytest -n0 <the test files covering the change>   # Python
bun run vitest run <the test files covering the change>  # React
```

Add `make security` when the diff changes Python under `scripts/` or a skill's
`scripts/` (the security scan covers them). Then push, open the PR, and wait for
every required status on the PR with `resolve-pr-concerns`'
`scripts/wait_for_pr_checks.py`; a "ready" claim cites those actual results.

When unsure which list a file belongs to, run the full check.

## When this rule is absent

Rules are not distributed by `npx skills`, so a repo that installed only the skills
may not have this file. Every skill sentence that offers the light check says: if
the repo has no `pr-check-scope` rule, run the full `make pr_check`.
