# Explain-Diff Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the `explain-diff` gist command into a proper `.agents/skills/explain-diff/` skill, adapted so it verifies AI-authored PRs match intent (not just generic diff explanation), per `docs/superpowers/specs/2026-08-11-explain-diff-skill-design.md`.

**Architecture:** A single `SKILL.md` (instructions) plus a bundled `scripts/render.py` (stdlib-only HTML template renderer, unchanged in its core rendering logic). The skill's job is entirely prompt/instruction authoring — there is no application code with unit tests; "testing" here means running this repo's `review-skill` structural validator and a manual rendering dry-run.

**Tech Stack:** Markdown (skill instructions), Python 3 stdlib (`render.py` — no dependencies, runs under plain `python3`).

## Global Constraints

- Skill directory: `.agents/skills/explain-diff/` — kebab-case, must exactly equal the frontmatter `name` field.
- No `README.md` inside the skill folder.
- Bundled scripts referenced skill-root-relative (`scripts/render.py`), not by absolute or harness-specific path.
- Name `.agents/skills/` as the canonical location in any prose; per-harness dirs (`.claude/skills/`) may appear only as an aside.
- Frontmatter `name` must not contain the reserved words `claude` or `anthropic`.
- `description` ≤ 1024 characters, no XML-angle-bracket tags.
- `SKILL.md` body ≤ 500 lines and ≤ ~20,000 characters.
- `scripts/render.py` must be executable (`chmod +x`).
- No hardcoded secrets; forward slashes only in any path references.
- Credit `ankitg12` (https://gist.github.com/ankitg12/8e808d387799de4e9839bc393f8e6405) and `geoffreylitt` (https://gist.github.com/geoffreylitt/a29df1b5f9865506e8952488eac3d524) explicitly in both `SKILL.md` and `render.py`'s docstring.
- Push to origin after every commit (`-u` on the first); commit per logical slice, not one giant end-of-session commit.
- PR body must close issue #22 (`Closes #22`).

---

### Task 1: Create the skill directory and port `render.py`

**Files:**
- Create: `.agents/skills/explain-diff/scripts/render.py`
- Read (source, do not modify): `/Users/daverenchmccauley/Documents/Projects/explain-diff/render.py`

**Interfaces:**
- Consumes: nothing from other tasks (first task).
- Produces: `scripts/render.py` with a `render(spec: dict) -> str` function and a `main()` CLI entrypoint (`python3 scripts/render.py <spec.json> [-o output.html]`) — Task 2 modifies this same file's CSS block in place; Task 5's dry run invokes it via `python3 scripts/render.py`.

- [ ] **Step 1: Create the directory structure**

```bash
mkdir -p .agents/skills/explain-diff/scripts
```

- [ ] **Step 2: Write `scripts/render.py`**

Port the source file at `/Users/daverenchmccauley/Documents/Projects/explain-diff/render.py` verbatim, with one change: update the module docstring's opening paragraph to credit both original authors and point at the canonical skill path instead of a Claude-specific one. Replace:

```python
"""
render.py — render a structured explain-diff spec into the self-contained HTML
page format used by the `explain-diff-html` command (see ~/.claude/commands/
explain-diff-html.md, recipe from
https://gist.github.com/geoffreylitt/a29df1b5f9865506e8952488eac3d524).

Why this exists: the CSS, quiz JavaScript, and page scaffolding are identical
```

with:

```python
"""
render.py — render a structured explain-diff spec into the self-contained HTML
page format used by the `explain-diff` skill (canonically at
.agents/skills/explain-diff/SKILL.md). Adapted from a gist by ankitg12
(https://gist.github.com/ankitg12/8e808d387799de4e9839bc393f8e6405), whose
renderer recipe originates from geoffreylitt
(https://gist.github.com/geoffreylitt/a29df1b5f9865506e8952488eac3d524).

Why this exists: the CSS, quiz JavaScript, and page scaffolding are identical
```

Everything else in the file (the `CSS` constant, `QUIZ_JS`, `slugify`, `render`, `main`) is ported byte-for-byte from the source in this step — Task 2 makes the one CSS addition afterward, as its own commit.

- [ ] **Step 3: Make it executable**

```bash
chmod +x .agents/skills/explain-diff/scripts/render.py
```

- [ ] **Step 4: Verify it runs**

```bash
cd .agents/skills/explain-diff && python3 scripts/render.py --help
```

Expected: argparse help text listing `spec` (positional) and `-o/--output`, no tracebacks.

- [ ] **Step 5: Commit**

```bash
git add .agents/skills/explain-diff/scripts/render.py
git commit -m "feat: port render.py into explain-diff skill, credit original authors"
git push
```

---

### Task 2: Add the `.callout.flag` CSS modifier

**Files:**
- Modify: `.agents/skills/explain-diff/scripts/render.py` (the `CSS` string constant)

**Interfaces:**
- Consumes: `scripts/render.py` from Task 1 (must already exist).
- Produces: a `.callout.flag` CSS class, consumed by SKILL.md's instructions (Task 3) telling the LLM to apply it to judgment-call callouts, and exercised by Task 5's dry-run spec.

- [ ] **Step 1: Add the CSS rule**

In the `CSS` constant, immediately after the existing `.callout` rule:

```python
  .callout { background: var(--callout-bg); border-left: 4px solid var(--accent); padding: .9rem 1.2rem;
    border-radius: 0 6px 6px 0; margin: 1.2rem 0; }
```

add:

```python
  .callout.flag { background: #fef2f2; border-left-color: #dc2626; }
```

(This reuses `.callout`'s padding/margin/border-radius via CSS cascade — `class="callout flag"` in generated HTML applies both rules. The red matches the existing `.feedback.incorrect` color already used elsewhere in this stylesheet, so the palette stays internally consistent.)

- [ ] **Step 2: Verify the CSS is well-formed**

```bash
grep -c "callout.flag" .agents/skills/explain-diff/scripts/render.py
python3 -m py_compile .agents/skills/explain-diff/scripts/render.py && echo "compiles OK"
```

Expected: the `grep -c` returns `1` (the new rule is present exactly once), and `compiles OK` prints with no syntax error.

- [ ] **Step 3: Commit**

```bash
git add .agents/skills/explain-diff/scripts/render.py
git commit -m "feat: add .callout.flag style for judgment-call callouts"
git push
```

---

### Task 3: Write `SKILL.md`

**Files:**
- Create: `.agents/skills/explain-diff/SKILL.md`

**Interfaces:**
- Consumes: `scripts/render.py` (Tasks 1-2) — referenced by path and by its `.callout`/`.callout.flag`/`.diagram`/`.flow`/`.box`/`.box.fail` CSS classes.
- Produces: the skill's frontmatter `name: explain-diff` and `description`, which Task 4's validator checks against the folder name and length/format constraints.

- [ ] **Step 1: Write the file**

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add .agents/skills/explain-diff/SKILL.md
git commit -m "feat: add explain-diff skill instructions"
git push
```

---

### Task 4: Validate the skill structurally

**Files:**
- Read: `.agents/skills/explain-diff/` (target)
- Read: `.agents/skills/review-skill/scripts/validate.py` (validator, unmodified)

**Interfaces:**
- Consumes: the completed `.agents/skills/explain-diff/` directory from Tasks 1-3.
- Produces: a passing (`0` failures) validator run, gating Task 5.

- [ ] **Step 1: Run the validator**

```bash
python3 .agents/skills/review-skill/scripts/validate.py .agents/skills/explain-diff
```

Expected: `Results: N passed, 0 failed, ...` — 0 in the failed count. Warnings are acceptable if justified (e.g. the "references/ files not mentioned" check is a SKIP since there is no `references/` dir), but re-read any WARN and decide whether it's worth fixing.

- [ ] **Step 2: Fix any FAIL results**

If the exit code is `1`, read the printed `FAILURES:` list, fix each item in `SKILL.md` or the directory structure, and re-run Step 1 until it exits `0`.

- [ ] **Step 3: Remove the generated result file from version control**

`validate.py` writes `.agents/skills/explain-diff/.validation-result.json`. Don't commit it — it's a throwaway artifact of this check, not part of the skill.

```bash
rm -f .agents/skills/explain-diff/.validation-result.json
git status
```

Expected: `.validation-result.json` does not appear in `git status` (either never staged, or removed before it was).

- [ ] **Step 4: Commit only if Steps 1-3 required file changes**

If Step 2 changed any tracked file:

```bash
git add .agents/skills/explain-diff/SKILL.md
git commit -m "fix: address explain-diff skill validator findings"
git push
```

If no fixes were needed, skip this step (nothing to commit).

---

### Task 5: Dry-run `render.py` end-to-end

**Files:**
- Create (temporary, not committed): `/tmp/explain-diff-sample-spec.json`

**Interfaces:**
- Consumes: `.agents/skills/explain-diff/scripts/render.py` (Tasks 1-2).
- Produces: a rendered HTML file proving the renderer (including the new `.callout.flag` rule) works end-to-end; this is the final gate before opening the PR.

- [ ] **Step 1: Write a sample spec exercising every feature, including `.callout.flag`**

```bash
cat > /tmp/explain-diff-sample-spec.json << 'EOF'
{
  "title": "Sample: add retry jitter",
  "subtitle": "Dry run for the explain-diff skill",
  "slug": "sample-retry-jitter",
  "sections": [
    {"id": "background", "heading": "Background", "html": "<p>The retry loop previously used a fixed delay.</p>"},
    {"id": "intuition", "heading": "Intuition", "html": "<p>Jitter avoids thundering-herd retries.</p><div class=\"diagram\"><div class=\"flow\"><div class=\"box\">Retry 1</div><span class=\"arrow\">&#8594;</span><div class=\"box fail\">Retry 2 (no jitter)</div></div></div>"},
    {"id": "judgment-calls", "heading": "Judgment Calls & Assumptions", "html": "<div class=\"callout flag\">The ask didn't specify a jitter algorithm; full jitter (random between 0 and the exponential delay) was chosen over equal jitter because it spreads retries more evenly.</div>"},
    {"id": "code", "heading": "Code walkthrough", "html": "<pre>delay = random.uniform(0, base_delay * 2 ** attempt)</pre>"}
  ],
  "quiz": [
    {
      "question": "Why was full jitter chosen over equal jitter for the retry delay?",
      "options": [
        {"text": "It was the only algorithm the retry library supported.", "correct": false},
        {"text": "It spreads retries more evenly across the delay window than equal jitter.", "correct": true}
      ]
    }
  ]
}
EOF
```

- [ ] **Step 2: Render it**

```bash
python3 .agents/skills/explain-diff/scripts/render.py /tmp/explain-diff-sample-spec.json -o /tmp/explain-diff-sample-output.html
```

Expected: prints `/tmp/explain-diff-sample-output.html`, exits 0, no traceback.

- [ ] **Step 3: Verify the output contains the new style and renders sensibly**

```bash
grep -c "callout flag" /tmp/explain-diff-sample-output.html
grep -c ".callout.flag" /tmp/explain-diff-sample-output.html
python3 -c "import html.parser; html.parser.HTMLParser().feed(open('/tmp/explain-diff-sample-output.html').read()); print('parses OK')"
```

Expected: both `grep -c` calls return `1` or more (the HTML uses the class, and the `<style>` block defines it), and `parses OK` prints with no parser exception.

- [ ] **Step 4: Clean up the temporary files**

```bash
rm -f /tmp/explain-diff-sample-spec.json /tmp/explain-diff-sample-output.html
```

(Nothing to commit — these files were never inside the repo.)

---

### Task 6: Open the pull request

**Files:** none (git/gh operations only)

**Interfaces:**
- Consumes: all prior tasks' commits on `feat/22-explain-diff-skill`.
- Produces: an open PR closing issue #22.

- [ ] **Step 1: Confirm the branch is clean and pushed**

```bash
git status
git log --oneline origin/main..HEAD
```

Expected: working tree clean; the log shows exactly this plan's commits (from Tasks 1-4).

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "Add explain-diff skill" --body "$(cat <<'EOF'
Adds `.agents/skills/explain-diff/`, adapted from a gist by
[ankitg12](https://gist.github.com/ankitg12/8e808d387799de4e9839bc393f8e6405)
(renderer recipe originally from
[geoffreylitt](https://gist.github.com/geoffreylitt/a29df1b5f9865506e8952488eac3d524)),
per the design in
docs/superpowers/specs/2026-08-11-explain-diff-skill-design.md.

Closes #22
EOF
)"
```

- [ ] **Step 3: Report the PR URL**

`gh pr create` prints the PR URL — confirm it opened successfully and share it.
