# Consumer Kit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repos install this template's skills pinned to a release, get a nightly
Dependabot-style update PR for every skill they installed, are protected by a drift check,
and cut their own releases with release-drafter, with the agent suggesting the bump and
drafting the summary paragraph.

**Architecture:** The logic lives in this repo as two **composite actions**
(`.github/actions/skills-drift/`, `.github/actions/skills-update/`), each bundling its
scripts. Repos run them from short caller workflows that reference this repo at a tag,
and Dependabot bumps that tag. Releases use release-drafter (draft on every merge,
published by a manual workflow). The template ships `skills-lock.json` pinned to its
newest release, and `make init` swaps the skill folders to that tag's content.

**Tech Stack:** GitHub Actions (composite actions, release-drafter v7), Node >= 22 stdlib
(`.mjs`), bash, Python 3.13 (stdlib) for the template machinery, pytest for every test
(Node and bash are driven through `subprocess`), the `skills` CLI (`npx -y skills@latest`).

**Spec:** `docs/superpowers/specs/2026-10-31-consumer-kit-design.md`

**Precondition:** PR #58 (issue #48) is merged. Before Task 1, rebase this branch onto
the new `origin/main`. Its only commits so far are the spec and this plan.

## Stack

| Layer | Branch | Tasks | Refs |
|---|---|---|---|
| L1 Pinned lock + drift check | `49-consumer-kit` | 1-4 | Refs #49 |
| L2 Nightly update PR | `49-l2-update-pr` | 5-6 | Refs #49 |
| L3 Release infrastructure | `49-l3-releases` | 7-8 | Refs #49 |
| L4 Agent side + docs | `49-l4-release-agent` | 9-12 | Closes #49 |

## Global Constraints

- **Lock entries for this repo:** `"source": "emigre459/agentic-ai-dev-template"`,
  `"sourceType": "github"`, `"skillPath": ".agents/skills/<name>/SKILL.md"`, `"ref"`
  (the newest `vX.Y.Z` tag; absent before the first release), and `"computedHash"`
  (the hash of the folder **at that tag**, or of the working tree when there is no
  `ref`).
- **The CLI's hash:**
  - SHA-256 over every regular file under the skill folder;
  - sorted by POSIX relative path with JavaScript `localeCompare`;
  - each file feeds `update(relativePath)` then `update(bytes)`;
  - skip directories named `.git` and `node_modules`; don't follow symlinks;
  - credit vercel-labs/skills `src/local-lock.ts` (MIT) in a header comment.
- **The template** is recognized by `scripts/init_template.py` existing at the repo
  root.
- **Composite-action refs:**
  - in the template, callers use the local path (`./.github/actions/<name>`);
  - `make init` rewrites them to
    `emigre459/agentic-ai-dev-template/.github/actions/<name>@<lock ref>` and deletes
    the local `.github/actions/skills-*` copies. Before the first release there is no
    ref, so init keeps the local path and the copies.
- **The `skills` CLI** always runs as `npx -y skills@latest ...` on Node 22, with
  `DISABLE_TELEMETRY=1` set.
- **Bump labels:** `minor`, `major`, `patch`. No label means patch. release-drafter's
  tag template is `v$RESOLVED_VERSION`.
- **Secrets** (all optional):
  - `SKILLS_UPDATE_APP_ID` + `SKILLS_UPDATE_APP_PRIVATE_KEY` (a GitHub App), or
  - `SKILLS_UPDATE_TOKEN` (a fine-grained PAT).

  Without either, the update PR uses `GITHUB_TOKEN` and dispatches `ci.yml` on the
  branch.
- **Required check context:** `skills drift`. It runs on `pull_request`, `push` to
  `main` and `merge_group`.
- **No downstream org names** anywhere (`tests/template/test_no_org_names.py`). Port
  the downstream helper's ideas without its names or issue numbers.
- **Every `SKILL.md` keeps the #48 preflight line.**
  `tests/template/test_skill_extension_points.py` must stay green, and a new skill gets
  the line too.
- **Stack mechanics follow `.agents/skills/build-from-issue/references/stacked-prs.md`:**
  set up with `gh stack init` before the first implementation commit, push with
  `gh stack push`, never merge `main` into a layer, and finish on L1.

## Review Focus

1. **Hash parity with the real CLI** for awkward paths: mixed case, `_`/`-`/`.`
   prefixes, nested dirs, an empty file, a symlink. Pinned by a golden fixture produced
   by the real CLI (Task 1).
2. **A lock entry whose folder is missing,** or exists only under `.claude/skills/`,
   is reported clearly rather than crashing or passing (Task 1).
3. **The update helper is idempotent:**
   - no change means no push;
   - an open PR is reused, not duplicated;
   - a missing label never aborts PR creation;
   - a GITHUB_TOKEN run dispatches CI exactly when a commit landed or the PR is new
   (Task 5).
4. **Tag lookup** ignores prereleases and non-semver tags. A source with no releases
   leaves its pinned entry alone and says so in the PR body (Task 5).
5. **`make init` with no network, or with a hash mismatch, fails loudly** before
   anything is deleted, so a retry works (Task 3).

---

### Task 1: Hash + drift-check script (L1)

**Files:**
- Create: `.github/actions/skills-drift/skills-lock.mjs`
- Create: `.github/actions/skills-drift/action.yml`
- Create: `tests/template/fixtures/skills_lock/` (fixture skill tree plus a
  `golden.json` produced by the real CLI)
- Test: `tests/template/test_skills_lock.py`

**Interfaces:**
- Produces, as a CLI: `node skills-lock.mjs hash <dir>` prints the hex hash.
- Produces: `node skills-lock.mjs check [--root <repo>] [--template]` exits:
  - 0 when every entry matches;
  - 1 on drift or a missing folder, with one line per problem
    (`DRIFT <name>: ...` / `MISSING <name>: ...`) plus a remedy line;
  - 2 on a usage error or bad lock.
- Produces: `node skills-lock.mjs write --root <repo> [--tag vX.Y.Z]`, used by Task 2.
- Produces: `action.yml`, a composite action with input `root` (default `.`). It runs
  `check`, adding `--template` when `scripts/init_template.py` exists under `root`.

- [ ] **Step 1: Make the golden fixture with the real CLI.**
  1. Create `fixtures/skills_lock/demo-skill/` holding `SKILL.md`, `Zeta.md`,
     `alpha.md`, `_private.md`, `.hidden`, `sub/b.txt`, `sub/A.txt` and an empty
     `empty.txt`.
  2. In a scratch dir, run `npx -y skills@latest add <fixture path> -y` and copy the
     resulting `computedHash` into `golden.json` as `{"demo-skill": "<hash>"}`.
  3. Record the CLI version in `golden.json` as `"cli_version"`.
- [ ] **Step 2: Write the failing tests** in `test_skills_lock.py` (skip the module
  when `node` is not on PATH):
  - `test_hash_matches_cli_golden`: `hash demo-skill` == golden.
  - `test_hash_ignores_git_node_modules_and_symlinks`: on a copy with `.git/x`,
    `node_modules/y` and a symlink added, the hash is still the golden value.
  - `test_check_passes_on_clean_install`: a temp repo with
    `.agents/skills/demo-skill/` and a lock holding the golden hash exits 0.
  - `test_check_reports_drift`: append a byte to a file; exit 1, output contains
    `DRIFT demo-skill` and `.agents/skill-config/`.
  - `test_check_finds_harness_dir`: folder only at `.claude/skills/demo-skill/`;
    exit 0.
  - `test_check_reports_missing`: no folder anywhere; exit 1 with `MISSING demo-skill`.
  - `test_check_rejects_bad_lock`: invalid JSON or no `skills` key; exit 2.
- [ ] **Step 3: Run** `uv run pytest tests/template/test_skills_lock.py -v`. Expected:
  FAIL (no script).
- [ ] **Step 4: Implement `skills-lock.mjs`** (stdlib only: `node:crypto`,
  `node:fs/promises`, `node:path`) and `action.yml`. `check` looks in
  `.agents/skills/<name>` first, then `.claude/skills/<name>`. Template mode is defined
  in Task 2; until then `--template` behaves like the plain check.
- [ ] **Step 5: Run** the same command. Expected: PASS.
- [ ] **Step 6: Commit and `gh stack push`:** "Add a skills-lock hash and drift check
  matching the skills CLI (#49)".

### Task 2: The template's lock and `make skills_lock` (L1)

**Files:**
- Modify: `.github/actions/skills-drift/skills-lock.mjs` (`write`, template-mode
  `check`)
- Create: `skills-lock.json`
- Modify: `Makefile` (template section): target `skills_lock`
- Test: `tests/template/test_skills_lock.py`

**Interfaces:**
- Consumes: Task 1's hash function.
- Produces: `make skills_lock [TAG=vX.Y.Z]`, which runs
  `node .github/actions/skills-drift/skills-lock.mjs write --root . [--tag $(TAG)]`.
  - With `--tag`: each entry gets `ref` = the tag and the hash of
    `git archive <tag> .agents/skills/<name>`, extracted to a temp dir.
  - Without it: no `ref`, hash of the working tree.
  - Entries are sorted by name, written as 2-space JSON with a trailing newline.
- Produces: in template mode (`check --template`), an entry with a `ref` is verified
  against that tag's content (it needs the tag fetched; the action fetches tags) and
  one without against the working tree.

- [ ] **Step 1: Write the failing tests:**
  - `test_write_without_tag`: in a temp git repo with two skills, `write` produces two
    sorted entries with no `ref`, and hashes equal to `hash <dir>`.
  - `test_write_with_tag`: commit, tag `v0.1.0`, edit a skill, then `write --tag
    v0.1.0`. The edited skill's hash is the tagged content's, not the working tree's.
  - `test_template_check_ignores_unreleased_edits`: after the previous test,
    `check --template` exits 0 while plain `check` exits 1.
  - `test_shipped_lock_is_current`: on the real repo, `check --template` exits 0 and
    the lock names exactly the directories under `.agents/skills/`.
- [ ] **Step 2: Run** and expect FAIL.
- [ ] **Step 3: Implement** `write` and template-mode `check`, and the `skills_lock`
  target. Generate `skills-lock.json` with `make skills_lock` (no release exists, so no
  `TAG`).
- [ ] **Step 4: Run** `uv run pytest tests/template/test_skills_lock.py -v`. Expected:
  PASS.
- [ ] **Step 5: Commit and push:** "Ship skills-lock.json for the template's own
  skills (#49)".

### Task 3: `make init` starts on the pinned release (L1)

**Files:**
- Modify: `scripts/init_template.py`
- Test: `tests/template/test_initialize.py`

**Interfaces:**
- Produces: `_pin_skills_to_release(root: Path, runner: Callable[..., object]) -> None`
  in `init_template.py`. For each lock entry with a `ref`:
  1. run `git fetch --depth 1 https://github.com/<source>.git tag <ref>`;
  2. replace `.agents/skills/<name>/` with `git archive FETCH_HEAD .agents/skills/<name>`;
  3. verify the hash with `node <action dir>/skills-lock.mjs hash`.

  When `node` is missing, it uses a Python port of the hash (it only ever hashes this repo's own skills), tested against the same
  golden fixture.
- Produces: `_point_actions_at_release(root: Path, ref: str | None) -> None`:
  - rewrites `uses: ./.github/actions/skills-<x>` to
    `uses: emigre459/agentic-ai-dev-template/.github/actions/skills-<x>@<ref>` in
    `.github/workflows/*.yml`;
  - deletes `.github/actions/skills-*`;
  - does nothing when `ref` is None.
- Both run in `initialize()` **before** step 5 (settings) and step 6 (self-destruct),
  after the stack rewrites.

- [ ] **Step 1: Write the failing tests** (fake `runner`; no network):
  - `test_init_swaps_skills_to_pinned_tag`: the fake fetch and archive supply a skill
    tree whose hash equals the lock's. Afterwards the folder holds that content.
  - `test_init_fails_on_hash_mismatch_before_deleting_anything`: the fake supplies
    different bytes. `RuntimeError` naming the skill; `scripts/init_template.py` still
    exists.
  - `test_init_fails_loudly_when_fetch_fails`: the fake fetch raises
    `CalledProcessError`. `RuntimeError` mentioning network access.
  - `test_init_without_ref_keeps_tree_and_local_actions`: a lock with no `ref` leaves
    the skills and `uses: ./.github/actions/...` untouched.
  - `test_init_points_actions_at_release`: with a `ref`, workflows reference the remote
    action at that tag and `.github/actions/skills-drift` is gone.
  - `test_python_hash_port_matches_golden`.
- [ ] **Step 2: Run** `uv run pytest tests/template/test_initialize.py -v`. Expected:
  the new tests FAIL.
- [ ] **Step 3: Implement both functions and the Python hash port.**
- [ ] **Step 4: Run** the same command plus `uv run pytest tests/template -q`.
  Expected: PASS.
- [ ] **Step 5: Commit and push:** "Start repos made from the template on the pinned
  skills release (#49)".

### Task 4: Wire the drift check into CI as a required check (L1)

**Files:**
- Modify: `.github/workflows/ci.yml`: a stack-independent job `skills-drift` (name
  `skills drift`). It runs `actions/checkout@v7` (`fetch-depth: 0`, `fetch-tags:
  true`), `actions/setup-node@v7` (Node 22), then `uses: ./.github/actions/skills-drift`.
  It sits outside the `STACK` markers, so it survives init.
- Modify: `.github/repo-settings/ruleset.json`: add the required context `skills drift`.
- Modify: `scripts/init_template.py`: `_CI_JOBS` gains `"skills drift"`.
- Test: `tests/template/test_initialize.py`, `tests/template/test_skills_lock.py`

- [ ] **Step 1: Write the failing tests:**
  - `test_ci_runs_skills_drift_everywhere`: parse `ci.yml`. The job exists, has
    `name: skills drift`, and the workflow triggers include `merge_group`.
  - `test_ruleset_requires_skills_drift`.
  - Extend `test_initialize_rewrites_required_status_checks` to expect `skills drift`
    among the seeded repo's contexts.
- [ ] **Step 2: Run** and expect FAIL. **Step 3: Implement.** **Step 4: Run**
  `uv run pytest tests/template -q` and expect PASS.
- [ ] **Step 5: Commit and push:** "Require the skills drift check in CI (#49)".

### Task 5: The update-PR action (L2)

**Files:**
- Create: `.github/actions/skills-update/action.yml`
- Create: `.github/actions/skills-update/bump-pins.mjs`
- Create: `.github/actions/skills-update/update-pr.sh`
- Test: `tests/template/test_skills_update.py`

**Interfaces:**
- Produces: `action.yml` (composite):
  - inputs: `token` (required), `dispatch-ci` (`"true"`/`"false"`), `branch`
    (default `skills-update`), `base` (default `main`), `ci-workflow` (default
    `ci.yml`), `labels` (default `dependencies`);
  - steps: copy `update-pr.sh` to `$RUNNER_TEMP`, rebuild `branch` from `origin/base`,
    run `bump-pins.mjs`, run `npx -y skills@latest update -p -y`, then run the helper.
- Produces: `node bump-pins.mjs --root <repo>` prints JSON
  `{"bumped":[{name,source,from,to,notes_url}], "available":[{source,name}],
  "removed":[{name,source}], "no_releases":[source]}`. It reinstalls each bumped
  source's installed skills at the new tag with the `skills` CLI.
  - "Newest" means the highest `vMAJOR.MINOR.PATCH` among the source's **published,
    non-prerelease** GitHub releases.
- Produces: `update-pr.sh`, a port of the prior art's helper (same preconditions,
  exit codes and `GH_CMD` seam). It adds:
  - the PR body is built from `$SKILLS_UPDATE_SUMMARY` (`bump-pins.mjs`'s JSON);
  - `DISPATCH_CI` controls the workflow dispatch;
  - a no-drift run closes an open update PR with a comment.

- [ ] **Step 1: Write the failing tests:**
  - **`bump-pins.mjs`** (fake `gh` and fake `npx` on PATH):
    - `test_bumps_to_newest_stable_semver`: releases `v0.1.0`, `v0.2.0`,
      `v0.3.0-rc.1` and `nightly`; a pin at `v0.1.0` is bumped to `v0.2.0`.
    - `test_leaves_branch_tracking_entries_alone`: no `ref`, no reinstall.
    - `test_source_without_releases_is_reported`.
    - `test_new_upstream_skill_listed_as_available_not_installed`.
    - `test_removed_upstream_skill_kept_and_flagged`.
  - **`update-pr.sh`** (temp git repo with a bare `origin`, fake `gh`):
    - `test_no_drift_exits_0_without_push`;
    - `test_no_drift_closes_stale_update_pr`;
    - `test_first_run_pushes_creates_pr_and_dispatches`;
    - `test_second_identical_run_reuses_pr_and_skips_push_and_dispatch`;
    - `test_missing_label_does_not_block_pr`;
    - `test_dispatch_disabled_with_token_path`;
    - `test_body_lists_bumps_available_and_removed`.
- [ ] **Step 2: Run** `uv run pytest tests/template/test_skills_update.py -v` and expect
  FAIL. **Step 3: Implement.** **Step 4: Run** and expect PASS.
- [ ] **Step 5: Commit and push:** "Add the nightly skills update-PR action (#49)".

### Task 6: The caller workflow and token path (L2)

**Files:**
- Create: `.github/workflows/skills-update.yml`:
  - `schedule` (one nightly cron) and `workflow_dispatch`;
  - `permissions: contents write, pull-requests write, actions write`;
  - `env: DISABLE_TELEMETRY: "1"`;
  - a job-level `if:` skips the template;
  - steps: `actions/create-github-app-token` when the App secrets exist, otherwise
    `SKILLS_UPDATE_TOKEN`, otherwise `github.token`; `dispatch-ci` is `"true"` only on
    the `github.token` path; then `uses: ./.github/actions/skills-update`.
- Modify: `.github/workflows/ci.yml`: add the `workflow_dispatch:` trigger.
- Test: `tests/template/test_skills_update.py`

- [ ] **Step 1: Write the failing tests:**
  - `test_update_workflow_skips_template`: the job's `if:` references
    `scripts/init_template.py` through a step output or `hashFiles`.
  - `test_update_workflow_token_precedence`: the App, then the PAT, then `github.token`;
    `dispatch-ci` is true only for `github.token`.
  - `test_ci_accepts_workflow_dispatch`.
- [ ] **Step 2: Run, expect FAIL; implement; run, expect PASS**
  (`uv run pytest tests/template -q`).
- [ ] **Step 3: Commit and push:** "Run the skills update PR nightly with an optional
  token (#49)".

### Task 7: Bump labels in repository settings (L3)

**Files:**
- Create: `.github/repo-settings/labels.json`: `[{"name":"patch",...},{"name":"minor",...},{"name":"major",...}]`,
  each with a color and a description that says what it bumps.
- Modify: `scripts/apply_repo_settings.py`:
  - `load_desired` reads `labels.json`;
  - the plan gains kind `"label"` (create a missing label, update a changed
    color/description, never delete);
  - applied in **both** phases.
- Test: `tests/template/test_apply_repo_settings.py`

- [ ] **Step 1: Write the failing tests:**
  - `test_plan_creates_missing_labels`;
  - `test_plan_updates_changed_label_color`;
  - `test_plan_never_deletes_extra_labels`;
  - `test_bootstrap_phase_includes_labels`;
  - `test_apply_creates_labels_via_gh` (the fake runner records
    `POST repos/<r>/labels`).
- [ ] **Step 2: Run, expect FAIL; implement; run, expect PASS.**
- [ ] **Step 3: Commit and push:** "Create the semver bump labels with the repo
  settings (#49)".

### Task 8: release-drafter, the publish workflow, and retiring CHANGELOG.md (L3)

**Files:**
- Create: `.github/release-drafter.yml`:
  - `name-template` and `tag-template` `v$RESOLVED_VERSION`;
  - `version-resolver`: `major` label, then `minor`, with `default: patch`;
  - `template: "$CHANGES"`.
- Create: `.github/workflows/release-drafter.yml`: on `push` to `main` only;
  `release-drafter/release-drafter@v7`; `permissions: contents write,
  pull-requests read`.
- Create: `.github/workflows/release.yml`:
  - `workflow_dispatch` with a required string input `summary`;
  - one job runs `release-drafter/release-drafter@v7` with `publish: true` and
    `header: ${{ inputs.summary }}`;
  - outputs `tag_name` and `html_url` in the job summary.
- Delete: `CHANGELOG.md`.
- Modify:
  - `tests/template/test_root_hygiene.py`: drop it from the allowed list;
  - `.agents/skills/build-from-issue/SKILL.md` Step 11: the parenthetical points at
    GitHub Releases;
  - `.github/review-guidelines.md`: drop `CHANGELOG` from the list.
- Test: `tests/template/test_release_workflows.py`

- [ ] **Step 1: Write the failing tests:**
  - `test_release_drafter_defaults_to_patch_with_label_overrides`;
  - `test_drafter_runs_only_on_push_to_main` (no `merge_group`, no `pull_request`);
  - `test_release_requires_summary_and_publishes`;
  - `test_no_changelog_remains`: no tracked file other than the spec, plan and
    test-kit docs mentions `CHANGELOG.md`.
- [ ] **Step 2: Run, expect FAIL; implement; run, expect PASS**
  (`uv run pytest tests/template -q`).
- [ ] **Step 3: Commit and push:** "Draft releases with release-drafter and publish by
  hand; retire CHANGELOG.md (#49)".

### Task 9: `build-from-issue` suggests a version bump (L4)

**Files:**
- Modify: `.agents/skills/build-from-issue/SKILL.md`:
  - Step 2 gains a "Release check" bullet block;
  - Step 11 gains the label step;
  - the stacked finalize puts the label on the top layer.
- Test: `tests/template/test_release_workflows.py`

Content to add:
- **At Step 2:**
  - `gh release list --exclude-drafts --limit 1 --json tagName,publishedAt`;
  - count the PRs merged since then (`gh pr list --state merged --search
    "merged:>=<date>" --json number`);
  - add one line to the read-back: "Last release `vX.Y.Z`, N days ago, M PRs since.
    This card reads as a **patch / minor / major / no release** bump because <one
    reason>."
  - with no release yet, suggest the first (`v0.1.0`);
  - the user can correct it.
- **At Step 11:** after the PR exists, `gh pr edit <n> --add-label minor` or `major`
  when that's the agreed bump (patch: no label). On a stack, label the top layer.

- [ ] **Step 1: Write the failing test** `test_build_from_issue_mentions_release_check`:
  the skill text contains `gh release list`, `--add-label`, and the words `patch`,
  `minor` and `major`.
- [ ] **Step 2: Run, expect FAIL; edit; run, expect PASS.**
- [ ] **Step 3: Commit and push:** "Have build-from-issue suggest the release bump
  (#49)".

### Task 10: The `release` skill (L4)

**Files:**
- Create: `.agents/skills/release/SKILL.md`: frontmatter `name: release`, a
  description with triggers ("cut a release", "release", "publish a version"), and the
  #48 preflight line for `release`.
- Modify: `skills-lock.json` (regenerated with `make skills_lock`).
- Test: `tests/template/test_skill_extension_points.py` (already parametrized over
  every skill), plus `tests/template/test_release_workflows.py`.

Steps the skill prescribes:
1. Find the draft release:
   `gh api repos/{o}/{r}/releases --jq '[.[]|select(.draft)][0]'`.
   No draft means nothing to release; stop.
2. Show its version and the merged-PR list. To change the version, relabel PRs and
   wait for the drafter.
3. Draft **at most one paragraph** on what the release delivers.
4. Get the user's explicit OK on the version and the paragraph with `AskUserQuestion`.
   Never publish without it.
5. Publish: `gh workflow run release.yml -f summary="<paragraph>"`, then wait with
   `gh run watch`.
6. Verify the tag and the published release exist (`gh release view vX.Y.Z`), and
   report the links.
7. **Template only:** open the re-pin PR. On branch `release-pin-vX.Y.Z`, run
   `make skills_lock TAG=vX.Y.Z`, commit and open the PR.

- [ ] **Step 1: Write the failing test** `test_release_skill_gates_publish_on_user_ok`:
  the skill text contains `AskUserQuestion`, `release.yml`, `make skills_lock TAG=` and
  `at most one paragraph`. The existing preflight tests now cover `release` too.
- [ ] **Step 2: Run, expect FAIL; write the skill; run
  `uv run python .agents/skills/review-skill/scripts/validate.py .agents/skills/release`
  (0 failed); `make skills_lock`; run `uv run pytest tests/template -q`; expect PASS.**
- [ ] **Step 3: Commit and push:** "Add a release skill that drafts the summary and
  publishes on the user's OK (#49)".

### Task 11: README and AGENTS.md (L4)

**Files:**
- Modify: `README.md`, new sections:
  - "Installing the skills": `npx skills add emigre459/agentic-ai-dev-template#vX.Y.Z`,
    pinning, and `DISABLE_TELEMETRY=1`;
  - "Staying up to date": the nightly PR, the token options, and adopting the kit in an
    existing repo with the two caller workflows;
  - "The drift check";
  - "Cutting a release": the labels, the `release` skill and `release.yml`, for the
    template and for your own repo.
- Modify: `AGENTS.md` `## Skills`: one sentence on the `release` skill and the
  `minor`/`major` labels.
- Test: `tests/template/test_release_workflows.py`

- [ ] **Step 1: Write the failing test** `test_readme_covers_install_pin_update_telemetry_release`:
  the README contains `DISABLE_TELEMETRY=1`, `#v`, `SKILLS_UPDATE_TOKEN`,
  `skills drift` and `release.yml`.
- [ ] **Step 2: Run, expect FAIL; write; run, expect PASS.**
- [ ] **Step 3: Commit and push:** "Document installing, updating and releasing (#49)".

### Task 12: Reviewer runbook (L4)

**Files:**
- Create: `docs/superpowers/test-kits/2026-10-31-consumer-kit-live-e2e-kit.md`

Sections, each with commands, the expected result, what failure looks like and the
first thing to check (plain ASCII in code blocks, never `git pull` on a stack branch):
0. Prerequisites.
1. Automated tests.
2. Hash parity: run `npx skills add` on a fixture and compare with `skills-lock.mjs
   hash`.
3. The drift check fails on a local edit in a scratch repo.
4. The update helper end to end in a scratch GitHub repo, no token: a PR is opened, CI
   is dispatched, and a second run reuses the PR.
5. A test release in a scratch repo: labels, the draft, `release.yml` with a summary,
   and the tag created.
6. How to review a stack: one layer at a time, bottom-up; land from the top PR.
7. A results table.

Scratch GitHub repos are created by the reviewer, never by the agent.

- [ ] **Step 1: Write the kit.** Dry-run sections 1-3 locally. Run 4-5 in a scratch
  repo only with the user's OK, otherwise mark them reviewer-run.
- [ ] **Step 2: Commit and push:** "Add a reviewer runbook for the consumer kit (#49)".
