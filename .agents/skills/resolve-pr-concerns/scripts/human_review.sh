#!/usr/bin/env bash
# Print a human reviewer's open concerns on a PR as JSON, and whether their
# request for changes still stands (resolve-pr-concerns Step 2h).
#
# Usage:
#   human_review.sh <pr-number> <owner>/<repo> [--config PATH]
#
# The reviewer bot's findings are read elsewhere (Steps 2a/2b); this lists what a
# PERSON asked for, which nothing else does:
#   threads              unresolved inline threads (line or file-level) a human
#                        started and the agent hasn't answered; reply on reply_to
#   review_bodies        human CHANGES_REQUESTED / COMMENTED review bodies not yet
#                        answered (the agent splits each body into its items)
#   open_concern_count   threads + review_bodies
#   changes_requested_by humans whose request for changes still stands: each
#                        one's newest APPROVED / CHANGES_REQUESTED / DISMISSED
#                        review decides (COMMENTED doesn't clear it, as on GitHub)
#   awaiting_rereview    of those, the ones already asked to review again
#   rerequest_reviewers  of those, the ones with nothing open and no pending
#                        request: run `gh pr edit <pr> --add-reviewer <login>`
#   answer_marker        the hidden marker every agent thread reply carries
#
# A human is any author that isn't a GitHub Bot, doesn't end in [bot] and isn't
# in any configured reviewer's bot_logins. The agent posts as whoever runs it, who
# can be the reviewer, so its answers are told apart by a hidden marker, never by
# login: "<!-- resolve-pr-concerns:answer -->" on a thread reply, and
# "<!-- resolve-pr-concerns:answer review=<id> -->" on the one top-level comment
# answering a review body. A thread is answered when its newest human comment
# carries the marker; a human follow-up after that reopens it. Every read is
# paginated.
#
# Exit codes: 0 — state printed; 64 — usage error, an explicit --config that
# doesn't exist, or an invalid one (default path: the repo root's
# .github/pr-reviewer.json). With no reviewer config at all (Step 1b), only
# GitHub Bot accounts are excluded.
#
# Designed for the resolve-pr-concerns skill. Dependencies: gh + jq + bash.

set -euo pipefail

PR=""
REPO=""
CONFIG=""
CONFIG_EXPLICIT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      if [[ $# -lt 2 ]]; then echo "--config needs a value" >&2; exit 64; fi
      CONFIG_EXPLICIT=1; CONFIG="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,35p' "$0"; exit 0 ;;
    *)
      if [[ -z "$PR" ]]; then PR="$1"
      elif [[ -z "$REPO" ]]; then REPO="$1"
      else echo "unexpected arg: $1" >&2; exit 64
      fi
      shift ;;
  esac
done

if [[ -z "$PR" || -z "$REPO" ]]; then
  echo "usage: $0 <pr-number> <owner>/<repo> [--config PATH]" >&2
  exit 64
fi
if [[ $CONFIG_EXPLICIT -eq 0 ]]; then
  CONFIG="$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.github/pr-reviewer.json"
fi
if [[ -r "$CONFIG" ]]; then
  if ! bots=$(jq -c '[.reviewers[]?.bot_logins[]?]' "$CONFIG" 2>/dev/null); then
    echo "PR-reviewer config $CONFIG is not valid JSON" >&2
    exit 64
  fi
elif [[ $CONFIG_EXPLICIT -eq 1 ]]; then
  echo "PR-reviewer config not found: $CONFIG" >&2
  exit 64
else
  bots='[]'  # no reviewer configured (Step 1b): only GitHub Bots are excluded
fi

OWNER="${REPO%%/*}"
NAME="${REPO#*/}"
THREADS_QUERY='query($owner:String!, $name:String!, $pr:Int!, $endCursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$pr) {
      reviewThreads(first:100, after:$endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved comments(first:1) { nodes { databaseId } } }
      }
    }
  }
}'

inline=$(gh api "repos/${REPO}/pulls/${PR}/comments" --paginate \
  --jq '.[] | {id, in_reply_to_id, subject_type, path, line, created_at, url: .html_url, body, login: .user.login, type: .user.type}')
reviews=$(gh api "repos/${REPO}/pulls/${PR}/reviews" --paginate \
  --jq '.[] | {id, state, body, submitted_at, url: .html_url, login: .user.login, type: .user.type}')
comments=$(gh api "repos/${REPO}/issues/${PR}/comments" --paginate \
  --jq '.[] | {body, login: .user.login, type: .user.type}')
resolved=$(gh api graphql --paginate -f query="$THREADS_QUERY" \
  -F owner="$OWNER" -F name="$NAME" -F pr="$PR" \
  --jq '.data.repository.pullRequest.reviewThreads.nodes[]
        | select(.comments.nodes[0].databaseId != null)
        | {root: .comments.nodes[0].databaseId, resolved: .isResolved}')
requested=$(gh api "repos/${REPO}/pulls/${PR}" --jq '[.requested_reviewers[]?.login]')

jq -n --argjson bots "$bots" --argjson requested "${requested:-[]}" \
  --slurpfile inline <(printf '%s\n' "$inline") \
  --slurpfile reviews <(printf '%s\n' "$reviews") \
  --slurpfile comments <(printf '%s\n' "$comments") \
  --slurpfile resolved <(printf '%s\n' "$resolved") '
  def human: (.login // "") as $l
    | $l != "" and .type != "Bot" and ($l | endswith("[bot]") | not)
      and ($bots | index($l) | not);
  def answer: (.body // "") | contains("<!-- resolve-pr-concerns:answer");

  ([$resolved[] | {key: (.root | tostring), value: .resolved}] | from_entries) as $done
  | ([$comments[] | select(human) | .body // ""
      | [match("<!-- resolve-pr-concerns:answer review=([0-9]+) -->"; "g")
         | .captures[0].string | tonumber][]]) as $answered
  | [$inline | group_by(.in_reply_to_id // .id)[]
     | sort_by(.created_at, .id) as $t
     | ($t[0].in_reply_to_id // $t[0].id) as $root
     | select($t[0].id == $root and ($done[$root | tostring] // false) == false)
     | select(($t[0] | human) and ($t[0] | answer | not))
     | [$t[] | select(human)] as $people
     | select($people[-1] | answer | not)
     | [$people[] | select(answer | not)][-1] as $ask
     | {root_id: $root, reply_to: $root, author: $ask.login,
        subject_type: $t[0].subject_type, path: $t[0].path, line: $t[0].line,
        body: $ask.body, url: $ask.url}] as $threads
  | [$reviews[] | select(human)
     | select(.state == "CHANGES_REQUESTED" or .state == "COMMENTED")
     | select((.body // "" | gsub("^\\s+|\\s+$"; "")) != "")
     | select(answer | not)
     | select(.id as $id | $answered | index($id) | not)
     | {review_id: .id, author: .login, state, submitted_at, body, url}] as $bodies
  | (reduce ([$reviews[] | select(human)
              | select(.state == "APPROVED" or .state == "CHANGES_REQUESTED"
                       or .state == "DISMISSED")] | sort_by(.submitted_at))[] as $r
       ({}; del(.[$r.login]) | .[$r.login] = $r.state)
     | to_entries | map(select(.value == "CHANGES_REQUESTED") | .key)) as $requesting
  | ([$threads[], $bodies[] | .author]) as $waiting
  | {threads: $threads,
     review_bodies: $bodies,
     open_concern_count: (($threads | length) + ($bodies | length)),
     changes_requested_by: $requesting,
     awaiting_rereview: [$requesting[] | select(. as $l | $requested | index($l))],
     rerequest_reviewers: [$requesting[] | select(. as $l
       | ($waiting | index($l) | not) and ($requested | index($l) | not))],
     answer_marker: "<!-- resolve-pr-concerns:answer -->"}'
