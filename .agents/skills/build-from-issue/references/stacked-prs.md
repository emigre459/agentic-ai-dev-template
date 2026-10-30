# Stacked PRs: opt-in, for a plan one review would miss things in

A **stack** is a chain of small PRs, each based on the one below it, so the reviewer
sees one layer's diff at a time. The point is **review quality**: a reviewer (an AI
one included) finds more on smaller, coherent layers than on one large diff that
mixes related changes. The cost is time: a stack stays open longer and every
restack touches the layers above.

**One PR is the default; the author decides.** `build-from-issue` offers a stack at
plan review (Step 3) only when it is warranted. This file covers this skill's side:
whether to stack, how to split, the plan's `## Stack` section, building and
finalizing. Once the PRs exist, `resolve-pr-concerns` drives the stack's review, CI
and landing (its own `references/stacked-prs.md`).

## Contents

The `gh stack` extension · When to stack · Don't stack · How to split into layers ·
Plan format · Building · Finalize · After finalize: review and landing · Gotchas

## The `gh stack` extension (the user installs it)

Stacks are built with GitHub's `gh stack` CLI extension (`github/gh-stack`, MIT; a
public preview). **Before offering a stack, check it is installed:**

```bash
gh stack --help
```

If that fails, say so plainly and show the user the commands to run themselves:

```bash
gh extension install github/gh-stack
npx skills add github/gh-stack    # optional: the extension's own agent skill
```

The agent never installs CLI extensions or system tools on the user's machine itself. Without the
extension, deliver the plan as one PR (or wait for the user to install it).

## When to stack

Offer a stack when the plan is **complex enough that even an AI reviewer would likely
miss how its changes relate** to each other in one diff. Rough signals, not rules:
several concerns a reviewer could approve separately (for example 5 or more tasks
across distinct parts of the code), or a large expected diff (on the order of 2k
changed lines of production code).

**Hard limits** — a stack outside them is not offered:

- **At most 4 layers** (2–4; one layer is just a PR).
- **Every layer comes from this one planned card** and is conceptually related to the
  others. Unrelated work belongs on its own card.
- **Fewer than 300 files changed across the whole stack.** More than that means the
  card is too big: split it into several cards via `/plan-issues`, don't stack
  harder.

Estimate the file count at plan time from the plan's file list; verify it before
`gh stack submit` (see Finalize).

## Don't stack

1. **One task**: the plan has a single task.
2. **Docs or config only**: no product code changes.
3. **A hotfix**: a fix for a broken `main` or a live incident, where review latency
   matters more than review depth.
4. **Can't split into green layers**: no split exists where every layer passes CI on
   its own (e.g. a rename whose every reader must change in the same commit).

A plan may still record the decision explicitly with `Stack: none - <reason>`.

## How to split into layers

- **Consecutive plan tasks, in dependency order**: foundations at the bottom, then the
  code that uses them, then wiring, then docs.
- **Each layer passes CI on its own** — lint, coverage with its gate, the per-test
  speed check, security. A layer that only passes once a later layer is on top is a
  bad split.
- **Each layer makes sense to review alone.** Its PR body says what it does and why
  that is the boundary.
- **Tests go in the same layer as the code they test.** A docs-only top layer is
  fine.
- **Don't pad layers.** Aim for a few hundred to roughly 1.5k changed lines each.
- **Every layer must be one the reviewer can review.** A layer over the configured
  reviewer's file limit (`resolve-pr-concerns`' `references/reviewers/<active>.md`
  or the adapter's repo-owned `notes` file says if it has one) can't get `review-gate`
  green, and a stack can't be merged with a ruleset bypass. Land such a change as its
  own PR first and build the stack on top of it.
- **Prefer one new test file per layer** when several layers would append tests to the
  same file: same-file appends conflict on every restack.

## Plan format

When the user picks a stack, `writing-plans` adds this section and orders the plan's
tasks by layer. A plan without a `## Stack` section is a single PR.

```markdown
## Stack

| Layer | Branch | Tasks | Refs |
|---|---|---|---|
| L1 <name> | `{N}-{slug}` | 1-3 | Refs #N |
| L2 <name> | `{N}-l2-{slug}` | 4-5 | Refs #N |
| L3 <name> | `{N}-l3-{slug}` | 6-7 | Closes #N |
```

- **2–4 rows**, `L1`, `L2`, ... in order, none skipped.
- **Branch names.** L1 is the session's own linked branch (`{issue-number}-{kebab-case}`,
  or whatever branch the session already runs on), so issue linking and in-flight
  detection keep working. Each layer above adds `-l<k>-` after the number.
- **Lower layers say `Refs #N`; the top layer says `Closes #N`.** A stack that
  delivers only a slice of the card says `Refs #N` on every layer.

## Building

1. **Set the stack up before the first implementation commit.** The spec and plan are
   already committed on the session branch (Step 3), which becomes L1. **L1 is never
   `main`:** if you are on `main`, first `git switch -c <N>-<kebab-title>` at the
   current `HEAD` and use that branch as L1 (`gh stack init main ...` would make
   `main` a layer, and `gh stack submit` then fails trying to open a `main`-to-`main`
   PR). Then create the other layers from it, adopt them, and rebase onto the fresh
   trunk:

   ```bash
   git branch <N>-l2-<slug> HEAD
   git branch <N>-l3-<slug> HEAD
   gh stack init <L1-branch> <N>-l2-<slug> <N>-l3-<slug>
   gh stack rebase
   gh stack bottom
   ```

   `gh stack init` leaves you on the **top** branch, so `gh stack bottom` goes back to
   L1. `gh stack rebase` fetches `origin` and rebases the stack onto `origin/main`.
2. **Build each task on its layer's branch.** Move with `gh stack up`, `gh stack down`,
   `gh stack bottom`, `gh stack top` or `gh stack checkout <branch>`. Those only
   navigate: before `gh stack up` from a layer that has new commits, run
   `gh stack rebase --upstack` so the layers above contain them.
3. **Push after every commit** with `gh stack push` (it pushes every layer with
   `--force-with-lease`). Rebasing layers is expected here; it is the one exception
   to merge-don't-rebase for pushed branches.
4. **A change that belongs in a lower layer goes in that layer**: check it out,
   commit, then `gh stack rebase --upstack` and `gh stack push`.
5. **With subagents (Step 5a)**, dispatch implementers in layer order, one at a time,
   and name the branch in each dispatch. The layers share one worktree, so two
   implementers must never run at once.
6. **E2E (Step 6) and the acceptance audit (Step 7) run on the top layer**, which
   holds the whole change; cite each criterion's evidence with its layer.

## Finalize

This replaces the single-PR path of Step 11; `finishing-a-development-branch` still
owns test verification and the cleanup checks, but its push/PR option is not taken:
`gh stack push` pushes the layers and `gh stack submit` opens their PRs.

1. **Local gate.** `make pr_check` on the **top** layer (it contains every layer's
   change) must exit 0, plus `make lint` on each lower layer, bottom-up
   (`gh stack bottom`, then `gh stack up`). A full suite per layer is redundant, and
   several at once can exhaust a laptop. A red check is a TDD RED on the layer that
   owns the failing code: fix it there, `gh stack rebase --upstack`, and re-run the
   top layer's `make pr_check`.
2. **Check the limits.** From the top layer,
   `git diff --name-only origin/main...HEAD | wc -l` must be under 300, and there
   must be no more than 4 layers. Over either: stop, and offer to split the card via
   `/plan-issues`.
3. **Open the PRs.** `gh stack submit --auto --open` creates every layer's PR,
   chained, and links them into a stack on GitHub. It replaces `gh pr create`.
4. **Write each layer's PR body** (`gh pr edit <PR> --title ... --body-file ...`):
   - the stack map: every layer, its PR number, and which one this is;
   - on every **lower** layer, a first line
     `> **Don't merge from this PR.** Land the stack from its top layer, #<top-PR>.`
     (merging a lower layer lands only it and the layers below);
   - `Refs #N` on lower layers and `Closes #N` on the top one (every layer
     `Refs #N` when the stack is only a slice of the card);
   - the layer's scope, why that is the boundary, and any deviations from the issue.
5. **CI gates each layer.** Each layer is a PR whose base is the layer below it (the
   bottom one's base is `main`), and every layer's PR runs CI and is gated on its own.
   A green local gate on the top layer is the pre-flight, not the verdict.

## After finalize: review and landing

Step 11b runs as usual, then Step 12 invokes `resolve-pr-concerns`, which detects
the stack and drives every layer **bottom-up**, each with its own final full review,
and reports merge-ready only when every layer is. Merge-ready is a report to the
user, never permission to merge.

Landing is the user's call, or the agent's only with a **one-off, explicit
permission** for this stack. The whole stack lands from its top PR
(`gh stack merge <top-PR> --yes`, or the merge button on the top PR) — never
`gh pr merge` on a layer, which can't merge a stacked PR.

- **With a merge queue** on `main`, `gh stack merge` enqueues every layer together;
  the queue tests each against the latest `main` and lands them bottom-up. `main`
  moving underneath is the queue's job.
- **Without a merge queue**, each layer's own PR checks gate it, and the stack lands
  bottom-up. Pass the repo's allowed merge method explicitly —
  `gh stack merge <top-PR> --yes --squash` in this template, which allows squash
  merges only (with `--yes` and no method, `gh stack merge` uses your last-used
  one). If a lower layer lands on its own, the layer above must be retargeted
  to the new base and **rebased onto it before it lands** (a stack-aware merge does
  this; `gh stack sync` updates the local branches afterwards).

`resolve-pr-concerns`' own `references/stacked-prs.md` has the details (fixing a
layer, catching up with the trunk, recovering a layer that wasn't retargeted).

## Gotchas

1. **Never merge `main` into a layer.** A stack must stay linear. Catch up with
   `gh stack sync` or `gh stack rebase`: at once when GitHub reports a layer
   `CONFLICTING` / `DIRTY`; a layer that is only `BEHIND` (which blocks only under
   the up-to-date rule) is caught up once, at the end — never mid-review just to be
   current.
2. **Stack tracking is per worktree.** Removing the worktree loses it; a session
   resuming elsewhere rebuilds it with `gh stack checkout <PR-number>`.
3. **Use non-interactive forms only**, e.g. `gh stack view --json`,
   `gh stack init <branches...>`, `gh stack submit --auto --open`,
   `gh stack merge <target> --yes`. Never a menu-only command.
4. **A stale local `main` is handled.** When `main` is checked out in another
   worktree, `gh stack rebase` says it can't update it and rebases onto `origin/main`.
   Don't update the other worktree's `main` yourself.
5. **Navigation switches the worktree's branch.** Finish a session with
   `gh stack bottom`, so the worktree sits on L1, its own branch.
6. **`gh stack push` sets no upstream on layer branches**, and every restack
   force-pushes them, so `git pull` on a layer fails or breaks the linear history.
   A live-E2E kit (Step 7b) for a stack runs from the worktree that built it, on the
   top branch; never write `git pull` in a kit or runbook for a stack branch.
7. **`git rerere` is on and its cache is shared by every worktree**, so a rebase may
   print `Staged '<file>' using previous resolution` with another session's
   resolution. Check the file holds exactly this layer's content before
   `git rebase --continue`; `git rerere forget <path>` drops a bad entry.
8. **An aborted `gh stack sync` exits 0.** It prints `Sync aborted` and changes
   nothing; check for that line before treating the stack as synced.
