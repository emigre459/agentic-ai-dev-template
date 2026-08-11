---
name: explain-diff
description: |
  Produce a rich, self-contained HTML explanation of a code change, diff,
  branch, or PR — background context, intuition, a code walkthrough, an
  explicit account of judgment calls the implementer made, and a
  comprehension quiz. Use when asked to explain a diff, PR, or branch,
  especially one an AI coding agent wrote from instructions and feedback,
  where a human reviewer needs to verify the implementation matches intent
  before merging.
allowed-tools: Read Grep Glob Write Bash(python3 scripts/render.py:*)
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
  CSS/JS boilerplate every time, which wastes tokens and drifts in quality —
  that's factored out once, here. Write a small JSON content spec (title,
  subtitle, slug, sections with raw HTML bodies, quiz questions with
  correct/incorrect options) and run, from this skill's own directory:
  ```
  python3 scripts/render.py <spec.json>
  ```
  This handles all CSS, JavaScript, page scaffolding, table of contents,
  quiz-option randomization, and the date-prefixed output filename
  automatically — only write the content spec, not the full HTML page. Run
  `python3 scripts/render.py --help` for the exact JSON schema if you haven't
  used it recently.
- Section `html` fields in the spec are raw HTML written directly — use
  `<pre>` for code blocks (already `white-space: pre-wrap` styled),
  `.diagram`/`.flow`/`.box`/`.box.fail` divs for flow diagrams, `.callout` for
  key definitions and edge cases, `.callout.flag` specifically for
  judgment-call callouts (visually distinct — don't reuse plain `.callout`
  for those), and plain `<table>` for comparison tables. See
  `scripts/render.py`'s docstring for the exact class names available.
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
