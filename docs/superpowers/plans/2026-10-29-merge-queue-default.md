# Merge queue default — implementation plan

Spec: `docs/superpowers/specs/2026-10-29-merge-queue-default-design.md`. Issue: #53.
Branch: `53-merge-queue-default`. Execution: orchestrator-driven TDD (one script, coupled
tasks). Push after every commit.

## Task 1 — The rule and the phase split

Files: `.github/repo-settings/ruleset.json`, `scripts/apply_repo_settings.py`,
`tests/template/test_apply_repo_settings.py`.

- RED: `ruleset_for_phase(..., "bootstrap")` drops `merge_queue` (and
  `required_status_checks`); `final` keeps it; the shipped `ruleset.json` has a
  `merge_queue` rule with `merge_method: SQUASH`; a bootstrap plan against a live
  ruleset that has `merge_queue` schedules a PUT (forbidden types include it).
- GREEN: add the rule; generalize the bootstrap filter to a `_DEFERRED_RULE_TYPES` set;
  pass it as `forbidden_rule_types`.
- Commit + push.

## Task 2 — Eligibility and the skip path

- RED:
  - `merge_queue_eligibility(repo_json, org_json)` → `yes` / `no` / `unknown` for:
    user-owned; org public; org private + enterprise plan; org private + team/free
    plan; org private + no plan field.
  - `main()` against a stub runner:
    - user-owned repo, final phase: the POSTed/PUT ruleset has no `merge_queue`, a
      notice names the reason, exit 0, and a second run with the live ruleset
      (no queue) plans no ruleset action;
    - org public: the rule is sent;
    - unknown + 422 naming `merge_queue`: one retry without it, notice, exit 0;
    - unknown + a different error: exit 2 (fail hard).
  - `orgs/{owner}` is only fetched for a private org repo.
- GREEN: implement; keep the 422 detection in one helper.
- Commit + push.

## Task 3 — Required checks run on `merge_group`

File: `tests/template/test_review_gate_workflow.py` (or a new
`test_merge_queue_checks.py`).

- Every `required_status_checks` context in `ruleset.json` equals the `name` (or job id)
  of a job in a `.github/workflows/*.yml` whose `on` includes `merge_group`.
- Verify it fails if a context is renamed (mutate in-test), then passes on the real files.
- Commit + push.

## Task 4 — Docs

`README.md` init steps (finalize phase enables the queue; merge with "Merge when ready";
user-owned repos get none), `AGENTS.md` (commands table row for
`finalize_repo_settings`; PR guidelines bullet). Commit + push.

## Task 5 — Verify and finalize

`make pr_check`; a dry run of `apply_repo_settings --phase final` against this
(user-owned) repo **answering "N"** at the prompt to show the skip notice and plan
without changing anything. Acceptance audit on #53, PR (`Closes #53`), momentum hook,
`resolve-pr-concerns`.
