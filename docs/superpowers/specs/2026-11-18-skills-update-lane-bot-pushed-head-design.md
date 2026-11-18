# Skills-update lane: the bot must have pushed the head (#93)

Small change. Approved in conversation on 2026-11-18.

## Intent

#82's skills-update lane in `review_gate.py` passes the nightly update PR on both
halves (code owner and review) when the PR's **author** is one of the lane file's
`authors`, it comes from the lane `branch`, and it isn't a fork. Anyone with write
access can push to that branch and inherit the pass: no review, no code-owner
approval, auto-merge already on. The lane should pass only code the bot itself put
at the head; a head anyone else pushed falls back to the owner and review halves.

## Goals / non-goals

- Goal: the lane also requires the push that made the current head the branch tip
  to have been made by a lane `authors` identity.
- Goal: tests for a bot-pushed head passing and a person's push on the bot's branch
  falling back; trust-model wording updated everywhere it appears.
- Non-goal: a dedicated GitHub App for `review-gate` itself (the documented insider
  gap in `review-gate.yml`).
- Anti-goal: anything that could make the lane pass *more* PRs than today, or crash
  a gate run (a red X trains people to ignore red).

## Approaches

1. **Pusher via the repository activity API** (chosen). `GET
   /repos/{o}/{r}/activity?ref=refs/heads/<branch>` records each push,
   force-push and branch creation with its server-side `actor` and `after` SHA.
   Nobody can forge that identity. Verified live on this repo: `dependabot[bot]`
   shows as the actor of its own force-pushes.
2. **Committer login on the head commit** (the card's wording, rejected). The
   update action commits as `github-actions[bot]`
   (`.github/actions/skills-update/action.yml:56`), so the action would need
   changing too. Worse, the committer is self-asserted: anyone can set `user.email`
   to the bot's noreply address and GitHub reports the bot's login. It adds the
   look of a check without the substance.
3. **Both** (rejected): the committer half adds nothing a spoofer can't fake.

## Design

- `_skills_update` reads the newest activity entry on `refs/heads/<lane.branch>`
  (`per_page=1`, newest first), only after `_is_lane_pull` passes, so every other
  PR pays no extra API call.
- **Strict rule:** the lane passes only when that newest entry's `after` equals
  the PR's head SHA **and** its `actor.login` is in `lane.authors`. Any of these
  falls back to the owner and review halves: an older bot push of the same SHA
  followed by someone else's push, an empty list, or a null actor. (Builder's
  call: "the newest push only", not "the bot pushed this SHA at some point", which
  is simpler to reason about and costs nothing in normal runs.)
- `skills_update_status` gains a `pusher: str | None` parameter and stays pure; a
  small pure helper `head_pusher(activities, head_sha)` returns the newest
  entry's actor login when its `after` matches the head, else None.
- **Fail closed, never crash:** a failed activity read (`CalledProcessError`, e.g.
  a 403 if the workflow token can't read activity) or an unparseable response
  prints one stderr line and the lane is off for that PR.
- **Lag:** if the activity record lands after the gate runs, the gate fails
  closed and the 30-minute sweep heals it. No retry loop (the script is
  stdlib-only by design); auto-merge may wait up to one sweep.
- No change to the update action: it already pushes with the App or PAT token,
  the identity `authors` lists.

## Docs to update

The README lane section (`README.md` "`.github/skills-update-lane.json`"), the
`review_gate.py` module docstring and `_is_lane_pull` / `skills_update_status`
docstrings, `resolve-pr-concerns` SKILL.md's lane paragraph, and the example lane
file's `description`.

## Left to verify live

Whether the review-gate workflow's `GITHUB_TOKEN` (`contents: read`) may read
`/activity`. If it can't, the lane is off (safe) and the workflow needs a
permission change; the live test kit must prove it on a consumer repo.
