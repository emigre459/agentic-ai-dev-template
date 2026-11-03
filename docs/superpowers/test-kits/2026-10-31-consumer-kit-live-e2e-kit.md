# Reviewer runbook: the consumer kit (#49)

**Who this is for:** a reviewer of the #49 stack who wasn't in the session that built
it. About 45 minutes: sections 0 to 3 are local and take about 10; sections 4 and 5
create a scratch GitHub repo that you own and take about 30. Every check says what you
should see, what failure looks like, and the first thing to look at.

**What the stack does, in one paragraph.** This repo publishes its dev-workflow skills
for other repos to install with `npx skills add`. #48 made the installed skill folders
read-only and gave repos files of their own to customize them. #49 gives repos the rest:
install pinned to a release tag; a nightly "Update agent skills" PR that moves the pins
forward, the way Dependabot does; a CI check that fails when a vendored skill was edited
(the drift check); and a hand-cut release flow (release-drafter, a `release` skill, a
`release.yml` workflow) with the version taken from PR labels. `CHANGELOG.md` is gone in
favor of GitHub Releases. The design is
`docs/superpowers/specs/2026-10-31-consumer-kit-design.md`; the README sections to read
alongside it are "Installing the skills", "Staying up to date", "The drift check" and
"Cutting a release".

**The stack** (four PRs; the top one, L4, holds the whole change):

| Layer | Branch | PR | Owns |
|-------|--------|----|------|
| L1 | `49-consumer-kit` | `59` | `skills-lock.json`, the drift action and `skills-lock.mjs`, `make init` pinning, the required `skills drift` check |
| L2 | `49-l2-update-pr` | `60` | the nightly update: `skills-update.yml`, the `skills-update` action, `bump-pins.mjs`, `update-pr.sh` |
| L3 | `49-l3-releases` | `61` | `release-drafter.yml` and its workflow, `release.yml`, the `patch`/`minor`/`major` labels, `CHANGELOG.md` deleted |
| L4 | `49-l4-release-agent` | `62` | the `release` skill, `build-from-issue` bump suggestions, README, `AGENTS.md`, this runbook |

Sections 4 and 5 are marked **reviewer-run**: they create a GitHub repository and run
workflows in it, which only you should do. The agent that wrote this kit ran sections 0
to 3, and the local half of section 4, but nothing on GitHub.

## 0. Prerequisites

You need `git`, `gh` (logged in), `node` 22 or newer with `npx`, `uv`, and `jq`. Turn off
the skills CLI's telemetry for this session:

```bash
export DISABLE_TELEMETRY=1
node --version
gh auth status
```

**Expect:** `node` prints `v22` or higher; `gh auth status` says you are logged in.

Sections 4 and 5 push `.github/workflows/` files. If your git remotes use HTTPS, the
token needs the `workflow` scope or the push is rejected: run
`gh auth refresh -s workflow` now. (SSH remotes don't need it.)

Optional: the `gh stack` extension gives you `gh stack view` and `gh stack checkout`.
You don't need it to review.

```bash
gh extension install github/gh-stack
```

Check out the top layer (it contains every layer's change). Use a fresh clone:

```bash
gh repo clone emigre459/agentic-ai-dev-template consumer-kit-review
cd consumer-kit-review
gh pr checkout 62
REVIEW_DIR=$PWD   # later sections come back here
git branch --show-current
```

**Expect:** the last line prints `49-l4-release-agent`.

**If not:** `gh pr checkout` fails with "no pull requests found" when you are not in the
clone, or `62` was not replaced with a number.

The stack branches are force-pushed whenever the author restacks them, so never update
a layer branch from the remote with a plain pull. To refresh after the author pushes,
re-run `gh pr checkout 62 --force`, which resets the local branch to the PR head.

## 1. Automated checks

Use one terminal for the whole kit. If you open a new one, set `DISABLE_TELEMETRY`,
`REVIEW_DIR`, `ME` and `SCRATCH` again (section 0, 4b).

```bash
cd "$REVIEW_DIR"
make tooling_deps
uv run pytest tests/template/test_skills_lock.py tests/template/test_initialize.py tests/template/test_skills_update.py tests/template/test_apply_repo_settings.py tests/template/test_release_workflows.py tests/template/test_skill_extension_points.py -q
```

**Expect:** every test passes, none skipped. (When this kit was written: `287 passed`.)

**If not:** a failing file tells you the layer. `test_skills_lock.py` and
`test_initialize.py` are L1, `test_skills_update.py` is L2, `test_apply_repo_settings.py`
and `test_release_workflows.py` are L3 (the latter also covers L4's `release` skill),
and `test_skill_extension_points.py` is #48's lint, now covering the new skill. First
check that `make tooling_deps` finished without errors.

Now a shape check. Every skill needs the preflight line, and the lock lists every
skill. All three numbers should be equal:

```bash
grep -l '^\*\*Repo config first' .agents/skills/*/SKILL.md | wc -l | tr -d ' '
ls .agents/skills | wc -l | tr -d ' '
jq '.skills | length' skills-lock.json
```

**Expect:** `11`, `11`, `11` (the ten earlier skills plus `release`).

**If not:** the first number being lower means a skill lost its preflight line; the
third being lower means the lock is missing a skill (run `make skills_lock`).

Then prove the shipped-lock guard actually guards something. Change one byte in a
vendored skill:

```bash
echo x >> .agents/skills/upgrade-deps/SKILL.md
node .github/actions/skills-drift/skills-lock.mjs check --root . --template; echo "exit=$?"
uv run pytest tests/template/test_skills_lock.py -q -k shipped_lock
git checkout -- .agents/skills/upgrade-deps/SKILL.md
git status --short
node .github/actions/skills-drift/skills-lock.mjs check --root . --template; echo "exit=$?"
```

**Expect:** the first `check` prints `DRIFT upgrade-deps: ...` and `exit=1`, then a line
about running `make skills_lock`. (Once the lock is pinned after the first release, that
line says `make skills_lock TAG=<ref>` instead, and a bare `make skills_lock` refuses to
run, because it would un-pin the lock.) The pytest run reports `1 failed`
(`test_shipped_lock_is_current`). After the `git checkout`, `git status --short` prints
nothing and the second `check` prints nothing and `exit=0`.

**If not:** an `exit=0` on the edited file means the template-mode check isn't hashing
the working tree; look at the `--template` branch of `skills-lock.mjs`. Anything left in
`git status --short` means restore it with `git checkout -- .agents/skills`.

(`--template` is what the CI job uses in this repo. Until the first release exists the
lock entries have no `ref`, so each is compared with the working tree. After the first
release and its re-pin PR, each is compared with that tag's copy of the skill.)

## 2. Hash parity with the real CLI

The drift check is only useful if its hash is byte-for-byte the one `npx skills add`
writes as `computedHash`. Install a real skill with the CLI into a scratch directory,
pinned to an older release so a newer one exists (that matters in section 4):

```bash
gh api repos/github/gh-stack/releases --jq '.[].tag_name'
rm -rf /tmp/kit-parity && mkdir /tmp/kit-parity && cd /tmp/kit-parity
npx -y skills@latest add github/gh-stack#v0.2.0 --skill gh-stack -a universal -y
cat skills-lock.json
```

If `v0.2.0` is not in the list, pick any tag from it that is not the newest, and use it
for the rest of this runbook. `-a universal` puts the skill in `.agents/skills/`.

**Expect:** the CLI prints `Installed 1 skill` and `./.agents/skills/gh-stack`.
`skills-lock.json` lists `gh-stack` with `"ref": "v0.2.0"` and a `computedHash`.

Now compare it with the script's own hash of the same folder:

```bash
LOCKED=$(jq -r '.skills["gh-stack"].computedHash' skills-lock.json)
MINE=$(node "$REVIEW_DIR/.github/actions/skills-drift/skills-lock.mjs" hash .agents/skills/gh-stack)
echo "$LOCKED"
echo "$MINE"
test "$LOCKED" = "$MINE" && echo MATCH
```

**Expect:** the two hex strings are identical and `MATCH` prints. (For `v0.2.0` when
this kit was written: `6a5289601eedbeedc2d79c2d8627277afb9b9a3fbd78b4da3399a46dd788ae0a`.)

**If not:** a mismatch is the stack's worst failure: the drift check would fail on every
untouched install. Report the tag and both hashes. First check you are on the L4 checkout
(`echo "$REVIEW_DIR"`), then whether a newer `skills` CLI changed its hashing
(`npx -y skills@latest --version`).

## 3. The drift check fails on a local edit

Still in `/tmp/kit-parity`. First the clean case:

```bash
node "$REVIEW_DIR/.github/actions/skills-drift/skills-lock.mjs" check --root .; echo "exit=$?"
```

**Expect:** no output and `exit=0`.

Now edit the installed skill, as someone ignoring "treat skill folders as read-only"
would:

```bash
echo "LOCAL-EDIT" >> .agents/skills/gh-stack/SKILL.md
node "$REVIEW_DIR/.github/actions/skills-drift/skills-lock.mjs" check --root .; echo "exit=$?"
```

**Expect:** a `DRIFT gh-stack: .agents/skills/gh-stack hashes to <hash>, skills-lock.json
has <hash>` line, then advice to revert the edit or move the change to
`.agents/rules/` or `.agents/skill-config/<skill>.md`, then `exit=1`.

Reinstalling the pinned tag restores the folder, and a deleted folder is reported as
missing:

```bash
npx -y skills@latest add github/gh-stack#v0.2.0 --skill gh-stack -a universal -y
node "$REVIEW_DIR/.github/actions/skills-drift/skills-lock.mjs" check --root .; echo "exit=$?"
rm -rf .agents/skills/gh-stack
node "$REVIEW_DIR/.github/actions/skills-drift/skills-lock.mjs" check --root .; echo "exit=$?"
```

**Expect:** `exit=0` after the reinstall; after the `rm`,
`MISSING gh-stack: no folder at .agents/skills/gh-stack or .claude/skills/gh-stack`
and `exit=1`.

**If not:** `exit=0` after the edit means the check isn't comparing the folder with the
lock; look at the `check` function in `.github/actions/skills-drift/skills-lock.mjs`.
An exit of `2` means it couldn't read the lock file: check you are in `/tmp/kit-parity`.

## 4. The nightly update PR, no token (reviewer-run)

This checks the L2 workflow end to end in a repo you own: it opens one "Update agent
skills" PR that bumps `gh-stack` to its newest release, dispatches CI on the branch, and
reuses the same PR on a second run. It uses no App and no personal access token, so it
runs the `GITHUB_TOKEN` path.

Use a personal account, not an organization: an organization can forbid the setting in
step 4d.

### 4a. A local dry run of the bump (no GitHub writes)

The part of the workflow that decides what to bump is `bump-pins.mjs`. Run it against a
scratch folder first:

```bash
rm -rf /tmp/kit-bump && mkdir /tmp/kit-bump && cd /tmp/kit-bump
npx -y skills@latest add github/gh-stack#v0.2.0 --skill gh-stack -a universal -y
node "$REVIEW_DIR/.github/actions/skills-update/bump-pins.mjs" --root . 2>/dev/null
jq -c '.skills["gh-stack"] | {ref, computedHash}' skills-lock.json
node "$REVIEW_DIR/.github/actions/skills-drift/skills-lock.mjs" check --root .; echo "exit=$?"
```

**Expect:** one line of JSON whose `bumped` array holds `gh-stack` with `"from":"v0.2.0"`
and `"to"` set to the newest release (`v0.3.0` when this kit was written), and
`"available":[]`, `"removed":[]`, `"no_releases":[]`. The lock then shows the new `ref`
and a new `computedHash`, and the drift check passes (`exit=0`) on the bumped folder.

**If not:** with no `bumped` entry, check that the newest release is not a draft or
pre-release, and that its tag looks like `vX.Y.Z`
(`gh api repos/github/gh-stack/releases/latest --jq .tag_name`). Run the command without
`2>/dev/null` to see the CLI's error output.

### 4b. Create the scratch repo

```bash
ME=$(gh api user --jq .login)
cd "$(mktemp -d)"
gh repo create "$ME/skills-kit-scratch" --private --clone
cd skills-kit-scratch
git checkout -B main
SCRATCH=$PWD
echo "$ME $SCRATCH"
```

**Expect:** the last line prints your login and the new directory.

### 4c. Copy in the kit's pieces and a minimal CI

The update workflow calls `./.github/actions/skills-update`, so the action goes in
beside it. The CI workflow is the smallest one that has what the update PR needs: a
`workflow_dispatch:` trigger (the action dispatches it on the `skills-update` branch)
and a `skills drift` job.

```bash
cd "$SCRATCH"
mkdir -p .github/workflows .github/actions
cp -R "$REVIEW_DIR/.github/actions/skills-update" .github/actions/
cp -R "$REVIEW_DIR/.github/actions/skills-drift" .github/actions/
cp "$REVIEW_DIR/.github/workflows/skills-update.yml" .github/workflows/
cat > .github/workflows/ci.yml <<'EOF'
name: ci

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

jobs:
  skills-drift:
    name: skills drift
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-node@v7
        with:
          node-version: "22"
      - uses: ./.github/actions/skills-drift
EOF
ls -R .github | head -20
```

**Expect:** `.github/actions/skills-drift`, `.github/actions/skills-update` and the two
workflow files.

Now install an OLD release of `gh-stack`, pinned to a tag. `-a universal` is what makes
`skills-lock.json` pin the release:

```bash
npx -y skills@latest add github/gh-stack#v0.2.0 --skill gh-stack -a universal -y
jq -c '.skills["gh-stack"] | {ref, skillPath}' skills-lock.json
node .github/actions/skills-drift/skills-lock.mjs check --root .; echo "exit=$?"
```

**Expect:** `{"ref":"v0.2.0","skillPath":"skills/gh-stack/SKILL.md"}`, then `exit=0`.

### 4d. Allow Actions to open PRs, then push

Without this setting a `GITHUB_TOKEN` run can't open a PR. This is the one setting the
README says the no-token path needs:

```bash
gh api -X PUT "repos/$ME/skills-kit-scratch/actions/permissions/workflow" -f default_workflow_permissions=write -F can_approve_pull_request_reviews=true
git add -A
git commit -m "Seed the scratch repo with the consumer kit"
git push -u origin main
```

**Expect:** the push succeeds and the `gh api` call prints nothing.

**If not:** a push rejected with "refusing to allow an OAuth App to create or update
workflow ... without `workflow` scope" means: run `gh auth refresh -s workflow` and
push again. If the `gh api` call returns a 409 or says the setting is managed by an
organization, you are not in a personal account.

### 4e. Run the update, and check the PR

```bash
gh workflow run skills-update.yml
```

Wait a few seconds for the run to appear, then watch it:

```bash
gh run list --workflow skills-update.yml --limit 1
gh run watch "$(gh run list --workflow skills-update.yml --limit 1 --json databaseId --jq '.[0].databaseId')" --exit-status
```

If `gh workflow run` says the workflow was not found, it hasn't registered yet: wait
10 seconds and retry. If `gh run list` is empty, wait and retry.

**Expect:** the run finishes green. Then there is exactly one PR from `skills-update`:

```bash
gh pr list --head skills-update --json number,title,labels
gh pr view skills-update --json body --jq .body
gh pr diff skills-update --name-only
```

**Expect:**
- one PR titled `Update agent skills`;
- a body with a `## Bumped` section listing `gh-stack` (github/gh-stack) from `v0.2.0`
  to the newest release, with a release-notes link, and a closing line that the PR never
  merges itself;
- a `## CI on this PR` section saying `ci.yml` was dispatched with `workflow_dispatch`,
  that `pull_request`-only checks (such as `review-gate`) did not run, and that
  `SKILLS_UPDATE_TOKEN` or the App secrets give the PR its full CI;
- the diff names `skills-lock.json` and files under `.agents/skills/gh-stack/`, and
  nothing else;
- the run's log shows a warning that the `dependencies` label could not be added (the
  scratch repo has no such label; labels are best-effort and this is harmless).

Check the pin moved on the branch, and that CI was dispatched on it:

```bash
gh api -H "Accept: application/vnd.github.raw" "repos/$ME/skills-kit-scratch/contents/skills-lock.json?ref=skills-update" | jq -c '.skills["gh-stack"] | {ref, computedHash}'
gh run list --workflow ci.yml --branch skills-update --json event,status,conclusion,createdAt --jq '[.[] | select(.event == "workflow_dispatch")]'
```

**Expect:** the `ref` is the newest release, and the list holds exactly one
`workflow_dispatch` run, which finishes `success` (the `skills drift` job is green on
the bumped folder). The PR page shows that check. The filter matters: a `pull_request`
run from a `GITHUB_TOKEN` push may also exist, parked in `action_required`. Ignore it;
it is why the action dispatches CI.

**If not:**
- No PR, and the log says "GitHub Actions is not permitted to create or approve pull
  requests": the setting in 4d did not take; open Settings, Actions, General, and
  tick it.
- No CI run on the branch: look at the last step of the log, "Open or refresh the update
  PR". A failure of `gh workflow run ci.yml` there means `ci.yml` lacks
  `workflow_dispatch:` or isn't on `main`.
- A "No skill drift" line means the lock was already on the newest release: re-do 4c with
  an older tag.

### 4f. A second run reuses the PR and doesn't push

```bash
gh workflow run skills-update.yml
gh run list --workflow skills-update.yml --limit 2 --json databaseId,createdAt,status,conclusion
```

Wait a few seconds after the dispatch until the first entry is a new run: its
`createdAt` must be later than the earlier run's, and the earlier run is the one with a
`conclusion`. Then watch it and check the results:

```bash
gh run watch "$(gh run list --workflow skills-update.yml --limit 1 --json databaseId --jq '.[0].databaseId')" --exit-status
gh pr list --head skills-update --json number
gh run list --workflow ci.yml --branch skills-update --json event,createdAt --jq '[.[] | select(.event == "workflow_dispatch")]'
```

**Expect:** still exactly one PR, with the same number as before. The run's log says
`origin/skills-update already holds this update; not pushing.` and
`Branch and PR are unchanged; the existing checks remain valid.` There is still exactly
one `workflow_dispatch` run of `ci.yml` on the branch, with the same `createdAt` as
before (no second dispatch, because nothing was pushed).

**If not:** two PRs means the PR lookup by branch name failed; look at `find_pr` in
`.github/actions/skills-update/update-pr.sh`. A second `workflow_dispatch` run means the "already
holds this update" test failed and it force-pushed again.

### 4g. Optional: the token path runs the PR's own CI

Skip this if you don't want to make a token. With `SKILLS_UPDATE_TOKEN` set, the push
comes from that token, so GitHub runs the PR's `pull_request` CI itself and the action
dispatches nothing. Create a fine-grained personal access token on github.com
(Settings, Developer settings) for `skills-kit-scratch` only, with Contents and Pull
requests: Read and write. Then store it, and close the PR and delete its branch so the
next run has to push:

```bash
cd "$SCRATCH"
gh secret set SKILLS_UPDATE_TOKEN --repo "$ME/skills-kit-scratch"
gh pr close skills-update --delete-branch
T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
gh workflow run skills-update.yml
```

`gh secret set` prompts for the token; paste it. Wait a few seconds, then watch the run
created after `T0` and check which CI ran on the new PR:

```bash
RUN=$(gh run list --workflow skills-update.yml --json databaseId,createdAt --jq "[.[] | select(.createdAt > \"$T0\")][0].databaseId // empty")
echo "run=$RUN"
gh run watch "$RUN" --exit-status
gh pr view skills-update --json number,body --jq '.body' | grep -c '## CI on this PR'
gh run list --workflow ci.yml --branch skills-update --json event,createdAt --jq "[.[] | select(.createdAt > \"$T0\") | .event]"
```

If `run=` is empty, the run hasn't started: wait and repeat the `RUN=` line.

**Expect:** the run is green and a new PR is open. `grep -c` prints `0`: no
`## CI on this PR` note on the token path. The event list is `["pull_request"]`, with no
`"workflow_dispatch"`, and the run's log says
`DISPATCH_CI is not 'true'; not dispatching ci.yml.`

**If not:** a `workflow_dispatch` entry, or the `## CI on this PR` note, means the
workflow did not pick the token: look at the "Choose the token" step in
`.github/workflows/skills-update.yml`. An empty event list after a minute means the push
did not trigger CI: check the token can write Contents on this repo.

Leave the PR unmerged. Section 5 reuses this repo.

## 5. A test release (reviewer-run)

This checks the L3 release flow in the same scratch repo: the labels, a draft release
that follows the PR label, the `main`-only guard, the approved-version guard, and a
published release with your summary above the PR list.

### 5a. Add the release pieces and the labels

```bash
cd "$SCRATCH"
git checkout main
cp "$REVIEW_DIR/.github/release-drafter.yml" .github/
cp "$REVIEW_DIR/.github/workflows/release-drafter.yml" "$REVIEW_DIR/.github/workflows/release.yml" .github/workflows/
git add -A
git commit -m "Add the release pieces"
git push origin main
```

Create the three labels from the template's own list (the same data
`make apply_repo_settings_bootstrap` uses):

```bash
jq -r '.[] | [.name, .color, .description] | @tsv' "$REVIEW_DIR/.github/repo-settings/labels.json" | while IFS="$(printf '\t')" read -r name color desc; do gh label create "$name" --color "$color" --description "$desc"; done
gh label list
```

**Expect:** `patch`, `minor` and `major` are created and listed, all lowercase.

**If not:** "already exists" is fine if you re-ran it. A missing `labels.json` means you
are not in the L3 or L4 checkout.

### 5b. Merge one PR labeled `minor`

```bash
git checkout -b scratch-feature
echo "a feature" > feature.txt
git add feature.txt
git commit -m "Add a feature"
git push -u origin scratch-feature
gh pr create --title "Add a feature" --body "Scratch PR for the release check." --label minor
gh pr merge scratch-feature --squash
git checkout main
git fetch origin
git reset --hard origin/main
```

(The scratch repo isn't a stack, so a hard reset of its `main` is fine here.)

Wait for the `release drafter` run for the merge commit. The latest run in the list may
still be the earlier one from 5a, so pick the run whose `headSha` is the merge commit:

```bash
SHA=$(git rev-parse origin/main)
RUN=$(gh run list --workflow release-drafter.yml --json databaseId,headSha --jq ".[] | select(.headSha == \"$SHA\") | .databaseId" | head -1)
echo "run=$RUN"
```

If `run=` is empty, the run hasn't started: wait a few seconds and repeat both lines.
Then watch it and read the draft. (`gh release view` with no tag shows the latest
*published* release, never a draft, so use the releases list.)

```bash
gh run watch "$RUN" --exit-status
gh api "repos/$ME/skills-kit-scratch/releases" --jq '.[0] | [.tag_name, .draft]'
gh api "repos/$ME/skills-kit-scratch/releases" --jq '.[0].body'
```

**Expect:** the first line prints `["v0.1.0",true]` (a draft named `v0.1.0`: a `minor`
PR on a repo with no releases), and the body lists `Add a feature` with its PR number.
An earlier draft from the first push (such as `v0.0.1`) may have existed; the draft
should now have moved to `v0.1.0`.

**If not:** a `v0.0.1` draft after the minor PR means the label was not read: check
`gh pr view scratch-feature --json labels` shows `minor`, and that the label name is exactly lowercase
(the config's `version-resolver` matches the names `major`, `minor`, `patch`). No draft
at all means the `release drafter` run failed: open it with `gh run view --log-failed`.

### 5c. The workflow refuses to publish from a branch

```bash
git push origin main:refs/heads/scratch-branch
echo '{"summary": "This must not publish.", "version": "v0.1.0"}' | gh workflow run release.yml --ref scratch-branch --json
```

Wait a few seconds for the run to appear, then look at it and its jobs:

```bash
gh run list --workflow release.yml --branch scratch-branch --json databaseId,status,conclusion
gh run view "$(gh run list --workflow release.yml --branch scratch-branch --limit 1 --json databaseId --jq '.[0].databaseId')" --json jobs --jq '.jobs[] | [.name, .status, .conclusion]'
gh release list
```

**Expect:** the run on `scratch-branch` is not a `success`: either the run's conclusion
is `skipped`, or the job list shows `publish release` with conclusion `skipped` (the
exact run-level value for a skipped-by-`if:` job is the less certain part, so judge by
the job). The release list still shows only the draft `v0.1.0`, no published release
and no `v0.1.0` tag. (A tag created from a branch would pin consumers to unmerged
code.)

**If not:** a published release, or a `success` run, means the `if: github.ref ==
'refs/heads/main'` guard in `.github/workflows/release.yml` is missing or wrong.

### 5d. A wrong version fails without publishing

`release.yml` publishes only the version you approved. Ask it for one release-drafter
would not publish:

```bash
T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo '{"summary": "This must not publish.", "version": "v9.9.9"}' | gh workflow run release.yml --ref main --json
```

Wait a few seconds, then watch the run created after `T0`:

```bash
RUN=$(gh run list --workflow release.yml --branch main --json databaseId,createdAt --jq "[.[] | select(.createdAt > \"$T0\")][0].databaseId // empty")
echo "run=$RUN"
gh run watch "$RUN" --exit-status; echo "exit=$?"
gh run view "$RUN" --log-failed | grep 'would publish'
gh release list
git ls-remote --tags origin
```

If `run=` is empty, wait and repeat the `RUN=` line.

**Expect:** `exit=1`. The failed step is "Check the version matches the approved one",
and its log says `release-drafter would publish v0.1.0, but the approved version is
v9.9.9. Nothing was published`. The release list still shows only the draft `v0.1.0`,
and `git ls-remote --tags` prints nothing.

**If not:** a published release, or a green run, means the check before the publish step
in `.github/workflows/release.yml` is missing or compares the wrong output. A failure in
the first step instead (the dry run) means release-drafter rejected `dry-run`: read its
log.

### 5e. Publish from main with a summary

```bash
T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo '{"summary": "Adds the first scratch feature, to check the release flow.", "version": "v0.1.0"}' | gh workflow run release.yml --ref main --json
```

Wait a few seconds, then watch the run created after `T0` (5d's failed run is older):

```bash
RUN=$(gh run list --workflow release.yml --branch main --json databaseId,createdAt --jq "[.[] | select(.createdAt > \"$T0\")][0].databaseId // empty")
echo "run=$RUN"
gh run watch "$RUN" --exit-status
gh release view v0.1.0 --json tagName,isDraft,body --jq '[.tagName, .isDraft] | @tsv'
gh release view v0.1.0 --json body --jq .body
git ls-remote --tags origin v0.1.0
```

**Expect:**
- the run is green and its summary page says `Released v0.1.0` with a link;
- the first command prints `v0.1.0` and `false` (published, not a draft);
- the body starts with your summary paragraph, then a blank line, then the merged-PR list
  with `Add a feature`;
- `git ls-remote` prints one line ending in `refs/tags/v0.1.0`.

**If not:**
- A run failure with "Resource not accessible by integration" means the workflow
  `permissions:` or the repo's default workflow permissions (set in 4d) are wrong.
- A published release with the summary and the first PR on one line means the template's
  leading blank lines were lost; look at `template:` in `.github/release-drafter.yml`.
- A second draft instead of a published release means `publish: true` didn't take; look
  at the `release-drafter` step in `release.yml`.

### 5f. Clean up

```bash
cd "$HOME"
gh repo delete "$ME/skills-kit-scratch" --yes
rm -rf "$SCRATCH" /tmp/kit-parity /tmp/kit-bump
```

`gh repo delete` needs the `delete_repo` scope: run `gh auth refresh -s delete_repo`
first, or delete the repo in its Settings page on github.com. If you made a token in
4g, delete it too (github.com, Settings, Developer settings).

## 6. Reviewing the stack

- **One layer at a time, bottom-up: L1, then L2, L3, L4.** Open each PR's "Files
  changed" tab: it shows only that layer's diff, because each PR is based on the one
  below it. Read the layer owning the code, using the table at the top.
- **Comments go on the layer that owns the code,** even when you saw the problem while
  running the top layer. The author fixes it there and restacks.
- **Don't merge from a lower layer.** The stack lands from the top PR (`62`), which
  brings the layers below it. Each layer must be green on its own before that.
- To see one layer's diff locally, compare it with the branch below it:

```bash
cd "$REVIEW_DIR"
git fetch origin 49-consumer-kit 49-l2-update-pr 49-l3-releases 49-l4-release-agent
git diff --stat origin/main...origin/49-consumer-kit
git diff --stat origin/49-consumer-kit origin/49-l2-update-pr
git diff --stat origin/49-l2-update-pr origin/49-l3-releases
git diff --stat origin/49-l3-releases origin/49-l4-release-agent
```

**Expect:** four summaries matching the "Owns" column above. A file in the wrong layer
is worth a comment.

A few reading checks on the top layer:

```bash
cd "$REVIEW_DIR"
test ! -e CHANGELOG.md && echo "CHANGELOG.md is gone"
grep -c '^\*\*Repo config first' .agents/skills/release/SKILL.md
```

**Expect:** `CHANGELOG.md is gone`, then `1`.

Read, in this order:
1. The README sections named at the top: do they make sense cold?
2. `.agents/skills/release/SKILL.md`: does it publish only after an explicit OK for that
   release, and never merge anything?
3. `build-from-issue` Steps 2 and 11: the release-recency report, the suggested bump, and
   the `minor`/`major` label on the top layer of a stack.
4. `.github/workflows/skills-update.yml`: which step sees which secret.

Judgment calls worth a second opinion:
- **New repos start exactly on the pinned release.** `make init` swaps the skill folders
  for the lock's tag, so a new repo never gets unreleased `main` content. The cost: after
  every release, a re-pin PR must update the lock.
- **Composite actions instead of reusable workflows.** The design said reusable
  workflows; the build uses composite actions (`uses: owner/repo/.github/actions/x@tag`),
  so the scripts always run from the same ref as the YAML. Does that read as simpler, or
  as a drift from the design?
- **The template's drift check verifies the lock against tags,** not the working tree
  (once entries have a `ref`), because `main` is allowed to be ahead of the lock.
- **Secrets are scoped to the steps that need them,** never put in job or workflow `env`,
  because the update action runs the unpinned `npx skills@latest`. Inside the action, the
  chosen token reaches only the push-credential and PR steps; the `npx` steps run with
  the job's own `github.token`.
- **Labels are matched case-insensitively and renamed to lowercase** by
  `make apply_repo_settings_bootstrap`: a repo's existing `Minor` is renamed `minor`
  (GitHub rejects a second `minor` beside it), because release-drafter's label match is
  exact.
- **`release.yml` publishes only from `main`** (check 5c), **and only the version you
  approved** (check 5d): a release-drafter dry run resolves it first.
- **The update PR never adds or drops skills.** New upstream skills are listed under
  "Available, not installed"; skills that vanished upstream are kept and flagged under
  "Removed upstream (kept)".
- **`CHANGELOG.md` is deleted** in favor of GitHub Releases, rather than kept as a
  second record to maintain.

Not covered here: `make init` with a real `ref`, swapping the skill folders for a tag
and removing skills the pinned lock doesn't list. It can only be exercised after the
first release and its re-pin PR, so until then only its tests cover it
(`test_initialize.py`).

## 7. Results

| # | Check | Pass? |
|---|-------|-------|
| 1 | Automated tests pass; counts are 11/11/11; an edited skill fails the shipped-lock guard | |
| 2 | The script's hash equals the CLI's `computedHash` | |
| 3 | The drift check passes clean, fails on an edit (`exit=1`) and on a missing folder | |
| 4a | The local bump dry run names `gh-stack` and the new release; the drift check still passes | |
| 4 | The update workflow opens one PR bumping `gh-stack`, with CI dispatched on the branch | |
| 4f | A second run reuses the PR and doesn't push | |
| 4g | (Optional) With `SKILLS_UPDATE_TOKEN`, the PR's own `pull_request` CI runs and nothing is dispatched | |
| 5 | A `minor` PR yields a `v0.1.0` draft; a branch run is skipped; a wrong `version` fails without publishing; a main run publishes with the summary above the PR list | |
| 6 | Each layer's diff reads well in its own PR; the judgment calls hold up | |

Leave findings as review comments on the PR for the layer that owns the code, ideally on
the line in question. For a failure in sections 1 to 5, include the command, the actual
output and the first thing you checked.
