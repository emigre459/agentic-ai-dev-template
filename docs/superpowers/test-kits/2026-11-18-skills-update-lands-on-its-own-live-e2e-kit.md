# Live test: the nightly skills update PR lands on its own (#82)

This proves, on real GitHub, what the unit tests can't:
- the App the setup script creates opens the update PR;
- that PR's CI runs without "Approve and run";
- `review-gate` passes it through the skills-update lane, even under a catch-all CODEOWNERS;
- it merges itself;
- with no secret, the nightly run fails loudly.

It takes about 20 minutes and needs two browser clicks. Everything happens in a throwaway repo you delete at the end.

**Pass criterion:** an "Update agent skills" PR authored by `<slug>[bot]` lands on `main` with no action from you after you start the workflow. Its `check` must run without approval, and its `review-gate` status must read `skills-update: tag-pinned skills only, no review required`.

## How the test is rigged

The scratch repo vendors the template's skills **pinned to a fake older tag, `v0.0.1`**, so the update job finds `v0.1.0` newer and opens a real update PR.

The repo's `main` also carries this branch's `resolve-pr-concerns/scripts/`, which hold the lane. `review-gate` runs from `main`, so the gate has the lane, and the update PR reverting those files is itself a tag-pinned skill folder change.

The workflow calls this branch's action by ref.

Use a **public** repo: rulesets and auto-merge need GitHub Pro or Team on private personal repos, and nothing in it is secret. On a personal account, the setup script must be run by the repo's owner, which is you.

## 0. Variables

Run every step from one terminal, starting inside your checkout of this branch (`T`).

```bash
T=$(git rev-parse --show-toplevel)
ME=$(gh api user --jq .login)
REPO="$ME/skills-update-e2e"
BRANCH=82-let-the-nightly-skills-update-pr-land-on-its-own
git -C "$T" fetch -q origin --tags && git -C "$T" status -sb | head -1
```

## 1. Create and seed the scratch repo (no secret yet)

```bash
cd ~ && gh repo create "$REPO" --public --clone && cd skills-update-e2e
git -C "$T" archive v0.1.0 .agents/skills | tar -x
cp "$T/skills-lock.json" .
python3 -c "import json;p='skills-lock.json';d=json.load(open(p));[e.__setitem__('ref','v0.0.1') for e in d['skills'].values()];open(p,'w').write(json.dumps(d,indent=2)+'\n')"
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
- the files are `skills-lock.json` plus the files under `.agents/skills/resolve-pr-concerns/scripts/`.

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
open "https://github.com/settings/apps"   # delete skills-update-e2e-skills-update
```

## After merge (separate, with your OK)

- The template's own `main` ruleset still has the Copilot rule until it's re-applied: `make finalize_repo_settings TARGET_REPO=emigre459/agentic-ai-dev-template`.
- Cut v0.2.0 (with #84) and merge the re-pin PR, so repos made from the template get the script and the lane.
