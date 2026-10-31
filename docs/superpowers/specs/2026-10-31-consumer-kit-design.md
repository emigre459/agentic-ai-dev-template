# Consumer kit: pinned skills, nightly update PR, drift check, releases — design

Issue: #49 (epic #51). Status: proposed. Builds on #48 (PR #58); the stack is built
after #58 merges.

## Intent

Epic #51 publishes this template's dev-workflow skills as an installable source. #48
made vendored skill folders read-only and gave repos owned extension points. This card
gives repos the rest:
- a one-command install pinned to a release;
- an update path that arrives the way Dependabot updates do;
- a check that catches local edits to vendored skills;
- a simple way to cut releases.

Repos created from the template get all of it on day one, for the template's skills
and for their own repo.

The user's picture of success: "I should get in my consumer repo a depbot-like PR,
fully CI'ed and ready for merge, that has the new skills in it". That covers every
skill the repo installed with the `skills` CLI, not only this template's (one of the
user's repos also installs langfuse's skill). The agent proposes releases when functionality changed
meaningfully. Every merged PR counts as a patch unless a label says minor or major. The
agent drafts at most one paragraph describing each release.

## Facts this design rests on (verified 2026-10-30/31)

- **`skills-lock.json` (project lock) entries** hold `source`, `ref` (branch or tag),
  `sourceType`, `skillPath` and `computedHash`. Source: vercel-labs/skills
  `src/local-lock.ts`.
- **`computedHash`** is SHA-256 over every regular file in the skill folder:
  - files are sorted by relative path with JavaScript `localeCompare`;
  - each file contributes its relative POSIX path, then its bytes;
  - `.git` and `node_modules` are skipped, and symlinks are not followed;
  - `__pycache__` is **not** skipped, so the drift check must run on a clean checkout.
- **`skills update` never moves an entry off its `ref`.** A tag-pinned entry is
  re-checked against that same tag, so it never updates on its own. A branch-tracking
  entry (or one with no ref) follows its branch (`src/update.ts`).
- `npx skills add <url>/tree/<ref>` and `owner/repo#<ref>` install at a ref and record
  it. A tree URL for this repo lists all 10 skills.
- `make init` doesn't touch `.agents/skills/`, so a lock the template ships stays valid
  in a new repo.
- **release-drafter** (3.9k★, maintained) keeps a draft GitHub Release current on every
  push to `main`:
  - its `version-resolver` reads `major` / `minor` / `patch` labels on the merged PRs;
    the highest wins, and the default is patch;
  - with `publish: true` it publishes the release and creates the tag;
  - it never commits to `main` and works with `GITHUB_TOKEN`.
- **A PR opened with `GITHUB_TOKEN` triggers no `pull_request` workflows.** Tags and
  releases created with it trigger no other workflows either.
- `CHANGELOG.md` has been frozen since 2026-09-28. References to it remain in
  `tests/template/test_root_hygiene.py`, `.agents/skills/build-from-issue/SKILL.md`
  (Step 11) and `.github/review-guidelines.md`.

## Prior art: a downstream repo's nightly skills PR (read 2026-10-31)

One of the user's repos already runs a nightly skills-update PR on the same CLI. It is
battle-tested, so L2 ports it (org-neutral, no downstream names or issue numbers)
rather than designing from scratch.

**The workflow** (`skills-update.yml`):
- runs `npx -y skills@latest update -p -y` on Node 22 (current CLIs need Node >= 22.20);
- rebuilds a fixed branch on the latest `main` before updating;
- copies its helper script outside the work tree before switching branches.

**The helper** (`skills_update_pr.sh`), all idempotent:
- pushes only when the skills tree or the base changed;
- finds the open PR with `gh pr list --head` (`gh pr view` misbehaves in Actions);
- adds labels after the PR exists, on a best-effort basis;
- dispatches `ci.yml` on the branch when a commit landed or the PR is new, because a
  `GITHUB_TOKEN` push parks the PR's own CI in `action_required`;
- has a test seam (`GH_CMD`) so it can be tested with no network.

**What it doesn't have:** its entries follow a branch, so it never bumps a tag pin; it
has no App/PAT token path; and it has no local-edit drift check (its separate check is
for freshness against upstream).

## Decisions (the user's)

1. Deliver as a **4-layer `gh stack`**, so Iris gets stacked-PR review experience.
   Build it after PR #58 merges.
2. **Releases are cut by hand**, through a workflow a human (or the agent, with the
   human's OK) starts. They are not automatic on merge.
3. **Bump rule:** every merged PR is a patch unless labeled `minor` or `major`. A release
   takes the highest label since the last release (release-drafter's resolver).
4. **Release notes:** a GitHub Release per tag, with a human-approved paragraph of at
   most one paragraph on top and release-drafter's merged-PR list below.
   **Delete `CHANGELOG.md`** and every reference to it.
5. **Release tooling ships to repos made from the template**, so they can cut their own
   releases the same way.
6. **`build-from-issue` checks release recency at the start of a build** and suggests
   whether finishing the card warrants a bump, and which.
7. **Nightly PR CI:**
   - when the repo sets a GitHub App or fine-grained PAT secret, use it, so the PR runs
     every check a human's PR would;
   - otherwise open the PR with `GITHUB_TOKEN` and dispatch CI on the branch, with the
     PR body saying which PR-only checks didn't run.
8. **The update PR covers every lock entry**, from this template or any other source.
   It never merges itself.
9. **Use release-drafter rather than building our own**, chosen over tagpr,
   release-please, semantic-release and changesets: no Conventional Commits, no release
   PR to get through the ruleset, and no `CHANGELOG.md`.
10. **New repos start exactly on the newest release**, never on unreleased `main`
    content:
    - the template's lock describes the newest release;
    - `make init` swaps the skill folders for that tag's content;
    - after each release, the `release` skill opens a small "re-pin" PR.

    This rules out the silent downgrade and the mislabeled update PR that a lock pinned
    to an older tag would cause.
11. **Delivered as reusable workflows.** Each repo's workflows are short callers that
    `uses: emigre459/agentic-ai-dev-template/.github/workflows/<name>.yml@vX.Y.Z`, so the
    logic lives here, and Dependabot's `github-actions` ecosystem bumps the pinned tag.
    Repos made from the template ship those callers. The README documents how an
    existing repo adopts the kit. Migrating the user's own existing repos is out of
    scope: separate cards.

## Goals, by layer

### L1 — The template ships a lock; a drift check guards vendored skills

1. A root `skills-lock.json` lists the template's skills (11 once L4 adds `release`).
   Each entry has:
   - `source: "emigre459/agentic-ai-dev-template"`, `sourceType: "github"`;
   - `skillPath: ".agents/skills/<name>/SKILL.md"`;
   - `ref`: the newest `v*` release tag;
   - `computedHash`: the hash of that skill's folder **at that tag**.

   Between releases, `main`'s folders are ahead of the lock; that is expected in the
   template. Before the first release exists, entries have no `ref`, and their hashes
   are those of the working tree. That is a transitional state until the first re-pin
   PR.
2. **`make init` starts a new repo on the release.** For each lock entry with a `ref`,
   it replaces `.agents/skills/<name>/` with that tag's folder, fetched with plain git
   from the template's GitHub URL (no Node needed). Then it checks the result against
   `computedHash`. Network failure, or a hash mismatch, fails init loudly.
3. A stdlib Node script reproduces the CLI's
   `computedHash` exactly (same algorithm, same `localeCompare` sort; credit
   vercel-labs/skills, MIT). Modes:
   - `check`: for every lock entry, find the installed folder and fail when its hash
     differs, naming each skill that drifted and what to do;
   - `write`: regenerate the template's own entries from a given tag's content. Template
     only; `make skills_lock` wraps it, and the re-pin PR uses it.

   Its location is the builder's call (see below). It must reach callers through the
   reusable workflow at the same ref as that workflow.
4. **A drift-check reusable workflow**, called by a CI job that runs on `pull_request`,
   `push` to `main` and `merge_group`, and is a **required** check in
   `.github/repo-settings/ruleset.json` (final phase).
   - In consuming repos, it compares each installed folder with `computedHash`. A
     failure says to revert the edit, or move it to `.agents/skill-config/`.
   - In the template (`scripts/init_template.py` present), it checks each entry against
     its tag's content instead, proving the shipped lock is accurate. Main's own edits
     aren't drift there.
5. The script finds each installed folder in the canonical `.agents/skills/<name>/`
   first, then the harness dirs (`.claude/skills/<name>/`). An entry whose folder exists
   nowhere fails as "missing".

### L2 — The nightly update PR (a reusable workflow plus a short caller)

A port of the prior art above, extended with the tag-pin bump and the token path. The
caller runs on a schedule (nightly) and on `workflow_dispatch`. It does nothing in the template
itself (`scripts/init_template.py` present): there the skills are the source, and
re-installing a tag would wipe unreleased work. Each run:
1. Rebuild branch `skills-update` from the latest `main`.
2. For each **tag-pinned** entry, look up its source's newest semver release tag. If it
   is newer, reinstall that entry's skills at the new tag with the `skills` CLI, which
   rewrites the folder and the lock.
3. Run `npx -y skills@latest update -p -y` for the **branch-tracking** entries, without
   touching tag-pinned ones.
3a. **New or removed skills at a newer tag:**
    - the PR updates the skills the repo already has;
    - it lists newly published skills as "available, not installed" in its body;
    - it keeps a skill that disappeared upstream and flags it.

    Nothing is added or dropped silently.
4. If nothing changed, close any open update PR with a comment and stop. Otherwise
   force-push the branch and open, or refresh, **one** PR. Its body lists each changed
   skill with its old and new ref, and links each source's release notes when it has
   any.
5. **Token:**
   - if `SKILLS_UPDATE_APP_ID` + `SKILLS_UPDATE_APP_PRIVATE_KEY` (a GitHub App) or
     `SKILLS_UPDATE_TOKEN` (a fine-grained PAT) is set, push and open the PR with it,
     so normal PR CI runs;
   - otherwise use `GITHUB_TOKEN` and `gh workflow run` CI on the branch (`ci.yml`
     gains `workflow_dispatch`), and the PR body says that PR-only checks such as
     `review-gate` didn't run.
6. Set `DISABLE_TELEMETRY=1`. Use least-privilege `permissions:`.

### L3 — Release infrastructure

1. **`.github/release-drafter.yml`:**
   - `version-resolver`: `major` / `minor` labels, `patch` by default;
   - `tag-template: v$RESOLVED_VERSION`, `name-template: v$RESOLVED_VERSION`;
   - a body of `$CHANGES` (the merged-PR list).
2. **`.github/workflows/release-drafter.yml`** runs on `push` to `main` (never
   `merge_group`) and keeps the draft current.
3. **`.github/workflows/release.yml`** runs on `workflow_dispatch` with a required
   `summary` input. It runs release-drafter with `publish: true` and the summary as the
   release `header`, which creates `vX.Y.Z` and the published release.
4. **Labels.** `minor` and `major` (and `patch`, for explicitness) are created in the
   repo-settings bootstrap phase, so new repos have them. The template repo gets them
   the same way.
5. **Delete `CHANGELOG.md`.** Remove it from `test_root_hygiene.py`'s list, reword
   `build-from-issue` Step 11's "no changelog step" note to point at Releases, and drop
   it from `review-guidelines.md`'s list.

### L4 — The agent side and docs

1. **`build-from-issue`:**
   - at the start of a build, alongside the Step 2 read-back, report the newest release
     (`gh release list`), how long ago it was, and how many PRs have merged since;
   - suggest the bump this card implies (patch, minor or major, with one line of
     reasoning, and a no-release case such as docs-only), and let the user correct it;
   - at Step 11, label the PR `minor` / `major` when that's the agreed bump (patch: no
     label). On a stack, label the top layer.
   - No release yet: say so, and suggest the first.
2. **A new `release` skill** (`.agents/skills/release/`, with the #48 preflight line):
   1. read the draft release (version, merged PRs);
   2. draft **at most one paragraph** on what the release delivers;
   3. show the version and the paragraph, and get the user's explicit OK. Publishing is
      outward-facing, and consumer repos pick it up that night. To change the version,
      relabel PRs and let the draft re-resolve;
   4. run `release.yml` with the summary;
   5. verify that the tag and the published release exist, and report the links.

   It never publishes without the user's OK for that release.
3. **README:**
   - install pinned to a tag;
   - updates (the nightly PR and the token setup);
   - the drift check and what to do when it fails;
   - cutting a release, for this template and for your own repo;
   - telemetry opt-out (`DISABLE_TELEMETRY=1`).
4. **`AGENTS.md`:** one pointer to the `release` skill and the bump labels.
5. **A human-reviewer runbook** (as in #48), for Iris:
   `docs/superpowers/test-kits/2026-10-31-consumer-kit-live-e2e-kit.md`. It covers:
   - the drift check failing on a local edit;
   - a nightly update PR in a scratch repo, without a token;
   - cutting a test release in a scratch repo.

   It must never use `git pull` on a stack branch (`references/stacked-prs.md`).

## Non-goals

- Automatic releases on merge, or auto-merging update PRs.
- Changes to the `skills` CLI, or forking it.
- Publishing to the skills.sh directory.
- A structured per-skill config (still prose; #48).
- Hooks distribution (#50).

## Anti-goals (a failure even if it "works")

- An update PR that silently drops a non-template skill, or reverts a repo's own edits
  without the drift check having flagged them first.
- A drift check whose hash disagrees with the CLI's, which would fail on every untouched
  install.
- Publishing a release, or pushing a tag, without a human's OK.
- Any workflow that commits straight to `main`.
- A secret the workflows can't run without (each has a no-secret path).

## Calls left to the builder

- **Exact CLI invocations for reinstalling a tag-pinned entry**, so the folders stay
  where they were installed (`.agents/skills` canonical plus the harness links).
  Verified end to end in a scratch repo before it's relied on.
- **How "newest semver release tag" is found** (releases API or tags), and how
  prereleases are ignored.
- **Where the drift-check and update-PR logic lives,** so a reusable workflow runs the
  code from its own ref:
  - inline in the reusable workflow;
  - scripts checked out from this repo at the called ref;
  - or another mechanism that the tests cover.

  Also: the required-check name. It must run on `merge_group`.
- **The `release.yml` wait/verify mechanics** that the `release` skill uses.
- **The first template release**, likely `v0.1.0`, is cut by the user with the
  `release` skill after the stack lands, followed by its re-pin PR.
- **Layer boundaries** may shift a little, as long as each layer passes CI on its own
  and there are at most 4.
