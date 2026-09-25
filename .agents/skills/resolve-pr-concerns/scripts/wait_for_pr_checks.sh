#!/usr/bin/env bash
# Wait until every check on a PR has reached a terminal state, then print a
# pass/fail/pending breakdown and exit with a status that reflects whether
# the PR is mergeable.
#
# Usage:
#   wait_for_pr_checks.sh <pr-number> [<owner>/<repo>] [--timeout-min N]
#                         [--bot-window-sec S] [--expected-bot NAME]
#                         [--config PATH]
#
# Behaviour:
#   - Polls every 30s (default; configurable).
#   - Considers a check "settled" when its bucket is one of:
#       pass / fail / cancel / skipping
#   - The PR reviewer is whichever one .github/pr-reviewer.json names
#     (--config overrides the path). Reviewer bots don't always run on every
#     push; if the only remaining "pending" checks are external bot reviews
#     (any configured reviewer's check_name, or Copilot) and we've waited
#     longer than --bot-window-sec since the last push, we stop waiting on
#     them and report them as "no-review" (still surfaced so the user can
#     decide to re-trigger or proceed).
#   - Expected external bots may take time to *register* a check after a
#     push (gh pr checks won't even list them until they boot up). Pass
#     --expected-bot NAME (repeatable) to keep the soft-wait window open
#     for bots that haven't appeared in the check list yet. Default
#     expected list is the active reviewer's check_name from the config.
#   - An explicit --config that is missing, or any config that is invalid /
#     has no check_name for its active reviewer, exits 64 before any gh call.
#     An absent DEFAULT config means "no reviewer configured": CI-only wait.
#   - Exit codes:
#       0 — all required checks passed
#       1 — one or more checks failed (or cancelled)
#       2 — timed out before settling
#      64 — usage error, a missing explicit --config, or an invalid config
#
# Designed for the resolve-pr-concerns skill — not a general-purpose tool.
# Keep the dependencies to gh+jq+coreutils so it works on any dev machine.

set -euo pipefail

PR=""
REPO=""
TIMEOUT_MIN=20
BOT_WINDOW_SEC=600   # don't wait longer than this for external bot reviews
POLL_SEC=30
EXPECTED_BOTS=()
CONFIG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout-min) TIMEOUT_MIN="$2"; shift 2 ;;
    --bot-window-sec) BOT_WINDOW_SEC="$2"; shift 2 ;;
    --poll-sec) POLL_SEC="$2"; shift 2 ;;
    --expected-bot) EXPECTED_BOTS+=("$2"); shift 2 ;;
    --config) CONFIG="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,37p' "$0"; exit 0 ;;
    *)
      if [[ -z "$PR" ]]; then PR="$1"
      elif [[ -z "$REPO" ]]; then REPO="$1"
      else echo "unexpected arg: $1" >&2; exit 64
      fi
      shift ;;
  esac
done

if [[ -z "$PR" ]]; then
  echo "usage: $0 <pr-number> [<owner>/<repo>] [--timeout-min N] [--bot-window-sec S] [--config PATH]" >&2
  exit 64
fi

# The PR reviewer is configured, never hardcoded: .github/pr-reviewer.json names
# the active reviewer and every adapter's check_name. An explicit --config must
# exist; an absent DEFAULT config means the repo has no reviewer configured
# (resolve-pr-concerns Step 1b) -- settle CI with no expected reviewer bot.
ACTIVE_CHECK=""
REVIEWER_CHECKS=""
# Active adapter fields for the post-settle "has the reviewer reviewed HEAD?"
# poll; the defaults (no logins) skip that wait when no reviewer is configured.
REVIEWER_NAME="the reviewer"
REVIEWER_LOGINS="[]"
REVIEWED_SHA_PATTERN=""
SUMMARY_IN_ISSUE_COMMENTS="false"
CONFIG_EXPLICIT=1
if [[ -z "$CONFIG" ]]; then
  CONFIG_EXPLICIT=0
  CONFIG="$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.github/pr-reviewer.json"
fi
if [[ ! -e "$CONFIG" && $CONFIG_EXPLICIT -eq 0 ]]; then
  echo "NOTE: no PR reviewer configured ($CONFIG absent) -- waiting on CI only (plus Copilot, and any --expected-bot)." >&2
elif [[ ! -r "$CONFIG" ]]; then
  echo "PR-reviewer config not found: $CONFIG" >&2
  echo "Create .github/pr-reviewer.json naming the active reviewer (see resolve-pr-concerns references/reviewers/), or pass --config PATH." >&2
  exit 64
else
  if ! ACTIVE_CHECK=$(jq -er '.reviewers[.active].check_name | select(type == "string" and length > 0)' "$CONFIG" 2>/dev/null); then
    echo "PR-reviewer config $CONFIG has no check_name for its active reviewer (or is not valid JSON)." >&2
    exit 64
  fi
  # Every adapter's check is an external reviewer (one per line) -- an inactive
  # adapter's bot may still be installed mid-cutover.
  REVIEWER_CHECKS=$(jq -r '.reviewers[].check_name // empty' "$CONFIG")
  REVIEWER_NAME=$(jq -r '.reviewers[.active].display_name // .active' "$CONFIG")
  REVIEWER_LOGINS=$(jq -c '.reviewers[.active].bot_logins // []' "$CONFIG")
  REVIEWED_SHA_PATTERN=$(jq -r '.reviewers[.active].reviewed_sha_pattern // empty' "$CONFIG")
  SUMMARY_IN_ISSUE_COMMENTS=$(jq -r '.reviewers[.active].summary_in_issue_comments // false' "$CONFIG")
fi

# Default expected-bots list: the active reviewer's check (the async reviewer
# this skill triggers). Override or extend with --expected-bot.
if [[ ${#EXPECTED_BOTS[@]} -eq 0 && -n "$ACTIVE_CHECK" ]]; then
  EXPECTED_BOTS=("$ACTIVE_CHECK")
fi

repo_arg=()
[[ -n "$REPO" ]] && repo_arg=(--repo "$REPO")
# Every array expansion below uses ${arr[@]+"${arr[@]}"}: under `set -u`, bash
# < 4.4 (macOS /bin/bash 3.2) treats an EMPTY array's "${arr[@]}" as unbound.

# External bot checks that may legitimately stay "pending" forever if not
# triggered: any configured reviewer's check (exact name) or Copilot. We stop
# waiting on these after BOT_WINDOW_SEC and report them as "no-review" rather
# than blocking the merge decision.
COPILOT_RE='^.*[Cc]opilot.*$'
is_external_bot() {
  grep -Fxq -- "$1" <<< "$REVIEWER_CHECKS" && return 0
  [[ "$1" =~ $COPILOT_RE ]]
}

start_ts=$(date +%s)
deadline=$(( start_ts + TIMEOUT_MIN * 60 ))

echo "Waiting for PR #${PR} checks to settle (timeout: ${TIMEOUT_MIN}m, poll: ${POLL_SEC}s)..." >&2

while :; do
  # gh pr checks --json gives bucket={pass|fail|pending|cancel|skipping}
  json=$(gh pr checks "$PR" ${repo_arg[@]+"${repo_arg[@]}"} --json name,bucket,state,link 2>/dev/null || echo "[]")

  if [[ "$json" == "[]" || -z "$json" ]]; then
    echo "  (no checks reported yet — waiting)" >&2
  else
    summary=$(echo "$json" | jq -r '
      group_by(.bucket) | map({(.[0].bucket): length}) | add // {} |
      to_entries | map("\(.key)=\(.value)") | join(" ")
    ')
    pending=$(echo "$json" | jq -r '[.[] | select(.bucket == "pending") | .name] | join("\n")')
    all_names=$(echo "$json" | jq -r '[.[].name] | join("\n")')

    # Filter pending list: any external reviewer bot's check is "soft pending"
    soft_pending=""
    hard_pending=""
    while IFS= read -r name; do
      [[ -z "$name" ]] && continue
      if is_external_bot "$name"; then
        soft_pending+="$name"$'\n'
      else
        hard_pending+="$name"$'\n'
      fi
    done <<< "$pending"

    # Identify expected external bots that haven't appeared in the check
    # list yet (they may still be booting up after the push). Treat them
    # as additional soft-pending entries so the script doesn't race past
    # an async reviewer that just hadn't registered when we first polled.
    missing_expected=""
    for bot in ${EXPECTED_BOTS[@]+"${EXPECTED_BOTS[@]}"}; do
      if ! grep -Fxq "$bot" <<< "$all_names"; then
        missing_expected+="$bot (not yet registered)"$'\n'
      fi
    done

    elapsed=$(( $(date +%s) - start_ts ))
    if [[ -z "${hard_pending}" ]]; then
      # All required (non-bot) checks settled. Decide whether to also wait
      # on async bot reviews — either ones in flight (soft_pending) or ones
      # we expect that haven't even registered yet (missing_expected).
      soft_blockers=""
      [[ -n "${soft_pending}" ]] && soft_blockers+="${soft_pending}"
      [[ -n "${missing_expected}" ]] && soft_blockers+="${missing_expected}"
      if [[ -n "${soft_blockers}" && $elapsed -lt $BOT_WINDOW_SEC ]]; then
        bot_status=$(echo "${soft_blockers}" | tr '\n' ',' | sed 's/,$//; s/,/, /g')
        echo "  $summary (waiting on external bot reviews — ${elapsed}s/${BOT_WINDOW_SEC}s window: ${bot_status})" >&2
      else
        # Either no bot reviews pending/missing or the window has elapsed.
        break
      fi
    else
      echo "  $summary (still pending: $(echo "$hard_pending" | tr '\n' ',' | sed 's/,$//'))" >&2
    fi
  fi

  if [[ $(date +%s) -ge $deadline ]]; then
    echo "TIMEOUT after ${TIMEOUT_MIN}m. Latest:" >&2
    gh pr checks "$PR" ${repo_arg[@]+"${repo_arg[@]}"} >&2 || true
    exit 2
  fi

  sleep "$POLL_SEC"
done

# Final report — print full table to stdout so the caller can grep, plus
# a structured summary to stderr.
echo "FINAL CHECK RESULTS:"
gh pr checks "$PR" ${repo_arg[@]+"${repo_arg[@]}"} || true

# Surface any expected external bots that never registered a check, so
# callers don't silently assume they were "no-review" when they may have
# just been slow to boot.
final_names=$(gh pr checks "$PR" ${repo_arg[@]+"${repo_arg[@]}"} --json name --jq '.[].name' 2>/dev/null || echo "")
for bot in ${EXPECTED_BOTS[@]+"${EXPECTED_BOTS[@]}"}; do
  if ! grep -Fxq "$bot" <<< "$final_names"; then
    echo "NOTE: expected bot '$bot' did not register a check within ${BOT_WINDOW_SEC}s — treating as no-review." >&2
  fi
done

# Reviewer bots report findings as REVIEW COMMENTS, not a failing check (a check can
# read "skipping" even when the review left findings), and the review can land a few
# seconds AFTER the check settles. So once checks are green, give the configured
# reviewer a short window to post its review of the HEAD commit, then surface any
# UNRESOLVED findings.
# We use the GraphQL reviewThreads `isResolved` flag as the source of truth — NOT a
# comment's `commit_id` (GitHub re-points resolved comments' commit_id to HEAD too,
# which would over-report already-resolved threads). (Needs a repo arg for owner/repo.)
if [[ -n "$REPO" ]]; then
  owner="${REPO%%/*}"
  name="${REPO##*/}"
  head=$(gh pr view "$PR" ${repo_arg[@]+"${repo_arg[@]}"} --json headRefOid --jq '.headRefOid' 2>/dev/null || echo "")
  gql='query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p){reviewThreads(first:100){nodes{isResolved comments(first:1){nodes{author{login} path line body}}}}}}}'
  unresolved_count() {
    gh api graphql -f query="$gql" -F o="$owner" -F n="$name" -F p="$PR" \
      --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved==false)] | length' 2>/dev/null || echo 0
  }
  # Count items authored by a reviewer login that vouch for HEAD: REST reviews carry
  # commit_id; otherwise the adapter's reviewed_sha_pattern (one capture group) may
  # name the reviewed SHA in the body. Reads the REST endpoint, not
  # `gh pr view --json reviews`, whose commit id can be null for some reviewers.
  # shellcheck disable=SC2016  # $vars below are jq variables, not shell
  on_head_jq='[add // [] | .[]
      | select(.user.login as $l | $logins | index($l))
      | select($head != "" and (
          (.commit_id // "") == $head
          or ($pat != "" and ([(.body // "") | match($pat; "gi") | .captures[0].string
                               | select(. != null and length >= 7)]
                              | any(. as $s | $head | startswith($s))))))] | length'
  reviewed_head_count() {
    local n c=0
    n=$(gh api "repos/$REPO/pulls/$PR/reviews" --paginate 2>/dev/null \
      | jq -s --argjson logins "$REVIEWER_LOGINS" --arg head "$head" \
          --arg pat "$REVIEWED_SHA_PATTERN" "$on_head_jq" 2>/dev/null || echo 0)
    # Some reviewers keep the verdict in an issue-comment they edit in place.
    if [[ "$SUMMARY_IN_ISSUE_COMMENTS" == "true" ]]; then
      c=$(gh api "repos/$REPO/issues/$PR/comments" --paginate 2>/dev/null \
        | jq -s --argjson logins "$REVIEWER_LOGINS" --arg head "$head" \
            --arg pat "$REVIEWED_SHA_PATTERN" "$on_head_jq" 2>/dev/null || echo 0)
    fi
    echo $(( ${n:-0} + ${c:-0} ))
  }
  # Wait for the reviewer's review of HEAD to appear, then let unresolved findings
  # settle (count stable across one interval) so late-landing inline findings
  # aren't missed.
  prev_count=-1
  settled=0
  for _ in $(seq 1 12); do  # up to ~3min
    # No reviewer configured -> nothing to wait for; just let the count settle.
    if [[ "$REVIEWER_LOGINS" == "[]" ]]; then reviewed=1; else reviewed=$(reviewed_head_count); fi
    count=$(unresolved_count)
    if [[ "${reviewed:-0}" -gt 0 && "${count:-0}" == "$prev_count" ]]; then
      settled=1
      break
    fi
    prev_count="${count:-0}"
    sleep 15
  done
  [[ "$settled" -eq 0 ]] && echo "  (note: ${REVIEWER_NAME}'s review may still be settling — re-check if it just landed)" >&2
  echo "UNRESOLVED REVIEW FINDINGS:"
  # Capture stdout and the gh exit code SEPARATELY so a failed query (network/auth)
  # is reported as an error — never silently read as "none unresolved", which on a
  # merge gate would be a dangerous false all-clear.
  if findings=$(gh api graphql -f query="$gql" -F o="$owner" -F n="$name" -F p="$PR" \
        --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved==false) | .comments.nodes[0] | "  - \(.author.login) \(.path):\(.line) — \((.body|split("\n")[0]|gsub("^### ";"")))"' 2>/dev/null); then
    if [[ -n "$findings" ]]; then
      echo "$findings"
      echo "  (unresolved review threads remain — address + resolve them; do NOT merge yet.)" >&2
    else
      echo "  none unresolved."
    fi
  else
    echo "  ERROR: could not query review threads (gh/GraphQL failed) — do NOT assume clear; verify manually before merging." >&2
  fi
fi

# Exit code reflects pass/fail at the "required" check level.
fails=$(gh pr checks "$PR" ${repo_arg[@]+"${repo_arg[@]}"} --json name,bucket --jq '[.[] | select(.bucket == "fail" or .bucket == "cancel")] | length')
if [[ "${fails:-0}" -gt 0 ]]; then
  echo "$fails check(s) failed or were cancelled. PR is NOT mergeable." >&2
  exit 1
fi

echo "All required checks passed." >&2
exit 0
