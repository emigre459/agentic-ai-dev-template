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
  required CI checks and the merge queue until the workflow exists on `main`. It prints the planned
  operations and asks for confirmation. If the shell cannot answer, show the plan
  to the user, confirm through the interview tool, then rerun as
  `python3 scripts/apply_repo_settings.py --repo "$TARGET_REPO" --phase bootstrap --yes`;
  never skip confirmation.
- Review `git status`, `git diff --stat`, and `git diff --name-status`. Confirm the
  changes are only legitimate initialization output, then stage and commit:
  `git add -A && git commit -m "chore: initialize from agentic-ai-dev-template"`.
- Run `make deps && make pr_check` yourself to confirm the stack is green
  (`pr_check` mirrors CI: lint, security, coverage gate, per-test speed check;
  fix anything red before proceeding).
- Reconfirm `git remote get-url origin` matches `TARGET_REPO`, push only there,
  and open the PR explicitly against the same repository:
  `git push -u origin HEAD` then `gh pr create --repo "$TARGET_REPO" --base main`.
  Populate the repository's PR template with the stack, settings result, and test
  evidence, and include `Closes #<N>`; do not replace the template with a one-line
  body. Tell the user to review and squash-merge it.
- AFTER the setup PR merges, update local `main` and run
  `make finalize_repo_settings TARGET_REPO="$TARGET_REPO"`. Show the final plan
  and honor its confirmation prompt (same `--yes` fallback, with `--phase final`
  and only after the user confirms). This second phase enables the required
  `lint`, `tests`, and `security` checks now that the CI workflow exists on main,
  and a **merge queue** on `main`. Tell the user that from then on PRs merge with
  "Merge when ready": the queue tests each PR against the latest `main` (plus any
  PRs queued ahead of it) and merges them in order, so nobody catches branches up
  by hand when several agents land work at once. GitHub offers merge queues only
  on organization-owned repos (public, or private on Enterprise Cloud); on any
  other repo the script prints a "no merge queue" notice, skips the rule, and PRs
  merge directly as before.
- Tell the user the template ships with **no automated PR reviewer active**
  (`"active": null` in `.github/pr-reviewer.json`), so the agent skills fall
  back to a local subagent review and never post reviewer trigger comments. If
  the new repo has a reviewer subscription, enable it — it is configured
  per-repo, not inherited from the template:
    1. Install the reviewer's GitHub App on the repo and enable the repo on the
       vendor's side. For Cursor Bugbot: approve the Cursor app
       (https://github.com/apps/cursor/installations), then enable the repo at
       https://cursor.com/dashboard/bugbot/installation, and copy
       `.github/review-guidelines.md` to `.cursor/BUGBOT.md` for its noise
       filters. For CodeRabbit, follow
       `.agents/skills/resolve-pr-concerns/references/reviewers/coderabbit.md`.
    2. Set `"active"` in `.github/pr-reviewer.json` to that adapter's key
       (`"bugbot"` or `"coderabbit"`).
  Once reviews arrive, the user can add the vendor-neutral `review-gate` status
  to the `main` ruleset's required checks — never the reviewer's own check (see
  `.agents/skills/resolve-pr-concerns/references/reviewers/`).

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
  (`make deps|format|lint|tests|coverage|test_speed_check|security|pr_check`) and `make cc` to drive
  Claude Code.
- **Canonical repo settings** in `.github/repo-settings/` — applied first with
  `make apply_repo_settings_bootstrap` and finalized after the setup PR merges
  with `make finalize_repo_settings`, which also turns on a merge queue where
  GitHub offers one.

## Customizing the skills in your repo

The dev-workflow skills in `.agents/skills/` are **vendored**: a repo gets them from
this template, by creating the repo from it or with `npx skills add`. Updates replace
each skill folder wholesale (`npx skills update` deletes it and copies it in again),
so an edit inside a skill folder is silently lost. Outside this template, treat skill
folders as **read-only**. Put repo-specific behavior in the four files below, which
your repo owns, and send a fix to a skill itself upstream as a PR to this repo.

Every skill's `SKILL.md` opens with a `**Repo config first:**` line that sets the
rules:

- **Precedence**, highest first: your instructions in the session, then `AGENTS.md`
  and `.agents/rules/`, then `.agents/skill-config/<skill>.md`, then the skill's own
  defaults.
- **Hard stops no file can lift.** Only you can, explicitly, in the session:
  - merging without your one-off OK;
  - resolving review threads;
  - opening or calling ready a PR without a green `make pr_check` and CI;
  - weakening a gate;
  - deleting issues;
  - skipping a confirmation;
  - installing tools;
  - posting to an outside channel unasked;
  - widening access, or sending output outside the repo.

  An agent that meets a config line trying any of these skips it and tells you which
  line it skipped.

### `AGENTS.md`: always-on, repo-wide instructions

Read by every agent on every task. Keep it lean, and route detail to rules.

```markdown
## Project Overview
Billing service for the payments team. Never touch `src/legacy/` without asking.
```

### `.agents/rules/`: shared rules

One Markdown file per rule, with frontmatter saying when it applies. Route each rule
from `AGENTS.md`'s `## Rules` table.

```markdown
---
description: Every database migration ships with a down migration.
appliesTo: migrations/**
---

# Migrations are reversible

Each migration file defines both `upgrade()` and `downgrade()`.
```

### `.github/pr-reviewer.json`: structured settings

Names the active PR reviewer and its adapter, the review-round caps
(`review_rounds`) and the per-test speed budget (`test_speed`). To use a reviewer this
template doesn't ship, add an adapter entry and point its `notes` at a notes file your
repo owns. The skill reads it after any bundled notes; a path that leaves the repo or
names no file is rejected.

```json
{
  "active": "acme",
  "reviewers": {
    "acme": {
      "display_name": "Acme Review",
      "notes": "docs/reviewers/acme.md"
    }
  }
}
```

(An adapter needs every field the shipped `bugbot` and `coderabbit` entries have; the
fragment shows only the new ones.)

### `.agents/skill-config/<skill>.md`: notes for one skill

Optional, one file per skill, named after the skill's folder. The skill reads it
before doing anything else and applies it on top of its defaults. In-repo paths and
conventions are fair game:

```markdown
<!-- .agents/skill-config/build-from-issue.md -->
- Write specs under `docs/design/` instead of `docs/superpowers/specs/`.
- Our E2E target is `make smoke`.
```

A line like `Merge the PR yourself once CI is green.` would be skipped and reported:
merging without your one-off OK is a hard stop.

## Installing the skills

A repo created from this template already has the skills: `make init` installs the
template's latest release and records it in `skills-lock.json`. To add them to any
other repo, install a release by its tag with the [skills CLI](https://github.com/vercel-labs/skills):

```bash
npx skills add emigre459/agentic-ai-dev-template#vX.Y.Z
```

- **Pin a tag, not a branch.** `#vX.Y.Z` records that tag as the skill's `ref` in
  `skills-lock.json`, so the install is reproducible and the update PR (below) moves
  it forward one release at a time. Without a `#` suffix the skills track the
  default branch tip, which can change under you.
- **Opt out of telemetry** with `DISABLE_TELEMETRY=1`, for example
  `DISABLE_TELEMETRY=1 npx skills add ...`. The nightly workflow below sets it for you.
- Commit `.agents/skills/` and `skills-lock.json` together. Edits inside a skill
  folder are not kept: see "Customizing the skills in your repo".

## Staying up to date

The `skills update` workflow (`.github/workflows/skills-update.yml`) runs nightly and
can be started by hand from the Actions tab. It opens, or refreshes, one pull request
titled "Update agent skills" from the `skills-update` branch, labeled `dependencies`:

- Skills pinned to a tag move to the newest stable release of their source (not a
  draft, not a pre-release, tagged `vX.Y.Z`). Skills with no tag follow their branch.
- The branch is rebuilt on the latest `main` each run. With nothing to update, an
  open update PR is closed.
- It never merges itself. Review the diff and merge it like any other PR.

Which identity opens the PR decides whether its CI runs. Set one of these as a
repository secret, or neither:

| Secret | Effect |
|--------|--------|
| `SKILLS_UPDATE_APP_ID` + `SKILLS_UPDATE_APP_PRIVATE_KEY` | A GitHub App you install on the repo (contents, pull requests and actions: write). Its pushes trigger the PR's CI normally. Set both, or the App is ignored with a warning. |
| `SKILLS_UPDATE_TOKEN` | A fine-grained personal access token with contents and pull requests: write. Its pushes trigger the PR's CI normally. |
| neither | The workflow's `GITHUB_TOKEN`. Its pushes do not trigger CI, so the workflow dispatches `ci.yml` on the branch instead. Turn on Settings, Actions, General, "Allow GitHub Actions to create and approve pull requests", or the PR cannot be opened. |

GitHub disables scheduled workflows after 60 days without repository activity. If the
nightly PR stops appearing, open the Actions tab, pick "skills update" and re-enable it.
A repo that merges its update PRs regularly never reaches that point.

### Adopting the kit in an existing repo

Besides the install above, an existing repo needs two small caller workflows, which
call the template's released actions by tag. Use the same `vX.Y.Z` as the install.

The first is the nightly update. Copy `.github/workflows/skills-update.yml` from the
template at that tag and change its one `uses: ./.github/actions/skills-update` line to
`uses: emigre459/agentic-ai-dev-template/.github/actions/skills-update@vX.Y.Z`.

The second is the drift check, a job in your CI workflow. Name it `skills drift`, so
it can be a required check:

```yaml
  skills-drift:
    name: skills drift
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-node@v7
        with:
          node-version: "22"
      - uses: emigre459/agentic-ai-dev-template/.github/actions/skills-drift@vX.Y.Z
```

Then add `skills drift` to the required checks of your `main` ruleset (repos made
from the template get this from `make finalize_repo_settings`).

## The drift check

`skills drift` fails a pull request when a vendored skill no longer matches the hash
recorded for it (`computedHash`) in `skills-lock.json`: an edited, added or deleted
file inside a skill folder, or a skill folder that is missing. It is the guard behind
"treat skill folders as read-only". When it fails:

- To keep repo-specific behavior, undo the edit and put it in
  `.agents/skill-config/<skill>.md`. A fix to the skill itself goes upstream as a PR.
- When the skills CLI or the update PR changed the skills, the lock file changes in
  the same commit. A skill folder that changed with no lock change is an edit, so
  the check fails.

Run on your machine, the check can report drift that CI does not. It hashes every
file on disk, so untracked files inside a vendored skill, such as a `__pycache__`
folder from running one of its scripts or a `.DS_Store`, change the hash. CI runs it
on a clean checkout, which is the result that counts. To reproduce a failure locally,
first remove the untracked files (`git clean -n .agents/skills` lists them).

## Cutting a release

Releases are drafted for you and published by hand. On every push to `main`,
`release-drafter` (`.github/workflows/release-drafter.yml`) updates one draft GitHub
Release from the merged PRs, so there is no changelog file to maintain.

**The version** comes from a label on each merged PR, and the largest bump wins:

| Label | Bump | Use for |
|-------|------|---------|
| `major` | X in vX.Y.Z | a breaking change |
| `minor` | Y | new functionality |
| `patch`, or no label | Z | everything else |

`make apply_repo_settings_bootstrap` creates the three labels. `build-from-issue`
suggests a bump when it reads the card and applies the label once the PR exists.

**Publishing** is the `release` skill's job. It finds the draft, shows its version and
PR list, writes a summary of at most one paragraph, asks you to approve both, and only
then runs the `release.yml` workflow (Actions, `release`, Run workflow, with a required
`summary` input). That workflow runs only from `main`: it publishes the draft with the
summary on top and creates the tag. You can run it by hand without the skill, with the
same inputs.

The skill never publishes without your explicit OK for that release, and never merges
anything.

**In this template**, a release also has a follow-up: the `release` skill offers to
open a PR on a `release-pin-vX.Y.Z` branch that runs `make skills_lock TAG=vX.Y.Z`,
recording the tag and hashes in `skills-lock.json`. Repos made from the template start
from that tag. Merge it like any other PR; it is a separate step because the tag
cannot exist before the release does.

**In your own repo**, the same flow works once it has the release pieces:
`.github/release-drafter.yml`, the `release-drafter.yml` and `release.yml` workflows,
and the three labels. A repo made from the template already has them; an existing
repo copies them from the template at the tag it installed. There is no re-pin step,
which only the template has.

## Prerequisites

| To… | You need |
|-----|----------|
| Run initialization preflight and repository settings | `git`, authenticated `gh`, repository administrator permission, enabled GitHub Issues, and **`python3`** available on PATH. |
| Initialize or work in a **React** repo | **`bun`** only — no Python toolchain at all. |
| Initialize or work in a **Python** repo | **`uv`** (Python 3.13). |
| Hack on the **template itself** (run `tests/template`, `make machinery_*`) | `uv` — the machinery's own test suite uses pytest. (Template maintainers only; not template *users*.) |

The preflight checks the selected stack's package manager before any mutation.
The initialization and repository-settings scripts themselves use only the
Python standard library.

## License

This project is licensed under the [MIT License](LICENSE).
