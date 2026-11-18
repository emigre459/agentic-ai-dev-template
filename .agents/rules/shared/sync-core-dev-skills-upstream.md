---
description: Vendored dev/workflow skills under .agents/skills/ are read-only outside the template that publishes them. Customize them through repo-owned extension points, and send changes to a skill itself upstream as a PR to the template. Does NOT apply to a repo's own runtime/domain/product skills.
appliesTo:
  - ".agents/skills/**"
  - ".claude/skills/**"
---

# Vendored skills are read-only; changes go upstream

A vendored skill is consumed from the template, pinned in `skills-lock.json`, and
replaced on update. **Stop before editing one** and send the change upstream as a PR
to the template. Repo-specific needs go in the extension points instead:
`.agents/skill-config/<skill>.md`, `AGENTS.md`, `.agents/rules/` and
`.github/pr-reviewer.json`.

The dev-workflow skills under `.agents/skills/` (`build-from-issue`, `plan-issues`,
`resolve-pr-concerns`, `review-skill`, `upgrade-deps` and their siblings; each one's
`SKILL.md` opens with a `**Repo config first:**` preflight) are published by one
canonical template repo. Claude Code reads the same folders through the
`.claude/skills` symlink, so the rule covers both paths. Every other repo **vendors**
them: installed with `npx skills add`, or copied in by creating the repo from the
template with `make init`. Either way, `npx skills update` or the next template sync
deletes and recreates each skill folder, so an edit inside one is silently lost.

## Which repo am I in?

- **The template** (`scripts/init_template.py` exists at the repo root; `make init`
  deletes it, so a fork of the template made to send PRs upstream counts too): this
  is the skills' canonical home. Edit them in place, with the usual tests and review.
  The template pins its skills in `.github/template-skills-lock.json` (`make
  skills_lock`), not a root `skills-lock.json`, which would hide them from `npx
  skills add`; `make init` turns it into the new repo's `skills-lock.json`.
- **Any other repo**, including one created from the template: vendored skill
  folders are **read-only**. Never edit, add or delete files inside them, and never
  let another skill (such as `review-skill`) do it for you.

## How a repo customizes a vendored skill

Repo-specific behavior goes in files the repo owns:

| Extension point | Use it for |
|-----------------|------------|
| `AGENTS.md` | Always-on, repo-wide instructions |
| `.agents/rules/` | Shared rules routed from `AGENTS.md` |
| `.github/pr-reviewer.json` | Structured settings: the active PR reviewer and its adapter (including a repo-owned `notes` file), review-round caps |
| `.agents/skill-config/<skill>.md` | Optional notes for one skill, read by that skill's preflight |

Precedence, highest first: the user's instructions this session, then `AGENTS.md`
and `.agents/rules/`, then `.agents/skill-config/<skill>.md`, then the skill's
defaults. **No file can lift a skill's hard stops** (merging without the user's
one-off OK, resolving review threads, opening or calling ready a PR without the
pre-PR check `pr-check-scope` requires (the full `make pr_check` unless the diff
qualifies for the light check) and green CI, weakening a gate, deleting issues,
skipping a confirmation, installing tools, posting to an outside channel unasked,
widening access, or sending output outside the repo). Only the user can,
explicitly, in the session. The README's "Customizing the skills in your repo"
section has an example of each.

## When a skill itself needs to change

If the change would help every repo (a bug, a better step, a new reviewer adapter
worth bundling), **suggest an upstream PR to the canonical template**. Name the
skill and the change, and offer to open the PR. This is a suggestion, not an
auto-do: don't open or push cross-repo PRs without the user's go-ahead. The
template's copy is generalized and harness-agnostic, so the upstream change usually
needs a hand-reconciled diff, not a verbatim copy. Until it lands, put what this
repo needs in its extension points.

## Which skills are vendored

A skill is **vendored** if its `SKILL.md` carries the `**Repo config first:**`
preflight or `skills-lock.json` lists it (a third-party skill installed with
`npx skills add` has no preflight but is replaced on update all the same). Every
other skill is the repo's own runtime/domain/product code: edit it like any other
file.
