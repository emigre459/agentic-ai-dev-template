---
name: explain-diff
description: |
  Produce a rich, self-contained HTML explanation of a code change, diff,
  branch, or PR — background context, intuition, a code walkthrough, an
  explicit account of judgment calls the implementer made, and a
  comprehension quiz. Use for a cold or general diff/PR/branch explanation —
  reviewing someone else's work, revisiting an old change, or a fresh session
  with no memory of how the change came about. For a same-session pre-merge
  check of a PR this session itself just wrote, use explain-diff-merge-review
  instead.
allowed-tools: Read Grep Glob Write Artifact Bash(uv run python scripts/render.py:*) Bash(git log:*) Bash(git diff:*) Bash(git show:*) Bash(gh pr view:*)
---

# Explain Diff

Adapted from a gist by [ankitg12](https://gist.github.com/ankitg12/8e808d387799de4e9839bc393f8e6405),
whose renderer recipe originates from
[geoffreylitt](https://gist.github.com/geoffreylitt/a29df1b5f9865506e8952488eac3d524).

Produce a rich, interactive explanation of the specified code change as a
self-contained HTML page.

## Step 1: Reconstruct intent

Before writing any section, establish what was actually asked for:

- If this session contains the conversation, instructions, and feedback that
  led to the change, use that as the primary source of intent.
- Otherwise (a cold session, or a change authored by someone/something else),
  fall back to the PR description, commit messages, and any linked issue.
- If neither source gives usable intent context, say so plainly in the
  Background section below rather than guessing at intent.

## Step 2: Write the sections

- **Background**: Explain the existing system relevant to this change.
  (Explore surrounding code broadly for this.) Include a deep background for
  beginners (skippable if the reader is already familiar), then a narrower
  background directly relevant to the change.
- **Intuition**: Explain the core intuition for the code change. Focus on the
  essence, not full details. Use concrete examples with toy data. Use figures
  and diagrams liberally.
- **Judgment Calls & Assumptions**: Using the intent established in Step 1,
  call out anything the implementer decided without an explicit instruction —
  ambiguous requirements resolved a particular way, scope added beyond the
  literal ask, or requested things not done (and why). **Always include this
  section, even when there's nothing notable.** If the diff faithfully
  matches the ask, say so explicitly (e.g. "No deviations from the request
  were found"). Never omit it silently — an absent section reads as "the
  check didn't happen," not "the check passed."
- **Code**: Do a high-level walkthrough of the changes. Group and order
  changes in an understandable way.
- **Quiz**: Come up with five questions that test the reader's knowledge of
  this PR — medium difficulty, hard enough that understanding the substance
  is required, not gotchas. **At least one or two questions must target a
  judgment call or non-obvious trade-off from the previous section**, when
  any exist — testing whether the reader could defend the decision, not just
  recall it. Present these as interactive multiple-choice questions that give
  feedback on click.

## Format

- **Use `scripts/render.py` (bundled with this skill) instead of hand-writing
  HTML.** Repeated invocations of this skill tend to regenerate near-identical
  CSS boilerplate every time, which wastes tokens and drifts in quality —
  that's factored out once, here. Write a small JSON content spec (title,
  subtitle, slug, sections with raw HTML bodies, quiz questions with
  correct/incorrect options) and run:
  ```
  uv run python scripts/render.py <spec.json>
  ```
  Resolve `scripts/render.py` relative to this skill's own directory — run it
  from there, or prefix it with the skill's path (Claude Code exposes that as
  `${CLAUDE_SKILL_DIR}`; other harnesses resolve skill-relative paths their own
  way). See `.agents/rules/shared/harness-agnostic-skills.md` for the full
  convention.

  This handles all CSS, page scaffolding, table of contents, quiz-option
  randomization (interactivity is pure CSS, no JavaScript — this keeps it
  working in sandboxed previews that disable script execution), and the
  date-prefixed output filename automatically — only write the content spec,
  not the full HTML page. Run `uv run python scripts/render.py --help` for the
  exact JSON schema if you haven't used it recently.
- **Prefer a rich-page publishing mechanism over a raw file attachment when
  showing the result.** If the current harness has one (e.g. Claude Code's
  `Artifact` tool), publish the rendered page through it rather than sending
  the file as a generic attachment. A plain file-attachment preview has been
  observed to intercept same-page anchor-link (`<a href="#...">`) clicks,
  breaking the table of contents, even though the underlying HTML is correct
  (verified working in an unrestricted browser) — a rich-page publishing
  pipeline does not have this problem. If no such mechanism is available in
  the current harness, tell the user to open the rendered file directly in a
  browser for full interactivity.
- Section `html` fields in the spec are raw HTML written directly. Compose that
  markup yourself — never paste text verbatim from a diff, PR description, issue
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
  - `class="callout flag"` — judgment-call callouts. Both classes are required
    together: `class="flag"` alone renders completely unstyled. Visually
    distinct from a plain `.callout` — don't reuse plain `.callout` for these.
  - Plain `<table>` for comparison tables.
- Please write with the clarity and flow of Martin Kleppmann, making it
  engaging and written in classic style. Transitions between sections should
  be smooth.
- Some tips on diagrams. Pick a small number of diagram families that can be
  reused throughout the explanation to explain various cases. Some useful
  kinds:
  - A very simplified version of the UI the user sees in the app, to explain
    UI changes.
  - A system diagram showing data flow or communication between components,
    with example data included.
- Don't use ASCII diagrams — use the renderer's HTML diagram classes instead.
