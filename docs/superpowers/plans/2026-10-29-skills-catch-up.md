# Skills Catch-Up (#47) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring build-from-issue, plan-issues, resolve-pr-concerns, clean-up-kanban, review-skill and pr-check up to the most advanced downstream behavior, org-neutral, with every helper bundled in its own skill on stdlib `python3`, plus a per-test speed guard for both stacks.

**Architecture:** Port downstream helper scripts into `.agents/skills/resolve-pr-concerns/scripts/` on top of canonical (keeping canonical's no-reviewer handling and conventions), replace the two bash+jq helpers with python, add a generalized merge gate and an opt-in-by-file CODEOWNERS gate, add a stack-neutral speed guard under root `scripts/` configured from `.github/pr-reviewer.json`, then port the SKILL.md prose and references.

**Tech Stack:** stdlib Python 3 (helpers), pytest + black + ruff + mypy (template machinery tests), GNU make, GitHub Actions, `gh` CLI, pytest/Vitest JUnit XML.

**Spec:** `docs/superpowers/specs/2026-10-29-skills-catch-up-design.md`

**Working notes (gitignored, present in this worktree):** `.context/inventory/{build-from-issue,plan-issues,resolve-pr-concerns-prose,scripts}.md` classify every downstream hunk; `.context/downstream/<repo>/…` holds the downstream sources. Abbreviations below: **W** = downstream repo A's `.claude/skills/work-the-backlog/scripts/` (under `.context/downstream/…`), **DT** = downstream repo A's `tests/`, **GA** = downstream repo A's root, **C** = canonical `.agents/skills/resolve-pr-concerns/scripts/`.

## Global Constraints

- No organization, product, private-repo, person/login, internal tool, Slack channel, CI check name, ruleset id, or downstream issue/PR number in any file under `.agents/skills/`, or in any test/fixture this plan adds. Use `owner/repo`, logins like `octocat` / `reviewer-a`, paths like `src/app/...`. Drop downstream `#NNNN` evidence; keep the reasoning.
- Vendor names (`bugbot`, `coderabbit`, case-insensitive) never appear in build-from-issue or the seam files `test_pr_reviewer_seam.py` guards; vendor strings live only in `.github/pr-reviewer.json` and `references/reviewers/<key>.md`.
- Every skill helper: stdlib-only `python3`, lives in its own skill's `scripts/`, invoked as `python3 <skill-dir>/scripts/<name>.py`; helpers in one folder may import each other by module name (as `review_gate` imports `reviewer_state` today). No skill references another skill's `scripts/`.
- Keep canonical conventions in every ported script: NumPy docstrings, `# nosec B404/B603/B607` on subprocess imports/calls, explicit `raise` (never control-flow `assert`), `NoActiveReviewer` / `"active": null` support.
- `.github/pr-reviewer.json` keeps `"active": null`.
- Harness-agnostic prose (`.agents/rules/shared/harness-agnostic-skills.md`): `.agents/skills/` canonical, skill-root-relative bundled paths, no mandatory Claude-only tool, no `ScheduleWakeup`/`Monitor` names.
- Merge-ready is a report to the human; no skill or script merges or treats its own output as merge permission. Permission is one-off and per PR.
- Speed guard: `budget_seconds` 10, dynamic (scaled by runner speed, clamp 0.5–2), configured only in `.github/pr-reviewer.json` → `test_speed`.
- Stacks: ≤ 4 layers, all from one planned card, < 300 files changed across the stack; one PR is the default; `gh stack` is installed manually by the user (never by the agent).
- Push after every commit (`.agents/rules/shared/push-every-commit.md`). Commit messages end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Gate after each task: `make machinery_lint && make machinery_tests` (fast); full `make pr_check` at Task 16.

## Review Focus

1. **The template's own config (`"active": null`) through every new code path** — every helper, the merge gate and the speed guard must run cleanly on the shipped config, not only on a test adapter. Owning tasks add a `*_with_the_shipped_config` test.
2. **Paginated `gh` output** (one JSON array per page) in every reader — `human_review`, `merge_gate`, `wait_for_pr_checks` all use `reviewer_state._gh_json`; Task 2 pins the flattening, Tasks 5/7/8 assert they call it with `--paginate`.
3. **CODEOWNERS edge syntax** (team/email owners, mixed lines, `!`/`[]` patterns, a missing or empty file) — the gate must never raise; Task 6 pins each.
4. **A react-only initialized repo** — the speed guard runs with only stdlib `python3`, no `scripts/__init__.py`, no `tomllib` dependency; Task 10 runs the checker by path in a tmp copy without the package marker.
5. **Malformed or empty JUnit, and test names with regex characters** — a missing/empty JUnit file is a loud failure (never a pass); a Vitest re-run filter escapes the test name. Task 9 pins both.

---

### Task 1: Org-name guard (hashed) + pyproject scrub

**Files:**
- Create: `tests/template/test_no_org_names.py`
- Modify: `pyproject.toml:38` (drop the downstream repo name and issue number from the comment; keep the advice)

**Interfaces:**
- Produces: `tokens_of(text: str) -> set[str]` (lower-cased alphanumeric tokens plus adjacent pairs joined by `-`, `_` and nothing) and `DENYLIST_SHA256: frozenset[str]` in the test module; later tasks rely on this test staying green.

- [ ] **Step 1: Write the failing test.** `test_no_org_name_in_skills_or_new_fixtures` walks `git ls-files .agents/skills tests/template pyproject.toml`, hashes each token from `tokens_of` with SHA-256, and asserts no hash is in `DENYLIST_SHA256`; failure message names file and line (not the token). Seed `DENYLIST_SHA256` with the SHA-256 of the lower-cased org name, its product names and its private repo names (hyphen, underscore and joined forms) — not personal logins (this repo's own owner shares one); fixtures use neutral logins anyway — compute them in a scratch shell from the names in `.context/inventory/*.md`, commit only the hex digests, and never write a plain name in the file or commit message. `test_tokens_of_joins_adjacent_pairs` asserts `tokens_of("Acme-Widget acme_widget") >= {"acme", "widget", "acme-widget", "acme_widget", "acmewidget"}`.
- [ ] **Step 2: Run** `uv run pytest tests/template/test_no_org_names.py -v` — expected FAIL on `pyproject.toml`.
- [ ] **Step 3: Edit the `pyproject.toml` comment** to say "Cross-module type-checking best practice (learned in a downstream repo)."
- [ ] **Step 4: Run** the test — expected PASS.
- [ ] **Step 5: Commit** `test: guard skills against downstream org names (#47)` and push (`git push`).

### Task 2: reviewer_state — loader, config schema, list SHA patterns, unthreaded findings, paginated `_gh_json`

**Files:**
- Modify: `.agents/skills/resolve-pr-concerns/scripts/reviewer_state.py`, `.github/pr-reviewer.json`, `tests/template/test_pr_reviewer_seam.py:39` (`_FIELDS`), `tests/template/test_reviewer_state.py`

**Interfaces:**
- Produces on `ReviewerAdapter`: `reviewed_sha_patterns: tuple[re.Pattern[str], ...]`, `unthreaded_finding_patterns: tuple[re.Pattern[str], ...]`, `incremental_review_done_pattern / _no_changes_pattern / _failed_pattern / _partial_pattern: re.Pattern[str] | None`; on `PreflightSpec`: `min_remaining: int` (default 1); on `LocalReviewSpec`: `start_failure_pattern: re.Pattern[str] | None`; on `RoundCaps`: `max_incremental_before_final: int = 1`, `max_incremental_after_final: int = 3`, `max_fix_iterations: int = 5` (existing local caps unchanged).
- Produces functions (signatures as in W): `unthreaded_findings(reviews, comments, head_sha, adapter) -> list[dict[str, str]]` (keys `source`, `id`, `text`), `_gh_json(args: Sequence[str]) -> Any` (raw_decode loop; flattens one-array-per-page).
- Keeps: `NoActiveReviewer`, `load_reviewer(config_path=None, key=None)`, `load_round_caps` with its `isinstance(config, dict)` guard.

- [ ] **Step 1: Port the loader tests** from `DT/skills/test_work_the_backlog_reviewer_state.py` groups `test_loader_incremental_fields_*`, `test_loader_*reviewed_sha_pattern*`, `test_loader_rejects_bad_unthreaded_finding_patterns`, `test_loader_reads_min_remaining`, `test_unthreaded_findings_*` (rewrite their payloads inline, no fixture), and `test_gh_json_flattens_one_array_per_page` into `tests/template/test_reviewer_state.py`, plus `test_loader_reads_start_failure_pattern` and `test_round_caps_read_incremental_and_fix_keys`. Keep every existing canonical test.
- [ ] **Step 2: Run** `uv run pytest tests/template/test_reviewer_state.py -v` — new tests FAIL.
- [ ] **Step 3: Port the loader/schema pieces from W** (`_pattern_list`, `_reviewed_sha`, incremental-field validation W:521-544 — all four set or all null, and require `command_reply_marker`; `unthreaded_finding_patterns` list W:637-643; `min_remaining` W:110/130/180-184; `_gh_json` W:1738-1758; `_comment_as_review` carrying `id` W:1722-1735). Add `local_review.start_failure_pattern` as an optional key (optional-keys set like `_PREFLIGHT_OPTIONAL_KEYS`). Strip issue refs/paths per Global Constraints.
- [ ] **Step 4: Update `.github/pr-reviewer.json`** for both adapters: `reviewed_sha_pattern` becomes a list (coderabbit: the existing range pattern, then `coveredCommitId":"([0-9a-f]{7,40})"`, then `change_assessment_commit:"([0-9a-f]{7,40})"`; bugbot: `null`), the four incremental patterns (coderabbit values from `GA/.github/pr-reviewer.json`; bugbot: all `null`), `unthreaded_finding_patterns` (coderabbit: the two GA values — merge-risk block and outside-diff block; bugbot: `[]`), coderabbit `local_preflight.min_remaining: 2`, coderabbit `local_review.start_failure_pattern: "Failed to start review"`, bugbot none, the refreshed coderabbit `rate_limited_pattern` `(?s)^(?!.*\bAction performed\b).*\b(?:Review rate limited|included review limit is currently reached)\b`, and `review_rounds` gains `max_incremental_before_final: 1`, `max_incremental_after_final: 3`, `max_fix_iterations: 5`. Extend `_FIELDS` in the seam test and allow list-or-null for `reviewed_sha_pattern`.
- [ ] **Step 5: Run** `uv run pytest tests/template -v` — PASS (seam test included).
- [ ] **Step 6: Commit** `feat(resolve-pr-concerns): reviewer_state loader for incremental, unthreaded and list SHA patterns (#47)` and push.

### Task 3: reviewer_state — incremental outcome, finished windows, on-head and running detection

**Files:**
- Modify: `C/reviewer_state.py`, `tests/template/test_reviewer_state.py`
- Create: `tests/template/fixtures/reviewer_state/synthetic-pr.json`

**Interfaces:**
- Produces (W signatures): `IncrementalReview` NamedTuple, `incremental_review(comments, adapter, reviews=(), ...) -> IncrementalReview` (outcome in `not_tracked|not_requested|pending|finished|no_changes|failed|not_completed`), `finished_windows(comments, adapter) -> list[tuple[str, str]]`, `review_verdict(reviews, head_sha, adapter, windows=...)`; `collect_state` output adds `incremental_review_outcome`, `incremental_review_on_head`, `review_running`, `head_committed_at`, `head_first_status_at`, `unthreaded_findings`; `needs_trigger` is false while `review_running`.

- [ ] **Step 1: Build the synthetic fixture.** A JSON timeline (`comments`, `reviews`, `commits` with `committedDate`, `statuses`) for `owner/repo` PR 1 that reproduces the orderings the DT per-PR replay tests and `test_collect_state_*` replays assert: a walkthrough comment carrying `coveredCommitId` and `change_assessment_commit` lines; command replies with the coderabbit `command_reply_marker` reading "Review finished", "Review failed", "No files to review", "Action not completed", and one "incremental review system" note; summary reviews matching `findings_pattern` and the clean case; an empty review and a nitpick-only review; a `<!-- final_review_risk_start -->…<!-- final_review_risk_end -->` block and an "Outside diff range comments (1)**" block. No code excerpts — bodies are one-line placeholders.
- [ ] **Step 2: Port the tests** for incremental outcome (`test_an_unanswered_trigger_is_pending`, `test_the_newest_trigger_owns_the_outcome`, `test_a_verdict_bearing_review_on_the_head_lands_a_lagging_reply`, …), windows (`test_finished_windows_span_request_to_the_replys_last_edit`, `test_a_window_ends_at_the_next_request_of_either_kind`), on-head (`test_the_request_is_on_head_when_it_follows_the_head_commit`, `test_a_status_before_the_request_keeps_it_on_head`), the replays against the synthetic fixture (renamed `test_synthetic_*`), `test_an_empty_review_alone_is_never_a_clean_verdict`, and `test_collect_state_with_the_shipped_config_reports_no_reviewer` (Review Focus 1).
- [ ] **Step 3: Run** the module's tests — new ones FAIL.
- [ ] **Step 4: Port from W** `incremental_review` (1203-1269), `finished_windows` (1272-1309), `review_verdict` window rule (759-807), `_THREADS_QUERY` head-commit date (686-688), the statuses read and on-head/running logic in `collect_state` (1884-1943, issue comments always read).
- [ ] **Step 5: Run** — PASS. **Step 6: Commit** `feat(resolve-pr-concerns): incremental review outcome and finished-window verdicts (#47)`; push.

### Task 4: reviewer_state — round budget and verdict carry

**Files:**
- Modify: `C/reviewer_state.py`, `tests/template/test_reviewer_state.py`
- Create: `tests/template/test_reviewer_state_round_budget.py`, `tests/template/test_reviewer_state_verdict_carry.py`

**Interfaces:**
- Produces (W signatures): `pr_round_caps(adapter, caps) -> RoundCaps`, `round_budget(comments, adapter, caps, ...) -> dict` (`phase`, `incremental_before_final`/`incremental_after_final` as `{"used": int, "cap": int}`, `trigger_allowed`, `final_review_due`, `max_fix_iterations`), CLI `--max-rounds-before-final N` / `--max-rounds-after-final N`; `CarrySource`, `carry_source(...)`, `patch_fingerprint(compare, base_blob=None) -> str | None`, `base_at_review(events, reviewed_at, current_base) -> str | None`, `_same_patch_carry(repo, pr, head_sha, source) -> bool`; `collect_state` adds `round_budget`, `verdict_carried_from`, `verdict_carry_reason` (`no_changes` | `same_patch`). `_COMPARE_FILE_CAP = 300`.

- [ ] **Step 1: Port tests** from `DT/skills/test_work_the_backlog_round_budget.py` (35) and `DT/skills/test_work_the_backlog_verdict_carry.py` (51) into the two new files (repo strings → `owner/repo`; config loaded from the real `.github/pr-reviewer.json`), and add `test_a_local_reviewer_gets_no_pr_incremental_rounds`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Port from W** `RoundCaps` additions (322-351), `pr_round_caps`, `round_budget` (1602-1719), CLI overrides with `_non_negative_int`, `carry_source` (1312-1370), `patch_fingerprint` and helpers (1414-1532), `base_at_review` (1558-1599), `_compare`, `_blob_at`, `_same_patch_carry` (1761-1836), and the `collect_state` carry wiring (1929-1935). Replace W's `assert` at 930 with a `raise ValueError`.
- [ ] **Step 4: Run** `uv run pytest tests/template -v` — PASS. **Step 5: Commit** `feat(resolve-pr-concerns): PR review-round budget and verdict carry (#47)`; push.

### Task 5: human_review.py replaces human_review.sh

**Files:**
- Create: `C/human_review.py`
- Delete: `C/human_review.sh`, `tests/template/test_resolve_pr_concerns_human_review_script.py`
- Create: `tests/template/test_human_review.py`

**Interfaces:**
- Produces: CLI `python3 human_review.py --repo <owner>/<repo> --pr <n> [--config <path>]` printing the same JSON contract as the `.sh` (`threads`, `review_bodies`, `open_concern_count`, `changes_requested_by`, `awaiting_rereview`, `rerequest_reviewers`, `answer_marker`); importable `load_bot_logins(config_path: Path | None) -> frozenset[str]`, `standing_requests(...)`, `collect_state(repo: str, pr: int, config_path: Path | None) -> dict`. Exit 64 on usage error or explicit missing/invalid `--config`; no default config → only GitHub Bot accounts excluded.

- [ ] **Step 1: Port tests** from `DT/skills/test_work_the_backlog_human_review.py` (30) plus the canonical cases `test_a_missing_explicit_config_is_a_usage_error`, `test_usage_error_without_a_pr`, `test_no_config_excludes_only_github_bots`, `test_every_read_is_paginated` (asserts each `_gh_json` call carries `--paginate` — Review Focus 2), `test_a_resolved_thread_on_a_later_page_is_not_a_concern`, `test_help_exits_zero`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Port W `human_review.py`** using `reviewer_state._gh_json`, adding the `.sh`'s config behavior and `-h`. **Step 4: Delete the `.sh` and its test.** **Step 5: Run** — PASS. **Step 6: Commit** `feat(resolve-pr-concerns): port the human-review reader to stdlib python (#47)`; push.

### Task 6: path_owners.py + review_gate (running, carried, ownership)

**Files:**
- Create: `C/path_owners.py`, `tests/template/test_path_owners.py`
- Modify: `C/review_gate.py`, `tests/template/test_review_gate.py`, `.agents/skills/resolve-pr-concerns/references/reviewers/{bugbot,coderabbit}.md` (one paragraph each on the ownership gate's team-owner skip)

**Interfaces:**
- Produces: `parse_codeowners(text: str) -> list[Rule]` that never raises on team/email owners or unsupported syntax — such lines become `Rule(pattern, owners=(), skipped=True)`; `owners_for(rules, path) -> tuple[str, ...] | None` (`None` = skipped/ownerless); `ownership_status(...)` as in W; `review_gate.ownership_gate_status(repo, pr, ...) -> tuple[str, str] | None` (`None` when no CODEOWNERS at `.github/CODEOWNERS`, `CODEOWNERS` or `docs/CODEOWNERS` on the default branch).
- `review_gate.main`: `"active": null` and no CODEOWNERS → post nothing (today's behavior); `"active": null` with CODEOWNERS → post the ownership-only status; otherwise ownership first, then the reviewer half.

- [ ] **Step 1: Port tests** from `DT/skills/test_work_the_backlog_path_owners.py` (25; neutral paths/logins) and the 12 new `DT/skills/test_work_the_backlog_review_gate.py` cases (rewrite the real-CODEOWNERS tests against a `tmp_path` file monkeypatched into the reader), and add `test_team_owner_lines_are_skipped_not_fatal`, `test_a_mixed_user_and_team_line_is_skipped`, `test_bang_and_bracket_patterns_are_skipped`, `test_an_empty_codeowners_gates_nothing`, `test_no_codeowners_and_no_reviewer_posts_nothing`, `test_codeowners_with_no_reviewer_posts_an_ownership_only_status`, `test_a_running_review_is_pending_not_failed`, `test_a_carried_verdict_passes` (Review Focus 3).
- [ ] **Step 2: Run** — FAIL. **Step 3: Port W `path_owners.py`** (skip instead of raise) and **W `review_gate.py`** additions (`_ownership_inputs`, `ownership_gate_status`, running/failed/carried texts in `gate_status`), keeping C's `NoActiveReviewer` path restructured per the interface above. **Step 4: Run** — PASS. **Step 5: Commit** `feat(resolve-pr-concerns): CODEOWNERS ownership gate, invisible without a CODEOWNERS file (#47)`; push.

### Task 7: local_review + review_preflight — min_remaining and capacity-aware start failure

**Files:**
- Modify: `C/local_review.py`, `C/review_preflight.py`, `tests/template/test_local_review.py`, `tests/template/test_review_preflight.py`

**Interfaces:**
- `review_preflight.check(...)` returns `exhausted` when `remaining < min_remaining` (detail "only N review(s) left, under the minimum of M"); unreadable `remaining` → state-only verdict with a note.
- `local_review` results and the round record gain `remaining_before` / `remaining_after` (`int | None`); a start failure matching `local_review.start_failure_pattern` whose post-failure re-read is under `min_remaining` → `exhausted` (exit 75), otherwise `failed`; no pattern configured → never `exhausted` from a start failure.

- [ ] **Step 1: Port tests** — the 9 new `DT/skills/test_work_the_backlog_review_preflight.py` and 20 new `DT/skills/test_work_the_backlog_local_review.py` cases (drop issue numbers from names; neutral usage JSON), plus `test_no_start_failure_pattern_never_reads_exhausted`. Keep every canonical-only test.
- [ ] **Step 2: Run** — FAIL. **Step 3: Port** W's `check` minimum gating (200-211, 192-196) and W's `_reread`, `_recheck_after`, `_start_failed_for_capacity`, remaining accounting — matching on the adapter's pattern, not a literal; keep C's `_stderr`, docstrings and "round" wording. **Step 4: Run** — PASS. **Step 5: Commit** `feat(resolve-pr-concerns): local-review allowance minimum and capacity-aware start failures (#47)`; push.

### Task 8: wait_for_pr_checks.py replaces the .sh

**Files:**
- Create: `C/wait_for_pr_checks.py`, `tests/template/test_wait_for_pr_checks.py`
- Delete: `C/wait_for_pr_checks.sh`, `tests/template/test_resolve_pr_concerns_wait_script.py`

**Interfaces:**
- CLI: `python3 wait_for_pr_checks.py <pr> [--repo owner/repo] [--config path] [--timeout-min N] [--bot-window-min N] [--poll-sec N]` — same flags as the `.sh`'s usage text; exit 0 all green, 1 a check failed, 2 timeout, **3 merge state CONFLICTING/DIRTY**, 64 usage/config error (including an active adapter whose `bot_logins` is empty or not a list of non-empty strings). Env `MERGESTATE_POLL_SEC` (default 2, tries 5).
- Uses `reviewer_state.collect_state` in-process for the soft-wait cutoff and prints `REVIEWER: <outcome> (verdict <v>, needs_trigger <b>)` or `REVIEWER: rate limited -- re-post the same <request> request after <retry_after>`.

- [ ] **Step 1: Port tests**: every case in the deleted `test_resolve_pr_concerns_wait_script.py` re-targeted at the `.py` (fake `gh` on PATH, same assertions), the 8 new DT cases, and new `test_conflicting_exits_3`, `test_dirty_exits_3`, `test_unknown_then_conflicting_exits_3`, `test_behind_warns_and_keeps_waiting`, `test_empty_bot_logins_is_a_usage_error`, `test_the_shipped_config_waits_on_ci_only` (Review Focus 1), `test_check_lists_are_read_paginated` (Review Focus 2). The fake `gh` answers `pr view --json mergeable,mergeStateStatus`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** by translating C `.sh` logic (all of it: `active:null` CI-only wait, soft adapter checks, review-gate cancel exemption, post-settle review-of-HEAD poll incl. `reviewed_sha_patterns` and issue-comment summaries, the fail-loud UNRESOLVED REVIEW FINDINGS listing) and adding GA's merge-state preflight (GA wait script 171-225) and reviewer cutoff (266-319). **Step 4: Run** — PASS. **Step 5: Commit** `feat(resolve-pr-concerns): port the PR-checks wait to stdlib python with a merge-state preflight (#47)`; push.

### Task 9: Speed guard — calibrator and checker

**Files:**
- Create: `scripts/runner_speed.py`, `scripts/check_test_speed.py`, `tests/template/test_runner_speed.py`, `tests/template/test_check_test_speed.py`
- Modify: `.github/pr-reviewer.json` (add `test_speed`), `tests/template/test_pr_reviewer_seam.py` (validate the section)

**Interfaces:**
- `.github/pr-reviewer.json` → `"test_speed": {"budget_seconds": 10, "recheck_fraction": 0.6, "factor_low": 0.5, "factor_high": 2.0, "shared_setup_budget_seconds": 30, "default_runner_class": "ubuntu-latest", "reference_seconds": {"ubuntu-latest": 0.874}, "_note": "<one line: budgets are seconds on a typical runner of the class; the benchmark is Python and stands in for single-thread CPU speed on React too>"}`.
- `runner_speed.py`: `calibrate(repeats: int = 5) -> float`, `factor(measured_s: float, reference_s: float, *, low: float, high: float) -> float`, `runner_class(settings: Mapping[str, Any]) -> str` (env `TEST_SPEED_RUNNER_CLASS`, else `default_runner_class`), `reference_for(name: str, settings) -> float` (unknown class → `SystemExit` naming the config key and the printed measurement to add).
- `check_test_speed.py` CLI: `python3 scripts/check_test_speed.py --junit <path> --allowlist <path> --rerun '<cmd with {test}>' [--config .github/pr-reviewer.json] [--factor F] [--no-recheck]`; exits 0 pass, 1 offenders, 2 bad input (missing/empty/unparseable JUnit, no testcases, bad config). Parses pytest (`classname`, `name`, `property name="shared_setup_seconds"`) and Vitest (`classname` = file, `name` = "suite > test") JUnit. `{test}` expands to the pytest node id (`path::name`) or, for Vitest, `<file> -t "<escaped name>"` with regex metacharacters escaped. Prints the runner class, calibration, reference and factor, each offender with its suite and alone times, stale allowlist entries as warnings, and the remedy order (speed it up, usually by mocking the slow dependency → move it to a slower test group → allowlist with a reason and tracking issue).

- [ ] **Step 1: Port tests** from GA `scripts/check_test_speed.py` behavior (recheck verdicts, recheck-fraction near-budget re-measure, shared-setup subtraction and its own budget, allowlist parse and stale warning, pinned factor validation incl. `nan`/`0`) and GA `scripts/runner_speed.py` (factor clamp, bad clamp, unknown class) into the two new test files, plus `test_missing_junit_is_exit_2`, `test_empty_junit_is_exit_2`, `test_vitest_junit_is_parsed`, `test_vitest_rerun_filter_escapes_regex_characters` (Review Focus 5), `test_budget_scales_with_the_factor`, `test_the_shipped_config_section_loads`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** the two scripts as stdlib-only, path-runnable (no `scripts.` package imports — `check_test_speed` loads `runner_speed` by sibling path), JSON config (no `tomllib`), porting GA logic minus the slow lane, xdist flag handling and repo-specific defaults. **Step 4: Run** — PASS. **Step 5: Commit** `feat: stack-neutral per-test speed guard, budget in pr-reviewer.json (#47)`; push.

### Task 10: Speed guard — wiring into both stacks, CI and init

**Files:**
- Modify: `stacks/python/Makefile`, `stacks/react/Makefile`, `Makefile` (root), `stacks/python/pyproject.toml` (register the fixture-timing plugin), `stacks/react/vitest.config.ts` (JUnit reporter on coverage runs), `.github/workflows/ci.yml`, `.gitignore` (`test-results.xml`), `scripts/init_template.py` only if a kept file would otherwise be removed
- Create: `stacks/python/tests/_pytest_fixture_timing.py` (from GA `tests/_pytest_fixture_timing.py`, generalized), `stacks/python/tests/slow_test_allowlist.txt`, `stacks/react/slow_test_allowlist.txt` (header comment only), `tests/template/test_speed_guard_wiring.py`

**Interfaces:**
- Each stack: `coverage` writes `test-results.xml`; `test_speed_check:` runs `python3 $(REPO_ROOT)/scripts/check_test_speed.py --junit test-results.xml --allowlist <stack allowlist> --rerun '<cmd>'` (python: `uv run pytest -p no:xdist -o addopts="" {test}`; react: `bunx vitest run {test}`); `pr_check: lint security` with the recipe `@$(MAKE) --no-print-directory coverage && $(MAKE) --no-print-directory test_speed_check` (the speed check reads the file coverage writes, an ordering `make -j` wouldn't guarantee between sibling prerequisites). Root: `test_speed_check` delegates to both stacks; `pr_check: lint machinery_tests security` with the same coverage-then-speed-check recipe. CI: a `make test_speed_check` step after `make coverage` in `python-tests` and `react-tests`, `if: github.event_name != 'push'`.

- [ ] **Step 1: Write wiring tests** in `test_speed_guard_wiring.py`: `test_each_stack_pr_check_runs_lint_security_then_coverage_then_speed_check`, `test_root_pr_check_includes_machinery_tests_and_the_speed_check`, `test_ci_runs_the_speed_check_after_coverage_except_on_push`, `test_the_budget_is_configured_in_exactly_one_place` (grep the tree: the number lives only in `.github/pr-reviewer.json`), `test_init_keeps_the_speed_guard_for_either_stack` (run `init_template` into tmp copies for `python` and `react` like `test_initialize.py` does; assert `scripts/check_test_speed.py`, `scripts/runner_speed.py` and `.github/pr-reviewer.json` remain), `test_the_checker_runs_without_the_package_marker` (copy the two scripts to a tmp dir without `__init__.py`, run `--help` with `python3 -I` — Review Focus 4).
- [ ] **Step 2: Run** — FAIL. **Step 3: Wire it** per the interface. **Step 4: Run** `uv run pytest tests/template -v` and `make -C stacks/python coverage test_speed_check` and `make -C stacks/react coverage test_speed_check` — PASS; record the `calibration` line each prints. **Step 5: Commit** `feat: wire the per-test speed guard into both stacks, pr_check and CI (#47)`; push. **Step 6:** after this push's CI run, read the `ubuntu-latest` calibration line from the `python-tests` job log and, if it differs from 0.874 by more than 10%, update `reference_seconds.ubuntu-latest` in a follow-up commit (`chore: seed the ubuntu-latest speed reference from CI`), push.

### Task 11: merge_gate.py

**Files:**
- Create: `C/merge_gate.py`, `tests/template/test_merge_gate.py`

**Interfaces:**
- CLI: `python3 merge_gate.py --repo owner/repo --pr N [--stack] [--acceptance-audit-clean true|false] [--config path]`; prints JSON `{"mergeable": bool, "blockers": [str, ...], "info": {"unthreaded_findings": [...], ...}, "layers": [...]}` (`layers` only with `--stack`); exit 0 when mergeable, 1 when blocked, 64 usage. `MERGED` → `already_merged`.
- `required_checks(repo: str, base: str) -> tuple[str, ...] | None` reads `repos/{repo}/rules/branches/{base}` (paginated) for `required_status_checks`; `None` (no such rule, or 404) → every reported check must be non-failing. `evaluate_gate(...)` reuses `human_review.standing_requests` and `reviewer_state` data; `"active": null` skips the reviewer half. `BEHIND` blocks only while the rule's `strict_required_status_checks_policy` is true. Stack layers via `repos/{repo}/stacks?pull_request=N`.

- [ ] **Step 1: Port tests** from `DT/skills/test_work_the_backlog_merge_gate.py` (44) rewritten against injected required-check names (`ci / lint`, `ci / tests`), dropping park tests, plus `test_required_checks_come_from_the_branch_rules`, `test_no_rules_means_every_reported_check_must_pass`, `test_a_rules_404_is_not_an_error`, `test_behind_blocks_only_under_a_strict_policy`, `test_unthreaded_findings_are_info_not_blockers`, `test_the_shipped_config_skips_the_reviewer_half`, `test_a_false_audit_adds_a_blocker_and_absence_does_not`, `test_the_output_never_says_merge` (the word "merge" never appears as an imperative in blocker or info text).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** from W `merge_gate.py` with the de-hardcoding above. **Step 4: Run** — PASS. **Step 5: Commit** `feat(resolve-pr-concerns): generalized merge-readiness report (#47)`; push.

### Task 12: review-skill validator

**Files:**
- Modify: `.agents/skills/review-skill/scripts/validate.py`, `.agents/skills/review-skill/SKILL.md` (`allowed-tools: Bash(python3:*) Read Glob Grep`; run `python3 scripts/validate.py <path>`)
- Create: `tests/template/test_validate_skill.py`

**Interfaces:**
- New check `check_external_script_references(skill_dir: Path, body: str) -> None` (FAIL when SKILL.md or its references tell the agent to run — `python3`, `python`, `bash`, `sh`, `uv run`, or a bare `scripts/x` invocation — a path that resolves outside `skill_dir`; `<placeholder>` paths exempt; prose pointers to `../<sibling>/SKILL.md` allowed). Shebang-aware executable check (only `#!` files need `+x`). `--json <path>` writes `{"results": [{"level", "check", "message"}]}`. Type hints + NumPy docstrings throughout.

- [ ] **Step 1: Write tests**: `test_a_cross_skill_script_invocation_fails`, `test_an_in_skill_script_passes`, `test_a_sibling_skill_md_pointer_is_allowed`, `test_placeholder_paths_are_exempt`, `test_importable_modules_need_no_exec_bit`, `test_json_report_is_written`, and `test_every_shipped_skill_passes` (runs the validator over each `.agents/skills/*`; asserts no FAIL).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (port the idea of ai-tools `_check_external_references`, scoped to invocations; port `_has_shebang`). **Step 4: Run** — PASS (fix any FAIL in shipped skills only where it's a real cross-skill invocation). **Step 5: Commit** `feat(review-skill): fail cross-skill script calls; stdlib python3 (#47)`; push.

### Task 13: resolve-pr-concerns prose + references

**Files:**
- Modify: `.agents/skills/resolve-pr-concerns/SKILL.md`, `references/reviewers/{bugbot,coderabbit}.md`
- Create: `.agents/skills/resolve-pr-concerns/references/stacked-prs.md`
- Modify: `tests/template/test_pr_reviewer_seam.py` (assertions for new must-say lines)

**Interfaces:**
- Consumes every helper CLI above. All `.sh` invocations become `python3 <skill-dir>/scripts/{wait_for_pr_checks,human_review}.py` with the new flags.

- [ ] **Step 1: Add seam assertions** (FAIL first): SKILL.md mentions `merge_gate.py`, `round_budget`, `incremental_review_outcome`, `unthreaded_findings`, `needs_trigger`, `exit 3`, `references/stacked-prs.md`, and "an empty" + "not proof"; contains no `.sh` helper path; coderabbit.md says the `CodeRabbit` signal is a commit status and mentions the 300-file review cap; bugbot.md mentions `BUGBOT_REVIEW_STALE`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Port the prose** per `.context/inventory/resolve-pr-concerns-prose.md` §1–§5 rows classed **a** (and **a + c** now that the scripts exist): poll-loop contract with 60-min timeout, act on incremental outcome, PR round budget, empty-set ≠ proof, inline threads ≠ evidence, unthreaded findings in one disposition comment checked in Step 7, merge state before any wait (exit 3), merge-not-rebase, verdict carry → `needs_trigger`, per-push trigger waits for the head's first status and stays in comment mode, structural enforcement sentence, no silent skip on trivial PRs (only a human's audited bypass) + CODEOWNERS approval reporting, no push during a running full review, poll-filter discipline, `min_remaining`, merge-ready from actual check results, speed-guard coaching (remedy order), the merge gate in Step 7, stack mode pointing at `references/stacked-prs.md`; and the §6 conflict resolutions (K1 batching principle, K2 merge, K3 carry, K9 marker model). Keep every §8 item. Write `references/stacked-prs.md` (detecting a stack, bottom-up driving with per-layer final review, fix on the owning layer, never merge `main` into a layer or `gh pr merge` a layer, CI per layer, `merge_gate.py --stack`, landing with/without a merge queue by the human or with one-off permission) generalized from `GA/references/stacked-prs.md` §Driving the review, §CI on a stack, §Landing, §Gotchas. Update both reviewer notes per inventory §5 (a-rows; RC9 hedged).
- [ ] **Step 4: Run** `uv run pytest tests/template -v` — PASS. **Step 5: Commit** `docs(resolve-pr-concerns): port downstream review handling, stacks and merge gate (#47)`; push.

### Task 14: build-from-issue prose + stacking reference

**Files:**
- Modify: `.agents/skills/build-from-issue/SKILL.md`
- Create: `.agents/skills/build-from-issue/references/stacked-prs.md`
- Modify: `tests/template/test_pr_reviewer_seam.py` (keep `local_review.py` / `review_preflight.py` must-say; add new must-says)

- [ ] **Step 1: Add assertions** (FAIL first): SKILL.md defines "target issue number"; mentions `Refs #N`; has no "4-option menu"; mentions `explain-diff-merge-review`, `docs/superpowers/test-kits/`, `references/stacked-prs.md`, `gh stack --help`; Slack step says "off by default" (or equivalent pinned phrase); no vendor names.
- [ ] **Step 2: Run** — FAIL. **Step 3: Port** per `.context/inventory/build-from-issue.md` §6 items 1–9 plus §5 lessons 1–3, and the stack rows (O.3, 3.1, 5.2, 6.3, 7.1, 11.5, 12.5, RF.8) generalized to the user's policy: offer a stack at plan review when one review would likely miss related changes; ≤ 4 layers, one planned card, < 300 files across the stack, else split via `/plan-issues`; check `gh stack --help` first and, if missing, print `gh extension install github/gh-stack` and `npx skills add github/gh-stack` for the user to run, never installing. `references/stacked-prs.md`: when to stack / don't-stack list, how to split, the plan's `## Stack` section, building per layer, finalize (`make pr_check` on the top layer; CI gates each layer; `gh stack submit`; per-layer PR bodies with Refs/Closes), with/without merge queue notes. Keep every §3 canonical-ahead item. Drop §6 item 11 rows.
- [ ] **Step 4: Run** — PASS. **Step 5: Commit** `docs(build-from-issue): port downstream improvements and opt-in stacked PRs (#47)`; push.

### Task 15: plan-issues + clean-up-kanban

**Files:**
- Modify: `.agents/skills/plan-issues/SKILL.md`, `.agents/skills/clean-up-kanban/SKILL.md`

- [ ] **Step 1: Port** per `.context/inventory/plan-issues.md` §1 rows A1–A9 (A6 named by trigger, not vendor), §5 bullets 1–2, §4 path fix (`../clean-up-kanban/SKILL.md`), and remove the orphaned tracker remnants (canonical lines ~247, 258, 344, 374) instead of porting A10. clean-up-kanban: K1–K3.
- [ ] **Step 2: Verify** `grep -n "Phase 3 step 0" .agents/skills/plan-issues/SKILL.md` shows the defined step and its citations; `grep -niE "jira|twin" .agents/skills/plan-issues/SKILL.md` is empty; `python3 .agents/skills/review-skill/scripts/validate.py .agents/skills/plan-issues` and `... clean-up-kanban` have no FAIL; `uv run pytest tests/template -v` PASS.
- [ ] **Step 3: Commit** `docs(plan-issues, clean-up-kanban): existing-card overlap gate, epic map, REST fallback (#47)`; push.

### Task 16: pr-check skill, docs, rules, allow-lists; full gate

**Files:**
- Modify: `.agents/skills/pr-check/SKILL.md`, `AGENTS.md` (quality-gates table + `make pr_check` sentence), `README.md` (pr_check mentions), `.agents/rules/shared/use-bundled-skill-scripts.md` (`.sh` → `.py`; drop the uv adaptation note for stdlib helpers), `.claude/settings.json` (any allow-list entries for the old `.sh` paths → `python3 …py`), `scripts/init_template.py` only if its rendered text describes `pr_check`

- [ ] **Step 1:** pr-check SKILL.md: `make pr_check` = lint + coverage + speed check + security, mirrors CI; report coverage shortfalls, speed offenders with the remedy order, security findings; `allowed-tools` adds nothing new beyond `Bash(make pr_check)`, `Bash(make format)`.
- [ ] **Step 2:** `grep -rn "human_review.sh\|wait_for_pr_checks.sh\|lint + tests" .agents AGENTS.md README.md .claude .github scripts` returns nothing.
- [ ] **Step 3: Run** `make pr_check` — exit 0 (needs `make deps` first). Paste the tail into the task report.
- [ ] **Step 4: Commit** `docs: pr_check mirrors CI; point every doc at the python helpers (#47)`; push.

### Task 17: Reconcile with #55 (merge queue default)

Run only once PR #55 has merged into `main`.

- [ ] **Step 1:** `git fetch origin && git merge origin/main` (a merge, never a rebase, on a branch with review history). Resolve `AGENTS.md`/`README.md` so both #55's merge-queue text and this branch's `pr_check` text survive.
- [ ] **Step 2:** `make pr_check` — exit 0. **Step 3:** Commit the merge, push.
