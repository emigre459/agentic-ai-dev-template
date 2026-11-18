# The nightly skills update PR lands on its own: design

Issue: #82 (parent epic #51). Release: part of v0.2.0, labeled `minor`.

## Intent

In a private consumer repo, the nightly "Update agent skills" PR can't land without
a person. A `github-actions[bot]` push parks its `pull_request` CI at "Approve and
run". `review-gate` stays red because PR reviewers skip bot PRs, and a catch-all
CODEOWNERS owner (`* @owner`) has to approve it too. Meanwhile agents keep following
the old skill text: gridium-agent's update PR held an API-deprecation fix for 4+
days while agents kept writing the deprecated call.

When this ships, a consumer that has set an App (or PAT) secret and the lane file
gets each tag-pinned skill release onto `main` with no person involved, and every
other PR stays exactly as gated as it is today.

## Goals

1. The update workflow pushes and opens its PR **only** with a GitHub App token or a
   fine-grained PAT, so CI runs without approval. With neither secret it fails loudly.
2. `review_gate.py` gets a third lane, the **skills-update lane**. It passes the
   update workflow's own PR on **both** halves (CODEOWNERS and review) when every
   changed and renamed-from path is `skills-lock.json` or lies inside a **tag-pinned**
   vendored skill folder.
3. The update script turns on auto-merge, which goes through the merge queue where
   the repo has one.
4. Tests cover the lane and the auto-merge call. The README documents the secret, the
   lane file and the tradeoff.

## Non-goals

- Consumer switch-overs (tracked downstream).
- Changing how skills are bumped (`bump-pins.mjs`, `npx skills update`).
- Checking the *content* of the PR's new `skills-lock.json` (for example, that refs
  stay tag-shaped). The author and branch checks already limit who can open a lane PR.
- Closing the threat model `review-gate.yml` already documents: a write-access
  insider can post the status from a workflow of their own.

## Anti-goals (a failure even if it "works")

- Any PR from a person, or from any author or branch other than the configured
  ones, passing the lane, whatever paths it touches.
- One non-skill path (or a branch-tracking skill folder) in an update PR, and the PR
  still passes without the owner and the review.
- The lane changing anything in a repo that hasn't opted in.
- A PAT or App token reaching the third-party `npx skills` steps (today's isolation
  stays).

## Decided with the user

- **No token: fail loudly.** The `GITHUB_TOKEN` + `ci.yml` dispatch fallback is
  removed, not kept as a degraded path (#49's 2026-11-11 amendment).
- **Tag-pinned only.** A skill qualifies only when its `skills-lock.json` entry on the
  default branch has a `ref` matching `^v\d+\.\d+\.\d+$`, the same test `bump-pins.mjs`
  uses (`SEMVER_TAG`). A branch-tracking skill moves to unreleased upstream text, so
  its update still needs a review.
- **The lane passes both halves** for the update workflow's own PR (#82's
  2026-11-17 amendment), so a catch-all CODEOWNERS needs no change.
- **Identity comes from an opt-in file on the default branch** (approach 1 below).

## Approaches considered (identity of the update PR)

The gate has to know that a PR came from the update workflow. Branch name and
same-repo alone aren't enough: anyone with write access can push a branch named
`skills-update`. The author must match too. With an App, the author is
`<app-slug>[bot]`. With a PAT, it's the token's account.

1. **Opt-in file `.github/skills-update-lane.json` on the default branch** (picked).
   `{"branch": "skills-update", "authors": ["acme-skills[bot]"]}`. It follows the
   docs-only lane's pattern. It's read from the default-branch checkout, so a PR
   can't loosen what gates it. Without the file the lane is off. Widening it takes a
   reviewed PR. Cost: the consumer looks up the bot login once.
2. **A repository variable** passed into `review-gate.yml`. It sits next to the
   secrets, but it isn't in git, so a settings change widens the lane unreviewed.
3. **No config: any `[bot]` author on the update branch.** No setup, but any other
   App with write access qualifies, and the PAT path can't use it.

## Design

### 1. Token: App or PAT, else fail (`.github/workflows/skills-update.yml`)

- The "Choose the token" step picks the App token, else `SKILLS_UPDATE_TOKEN`. With
  neither, it emits `::error::` and exits 1. The error names both secret options
  and points at the README's "Staying up to date" section. It no longer reads
  `github.token`, and it no longer outputs `dispatch`.
- The `dispatch-ci` input is dropped from the `uses:` call. The job's
  `actions: write` permission is dropped, and so is the App token's
  `permission-actions: write`: both existed only for the dispatch.
- The template-repo skip and the half-configured-App error stay as they are.
- The header comment changes from "The PR never merges itself" / "With neither, the
  run uses GITHUB_TOKEN…" to the new behavior.

### 2. Action and script (`.github/actions/skills-update/`)

- `action.yml`: remove the `dispatch-ci` and `ci-workflow` inputs and their env. The
  `token` description says it must be an App token or a PAT (a `GITHUB_TOKEN` push
  doesn't trigger CI, and its PR can't pass the lane). The description stops
  saying "the PR never merges itself".
- `update-pr.sh`:
  - Remove `DISPATCH_CI`, `CI_WORKFLOW`, step 5 (the dispatch) and the body's "CI on
    this PR" section.
  - The body's closing line says auto-merge is on: the PR merges itself once its
    required checks pass, and it's rebuilt nightly.
  - **New step: auto-merge.** It runs after labels, and only when this run pushed
    or created the PR (an unchanged run keeps the existing setting, so a person who
    turned it off on an unchanged PR isn't overridden; new content is a new
    decision):
    `"$GH_CMD" pr merge "$pr_number" --auto --squash --match-head-commit "$head"`.
    `$head` is the SHA that is actually on the remote branch: `HEAD` after a push,
    else the existing remote SHA (an up-to-date skip leaves a local commit whose SHA
    differs from the remote one even though the trees match). With a merge queue,
    `gh` enqueues the PR, or enables auto-merge until the checks pass, and ignores the
    strategy flag.
  - If the call fails, the script prints `::error::` naming the PR and the likely
    cause (Settings → General → "Allow auto-merge" off, or the token lacks
    `contents`/`pull_requests: write`) and exits 1. The PR is already open by then.

### 3. The skills-update lane (`review_gate.py`)

**Config.** `.github/skills-update-lane.json` on the default branch, read via
`REPO_ROOT`:

```json
{"branch": "skills-update", "authors": ["acme-skills[bot]"]}
```

`branch` is a non-empty string and `authors` a non-empty list of non-empty strings.
Other keys (such as `description`) are ignored. A missing file means the lane is off
(silent). An unreadable or malformed file means the lane is off, with a stderr line,
the same as `load_docs_allowlist`. The file is loaded once per process (as with
`_cached_docs_allowlist`).

**Qualifying folders.** Read from `REPO_ROOT / "skills-lock.json"`, the default
branch's lock. For each entry in `skills` whose `ref` matches `^v\d+\.\d+\.\d+$`,
the local folders are `.agents/skills/<name>/` and `.claude/skills/<name>/`, where
`<name>` is the entry's key. `skillPath` is **not** used: it is the skill's path in
the *upstream* repo, which matches the local folder only when the source lays its
skills out the way this template does. The local folder is where the skills CLI
installs the skill (`bump-pins.mjs`'s `agentFor`: `.agents/skills/<name>`, else
`.claude/skills/<name>`). Accepting both roots is safe: git only lists
`.claude/skills/<name>/…` paths when that directory is real, not a symlink. A key
that isn't a plain folder name (empty, `.` or `..`, or containing `/`) is skipped. A
missing or malformed lock, or no tag-pinned entries, turns the lane off (fail
closed).

**Pure decision function**, unit-testable without `gh`:
`skills_update_status(pull, files, lane, folders, repo) -> tuple[str, str] | None`
returns the lane's pass when **all** hold, else None:

- `pull["user"]["login"]` is in `lane.authors`;
- `pull["head"]["ref"] == lane.branch`;
- `pull["head"]["repo"]` is not null and its `full_name` equals `repo` (no forks);
- `files` is non-empty and `pull["changed_files"] == len(files)` (a capped list
  fails closed);
- every `filename` and every `previous_filename` is `skills-lock.json` or starts
  with `<folder>/` for some qualifying folder.

The pass is `("success", "skills-update: tag-pinned skills only, no review required")`
(under 140 characters).

**Wiring.** A `_skills_update(repo, pr, head_sha)` I/O wrapper reads nothing unless
the lane file loaded. It fetches the pull, fails closed (None) when the pull's head
SHA isn't `head_sha` (docs-only does the same), then lists the files and calls the
pure function. In `_want`:

- with a reviewer: `collect_state` → **lane** → ownership → docs-only → reviewer;
- with no reviewer (`adapter is None`), the lane is checked before `_ownership`.
  With no `head_sha` known yet, the wrapper takes the pull's own head. `main()`
  is unchanged: only CODEOWNERS or a reviewer turns the status on, so with neither
  nothing is posted, whatever the lane file says.

The module docstring and the `post_for_pr`/`_want` docstrings describe the new order.

**Example file.** `.agents/skills/resolve-pr-concerns/references/skills-update-lane.example.json`,
with a `description` key, like the docs-only example. The template ships no
`.github/skills-update-lane.json`, and a test asserts that, as for docs-only.

### 4. Docs

- **README, "Staying up to date":** the secrets table loses the "neither" row and
  says the run fails without a secret. The App needs contents and pull requests:
  write (no longer actions). The "never merges itself" bullet becomes auto-merge
  (merge queue where there is one, needs "Allow auto-merge", which repos made from
  the template already have via `merge-settings.json`). The paragraph on the
  `GITHUB_TOKEN` path's `gh workflow run ci.yml` requirements goes. The `skills
  drift` triggers requirement stays, minus `workflow_dispatch`.
- **README, new `.github/skills-update-lane.json` subsection** beside the docs-only
  one: what it does, how to find the bot login (App: `<slug>[bot]`; PAT: the
  token's account login), the copy command for the example, and the tradeoff.
  Upstream skill text then reaches agents without anyone reading it, which is why
  only tag-pinned skills (released upstream versions) qualify, and why a
  branch-tracking skill, any other path, or anyone else's PR still needs the owner
  and the review.
- **README, "Start here" interview:** a step telling the user to set the App
  secrets (recommended) or the PAT secret before the first nightly run, because
  without one the run fails. Creating `.github/skills-update-lane.json` is an
  optional follow-up once the bot login is known. `test_readme_workflow.py` guards
  the step, as it does the docs-only one.
- **PAT identity (README):** a PAT used with the lane must belong to a dedicated
  machine account, never a person, because the lane trusts its `authors`. The lane
  file must never list a person. The App is the recommended path. *(My call, from
  the builder check.)*
- **`resolve-pr-concerns/SKILL.md`:** one paragraph beside "Docs-only PRs" saying a
  PR in the skills-update lane needs no review request, and `review-gate` passes it
  as `skills-update: …`.
- Any other text saying the update PR "never merges itself" (grep the repo,
  including `upgrade-deps`) is updated.

### 5. Drop the Copilot ruleset rule (decided with the user)

The template's `main` ruleset (`.github/repo-settings/ruleset.json`) also requires
every review thread to be resolved, and its `copilot_code_review` rule auto-requests
Copilot. Copilot threads on the update PR would stall it even with a green gate. The
user doesn't use Copilot and wants the reviewer config we author
(`.github/pr-reviewer.json` + `review-gate`) as the single source of truth for
reviews, so:

- Remove the `copilot_code_review` rule from `ruleset.json`. Thread resolution stays
  required.
- Update `docs/initialization-live-test.md`'s note about that rule, and any test that
  lists the ruleset's rule types.
- The live template repo's ruleset is **not** changed by this PR. Re-applying it
  (`make finalize_repo_settings TARGET_REPO=…`) is a separate, outward-facing step
  that needs the user's OK after merge.
- Other threads (the configured reviewer's, a person's) can still need a person to
  resolve them. The README says so, and the live test checks what actually happens on
  an App-authored update PR. A follow-up card follows if it stalls routinely.

### 6. Tests

- `tests/template/test_review_gate.py`:
  - the pure function: a pass on lock + tag-pinned folders under either root. None
    for: a wrong author, a wrong branch, a fork (including a null head repo), a
    branch-tracking folder, a non-skill path, a renamed-from non-skill path, a capped
    file list, an empty list, and a folder-prefix lookalike
    (`.agents/skills/build-from-issue-x/…`);
  - a lock entry whose upstream `skillPath` differs from its local folder (for
    example `skills/pdf/SKILL.md`) still qualifies by name, and an upstream path
    such as `src/foo/SKILL.md` never lets `src/foo/` through;
  - config loading: missing, malformed, and mistyped fields all mean off;
    lock-derived folders cover tag-pinned entries only;
  - wiring through `post_for_pr` with a catch-all CODEOWNERS (`* @owner`): the
    update PR passes; a person's PR on the same paths still needs the owner; an
    update PR with one non-skill path needs both halves; a head that moved between
    reads fails closed; the no-reviewer path passes too;
  - the template ships no `.github/skills-update-lane.json`, and the example parses
    as a valid lane config.
- `tests/template/test_skills_update.py`:
  - auto-merge is called with `--auto --squash --match-head-commit <remote SHA>`
    after a push, after a create on an already-current branch (the remote SHA, not the
    local one), and not on an unchanged run;
  - an auto-merge failure exits 1 with `::error::`;
  - the dispatch tests and the `GITHUB_TOKEN` body note are removed; there's no
    `workflow run` call;
  - the workflow: the token step fails with no App or PAT, the precedence is
    App then PAT, there's no `github.token` in the pick, no `dispatch-ci` input, and
    no `actions: write`;
  - the action inputs are `token`, `branch`, `base` and `labels`.
- The README guards in `test_readme_workflow.py` are extended for the new subsection
  if that file guards docs sections that way.

## Left to the builder

- Exact helper names and error wording, within the shapes above.
- Whether `ci.yml` keeps its `workflow_dispatch:` trigger (harmless either way; this
  card doesn't require removing it, and `test_ci_accepts_workflow_dispatch` can stay
  or go accordingly).
