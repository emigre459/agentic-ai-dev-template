#!/usr/bin/env python3
"""Post the vendor-neutral ``review-gate`` commit status for a PR.

The PR reviewer's own status check passes on heads it never reviewed (skipped or
rate-limited pushes), so a ruleset should require this status instead. It is green
only when the configured reviewer (``.github/pr-reviewer.json``) has a verdict on the
current head AND its final full review finished -- the state ``reviewer_state``
reports. A verdict carried onto a rebase-only head (``verdict_carried_from``) counts
like one on the head itself. A reviewer with a local review runs its fix rounds off
the PR, so its gate is the final full review finished, whatever the head. Its open
threads block the merge through the ruleset's required conversation resolution when
the default branch's rules require it (resolving a thread fires no Actions event, so
a status would lag); otherwise the gate itself fails while any is open, and the
periodic sweep picks up the resolution.

A repo may opt in to a skills-update lane (``.github/skills-update-lane.json``): the
update workflow's own PR (its bot author, its branch, this repo, no fork) whose every
changed and renamed-from path is ``skills-lock.json`` or inside a tag-pinned skill's
folder passes as "skills-update: tag-pinned skills only, no review required" on BOTH
halves, before the CODEOWNERS half is read. Any other PR, or no lane file, falls
through to the order below.

With a CODEOWNERS file (see ``path_owners``), a PR touching a path its author
doesn't own also needs that path's owner to approve the head; that half is reported
first. Once no owner's approval is missing, a docs-only PR (every changed and
renamed-from path in the repo's opt-in ``.github/docs-only-paths.json`` allowlist)
passes as "docs-only: no review required" without a reviewer verdict. The template
ships no such file, so with it absent no PR is docs-only; a malformed file or a
capped file list fails closed the same way. CODEOWNERS alone turns the status on:
with no reviewer configured (``"active": null``) the status is the ownership half
only. A CODEOWNERS file that names no user owner (empty, or only team, email or
unsupported-syntax lines, which ``path_owners`` skips) counts as no file. With
neither, nothing is posted. Run by
``.github/workflows/review-gate.yml`` from the default branch's checkout, never from
PR code, so a PR can't loosen the CODEOWNERS that gates it.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import subprocess  # nosec B404 -- fixed gh argv, no shell
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import merge_gate
import path_owners
import reviewer_state

STATUS_CONTEXT = "review-gate"
# scripts/ -> resolve-pr-concerns/ -> skills/ -> .agents/ -> repo root. The workflow
# checks out the default branch, so the CODEOWNERS read here is the default branch's.
REPO_ROOT = Path(__file__).resolve().parents[4]
OWNERSHIP_CLEAR = "no changed path needs another owner's approval"
# Opt-in: the template ships no such file (an example lives in the skill's
# references/). Read from the default-branch checkout, like CODEOWNERS.
DOCS_ONLY_PATHS_FILE = REPO_ROOT / ".github" / "docs-only-paths.json"
DOCS_ONLY_STATUS = ("success", "docs-only: no review required")
# Opt-in like the docs-only file; the lane reads the default branch's lock too.
SKILLS_UPDATE_LANE_FILE = REPO_ROOT / ".github" / "skills-update-lane.json"
SKILLS_LOCK_FILE = REPO_ROOT / "skills-lock.json"
SKILLS_UPDATE_STATUS = (
    "success",
    "skills-update: tag-pinned skills only, no review required",
)
_SEMVER_TAG = re.compile(r"^v\d+\.\d+\.\d+$")  # same test as bump-pins.mjs
_SKILL_ROOTS = (".agents/skills", ".claude/skills")
_MAX_DESCRIPTION = 140  # GitHub rejects longer status descriptions
# Posts per run before giving up on a state that keeps changing (the sweep retries).
_MAX_POSTS = 5


class DocsAllowlist(NamedTuple):
    """Which paths a docs-only PR may touch.

    Attributes
    ----------
    dirs : tuple[str, ...]
        Directory prefixes whose files may be docs.
    dir_suffixes : tuple[str, ...]
        File suffixes (lowercased match) allowed under ``dirs``.
    top_level_suffix : str
        Suffix allowed for files at the repository root.
    excluded_names : tuple[str, ...]
        Base names that are never docs, at any depth (case-insensitive).
    """

    dirs: tuple[str, ...]
    dir_suffixes: tuple[str, ...]
    top_level_suffix: str
    excluded_names: tuple[str, ...]


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(item, str) and item for item in value):
        return None
    return tuple(value)


def load_docs_allowlist(path: Path) -> DocsAllowlist | None:
    """Read the docs-only allowlist, failing closed on any problem.

    Parameters
    ----------
    path : Path
        The ``docs-only-paths.json`` file to read.

    Returns
    -------
    DocsAllowlist or None
        The allowlist, or None (no PR is docs-only) when the file is missing,
        isn't a JSON object, or any of ``dirs``, ``dir_suffixes``,
        ``excluded_names`` (non-empty string lists) and ``top_level_suffix`` (a
        non-empty string) is missing or mistyped. Other keys are ignored.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"{path}: unreadable docs-only allowlist: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        data = {}
    dirs = _string_tuple(data.get("dirs"))
    dir_suffixes = _string_tuple(data.get("dir_suffixes"))
    excluded_names = _string_tuple(data.get("excluded_names"))
    top_level_suffix = data.get("top_level_suffix")
    if (
        dirs is None
        or dir_suffixes is None
        or excluded_names is None
        or not isinstance(top_level_suffix, str)
        or not top_level_suffix
    ):
        print(f"{path}: malformed docs-only allowlist", file=sys.stderr)
        return None
    return DocsAllowlist(dirs, dir_suffixes, top_level_suffix, excluded_names)


@functools.cache
def _cached_docs_allowlist(path: Path) -> DocsAllowlist | None:
    """Load the allowlist once per path per process, so a sweep reads it once."""
    return load_docs_allowlist(path)


def _is_docs_path(path: str, allowlist: DocsAllowlist) -> bool:
    excluded = {name.lower() for name in allowlist.excluded_names}
    if path.rsplit("/", 1)[-1].lower() in excluded:
        return False
    if path.startswith(allowlist.dirs):
        return path.lower().endswith(allowlist.dir_suffixes)
    return "/" not in path and path.endswith(allowlist.top_level_suffix)


def docs_only_status(
    files: Sequence[Mapping[str, Any]],
    changed_files: int,
    allowlist: DocsAllowlist | None,
) -> tuple[str, str] | None:
    """Return a passing status when a PR touches only docs, else None.

    Parameters
    ----------
    files : Sequence[Mapping[str, Any]]
        The PR's files as the REST API lists them: ``filename`` and, for a
        rename, ``previous_filename``.
    changed_files : int
        GitHub's count of changed files; more than ``len(files)`` means the list
        was capped, so the PR is never docs-only.
    allowlist : DocsAllowlist or None
        The docs-only paths; None (no file, or a malformed one) makes no PR
        docs-only.

    Returns
    -------
    tuple[str, str] or None
        ``DOCS_ONLY_STATUS``, or None when a review is needed.
    """
    if allowlist is None or not files or changed_files != len(files):
        return None
    paths = [f["filename"] for f in files]
    paths += [f["previous_filename"] for f in files if f.get("previous_filename")]
    if all(_is_docs_path(path, allowlist) for path in paths):
        return DOCS_ONLY_STATUS
    return None


class SkillsUpdateLane(NamedTuple):
    """Who may open the skills-update PR, and from which branch.

    Attributes
    ----------
    branch : str
        The head branch the update workflow pushes.
    authors : tuple[str, ...]
        Logins allowed to author that PR (the workflow's bot).
    """

    branch: str
    authors: tuple[str, ...]


def load_skills_update_lane(path: Path) -> SkillsUpdateLane | None:
    """Read the skills-update lane file, failing closed on any problem.

    Parameters
    ----------
    path : Path
        The ``skills-update-lane.json`` file to read.

    Returns
    -------
    SkillsUpdateLane or None
        The lane, or None (the lane is off) when the file is missing (silently),
        isn't a JSON object, or ``branch`` (a non-empty string) or ``authors`` (a
        non-empty list of non-empty strings) is missing or mistyped. Other keys
        are ignored.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"{path}: unreadable skills-update lane: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        data = {}
    branch = data.get("branch")
    authors = _string_tuple(data.get("authors"))
    if not isinstance(branch, str) or not branch or authors is None:
        print(f"{path}: malformed skills-update lane", file=sys.stderr)
        return None
    return SkillsUpdateLane(branch, authors)


def tag_pinned_skill_folders(lock_path: Path) -> tuple[str, ...]:
    """Return the local folders of every tag-pinned skill in the lock.

    The folder comes from the entry's key (where the skills CLI installs it), not
    its ``skillPath``, which is a path in the upstream repo.

    Parameters
    ----------
    lock_path : Path
        The ``skills-lock.json`` file to read.

    Returns
    -------
    tuple[str, ...]
        ``.agents/skills/<name>`` and ``.claude/skills/<name>`` for each entry whose
        ``ref`` is a ``vX.Y.Z`` tag; empty (the lane is off) when the lock is missing
        or malformed. Keys that aren't a plain folder name are skipped.
    """
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, ValueError) as exc:
        print(f"{lock_path}: unreadable skills lock: {exc}", file=sys.stderr)
        return ()
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, dict):
        return ()
    folders = []
    for name, entry in skills.items():
        ref = entry.get("ref") if isinstance(entry, dict) else None
        if not isinstance(ref, str) or not _SEMVER_TAG.match(ref):
            continue
        if name in ("", ".", "..") or "/" in name or "\\" in name:
            continue
        folders += [f"{root}/{name}" for root in _SKILL_ROOTS]
    return tuple(folders)


@functools.cache
def _cached_skills_update_lane(path: Path) -> SkillsUpdateLane | None:
    """Load the lane once per path per process, so a sweep reads it once."""
    return load_skills_update_lane(path)


@functools.cache
def _cached_tag_pinned_folders(path: Path) -> tuple[str, ...]:
    """Read the lock once per path per process, so a sweep reads it once."""
    return tag_pinned_skill_folders(path)


def skills_update_status(
    pull: Mapping[str, Any],
    files: Sequence[Mapping[str, Any]],
    lane: SkillsUpdateLane | None,
    folders: Sequence[str],
    repo: str,
) -> tuple[str, str] | None:
    """Return a passing status for the skills-update PR, else None.

    Parameters
    ----------
    pull : Mapping[str, Any]
        The PR as the REST API returns it.
    files : Sequence[Mapping[str, Any]]
        The PR's files: ``filename`` and, for a rename, ``previous_filename``.
    lane : SkillsUpdateLane or None
        The opted-in lane; None turns the lane off.
    folders : Sequence[str]
        Tag-pinned skill folders (``tag_pinned_skill_folders``); empty turns the
        lane off.
    repo : str
        Repository as ``owner/name``; a head from any other repo (a fork) is refused.

    Returns
    -------
    tuple[str, str] or None
        ``SKILLS_UPDATE_STATUS``, or None when the PR isn't the update PR or
        touches any other path.
    """
    if lane is None or not folders:
        return None
    head = pull["head"]
    if (
        pull["user"]["login"] not in lane.authors
        or head["ref"] != lane.branch
        or (head.get("repo") or {}).get("full_name") != repo
        or not files
        or int(pull["changed_files"]) != len(files)
    ):
        return None
    paths = [f["filename"] for f in files]
    paths += [f["previous_filename"] for f in files if f.get("previous_filename")]
    if all(
        p == "skills-lock.json" or any(p.startswith(f + "/") for f in folders)
        for p in paths
    ):
        return SKILLS_UPDATE_STATUS
    return None


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_DESCRIPTION else text[: _MAX_DESCRIPTION - 1] + "…"


def gate_status(
    state: Mapping[str, Any], *, threads_enforced: bool = False
) -> tuple[str, str]:
    """Return (status state, description) for one PR's ``collect_state`` output.

    Parameters
    ----------
    state : Mapping[str, Any]
        Reviewer state as returned by ``reviewer_state.collect_state``.
    threads_enforced : bool, default False
        Whether the default branch's rules require review threads resolved; only a
        locally reviewed PR reads it (``_local_gate_status``).

    Returns
    -------
    tuple[str, str]
        The commit-status state (``success``, ``pending`` or ``failure``) and its
        clipped description.
    """
    if (state.get("local_review") or {}).get("configured"):
        return _local_gate_status(state, threads_enforced=threads_enforced)
    name, sha7 = state["reviewer_name"], state["head_sha"][:7]
    trigger = state["trigger_comment"]
    if state["verdict"] not in ("pass", "findings"):
        # Pending, not failure: the reviewer's reply edit re-runs this workflow.
        if state.get("review_running"):
            return "pending", _clip(f"{name} is reviewing {sha7}")
        if state.get("incremental_review_on_head") and state.get(
            "incremental_review_outcome"
        ) in ("failed", "not_completed"):
            return "failure", _clip(
                f"{name}'s last review request did not complete: "
                f"post `{trigger}` for {sha7}"
            )
        return "failure", _clip(f"{name} has not reviewed {sha7}: post `{trigger}`")
    final = state["final_review_outcome"]
    command = state.get("final_review_comment")
    if final in ("finished", "not_required"):
        source = state.get("verdict_carried_from")
        if source:
            why = (
                "same diff"
                if state.get("verdict_carry_reason") == "same_patch"
                else "nothing new to review"
            )
            return "success", _clip(
                f"{name}'s verdict on {source[:7]} carries to {sha7} ({why})"
            )
        return "success", _clip(f"{name} reviewed {sha7}")
    if final == "not_requested":
        return "failure", _clip(
            f"{name} reviewed {sha7}; final full review not run: post `{command}`"
        )
    if final == "pending":
        return "pending", _clip(f"{name}'s final full review is still running")
    if final == "failed":
        return "failure", _clip(
            f"{name}'s final full review failed: wait 20+ min, "
            f"post `{command}` once more"
        )
    return "failure", _clip(f"unrecognized final-review outcome: {final}")


def _local_gate_status(
    state: Mapping[str, Any], *, threads_enforced: bool
) -> tuple[str, str]:
    """Gate a PR whose fix rounds ran locally on its one final full review.

    Fixes pushed after the full review move the head, and are re-checked locally
    rather than by another PR review (which would spend the scarce PR quota). So
    the gate needs the full review finished, not a verdict on the current head.
    When the default branch's rules require conversation resolution, the
    reviewer's open threads block the merge there, not here: resolving a thread
    fires no Actions event, so a status computed from them would lag. Without
    that rule nothing else holds them, so the gate fails while any is open (the
    periodic sweep clears it once they're resolved).

    Parameters
    ----------
    state : Mapping[str, Any]
        Reviewer state as returned by ``reviewer_state.collect_state``.
    threads_enforced : bool
        Whether the default branch's rules require review threads resolved.

    Returns
    -------
    tuple[str, str]
        The commit-status state and its clipped description.
    """
    name, sha7 = state["reviewer_name"], state["head_sha"][:7]
    command = state.get("final_review_comment")
    final = state["final_review_outcome"]
    if final == "not_requested":
        return "failure", _clip(f"{name}'s full review not run: post `{command}`")
    if final == "pending":
        return "pending", _clip(f"{name}'s full review is still running")
    if final == "failed":
        return "failure", _clip(
            f"{name}'s full review failed: wait 20+ min, post `{command}` once more"
        )
    if final not in ("finished", "not_required"):
        return "failure", _clip(f"unrecognized final-review outcome: {final}")
    open_threads = len(state.get("open_findings") or [])
    if open_threads and not threads_enforced:
        return "failure", _clip(f"{open_threads} {name} thread(s) still open")
    local = (
        "local review"
        if state["local_review"].get("reviewed_head")
        else "no local review"
    )
    return "success", _clip(f"{name}'s full review finished; {local} on {sha7}")


def _gh(args: Sequence[str]) -> None:
    subprocess.run(  # nosec B603 B607 -- fixed gh argv, no shell
        ["gh", *args], check=True, capture_output=True, text=True
    )


def _gh_output(args: Sequence[str]) -> str:
    return subprocess.run(  # nosec B603 B607 -- fixed gh argv, no shell
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


class OwnershipInputs(NamedTuple):
    """What the ownership half of the gate reads from a PR.

    Attributes
    ----------
    author : str
        The PR author's login.
    changed_files : int
        GitHub's count of changed files (may exceed ``files`` when truncated).
    files : list[str]
        The changed files as listed.
    renamed_from : list[str]
        Old paths of renamed files.
    reviews : list[Mapping[str, Any]]
        The PR's reviews, oldest first.
    head_sha : str
        The PR's head commit when it was read.
    """

    author: str
    changed_files: int
    files: list[str]
    renamed_from: list[str]
    reviews: list[Mapping[str, Any]]
    head_sha: str


def _ownership_inputs(repo: str, pr: int) -> OwnershipInputs:
    pull = reviewer_state._gh_json(["api", f"repos/{repo}/pulls/{pr}"])
    listed = (
        reviewer_state._gh_json(
            ["api", "--paginate", f"repos/{repo}/pulls/{pr}/files?per_page=100"]
        )
        or []
    )
    reviews = (
        reviewer_state._gh_json(
            ["api", "--paginate", f"repos/{repo}/pulls/{pr}/reviews?per_page=100"]
        )
        or []
    )
    return OwnershipInputs(
        author=pull["user"]["login"],
        changed_files=int(pull["changed_files"]),
        files=[f["filename"] for f in listed],
        renamed_from=[
            f["previous_filename"] for f in listed if f.get("previous_filename")
        ],
        reviews=reviews,
        head_sha=str(pull["head"]["sha"]),
    )


def _codeowners_rules() -> list[path_owners.Rule] | None:
    """Return the checkout's CODEOWNERS rules, or None when there are none to enforce.

    A file naming no user owner (empty, ownerless, or only lines ``path_owners``
    skips) gates nothing, so it counts as no file: the gate stays invisible.
    """
    path = path_owners.find_codeowners(REPO_ROOT)
    if path is None:
        return None
    rules = path_owners.load_rules(path)
    return rules if any(rule.owners for rule in rules) else None


def ownership_gate_status(repo: str, pr: int, head_sha: str) -> tuple[str, str] | None:
    """Return the ownership half of the gate for ``pr``, or None without CODEOWNERS.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    pr : int
        Pull request number.
    head_sha : str
        The head the gate is being computed for.

    Returns
    -------
    tuple[str, str] or None
        ``("failure", description)`` while a path owner's approval is missing,
        ``("success", OWNERSHIP_CLEAR)`` when none is, or None when the default
        branch has no CODEOWNERS naming a user owner (the PR isn't read at all).
    """
    return _ownership(repo, pr, head_sha)[1]


def _ownership(
    repo: str, pr: int, head_sha: str | None
) -> tuple[str, tuple[str, str] | None]:
    """Return (head SHA, ownership status) for ``pr``; see ``ownership_gate_status``.

    With ``head_sha`` None the head is the one the PR read reports (no reviewer
    state to take it from), so the pull is fetched once.
    """
    rules = _codeowners_rules()
    if rules is None:
        return head_sha or "", None
    pr_in = _ownership_inputs(repo, pr)
    head = head_sha or pr_in.head_sha
    status = path_owners.ownership_status(
        rules,
        pr_in.files,
        pr_in.author,
        path_owners.approvers(pr_in.reviews, head),
        changed_files=pr_in.changed_files,
        renamed_from=pr_in.renamed_from,
    )
    return head, status or ("success", OWNERSHIP_CLEAR)


def _docs_only(repo: str, pr: int, head_sha: str) -> tuple[str, str] | None:
    """Return the docs-only pass for ``pr``, or None when it needs a review.

    Reads nothing from the PR unless the repo opted in with an allowlist. A push
    between the reviewer-state read and this one moves the head, so the files would
    belong to a different head: fail closed (the push triggers its own run).
    """
    allowlist = _cached_docs_allowlist(DOCS_ONLY_PATHS_FILE)
    if allowlist is None:
        return None
    pull = reviewer_state._gh_json(["api", f"repos/{repo}/pulls/{pr}"])
    if str(pull["head"]["sha"]) != head_sha:
        return None
    listed = (
        reviewer_state._gh_json(
            ["api", "--paginate", f"repos/{repo}/pulls/{pr}/files?per_page=100"]
        )
        or []
    )
    return docs_only_status(listed, int(pull["changed_files"]), allowlist)


def _skills_update(
    repo: str, pr: int, head_sha: str | None
) -> tuple[str, tuple[str, str] | None]:
    """Return (head SHA, the skills-update pass or None) for ``pr``.

    Reads nothing from the PR unless the repo opted in with a lane file and the lock
    has tag-pinned skills. With ``head_sha`` given, a push since it was read moves
    the head, so the files would belong to a different head: fail closed (the push
    triggers its own run). With it None the head is the pull's own.
    """
    lane = _cached_skills_update_lane(SKILLS_UPDATE_LANE_FILE)
    folders = _cached_tag_pinned_folders(SKILLS_LOCK_FILE) if lane else ()
    if lane is None or not folders:
        return head_sha or "", None
    pull = reviewer_state._gh_json(["api", f"repos/{repo}/pulls/{pr}"])
    head = str(pull["head"]["sha"])
    if head_sha is not None and head != head_sha:
        return head_sha, None
    listed = (
        reviewer_state._gh_json(
            ["api", "--paginate", f"repos/{repo}/pulls/{pr}/files?per_page=100"]
        )
        or []
    )
    return head, skills_update_status(pull, listed, lane, folders, repo)


def _threads_enforced(repo: str) -> bool:
    """Return whether the default branch's rules require review threads resolved.

    Reads the rules through ``merge_gate`` (one rules reader for both gates). A 404
    or a failed read counts as not enforced, so the gate waits on the threads itself
    rather than passing a PR nothing would hold.
    """
    try:
        repository = reviewer_state._gh_json(["api", f"repos/{repo}"])
        rules = merge_gate.branch_rules(repo, str(repository["default_branch"]))
    except (subprocess.CalledProcessError, KeyError, TypeError, ValueError) as exc:
        print(f"{repo}: rules unreadable, gating on threads: {exc}", file=sys.stderr)
        return False
    return merge_gate.threads_must_resolve(rules)


def _current_gate_status(repo: str, sha: str) -> tuple[str, str] | None:
    """Return the newest ``review-gate`` (state, description) on ``sha``, if any.

    GitHub lists a commit's statuses newest first.
    """
    statuses = json.loads(
        _gh_output(["api", f"repos/{repo}/commits/{sha}/statuses?per_page=100"]) or "[]"
    )
    for status in statuses:
        if status.get("context") == STATUS_CONTEXT:
            return (status.get("state") or "", status.get("description") or "")
    return None


def _open_prs(repo: str) -> list[int]:
    """Return every open PR number (REST pagination, no cap)."""
    out = _gh_output(
        [
            "api",
            "--paginate",
            f"repos/{repo}/pulls?state=open&per_page=100",
            "--jq",
            ".[].number",
        ]
    )
    return [int(n) for n in out.split()]


def _post(repo: str, sha: str, state: str, description: str) -> None:
    args = [
        "api",
        "-X",
        "POST",
        f"repos/{repo}/statuses/{sha}",
        "-f",
        f"state={state}",
        "-f",
        f"context={STATUS_CONTEXT}",
        "-f",
        f"description={description}",
    ]
    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        args += ["-f", f"target_url={server}/{repo}/actions/runs/{run_id}"]
    _gh(args)


def _want(
    repo: str, pr: int, adapter: reviewer_state.ReviewerAdapter | None
) -> tuple[str, tuple[str, str] | None]:
    """Return (head SHA, status to post) for ``pr``; no status when nothing gates it.

    The skills-update lane (opt-in) passes first, on both halves. Otherwise a missing
    owner's approval wins (it is the slower, human half); then a docs-only PR (opt-in)
    passes; otherwise the reviewer's gate decides, or the ownership half alone when
    there is no reviewer.
    """
    if adapter is None:
        head, lane_pass = _skills_update(repo, pr, None)
        if lane_pass is not None:
            return head, lane_pass
        return _ownership(repo, pr, None)
    state = reviewer_state.collect_state(repo, pr, adapter)
    head_sha = state["head_sha"]
    _, lane_pass = _skills_update(repo, pr, head_sha)
    if lane_pass is not None:
        return head_sha, lane_pass
    owners = ownership_gate_status(repo, pr, head_sha)
    if owners is not None and owners[0] == "failure":
        return head_sha, owners
    docs_only = _docs_only(repo, pr, head_sha)
    if docs_only is not None:
        return head_sha, docs_only
    # Rules are read only when they decide something: a locally reviewed PR with an
    # open reviewer thread.
    enforced = bool(
        (state.get("local_review") or {}).get("configured")
        and state.get("open_findings")
        and _threads_enforced(repo)
    )
    return head_sha, gate_status(state, threads_enforced=enforced)


def post_for_pr(
    repo: str, pr: int, adapter: reviewer_state.ReviewerAdapter | None
) -> None:
    """Compute the gate for ``pr`` and post it on the head it was computed for.

    The opt-in skills-update lane (``skills_update_status``) passes the update PR
    before either half is read. Otherwise the gate fails while a path owner's
    approval is missing, whatever the reviewer's state; once none is missing a docs-only PR passes (``docs_only_status``, opt-in);
    any other PR gets the reviewer's gate (``gate_status``). With no reviewer
    (``adapter`` None) it is the ownership half alone, and nothing is posted when
    there is no CODEOWNERS file naming a user owner either.

    Runs are never cancelled (a cancelled run is a red X on the PR and on `main`),
    so several can overlap. A run posts only when the head's newest status differs
    from what it computed, then re-reads and repeats until they match: if an
    overlapping run's stale result lands on top, the next pass puts the fresh one
    back. Skipping unchanged statuses also keeps the periodic sweep well under
    GitHub's 1,000-statuses-per-SHA limit.

    Parameters
    ----------
    repo : str
        Repository as ``owner/name``.
    pr : int
        Pull request number.
    adapter : reviewer_state.ReviewerAdapter or None
        Configured reviewer adapter, or None when no reviewer is active.
    """
    posts = 0
    while True:
        head_sha, want = _want(repo, pr, adapter)
        # Never a later head: a push mid-run triggers its own run for the new head.
        if (
            want is None
            or _current_gate_status(repo, head_sha) == want
            or posts == _MAX_POSTS
        ):
            return
        _post(repo, head_sha, *want)
        posts += 1


def post_for_all_open_prs(
    repo: str, adapter: reviewer_state.ReviewerAdapter | None
) -> None:
    """Recompute every open PR (the periodic sweep heals any status left stale).

    One PR's failure doesn't stop the rest; the run fails at the end if any did.
    """
    prs = _open_prs(repo)
    failed = []
    for pr in prs:
        try:
            post_for_pr(repo, pr, adapter)
        except Exception as exc:  # noqa: BLE001 -- isolate per PR, report below
            print(f"#{pr}: {exc}", file=sys.stderr)
            failed.append(pr)
    if failed:
        raise SystemExit(f"{len(failed)} of {len(prs)} PRs failed: {failed}")


def post_for_merge_group(repo: str, sha: str) -> None:
    """Pass a merge-group commit: a PR must satisfy its checks to be enqueued."""
    _post(repo, sha, "success", "PR-level review gate held when it was enqueued")


def main() -> None:
    """Post the gate for ``--pr``, or pass a ``--merge-group-sha`` commit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=int)
    target.add_argument("--merge-group-sha")
    target.add_argument("--all-open", action="store_true", help="every open PR")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="reviewer config (default: <repo>/.github/pr-reviewer.json)",
    )
    args = parser.parse_args()
    if args.merge_group_sha:
        post_for_merge_group(args.repo, args.merge_group_sha)
        return
    adapter: reviewer_state.ReviewerAdapter | None
    try:
        adapter = reviewer_state.load_reviewer(args.config)
    except reviewer_state.NoActiveReviewer as exc:
        adapter = None
        if _codeowners_rules() is None:
            # Nothing to gate. Post nothing: a ruleset that requires review-gate
            # with no reviewer active stays blocked, which is the right signal.
            print(f"::notice::{exc}")
            return
        print(f"::notice::{exc} Posting the CODEOWNERS half of the gate only.")
    if args.all_open:
        post_for_all_open_prs(args.repo, adapter)
    else:
        post_for_pr(args.repo, args.pr, adapter)


if __name__ == "__main__":
    main()
