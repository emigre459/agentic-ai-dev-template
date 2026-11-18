#!/usr/bin/env python3
"""Check that the skills CLI can install every skill this repo publishes.

Runs the same call the nightly skills update makes
(``npx -y skills@<version> add <source> --skill <n> ... -a universal -y``) with
this checkout as the source, in a fresh throwaway git project, and exits 0 only
if every skill named in ``.github/template-skills-lock.json`` lands at
``.agents/skills/<name>/SKILL.md``. It guards against whatever makes a release
uninstallable (a root ``skills-lock.json`` hiding the skills, invalid
frontmatter). It installs from the local checkout; the GitHub
``owner/repo#tag`` source path is covered by the live test kit.

Set ``NPX_CMD`` to substitute the ``npx`` executable (the test seam) and
``SKILLS_INSTALL_TIMEOUT`` (seconds, default 600) to bound the install.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- fixed argv, no shell
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_TAIL_LINES = 40
_DEFAULT_TIMEOUT_S = 600


class LockError(Exception):
    """The template skills lock is missing, unreadable, or malformed."""


def skill_names(repo: Path = REPO_ROOT) -> list[str]:
    """Return the skill names in the template's skills lock.

    Parameters
    ----------
    repo : Path
        Repository root.

    Returns
    -------
    list[str]
        The keys of ``skills`` in ``.github/template-skills-lock.json``.
    """
    path = repo / ".github" / "template-skills-lock.json"
    try:
        lock = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise LockError(f"cannot read {path}: {exc}") from exc
    skills = lock.get("skills") if isinstance(lock, dict) else None
    if not isinstance(skills, dict):
        raise LockError(f"{path} has no 'skills' object")
    return list(skills)


def cli_version(repo: Path = REPO_ROOT) -> str:
    """Return the pinned skills CLI version.

    Parameters
    ----------
    repo : Path
        Repository root.

    Returns
    -------
    str
        ``dependencies.skills`` from ``.github/actions/skills-cli/package.json``.
    """
    pkg = json.loads(
        (repo / ".github" / "actions" / "skills-cli" / "package.json").read_text()
    )
    return str(pkg["dependencies"]["skills"])


def main() -> int:
    """Install every skill into a temp project and verify each landed.

    Returns
    -------
    int
        0 if all skills installed, 1 otherwise.
    """
    try:
        names = skill_names()
    except LockError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not names:
        print(
            "the lock lists no skills (.github/template-skills-lock.json)",
            file=sys.stderr,
        )
        return 1
    raw_timeout = os.environ.get("SKILLS_INSTALL_TIMEOUT", str(_DEFAULT_TIMEOUT_S))
    try:
        timeout = float(raw_timeout)
    except ValueError:
        print(
            f"SKILLS_INSTALL_TIMEOUT must be a number of seconds, got {raw_timeout!r}",
            file=sys.stderr,
        )
        return 1
    argv = [
        os.environ.get("NPX_CMD", "npx"),
        "-y",
        f"skills@{cli_version()}",
        "add",
        str(REPO_ROOT),
        *[arg for name in names for arg in ("--skill", name)],
        "-a",
        "universal",
        "-y",
    ]
    env = {**os.environ, "DISABLE_TELEMETRY": "1"}
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        subprocess.run(  # nosec B603 B607 -- fixed argv, no shell
            ["git", "init", "-q"], cwd=project, check=True
        )
        try:
            proc = subprocess.run(  # nosec B603 B607 -- fixed argv, no shell
                argv,
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except FileNotFoundError:
            print(f"npx not found: {argv[0]}", file=sys.stderr)
            return 1
        except subprocess.TimeoutExpired:
            print(f"skills CLI timed out after {timeout:g}s", file=sys.stderr)
            return 1
        output = (proc.stdout + proc.stderr).strip()
        tail = "\n".join(output.splitlines()[-_TAIL_LINES:])
        missing = [
            n
            for n in names
            if not (project / ".agents" / "skills" / n / "SKILL.md").is_file()
        ]
    if proc.returncode != 0 or missing:
        print(
            f"skills CLI exit code {proc.returncode}; "
            f"missing skills: {', '.join(missing) or 'none'}",
            file=sys.stderr,
        )
        print(f"--- CLI output (tail) ---\n{tail}", file=sys.stderr)
        return 1
    print(f"OK: all {len(names)} skills installed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
