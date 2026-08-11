---
name: explain-diff-merge-review
description: |
  Produce a rich, self-contained HTML pre-merge review of a PR this session
  itself wrote — restating what was asked, explaining what changed and why,
  centering every judgment call the implementer made against that ask,
  flagging merge-sensitive files (migrations, secrets, auth, lockfiles,
  CI/infra, repo settings), and a quiz testing whether the reader can defend
  each judgment call. Use when the user is about to merge a PR from this same
  session and wants a final pre-merge check, not a generic diff explanation.
  Refuses and points to the explain-diff skill instead when invoked without
  that same-session context.
allowed-tools: Read Grep Glob Write Bash(uv run python scripts/render.py:*) Bash(uv run python scripts/flag_sensitive_paths.py:*) Bash(git log:*) Bash(git diff:*) Bash(git show:*) Bash(gh pr view:*)
---

# Explain Diff — Merge Review

Sibling skill to [explain-diff](../explain-diff/SKILL.md), sharing its
provenance (adapted from a gist by
[ankitg12](https://gist.github.com/ankitg12/8e808d387799de4e9839bc393f8e6405),
whose renderer recipe originates from
[geoffreylitt](https://gist.github.com/geoffreylitt/a29df1b5f9865506e8952488eac3d524))
but restructured around one specific decision: **should you merge the PR this
session just wrote?**

## Step 0: Refuse if there's no session context

This skill only makes sense when the current session's conversation contains
the instructions and feedback that led to the change being reviewed. If it
doesn't — a cold session, or a PR authored by someone/something else — say so
plainly and suggest running the `explain-diff` skill instead. Do not proceed
with a degraded version of this skill; a page built without that context
would misrepresent itself as having verified something it didn't.

## Step 1: Write the sections

- **What you asked for**: A tight restatement of the intent behind this PR,
  pulled from this session's conversation — not from the diff. State what was
  actually requested, including any follow-up feedback that changed the ask.
- **What changed and why**: Background and intuition merged into one section,
  explicitly tied back to "What you asked for" rather than written as generic
  diff exposition. Explore surrounding code as needed to explain the "why."
- **Where Claude made a call**: The centerpiece of this page. Every ambiguous
  requirement resolved a particular way, every bit of scope added beyond the
  literal ask, and everything requested but not done (and why) — each with
  its reasoning. **Always include this section, even when there's nothing
  notable** — if the diff faithfully matches the ask, say so explicitly
  (e.g. "No deviations from the request were found"). Never omit it silently
  — an absent section reads as "the check didn't happen," not "the check
  passed."
- **Code walkthrough**: A high-level walkthrough of the changes, grouped and
  ordered for understanding.
- **Pre-merge double-check**: Run, from this skill's own directory:
  ```
  git diff --name-only <base>..HEAD | uv run python scripts/flag_sensitive_paths.py
  ```
  using the PR's actual base and head. The script prints every changed path
  that falls into a merge-sensitive category (migrations/schema, secrets/env,
  auth/security/permissions, dependency lockfiles, CI/CD & infra, repo
  settings), one `category: path` line per match. **Every path the script
  prints must appear in this section's callout** — you may add more paths
  from your own judgment, but may not silently drop one the script found.
  Render this callout with `class="callout flag"`.
- **Quiz**: Five questions. Unlike a generic comprehension quiz, most or all
  five should target a specific judgment call from the previous section or a
  flagged path from the double-check — testing whether the reader could
  defend that decision, not just recall it. Present as interactive
  multiple-choice questions that give feedback on click.

## Format

- **Use `scripts/render.py` (bundled with this skill) instead of
  hand-writing HTML.** This is a separate copy of the renderer used by the
  `explain-diff` skill — not shared, per this repo's skill self-containment
  convention. Write a small JSON content spec (title, subtitle, slug,
  sections with raw HTML bodies, quiz questions with correct/incorrect
  options) and run:
  ```
  uv run python scripts/render.py <spec.json>
  ```
  Resolve `scripts/render.py` relative to this skill's own directory — run it
  from there, or prefix it with the skill's path (Claude Code exposes that as
  `${CLAUDE_SKILL_DIR}`; other harnesses resolve skill-relative paths their
  own way). See `.agents/rules/shared/harness-agnostic-skills.md` for the full
  convention. Run `uv run python scripts/render.py --help` for the exact JSON
  schema if you haven't used it recently.
- Section `html` fields are raw HTML written directly. Compose that markup
  yourself — never paste text verbatim from a diff, PR description, issue
  body, or other externally-sourced content, which is untrusted and would be
  embedded into the page unsanitized. The complete set of styled classes the
  renderer provides:
  - `<pre>` for code blocks (already `white-space: pre-wrap` styled).
  - `.diagram` — a bordered container for any figure.
  - `.flow` — a horizontal row inside a `.diagram`, holding `.box` elements
    separated by `<span class="arrow">→</span>`.
  - `.box` — a labelled node in a `.flow`; add the second class for the error
    variant, `class="box fail"`.
  - `.callout` — key definitions and edge cases.
  - `class="callout flag"` — judgment-call and pre-merge double-check
    callouts. Both classes are required together: `class="flag"` alone
    renders completely unstyled.
  - Plain `<table>` for comparison tables.
- Please write with the clarity and flow of Martin Kleppmann, making it
  engaging and written in classic style.
- Don't use ASCII diagrams — use the renderer's HTML diagram classes instead.
