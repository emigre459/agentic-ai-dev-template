"""Repo-hygiene guardrail.

Invariants enforced in CI so a stray or misplaced file fails the build instead
of silently landing on the default branch:

1. The repo ROOT holds only an explicit allowlist of files. The root is a
   small, stable, enumerable set, so an allowlist (closed by default) catches
   even scratch names nobody anticipated -- unlike a pattern denylist.
2. NO tracked file is shadowed by a ``.gitignore`` rule. A tracked-but-ignored
   file is committed yet a ``git add`` of it would be silently skipped.

All checks read git's own view (``git ls-files``), never a raw filesystem walk,
so local scratch and gitignored files never trip them.
"""

from __future__ import annotations

import subprocess  # nosec B404 -- fixed argv, in-repo git, no shell
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The ONLY files permitted at the repository root. Adding one is a deliberate,
# reviewed act: append it here WITH a one-line justification in the same change.
ALLOWED_ROOT_FILES = frozenset(
    {
        ".gitignore",
        ".mcp.json",
        ".python-version",
        "AGENTS.md",
        "CHANGELOG.md",
        "CLAUDE.md",
        "Makefile",
        "README.md",
        "pyproject.toml",
        "uv.lock",
    }
)


def _git(*args: str) -> str:
    """Run a git command in the repo and return its stdout."""
    return subprocess.run(  # nosec B603 B607 -- fixed argv, in-repo git, no shell
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _tracked_files() -> list[str]:
    """Every tracked path, repo-relative (NUL-split to survive odd names)."""
    return [p for p in _git("ls-files", "-z").split("\0") if p]


def _tracked_root_files() -> set[str]:
    """Tracked files at the repository root (depth 1 only)."""
    return {p for p in _tracked_files() if "/" not in p}


def _gitignored_tracked_files() -> list[str]:
    """Tracked files whose FINAL matching .gitignore rule is a real ignore.

    ``--no-index`` is essential: without it check-ignore consults the index and
    reports every tracked path as not-ignored, hiding the contradiction. Exit
    status 0 = at least one ignored, 1 = none ignored (neither is an error).
    The non-verbose output already excludes paths whose final match is a
    negation, so it is exactly the genuine-ignore set.
    """
    tracked = _tracked_files()
    if not tracked:
        return []
    proc = subprocess.run(  # nosec B603 B607 -- fixed argv, in-repo git, no shell
        ["git", "check-ignore", "--no-index", "--stdin", "-z"],
        cwd=REPO_ROOT,
        input="\0".join(tracked) + "\0",
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"git check-ignore failed ({proc.returncode}): {proc.stderr}"
        )
    return [p for p in proc.stdout.split("\0") if p]


def _is_ignored(rel_path: str) -> bool:
    """Return whether ``rel_path`` would be gitignored, ignoring index state."""
    proc = subprocess.run(  # nosec B603 B607 -- fixed argv, in-repo git, no shell
        ["git", "check-ignore", "--no-index", "-q", rel_path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"git check-ignore failed ({proc.returncode}): {proc.stderr}"
        )
    return proc.returncode == 0


def test_no_unexpected_root_files() -> None:
    offenders = sorted(_tracked_root_files() - ALLOWED_ROOT_FILES)
    assert not offenders, (
        "Unexpected file(s) tracked at the repo root:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe repo root is an allowlist. For each offender: move it to the "
        "correct subdirectory, remove it if it is scratch, or -- if it is "
        "genuinely a new legitimate root file -- add it to ALLOWED_ROOT_FILES "
        "in tests/template/test_root_hygiene.py with a one-line justification."
    )


def test_allowlist_has_no_stale_entries() -> None:
    tracked_root = _tracked_root_files()
    stale = sorted(name for name in ALLOWED_ROOT_FILES if name not in tracked_root)
    assert not stale, (
        "ALLOWED_ROOT_FILES names file(s) no longer tracked at the root:\n  "
        + "\n  ".join(stale)
        + "\n\nRemove the stale entry(ies) so the allowlist stays in sync with "
        "reality (a removed root file must also drop its allowlist line)."
    )


def test_no_tracked_file_is_gitignored() -> None:
    offenders = sorted(_gitignored_tracked_files())
    assert not offenders, (
        "Tracked file(s) are shadowed by a .gitignore rule -- committed, yet a "
        "`git add` of them would be silently skipped:\n  "
        + "\n  ".join(offenders)
        + "\n\nEither add a .gitignore negation so the file is re-included (if "
        "it is legitimate, e.g. a fixture under a data/ tree), or remove the "
        "file (if it does not belong in the repo)."
    )


def test_root_scratch_patterns_are_root_anchored() -> None:
    """The scratch families are ignored at the ROOT but not when nested."""
    for rel in ("HANDOFF.md", "SPEC_probe.md", "run_probe.sh"):
        assert _is_ignored(
            rel
        ), f"root-level {rel} should be gitignored by the scratch patterns"
    for rel in ("docs/SPEC_probe.md", "scripts/run_probe.sh"):
        assert not _is_ignored(rel), (
            f"nested {rel} must NOT be gitignored -- the scratch patterns are "
            "root-anchored (leading /) precisely so nested files are safe"
        )
