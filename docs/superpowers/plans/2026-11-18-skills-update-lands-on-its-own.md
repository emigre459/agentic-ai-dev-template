# Skills Update PR Lands On Its Own: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The nightly "Update agent skills" PR lands with no person involved: it's opened with an App/PAT token (failing loudly without one), it passes `review-gate` through a skills-only lane, and it auto-merges. A stdlib script lets a coding agent set up the App with two clicks.

**Architecture:** There are four separable slices:
- a pure lane decision in `review_gate.py`, fed by two default-branch files (`.github/skills-update-lane.json`, `skills-lock.json`);
- token selection in the workflow and auto-merge in `update-pr.sh`;
- one ruleset rule removed;
- a manifest-flow setup script bundled in `upgrade-deps`.

The README ties them together.

**Tech Stack:** stdlib Python 3 (gate + setup script), bash (`update-pr.sh`), GitHub Actions YAML, pytest (`uv run pytest tests/template`).

**Spec:** `docs/superpowers/specs/2026-11-18-skills-update-lands-on-its-own-design.md`

## Global Constraints

- Lane config file: `.github/skills-update-lane.json`, shape `{"branch": str, "authors": [str, ...]}`. The template ships none, and ships the example at `.agents/skills/resolve-pr-concerns/references/skills-update-lane.example.json`.
- Tag-pinned = the lock entry's `ref` matches `^v\d+\.\d+\.\d+$`. Qualifying local folders are `.agents/skills/<name>/` and `.claude/skills/<name>/` (`<name>` = the lock key, never `skillPath`).
- Lane pass status, verbatim: `("success", "skills-update: tag-pinned skills only, no review required")`.
- Gate order with a reviewer: `collect_state` → lane → ownership → docs-only → reviewer. Without a reviewer: lane → ownership. `main()`'s "post nothing" rule is unchanged.
- Auto-merge call, verbatim: `"$GH_CMD" pr merge "$pr_number" --auto --squash --match-head-commit "$head"`. It runs only when this run pushed or created the PR, with `$head` = the SHA on the remote branch.
- The token is the App, else `SKILLS_UPDATE_TOKEN`, else `::error::` and exit 1. No `github.token` fallback, no `dispatch-ci` / `ci-workflow` inputs, no `actions: write` anywhere in `skills-update.yml`.
- The App manifest's `default_permissions` are exactly `{"contents": "write", "pull_requests": "write"}`, with `public: false` and the webhook inactive.
- The App's private key (`pem`) never appears in argv, on disk, or in stdout/stderr. It reaches `gh secret set` on stdin only.
- New scripts are stdlib-only and run with `python3`. Default-lane tests never connect off loopback.
- Python style per `.agents/rules/python/python-best-practices.md` (type hints, numpy docstrings), with narrow excepts (`no-blind-except`).

## Review Focus

- **A lock entry with an upstream `skillPath` unlike its local folder** (`skills/pdf/SKILL.md`): it still qualifies by name, and `src/foo/SKILL.md` never lets `src/foo/` through. Owned by Task 1.
- **A lock key that isn't a plain folder name** (`..`, `a/b`, `""`) is skipped rather than widening the prefix match. Owned by Task 1.
- **Lane file with the author listed twice, or extra keys** (`description`): it loads fine. The setup script adds an author without duplicating and keeps other keys. Owned by Tasks 1 and 4.
- **An update run that creates the PR on an already-current remote branch:** `--match-head-commit` gets the remote SHA, not the local commit's. Owned by Task 2.
- **The redirect arriving with a missing or wrong `state`, or a pasted full URL instead of a bare code:** the first is refused, the second accepted. Owned by Task 4.

---

### Task 1: The skills-update lane in `review_gate.py`

**Files:**
- Modify: `.agents/skills/resolve-pr-concerns/scripts/review_gate.py` (module docstring, new loaders + pure function + I/O wrapper, `_want`, `post_for_pr` docstring)
- Create: `.agents/skills/resolve-pr-concerns/references/skills-update-lane.example.json`
- Modify: `.agents/skills/resolve-pr-concerns/SKILL.md` (one paragraph after the "Docs-only PRs, when the repo has opted in." paragraph, ~line 533)
- Test: `tests/template/test_review_gate.py`

**Interfaces:**
- Produces:
  - `class SkillsUpdateLane(NamedTuple): branch: str; authors: tuple[str, ...]`
  - `load_skills_update_lane(path: Path) -> SkillsUpdateLane | None`: None when missing (silent) or malformed (stderr line).
  - `tag_pinned_skill_folders(lock_path: Path) -> tuple[str, ...]`: e.g. `(".agents/skills/x", ".claude/skills/x", …)`. Returns `()` on missing/malformed lock. Skips keys that are empty, `.`, `..`, or contain `/` or `\`.
  - `skills_update_status(pull: Mapping[str, Any], files: Sequence[Mapping[str, Any]], lane: SkillsUpdateLane | None, folders: Sequence[str], repo: str) -> tuple[str, str] | None`
  - Constants: `SKILLS_UPDATE_LANE_FILE = REPO_ROOT / ".github" / "skills-update-lane.json"`, `SKILLS_LOCK_FILE = REPO_ROOT / "skills-lock.json"`, `SKILLS_UPDATE_STATUS` (the verbatim pass tuple).
  - `_skills_update(repo: str, pr: int, head_sha: str | None) -> tuple[str, tuple[str, str] | None]`: returns (the pull's head SHA, pass or None). It reads nothing from GitHub unless the lane file loaded and folders are non-empty (then it returns `(head_sha or "", None)`). With `head_sha` given and the pull's head different, it returns None.
  - Cached loaders via `functools.cache`, as with `_cached_docs_allowlist`.

- [ ] **Step 1: Write the failing tests** in `tests/template/test_review_gate.py` (new section). Use a `_pull(**over)` helper: `{"user": {"login": "acme-skills[bot]"}, "head": {"ref": "skills-update", "sha": SHA, "repo": {"full_name": REPO}}, "changed_files": n}`. Add `LANE = review_gate.SkillsUpdateLane("skills-update", ("acme-skills[bot]",))` and `FOLDERS = (".agents/skills/alpha", ".claude/skills/alpha")`.
  - `test_lane_passes_lock_and_tag_pinned_folders`: files `skills-lock.json` and `.agents/skills/alpha/SKILL.md` → `== review_gate.SKILLS_UPDATE_STATUS`.
  - `test_lane_passes_claude_skills_root`: `.claude/skills/alpha/x.md` → pass.
  - Parametrized `test_lane_refuses` → `None`, one case each:
    - wrong author;
    - wrong branch;
    - a fork (`head.repo.full_name="evil/repo"`);
    - `head.repo` None;
    - branch-tracking folder `.agents/skills/beta/SKILL.md`;
    - non-skill path `README.md`;
    - a rename with `previous_filename="src/x.py"`;
    - `changed_files=3` with 2 listed;
    - an empty file list;
    - the lookalike `.agents/skills/alpha-x/SKILL.md`;
    - `lane=None`;
    - `folders=()`.
  - `test_tag_pinned_folders_use_the_lock_key_not_skill_path(tmp_path)`: lock entries
    - `alpha` (`ref "v1.2.3"`, `skillPath "skills/alpha/SKILL.md"`)
    - `beta` (`ref "main"`)
    - `gamma` (no ref)
    - `src` (`ref "v1.0.0"`, `skillPath "src/foo/SKILL.md"`)
    - `..` and `a/b` (both `ref "v1.0.0"`)

    Expect the folders to be exactly alpha's and src's two roots each, and `src/foo` not in them.
  - `test_tag_pinned_folders_missing_or_malformed_lock_is_empty(tmp_path)`: a missing file, `"not json"`, and `{"skills": []}` each give `()`.
  - `test_lane_config_loading(tmp_path)`:
    - missing → None;
    - `{"branch": "skills-update", "authors": ["a[bot]"], "description": "x"}` → `SkillsUpdateLane("skills-update", ("a[bot]",))`;
    - malformed cases, each → None: `{"branch": ""…}`, `{"authors": []}`, `{"authors": [""]}`, a missing branch, `"[]"`, `"not json"`.
  - `test_shipped_lane_example_is_valid_and_template_ships_no_lane_file`: the example loads non-None, and `REPO_ROOT/.github/skills-update-lane.json` doesn't exist.
  - Wiring through `post_for_pr`, with `_codeowners(monkeypatch, tmp_path, "* @owner\n")` (existing helper):
    - `REPO_ROOT` monkeypatched to `tmp_path` holding `.github/skills-update-lane.json` and `skills-lock.json`;
    - `_cached_*` caches cleared;
    - `reviewer_state._gh_json` faked for `pulls/<n>` and `pulls/<n>/files`;
    - `_post` and `_current_gate_status` captured.

    Cases:
    - `test_update_pr_passes_both_halves_under_a_catch_all_codeowners`: posts `SKILLS_UPDATE_STATUS`, and the reviewer state says unreviewed.
    - `test_a_persons_pr_on_skill_paths_still_needs_the_owner`: author `alice` → the posted state is `failure` from the ownership half.
    - `test_an_update_pr_with_one_non_skill_path_needs_both_halves`: → `failure`.
    - `test_a_moved_head_fails_closed`: the pull's head SHA differs from `collect_state`'s → not the lane status.
    - `test_the_lane_passes_with_no_reviewer_configured`: `post_for_pr(REPO, 1, None)` → lane status.

- [ ] **Step 2: Run them, expect failures:** `uv run pytest tests/template/test_review_gate.py -k "lane or tag_pinned or update_pr or persons_pr or moved_head" -q` → FAIL (`AttributeError: … SkillsUpdateLane`).

- [ ] **Step 3: Implement** the Interfaces above in `review_gate.py`, then rewire `_want`:
  - with an adapter: after `collect_state`, call `_skills_update(repo, pr, head_sha)`; a pass returns before the ownership half;
  - with no adapter: call `_skills_update(repo, pr, None)` first. A pass returns `(its head, pass)`; else fall through to `_ownership(repo, pr, None)`.

  Path rule: `p == "skills-lock.json" or any(p.startswith(f + "/") for f in folders)`, applied to every `filename` and every non-empty `previous_filename`. Update the module docstring and the `_want`/`post_for_pr` docstrings with the new order. Write the example JSON with a `description` key explaining opt-in, the tag-pinned rule, and the copy command `cp .agents/skills/resolve-pr-concerns/references/skills-update-lane.example.json .github/skills-update-lane.json`, plus `"branch": "skills-update"` and `"authors": ["your-app-slug[bot]"]`. Add the SKILL.md paragraph: a PR in the skills-update lane needs no review request (don't post trigger/final-review comments), and `review-gate` passes it as `skills-update: tag-pinned skills only, no review required`. Without the file, nothing changes.

- [ ] **Step 4: Run the tests:** `uv run pytest tests/template/test_review_gate.py tests/template/test_pr_reviewer_seam.py -q` → all PASS. Then `make machinery_lint` → clean.

- [ ] **Step 5: Commit and push**

```bash
git add .agents/skills/resolve-pr-concerns tests/template/test_review_gate.py
git commit -m "Add the skills-update lane to review-gate (#82)"
git push
```

### Task 2: Token or fail, no dispatch, auto-merge

**Files:**
- Modify: `.github/workflows/skills-update.yml` (header comment, job permissions, App token permissions, "Choose the token" step, `uses:` inputs)
- Modify: `.github/actions/skills-update/action.yml` (description, `token` description, drop the `dispatch-ci`/`ci-workflow` inputs and env)
- Modify: `.github/actions/skills-update/update-pr.sh` (header env docs, drop `DISPATCH_CI`/`CI_WORKFLOW`/step 5/the body CI section, new closing body line, new auto-merge step)
- Test: `tests/template/test_skills_update.py`

**Interfaces:**
- Produces: the action's inputs are exactly `token`, `branch`, `base`, `labels`. `update-pr.sh` env no longer reads `DISPATCH_CI`/`CI_WORKFLOW`.

- [ ] **Step 1: Write and adjust the failing tests** in `tests/template/test_skills_update.py`:
  - Fake `gh`: add a `pr merge` branch that logs, and exits 1 with `auto-merge is not allowed` on stderr when `FAKE_GH_FAIL_MERGE` is set. Remove the `workflow run` branch, so any dispatch now fails as "unexpected gh call".
  - `_Helper.run`: drop the `DISPATCH_CI` and `CI_WORKFLOW` env.
  - Replace the dispatch assertions in `test_first_run_pushes_creates_pr_and_dispatches` (rename to `…_and_enables_auto_merge`), `test_second_identical_run_reuses_pr_and_skips_push_and_dispatch` (→ `…_skips_push_and_auto_merge`), `test_changed_drift_force_pushes_and_dispatches_again` (→ `…_and_re_enables_auto_merge`) and `test_missing_label_does_not_block_pr`:
    - `helper.gh_calls("pr", "merge") == [["pr", "merge", "7", "--auto", "--squash", "--match-head-commit", <origin skills-update SHA>]]`, read via `git rev-parse` in `helper.origin`;
    - an unchanged run makes no `pr merge` call.
  - New `test_create_on_an_already_current_branch_matches_the_remote_sha`: run once, delete the PR-state file (the PR closed by hand), rebuild the branch, re-run → the `pr merge` `--match-head-commit` equals the remote SHA, which differs from the local `HEAD`.
  - New `test_auto_merge_failure_fails_the_run`: `FAKE_GH_FAIL_MERGE=1` → returncode 1, and stdout+stderr contains `::error::` and `Allow auto-merge`.
  - Delete `test_dispatch_disabled_with_token_path` and `test_body_says_which_checks_did_not_run_on_the_github_token_path`. New `test_body_says_auto_merge_is_on`: the body contains `auto-merge` and doesn't contain `never merges itself`.
  - `test_action_is_a_composite_with_the_documented_inputs`: inputs `== {"token", "branch", "base", "labels"}`.
  - `test_update_workflow_token_precedence` / `test_token_step_picks_app_then_pat_then_github_token`: rewrite to App → PAT, and neither → exit 1 with `::error::` naming `SKILLS_UPDATE_APP_ID` and `SKILLS_UPDATE_TOKEN` and the setup command `setup_skills_update_app.py`. No `GITHUB_TOKEN` in the step env, no `dispatch` output. The `uses:` `with` is exactly `{"token": "${{ steps.token.outputs.value }}"}`.
  - New `test_update_workflow_grants_no_actions_write`: the job permissions have no `actions` key, and the App step has no `permission-actions`.

- [ ] **Step 2: Run, expect failures:** `uv run pytest tests/template/test_skills_update.py -q` → the new and changed tests FAIL.

- [ ] **Step 3: Implement.**
  - In `update-pr.sh`, after labels: when `pushed` or `created` is true, set `head` = `git rev-parse HEAD` if pushed, else `remote_sha`. Run the verbatim auto-merge call. On failure, print `::error::could not enable auto-merge on PR #N` plus the cause hint (Settings → General → "Allow auto-merge", or the token lacks contents/pull requests: write) and exit 1. Otherwise log that the run kept the existing setting.
  - The body's closing line: `This branch is rebuilt on the latest \`<base>\` every night. Auto-merge is on: it merges itself once its required checks pass (through the merge queue where there is one).`
  - The workflow's no-token error text: `No SKILLS_UPDATE_APP_ID/SKILLS_UPDATE_APP_PRIVATE_KEY (GitHub App) or SKILLS_UPDATE_TOKEN (PAT) secret is set. Set one up with: python3 .agents/skills/upgrade-deps/scripts/setup_skills_update_app.py --repo "$GITHUB_REPOSITORY" (see the README, "Staying up to date").`

- [ ] **Step 4: Run** `uv run pytest tests/template/test_skills_update.py -q` → PASS. Run `make machinery_lint` → clean.

- [ ] **Step 5: Commit and push:** `git commit -m "Skills update: require an App or PAT token and enable auto-merge (#82)"`, then `git push`.

### Task 3: Drop the Copilot ruleset rule

**Files:**
- Modify: `.github/repo-settings/ruleset.json` (remove the `copilot_code_review` rule object)
- Modify: `docs/initialization-live-test.md:168-170` (drop the sentence about that rule being rejected; say the template no longer requests Copilot)
- Test: `tests/template/test_apply_repo_settings.py` (or whichever file asserts the ruleset's rule types; `grep -n copilot tests/template` finds none today)

- [ ] **Step 1: Write the failing test** `test_ruleset_has_no_copilot_review_rule`: load `ruleset.json` and assert no rule has `type == "copilot_code_review"`, while `pull_request.parameters.required_review_thread_resolution` is still True.
- [ ] **Step 2: Run** `uv run pytest tests/template/test_apply_repo_settings.py -k copilot -q` → FAIL.
- [ ] **Step 3: Remove the rule and edit the doc.**
- [ ] **Step 4: Run** `uv run pytest tests/template/test_apply_repo_settings.py tests/template/test_initialize.py tests/template/test_merge_queue_checks.py -q` → PASS.
- [ ] **Step 5: Commit and push:** `git commit -m "Drop the Copilot review rule: review-gate is the single source of review truth (#82)"`, then `git push`.

### Task 4: `setup_skills_update_app.py`

**Files:**
- Create: `.agents/skills/upgrade-deps/scripts/setup_skills_update_app.py`
- Modify: `.agents/skills/upgrade-deps/SKILL.md` (one line under bundled scripts: when the nightly run fails with no token, run this script, and what it does)
- Test: `tests/template/test_setup_skills_update_app.py`

**Interfaces** (all in the script; `gh` is invoked as `GH_CMD` env or `gh`, the test seam):
- `build_manifest(repo: str, redirect_url: str, name: str | None = None) -> dict[str, Any]`: name defaults to `f"{repo.split('/')[1]}-skills-update"`; `url` = `https://github.com/<repo>`; `hook_attributes={"url": <repo url>, "active": False}`; `public: False`; `redirect_url`; and the exact `default_permissions`.
- `registration_url(org: str | None) -> str`: `https://github.com/settings/apps/new` or `https://github.com/organizations/<org>/settings/apps/new`.
- `form_html(action: str, manifest: Mapping[str, Any], state: str) -> str`: an auto-submitting POST form with hidden `manifest` (JSON, HTML-escaped) and `state`. `action` = `registration_url(...) + "?state=" + state`.
- `parse_callback(text: str, expected_state: str) -> str`: accepts a full redirect URL or path (`…?code=X&state=S`) or a bare code. Raises `ValueError` on a state mismatch or a missing code. A bare code skips the state check only when pasted (`allow_bare=True` default False for the listener path): signature `parse_callback(text: str, expected_state: str, *, allow_bare: bool = False) -> str`.
- `wait_for_callback(port: int, expected_state: str, timeout: float) -> str`: a one-shot `http.server` on `127.0.0.1`. It answers the browser with a short "you can close this tab" page and returns the code via `parse_callback`.
- `convert_manifest(code: str) -> AppCredentials`, where `AppCredentials(NamedTuple): app_id: int; slug: str; pem: str` and `__repr__` hides `pem`. It runs `gh api -X POST /app-manifests/<code>/conversions`.
- `set_secrets(repo: str, creds: AppCredentials, org_secrets_org: str | None) -> None`: `gh secret set SKILLS_UPDATE_APP_ID --body <id>` and `gh secret set SKILLS_UPDATE_APP_PRIVATE_KEY` with `input=pem`. Repo scope uses `--repo <repo>`. Org scope uses `--org <org> --visibility selected --repos <repo-name>`. A failure raises `SystemExit` with a message that never includes the pem.
- `write_lane_file(path: Path, author: str, branch: str = "skills-update") -> bool`: creates or merges (adds the author if absent, keeps other keys, writes with `indent=2` + newline). Returns whether it changed.
- `main(argv: Sequence[str] | None = None) -> int`: flags `--repo` (required), `--org`, `--org-secrets`, `--existing-app SLUG`, `--no-lane`, `--name`, `--port` (default 0 = any free port), `--no-browser` (print URLs and read the pasted redirect from stdin). Preflight: `gh auth status`, and `gh api repos/<repo> --jq .permissions.admin` must be `true`, else exit 1 naming the problem. It opens files and URLs with `webbrowser.open`, and with `--no-browser` it prints them instead. It asks the person to press Enter after installing. It prints a summary and next steps (commit the lane file; a `workflow_dispatch` run of "skills update" proves the install).

- [ ] **Step 1: Write the failing tests** (load the script with `importlib`, like `test_review_gate._load`; use a fake `gh` on `GH_CMD` that logs argv and stdin to JSON lines, returns the canned conversion JSON `{"id": 42, "slug": "acme-skills", "pem": "-----BEGIN RSA PRIVATE KEY-----\nSECRET\n-----END RSA PRIVATE KEY-----\n", …}`, and `{"admin": true}`-style repo JSON):
  - `test_manifest_is_private_minimal_and_webhookless`: permissions exact, `public is False`, `hook_attributes["active"] is False`, `redirect_url` passed through, default name `repo-skills-update`.
  - `test_registration_url_user_vs_org`.
  - `test_form_posts_manifest_and_state`: `method="post"`, the action contains `state=`, and the manifest JSON round-trips after unescaping.
  - `test_parse_callback`: a full URL with the right state → code; a wrong state → ValueError; a missing state on the listener path → ValueError; a bare code with `allow_bare=True` → code; no code → ValueError.
  - `test_wait_for_callback_returns_the_code_on_loopback`: start it in a thread on port 0 (expose the bound port through a `ready` callback or return tuple; implementer's choice, keep it testable) and `urllib.request.urlopen` the callback URL. Loopback is allowed by the no-network rule.
  - `test_set_secrets_sends_the_key_on_stdin_only`: no argv element contains `SECRET`, the stdin of the key call is the pem, and the id goes via `--body 42`.
  - `test_org_secrets_are_scoped_to_the_repo`: `--org acme --visibility selected --repos repo`.
  - `test_credentials_repr_hides_the_key`.
  - `test_write_lane_file_creates_merges_and_dedupes(tmp_path)`.
  - `test_main_existing_app_skips_creation_and_secrets(tmp_path, monkeypatch)`: with `webbrowser.open` stubbed and Enter fed, no `app-manifests` or `secret set` call, and the lane file is written.
  - `test_main_create_flow_end_to_end(tmp_path, monkeypatch)` with `--no-browser`: the pasted redirect URL (built from the state the script printed) is fed via stdin. Expect the conversion called with the code, both secrets set, the lane file holding `acme-skills[bot]`, return 0, and `SECRET` absent from the captured stdout/stderr.
  - `test_main_refuses_without_admin`: the fake repo JSON has admin false → returns 1 with the message `admin`.
  - `test_failed_gh_call_never_leaks_the_key`: the fake `gh` fails `secret set` → SystemExit, and the message has no `SECRET`.

- [ ] **Step 2: Run** `uv run pytest tests/template/test_setup_skills_update_app.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** the script to the Interfaces. Add the `upgrade-deps/SKILL.md` line.
- [ ] **Step 4: Run** `uv run pytest tests/template/test_setup_skills_update_app.py -q` → PASS. Run `make machinery_lint` → clean (mypy/ruff cover `.agents` scripts the way they do the existing ones; if lint doesn't reach `.agents`, run `uv run ruff check` and `uv run mypy` on the file directly).
- [ ] **Step 5: Commit and push:** `git commit -m "Add setup_skills_update_app.py: create the update App with two clicks (#82)"`, then `git push`.

### Task 5: README and remaining docs

**Files:**
- Modify: `README.md`, in these places:
  - "Start here" interview: a new question 7 and a new THEN step;
  - the skill table row for `resolve-pr-concerns`;
  - a new `### .github/skills-update-lane.json` subsection after the docs-only one;
  - "Staying up to date": the bullets, the secrets table, and PAT machine-account wording;
  - "Adopting the kit in an existing repo": drop the dispatch requirements, add the setup command.
- Modify: any remaining text saying the update PR never merges itself (`grep -rn "never merges itself\|merges itself" --include=*.md --include=*.yml --include=*.sh . | grep -v docs/superpowers`).
- Test: `tests/template/test_readme_workflow.py`

Interview copy (exact):
- Question 7: `Set up the GitHub App that opens the nightly skills update PR? It takes two clicks (Create, then Install) and lets that PR land on its own. Options: now (recommended) / I'll use a fine-grained PAT / later (the nightly run fails until one is set).`
- THEN step (before the "Review `git status`" step): `Only if the user answered "now" to question 7: python3 .agents/skills/upgrade-deps/scripts/setup_skills_update_app.py --repo "$TARGET_REPO"` (add `--org <org>` for an org-owned repo). Walk the user through the two clicks, and the lane file it writes lands in the initialization commit. On PAT: show the "Staying up to date" PAT steps. On later: say the nightly run fails until a secret is set, and give the command.

- [ ] **Step 1: Write the failing tests:**
  - `test_readme_interview_offers_skills_update_app_setup`: exactly one numbered question contains `two clicks` and `nightly skills update PR`.
  - `test_readme_setup_runs_app_script_before_init_commit`: the script command is in `_interview_block()`, before `chore: initialize from agentic-ai-dev-template`.
  - `test_readme_customizing_documents_skills_update_lane`: `.github/skills-update-lane.json` is in the "Customizing the skills in your repo" section.
  - `test_readme_staying_up_to_date_has_no_github_token_fallback`: the "## Staying up to date" section contains `setup_skills_update_app.py`, `machine account` and `Allow auto-merge`, and contains neither `| neither |` nor `never merges itself`.
- [ ] **Step 2: Run** `uv run pytest tests/template/test_readme_workflow.py -q` → FAIL.
- [ ] **Step 3: Write the README edits** per spec §4/§6:
  - the secrets table: App (contents + pull requests: write; recommended; set up with the script), then PAT (fine-grained, owned by a dedicated machine account), then a line saying "With neither, the nightly run fails.";
  - the auto-merge bullet (merge queue where present; "Allow auto-merge", which template repos get from `merge-settings.json`);
  - the lane subsection: what it does, finding the login (App `<slug>[bot]`; PAT: the machine account's login), the copy command, the tradeoff that upstream skill text reaches agents unread so only tag-pinned skills qualify, the rule that the file must never list a person, and that a person may still need to resolve a reviewer's thread;
  - adoption: drop the `gh workflow run ci.yml` paragraph and `workflow_dispatch` from the drift-job trigger list, and add the setup command.
- [ ] **Step 4: Run** `uv run pytest tests/template -q` → PASS.
- [ ] **Step 5: Commit and push:** `git commit -m "Document the App setup, the skills-update lane and auto-merge (#82)"`, then `git push`.
