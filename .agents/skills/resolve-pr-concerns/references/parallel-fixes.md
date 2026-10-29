# Parallel fix mode (resolve-pr-concerns Step 4a/4b)

Read this before fanning concerns out to bughunter agents. Serial mode (SKILL.md
Step 4c) is the default; parallel mode is an optimization with a strict gate.

## 4a. The gate

Enter parallel mode **only if all of these are true**:

| Criterion | Reason |
|---|---|
| **2–4 concerns** (cap = 4) | Below 2: nothing to parallelize. Above 4: orchestration overhead and cherry-pick juggling exceed savings. The cap is a conservative starting point; revisit upward once the cap proves comfortable in practice. |
| All concerns classified `simple` (Step 3) | `complex` concerns need per-concern user alignment — bughunter agents work autonomously and can't pause to ask. |
| Concerns are **directory-disjoint** (not just file-disjoint) | Sibling files in the same module often share imports / fixtures / `conftest.py`. Directory-level disjointness is the safer gate. If any two concerns share a directory, fall back to serial for those. |
| Each fix scope is **bounded** (heuristic: < 30 lines of expected diff, single file or 2 closely-related files per concern) | Big changes are atomic units anyway; parallelization gives nothing and risks more collision. |
| No concern requires re-architecting | Bughunter agents have narrow scope; refactors need a planning phase. |

If the gate passes, use `AskUserQuestion` to confirm before dispatching:

> "I plan to fix N concerns in parallel via worktrees. Concerns: [list with one-line summaries]. Each will be fixed in its own worktree by a separate bughunter agent; I'll cherry-pick the results back into the PR branch as atomic per-concern commits and do a single push. Proceed, or fall back to serial?"

If any criterion fails, or the user declines, fall back to serial (SKILL.md Step 4c).

## 4b. Dispatch (gate passed and the user confirmed)

Use `superpowers:dispatching-parallel-agents` with `superpowers:using-git-worktrees` for isolation:

1. **Spin up worktrees**: one per bughunter, each branched off the current PR HEAD. Use `superpowers:using-git-worktrees`.
2. **Dispatch ≤4 bughunters concurrently** (single message with multiple Agent calls). Each bughunter gets:
   - Specific scope: one concern, with full context (the bot's exact comment, file paths, line numbers, any related code excerpts).
   - Constraints: "Only edit files within `<assigned-directory>`. Use `superpowers:test-driven-development` (RED → Verify-RED → GREEN → Verify-GREEN). Run `make pr_check` in your worktree before reporting back. Commit your fix in the worktree with a focused message referencing the concern. **Do not push.** Report back: commit SHA + one-paragraph summary of root cause and fix."
   - Worktree path so the bughunter knows where to operate.
3. **Collect results**. Each bughunter reports a commit SHA in its worktree branch.
4. **Serial integration gate (orchestrator-owned)**:
   - Cherry-pick each bughunter's commit onto the PR branch in the order they reported back. Each lands as its own atomic commit (no squashing — preserves per-concern granularity).
   - If a cherry-pick conflicts: directory-disjoint check missed something. Abort just that cherry-pick, drop that concern from the parallel batch, surface to user, fall back to serial for that concern.
   - Run `make pr_check` once on the fully integrated state.
   - If `pr_check` fails on the integrated state but passed in each worktree, hidden coupling is the cause. Reset the integration (revert the cherry-picks), surface to user, fall back to serial for the affected concerns. Invoke `superpowers:systematic-debugging` to find the coupling.
5. **Single `git push`** covers all parallel work. Then the orchestrator replies on each fixed thread with its final SHA (SKILL.md Step 4d).
6. **Clean up worktrees** via `superpowers:using-git-worktrees` cleanup conventions (don't remove harness-owned worktrees).

On a stack, a bughunter whose fix is on a lower layer runs `make lint` plus the
focused tests for its fix instead of `make pr_check`, and the orchestrator runs
`make pr_check` once on the integrated top layer.

## Red flags

- Dispatching parallel bughunters for `complex` concerns — they need user alignment per concern, not autonomous fixing.
- Dispatching bughunters whose concerns share a directory — directory-level disjointness is the gate, not file-level. Fall back to serial.
- Letting bughunters push their own work — the orchestrator owns the single serial push as the integration gate.
- Squashing parallel commits at integration — cherry-pick preserves the per-concern atomicity that justified parallelizing in the first place.
- Dispatching ≥5 bughunters "just this once" — the cap is 4; fall back to serial above that.
