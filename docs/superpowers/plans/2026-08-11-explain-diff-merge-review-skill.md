# Explain-Diff-Merge-Review Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `.agents/skills/explain-diff-merge-review/` — a sibling skill to the already-merged `explain-diff` that restructures its output around one specific decision (should this session's own PR be merged?), per `docs/superpowers/specs/2026-08-11-explain-diff-merge-review-design.md`.

**Architecture:** A `SKILL.md` (six-section instructions + a cold-invocation refusal), a bundled `scripts/render.py` (a separate copy of `explain-diff`'s current renderer, not shared), and a new bundled `scripts/flag_sensitive_paths.py` (a small pure-function script that greps a changed-file list against six merge-sensitive path categories). A one-line edit to `explain-diff/SKILL.md`'s description hands off the same-session pre-merge case to the new skill, avoiding trigger ambiguity between the two.

**Tech Stack:** Markdown (skill instructions), Python 3 stdlib only (both scripts — no dependencies).

## Global Constraints

- Skill directory: `.agents/skills/explain-diff-merge-review/` — kebab-case, must exactly equal the frontmatter `name` field.
- No `README.md` inside the skill folder.
- Bundled scripts referenced skill-root-relative (`scripts/render.py`, `scripts/flag_sensitive_paths.py`).
- Frontmatter `name` must not contain the reserved words `claude` or `anthropic`.
- `description` ≤ 1024 characters, no XML-angle-bracket tags.
- `SKILL.md` body ≤ 500 lines and ≤ ~20,000 characters.
- Both bundled scripts must be executable (`chmod +x`).
- No hardcoded secrets; forward slashes only in any path references.
- Use `uv run python`, never bare `python`/`python3`, for every invocation instruction (per `.agents/rules/python/uv-python.md`, alwaysApply).
- `render.py` is a **separate copy** from `explain-diff/scripts/render.py` — do not import from or symlink to it.
- Credit `ankitg12` (https://gist.github.com/ankitg12/8e808d387799de4e9839bc393f8e6405) and `geoffreylitt` (https://gist.github.com/geoffreylitt/a29df1b5f9865506e8952488eac3d524) in the new `render.py`'s docstring, same as the existing copy.
- Push to origin after every commit (`-u` on the first); commit per logical slice.
- PR body must close issue #24 (`Closes #24`).

---

### Task 1: Port `render.py` into the new skill

**Files:**
- Create: `.agents/skills/explain-diff-merge-review/scripts/render.py`
- Read (source, do not modify): `.agents/skills/explain-diff/scripts/render.py` (already merged, current state)

**Interfaces:**
- Consumes: nothing from other tasks (first task).
- Produces: `scripts/render.py` with a `render(spec: dict) -> str` function and `main()` CLI entrypoint (`uv run python scripts/render.py <spec.json> [-o output.html]`) — Task 6's dry run invokes it.

- [ ] **Step 1: Create the directory structure**

```bash
mkdir -p .agents/skills/explain-diff-merge-review/scripts
```

- [ ] **Step 2: Write `scripts/render.py`**

Copy `.agents/skills/explain-diff/scripts/render.py` verbatim, with these changes to the module docstring only (the `CSS` constant, `slugify`, `render`, and `main` functions are byte-for-byte identical to the source — this skill's renderer is not otherwise different from explain-diff's):

Replace the docstring's opening two paragraphs:

```python
"""
render.py — render a structured explain-diff spec into the self-contained HTML
page format used by the `explain-diff` skill (canonically at
.agents/skills/explain-diff/SKILL.md). Adapted from a gist by ankitg12
(https://gist.github.com/ankitg12/8e808d387799de4e9839bc393f8e6405), whose
renderer recipe originates from geoffreylitt
(https://gist.github.com/geoffreylitt/a29df1b5f9865506e8952488eac3d524).

Why this exists: the CSS, quiz JavaScript, and page scaffolding are identical
across every invocation of the explain-diff skill — only the content (prose,
diagrams, quiz questions) actually changes per diff. Regenerating the full
~250 lines of boilerplate CSS/JS by hand every time wastes tokens. This script
takes a small JSON spec with just the content and renders the final page.
```

with:

```python
"""
render.py — render a structured explanation spec into the self-contained HTML
page format used by the `explain-diff-merge-review` skill (canonically at
.agents/skills/explain-diff-merge-review/SKILL.md). A separate copy of the
renderer originally built for the `explain-diff` skill
(.agents/skills/explain-diff/scripts/render.py) — kept independent per this
repo's skill self-containment convention rather than shared, so the two may
drift over time. That renderer in turn adapted a gist by ankitg12
(https://gist.github.com/ankitg12/8e808d387799de4e9839bc393f8e6405), whose
approach originates from geoffreylitt
(https://gist.github.com/geoffreylitt/a29df1b5f9865506e8952488eac3d524).

Why this exists: the CSS and page scaffolding are identical across every
invocation of the explain-diff-merge-review skill — only the content (prose,
diagrams, quiz questions) actually changes per PR. Regenerating that
boilerplate by hand every time wastes tokens. This script takes a small JSON
spec with just the content and renders the final page.
```

Then update the two remaining self-references further down the docstring — the "Spec format (JSON)" example's `sections` list and the closing paragraph — to match this skill's own section ids and prose, replacing:

```python
Spec format (JSON):
{
  "title": "Rewriting the retry logic: exponential backoff with jitter",
  "subtitle": "Prepared 2026-07-15 · PR #482",
  "slug": "retry-backoff-refactor",
  "sections": [
    {"id": "background", "heading": "Background", "html": "<p>...</p>"},
    {"id": "intuition", "heading": "Intuition", "html": "<p>...</p><div class=\"diagram\">...</div>"},
    {"id": "judgment-calls", "heading": "Judgment Calls & Assumptions",
     "html": "<div class=\"callout flag\"><strong>Scope added:</strong> jitter was not requested, but ...</div>"},
    {"id": "code", "heading": "Code walkthrough", "html": "<pre><code>...</code></pre>"}
  ],
```

with:

```python
Spec format (JSON):
{
  "title": "Adding per-tenant rate limits before merge",
  "subtitle": "Prepared 2026-08-11 · PR #58",
  "slug": "tenant-rate-limits",
  "sections": [
    {"id": "ask", "heading": "What you asked for", "html": "<p>...</p>"},
    {"id": "changed", "heading": "What changed and why", "html": "<p>...</p>"},
    {"id": "judgment-calls", "heading": "Where Claude made a call",
     "html": "<div class=\"callout flag\"><strong>Scope added:</strong> rate limiting was not requested, but ...</div>"},
    {"id": "code", "heading": "Code walkthrough", "html": "<pre><code>...</code></pre>"},
    {"id": "double-check", "heading": "Pre-merge double-check",
     "html": "<div class=\"callout flag\">flagged: stacks/react/bun.lock (dependency lockfiles)</div>"}
  ],
```

And replace the closing paragraph:

```python
The "html" fields are raw HTML — write real markup (headings, <pre> blocks,
tables, and the styled divs listed in the Format section of the explain-diff
skill's SKILL.md), not markdown. This keeps the script a pure template renderer;
all the writing judgment (what to explain, which diagrams to draw) still belongs
to the LLM following the explain-diff skill, same as before — this just removes
the repetitive part.
"""
```

with:

```python
The "html" fields are raw HTML — write real markup (headings, <pre> blocks,
tables, and the styled divs listed in the Format section of the
explain-diff-merge-review skill's SKILL.md), not markdown. This keeps the
script a pure template renderer; all the writing judgment (what to explain,
which judgment calls and flagged paths matter) still belongs to the LLM
following the explain-diff-merge-review skill, same as before — this just
removes the repetitive part.
"""
```

Everything below the docstring (`import` lines, the `CSS` constant, `slugify()`, `render()`, `main()`) is copied byte-for-byte from the source — do not alter any of it in this task.

- [ ] **Step 3: Make it executable**

```bash
chmod +x .agents/skills/explain-diff-merge-review/scripts/render.py
```

- [ ] **Step 4: Verify it runs and matches source except the docstring**

```bash
cd .agents/skills/explain-diff-merge-review && uv run python scripts/render.py --help && cd -
diff <(tail -n +56 .agents/skills/explain-diff/scripts/render.py) <(tail -n +56 .agents/skills/explain-diff-merge-review/scripts/render.py)
```

Expected: `--help` prints argparse help with no traceback; the `diff` prints nothing (everything from line 56 onward — i.e. everything after the docstring — is byte-for-byte identical between the two files). If your source file's docstring is a different length than 56 lines, adjust the line number to start just after the closing `"""`.

- [ ] **Step 5: Commit**

```bash
git add .agents/skills/explain-diff-merge-review/scripts/render.py
git commit -m "feat: port render.py into explain-diff-merge-review skill"
git push
```

---

### Task 2: Write `flag_sensitive_paths.py`

**Files:**
- Create: `.agents/skills/explain-diff-merge-review/scripts/flag_sensitive_paths.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: a `flag(paths: list[str]) -> list[tuple[str, str]]` function returning `(category, path)` pairs in input order, and a `main()` CLI reading paths from `sys.argv[1:]` (falling back to newline-separated stdin when no argv paths are given) — Task 3's `SKILL.md` instructs piping `git diff --name-only` into this script; Task 6's dry run exercises both the function and the CLI.

- [ ] **Step 1: Write the failing test**

```bash
mkdir -p /tmp/fsp-test && cat > /tmp/fsp-test/test_flag.py << 'EOF'
import sys
sys.path.insert(0, "/Users/daverenchmccauley/Documents/Projects/agentic-ai-dev-template/.agents/skills/explain-diff-merge-review/scripts")
import flag_sensitive_paths as fsp

positive = [
    "db/migrations/0001_init.sql",
    ".env.production",
    "src/auth/login.py",
    "stacks/react/bun.lock",
    ".github/workflows/ci.yml",
    ".github/repo-settings/ruleset.json",
]
negative = [
    "README.md",
    "src/app.py",
    "docs/notes.md",
    "stacks/react/src/App.tsx",
]

matches = fsp.flag(positive + negative)
matched_paths = {path for _category, path in matches}

for p in positive:
    assert p in matched_paths, f"expected {p!r} to be flagged, it wasn't"
for p in negative:
    assert p not in matched_paths, f"expected {p!r} to NOT be flagged, it was"

categories = dict(matches)
assert categories["db/migrations/0001_init.sql"] == "migrations/schema"
assert categories[".env.production"] == "secrets/env"
assert categories["src/auth/login.py"] == "auth/security/permissions"
assert categories["stacks/react/bun.lock"] == "dependency lockfiles"
assert categories[".github/workflows/ci.yml"] == "CI/CD & infra"
assert categories[".github/repo-settings/ruleset.json"] == "repo settings"

print(f"OK: {len(positive)} positive matches, {len(negative)} negative non-matches, all categories correct")
EOF
python3 /tmp/fsp-test/test_flag.py
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 /tmp/fsp-test/test_flag.py`
Expected: `ModuleNotFoundError: No module named 'flag_sensitive_paths'` (the file doesn't exist yet — this is the right failure).

- [ ] **Step 3: Write the minimal implementation**

```python
#!/usr/bin/env python3
"""
flag_sensitive_paths.py — flag changed file paths that fall into
merge-sensitive categories, for the explain-diff-merge-review skill's
"Pre-merge double-check" section.

Usage:
    git diff --name-only <base>..HEAD | uv run python scripts/flag_sensitive_paths.py
    uv run python scripts/flag_sensitive_paths.py path/one path/two ...

Reads changed file paths from stdin (one per line) if no paths are given as
command-line arguments; otherwise uses the given arguments. Prints one
"category: path" line per match — a path matching multiple categories prints
once per matching category. This is an informational scan, not a gate: it
always exits 0, whether or not anything matched.
"""
import re
import sys

CATEGORIES = [
    ("migrations/schema", [
        r"(^|/)migrations/",
        r"(^|/)alembic/",
        r"(^|/)db/migrate/",
        r"\.sql$",
    ]),
    ("secrets/env", [
        r"(^|/)\.env(\..*)?$",
        r"secret",
        r"credential",
        r"password",
    ]),
    ("auth/security/permissions", [
        r"auth",
        r"security",
        r"permission",
        r"acl",
    ]),
    ("dependency lockfiles", [
        r"\.lock$",
        r"(^|/)package-lock\.json$",
        r"requirements.*\.txt$",
        r"(^|/)go\.sum$",
    ]),
    ("CI/CD & infra", [
        r"(^|/)\.github/workflows/",
        r"(^|/)Dockerfile",
        r"(^|/)docker-compose",
        r"\.tf$",
    ]),
    ("repo settings", [
        r"(^|/)\.github/repo-settings/",
        r"(^|/)CODEOWNERS$",
    ]),
]

_COMPILED = [
    (name, [re.compile(p, re.IGNORECASE) for p in patterns])
    for name, patterns in CATEGORIES
]


def flag(paths):
    """Return a list of (category, path) for every path matching a category's
    patterns, in input order. A path matching multiple categories appears once
    per matching category."""
    matches = []
    for path in paths:
        for category, patterns in _COMPILED:
            if any(p.search(path) for p in patterns):
                matches.append((category, path))
    return matches


def main():
    paths = sys.argv[1:]
    if not paths:
        paths = [line.strip() for line in sys.stdin if line.strip()]

    matches = flag(paths)
    for category, path in matches:
        print(f"{category}: {path}")

    if not matches:
        print("no sensitive paths flagged", file=sys.stderr)


if __name__ == "__main__":
    main()
```

Save this as `.agents/skills/explain-diff-merge-review/scripts/flag_sensitive_paths.py`.

- [ ] **Step 4: Make it executable and run the test to verify it passes**

```bash
chmod +x .agents/skills/explain-diff-merge-review/scripts/flag_sensitive_paths.py
python3 /tmp/fsp-test/test_flag.py
```

Expected: `OK: 6 positive matches, 4 negative non-matches, all categories correct`

- [ ] **Step 5: Verify the CLI itself (both argv and stdin modes)**

```bash
cd .agents/skills/explain-diff-merge-review
uv run python scripts/flag_sensitive_paths.py .env.production stacks/react/bun.lock README.md
printf '.github/workflows/ci.yml\nsrc/app.py\n' | uv run python scripts/flag_sensitive_paths.py
cd -
```

Expected first command: two lines printed (`secrets/env: .env.production` and `dependency lockfiles: stacks/react/bun.lock`); `README.md` produces no line. Expected second command: one line printed (`CI/CD & infra: .github/workflows/ci.yml`); `src/app.py` produces no line.

- [ ] **Step 6: Clean up the test file and commit**

```bash
rm -rf /tmp/fsp-test
git add .agents/skills/explain-diff-merge-review/scripts/flag_sensitive_paths.py
git commit -m "feat: add flag_sensitive_paths.py for pre-merge double-check"
git push
```

---

### Task 3: Write `SKILL.md`

**Files:**
- Create: `.agents/skills/explain-diff-merge-review/SKILL.md`

**Interfaces:**
- Consumes: `scripts/render.py` (Task 1) and `scripts/flag_sensitive_paths.py` (Task 2) — referenced by path and by name.
- Produces: the skill's frontmatter `name: explain-diff-merge-review` and `description`, which Task 5's validator checks.

- [ ] **Step 1: Write the file**

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add .agents/skills/explain-diff-merge-review/SKILL.md
git commit -m "feat: add explain-diff-merge-review skill instructions"
git push
```

---

### Task 4: Edit `explain-diff`'s description for trigger disambiguation

**Files:**
- Modify: `.agents/skills/explain-diff/SKILL.md:3-10` (the frontmatter `description` field)

**Interfaces:**
- Consumes: nothing from other tasks (independent of Tasks 1-3).
- Produces: an updated `description` that Task 5's validator re-checks against the same frontmatter constraints as before (length, no XML tags).

- [ ] **Step 1: Make the edit**

In `.agents/skills/explain-diff/SKILL.md`, replace:

```yaml
description: |
  Produce a rich, self-contained HTML explanation of a code change, diff,
  branch, or PR — background context, intuition, a code walkthrough, an
  explicit account of judgment calls the implementer made, and a
  comprehension quiz. Use when asked to explain a diff, PR, or branch,
  especially one an AI coding agent wrote from instructions and feedback,
  where a human reviewer needs to verify the implementation matches intent
  before merging.
```

with:

```yaml
description: |
  Produce a rich, self-contained HTML explanation of a code change, diff,
  branch, or PR — background context, intuition, a code walkthrough, an
  explicit account of judgment calls the implementer made, and a
  comprehension quiz. Use for a cold or general diff/PR/branch explanation —
  reviewing someone else's work, revisiting an old change, or a fresh session
  with no memory of how the change came about. For a same-session pre-merge
  check of a PR this session itself just wrote, use explain-diff-merge-review
  instead.
```

- [ ] **Step 2: Verify the frontmatter still parses and the description still meets constraints**

```bash
python3 .agents/skills/review-skill/scripts/validate.py .agents/skills/explain-diff
rm -f .agents/skills/explain-diff/.validation-result.json
```

Expected: `0 failed` in the results line (same as before this edit — this change only touches prose within an already-valid field).

- [ ] **Step 3: Commit**

```bash
git add .agents/skills/explain-diff/SKILL.md
git commit -m "docs: hand off same-session pre-merge case to explain-diff-merge-review"
git push
```

---

### Task 5: Validate the new skill structurally

**Files:**
- Read: `.agents/skills/explain-diff-merge-review/` (target)
- Read: `.agents/skills/review-skill/scripts/validate.py` (validator, unmodified)

**Interfaces:**
- Consumes: the completed `.agents/skills/explain-diff-merge-review/` directory from Tasks 1-3.
- Produces: a passing (`0` failures) validator run, gating Task 6.

- [ ] **Step 1: Run the validator**

```bash
python3 .agents/skills/review-skill/scripts/validate.py .agents/skills/explain-diff-merge-review
```

Expected: `Results: N passed, 0 failed, ...`. Warnings are acceptable if justified (e.g. the "no `references/` directory" check is a SKIP, matching `explain-diff`'s own result) — re-read any WARN and decide whether it's worth fixing.

- [ ] **Step 2: Fix any FAIL results**

If the exit code is `1`, read the printed `FAILURES:` list, fix each item in `SKILL.md` or the directory structure, and re-run Step 1 until it exits `0`.

- [ ] **Step 3: Remove the generated result file from version control**

```bash
rm -f .agents/skills/explain-diff-merge-review/.validation-result.json
git status
```

Expected: `.validation-result.json` does not appear in `git status`.

- [ ] **Step 4: Commit only if Step 2 required file changes**

If Step 2 changed any tracked file:

```bash
git add .agents/skills/explain-diff-merge-review/SKILL.md
git commit -m "fix: address explain-diff-merge-review skill validator findings"
git push
```

If no fixes were needed, skip this step (nothing to commit).

---

### Task 6: Dry-run both scripts end-to-end

**Files:**
- Create (temporary, not committed): `/tmp/merge-review-sample-spec.json`

**Interfaces:**
- Consumes: `scripts/render.py` (Task 1) and `scripts/flag_sensitive_paths.py` (Task 2).
- Produces: a rendered HTML file proving both scripts work together as `SKILL.md` instructs; this is the final gate before opening the PR.

- [ ] **Step 1: Run the flagging script against a realistic changed-file list**

```bash
cd .agents/skills/explain-diff-merge-review
printf 'docs/superpowers/specs/2026-08-11-explain-diff-merge-review-design.md\n.agents/skills/explain-diff-merge-review/scripts/render.py\nstacks/react/bun.lock\n.github/workflows/ci.yml\n' | uv run python scripts/flag_sensitive_paths.py
```

Expected output (order may vary, both lines must appear):
```
dependency lockfiles: stacks/react/bun.lock
CI/CD & infra: .github/workflows/ci.yml
```
The two doc/script paths must NOT appear in the output.

- [ ] **Step 2: Write a sample spec exercising every feature, including the flagged paths from Step 1**

```bash
cat > /tmp/merge-review-sample-spec.json << 'EOF'
{
  "title": "Sample: add per-tenant rate limits",
  "subtitle": "Dry run for the explain-diff-merge-review skill",
  "slug": "sample-rate-limits",
  "sections": [
    {"id": "ask", "heading": "What you asked for", "html": "<p>Add a rate limit per tenant on the ingest endpoint.</p>"},
    {"id": "changed", "heading": "What changed and why", "html": "<p>A token-bucket limiter now sits in front of the ingest handler.</p>"},
    {"id": "judgment-calls", "heading": "Where Claude made a call", "html": "<div class=\"callout flag\">The ask didn't specify an algorithm; token bucket was chosen over fixed window because the ask mentioned bursty traffic.</div>"},
    {"id": "code", "heading": "Code walkthrough", "html": "<pre>bucket.consume(tenant_id, cost=1)</pre>"},
    {"id": "double-check", "heading": "Pre-merge double-check", "html": "<div class=\"callout flag\">flagged: stacks/react/bun.lock (dependency lockfiles); .github/workflows/ci.yml (CI/CD & infra)</div>"}
  ],
  "quiz": [
    {
      "question": "Why was a token bucket chosen over a fixed window for the rate limiter?",
      "options": [
        {"text": "Fixed windows aren't supported by the library in use.", "correct": false},
        {"text": "The ask specifically called out bursty traffic, which fixed windows handle poorly.", "correct": true}
      ]
    }
  ]
}
EOF
```

- [ ] **Step 3: Render it**

```bash
uv run python scripts/render.py /tmp/merge-review-sample-spec.json -o /tmp/merge-review-sample-output.html
```

Expected: prints `/tmp/merge-review-sample-output.html`, exits 0, no traceback.

- [ ] **Step 4: Verify the output**

```bash
grep -c "callout flag" /tmp/merge-review-sample-output.html
python3 -c "import html.parser; html.parser.HTMLParser().feed(open('/tmp/merge-review-sample-output.html').read()); print('parses OK')"
python3 -c "
content = open('/tmp/merge-review-sample-output.html').read()
assert '<script>' not in content, 'no JS expected'
assert 'type=\"radio\"' in content, 'quiz should use radio inputs, not a <script>-driven click handler'
print('no-JS quiz structure confirmed')
"
cd -
```

Expected: `grep -c` returns `2` or more; `parses OK` prints with no exception; `no-JS quiz structure confirmed` prints with no assertion error.

- [ ] **Step 5: Clean up the temporary files**

```bash
rm -f /tmp/merge-review-sample-spec.json /tmp/merge-review-sample-output.html
```

(Nothing to commit — these files were never inside the repo.)

---

### Task 7: Open the pull request

**Files:** none (git/gh operations only)

**Interfaces:**
- Consumes: all prior tasks' commits on `feat/24-explain-diff-merge-review`.
- Produces: an open PR closing issue #24.

- [ ] **Step 1: Confirm the branch is clean and pushed**

```bash
git status
git log --oneline origin/main..HEAD
```

Expected: working tree clean; the log shows exactly this plan's commits (from Tasks 1-4, plus Task 5 only if it required a fix commit).

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "Add explain-diff-merge-review skill" --body "$(cat <<'EOF'
Adds `.agents/skills/explain-diff-merge-review/`, a sibling to the
already-merged `explain-diff` skill, restructured around one decision:
should this session's own PR be merged? Per the design in
docs/superpowers/specs/2026-08-11-explain-diff-merge-review-design.md.

Also edits `explain-diff/SKILL.md`'s description to hand off the
same-session pre-merge case to this new skill, avoiding trigger ambiguity
between the two.

Closes #24
EOF
)"
```

- [ ] **Step 3: Report the PR URL**

`gh pr create` prints the PR URL — confirm it opened successfully and share it.
