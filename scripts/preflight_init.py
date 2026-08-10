#!/usr/bin/env python3
"""Validate a template checkout before any initialization mutation."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.init_template import ensure_clean_worktree


def repo_from_remote(url: str) -> str:
    """Extract ``owner/repository`` from a GitHub remote URL.

    Parameters
    ----------
    url
        HTTPS or SSH GitHub remote URL.

    Returns
    -------
    str
        Normalized GitHub repository name.

    Raises
    ------
    RuntimeError
        If the remote is not a recognized GitHub URL.
    """
    value = url.strip()
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:)(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?",
        value,
    )
    if match is None:
        raise RuntimeError(f"origin is not a recognized GitHub repository URL: {value}")
    return match.group("repo")


def _run(
    args: list[str],
    runner: Callable[..., Any],
    root: Path,
) -> Any:
    """Run one preflight command with a concise failure message."""
    try:
        return runner(
            args,
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "No error detail returned.").strip()
        raise RuntimeError(f"`{' '.join(args)}` failed: {detail}") from exc


def run_preflight(
    root: Path | str,
    repo: str,
    stack: str,
    runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, str]:
    """Validate tools, local state, target identity, and GitHub permissions.

    Parameters
    ----------
    root
        Template repository root.
    repo
        Explicit GitHub target in ``owner/repository`` form.
    stack
        Selected stack, ``python`` or ``react``.
    runner
        Subprocess-compatible command runner, by default ``subprocess.run``.
    which
        Executable lookup function, by default ``shutil.which``.

    Returns
    -------
    dict[str, str]
        Confirmed account, repository, and stack.

    Raises
    ------
    RuntimeError
        If any guard fails.
    """
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
        raise RuntimeError("TARGET_REPO must use owner/repository form.")
    if stack not in {"python", "react"}:
        raise RuntimeError("STACK must be either python or react.")

    required = ["git", "gh", "python3", "uv" if stack == "python" else "bun"]
    missing = [command for command in required if which(command) is None]
    if missing:
        raise RuntimeError(
            "Required command(s) not found on PATH: " + ", ".join(missing) + "."
        )

    root = Path(root)
    ensure_clean_worktree(root, runner=runner)
    _run(["gh", "auth", "status"], runner, root)

    origin_url = str(
        _run(["git", "remote", "get-url", "origin"], runner, root).stdout
    ).strip()
    origin_repo = repo_from_remote(origin_url)
    if origin_repo.casefold() != repo.casefold():
        raise RuntimeError(
            f"origin points to {origin_repo}, but TARGET_REPO is {repo}. "
            "Fix the remotes or choose the intended target before continuing."
        )

    account = json.loads(_run(["gh", "api", "user"], runner, root).stdout)["login"]
    repo_data = json.loads(_run(["gh", "api", f"repos/{repo}"], runner, root).stdout)
    actual_repo = str(repo_data.get("full_name", ""))
    if actual_repo.casefold() != repo.casefold():
        raise RuntimeError(
            f"GitHub returned {actual_repo or 'an unknown repository'} for target {repo}."
        )
    if not repo_data.get("permissions", {}).get("admin", False):
        raise RuntimeError(
            f"GitHub account {account} does not have administrator permission for {repo}."
        )
    if not repo_data.get("has_issues", False):
        raise RuntimeError(
            f"GitHub Issues are disabled for {repo}. Enable Issues before initialization."
        )

    return {"account": str(account), "repo": actual_repo, "stack": stack}


def main(argv: list[str] | None = None) -> int:
    """Run the initialization preflight CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="target in owner/repository form")
    parser.add_argument("--stack", required=True, choices=("python", "react"))
    args = parser.parse_args(argv)
    try:
        result = run_preflight(Path.cwd(), args.repo, args.stack)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 2
    print("Initialization preflight passed:")
    print(f"  GitHub account: {result['account']}")
    print(f"  Target repository: {result['repo']}")
    print(f"  Selected stack: {result['stack']}")
    print("  Working tree: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
