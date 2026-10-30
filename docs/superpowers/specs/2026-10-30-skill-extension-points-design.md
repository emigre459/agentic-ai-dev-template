# Consumer-owned extension points for vendored skills — design

Issue: #48 (epic #51). Status: proposed.

## Intent

Epic #51 publishes this template's dev-workflow skills as an installable source:
`npx skills add emigre459/agentic-ai-dev-template`. Installed skills are updated with
`npx skills update`, which deletes each skill folder (`rm -rf`) and copies it in again.
It never looks for local edits. A repo-specific edit inside a vendored skill folder is
therefore silently lost on the next update.

So a consuming repo must be able to customize every skill **without editing any skill
folder**, and no skill may ever tell the agent to edit its own folder. Each customization
goes in a file the consuming repo owns.

## Prior art (researched 2026-10-29)

- No official mechanism. The `skills` CLI (vercel-labs/skills) has no override or
  preserve-local mode; a `--preserve-local` proposal is open as vercel-labs/skills#455.
  The Agent Skills spec has no override concept either. Claude Code's `skillOverrides`
  setting only changes a skill's visibility, not its content.
- The closest community convention is open-mercato/skills (255 stars, 42 skills). Each
  `SKILL.md` carries one literal line: "**ALWAYS check first:** Apply
  `.ai/skills/<name>/SKILL.md` when present; safety rules still win." `scripts/lint.sh`
  fails if the line is missing, deriving the expected path from the directory name.
  The override may change repo specifics, but "can never relax safety or quality rules,
  expand tool or network access, or redirect outputs". A directive that tries is skipped
  and reported. Repo-wide values live separately in `.ai/agentic.config.json`.
  oskargargas/og-skills (`og-skill-overrides`) reuses that path.
- This design follows the open-mercato shape: a preflight line in each skill, a lint
  gate, and a safety floor. It deliberately keeps this repo's own path,
  `.agents/skill-config/<skill>.md`, as an experiment (the user's call). The file is not
  named `SKILL.md`, so it can't be mistaken for a skill.

## Goals

1. **Four extension points, all owned by the consuming repo:**
   - `AGENTS.md`: always-on, repo-wide instructions.
   - `.agents/rules/`: shared rules routed from `AGENTS.md`.
   - `.github/pr-reviewer.json`: structured settings, such as the PR reviewer, review
     round caps and the test-speed budget.
   - `.agents/skill-config/<skill>.md`: optional per-skill notes, new in this change.
2. **A preflight line in every skill.** Each of the 10 `SKILL.md` files under
   `.agents/skills/` carries the same line, naming its own config path. It goes near the
   top of the body (after the H1, before the first workflow step), so a partial load
   still sees it. The line states the whole contract itself, because a consuming repo may
   have none of this repo's `.agents/rules/`.
3. **Precedence** (the user's call): the user's in-session instructions, then
   `AGENTS.md` and `.agents/rules/`, then `.agents/skill-config/<skill>.md`, then the
   skill's defaults.
4. **A fixed safety floor** (the user's call: "whichever is more conservative; fewer
   agent-accessible escape hatches"). No file the agent reads can loosen a skill's hard
   stops: not `skill-config`, not `AGENTS.md`, not a rule. Only the user can, explicitly
   and in the session, as one-off merge permission already works. A config line that
   tries is skipped, and the agent tells the user which line it skipped. The hard stops:
   - merging, or enqueuing into a merge queue, without the user's explicit one-off OK
     for that PR;
   - resolving review threads;
   - opening a PR, or claiming it ready, without a green `make pr_check` / CI;
   - weakening a gate: skipping or disabling a check, test, hook or lint rule, or
     allowlisting before trying the fix the skill prescribes;
   - deleting issues instead of closing them with a comment;
   - skipping a confirmation or approval step the skill requires;
   - installing CLI extensions or system tools on the user's machine;
   - posting to Slack or any other outside channel unless the user asked this session;
   - widening tool or network access, or redirecting where output goes (for example,
     pushing to another remote or posting PRs elsewhere).
5. **Reviewer notes leave the skill folder.** `resolve-pr-concerns` currently says that
   adding a reviewer means adding `references/reviewers/<key>.md` *inside the skill*,
   the one place a skill tells the agent to edit its own folder. Instead, each adapter
   in `.github/pr-reviewer.json` gets an optional `notes` field: a repo-relative path to
   a repo-owned notes file. Without it, the bundled `references/reviewers/<key>.md`
   applies. The config loaders reject a `notes` value that is not a string, is absolute,
   or contains `..`. The prose that tells the agent where to read a reviewer's quirks
   reads `notes` first. A reviewer worth sharing goes upstream as a PR to this repo.
6. **The sync rule is rewritten.** `.agents/rules/shared/sync-core-dev-skills-upstream.md`
   says: in a consuming repo, vendored skill folders are read-only; customize through
   the four extension points; a change to a skill itself goes upstream as a PR to this
   repo. In this template repo, skills are edited in place. The current line that "an
   inheriting repo's copy may carry repo-specific wording" goes.
7. **README.** A new section, "Customizing the skills in your repo", documents each
   extension point with a short, concrete example, plus the precedence order and the
   hard-stop floor.
8. **An enforcement test** (`tests/template/`, in the style of `test_no_org_names.py`):
   - every `.agents/skills/*/SKILL.md` contains the exact preflight line with its own
     directory name;
   - no file under `.agents/skills/` tells the agent to create or edit files in its own
     skill folder. This is a phrase-matching guard (for example "add a
     `references/…` file", "edit this skill"). It is honest about being a heuristic,
     and it must pass on the fixed tree and fail on the current line 60 wording.
9. **A human-reviewer runbook.** The user will hand the PR to a friend to review, so
   the PR ships a self-contained, copy-pasteable runbook that someone with no context
   can follow. It goes in `docs/superpowers/test-kits/2026-10-30-skill-extension-points-live-e2e-kit.md`,
   plain-ASCII code blocks. It walks through:
   - installing the skills from this PR's branch into a scratch repo with
     `npx skills add`;
   - adding an `.agents/skill-config/<skill>.md` and seeing an agent honor it;
   - a config line that tries to lift a hard stop, and seeing the agent refuse it and
     say so;
   - re-installing or updating the skills and confirming the repo's config survives
     while the skill folder is replaced;
   - a custom reviewer adapter with a `notes` path, and `reviewer_state.py` rejecting a
     `notes` path that escapes the repo;
   - running the template's own test suite.

   Each step states the expected result, what failure looks like and the first thing
   to check. A pass/fail summary sits at the end.

## Calls made from the user's "most conservative" preference (mine, for review)

The builder check raised these. Each answer follows "fewer agent-accessible escape
hatches" rather than a separate ruling from the user.

1. **Read-only everywhere except the canonical template.** A repo made from the
   template with `make init` holds plain copies of the skills, which no updater replaces
   until #49 ships. Those copies are still read-only: a later template sync or
   `skills update` would wipe local edits just the same. The sync rule names one
   exception: the canonical template repo itself. It is identified by
   `scripts/init_template.py` existing, which `make init` deletes, so a fork made to
   send PRs upstream also counts as the template.
2. **Reviewer `notes` add to the bundled notes; they never replace them.** For a
   reviewer with a bundled `references/reviewers/<key>.md`, the agent reads the bundled
   file and then the repo's `notes`. Repo notes win on repo specifics but can't lift a
   hard stop. For example, the bundled list of commands the agent must never post still
   applies. A reviewer with no bundled file uses the repo's `notes` alone.
3. **No skill edits any vendored skill folder, not just its own.** `review-skill` Step 5
   ("offer to fix … make the edits") gains a branch: outside the canonical template it
   offers an `.agents/skill-config/<skill>.md` entry or an upstream PR, never an in-place
   edit. The enforcement test's phrase guard covers all skills, not just a skill's own
   folder.
4. **The gate stays `make pr_check`.** No file may substitute another command; that
   would be a path around the floor. The skills already assume `make pr_check`
   everywhere, so a repo without it is outside what they support today. The floor's
   wording says "green `make pr_check` and CI".
5. **"Redirecting output" means destinations outside the repo:** another git remote,
   another GitHub repo, an outside service, or a path outside the worktree. Paths
   inside the repo, such as "write specs under `docs/design/`", are repo specifics a
   config may set. The README and runbook use an in-repo path change as the "honored"
   example.
6. **Placement in `pr-check` and `run-tests`**, which have no H1: the preflight line
   goes right after the frontmatter.

## Non-goals

- Shipping a consuming-repo kit: tagged releases, the nightly update PR, the drift check
  for local edits, and the template consuming its own skills are #49.
- Hooks that inject the config at skill load (og-skills' approach). The preflight line
  is harness-neutral and needs no hooks.
- A structured per-skill config format (YAML/JSON). Per-skill notes are prose; structured
  values stay in `.github/pr-reviewer.json`.
- Making `flag_sensitive_paths.py` or other bundled scripts read `skill-config`. A repo
  that wants extra sensitive paths writes them in prose in the skill's config file.
- Changing the bundled reviewer adapters' content.

## Anti-goals (a failure even if it "works")

- Any path by which a file the agent reads can lift a hard stop.
- A preflight line that differs between skills except for the skill name, so the lint
  can't hold it exact.
- A skill or reference that still tells the agent to edit its own folder.
- A runbook that only works for someone who sat in this session.

## Calls left to the builder

- The exact wording of the preflight line (one line, roughly open-mercato's length, and
  it must state: the path, apply when present, precedence, the hard-stop floor, and
  skip-and-report).
- Whether the hard-stop list lives in the preflight line or in one sentence plus a
  bundled `references/` file per skill. Skills install one at a time, so a shared file
  outside the skill folder can't be relied on.
- The test's phrase list, and where the `notes` validation lives among the scripts.
  Every script that loads `.github/pr-reviewer.json` should agree.
- How the runbook installs from the PR branch (a GitHub tree URL or a local clone path),
  verified against the `skills` CLI docs.
