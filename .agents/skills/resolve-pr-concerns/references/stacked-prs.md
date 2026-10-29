# Driving a stacked PR to merge-ready

A **stack** is a chain of small PRs, each based on the one below it, so the reviewer
sees one layer's diff at a time and finds more on each. It is built with GitHub's
`gh stack` CLI extension (`github/gh-stack`, a public preview). One PR is the
default; `build-from-issue` decides at plan review whether a card is stacked, within
hard limits: **at most 4 layers**, every layer from the same planned card, and
**fewer than 300 files** changed across the whole stack. This file covers what
`resolve-pr-concerns` does once a stack exists: reviewing, fixing, CI and landing.

## Contents

Detecting a stack · Driving the review · Fixing a layer · CI on a stack ·
Reporting and the merge gate · Landing — the human's call · Gotchas

## Detecting a stack, and the extension

```bash
gh api "repos/<owner>/<repo>/stacks?pull_request=<num>" --jq '.[].pull_requests[].number'
```

No output (exit 0), or a 404 (a repository without the stacks API), means the PR is in no stack. PR numbers, bottom first, mean it is
a layer. When this worktree tracks the stack, `gh stack view --json` gives the same
list; a session resuming in another worktree rebuilds the tracking with
`gh stack checkout <PR-number>`.

Fixing a layer needs the extension. Check with `gh stack --help`. If it is missing,
tell the user and show them the commands to run themselves:
`gh extension install github/gh-stack` (and, optionally, its agent skill:
`npx skills add github/gh-stack`). The agent never installs CLI extensions or
system tools on the user's machine itself.

## Driving the review

- **Every layer, bottom-up.** Each layer runs the normal loop (SKILL.md Steps 2–5a)
  with **its own final full review**, because every layer is held to the base
  branch's ruleset: GitHub applies the bottom PR's base rules to the whole stack, so
  every layer needs its own green `review-gate`, required checks and (if the rules
  require it) resolved threads.
- **Each layer has its own review-round budget** (`reviewer_state.py`'s
  `round_budget`, per PR). Run the final reviews bottom-up: finish a lower layer's
  final review and its fixes before posting an upper layer's, so a cascade from
  below lands before the upper layer spends its after-final rounds.
- **A stack spends PR reviews per layer**, so it is the likeliest thing to run into
  the reviewer's rate limit. With a reviewer that reviews locally, run each layer's
  fix rounds locally and give each layer only its final full review. A refusal is a
  wait, not a failed review (SKILL.md, "Review requests and the rate limit").
- **Every layer must be one the reviewer can review.** A layer over the reviewer's
  file limit (its notes file says if it has one) can't get `review-gate` green, and
  a stack can't be merged with a ruleset bypass. Such a change lands as its own PR
  first, with the stack built on top of it.

## Fixing a layer

1. **Fix on the layer that owns the code**, never on whichever layer happens to be
   checked out: `gh stack checkout <branch>`, commit the fix there.
2. **Restack only your own change**: `gh stack rebase --upstack --no-trunk`, then
   `gh stack push` (it pushes every layer with `--force-with-lease`).
3. **Local gate.** `make pr_check` once, on the top layer (it contains every layer's
   change), plus `make lint` and the fix's focused tests on the layer you changed.
   A full suite per layer is redundant, and several at once can exhaust a laptop.
   Each layer's PR runs CI anyway (below).
4. **Re-review only where needed.** Every layer above the fix now has a new head. A
   layer whose own diff the rebase left unchanged keeps its verdict
   (`reviewer_state.py` reports `verdict_carried_from`, `verdict_carry_reason:
   same_patch`), and `review-gate` counts it. Trigger the reviewer only on a layer
   that reports `needs_trigger: true` — one whose own diff changed, e.g. a conflict
   resolution that edited a line. Final full reviews already done still count: they
   aren't tied to the head.
5. **Reply with the SHA as pushed.** A later rebase rewrites it; that's accepted,
   don't re-edit old replies.

**Never merge `main` into a layer.** A stack must stay linear, and the merge refuses
otherwise. Rebasing layers (which `gh stack` does with `--force-with-lease`) is the
one exception to the merge-don't-rebase habit for branches with review history.
When GitHub reports a layer `CONFLICTING` / `DIRTY`, catch up at once; a layer
that is only `BEHIND` (which blocks only under the up-to-date rule) is caught up
once, at the end, never mid-review (next paragraph). To catch up, run
`gh stack sync` (or `gh stack rebase`, resolve, `gh stack rebase --continue`), then
`gh stack push`. A sync can move every layer, so
compare the layers' heads before and after (`git rev-parse <branch>` and the remote),
re-read CI on each layer whose head changed, and trigger only where `needs_trigger`
is true.

**Don't catch up with the trunk mid-review** unless GitHub reports a conflict. A
sync rebases every layer onto the newest trunk and force-pushes them all, and any
layer whose own diff changes loses its verdict. With a merge queue the queue tests
the combined state anyway; without one, catch up once, at the end, when the rules
require it.

## CI on a stack

Each layer is a PR whose base is the layer below it (the bottom one's base is
`main`), and CI runs on every PR whatever its base, so **every layer's PR runs full
CI and is gated on its own**. Read each layer's checks; a layer whose checks never
register is usually conflicting (SKILL.md Step 2f). `wait_for_pr_checks.py` runs
per layer.

## Reporting and the merge gate

Step 7 reports one row per layer (PR, CI, verdict on head, final full review, open
threads, merge state), then the stack verdict:

```bash
python3 <skill-dir>/scripts/merge_gate.py --repo <owner>/<repo> --pr <top-PR> --stack [--acceptance-audit-clean true|false]
```

`merge_gate.py --stack` evaluates every open layer and reports `layers` with each
one's blockers; the stack is clear only when every layer is. A top layer that reads
clean says nothing about the layers below it. Like the single-PR report, this is a
report for the human, never permission to land anything.

## Landing — the human's call

Landing is a human action, or the agent's only with a **one-off, explicit
permission** from the user for this stack; it never carries over to another PR or
stack.

- **Land the whole stack from its top PR**: `gh stack merge <top-PR> --yes`, or the
  merge button on the top PR — and **never `gh pr merge` a layer**: it can't merge
  a stacked PR. The merge button on a lower layer lands only that layer and those
  below it.
- **With a merge queue** on the base branch, `gh stack merge` adds every layer to
  the queue together; the queue tests each against the latest `main` and lands them
  bottom-up (possibly in separate groups). `main` moving underneath is the queue's
  job, not yours.
- **Without a merge queue**, the stack merge lands the layers bottom-up as one
  all-or-nothing operation, and each layer's own PR checks gate it. Pass the repo's
  allowed merge method explicitly — `gh stack merge <top-PR> --yes --squash` in this
  template, which allows squash merges only (with `--yes` and no method,
  `gh stack merge` uses your last-used one). If a lower
  layer lands on its own (an early landing, which needs a stated reason in the PR
  thread), the layer above it must be **retargeted to the new base and rebased onto
  it before it lands**; a stack-aware merge does this, and `gh stack sync` afterwards
  updates the local branches. A retarget leaves no record of the old base, so that
  layer's verdict can't carry: it needs one more incremental review.
- **After any partial landing, check the next PR's base.** If it still names the
  merged branch (a merge that bypassed the stack-aware flow), see gotcha 9.
- **Land promptly.** Every hour a green stack waits, `main` moves, and each real
  conflict costs a restack plus a review of every layer whose diff changed.

## Gotchas

1. **Stack tracking is per worktree** (`gh stack` writes under the worktree's own
   git dir). Removing a worktree loses it; `gh stack checkout <PR-number>` rebuilds
   it.
2. **Use non-interactive forms only**, e.g. `gh stack view --json`,
   `gh stack submit --auto --open`, `gh stack merge <target> --yes`. Never a
   menu-only command.
3. **A stale local `main` is handled**: `gh stack rebase` rebases onto
   `origin/main` and says so when it can't update a `main` checked out elsewhere.
   Don't update the other worktree's `main` yourself.
4. **An aborted `gh stack sync` exits 0.** When the local and remote stacks have
   diverged it prints `Sync aborted` and changes nothing. Check for that line (or
   compare heads before and after) before treating the stack as synced.
5. **`gh stack push` sets no upstream on layer branches**, and every restack
   force-pushes them, so `git pull` on a layer fails or breaks the linear history.
   Use `gh stack checkout <PR>` to rebuild local tracking, `gh stack sync` to update.
6. **Navigation switches the worktree's branch** (`init` leaves you on the top
   layer; `up` / `down` / `top` / `bottom` move). Finish a session with
   `gh stack bottom`.
7. **`gh stack view --json` can show a stale head** right after a
   `rebase --upstack --no-trunk`. Trust `git rev-parse <branch>` and the remote.
8. **Layers that append to the same file conflict on every cascade.** `gh stack`
   enables `git rerere`, whose cache is shared by every worktree, so a rebase may
   print `Staged '<file>' using previous resolution` with another session's
   resolution. Check the file holds exactly this layer's content before
   `git rebase --continue`; `git rerere forget <path>` drops a bad entry.

Gotchas 9 and 10 change the stack's state (unstack, retarget, reopen, re-enqueue):
take those steps only under the landing permission, or at the human's direction.

9. **A lower layer landed but the next PR wasn't retargeted.** Its base still names
   the merged branch, and landing fails on a base conflict the PR page can't show.
   Reopen any closed layer first. Then `gh stack sync` (drops the merged layer and
   rebases the rest onto `main`; check each layer's own diff is unchanged); GitHub
   refuses to change a stacked PR's base, so `gh stack unstack`, `gh pr edit
   <next-PR> --base main`, and `gh stack link <PRs bottom-to-top>`. A base change
   alone starts no CI run, so push a new commit to that layer (an empty one is
   fine) and restack. Don't delete the merged branch to force a retarget: GitHub
   closes the PR above it instead. If a layer's verdict can't carry and the
   reviewer won't re-review commits it has seen, it would need a second full
   review: ask the human first, since that breaks the one-full-review-per-PR rule.
10. **With a merge queue, one failing entry removes every layer above it**, even
    layers whose own run passed. Read the failed run's log before re-enqueueing
    (`gh stack merge <highest-open-PR> --yes`). If the queue refuses one layer
    outright, read the reason from the PR's timeline (GraphQL
    `RemovedFromMergeQueueEvent`); landing the layers below first, then the
    retargeted layer, has worked.
