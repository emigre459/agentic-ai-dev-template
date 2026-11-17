# Dependabot Major-Only Version Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dependabot opens version-update PRs only for majors (plus uv-build minors in the python stack), security updates unchanged. A guard test pins this policy, and the open #65 and #72 bumps are folded in.

**Architecture:** One config file (`.github/dependabot.yml`), guarded by a new PyYAML-based test in `tests/template/`. `init` output is checked by extending `test_initialize.py`. Two small dependency bumps go in the stacks.

**Tech Stack:** Dependabot v2 config, pytest + PyYAML (already a template-tooling dep), uv, bun.

**Spec:** `docs/superpowers/specs/2026-11-16-dependabot-major-only-design.md`

## Global Constraints

- Major rule, verbatim, on every entry: `{dependency-name: "*", update-types: ["version-update:semver-major"]}`; on the two uv entries it also has `dependency-type: "direct"` (amended 2026-11-16, see the spec).
- uv-build exception, only on the uv `/stacks/python` entry: `{dependency-name: "uv-build", dependency-type: "direct", update-types: ["version-update:semver-minor"]}`.
- Remove groups `python-dev` (both), `react-dev`, `github-actions`. Keep `vite` and `vitest` (patterns only, no `update-types`).
- `# >>> STACK:<x>` / `# <<< STACK:<x>` markers unchanged.
- Schedules, labels and commit-message prefixes unchanged. No `open-pull-requests-limit`.
- `stacks/python` build pin: `uv_build>=0.13.0,<0.14.0`. jsdom: `^31.0.0`.

## Review Focus

1. A seeded **react** repo: the bun and github-actions entries must still carry the major rule after `init` strips blocks. Test: `test_initialize_react_*` assertion (Task 1, Step 4).
2. A seeded **python** repo: the uv entry is rewritten to `/` and must keep both allow rules. Test: python `init` assertion (Task 1, Step 4).
3. An `updates` entry added later without `allow` (e.g. #59's npm entry). The guard iterates every entry, so it fails. Test: `test_every_entry_allows_only_majors` (Task 1).
4. A group left with `update-types: ["minor","patch"]` would silently match nothing. Test: `test_no_minor_patch_groups` (Task 1).
5. The uv-build exception leaking to another entry, or being misspelled `uv_build`. Test: `test_uv_build_minor_exception_only_on_python_stack` (Task 1).

---

### Task 1: Major-only policy in dependabot.yml + guard tests

**Files:**
- Create: `tests/template/test_dependabot_policy.py`
- Modify: `.github/dependabot.yml`
- Modify: `tests/template/test_initialize.py` (the two dependabot blocks, ~lines 46-52 and ~101-105)

**Interfaces:**
- Produces: `MAJOR_RULE: dict` and `UV_BUILD_MINOR_RULE: dict` constants, and `load_updates(path: Path) -> list[dict]` in `tests/template/test_dependabot_policy.py`. `test_initialize.py` imports them via `from tests.template.test_dependabot_policy import MAJOR_RULE, UV_BUILD_MINOR_RULE, load_updates`. Check that the `tests.template...` import style works the way `scripts.init_template` is imported; if the package isn't importable, inline the constants instead.

- [ ] **Step 1: Write the failing tests** in `test_dependabot_policy.py` (each reads `REPO_ROOT / ".github/dependabot.yml"` through `load_updates`):
  - `test_every_entry_allows_only_majors`: for each entry, `MAJOR_RULE in entry["allow"]`, and every other rule `== UV_BUILD_MINOR_RULE`.
  - `test_uv_build_minor_exception_only_on_python_stack`: entries containing `UV_BUILD_MINOR_RULE` are exactly the one with `package-ecosystem == "uv"` and `directory == "/stacks/python"`.
  - `test_no_minor_patch_groups`: no group has an `update-types` key. Also `set(groups of the bun entry) == {"vite", "vitest"}`, and no other entry has `groups`.
  - `test_stack_markers_intact`: the raw text contains each `# >>> STACK:` / `# <<< STACK:` pair for `python`, `react`, `template`.
- [ ] **Step 2: Run** `uv run pytest tests/template/test_dependabot_policy.py -v`. Expected: the first three FAIL; the markers test passes.
- [ ] **Step 3: Edit `.github/dependabot.yml`** per the Global Constraints. Add a header comment (above `version: 2`) explaining the policy: majors only, each on its own PR; `allow`/`update-types` affect version updates only, so security updates always open; `vite`/`vitest` groups keep peer majors together; the uv-build exception exists because uv ships breaking changes in 0.x minors; guarded by `tests/template/test_dependabot_policy.py`. Rewrite the stale "Bundle routine minor/patch bumps" comments.
- [ ] **Step 4: Extend `test_initialize.py`.**
  - Python test: `load_updates` on the seeded config. Every entry has `MAJOR_RULE`. The uv entry (`directory == "/"`) has `allow == [MAJOR_RULE, UV_BUILD_MINOR_RULE]`.
  - React test: every entry has `allow == [MAJOR_RULE]`.
- [ ] **Step 5: Run** `uv run pytest tests/template/test_dependabot_policy.py tests/template/test_initialize.py -v`. Expected: all PASS.
- [ ] **Step 6: Commit + push** (`git push` after every commit): "Limit Dependabot version updates to majors (#74)".

### Task 2: Fold #65, pin uv_build to the 0.13 line

**Files:** Modify `stacks/python/pyproject.toml:15-17`

- [ ] **Step 1:** Set `requires = ["uv_build>=0.13.0,<0.14.0"]`, with a comment above it: uv bumps the minor version for breaking changes, and `build-system.requires` isn't locked by `uv.lock`, so the range spans one minor line matching the uv CI installs; Dependabot raises the next line (uv-build minor exception in `.github/dependabot.yml`).
- [ ] **Step 2: Verify** `cd stacks/python && uv build --out-dir "$(mktemp -d)"` succeeds with no "does not contain the current uv version" warning, and `make -C stacks/python tests` passes.
- [ ] **Step 3: Commit + push:** "Pin stacks/python uv_build to the 0.13 line (folds #65)".

### Task 3: Fold #72, jsdom 31

**Files:** Modify `stacks/react/package.json` (jsdom), `stacks/react/bun.lock`

- [ ] **Step 1:** Set `"jsdom": "^31.0.0"` and relock with `cd stacks/react && bun install`.
- [ ] **Step 2: Verify** `make -C stacks/react tests` passes. Vitest uses the jsdom environment (`vitest.config.ts`).
- [ ] **Step 3: Commit + push:** "Bump jsdom to 31 in stacks/react (folds #72)".

### Final gate (orchestrator)

`make pr_check` at the repo root exits 0.
