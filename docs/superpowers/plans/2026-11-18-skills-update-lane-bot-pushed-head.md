# Skills-update lane: bot-pushed head — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `review_gate.py`'s skills-update lane passes a PR only when the newest push on the lane branch made the PR's head and was made by a lane `authors` identity (#93).

**Architecture:** After `_is_lane_pull` matches, `_skills_update` reads the newest repository-activity entry for `refs/heads/<lane.branch>`, reduces it to the head's pusher with a pure helper, and passes that pusher to `skills_update_status`, which now also requires it to be in `lane.authors`. Any read failure fails closed (the lane is off for that PR) with one stderr line.

**Tech Stack:** Python 3 stdlib (the script runs on bare `python3` in CI), `gh api`, pytest via `uv run`.

**Spec:** `docs/superpowers/specs/2026-11-18-skills-update-lane-bot-pushed-head-design.md`

## Global Constraints

- `review_gate.py` stays stdlib-only: no tenacity, no third-party imports, no retry loop.
- Narrow excepts only (`.agents/rules/python/no-blind-except.md`): catch `subprocess.CalledProcessError` and `ValueError` around the activity read, nothing broader.
- The lane may never pass a PR it doesn't pass today; every new condition only removes passes.
- Non-lane PRs make no activity call (the 30-minute sweep runs over every open PR).
- Numpy-style docstrings, type hints, as in the rest of the file.
- Test command: `uv run pytest tests/template/test_review_gate.py -q`.

## Review Focus

1. **Activity lag:** the push event fires before the activity record lands, so the newest entry's `after` is the previous head. Expect a fail-closed result, not a pass (Task 1, `test_a_lagging_activity_log_fails_closed`).
2. **Token can't read `/activity` (403):** expect the lane off and a stderr line, never a crashed run (Task 1, `test_an_unreadable_activity_log_fails_closed`).
3. **A person clicks "Update branch" or pushes a fixup to `skills-update`:** expect the owner and review halves back (Task 1, `test_a_persons_push_on_the_bots_branch_needs_both_halves`).
4. **A malformed activity payload** (not a list, null actor, non-string login): expect None from `head_pusher`, not a `KeyError` (Task 1, parametrized `test_head_pusher_refuses`).
5. **Sweep cost:** a non-lane PR must not trigger the activity read (Task 1: the existing `test_a_non_lane_pr_reads_no_files_and_needs_the_owner_with_no_reviewer` fails if it does, because the fake `gh_json` asserts on unknown URLs).

---

### Task 1: Require the lane's identity to have pushed the head

**Files:**
- Modify: `.agents/skills/resolve-pr-concerns/scripts/review_gate.py` (module docstring lines 16-21; `_is_lane_pull` ~295; `skills_update_status` ~305-349; `_skills_update` ~615-641)
- Test: `tests/template/test_review_gate.py` (lane section from line 971; `_LaneRepo` ~1131; `_lane_run` ~1159)

**Interfaces:**
- Produces:
  - `head_pusher(activities: object, head_sha: str) -> str | None`: the login of the newest entry's `actor` when it is a dict whose `after == head_sha`, else None (empty or non-list input, non-dict entry, null actor, non-string login).
  - `skills_update_status(pull, files, lane, folders, repo, pusher: str | None) -> tuple[str, str] | None`: the existing checks plus `pusher in lane.authors`.
  - Activity URL read in `_skills_update`: `repos/{repo}/activity?ref={quote('refs/heads/' + lane.branch, safe='/')}&per_page=1` via `reviewer_state._gh_json(["api", url])`.

- [ ] **Step 1: Write the failing tests**

In the lane section:
- `_lane_status(...)` gains `pusher: Any = "acme-skills[bot]"` and passes it as the sixth argument.
- `test_lane_refuses` gains cases `{"pusher": "alice"}` (id `pushed-by-a-person`) and `{"pusher": None}` (id `no-pusher`).
- `test_head_pusher_returns_the_newest_actor_when_it_made_the_head`: `head_pusher([{"after": SHA, "actor": {"login": "acme-skills[bot]"}}, {"after": "1" * 40, "actor": {"login": "alice"}}], SHA) == "acme-skills[bot]"`.
- `test_head_pusher_refuses` parametrized, each `is None`: `[]`; `{}`; `[{"after": "1" * 40, "actor": {"login": "acme-skills[bot]"}}, {"after": SHA, "actor": {"login": "acme-skills[bot]"}}]` (id `bot-pushed-sha-earlier-not-newest`); `[{"after": SHA, "actor": None}]`; `["x"]`; `[{"after": SHA, "actor": {"login": 7}}]`.

In `_LaneRepo`: pop `activity` from `over` before `_pull(**over)`, default `[{"activity_type": "force_push", "after": SHA, "actor": {"login": "acme-skills[bot]"}}]`; `gh_json` returns it for `f"repos/{REPO}/activity?ref=refs/heads/skills-update&per_page=1"`, and raises it when it is an exception instance. The existing passing tests keep passing with this default.

New integration tests through `_lane_run`:
- `test_a_persons_push_on_the_bots_branch_needs_both_halves`: `activity=[{"after": SHA, "actor": {"login": "alice"}}, {"after": "1" * 40, "actor": {"login": "acme-skills[bot]"}}]`, `_state(verdict="none")` → `[p[0] for p in posts] == ["failure"]` and `"@owner must approve" in posts[0][1]`.
- `test_a_persons_push_with_no_reviewer_needs_the_owner`: the same activity with state `None` → one `failure` post naming `@owner must approve`.
- `test_a_lagging_activity_log_fails_closed`: `activity=[{"after": "1" * 40, "actor": {"login": "acme-skills[bot]"}}]`, `_state(verdict="none")` → `failure`.
- `test_an_unreadable_activity_log_fails_closed` (uses `capsys`): `activity=subprocess.CalledProcessError(1, "gh")`, `_state(verdict="none")` → `failure` post, and `"activity unreadable" in capsys.readouterr().err`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/template/test_review_gate.py -q -k "lane or pusher or push or activity"`
Expected: FAIL: `head_pusher` missing, `skills_update_status()` takes 5 positional arguments, and the person-push tests post the lane's success.

- [ ] **Step 3: Implement**

- Add `head_pusher` next to `_is_lane_pull`, with a docstring saying it reads only the newest entry (strict: a later push by anyone else wins).
- `skills_update_status`: add the `pusher` parameter (documented: "login that pushed the head (`head_pusher`); None or a non-author refuses"), and refuse when `pusher not in lane.authors`.
- `_skills_update`: after the `_is_lane_pull` check and before the files read, read the activity URL. On `(subprocess.CalledProcessError, ValueError)` print `f"{repo}#{pr}: skills-update lane: activity unreadable, lane off: {exc}"` to stderr and return `head, None`. If `head_pusher(...)` is not in `lane.authors`, return `head, None` without reading files. Pass the pusher to `skills_update_status`. Import `urllib.parse.quote` if not already imported.
- Module docstring (lines 16-21): "the update workflow's own PR (its bot author, its branch, this repo, no fork, and a head the bot itself pushed: the newest push on the branch, per the repository activity log)". `_is_lane_pull`'s docstring is unchanged (it still checks author, branch, repo).

- [ ] **Step 4: Run the file to verify everything passes**

Run: `uv run pytest tests/template/test_review_gate.py -q`
Expected: all pass, including every pre-existing lane test.

- [ ] **Step 5: Lint, then commit and push**

Run: `make lint` → exit 0. Then:
```bash
git add .agents/skills/resolve-pr-concerns/scripts/review_gate.py tests/template/test_review_gate.py
git commit -m "Require the lane's bot to have pushed the update PR's head (#93)"
git push
```

### Task 2: Update the trust-model wording

**Files:**
- Modify: `README.md:280-308` (the `.github/skills-update-lane.json` section)
- Modify: `.agents/skills/resolve-pr-concerns/SKILL.md:535` (the lane paragraph)
- Modify: `.agents/skills/resolve-pr-concerns/references/skills-update-lane.example.json` (`description`)
- Test: `tests/template/test_readme_workflow.py:116` (`test_readme_customizing_documents_skills_update_lane`), `tests/template/test_review_gate.py` (`test_shipped_lane_example_is_valid_and_template_ships_no_lane_file`)

**Interfaces:**
- Consumes: Task 1's behavior (the newest push on the branch must be by one of `authors` and must have made the head).

- [ ] **Step 1: Write the failing tests**

- In `test_readme_customizing_documents_skills_update_lane`: `assert "pushed by one of its `authors`" in readme[start:end]`.
- In `test_shipped_lane_example_is_valid_and_template_ships_no_lane_file`: `assert "head pushed by" in json.loads(LANE_EXAMPLE.read_text())["description"]`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/template/test_readme_workflow.py tests/template/test_review_gate.py -q -k "skills_update_lane or lane_example"`
Expected: 2 FAIL on the missing phrases.

- [ ] **Step 3: Edit the three docs**

- README: the PR "must come from the file's `branch`, be opened by one of its `authors`, have its head pushed by one of its `authors` (the newest push on the branch, per GitHub's repository activity log), and touch only …". After "or anyone else's PR", add: "A push to the branch by anyone else, including the Update branch button, takes the PR out of the lane until the bot pushes a fresh head." In the tradeoff paragraph, "the lane trusts its `authors`" stays: it is still true.
- SKILL.md lane paragraph: "(opened by one of those authors from that branch in this repo, with its newest push made by one of them, touching only …)".
- Example JSON `description`: "(opened by one of `authors` from `branch` in this repo, with its head pushed by one of `authors`)".

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/template/test_readme_workflow.py tests/template/test_review_gate.py -q`
Expected: all pass.

- [ ] **Step 5: Commit and push**

```bash
git add README.md .agents/skills/resolve-pr-concerns/SKILL.md .agents/skills/resolve-pr-concerns/references/skills-update-lane.example.json tests/template/test_readme_workflow.py tests/template/test_review_gate.py
git commit -m "Document that the lane needs a bot-pushed head (#93)"
git push
```
