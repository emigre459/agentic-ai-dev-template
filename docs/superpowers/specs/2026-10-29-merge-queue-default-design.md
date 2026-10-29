# Merge queue as the template default — design

Issue: #53. Status: proposed.

## Intent

Agents open PRs quickly. When two developers' agents race for the latest `main`,
requiring up-to-date branches means endless catch-up merges, and dropping that
requirement means merging code CI never saw combined with `main`. A merge queue
tests each PR against the latest `main` (plus whatever is queued ahead of it) and
merges in order. Make it the default for repos created from this template.

## Already in place (from #40)

- `ci.yml` and `review-gate.yml` trigger on `merge_group`; `review_gate.py` passes a
  merge-group commit. `test_review_gate_workflow.py` asserts the `ci.yml` trigger.
- The ruleset's `required_status_checks` has `strict_required_status_checks_policy:
  false` (a queue replaces "branch must be up to date").
- Skills already say: with a queue, merge with "Merge when ready"; `BEHIND` is moot.

## Goals

1. `.github/repo-settings/ruleset.json` declares a `merge_queue` rule:
   `merge_method: SQUASH` (matches the only allowed merge method),
   `grouping_strategy: ALLGREEN`, `max_entries_to_build: 5`,
   `min_entries_to_merge: 1`, `max_entries_to_merge: 5`,
   `min_entries_to_merge_wait_minutes: 5`, `check_response_timeout_minutes: 60`
   (GitHub's UI defaults).
2. The **bootstrap** phase strips it alongside `required_status_checks` (a queue with
   no required checks merges untested; the checks only exist once `ci.yml` reaches
   `main`). The **final** phase applies it. A bootstrap run that finds it live removes
   it, like it does required checks.
3. `apply_repo_settings` only sends the rule where GitHub offers a merge queue, and
   says so when it doesn't:
   - **Eligibility** (from `GET repos/{repo}`, plus `GET orgs/{owner}` only for a
     private org repo): user-owned → **no**; org-owned public → **yes**; org-owned
     private → **yes** if the org plan is `enterprise`, **no** if the plan is
     visible and isn't, **unknown** if the plan isn't visible to the caller.
   - **no** → the rule is dropped from the desired ruleset before planning, and a
     notice says why and that merges proceed without a queue. Re-runs stay no-ops.
   - **unknown** → the rule is sent; if GitHub rejects it (HTTP 422 naming
     `merge_queue`), the PUT/POST is retried once without it and the same notice is
     printed. (Re-runs retry; acceptable for this rare case.)
   - Any other failure still fails hard.
4. A test asserts every required status-check context in `ruleset.json` is produced by
   a job in a workflow that triggers on `merge_group` (otherwise the queue waits on a
   check that never reports).
5. README init steps and `AGENTS.md` tell users merges go through "Merge when ready"
   (and that user-owned repos get no queue).

## Non-goals

- Enabling the queue on this template repo (user-owned; ineligible).
- New queue tuning knobs or per-repo overrides beyond editing `ruleset.json`.

## Anti-goals

- Failing `make finalize_repo_settings` on a repo GitHub won't give a queue.
- A queue that waits forever on a required check that doesn't run on `merge_group`.

## Calls left to the builder (mine)

- The parameter values above (GitHub's defaults).
- Eligibility via owner type / visibility / org plan, with the 422 fallback only for
  the "plan not visible" case. Sources: GitHub's "Managing a merge queue" docs
  (org-owned public, or org-owned private on Enterprise Cloud); third-party reports
  of 422 "Invalid rule 'merge_queue'" on user-owned and Team-plan private repos.
- The 422 match is on the error text containing `merge_queue`, kept in one helper.
