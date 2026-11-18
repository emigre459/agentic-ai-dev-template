# Tidy the small leftovers from #66: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land #84's five tidy-ups, each with a guard test, so the v0.2.0 skills leave no refs in a user's clone, name no downstream code, and state the light-check fallback consistently.

**Architecture:** Five independent edits: one script fix in `update_prs.py` and four doc or frontmatter edits in `.agents/skills/*/SKILL.md`. Each has a pytest guard under `tests/template/`.

**Tech Stack:** Python 3 (stdlib), pytest, `uv run`.

**Spec:** `docs/superpowers/specs/2026-11-18-tidy-66-leftovers-design.md`

## Global Constraints

- No skill behavior changes beyond the five items in the spec.
- The picomatch ignore (`stacks/react/Makefile`, `tests/template/test_react_audit_ignores.py`) is NOT touched; it moves to its own card.
- Do not touch `.claude/settings.json` or the root `Makefile`, which open PR #81 edits.
- Run tests with `uv run pytest <path> -q`.

## Review Focus

1. `merge-tree` raising `GhError` (a conflicting PR): the private ref must still be deleted. Task 1 tests this.
2. `git update-ref -d` itself failing must not mask the original result or raise out of `collect`. Task 1 makes the delete best-effort: swallow `GhError` only.
3. A skill added later gets the new preflight automatically. The exact-match template in Task 4 enforces this.
4. Release check, with a published release and no drafter config: unchanged (state the last release). Task 5's prose keeps this branch.
5. The pr-check body's command must match the allow-list entry character for character. Task 3 tests this.

---

### Task 1: Private, deleted ref in `update_prs._changes_main`

**Files:**
- Modify: `.agents/skills/upgrade-deps/scripts/update_prs.py:1045-1052` (`_changes_main`)
- Test: `tests/template/test_upgrade_deps_update_prs.py`

**Interfaces:** `_changes_main(runner: Runner, base_ref: str, number: int) -> bool | None` keeps its signature and return.

- [ ] **Step 1: Write the failing tests.** Teach `FakeGh.__call__` to accept `["git", "update-ref", "-d", ref]` (return `""`) and add an opt-in `merge_tree_conflict: bool = False` that makes `git merge-tree` raise `update_prs.GhError`. Add tests:
  - `test_changes_main_fetches_into_a_private_ref_and_deletes_it`: call `update_prs._changes_main(gh, "refs/remotes/origin/main", 7)`. Assert that the fetch argv ends with `"+refs/pull/7/head:refs/upgrade-deps/pr-7"`, that `["git", "update-ref", "-d", "refs/upgrade-deps/pr-7"]` is in `gh.calls` after the merge-tree call, and that no call mentions `refs/remotes/origin/pr-`.
  - `test_changes_main_deletes_the_ref_when_merge_tree_conflicts`: with `merge_tree_conflict=True`, the result is `None` and the `update-ref -d` call is still recorded.
  - `test_changes_main_tolerates_a_failed_ref_delete`: a runner whose `update-ref` raises `GhError` still returns the merge-tree verdict.
- [ ] **Step 2:** `uv run pytest tests/template/test_upgrade_deps_update_prs.py -q -k changes_main`. Expected: FAIL (old ref name, no delete).
- [ ] **Step 3: Implement.** `ref = f"refs/upgrade-deps/pr-{number}"`. Wrap merge-tree and rev-parse in `try/finally`, and in the `finally` run `runner(["git", "update-ref", "-d", ref])` inside `try/except GhError: pass` (best-effort cleanup, with a one-line comment saying why it's narrow and safe). Update the function's comment or docstring to say why: the namespace stays out of `git branch -r` and the user's remotes.
- [ ] **Step 4:** Run the whole file: `uv run pytest tests/template/test_upgrade_deps_update_prs.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit ("Keep update_prs' PR refs out of the user's clone (#84)") and push.

### Task 2: Generic staleness example in `clean-up-kanban` §2

**Files:**
- Modify: `.agents/skills/clean-up-kanban/SKILL.md:80`
- Test: `tests/template/test_skill_extension_points.py` (append; it already loads every skill)

- [ ] **Step 1: Write the failing test** `test_clean_up_kanban_examples_are_generic`: the flattened SKILL.md contains none of `BmsSnapshot`, `analysis.py`, `deepagents`.
- [ ] **Step 2:** Run it. Expected: FAIL.
- [ ] **Step 3:** Replace the sentence's examples with: "A card citing a module, type or framework the repo has since deleted or replaced is prima facie stale." Leave every other line alone.
- [ ] **Step 4:** Run it. Expected: PASS.
- [ ] **Step 5:** Commit and push.

### Task 3: Anchor `pr-check`'s single-flight allow-list entry

**Files:**
- Modify: `.agents/skills/pr-check/SKILL.md` (frontmatter `allowed-tools`, and the command block under the preflight)
- Test: `tests/template/test_pr_reviewer_seam.py` (next to `test_single_flight_full_check_is_allow_listed`)

**Interfaces:** the exact string `_ANCHORED = "python3 .agents/skills/resolve-pr-concerns/scripts/single_flight.py suite -- make pr_check"`.

- [ ] **Step 1: Write the failing test** `test_pr_check_allow_lists_only_the_anchored_lock_command`: parse the frontmatter (yaml between the first two `---`) and assert:
  - `f"Bash({_ANCHORED})"` is in `allowed-tools`;
  - no `*` appears before `single_flight.py` in it;
  - `_ANCHORED` appears in the skill body's fenced command block.
- [ ] **Step 2:** Run it. Expected: FAIL.
- [ ] **Step 3:** Set the `allowed-tools` entry to `Bash(python3 .agents/skills/resolve-pr-concerns/scripts/single_flight.py suite -- make pr_check)`. Make the body's code block exactly that command, and add one line saying to run it from the repo root (Claude Code reads the folder through the `.claude/skills` symlink, but the canonical path is `.agents/skills`). `test_full_check_runs_through_the_single_flight_lock` must still pass (it checks a substring).
- [ ] **Step 4:** `uv run pytest tests/template/test_pr_reviewer_seam.py tests/template/test_skill_frontmatter.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit and push.

### Task 4: Light-check fallback in every skill's preflight hard stop

**Files:**
- Modify: `tests/template/test_skill_extension_points.py` (`PREFLIGHT_TEMPLATE`)
- Modify: all 14 `.agents/skills/*/SKILL.md` (the preflight paragraph)

- [ ] **Step 1:** In `PREFLIGHT_TEMPLATE`, change `"`make pr_check` unless the diff qualifies for the light check)"` to `"`make pr_check` unless the diff qualifies for the light check; if the repo has no `pr-check-scope` rule, the full `make pr_check`)"`.
- [ ] **Step 2:** `uv run pytest tests/template/test_skill_extension_points.py -q`. Expected: FAIL for all 14 skills.
- [ ] **Step 3:** Apply the same textual replacement to each SKILL.md's preflight. It's one exact string per file, so a scripted literal replace is fine; afterwards, check that `git diff --stat` shows 14 files with one line each.
- [ ] **Step 4:** Run it. Expected: PASS.
- [ ] **Step 5:** Commit and push.

### Task 5: Release check skips a repo that doesn't publish releases

**Files:**
- Modify: `.agents/skills/build-from-issue/SKILL.md:252-256` (Step 2, "Release check")
- Test: `tests/template/test_pr_reviewer_seam.py` (uses its `_flat`, `_section`, `_BUILD`)

- [ ] **Step 1: Write the failing test** `test_release_check_skips_repos_without_release_pieces`. In `_section(_flat(_BUILD), "## Step 2:")`, assert that it contains `".github/release-drafter.yml"` and `"skip"`, and that `"v0.1.0"` is still present (the drafter-config branch keeps it).
- [ ] **Step 2:** Run it. Expected: FAIL.
- [ ] **Step 3:** Replace the "On a repo with no published release" bullet with two:
  - With no published release and no `.github/release-drafter.yml`, the repo doesn't publish releases: say so in one line and skip the bump suggestion.
  - With no published release but a release-drafter config, say so and suggest the first one, `v0.1.0`.
- [ ] **Step 4:** Run it. Expected: PASS.
- [ ] **Step 5:** Commit and push.

### Task 6: Full gate

- [ ] `make lint` and `uv run pytest tests/template -q`: green. (Step 11 runs the full `make pr_check` through the lock.)
