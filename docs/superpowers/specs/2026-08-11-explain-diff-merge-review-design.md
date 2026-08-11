# Design: `explain-diff-merge-review` skill (Approach B)

- **Issue:** #24
- **Status:** Approved by user, ready for implementation

## Background

`explain-diff` (Approach A, [design doc](2026-08-11-explain-diff-skill-design.md), merged
in [#23](https://github.com/emigre459/agentic-ai-dev-template/pull/23)) deliberately deferred
a fuller "merge-readiness restructure" — Approach B — until A was proven out. It has been: #23
merged, used live on itself immediately after merging, and in that use a real bug surfaced and
got fixed (the quiz's click-to-reveal feedback depended on inline JavaScript that a sandboxed
preview refused to execute; fixed by moving to a pure-CSS radio/label/`:checked` pattern).

This design builds that second skill: `explain-diff-merge-review`.

## Problem with using explain-diff alone for this case

Approach A is deliberately general-purpose — it degrades gracefully without session context
(falling back to PR description/commits/issue) so it works cold, on someone else's PR, in a
fresh session. That generality is exactly why it undersells the one case this user actually
runs it for most: the same agent that just wrote a PR from the user's own instructions and
feedback explaining it back to them, in the same session, right before merge. That case
deserves a page organized around the merge decision itself, not around teaching a diff to a
stranger — and it can lean on session context without hedging, since when it's genuinely
unavailable the right answer is "use explain-diff instead," not a weaker version of the same
page.

## Scope: Approach B

### Placement

- `.agents/skills/explain-diff-merge-review/SKILL.md` — new skill, canonical location.
- `.agents/skills/explain-diff-merge-review/scripts/render.py` — a **separate copy** of the
  renderer, not shared with `explain-diff`. This costs some duplication, but matches the
  Agent Skills convention that each skill folder is self-contained, and matches how
  `explain-diff` itself was built. The two copies may drift over time; that's an accepted
  cost of skill independence, not an oversight.
- `.agents/skills/explain-diff-merge-review/scripts/flag_sensitive_paths.py` — new bundled
  script (see below).
- One-line edit to `.agents/skills/explain-diff/SKILL.md`'s `description` field, handing off
  the same-session pre-merge case to the new skill, to avoid the two skills' descriptions
  both plausibly matching the same trigger.

### Structure

Six sections, reordered from A around the merge decision itself:

1. **What you asked for.** A tight restatement of intent pulled from *this session's*
   conversation only — no PR-description/commit-message/issue fallback (see Cold invocation,
   below). Not the diff; the ask.
2. **What changed and why.** Background and intuition merged into one section, explicitly
   tied back to (1) rather than written as generic diff exposition.
3. **Where Claude made a call.** The centerpiece — every ambiguity resolution, scope
   addition, or deviation from the literal ask, each with its reasoning. Same
   never-silently-omitted rule as A's Judgment Calls section ("No deviations found" if
   empty), but promoted to position 3 and framed in the prose as the reason this page exists,
   not a courtesy.
4. **Code walkthrough.**
5. **Pre-merge double-check.** Run `scripts/flag_sensitive_paths.py` against the PR's changed
   file list (from `git diff --name-only <base>..HEAD`). It prints candidate paths grouped by
   category, matched against a fixed pattern set:
   - Database/schema migrations (`migrations/`, `alembic/`, `db/migrate/`, `*.sql`)
   - Secrets/env (`.env*`, and any path containing `secret`, `credential`, or `password`)
   - Auth/security/permissions (`auth`, `security`, `permission`, `acl` in the path)
   - Dependency lockfiles (`*.lock`, `package-lock.json`, `requirements.txt`, `go.sum`)
   - CI/CD & infra (`.github/workflows/`, `Dockerfile*`, `docker-compose*`, `*.tf`)
   - Repo settings (`.github/repo-settings/`, `CODEOWNERS`)

   The skill's instructions require every path the script flags to appear in this section's
   callout — the LLM may add more from its own judgment, but may not silently drop one the
   script found. This guarantees the highest-risk file categories never get missed on an off
   day, while leaving the actual risk assessment to the LLM.
6. **Quiz.** Every question targets a judgment call from (3) or a flagged path from (5),
   testing whether the reader could defend the decision — not recall it. Stricter than A's
   "1-2 of 5"; here the whole page is organized around decisions worth defending, so most or
   all five questions should be decision-testing.

### Cold invocation

If the current session contains no conversation to draw "what you asked for" from, the skill
refuses outright: it says so plainly and tells the user to run `explain-diff` instead. No
best-effort fallback — the page's entire value proposition (verifying the agent's own
judgment calls against what was actually asked) doesn't exist without that context, and a
degraded version would be actively misleading rather than merely weaker.

### Output

Same rich, self-contained HTML approach as A: a JSON content spec authored by the LLM,
rendered by the skill's own `render.py`, using the same CSS class vocabulary (`.diagram`,
`.flow`/`.box`/`.box.fail`/`.arrow`, `.callout`, `.callout.flag`, `<table>`) for consistency
between the two skills' output. `.callout.flag` is used for both judgment-call callouts (as
in A) and the pre-merge double-check callout.

### Validation plan

Same bar as A: `.agents/skills/review-skill/scripts/validate.py` against the new skill
directory (0 failures), a manual dry-run of `render.py` against a sample spec, and a manual
test of `flag_sensitive_paths.py` against a small synthetic changed-file list covering each
pattern category (confirm it flags matches, confirm it doesn't flag unrelated paths).

## Out of scope

- Sharing `render.py` between the two skills (rejected — breaks self-containment).
- A lighter markdown/terminal output mode (rejected — HTML stays consistent with A and more
  scannable for diagrams/callouts).
- Any change to `explain-diff`'s own structure or behavior beyond the one-line description
  edit for trigger disambiguation.
