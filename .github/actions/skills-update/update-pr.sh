#!/usr/bin/env bash
# Commit the skill update on $BRANCH, push it only when needed, and open or
# refresh the "Update agent skills" PR. Run by the skills-update composite action
# after the branch was rebuilt on origin/$BASE and the skills were updated.
#
# Env:
#   BRANCH                 update branch; HEAD must be on it (default skills-update)
#   BASE                   base branch, fetched as refs/remotes/$REMOTE/$BASE (default main)
#   REMOTE                 git remote (default origin)
#   GH_CMD                 the gh executable (default gh); the test seam
#   PR_LABELS              space-separated labels, applied best-effort after creation
#   DISPATCH_CI            "true" to dispatch $CI_WORKFLOW on the branch (default true);
#                          the PR body then says which pull_request-only checks did not run
#   CI_WORKFLOW            workflow file to dispatch (default ci.yml)
#   SKILLS_UPDATE_SUMMARY  bump-pins.mjs's JSON; builds the PR body (may be empty or {})
#
# Exit codes: 0 nothing to do, or the PR is open and current; 1 a precondition is
# violated or an unexpected git/gh call failed.
#
# Needs bash 3.2+ (macOS) and node (for the PR body; always present where the
# `skills` CLI runs).

set -euo pipefail

BRANCH="${BRANCH:-skills-update}"
BASE="${BASE:-main}"
REMOTE="${REMOTE:-origin}"
GH_CMD="${GH_CMD:-gh}"
PR_LABELS="${PR_LABELS:-}"
DISPATCH_CI="${DISPATCH_CI:-true}"
CI_WORKFLOW="${CI_WORKFLOW:-ci.yml}"
SKILLS_UPDATE_SUMMARY="${SKILLS_UPDATE_SUMMARY:-}"
TITLE="Update agent skills"

log() { printf '%s\n' "$*"; }
die() { printf 'update-pr: %s\n' "$*" >&2; exit 1; }

# --- Preconditions ---------------------------------------------------------
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not inside a git work tree"
head_branch="$(git symbolic-ref --short -q HEAD || true)"
[ "$head_branch" = "$BRANCH" ] || die "HEAD is on '${head_branch:-detached}', expected '$BRANCH'"
git rev-parse --verify -q "refs/remotes/$REMOTE/$BASE" >/dev/null \
  || die "$REMOTE/$BASE is not fetched (expected refs/remotes/$REMOTE/$BASE)"

# --- PR body, from bump-pins.mjs's JSON -------------------------------------
build_body() {
  SKILLS_UPDATE_BASE="$BASE" SKILLS_UPDATE_DISPATCH_CI="$DISPATCH_CI" \
    SKILLS_UPDATE_CI_WORKFLOW="$CI_WORKFLOW" node -e '
    const raw = (process.env.SKILLS_UPDATE_SUMMARY || "").trim();
    let s = {};
    if (raw) {
      try { s = JSON.parse(raw); } catch (e) {
        process.stderr.write("update-pr: SKILLS_UPDATE_SUMMARY is not valid JSON: " + e.message + "\n");
        process.exit(1);
      }
    }
    const list = (v) => (Array.isArray(v) ? v : []);
    const out = ["Automated update of the agent skills installed with the `skills` CLI."];
    const section = (title, lines) => { if (lines.length) out.push("", "## " + title, "", ...lines); };
    section("Bumped", list(s.bumped).map((b) =>
      "- `" + b.name + "` (" + b.source + "): " + b.from + " → " + b.to + " ([release notes](" + b.notes_url + "))"));
    section("Available, not installed", list(s.available).map((a) =>
      "- `" + a.name + "` (" + a.source + ")"));
    section("Removed upstream (kept)", list(s.removed).map((r) =>
      "- `" + r.name + "` (" + r.source + ")"));
    section("No releases found", list(s.no_releases).map((src) => "- " + src));
    // GITHUB_TOKEN path: say plainly which checks this PR did not get.
    if (process.env.SKILLS_UPDATE_DISPATCH_CI === "true") {
      section("CI on this PR", [
        "This branch was pushed with the workflow `GITHUB_TOKEN`, whose pushes do not trigger `pull_request` workflows, so `" +
          process.env.SKILLS_UPDATE_CI_WORKFLOW + "` was dispatched on it with `workflow_dispatch` instead. " +
          "Checks that run only on `pull_request` (such as `review-gate`) did not run on this PR.",
        "",
        "To get the PR its full CI, set the `SKILLS_UPDATE_TOKEN` secret (a fine-grained PAT), or the " +
          "`SKILLS_UPDATE_APP_ID` and `SKILLS_UPDATE_APP_PRIVATE_KEY` secrets (a GitHub App).",
      ]);
    }
    out.push("", "This branch is rebuilt on the latest `" + process.env.SKILLS_UPDATE_BASE +
      "` every night, so it is force-pushed whenever the update changes. This PR never merges itself: review the diff and merge it yourself.");
    process.stdout.write(out.join("\n") + "\n");
  '
}
body="$(build_body)" || exit 1

# --- Find the open update PR ("pr list", not "pr view": it fails in Actions) --
# --head matches the branch NAME only, so a PR from a fork's same-named branch
# would match too; keep only PRs from this repository.
find_pr() {
  "$GH_CMD" pr list --head "$BRANCH" --base "$BASE" --state open \
    --json number,isCrossRepository \
    --jq '[.[] | select(.isCrossRepository | not)][0].number // ""'
}

# --- 1. Commit the drift -----------------------------------------------------
# skills-lock.json plus whichever skill folders are real directories here (in
# repos made from the template .claude/skills is a symlink to .agents/skills).
paths=()
if [ -f skills-lock.json ]; then paths+=(skills-lock.json); fi
for dir in .agents/skills .claude/skills; do
  if [ -d "$dir" ] && [ ! -L "$dir" ]; then paths+=("$dir"); fi
done

if [ "${#paths[@]}" -gt 0 ]; then
  git add -A -- "${paths[@]}"
fi
if [ "${#paths[@]}" -eq 0 ] || git diff --cached --quiet -- "${paths[@]}"; then
  log "No skill drift on $BASE. Nothing to do."
  stale="$(find_pr)"
  if [ -n "$stale" ]; then
    log "Closing update PR #$stale: the skills are up to date."
    "$GH_CMD" pr close "$stale" --comment \
      "Closing: the skills on \`$BASE\` are already up to date, so this update is no longer needed. The nightly run opens a new PR when there is something to update."
  fi
  exit 0
fi
git commit -q -m "$TITLE"

# --- 2. Push only when needed ----------------------------------------------
# Every force-push re-parks a GITHUB_TOKEN PR's pull_request CI runs in
# action_required, so skip the push when the remote branch already holds this
# exact tree on top of the current base.
pushed=false
new_tree="$(git rev-parse 'HEAD^{tree}')"
base_sha="$(git rev-parse "refs/remotes/$REMOTE/$BASE")"
remote_sha="$(git rev-parse --verify -q "refs/remotes/$REMOTE/$BRANCH" || true)"
up_to_date=false
if [ -n "$remote_sha" ]; then
  remote_tree="$(git rev-parse "$remote_sha^{tree}")"
  remote_parent="$(git rev-parse -q --verify "$remote_sha^" || true)"
  if [ "$remote_tree" = "$new_tree" ] && [ "$remote_parent" = "$base_sha" ]; then
    up_to_date=true
  fi
fi
if [ "$up_to_date" = true ]; then
  log "$REMOTE/$BRANCH already holds this update; not pushing."
else
  if [ -n "$remote_sha" ]; then
    git push "$REMOTE" "HEAD:refs/heads/$BRANCH" \
      --force-with-lease="refs/heads/$BRANCH:$remote_sha"
  else
    git push "$REMOTE" "HEAD:refs/heads/$BRANCH"
  fi
  pushed=true
fi

# --- 3. Create or reuse the PR, idempotently ----------------------------------
created=false
pr_number="$(find_pr)"
if [ -z "$pr_number" ]; then
  "$GH_CMD" pr create --head "$BRANCH" --base "$BASE" --title "$TITLE" --body "$body"
  created=true
  pr_number="$(find_pr)"
  [ -n "$pr_number" ] || die "created the PR but could not resolve its number"
else
  log "Refreshing the body of PR #$pr_number."
  "$GH_CMD" pr edit "$pr_number" --body "$body"
fi

# --- 4. Labels: best-effort, after the PR exists -------------------------------
# An unknown label passed to "pr create" aborts creation, so add them afterwards,
# one at a time. "${labels[@]}" on an empty array is fatal under set -u in bash 3.2.
labels=()
for label in $PR_LABELS; do labels+=("$label"); done
if [ "${#labels[@]}" -gt 0 ]; then
  for label in "${labels[@]}"; do
    "$GH_CMD" pr edit "$pr_number" --add-label "$label" \
      || echo "::warning::could not add label '$label' to PR #$pr_number"
  done
fi

# --- 5. Dispatch CI ---------------------------------------------------------
# GitHub parks the pull_request runs of a GITHUB_TOKEN-pushed PR in
# action_required; workflow_dispatch is the one trigger it always honours from
# that token. A brand-new PR on an already-current branch still needs one.
if [ "$pushed" != true ] && [ "$created" != true ]; then
  log "Branch and PR are unchanged; the existing checks remain valid."
elif [ "$DISPATCH_CI" != true ]; then
  log "DISPATCH_CI is not 'true'; not dispatching $CI_WORKFLOW."
else
  "$GH_CMD" workflow run "$CI_WORKFLOW" --ref "$BRANCH"
fi
