# Design: `explain-diff` skill (Approach A)

- **Issue:** #22
- **Status:** Approved by user, ready for implementation

## Background

`explain-diff` originates from a public gist by
[ankitg12](https://gist.github.com/ankitg12/8e808d387799de4e9839bc393f8e6405)
(forked for credit to
[emigre459](https://gist.github.com/emigre459/9b092988e204b38051e45558232a3cee)),
building on a rendering recipe from
[geoffreylitt](https://gist.github.com/geoffreylitt/a29df1b5f9865506e8952488eac3d524).
It's a Claude Code command that turns a code diff/PR into a rich, self-contained
HTML explainer (background, intuition, code walkthrough, quiz), rendered via a
bundled `render.py` template script so the LLM only has to author a small JSON
content spec rather than regenerate CSS/JS boilerplate each time.

Reviewed for security/prompt-injection risk before forking: no hidden
directives, no network calls, no `eval`/`exec`/`subprocess` in `render.py` — safe
to build on.

## Problem with the as-forked version

The gist's framing optimizes for "explain this diff like a textbook" — good for
onboarding to unfamiliar code, but not calibrated to the primary real use case:

> Claude Code writes a PR based on my instructions and feedback. Cursor Bugbot
> handles correctness review. I'm the only human in the loop, so I need to be
> fully aware of what we did and why before merging.

That use case needs verification that the implementation matches *intent*, and
visibility into any judgment calls the AI made unilaterally — not generic
reading comprehension. The gist's quiz and structure don't surface either.

## Scope: Approach A (this design)

Deliberately the smaller of two considered approaches. It stays close to the
original structure and remains useful even without session context (e.g.
reviewing a cold PR later, or one authored by someone/something else) —
unlike the fuller "merge-readiness restructure" (Approach B), which assumes
same-session conversation history and is being deferred to a separate skill
(`explain-diff-merge-review`) built after this one is merged and tested.
Approach B and porting the Notion variant are explicitly out of scope here.

## Design

### Placement

- `.agents/skills/explain-diff/SKILL.md` — canonical location per this repo's
  skill conventions (kebab-case dir, no `README.md`, portable frontmatter
  fields, skill-relative script paths per
  `.agents/rules/shared/harness-agnostic-skills.md`).
- `.agents/skills/explain-diff/scripts/render.py` — moved from gist root,
  referenced as `scripts/render.py` (skill-root-relative), executable.
- Credits to ankitg12 and geoffreylitt included directly in `SKILL.md`.

### Content changes vs. the gist original

1. **Intent reconstruction (new first step).** Before writing any section,
   reconstruct what was actually asked: primarily from the current session's
   conversation/instruction/feedback history. If that's thin or absent
   (cold/fresh session, PR authored by someone else), fall back to the PR
   description, commit messages, and linked issue. If none of those give
   usable intent context either, say so plainly rather than guessing.

2. **New section: "Judgment Calls & Assumptions"** (inserted after Intuition,
   before Code walkthrough). Lists anything the implementer (Claude) decided
   on the user's behalf: ambiguous instructions resolved a particular way,
   scope added beyond the literal ask, or requested things not done (and why).
   **Never silently omitted** — if nothing notable is found, the section says
   so explicitly ("No deviations from the request were found"), so its
   absence never reads as "the check didn't run."

3. **Visual treatment.** One additive CSS rule in `render.py`: a
   `.callout.flag` modifier (distinct accent color) for judgment-call
   callouts specifically, so they can't be skimmed past. No structural
   change to the renderer — the existing generic `sections` JSON schema
   already supports arbitrary section content; this only adds a CSS class
   option.

4. **Quiz reframing.** At least 1-2 of the 5 questions must target a
   judgment call or non-obvious trade-off when any exist ("could you defend
   this decision"), not just fact recall. Remaining questions stay as
   general comprehension checks, as in the original.

5. Everything else — Background, Intuition, Code walkthrough sections,
   Kleppmann-style prose guidance, diagram-family tips, "no ASCII diagrams" —
   carries over unchanged from the gist.

### Validation plan

- Run `.agents/skills/review-skill/scripts/validate.py` against
  `.agents/skills/explain-diff/` and fix any structural findings.
- Dry-run `render.py` against a small sample spec JSON to confirm it still
  produces valid HTML (including the new `.callout.flag` style).
- Manual read-through of `SKILL.md` against `review-skill`'s qualitative
  checklist (description format, instruction clarity, progressive
  disclosure).

### Distribution

- PR into `emigre459/agentic-ai-dev-template`, closing issue #22.
- The gist fork stays as-is (archival credit record); no further gist edits.
- **Follow-up, after merge and some real-world use:** if/when this skill (or
  its Approach-B successor) is submitted to Vercel's public skills package,
  credit ankitg12 and geoffreylitt in that submission too.

## Out of scope

- Approach B (`explain-diff-merge-review`): full merge-readiness restructure
  — intent-first framing, judgment calls as the centerpiece, quiz testing
  decision-defense specifically, and an auto-generated pre-merge
  double-check callout. Deferred to a follow-up skill once A is proven out.
- Porting `explain-diff-notion.md`.
