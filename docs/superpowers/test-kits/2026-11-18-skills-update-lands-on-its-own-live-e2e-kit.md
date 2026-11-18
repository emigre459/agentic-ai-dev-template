# Live test: the nightly skills update PR lands on its own (#82)

This proves, on real GitHub, what the unit tests can't:
- the App the setup script creates opens the update PR;
- that PR's CI runs without "Approve and run";
- `review-gate` passes it through the skills-update lane, even under a catch-all CODEOWNERS;
- it merges itself;
- with no secret, the nightly run fails loudly.

It takes about 20 minutes and needs two browser clicks. Everything happens in a throwaway repo you delete at the end.

The kit has two parts:
- **Part 1 (now, before merge)** tests this branch on the path an existing repo takes when it adopts the kit. It's rigged, because a repo made from the template can't see this branch yet.
- **Part 2 (after merge, v0.2.0 and the re-pin)** uses the template as a template, through the README's Start-here interview. See the end of this file.

**Pass criterion:** an "Update agent skills" PR authored by `<slug>[bot]` lands on `main` with no action from you after you start the workflow. Its `check` must run without approval, and its `review-gate` status must read `skills-update: tag-pinned skills only, no review required`.

## How the test is rigged

There are two throwaway repos: a **source** that stands in for the template, and a **scratch** consumer.

- **The source, `<you>/skills-source-e2e`,** carries this branch's tree, which is installable by the skills CLI, and has its own `v0.1.0` release. The real template's published `v0.1.0` can't be used: it has a root `skills-lock.json` that hides its skills from the CLI, the bug this branch fixes. So nothing is released from the real template before merge.
- **The scratch repo** vendors the skills from the source, **pinned to a fake older tag, `v0.0.1`**, so the update job finds the source's `v0.1.0` newer and opens a real update PR.
- **The scratch repo's `main`** also carries this branch's `resolve-pr-concerns/scripts/`, which hold the lane. `review-gate` runs from `main`, so the gate has the lane. The update PR then rewrites the skill folders to the source's `v0.1.0`, and those are tag-pinned skill folder changes too.
- **The scratch workflow** calls this branch's update action by ref.

Use **public** repos: rulesets and auto-merge need GitHub Pro or Team on private personal repos, and nothing in them is secret. On a personal account, the setup script must be run by the repo's owner, which is you.

**Already ran steps 1-3 before this rig changed** (your step 4 failed with `SKILLS_NO_MATCHING_SKILLS`)? Keep the scratch repo, its App and secrets. Run step 0 and step 0b below, then skip to **"3b. Re-point an existing scratch repo"**.

## 0. Variables

Run every step from one terminal, starting inside your checkout of this branch (`T`).

```bash
T=$(git rev-parse --show-toplevel)
ME=$(gh api user --jq .login)
REPO="$ME/skills-update-e2e"
SRC="$ME/skills-source-e2e"
BRANCH=82-let-the-nightly-skills-update-pr-land-on-its-own
git -C "$T" fetch -q origin && git -C "$T" status -sb | head -1
```

## 0b. Create the throwaway source with a `v0.1.0` release

```bash
gh repo create "$SRC" --public
git -C "$T" push -q "git@github.com:$SRC.git" "origin/$BRANCH:refs/heads/main"
gh release create v0.1.0 -R "$SRC" --target main --title v0.1.0 --notes "Live-test source for #82"
gh api "repos/$SRC/contents/skills-lock.json" >/dev/null 2>&1 && echo "UNEXPECTED: root lock present" || echo "no root lock (good)"
```

The source's own CI also runs on that push. Ignore it.


## 1. Create and seed the scratch repo (no secret yet)

```bash
cd ~ && gh repo create "$REPO" --public --clone && cd skills-update-e2e
git symbolic-ref HEAD refs/heads/main   # the clone of an empty repo starts on your init.defaultBranch (often master)
git -C "$T" archive v0.1.0 .agents/skills | tar -x
cp "$T/.github/template-skills-lock.json" skills-lock.json
SRC="$SRC" python3 -c "import json,os;p='skills-lock.json';d=json.load(open(p));[e.update(ref='v0.0.1',source=os.environ['SRC']) for e in d['skills'].values()];open(p,'w').write(json.dumps(d,indent=2)+'\n')"
rm -rf .agents/skills/resolve-pr-concerns/scripts
cp -R "$T/.agents/skills/resolve-pr-concerns/scripts" .agents/skills/resolve-pr-concerns/scripts
mkdir -p .github/workflows
sed "s#uses: ./.github/actions/skills-update#uses: emigre459/agentic-ai-dev-template/.github/actions/skills-update@$BRANCH#" \
  "$T/.github/workflows/skills-update.yml" > .github/workflows/skills-update.yml
cp "$T/.github/workflows/review-gate.yml" .github/workflows/
cp "$T/.github/pr-reviewer.json" .github/
printf '* @%s\n' "$ME" > .github/CODEOWNERS
cat > .github/workflows/ci.yml <<'EOF'
name: ci
on: [pull_request]
jobs:
  check:
    name: check
    runs-on: ubuntu-latest
    steps:
      - run: echo "CI ran without approval for ${{ github.event.pull_request.user.login }}"
EOF
grep -c 'skills-update@' .github/workflows/skills-update.yml   # expect 1
git add -A && git commit -qm "Seed the skills-update live test" && git push -q -u origin main
```

**Check 1 (fail loudly):** pushing `skills-lock.json` to `main` starts "skills update" with no secret set.

```bash
sleep 20; gh run list -R "$REPO" --workflow skills-update.yml --limit 1
gh run view -R "$REPO" --log-failed $(gh run list -R "$REPO" --workflow skills-update.yml --limit 1 --json databaseId --jq '.[0].databaseId') | grep '::error::\|No SKILLS_UPDATE'
```

Expected: the run fails, and the log says `No SKILLS_UPDATE_APP_ID/SKILLS_UPDATE_APP_PRIVATE_KEY (GitHub App) or SKILLS_UPDATE_TOKEN (PAT) secret is set. Set one up with: python3 .agents/skills/upgrade-deps/scripts/setup_skills_update_app.py --repo <you>/skills-update-e2e ...`. The repo name must be your real one, not a literal `$GITHUB_REPOSITORY`.

## 2. Set up the App: two clicks

Run it from inside the scratch clone (the lane file goes at its git root), in your own terminal:

```bash
python3 "$T/.agents/skills/upgrade-deps/scripts/setup_skills_update_app.py" --repo "$REPO"
```

1. A browser tab opens on GitHub's "Create GitHub App" page, pre-filled as `skills-update-e2e-skills-update` with contents and pull requests: write and no webhook. **Click Create.**
2. GitHub redirects to `127.0.0.1`. The script stores the two secrets and opens the install page. **Click Install** and pick only `skills-update-e2e`, then press Enter in the terminal.
3. The script prints a summary with the App ID and slug, and says it wrote `.github/skills-update-lane.json`.

If the redirect never arrives (for example, a remote shell), rerun with `--no-browser` and paste the redirect URL within the hour. If an App was created but the script died before collecting its key, delete that App at https://github.com/settings/apps before you retry.

**Check 2:**

```bash
gh secret list -R "$REPO"            # SKILLS_UPDATE_APP_ID and SKILLS_UPDATE_APP_PRIVATE_KEY
cat .github/skills-update-lane.json  # {"branch": "skills-update", "authors": ["<slug>[bot]"]}
git status --short                   # the key appears nowhere on disk: only the lane file is new
```

Commit the lane file to `main` before turning on the ruleset (which blocks direct pushes):

```bash
git add .github/skills-update-lane.json && git commit -qm "Opt in to the skills-update lane" && git push -q
```

## 3. Repo settings: auto-merge and a ruleset requiring the checks

```bash
gh repo edit "$REPO" --enable-auto-merge --enable-squash-merge --enable-merge-commit=false --enable-rebase-merge=false
cat > /tmp/e2e-ruleset.json <<'EOF'
{"name": "main", "target": "branch", "enforcement": "active",
 "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
 "rules": [
  {"type": "pull_request", "parameters": {"required_approving_review_count": 0,
    "dismiss_stale_reviews_on_push": true, "require_code_owner_review": false,
    "require_last_push_approval": false, "required_review_thread_resolution": true}},
  {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": false,
    "required_status_checks": [{"context": "review-gate"}, {"context": "check"}]}}]}
EOF
gh api -X POST "repos/$REPO/rulesets" --input /tmp/e2e-ruleset.json --jq '.id, .enforcement'
```

## 3b. Re-point an existing scratch repo (only if you ran steps 1-3 before the rig changed)

Your scratch repo's lock still points at the real template, whose `v0.1.0` can't be installed. Point it at the source through a PR, because the ruleset blocks direct pushes:

```bash
cd ~/skills-update-e2e && git checkout -q main && git pull -q
git checkout -q -b point-at-test-source
SRC="$SRC" python3 -c "import json,os;p='skills-lock.json';d=json.load(open(p));[e.update(source=os.environ['SRC']) for e in d['skills'].values()];open(p,'w').write(json.dumps(d,indent=2)+'\n')"
git commit -qam "Point the skills at the live-test source" && git push -q -u origin point-at-test-source
gh pr create -R "$REPO" --fill && gh pr checks --watch && gh pr merge --squash --delete-branch
git checkout -q main && git pull -q
```

That PR is yours, so the lane doesn't apply. `review-gate` passes it through the ownership half instead: you own every path, and no reviewer is configured. Merging it changes `skills-lock.json` on `main`, which starts "skills update" by itself. Step 4 still starts its own run and watches that one.

## 4. Run the update and watch it land

```bash
gh workflow run skills-update.yml -R "$REPO" && sleep 10
gh run watch -R "$REPO" $(gh run list -R "$REPO" --workflow skills-update.yml --limit 1 --json databaseId --jq '.[0].databaseId') --exit-status
PR=$(gh pr list -R "$REPO" --head skills-update --state all --json number --jq '.[0].number'); echo "PR #$PR"
gh pr view -R "$REPO" "$PR" --json author,autoMergeRequest,files --jq '{author: .author.login, auto: (.autoMergeRequest != null), files: [.files[].path] | length}'
```

Expected:
- the run is green;
- `author` is `<slug>` (the App bot; `gh` drops the `[bot]` suffix in this field), never `github-actions` or you;
- `auto` is `true`;
- the files are `skills-lock.json` plus files under `.agents/skills/<name>/` for skills whose content differs between the template's real `v0.1.0` and this branch. Every one must be under `.agents/skills/` or be the lock (check with `gh pr diff -R "$REPO" "$PR" --name-only | grep -v -e '^skills-lock.json$' -e '^\.agents/skills/'`, which must print nothing).

**Check 3 (CI ran without approval, and the gate passed through the lane):**

```bash
gh pr checks -R "$REPO" "$PR" --watch
gh api "repos/$REPO/commits/$(gh pr view -R "$REPO" "$PR" --json headRefOid --jq .headRefOid)/statuses" \
  --jq '[.[] | select(.context=="review-gate")][0] | .state + ": " + .description'
```

Expected:
- `check` passes and was never "Waiting for approval";
- `review-gate` reads `success: skills-update: tag-pinned skills only, no review required`, although CODEOWNERS says `* @<you>` and you never approved.

**Check 4 (it merged itself):**

```bash
sleep 30; gh pr view -R "$REPO" "$PR" --json state,mergedBy,mergedAt --jq '.state + " by " + (.mergedBy.login // "-") + " at " + (.mergedAt // "-")'
```

Expected: `MERGED`, merged by the App, with no click from you.

**Pass** = Checks 1-4 all as expected. Report back which check failed and its output, if any.

## What can go wrong, and what to check first

| Symptom | Likely cause | First check |
|---|---|---|
| Step 2: the browser page shows 404 | You passed `--org` on a personal repo | Rerun without `--org` |
| Step 4: "Resource not accessible by integration" in the run | The App isn't installed on the repo | https://github.com/settings/installations, then add the repo |
| The run fails at "could not enable auto-merge" | Auto-merge is off in settings | `gh repo edit "$REPO" --enable-auto-merge`, then `gh pr merge <N> --auto --squash` |
| `review-gate` is missing on the PR | `review-gate.yml` didn't run, or the lane file/lock didn't load | The Actions tab, then the "review gate" run log (`malformed` lines on stderr) |
| `review-gate` says `@<you> must approve` | The PR author isn't the login in the lane file | `gh pr view "$PR" --json author` vs `.github/skills-update-lane.json` |
| The push of the rebuilt branch is refused for workflow files | The App lacks `workflows` permission and the branch touches `.github/workflows/` | This rig never changes workflows after seeding; report it if it happens |

## Not covered here (covered by unit tests, or out of reach)

- **A person's PR, a fork, or an extra non-skill path being refused by the lane:** unit tests in `tests/template/test_review_gate.py` (`test_lane_refuses`, `test_a_persons_pr_on_skill_paths_still_needs_the_owner`, `test_an_update_pr_with_one_non_skill_path_needs_both_halves`). As the repo owner and only user, you can't author a non-owner PR here.
- **The merge queue:** personal repos can't have one. In an org repo, `gh pr merge --auto` enqueues instead.
- **The org paths of the setup script** (`--org`, `--org-secrets`, `--existing-app`): these need an org you admin. Run them there if you want to; the flow is the same two clicks.

## Cleanup

```bash
gh repo delete "$REPO" --yes          # needs the delete_repo scope: gh auth refresh -s delete_repo
gh repo delete "$SRC" --yes
open "https://github.com/settings/apps"   # delete skills-update-e2e-skills-update
```

## After merge (separate, with your OK)

- The template's own `main` ruleset still has the Copilot rule until it's re-applied: `make finalize_repo_settings TARGET_REPO=emigre459/agentic-ai-dev-template`.
- Cut v0.2.0 (with #84) and merge the re-pin PR, so repos made from the template get the script and the lane. Then run Part 2.

---

# Part 2: use the template as a template (after v0.2.0 and the re-pin)

This tests the path most people take: a new repo made from the template, set up by a coding agent through the README's Start-here prompt.

It proves that:
- `make init` installs skills that include the setup script and the lane;
- the agent offers the App at interview question 7 and runs the setup with your two clicks;
- the lane file lands in the initialization PR;
- the nightly run is green from day one.

It takes about 30 minutes, mostly the interview and the initialization PR's CI.

**Pass criterion:**
- the initialization PR contains `.github/skills-update-lane.json` naming `<slug>[bot]`;
- the repo has both App secrets;
- a by-hand "skills update" run right after setup is **green** and opens no PR;
- once a newer release exists, its update PR lands on its own, as in Part 1, Checks 3-4.

## P0. Preconditions

Run these from any checkout of the template. Both must hold, or the new repo gets the old skills:

```bash
gh release view -R emigre459/agentic-ai-dev-template --json tagName,isDraft --jq '.tagName + " draft=" + (.isDraft|tostring)'   # expect v0.2.0 draft=false (or newer)
gh api repos/emigre459/agentic-ai-dev-template/contents/skills-lock.json --jq .content | base64 --decode \
  | python3 -c "import json,sys;print(sorted({e.get('ref') for e in json.load(sys.stdin)['skills'].values()}))"   # expect ['v0.2.0'] (the re-pin merged)
```

## P1. Create the repo from the template

```bash
ME=$(gh api user --jq .login)
REPO2="$ME/skills-update-template-e2e"
cd ~ && gh repo create "$REPO2" --template emigre459/agentic-ai-dev-template --public --clone && cd skills-update-template-e2e
```

Use a public repo again: on a free personal account, rulesets and auto-merge need it.

## P2. Run the Start-here interview with your coding agent

1. Open your agent in `~/skills-update-template-e2e`.
2. Paste the prompt from the README's "Start here: initialize this template" section.
3. Answer the interview. Any stack works; pick the one you know best. At **question 7**, answer **now**.
4. When the agent runs `setup_skills_update_app.py`, it should run it in the background or with a long timeout, or ask you to run it in your terminal. **Click Create**, then **click Install** on `skills-update-template-e2e` only.

**Check P2:** before the agent commits, in a second terminal:

```bash
cd ~/skills-update-template-e2e
test -f .agents/skills/upgrade-deps/scripts/setup_skills_update_app.py && echo "script installed"
grep -c 'def skills_update_status' .agents/skills/resolve-pr-concerns/scripts/review_gate.py      # expect 1
grep 'skills-update@' .github/workflows/skills-update.yml                                         # expect ...@v0.2.0 (or newer)
cat .github/skills-update-lane.json                                                               # {"branch": "skills-update", "authors": ["<slug>[bot]"]}
gh secret list -R "$REPO2"                                                                        # both SKILLS_UPDATE_APP_* secrets
```

If the script file is missing, P0 didn't hold. The agent should then have offered the PAT path instead (that's the fallback for the window before the re-pin), and this test can't continue.

## P3. Merge the initialization PR, then finalize

Let the agent open the initialization PR. Then:

```bash
PR0=$(gh pr list -R "$REPO2" --state open --json number --jq '.[0].number')
gh pr view -R "$REPO2" "$PR0" --json files --jq '[.files[].path] | map(select(. == ".github/skills-update-lane.json"))'   # expect the lane file
```

Squash-merge it once CI is green, as the interview says. Then let the agent run `make finalize_repo_settings TARGET_REPO="$REPO2"`, which turns on the required checks. It can't add a merge queue on a personal repo; that's expected.

## P4. A by-hand run is green, with nothing to update

```bash
gh workflow run skills-update.yml -R "$REPO2" && sleep 10
gh run watch -R "$REPO2" $(gh run list -R "$REPO2" --workflow skills-update.yml --limit 1 --json databaseId --jq '.[0].databaseId') --exit-status
gh run view -R "$REPO2" --log $(gh run list -R "$REPO2" --workflow skills-update.yml --limit 1 --json databaseId --jq '.[0].databaseId') | grep -E 'No skill drift|already up to date|Nothing to do' | head -3
```

Expected: the run is green, it reports no drift, and it opens no PR. The repo is pinned to the newest release, so there's nothing to update yet. This also proves the App token minted and the install worked: a missing or uninstalled App fails before the drift check.

## P5. When the next release ships: it lands on its own

Leave the repo in place until the template publishes a release newer than its pin (v0.2.1, say). The next nightly run, or a by-hand one as in P4, should then open an "Update agent skills" PR authored by `<slug>[bot]`. Check it with Part 1's Check 3 and Check 4, replacing `REPO` with `REPO2`. Note that this repo's real CI checks replace Part 1's `check` job.

## P6. Cleanup

```bash
gh repo delete "$REPO2" --yes
open "https://github.com/settings/apps"   # delete skills-update-template-e2e-skills-update
```
