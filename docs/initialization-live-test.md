# Live test: template initialization (PR #21)

A hands-on check of the hardened initialization flow against a real, throwaway
GitHub repository. Budget about 30 minutes, most of it waiting on CI.

## Why a live test is worth it here

The unit tests in `tests/template/` swap `gh` for a fake. That shows the planning
logic is correct, but not that GitHub behaves the way the fakes assume. The
riskiest claims in this PR are about real GitHub behavior:

- a bootstrap ruleset without `required_status_checks` is accepted, and a
  pre-existing check rule gets stripped by the `PUT`;
- the setup PR can actually merge under the bootstrap ruleset;
- the final phase restores checks whose names match the jobs CI really runs;
- the preflight reads the real `gh api` fields (`permissions.admin`,
  `has_issues`, `full_name`).

Only a live run proves those. Everything else (argument parsing, package-name
validation, error wording) is covered by unit tests, and the negative checks
below just confirm it end to end.

## 0. Setup

You need `git`, `python3`, `uv`, and `gh` logged in as an account that can
create and delete repositories. Cleanup needs the `delete_repo` scope:

```bash
gh auth refresh -s delete_repo
```

Create a **public** sandbox. On a free personal account, rulesets are not
available on private repositories, so a private sandbox gives a false failure.

```bash
export OWNER=<your-github-login>
export TARGET_REPO="$OWNER/init-live-test"
export TEMPLATE=emigre459/agentic-ai-dev-template

gh repo create "$TARGET_REPO" --public
export WORK="$(mktemp -d)" && cd "$WORK"
git clone --branch iris/test-access "https://github.com/$TEMPLATE.git" sandbox
cd sandbox
git remote set-url origin "https://github.com/$TARGET_REPO.git"
git checkout -b main && git push -u origin main
```

The sandbox's `main` is now the PR branch, which is the state a user of the
merged template would start from.

Simulate the "copied ruleset blocks the first PR" failure by applying the
template's own final ruleset first. It requires the template's `python-lint`
style checks, which a seeded repo will never produce. Type `y` at the prompt:

```bash
python3 scripts/apply_repo_settings.py --repo "$TARGET_REPO" --phase final
```

## 1. Guardrails (nothing should change)

Run each command and compare the result. After each one, `git status --short`
should print nothing (except in 1b, where you created the file yourself).

| # | Command | Expected |
|---|---------|----------|
| 1a | `make init_preflight STACK=python TARGET_REPO=$TEMPLATE` | Fails: `origin points to <you>/init-live-test, but TARGET_REPO is emigre459/...` |
| 1b | `touch scratch.txt && make init_preflight STACK=python TARGET_REPO=$TARGET_REPO; rm scratch.txt` | Fails: refuses because of existing local changes and lists `?? scratch.txt` |
| 1c | `make init STACK=python PROJECT_NAME=class DESCRIPTION=x` | Fails with `'class' cannot produce a valid Python package name`; tree unchanged, `stacks/` still present |
| 1d | `make apply_repo_settings_bootstrap` | Fails: `TARGET_REPO=owner/repository is required` |
| 1e | `python3 scripts/apply_repo_settings.py --repo $OWNER/does-not-exist --phase final` | Exit 2, a plain `GitHub command ... failed. GitHub said: ... HTTP 404` message with the admin-permission hint, and no Python traceback |

## 2. Happy path (Python stack)

**Preflight.** It should pass and print your account, `TARGET_REPO`, `python`,
and `Working tree: clean`:

```bash
make init_preflight STACK=python TARGET_REPO="$TARGET_REPO"
```

**Issue and branch, targeted explicitly:**

```bash
N=$(gh issue create --repo "$TARGET_REPO" --title "Project setup" --body "live test" | grep -o '[0-9]*$')
gh issue develop "$N" --repo "$TARGET_REPO" --name "chore/$N-project-setup" --base main --checkout
```

**Initialize:**

```bash
make init STACK=python PROJECT_NAME=live-test-app DESCRIPTION="Live test"
ls src/                                          # expect: live_test_app
grep -rn example_app src tests pyproject.toml    # expect: no output
grep module-name pyproject.toml                  # expect: module-name = "live_test_app"
grep '"context"' .github/repo-settings/ruleset.json   # expect: lint, tests, security
```

**Bootstrap settings.** Type `y` at the prompt. The plan should include
`ruleset: PUT`, because the ruleset seeded in step 0 has check rules to strip.

```bash
make apply_repo_settings_bootstrap TARGET_REPO="$TARGET_REPO"
RS=$(gh api "repos/$TARGET_REPO/rulesets" --jq '.[] | select(.name=="main") | .id')
gh api "repos/$TARGET_REPO/rulesets/$RS" --jq '[.rules[].type]'
# expect: pull_request present, required_status_checks ABSENT
make apply_repo_settings_bootstrap TARGET_REPO="$TARGET_REPO"
# expect: "settings already aligned — no changes."
```

**Commit, check, open the PR:**

```bash
git add -A && git commit -m "chore: initialize from agentic-ai-dev-template"
make deps && make pr_check                       # expect: green
git push -u origin HEAD
gh pr create --repo "$TARGET_REPO" --base main --title "Project setup" --body "Closes #$N"
```

**The key check: the setup PR can merge.** Wait for CI, then:

```bash
gh pr checks --repo "$TARGET_REPO" --watch
gh pr merge --repo "$TARGET_REPO" --squash --delete-branch
# expect: merges. Before this PR, the seeded ruleset would have blocked it
# waiting on python-lint/python-tests/... checks that never report.
```

**Finalize.** Type `y` at the prompt:

```bash
git checkout main && git pull
make finalize_repo_settings TARGET_REPO="$TARGET_REPO"
gh api "repos/$TARGET_REPO/rulesets/$RS" \
  --jq '.rules[] | select(.type=="required_status_checks") | [.parameters.required_status_checks[].context]'
# expect: ["lint","tests","security"]
make finalize_repo_settings TARGET_REPO="$TARGET_REPO"
# expect: "settings already aligned — no changes."
```

**Optional proof that the final checks are real:** push a one-line change on a
new branch, open a PR, and confirm `gh pr checks` lists `lint`, `tests`, and
`security` as required and passing. If a name never reports, the PR hangs,
which means a mismatch between `_CI_JOBS` and the workflow job IDs.

## 3. Optional: React stack

Delete the sandbox (section 4), recreate it (section 0), then run section 2 with
`STACK=react` and any `PROJECT_NAME`. Skip the Python package checks. Instead,
confirm that `pyproject.toml`, `uv.lock`, and `.python-version` are gone from the
root and that `make deps && make pr_check` runs with `bun` only.

## 4. Cleanup

```bash
gh repo delete "$TARGET_REPO" --yes
rm -rf "$WORK"
```

## Not covered by this runbook

- **Missing admin permission.** Testing it needs a second account without admin
  on the target. Unit tests cover it:
  `test_run_preflight_rejects_unusable_repository[...administrator permission]`.
- **Fork/upstream layouts.** Check 1a covers the core risk (an `origin` that
  doesn't match the target). A true fork setup adds nothing the preflight
  checks differently.

If a step outside this PR's scope fails, such as the `copilot_code_review` rule
being rejected on an account without Copilot, note it separately. That rule
already exists on `main`.

## Results to report

| Step | Pass? | Notes |
|------|-------|-------|
| 1a–1e guardrails | | |
| Preflight passes | | |
| Package renamed | | |
| Bootstrap strips checks + idempotent | | |
| Setup PR merges | | |
| Finalize restores lint/tests/security + idempotent | | |
| (optional) React run | | |
