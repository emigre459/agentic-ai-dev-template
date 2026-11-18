# Retire the Per-Test Speed Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-test speed guard on both stacks with a fixed per-test timeout, and remove every trace of the guard from Make, CI, config, tests and prose.

**Architecture:** The timeouts live in each stack's test config (`[tool.pytest.ini_options]`, `vitest.config.ts`) so every invocation gets them. The guard (two scripts, allowlists, the fixture-timing plugin, Make targets, CI steps, the `test_speed` config block) is deleted outright. A new machinery test file pins the timeouts and the absence of the guard.

**Tech Stack:** pytest + pytest-timeout + pytest-xdist (uv), Vitest 5.0.5 (bun), GNU Make, GitHub Actions, the template's machinery tests (`make machinery_tests`).

**Spec:** `docs/superpowers/specs/2026-11-18-retire-per-test-speed-guard-design.md`

## Global Constraints

- Python per-test limit: `timeout = 60` in `stacks/python/pyproject.toml` `[tool.pytest.ini_options]`; no `timeout_method` line (plugin default). Comment states the derivation: about 3x downstream's measured 20.5 s slowest default-lane test (the leading downstream repo's PR #3081); the template's own slowest is 0.066 s; switch to `timeout_method = "thread"` if the code under test uses SIGALRM.
- React per-test limit: `testTimeout: 10_000` in `stacks/react/vitest.config.ts` `test` block. Comment: twice Vitest's 5 s default, room for slow jsdom/user-event tests; the template's own slowest is 0.037 s.
- `pytest-timeout` joins the Python stack's `dev` dependency group via `uv add --dev pytest-timeout` (run in `stacks/python`), so `uv.lock` updates with it.
- `skills-lock.json` is not touched (pinned entries verify against the `v0.1.0` tag).
- `docs/superpowers/` history (old specs/plans) is not edited; scans exclude it.
- `test-results.xml` stays in `.gitignore` (coverage still writes it); only its comment changes.

## Review Focus

1. A hung Python test under xdist (`-n auto`): expect it to fail under its own name with pytest-timeout's "Timeout" message and the rest of the run to continue. Pinned by Step 6 E2E (a temporary sleeping test run with `-o timeout=2`), not a committed test, because a committed hanging test would slow every run.
2. A hung Vitest test: expect a "Test timed out in 10000ms" failure. Pinned by Step 6 E2E with a temporary test.
3. `make init` for a single stack after the removal: expect it to still produce a working repo with no dangling reference to the deleted files. Pinned by the existing init tests (`test_initialize.py`) staying green in Task 2, plus Task 2's "no remnant" scan over the files init keeps.
4. A consumer's `make pr_check` still runs coverage after lint and security: pinned by Task 2's `test_each_stack_pr_check_runs_lint_security_then_coverage`.
5. Prose drift: a skill or rule still telling an agent to run `make test_speed_check`. Pinned by Task 3's prose scan.

---

### Task 1: Per-test timeouts on both stacks

**Files:**
- Create: `tests/template/test_test_timeouts.py`
- Modify: `stacks/python/pyproject.toml` (dev group, `[tool.pytest.ini_options]`), `stacks/python/uv.lock`, `stacks/react/vitest.config.ts`

**Interfaces:**
- Produces: `tests/template/test_test_timeouts.py` with `REPO_ROOT = Path(__file__).resolve().parents[2]`; Tasks 2 and 3 append tests to this file.

- [ ] **Step 1: Write the failing tests**

```python
def test_python_stack_sets_a_60s_per_test_timeout() -> None:
    ini = tomllib.loads((REPO_ROOT / "stacks/python/pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert ini["timeout"] == 60
    assert "timeout_method" not in ini

def test_python_stack_has_pytest_timeout_as_a_dev_dependency() -> None:
    dev = tomllib.loads(...)["dependency-groups"]["dev"]
    assert any(re.match(r"pytest-timeout\b", d) for d in dev)

def test_react_stack_sets_a_10s_test_timeout() -> None:
    text = (REPO_ROOT / "stacks/react/vitest.config.ts").read_text()
    assert re.search(r"^\s*testTimeout:\s*10_000,", text, re.M)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/template/test_test_timeouts.py -v`
Expected: 3 FAIL (KeyError `timeout`, no dev dep, no match).

- [ ] **Step 3: Implement**

`cd stacks/python && uv add --dev pytest-timeout`; add `timeout = 60` with the derivation comment after `addopts`; add `testTimeout: 10_000,` with its comment to the Vitest `test` block.

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/template/test_test_timeouts.py -v` → 3 PASS. Run `make -C stacks/python tests` and `make -C stacks/react tests` → green; pytest's header lists `timeout: 60.0s`.

- [ ] **Step 5: Commit and push**

---

### Task 2: Remove the guard from Make, CI, config, scripts and tests

**Files:**
- Delete: `scripts/check_test_speed.py`, `scripts/runner_speed.py`, `tests/template/test_check_test_speed.py`, `tests/template/test_runner_speed.py`, `tests/template/test_speed_guard_wiring.py`, `stacks/python/tests/_pytest_fixture_timing.py`, `stacks/python/tests/slow_test_allowlist.txt`, `stacks/react/slow_test_allowlist.txt`
- Modify: `Makefile` (drop `test_speed_check`; `pr_check` recipe becomes `@$(MAKE) --no-print-directory coverage`; fix the help text and the ordering comment), `stacks/python/Makefile` and `stacks/react/Makefile` (same), `stacks/python/tests/conftest.py` (drop the re-export, `__all__` and the docstring paragraph about it), `.github/workflows/ci.yml` (drop both "Per-test speed guard" steps and the `react-tests` job's `setup-python` step with its comment), `.github/pr-reviewer.json` (drop `test_speed`), `.gitignore` (comment: "JUnit from `make coverage`"), `tests/template/test_pr_reviewer_seam.py` (drop the `_SPEED_FIELDS` block, its three tests and the `"test_speed_check"` phrase), `tests/template/test_reviewer_state.py:1532-1534` (example section renamed to `future_section`)
- Test: `tests/template/test_test_timeouts.py`

**Interfaces:**
- Consumes: `REPO_ROOT` from Task 1.
- Produces: `_GUARD_TERMS = re.compile(r"test_speed|check_test_speed|runner_speed|slow_test_allowlist|_pytest_fixture_timing|speed (?:check|guard|budget)", re.I)` in the test file; Task 3 reuses it.

- [ ] **Step 1: Write the failing tests**

```python
_GUARD_FILES = ["scripts/check_test_speed.py", "scripts/runner_speed.py",
    "stacks/python/tests/_pytest_fixture_timing.py",
    "stacks/python/tests/slow_test_allowlist.txt", "stacks/react/slow_test_allowlist.txt"]

def test_the_guard_files_are_gone() -> None:
    assert [p for p in _GUARD_FILES if (REPO_ROOT / p).exists()] == []

@pytest.mark.parametrize("rel", ["Makefile", "stacks/python/Makefile", "stacks/react/Makefile",
    ".github/workflows/ci.yml", ".github/pr-reviewer.json", ".gitignore",
    "stacks/python/tests/conftest.py"])
def test_no_build_file_names_the_speed_guard(rel: str) -> None:
    assert not _GUARD_TERMS.search((REPO_ROOT / rel).read_text())

@pytest.mark.parametrize("stack", ["python", "react"])
def test_each_stack_pr_check_runs_lint_security_then_coverage(stack: str) -> None:
    text = (REPO_ROOT / f"stacks/{stack}/Makefile").read_text()
    assert re.search(r"^pr_check: lint security\b", text, re.M)
    assert "$(MAKE) --no-print-directory coverage" in text

def test_react_tests_job_sets_up_no_python() -> None:
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    job = ci.split("  react-tests:")[1].split("  react-security:")[0]
    assert "setup-python" not in job
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/template/test_test_timeouts.py -v`
Expected: the new tests FAIL (files exist, terms found, `setup-python` present); `pr_check` test may already pass.

- [ ] **Step 3: Implement** the deletions and edits listed under **Files**.

- [ ] **Step 4: Verify**

Run: `make machinery_tests` → all green (including `test_initialize.py` and the edited seam/reviewer-state tests). Run `make -C stacks/python pr_check` and `make -C stacks/react pr_check` → exit 0. `make test_speed_check` → "No rule to make target".

- [ ] **Step 5: Commit and push**

---

### Task 3: Prose — skills, rule, README

**Files:**
- Modify: `.agents/skills/resolve-pr-concerns/SKILL.md` (lines ~27, ~36, ~324, §2g ~370-380), `.agents/skills/pr-check/SKILL.md` (~24-52), `.agents/skills/build-from-issue/SKILL.md` (~360, ~430, ~515), `.agents/skills/build-from-issue/references/stacked-prs.md` (~75), `.agents/skills/upgrade-deps/SKILL.md` (~168), `.agents/rules/shared/pr-check-scope.md` (~9), `README.md` (~77, ~128, ~214)
- Test: `tests/template/test_test_timeouts.py`

**Interfaces:**
- Consumes: `_GUARD_TERMS` from Task 2.

- [ ] **Step 1: Write the failing tests**

```python
def _prose_files() -> list[Path]:
    return sorted((REPO_ROOT / ".agents").rglob("*.md")) + [REPO_ROOT / "README.md"]

def test_no_skill_rule_or_readme_names_the_speed_guard() -> None:
    hits = [str(p.relative_to(REPO_ROOT)) for p in _prose_files() if _GUARD_TERMS.search(p.read_text())]
    assert hits == []

def test_resolve_pr_concerns_coaches_the_per_test_timeout() -> None:
    text = (REPO_ROOT / ".agents/skills/resolve-pr-concerns/SKILL.md").read_text()
    section = text.split("### 2g. ")[1].split("\n### ")[0]
    assert section.lower().startswith("per-test timeout")
    for phrase in ("pytest-timeout", "testTimeout", "hung"):
        assert phrase in section, phrase
```

`AGENTS.md` is left to build-from-issue Step 9 (the `update-project-memory` skill); the Step 9 edit extends this scan to `AGENTS.md`.

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/template/test_test_timeouts.py -v`: both FAIL, listing the files above.

- [ ] **Step 3: Implement**

§2g becomes "### 2g. Per-test timeout — coach, don't just relay": nothing fails a test for merely being slow (keep the phrase "speed budget" out, the scan forbids it); a test fails for time only when it hits the stack's per-test limit (pytest-timeout's `timeout` in the Python stack's `pyproject.toml`, Vitest's `testTimeout`), set well above the slowest healthy test, so a hit means hung or badly broken. Remedies in order: fix what it waited on (a `sleep`, a poll with no exit, a lock, an unmocked dependency); reclassify it to an opt-in lane (`integration` / `e2e`) if it truly needs that time; an explicit per-test timeout (`@pytest.mark.timeout(N)`, Vitest's per-test timeout argument) only with a derivation comment, never a raised number to make it pass. Proactive coach: for new or changed tests, glance at `pytest --durations=10` / the Vitest reporter. Elsewhere, delete the speed-check clauses so each sentence still reads (match downstream #3081's wording in build-from-issue and stacked-prs.md); pr-check's report item 4 is removed and items renumbered.

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/template -v` → green (the `test_validate_skill.py`, `test_skill_extension_points.py`, `test_pr_check_scope.py` and `test_readme_workflow.py` checks included).

- [ ] **Step 5: Commit and push**
