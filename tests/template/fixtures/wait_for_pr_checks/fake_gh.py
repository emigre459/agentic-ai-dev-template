"""A stand-in ``gh`` for the ``wait_for_pr_checks.py`` tests.

``answer`` maps one ``gh`` argv onto a scenario dict; the tests call it in-process
(behind a patched ``subprocess.run``) and, through ``main``, as a real ``gh`` on
``PATH``. Scenario keys, all optional:

``checks``
    The PR's checks (``name`` / ``bucket`` / ``state`` / ``link``), served to every
    ``gh pr checks`` read; ``None`` makes the read fail like gh's "no checks reported".
``merge``
    ``"<mergeable>/<mergeStateStatus>"`` per ``gh pr view --json mergeable,...``
    read, in order; the last one repeats. Defaults to ``MERGEABLE/CLEAN``.
``head``
    The PR's head SHA (``gh pr view --json headRefOid``).
``repo``
    ``owner/name`` for ``gh repo view``; absent, that read fails.
``thread_pages`` / ``review_pages`` / ``comment_pages``
    Pages of review-thread nodes, REST reviews and issue comments. With
    ``--paginate`` every page is printed, one JSON document per page (as an older
    ``gh`` does); without it only the first.
``fail``
    Argv prefixes (joined by spaces) whose reads exit 1.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _flag(argv: list[str], name: str) -> str | None:
    """Return the value after ``name`` in ``argv``, if any."""
    if name in argv[:-1]:
        return argv[argv.index(name) + 1]
    return None


def _fields(checks: list[dict[str, Any]], fields: str) -> list[dict[str, Any]]:
    wanted = fields.split(",")
    return [{k: c.get(k) for k in wanted} for c in checks]


def _table(checks: list[dict[str, Any]]) -> tuple[int, str, str]:
    rows = "".join(f"{c['name']}\t{c['bucket']}\n" for c in checks)
    buckets = {c["bucket"] for c in checks}
    # gh exits 1 on a failure and 8 on a pending check, even when it prints a table.
    code = 1 if "fail" in buckets else 8 if "pending" in buckets else 0
    return code, rows, ""


def _pages(pages: list[Any], paginate: bool) -> str:
    shown = pages if paginate else pages[:1]
    return "".join(json.dumps(page) + "\n" for page in shown) or "[]\n"


def _thread_page(nodes: list[Any], last: bool) -> dict[str, Any]:
    threads = {
        "pageInfo": {"hasNextPage": not last, "endCursor": None if last else "c"},
        "nodes": nodes,
    }
    return {"data": {"repository": {"pullRequest": {"reviewThreads": threads}}}}


def answer(
    scenario: dict[str, Any], argv: list[str], state: dict[str, Any]
) -> tuple[int, str, str]:
    """Answer one ``gh`` call from ``scenario``.

    Parameters
    ----------
    scenario : dict[str, Any]
        What the fake PR looks like (see the module docstring).
    argv : list[str]
        The arguments after ``gh``.
    state : dict[str, Any]
        Mutable call state (the merge-state read count), kept across calls.

    Returns
    -------
    tuple[int, str, str]
        Exit code, stdout and stderr.
    """
    joined = " ".join(argv)
    for prefix in scenario.get("fail", []):
        if joined.startswith(prefix):
            return 1, "", f"fake gh: {prefix} failed"
    paginate = "--paginate" in argv
    json_fields = _flag(argv, "--json")
    if argv[:2] == ["pr", "checks"]:
        checks = scenario.get("checks", [])
        if checks is None:
            return 1, "", "no checks reported on the 'topic' branch"
        if json_fields is None:
            return _table(checks)
        return 0, json.dumps(_fields(checks, json_fields)), ""
    if argv[:2] == ["pr", "view"] and json_fields == "mergeable,mergeStateStatus":
        sequence = scenario.get("merge") or ["MERGEABLE/CLEAN"]
        read = state.get("merge_reads", 0)
        state["merge_reads"] = read + 1
        mergeable, status = sequence[min(read, len(sequence) - 1)].split("/")
        doc = {"mergeable": mergeable, "mergeStateStatus": status}
        return 0, json.dumps(doc), ""
    if argv[:2] == ["pr", "view"] and json_fields == "headRefOid":
        return 0, json.dumps({"headRefOid": scenario.get("head", "")}), ""
    if argv[:2] == ["repo", "view"]:
        if "repo" not in scenario:
            return 1, "", "not a repository"
        return 0, json.dumps({"nameWithOwner": scenario["repo"]}), ""
    if argv[:2] == ["api", "graphql"]:
        pages = scenario.get("thread_pages") or [[]]
        docs = [_thread_page(p, i == len(pages) - 1) for i, p in enumerate(pages)]
        return 0, _pages(docs, paginate), ""
    if len(argv) > 1 and argv[0] == "api" and argv[1].endswith("/reviews"):
        return 0, _pages(scenario.get("review_pages", []), paginate), ""
    if len(argv) > 1 and argv[0] == "api" and argv[1].endswith("/comments"):
        return 0, _pages(scenario.get("comment_pages", []), paginate), ""
    return 1, "", f"fake gh: unexpected call: {joined}"


def main() -> int:
    """Serve one call from ``$FAKE_GH_SCENARIO``, logging argv to ``$FAKE_GH_LOG``.

    Returns
    -------
    int
        The fake call's exit code.
    """
    scenario_path = Path(os.environ["FAKE_GH_SCENARIO"])
    state_path = scenario_path.with_suffix(".state.json")
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    argv = sys.argv[1:]
    with Path(os.environ["FAKE_GH_LOG"]).open("a") as log:
        log.write(json.dumps(argv) + "\n")
    code, out, err = answer(json.loads(scenario_path.read_text()), argv, state)
    state_path.write_text(json.dumps(state))
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


if __name__ == "__main__":
    sys.exit(main())
