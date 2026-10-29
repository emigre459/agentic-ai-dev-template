"""Tests for carrying the reviewer's verdict across heads in ``reviewer_state.py``.

A rebase or a merge from the base gives a PR a new head without changing its own
diff. ``reviewer_state`` fingerprints the head's own diff and the newest reviewed
commit's own diff (each from the base it had), and carries that commit's verdict
when they match (``same_patch``). A head the reviewer answered "no files to review"
inherits the newest reviewed commit's verdict too (``no_changes``).

The helpers are pure over GitHub API payloads; ``collect_state`` is driven through
a fake ``gh``. Adapters come from the real ``.github/pr-reviewer.json``.
"""

from __future__ import annotations

import importlib.util
import subprocess  # nosec B404 -- only CalledProcessError is used
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "resolve-pr-concerns"
    / "scripts"
    / "reviewer_state.py"
)


def _load_module() -> ModuleType:
    """Load the standalone reviewer_state script as a module."""
    spec = importlib.util.spec_from_file_location("reviewer_state", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["reviewer_state"] = module
    spec.loader.exec_module(module)
    return module


reviewer_state = _load_module()
CODERABBIT = reviewer_state.load_reviewer(key="coderabbit")
# Carries decide whether a moved head needs a PR re-review, which only a reviewer
# with no local review takes; the CodeRabbit adapter reviews locally, so these tests
# drive a copy of it without one.
CODERABBIT_PR = CODERABBIT._replace(local_review=None, local_preflight=None)
REPO = "owner/repo"


def _file(name: str, patch: str | None, **over: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "filename": name,
        "status": "modified",
        "sha": "blob1",
        "patch": patch,
        "changes": 0 if patch is None else 1,
    }
    entry.update(over)
    return entry


def _compare(*files: dict[str, Any]) -> dict[str, Any]:
    return {"files": list(files)}


ADD = "@@ -10,3 +10,4 @@ def f():\n ctx\n+new line\n ctx\n"
ADD_SHIFTED = "@@ -42,3 +57,4 @@ def f():\n ctx\n+new line\n ctx\n"
ADD_EDITED = "@@ -10,3 +10,4 @@ def f():\n ctx\n+new line, edited\n ctx\n"


# --- patch_fingerprint --------------------------------------------------------


def test_fingerprint_ignores_hunk_line_numbers() -> None:
    fp = reviewer_state.patch_fingerprint
    assert fp(_compare(_file("a.py", ADD))) == fp(_compare(_file("a.py", ADD_SHIFTED)))


def test_fingerprint_sees_an_edited_line() -> None:
    fp = reviewer_state.patch_fingerprint
    assert fp(_compare(_file("a.py", ADD))) != fp(_compare(_file("a.py", ADD_EDITED)))


def test_fingerprint_sees_a_different_file_set() -> None:
    fp = reviewer_state.patch_fingerprint
    one = _compare(_file("a.py", ADD))
    two = _compare(_file("a.py", ADD), _file("b.py", ADD))
    assert fp(one) != fp(two)


def test_fingerprint_ignores_file_order() -> None:
    fp = reviewer_state.patch_fingerprint
    assert fp(_compare(_file("a.py", ADD), _file("b.py", ADD))) == fp(
        _compare(_file("b.py", ADD), _file("a.py", ADD))
    )


def test_fingerprint_sees_a_rename() -> None:
    fp = reviewer_state.patch_fingerprint
    renamed = _file("b.py", ADD, status="renamed", previous_filename="a.py")
    assert fp(_compare(_file("b.py", ADD))) != fp(_compare(renamed))


def test_fingerprint_uses_the_blob_for_a_binary_file() -> None:
    fp = reviewer_state.patch_fingerprint
    assert fp(_compare(_file("logo.png", None, sha="x"))) != fp(
        _compare(_file("logo.png", None, sha="y"))
    )


def test_fingerprint_is_none_when_github_omitted_a_text_patch() -> None:
    big = _file("big.py", None, changes=4000)
    assert reviewer_state.patch_fingerprint(_compare(big)) is None


# A text patch GitHub left out for size is fingerprinted by its blob pair instead
# (one very large change would otherwise block every carry): the file's blob at the
# merge base and at the head. Two heads with the same pair make the same change to
# it, exactly.
def _omitted(name: str, head_blob: str, **over: Any) -> dict[str, Any]:
    return _file(name, None, changes=2484, sha=head_blob, **over)


def _with_base(*files: dict[str, Any], merge_base: str = "mb") -> dict[str, Any]:
    return {"files": list(files), "merge_base_commit": {"sha": merge_base}}


def _blobs(table: dict[str, str]) -> Any:
    return lambda path, ref: table.get(f"{path}@{ref}")


def test_an_omitted_patch_fingerprints_by_its_blob_pair() -> None:
    fp = reviewer_state.patch_fingerprint
    one = fp(
        _with_base(_omitted("big.py", "head-blob"), merge_base="mb1"),
        base_blob=_blobs({"big.py@mb1": "base-blob"}),
    )
    two = fp(
        _with_base(_omitted("big.py", "head-blob"), merge_base="mb2"),
        base_blob=_blobs({"big.py@mb2": "base-blob"}),
    )
    assert one is not None and one == two


def test_an_omitted_patch_with_a_different_head_blob_differs() -> None:
    fp = reviewer_state.patch_fingerprint
    blobs = _blobs({"big.py@mb": "base-blob"})
    assert fp(_with_base(_omitted("big.py", "head-1")), base_blob=blobs) != fp(
        _with_base(_omitted("big.py", "head-2")), base_blob=blobs
    )


def test_an_omitted_patch_with_a_different_base_blob_differs() -> None:
    fp = reviewer_state.patch_fingerprint
    head = _omitted("big.py", "head-blob")
    assert fp(
        _with_base(head, merge_base="mb1"), base_blob=_blobs({"big.py@mb1": "b1"})
    ) != fp(_with_base(head, merge_base="mb2"), base_blob=_blobs({"big.py@mb2": "b2"}))


def test_an_omitted_patch_whose_base_blob_is_unknown_is_none() -> None:
    compare = _with_base(_omitted("big.py", "head-blob"))
    assert reviewer_state.patch_fingerprint(compare, base_blob=_blobs({})) is None


def test_an_omitted_patch_without_a_merge_base_is_none() -> None:
    compare = {"files": [_omitted("big.py", "head-blob")]}
    blobs = _blobs({"big.py@mb": "base-blob"})
    assert reviewer_state.patch_fingerprint(compare, base_blob=blobs) is None


def test_a_renamed_omitted_patch_reads_its_old_path_at_the_base() -> None:
    renamed = _omitted(
        "new.py", "head-blob", status="renamed", previous_filename="old.py"
    )
    blobs = _blobs({"old.py@mb": "base-blob"})
    assert reviewer_state.patch_fingerprint(_with_base(renamed), base_blob=blobs)


def test_an_added_omitted_patch_needs_no_base_blob() -> None:
    added = _omitted("new.py", "head-blob", status="added")
    assert reviewer_state.patch_fingerprint(_with_base(added), base_blob=_blobs({}))


def test_a_removed_omitted_patch_is_none() -> None:
    removed = _omitted("gone.py", "blob", status="removed")
    blobs = _blobs({"gone.py@mb": "blob"})
    assert (
        reviewer_state.patch_fingerprint(_with_base(removed), base_blob=blobs) is None
    )


def test_too_many_omitted_patches_is_none() -> None:
    cap = reviewer_state._OMITTED_PATCH_LOOKUP_CAP
    files = [_omitted(f"f{i}.py", "h") for i in range(cap + 1)]
    blobs = _blobs({f"f{i}.py@mb": "b" for i in range(cap + 1)})
    assert reviewer_state.patch_fingerprint(_with_base(*files), base_blob=blobs) is None


def test_fingerprint_is_none_at_the_file_cap() -> None:
    assert reviewer_state._COMPARE_FILE_CAP == 300
    files = [_file(f"f{i}.py", ADD) for i in range(300)]
    assert reviewer_state.patch_fingerprint(_compare(*files)) is None


def test_fingerprint_is_none_without_a_file_list() -> None:
    assert reviewer_state.patch_fingerprint({}) is None


def test_fingerprint_is_none_for_a_payload_that_is_not_an_object() -> None:
    assert reviewer_state.patch_fingerprint([{"files": []}]) is None


def test_fingerprint_is_none_for_a_file_entry_that_is_not_an_object() -> None:
    assert (
        reviewer_state.patch_fingerprint({"files": [_file("a.py", ADD), "x"]}) is None
    )


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"filename": "a.py", "status": "modified", "patch": None, "changes": 0},
        {"filename": "a.py", "status": "modified", "patch": None, "sha": "x"},
        {"filename": "a.py", "status": "modified", "patch": 7, "changes": 1},
        {"status": "modified", "patch": ADD, "changes": 1},
        {"filename": "a.py", "patch": ADD, "changes": 1},
    ],
)
def test_fingerprint_rejects_an_incomplete_file_entry(entry: dict[str, Any]) -> None:
    """Two empty entries must not fingerprint alike."""
    assert reviewer_state.patch_fingerprint({"files": [entry]}) is None


# --- base_at_review -----------------------------------------------------------


def _pushed(at: str, before: str | None) -> dict[str, Any]:
    return {
        "__typename": "BaseRefForcePushedEvent",
        "createdAt": at,
        "beforeCommit": None if before is None else {"oid": before},
    }


def _retargeted(at: str) -> dict[str, Any]:
    return {"__typename": "BaseRefChangedEvent", "createdAt": at}


REVIEWED = "2026-09-30T19:26:57Z"


def _base(events: list[Any], complete: bool = True) -> str | None:
    base: str | None = reviewer_state.base_at_review(
        events, REVIEWED, "cur", complete=complete
    )
    return base


def test_base_is_the_current_one_when_nothing_happened_since() -> None:
    assert _base([_pushed("2026-09-30T19:21:13Z", "old")]) == "cur"


def test_base_is_the_tip_before_the_first_later_force_push() -> None:
    events = [
        _pushed("2026-09-30T21:57:50Z", "b1"),
        _pushed("2026-09-30T22:37:55Z", "b2"),
    ]
    assert _base(events) == "b1"


def test_events_are_ordered_by_time_not_by_list_position() -> None:
    events = [
        _pushed("2026-09-30T22:37:55Z", "b2"),
        _pushed("2026-09-30T21:57:50Z", "b1"),
    ]
    assert _base(events) == "b1"


def test_an_event_at_the_review_second_counts_as_after() -> None:
    assert _base([_pushed(REVIEWED, "b0")]) == "b0"


def test_a_retarget_after_the_review_means_unknown() -> None:
    events = [
        _retargeted("2026-09-30T20:00:00Z"),
        _pushed("2026-09-30T21:00:00Z", "b1"),
    ]
    assert _base(events) is None


def test_a_force_push_without_its_before_commit_means_unknown() -> None:
    assert _base([_pushed("2026-09-30T20:00:00Z", None)]) is None


def test_a_timeline_that_may_not_reach_the_review_means_unknown() -> None:
    assert _base([_pushed("2026-09-30T20:00:00Z", "b1")], complete=False) is None


def test_a_partial_timeline_that_reaches_back_past_the_review_is_enough() -> None:
    events = [
        _pushed("2026-09-30T19:00:00Z", "x"),
        _pushed("2026-09-30T20:00:00Z", "b1"),
    ]
    assert _base(events, complete=False) == "b1"


def test_an_unreadable_timeline_node_means_unknown() -> None:
    """GraphQL nulls a node it can't resolve; that event could be a retarget."""
    events: list[Any] = [None, _pushed("2026-09-30T21:00:00Z", "b1")]
    assert _base(events) is None


# --- collect_state ------------------------------------------------------------

R = "r" * 40  # the reviewed commit
H = "h" * 40  # the new head
CLEAN = "No actionable comments were generated in the recent review. 🎉"
REVIEW_AT = "2026-09-30T19:26:57Z"
LATER = "2026-09-30T21:57:50Z"


def _rabbit_review(commit: str, at: str, body: str = CLEAN) -> dict[str, Any]:
    return {
        "body": body,
        "user": {"login": "coderabbitai[bot]"},
        "commit_id": commit,
        "submitted_at": at,
        "state": "COMMENTED",
    }


def _fake_gh(
    *,
    reviews: list[dict[str, Any]],
    compares: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    base: str = "cur",
    comments: list[dict[str, Any]] | None = None,
    has_previous: bool = False,
    calls: list[str] | None = None,
    blobs: dict[str, str] | None = None,
) -> Any:
    def _gh_json(args: list[str]) -> Any:
        joined = " ".join(args)
        if calls is not None:
            calls.append(joined)
        if "graphql" in args and "timelineItems" in joined:
            timeline = {"pageInfo": {"hasPreviousPage": has_previous}, "nodes": events}
            pull = {"baseRefOid": base, "timelineItems": timeline}
            return {"data": {"repository": {"pullRequest": pull}}}
        if "graphql" in args:
            commit = {"commit": {"committedDate": "2026-09-30T23:00:00Z"}}
            pull = {
                "headRefOid": H,
                "mergeStateStatus": "CLEAN",
                "commits": {"nodes": [commit]},
                "reviewThreads": {"nodes": []},
            }
            return {"data": {"repository": {"pullRequest": pull}}}
        if "/compare/" in joined:
            return compares[joined.split("/compare/", 1)[1].split("?", 1)[0]]
        if "/contents/" in joined:
            path, ref = joined.split("/contents/", 1)[1].split("?ref=", 1)
            sha = (blobs or {}).get(f"{path}@{ref}")
            if sha is None:  # what `gh api` does on a 404
                raise subprocess.CalledProcessError(1, args)
            return {"type": "file", "path": path, "sha": sha}
        if joined.endswith("/reviews --paginate"):
            return reviews
        if joined.endswith("/comments --paginate"):
            return comments or []
        if "/statuses" in joined:
            return []
        raise AssertionError(f"unexpected gh call: {args}")

    return _gh_json


def _collect(monkeypatch: Any, **fake: Any) -> dict[str, Any]:
    monkeypatch.setattr(reviewer_state, "_gh_json", _fake_gh(**fake))
    state: dict[str, Any] = reviewer_state.collect_state(REPO, 7, CODERABBIT_PR)
    return state


def test_a_rebase_only_head_carries_the_verdict(monkeypatch: Any) -> None:
    """The base was force-pushed after the review; the PR's own diff is unchanged."""
    state = _collect(
        monkeypatch,
        reviews=[_rabbit_review(R, REVIEW_AT)],
        events=[_pushed(LATER, "b1")],
        compares={
            f"b1...{R}": _compare(_file("a.py", ADD)),
            f"cur...{H}": _compare(_file("a.py", ADD_SHIFTED)),
        },
    )
    assert (state["verdict"], state["reported_finding_count"]) == ("pass", 0)
    assert state["verdict_carried_from"] == R
    assert state["verdict_carry_reason"] == "same_patch"
    assert state["needs_trigger"] is False


def test_a_carried_findings_verdict_keeps_its_count(monkeypatch: Any) -> None:
    state = _collect(
        monkeypatch,
        reviews=[_rabbit_review(R, REVIEW_AT, body="Actionable comments posted: 2")],
        events=[_pushed(LATER, "b1")],
        compares={
            f"b1...{R}": _compare(_file("a.py", ADD)),
            f"cur...{H}": _compare(_file("a.py", ADD)),
        },
    )
    assert (state["verdict"], state["reported_finding_count"]) == ("findings", 2)


def test_a_one_line_change_needs_a_fresh_review(monkeypatch: Any) -> None:
    extra = "@@ -10,3 +10,5 @@ def f():\n ctx\n+new line\n+one more\n ctx\n"
    state = _collect(
        monkeypatch,
        reviews=[_rabbit_review(R, REVIEW_AT)],
        events=[_pushed(LATER, "b1")],
        compares={
            f"b1...{R}": _compare(_file("a.py", ADD)),
            f"cur...{H}": _compare(_file("a.py", extra)),
        },
    )
    assert state["verdict"] == "none"
    assert state["verdict_carried_from"] is None
    assert state["verdict_carry_reason"] is None
    assert state["needs_trigger"] is True


def test_a_conflict_resolution_that_edits_a_line_needs_a_fresh_review(
    monkeypatch: Any,
) -> None:
    state = _collect(
        monkeypatch,
        reviews=[_rabbit_review(R, REVIEW_AT)],
        events=[_pushed(LATER, "b1")],
        compares={
            f"b1...{R}": _compare(_file("a.py", ADD)),
            f"cur...{H}": _compare(_file("a.py", ADD_EDITED)),
        },
    )
    assert state["verdict"] == "none"
    assert state["needs_trigger"] is True


def test_a_clean_merge_from_main_carries_the_verdict(monkeypatch: Any) -> None:
    """A single PR on main: no base events, so both sides diff from main's tip."""
    state = _collect(
        monkeypatch,
        reviews=[_rabbit_review(R, REVIEW_AT)],
        events=[],
        base="main-tip",
        compares={
            f"main-tip...{R}": _compare(_file("a.py", ADD)),
            f"main-tip...{H}": _compare(_file("a.py", ADD_SHIFTED)),
        },
    )
    assert (state["verdict"], state["verdict_carry_reason"]) == ("pass", "same_patch")


BIG = "src/app/big_module.py"


def test_a_clean_merge_with_a_size_omitted_patch_carries_by_blob_pair(
    monkeypatch: Any,
) -> None:
    """GitHub omits a big file's patch on both sides; its blobs still match."""
    state = _collect(
        monkeypatch,
        reviews=[_rabbit_review(R, REVIEW_AT)],
        events=[],
        base="main-tip",
        compares={
            f"main-tip...{R}": _with_base(
                _file("a.py", ADD), _omitted(BIG, "head-blob"), merge_base="old-mb"
            ),
            f"main-tip...{H}": _with_base(
                _file("a.py", ADD_SHIFTED),
                _omitted(BIG, "head-blob"),
                merge_base="main-tip",
            ),
        },
        blobs={f"{BIG}@old-mb": "base-blob", f"{BIG}@main-tip": "base-blob"},
    )
    assert (state["verdict"], state["verdict_carry_reason"]) == ("pass", "same_patch")
    assert state["needs_trigger"] is False


def test_a_size_omitted_patch_that_changed_is_not_carried(monkeypatch: Any) -> None:
    state = _collect(
        monkeypatch,
        reviews=[_rabbit_review(R, REVIEW_AT)],
        events=[],
        base="main-tip",
        compares={
            f"main-tip...{R}": _with_base(_omitted(BIG, "head-1"), merge_base="old-mb"),
            f"main-tip...{H}": _with_base(
                _omitted(BIG, "head-2"), merge_base="main-tip"
            ),
        },
        blobs={f"{BIG}@old-mb": "base-blob", f"{BIG}@main-tip": "base-blob"},
    )
    assert state["verdict"] == "none"
    assert state["needs_trigger"] is True


def test_a_size_omitted_patch_whose_base_blob_is_missing_is_not_carried(
    monkeypatch: Any,
) -> None:
    """The contents API 404s (``gh`` exits non-zero): no carry, no crash."""
    state = _collect(
        monkeypatch,
        reviews=[_rabbit_review(R, REVIEW_AT)],
        events=[],
        base="main-tip",
        compares={
            f"main-tip...{R}": _with_base(_omitted(BIG, "h"), merge_base="old-mb"),
            f"main-tip...{H}": _with_base(_omitted(BIG, "h"), merge_base="main-tip"),
        },
        blobs={},
    )
    assert state["verdict"] == "none"


def test_a_merge_from_main_whose_resolution_edits_code_is_not_carried(
    monkeypatch: Any,
) -> None:
    state = _collect(
        monkeypatch,
        reviews=[_rabbit_review(R, REVIEW_AT)],
        events=[],
        base="main-tip",
        compares={
            f"main-tip...{R}": _compare(_file("a.py", ADD)),
            f"main-tip...{H}": _compare(_file("a.py", ADD_EDITED)),
        },
    )
    assert state["verdict"] == "none"


def test_a_retarget_after_the_review_is_not_carried(monkeypatch: Any) -> None:
    calls: list[str] = []
    state = _collect(
        monkeypatch,
        reviews=[_rabbit_review(R, REVIEW_AT)],
        events=[_retargeted(LATER)],
        compares={},
        calls=calls,
    )
    assert state["verdict"] == "none"
    assert not any("/compare/" in c for c in calls)


def test_no_carry_when_the_timeline_payload_is_missing(monkeypatch: Any) -> None:
    """A fake that answers every GraphQL call with the threads payload."""

    def _gh_json(args: list[str]) -> Any:
        if "graphql" in args:
            pull = {
                "headRefOid": H,
                "mergeStateStatus": "CLEAN",
                "reviewThreads": {"nodes": []},
            }
            return {"data": {"repository": {"pullRequest": pull}}}
        if any(a.endswith("/reviews") for a in args):
            return [_rabbit_review(R, REVIEW_AT)]
        return []

    monkeypatch.setattr(reviewer_state, "_gh_json", _gh_json)
    state = reviewer_state.collect_state(REPO, 1, CODERABBIT_PR)
    assert state["verdict"] == "none"
    assert state["needs_trigger"] is True


def test_a_gh_error_means_no_carry(monkeypatch: Any) -> None:
    inner = _fake_gh(reviews=[_rabbit_review(R, REVIEW_AT)], events=[], compares={})

    def _gh_json(args: list[str]) -> Any:
        if any("/compare/" in a for a in args):
            raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 404")
        return inner(args)

    monkeypatch.setattr(reviewer_state, "_gh_json", _gh_json)
    state = reviewer_state.collect_state(REPO, 1, CODERABBIT_PR)
    assert state["verdict"] == "none"


def test_non_json_gh_output_means_no_carry(monkeypatch: Any) -> None:
    inner = _fake_gh(reviews=[_rabbit_review(R, REVIEW_AT)], events=[], compares={})

    def _gh_json(args: list[str]) -> Any:
        if any("/compare/" in a for a in args):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return inner(args)

    monkeypatch.setattr(reviewer_state, "_gh_json", _gh_json)
    state = reviewer_state.collect_state(REPO, 1, CODERABBIT_PR)
    assert state["verdict"] == "none"


def test_odd_payload_shapes_mean_no_carry(monkeypatch: Any) -> None:
    """A list where an object belongs must not crash the gate."""
    inner = _fake_gh(reviews=[_rabbit_review(R, REVIEW_AT)], events=[], compares={})

    def _gh_json(args: list[str]) -> Any:
        if "timelineItems" in " ".join(args):
            return [{"data": {}}, {"data": {}}]
        return inner(args)

    monkeypatch.setattr(reviewer_state, "_gh_json", _gh_json)
    state = reviewer_state.collect_state(REPO, 1, CODERABBIT_PR)
    assert state["verdict"] == "none"


def test_a_compare_that_is_not_an_object_means_no_carry(monkeypatch: Any) -> None:
    inner = _fake_gh(reviews=[_rabbit_review(R, REVIEW_AT)], events=[], compares={})

    def _gh_json(args: list[str]) -> Any:
        if any("/compare/" in a for a in args):
            return [{"files": []}]
        return inner(args)

    monkeypatch.setattr(reviewer_state, "_gh_json", _gh_json)
    state = reviewer_state.collect_state(REPO, 1, CODERABBIT_PR)
    assert state["verdict"] == "none"


@pytest.mark.parametrize(
    "timeline",
    [
        {"nodes": []},
        {"pageInfo": None, "nodes": []},
        {"pageInfo": {}, "nodes": []},
        {"pageInfo": {"hasPreviousPage": None}, "nodes": []},
        {"pageInfo": {"hasPreviousPage": 0}, "nodes": []},
    ],
)
def test_a_timeline_without_a_paging_flag_means_no_carry(
    monkeypatch: Any, timeline: dict[str, Any]
) -> None:
    """An unknown page state is not a complete timeline."""
    inner = _fake_gh(
        reviews=[_rabbit_review(R, REVIEW_AT)],
        events=[],
        compares={
            f"cur...{R}": _compare(_file("a.py", ADD)),
            f"cur...{H}": _compare(_file("a.py", ADD)),
        },
    )

    def _gh_json(args: list[str]) -> Any:
        if "timelineItems" in " ".join(args):
            pull = {"baseRefOid": "cur", "timelineItems": timeline}
            return {"data": {"repository": {"pullRequest": pull}}}
        return inner(args)

    monkeypatch.setattr(reviewer_state, "_gh_json", _gh_json)
    state = reviewer_state.collect_state(REPO, 1, CODERABBIT_PR)
    assert state["verdict"] == "none"


def test_a_partial_timeline_reaching_back_past_the_review_still_carries(
    monkeypatch: Any,
) -> None:
    """``hasPreviousPage`` true, but the oldest listed event predates the review."""
    state = _collect(
        monkeypatch,
        reviews=[_rabbit_review(R, REVIEW_AT)],
        events=[_pushed("2026-09-30T10:00:00Z", "x"), _pushed(LATER, "b1")],
        has_previous=True,
        compares={
            f"b1...{R}": _compare(_file("a.py", ADD)),
            f"cur...{H}": _compare(_file("a.py", ADD)),
        },
    )
    assert state["verdict_carry_reason"] == "same_patch"


def test_a_head_with_its_own_verdict_skips_the_carry_check(monkeypatch: Any) -> None:
    calls: list[str] = []
    state = _collect(
        monkeypatch,
        reviews=[_rabbit_review(R, REVIEW_AT), _rabbit_review(H, LATER)],
        events=[],
        compares={},
        calls=calls,
    )
    assert state["verdict"] == "pass"
    assert state["verdict_carried_from"] is None
    assert state["verdict_carry_reason"] is None
    assert not any("timelineItems" in c or "/compare/" in c for c in calls)


def test_only_the_newest_verdict_is_a_source(monkeypatch: Any) -> None:
    """An older review whose diff matches does not stand in for a newer one."""
    older = "o" * 40
    state = _collect(
        monkeypatch,
        reviews=[
            _rabbit_review(older, "2026-09-30T10:00:00Z"),
            _rabbit_review(R, REVIEW_AT),
        ],
        events=[],
        compares={
            f"cur...{R}": _compare(_file("a.py", ADD_EDITED)),
            f"cur...{H}": _compare(_file("a.py", ADD)),
        },
    )
    assert state["verdict"] == "none"


def test_the_base_is_read_as_of_the_earliest_review_on_the_source(
    monkeypatch: Any,
) -> None:
    """A later re-review of the same commit doesn't move the base it was read on."""
    state = _collect(
        monkeypatch,
        reviews=[
            _rabbit_review(R, REVIEW_AT),
            _rabbit_review(R, "2026-09-30T23:30:00Z"),
        ],
        events=[_pushed(LATER, "b1")],
        compares={
            f"b1...{R}": _compare(_file("a.py", ADD)),
            f"cur...{H}": _compare(_file("a.py", ADD)),
        },
    )
    assert state["verdict_carry_reason"] == "same_patch"


# --- carry_source ---------------------------------------------------------------


def test_carry_source_is_the_newest_reviewed_commit_with_a_verdict() -> None:
    older = "o" * 40
    reviews = [
        _rabbit_review(
            older, "2026-09-30T10:00:00Z", body="Actionable comments posted: 1"
        ),
        _rabbit_review(R, REVIEW_AT, body="Actionable comments posted: 3"),
        _rabbit_review(R, LATER),
    ]
    source = reviewer_state.carry_source(reviews, H, CODERABBIT, [])
    assert source == reviewer_state.CarrySource(R, "pass", 0, REVIEW_AT)


def test_carry_source_skips_the_head_and_respects_before() -> None:
    reviews = [_rabbit_review(H, REVIEW_AT), _rabbit_review(R, LATER)]
    source = reviewer_state.carry_source(reviews, H, CODERABBIT, [], before=REVIEW_AT)
    assert source is None


def test_carry_source_skips_a_commit_with_no_verdict() -> None:
    a, b = "a" * 40, "b" * 40
    reviews = [_rabbit_review(a, "2026-09-30T10:00:00Z"), _rabbit_review(b, LATER, "")]
    source = reviewer_state.carry_source(reviews, H, CODERABBIT, [])
    assert source is not None and source.sha == a


# --- The "no files to review" carry ------------------------------------------------

_NO_CHANGES_REPLY = (
    "<!-- This is an auto-generated reply by CodeRabbit -->\n"
    "CodeRabbit is an incremental review system ... No files to review."
)
_NO_CHANGES_EXCHANGE = [
    {
        "body": "@coderabbitai review",
        "user": {"login": "octocat"},
        "created_at": "2026-09-30T23:10:00Z",
    },
    {
        "body": _NO_CHANGES_REPLY,
        "user": {"login": "coderabbitai[bot]"},
        "created_at": "2026-09-30T23:10:05Z",
        "updated_at": "2026-09-30T23:10:09Z",
    },
]


def test_no_changes_carry_reports_its_source(monkeypatch: Any) -> None:
    calls: list[str] = []
    state = _collect(
        monkeypatch,
        reviews=[_rabbit_review(R, REVIEW_AT)],
        comments=_NO_CHANGES_EXCHANGE,
        events=[],
        compares={},
        calls=calls,
    )
    assert state["incremental_review_outcome"] == "no_changes"
    assert state["verdict"] == "pass"
    assert state["verdict_carried_from"] == R
    assert state["verdict_carry_reason"] == "no_changes"
    assert state["needs_trigger"] is False
    assert not any("timelineItems" in c for c in calls)


def test_a_no_changes_reply_with_nothing_to_carry_still_spends_a_round(
    monkeypatch: Any,
) -> None:
    """With no verdict to carry, re-triggering would get the same reply forever."""
    state = _collect(
        monkeypatch,
        reviews=[],
        comments=_NO_CHANGES_EXCHANGE,
        events=[],
        compares={},
    )
    assert state["verdict"] == "none"
    assert state["verdict_carried_from"] is None
    assert state["round_budget"]["incremental_before_final"]["used"] == 1


def test_a_carried_no_changes_reply_spends_no_round(monkeypatch: Any) -> None:
    state = _collect(
        monkeypatch,
        reviews=[_rabbit_review(R, REVIEW_AT)],
        comments=_NO_CHANGES_EXCHANGE,
        events=[],
        compares={},
    )
    assert state["verdict_carry_reason"] == "no_changes"
    assert state["round_budget"]["incremental_before_final"]["used"] == 0


def test_a_no_changes_carry_ignores_reviews_after_the_reply(monkeypatch: Any) -> None:
    """The source is the newest verdict the reviewer had when it answered."""
    late = "l" * 40
    state = _collect(
        monkeypatch,
        reviews=[
            _rabbit_review(R, REVIEW_AT),
            _rabbit_review(
                late, "2026-09-30T23:20:00Z", "Actionable comments posted: 4"
            ),
        ],
        comments=_NO_CHANGES_EXCHANGE,
        events=[],
        compares={},
    )
    assert state["verdict_carried_from"] == R
    assert state["verdict_carry_reason"] == "no_changes"
