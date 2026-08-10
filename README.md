# agentic-ai-dev-template

A template repo that seeds new repos with AI-powered development best
practices — CI/CD, testing, agent rules/skills, and repo settings — for either a
Python backend or a React + TypeScript frontend.

<!-- INTERVIEW:start -->
## Start here: initialize this template

Paste the prompt below into your agentic coding harness (Claude Code, Cursor, …).
It interviews you, validates the local checkout and exact GitHub target, then
initializes the chosen stack through a pull request. Repository settings are
applied in two phases so the first PR is not blocked on CI jobs that do not exist
on `main` yet.

```text
You are initializing a new repository created from the
agentic-ai-dev-template repo. Drive this end to end.

FIRST: use your harness's native structured-interview tool if you have one
(e.g. Claude Code's AskUserQuestion, Cursor's equivalent) to ask the questions
below — these are easier to answer than free-form text. Only fall back to plain
chat questions if no such tool exists. Ask ONE question at a time.

Interview:
1. Is this a FRONTEND or BACKEND repo?
2. Confirm the stack, seeded with sensible defaults:
   - Backend default: Python 3.13 + uv (black, ruff, mypy, pytest, bandit).
   - Frontend default: Vite + React + TypeScript + bun
     (Biome [opinionated formatting], Vitest, tsc).
   The two shipped stacks are the only supported choices today; other
   languages/frameworks are a future template extension.
3. What is the project NAME (short, kebab-case) and a ONE-LINE description?
4. Confirm the exact target GitHub repository in `owner/repository` form. First
   show `git remote -v`, the current branch and its tracking branch, and
   `gh auth status`. Store the confirmed value as `TARGET_REPO`; never infer it
   again from the current branch. If this is a fork, use `origin` for the fork and
   `upstream` for the template source.
5. For a backend, show the derived Python import package (`my-project` becomes
   `my_project`) and confirm it is acceptable.

THEN run, in order:
- Run the non-mutating preflight BEFORE creating anything:
  `make init_preflight STACK=<python|react> TARGET_REPO="$TARGET_REPO"`.
  It must confirm the tools, GitHub account, exact `origin` target, administrator
  permission, enabled GitHub Issues, and a clean working tree. If it fails, stop
  and fix the named condition. Never discard, overwrite, or silently include
  existing local changes; ask the user to commit or stash them first.
- Create a "project setup" issue and linked branch explicitly in `TARGET_REPO`:
    - `gh issue create --repo "$TARGET_REPO" --title "Project setup: initialize from agentic-ai-dev-template" --body "..."`
      (brief bullet-checklist body; if the repo already has epics on its board,
      parent the issue under the one that fits — otherwise standalone is fine
      for this one-time bootstrap).
    - `gh issue develop <N> --repo "$TARGET_REPO" --name chore/<N>-project-setup --base main --checkout`
- `make init STACK=<python|react> PROJECT_NAME="<name>" DESCRIPTION="<desc>"`
  (this promotes the chosen stack to root, prunes the other, and removes the
  template machinery; it also refuses to run if local changes appeared after
  preflight).
- `make apply_repo_settings_bootstrap TARGET_REPO="$TARGET_REPO"`. This applies
  PR/merge preferences and security settings but deliberately defers the new
  required CI checks until the workflow exists on `main`. It prints the planned
  operations and asks for confirmation. If the shell cannot answer, show the plan
  to the user, confirm through the interview tool, then rerun the script with
  `--yes`; never skip confirmation.
- Review `git status`, `git diff --stat`, and `git diff --name-status`. Confirm the
  changes are only legitimate initialization output, then stage and commit:
  `git add -A && git commit -m "chore: initialize from agentic-ai-dev-template"`.
- Run `make deps && make pr_check` yourself to confirm the stack is green
  (fix anything red before proceeding).
- Reconfirm `git remote get-url origin` matches `TARGET_REPO`, push only there,
  and open the PR explicitly against the same repository:
  `git push -u origin HEAD` then `gh pr create --repo "$TARGET_REPO" --base main`.
  Populate the repository's PR template with the stack, settings result, and test
  evidence, and include `Closes #<N>`; do not replace the template with a one-line
  body. Tell the user to review and squash-merge it.
- AFTER the setup PR merges, update local `main` and run
  `make finalize_repo_settings TARGET_REPO="$TARGET_REPO"`. Show the final plan
  and honor its confirmation prompt. This second phase enables the required
  `lint`, `tests`, and `security` checks now that the CI workflow exists on main.
- Tell the user that automated PR review (Cursor Bugbot) may need to be enabled
  on this new repo — it is configured per-repo, not inherited from the template.
  Two steps:
    1. Install/approve the Cursor GitHub App for the repo (request access, or
       approve if you're a GitHub org admin):
       https://github.com/apps/cursor/installations
    2. Enable the repo on the Cursor side:
       https://cursor.com/dashboard/bugbot/installation
  Once enabled, Bugbot reviews automatically on each push (no `bugbot run`
  comment needed).

Do not use implicit GitHub repository detection for any mutation. Do not invent
settings, skip confirmation prompts, or continue after a preflight failure.
```
<!-- INTERVIEW:end -->

## What this template provides

- **Dual stacks** under `stacks/python` and `stacks/react`, each runnable and
  CI-green out of the box; `make init` collapses to the one you choose.
- **Shared agent infra:** `AGENTS.md` (single source of truth) routing to
  `.agents/rules/`, plus harness-agnostic skills in `.agents/skills/` (Claude Code
  reads them via the `.claude/skills` symlink; Cursor natively).
- **`Makefile` orchestration** with identical verbs across stacks
  (`make deps|format|lint|tests|coverage|security|pr_check`) and `make cc` to drive
  Claude Code.
- **Canonical repo settings** in `.github/repo-settings/` — applied first with
  `make apply_repo_settings_bootstrap` and finalized after the setup PR merges
  with `make finalize_repo_settings`.

## Prerequisites

| To… | You need |
|-----|----------|
| Run initialization preflight and repository settings | `git`, authenticated `gh`, repository administrator permission, enabled GitHub Issues, and **`python3`** available on PATH. |
| Work in a **React** repo (after init) | **`bun`** only — no Python toolchain at all. |
| Work in a **Python** repo (after init) | **`uv`** (Python 3.13). |
| Hack on the **template itself** (run `tests/template`, `make machinery_*`) | `uv` — the machinery's own test suite uses pytest. (Template maintainers only; not template *users*.) |

The preflight checks the selected stack's package manager before any mutation.
The initialization and repository-settings scripts themselves use only the
Python standard library.
