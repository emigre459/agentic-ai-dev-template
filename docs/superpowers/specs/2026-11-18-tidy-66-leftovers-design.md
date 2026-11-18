# Tidy the small leftovers from #66: design

Issue: #84 (parent epic #51). A small change: five independent tidy-ups, each with a
guard test. Ships in v0.2.0 with #82 and #83.

## Intent

#66's reviews left small inconsistencies that didn't block its merge. Downstream repos
are about to pin v0.2.0. These fixes should land before then, so the pinned skills
don't leave refs in a user's clone, name another repo's code, or carry slightly
different wording of the same rule.

**Non-goals:** any change to a skill's behavior beyond the five items below.

**Split out:** the React audit's `--ignore=GHSA-fc5q-39xc-vjpf`. No fixed `picomatch`
has shipped (npm `latest` is 4.0.7, and the advisory lists no patched version), so per
the card's 2026-11-17 amendment that item moves to its own card under #51, and this PR
closes #84.

## The changes

1. **`upgrade-deps/scripts/update_prs.py`, `_changes_main`.** It now fetches each PR head
   into `refs/upgrade-deps/pr-<N>` instead of `refs/remotes/origin/pr-<N>`. A
   `try/finally` deletes the ref with `git update-ref -d`, including when `merge-tree`
   fails. A private namespace keeps the ref out of `git branch -r`, out of tab
   completion, and away from anything a user or `git fetch --prune` manages.
   *Test:* the fake runner records the fetch target and the `update-ref -d`, on both
   the success path and the conflict path.
2. **`clean-up-kanban` §2 (the card says §5; §5 holds no examples).** The staleness
   example `BmsSnapshot`, `analysis.py`, deepagents-as-runtime names a downstream repo's
   code. It becomes generic: "a card citing a module, type or framework the repo has
   since deleted or replaced". Only that line changes. The other dated provenance notes
   in the file stay (out of scope).
3. **`pr-check` `allowed-tools`.** `Bash(python3 *single_flight.py suite -- make pr_check)`
   becomes `Bash(python3 .agents/skills/resolve-pr-concerns/scripts/single_flight.py suite -- make pr_check)`,
   the same entry `.claude/settings.json` already allows. Today's `*` matches any file
   named `single_flight.py` anywhere. The skill body's command block shows that exact
   path, so an agent that copies it gets no prompt.
   *Test:* the frontmatter carries the anchored entry, and no `*` appears before
   `single_flight.py`.
4. **Preflight hard stop, in all 14 skills.** "(the full `make pr_check` unless the diff
   qualifies for the light check)" becomes "(the full `make pr_check` unless the diff
   qualifies for the light check; if the repo has no `pr-check-scope` rule, the full
   `make pr_check`)". `test_skill_extension_points.py`'s exact-match template is updated
   in step, so the test itself enforces the change across every skill.
5. **`build-from-issue` Step 2 release check (card amendment).** When the repo has no
   published release **and** no `.github/release-drafter.yml`, it says so in one line
   ("this repo doesn't publish releases; skipping the release check") and suggests no
   version. With either piece present, behavior is unchanged: no release but a drafter
   config still gets the `v0.1.0` suggestion. *Test:* a doc-guard asserts both branches
   are in Step 2.

## Approaches

Each item has one sensible fix:
- **Item 1:** an alternative is no ref at all (fetch to `FETCH_HEAD` and use the SHA).
  The card asks for a private namespace plus delete. It also keeps the object reachable
  for the duration of the `merge-tree` call.
- **Item 3:** an alternative is to drop the entry and rely on `.claude/settings.json`.
  That breaks repos whose settings don't carry it.

## Left to the builder

The exact wording of the generic kanban example and of the release-check skip line,
within the meaning above.
