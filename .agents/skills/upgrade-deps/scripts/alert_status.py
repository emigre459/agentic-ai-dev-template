#!/usr/bin/env python3
"""Report whether each open security alert is actually fixed on this tree.

Reads the repo's open Dependabot alerts (``gh api``) or a saved copy
(``--alerts-file``), finds each alert's lockfile, and tests every version of the
package locked there against the alert's vulnerable range. An alert is ``cleared``
only when no locked copy -- top-level or nested -- is still in range; the Dependabot
UI only catches up after the fix reaches the default branch, so this is the check to
run on a refresh branch.

Prints JSON: ``alerts`` (one entry per alert: ``number``, ``severity``, ``package``,
``lockfile``, ``locked_versions``, ``vulnerable_range``, ``first_patched``,
``status``), plus ``open`` and ``unknown`` counts. ``status`` is ``cleared``,
``open``, or ``unknown`` (the lockfile or the range couldn't be read).

Exit codes: 0 every alert cleared; 1 any alert open or unknown; 64 usage error or a
failed ``gh`` call. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404 -- fixed gh argv, no shell
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

#: ``argv -> stdout``; raises ``GhError`` when the command fails.
Runner = Callable[[Sequence[str]], str]

_LOCKFILES = ("uv.lock", "bun.lock")
_CLAUSE = re.compile(r"^\s*(<=|>=|<|>|=)\s*(\S+)\s*$")
_RELEASE = re.compile(r"^v?(\d+(?:\.\d+)*)(.*)$")
_POST = re.compile(r"^[.-]?(?:post|rev|r)\d*$", re.IGNORECASE)
# A bun.lock package entry: `"<key>": ["<name>@<version>", ...`
_BUN_ENTRY = re.compile(r'^\s*"[^"]+":\s*\["(?P<spec>[^"]+)"')


class GhError(RuntimeError):
    """A ``gh`` command failed."""


def _version_key(version: str) -> tuple[tuple[int, ...], int, str]:
    """Sort key: release numbers, then pre (0) < release (1) < post (2)."""
    match = _RELEASE.match(version.split("+", 1)[0].strip())
    if not match:
        raise ValueError(f"unparseable version {version!r}")
    release = tuple(int(part) for part in match.group(1).split("."))
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    rest = match.group(2)
    if not rest:
        return (release, 1, "")
    if _POST.match(rest):
        return (release, 2, rest.lower())
    return (release, 0, rest.lower())


def in_range(version: str, vulnerable_range: str) -> bool:
    """Whether ``version`` falls in an advisory's ``vulnerable_version_range``.

    The range is comma-joined clauses (``>= 7.0.0, < 7.30.1``), all of which must
    hold.

    Raises
    ------
    ValueError
        A clause or version that can't be parsed.
    """
    key = _version_key(version)
    for clause in vulnerable_range.split(","):
        match = _CLAUSE.match(clause)
        if not match:
            raise ValueError(
                f"unparseable range clause {clause!r} in {vulnerable_range!r}"
            )
        op, bound = match.group(1), _version_key(match.group(2))
        holds = {
            "<": key < bound,
            "<=": key <= bound,
            ">": key > bound,
            ">=": key >= bound,
            "=": key == bound,
        }[op]
        if not holds:
            return False
    return True


def _normalize_pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lockfile_for(root: Path, manifest_path: str) -> Path | None:
    """Return the lockfile that pins the packages an alert's manifest declares.

    A lockfile maps to itself; any other manifest maps to a lockfile beside it.
    """
    manifest = root / manifest_path
    if manifest.name in _LOCKFILES:
        return manifest
    for name in _LOCKFILES:
        candidate = manifest.parent / name
        if candidate.is_file():
            return candidate
    return None


def _uv_versions(lockfile: Path, package: str) -> list[str]:
    data = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    wanted = _normalize_pypi(package)
    return [
        str(entry["version"])
        for entry in data.get("package", [])
        if _normalize_pypi(str(entry.get("name", ""))) == wanted and "version" in entry
    ]


def _bun_versions(lockfile: Path, package: str) -> list[str]:
    versions = []
    in_packages = False
    for line in lockfile.read_text(encoding="utf-8").splitlines():
        if not in_packages:
            in_packages = line.strip().startswith('"packages":')
            continue
        match = _BUN_ENTRY.match(line)
        if not match:
            continue
        name, _, version = match.group("spec").rpartition("@")
        if name == package and version:
            versions.append(version)
    return versions


def locked_versions(lockfile: Path, package: str, ecosystem: str) -> list[str]:
    """Every version of ``package`` locked in ``lockfile`` (nested copies included).

    ``ecosystem`` is the alert's (``pip`` names are compared PEP 503-normalized).
    """
    if lockfile.name == "uv.lock":
        return _uv_versions(lockfile, package)
    if lockfile.name == "bun.lock":
        return _bun_versions(lockfile, package)
    raise ValueError(f"unsupported lockfile {lockfile.name!r}")


def status_of(alert: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """One alert's status on the tree at ``root``."""
    dependency = alert.get("dependency") or {}
    package = (dependency.get("package") or {}).get("name", "")
    ecosystem = (dependency.get("package") or {}).get("ecosystem", "")
    vulnerability = alert.get("security_vulnerability") or {}
    vulnerable = vulnerability.get("vulnerable_version_range", "")
    patched = (vulnerability.get("first_patched_version") or {}).get("identifier")
    lockfile = lockfile_for(root, dependency.get("manifest_path", ""))
    result: dict[str, Any] = {
        "number": alert.get("number"),
        "severity": (alert.get("security_advisory") or {}).get("severity"),
        "package": package,
        "lockfile": None,
        "locked_versions": [],
        "vulnerable_range": vulnerable,
        "first_patched": patched,
        "status": "unknown",
    }
    if lockfile is None or not lockfile.is_file():
        return result
    result["lockfile"] = lockfile.relative_to(root).as_posix()
    try:
        versions = locked_versions(lockfile, package, ecosystem)
        still_vulnerable = [v for v in versions if in_range(v, vulnerable)]
    except (ValueError, tomllib.TOMLDecodeError):
        return result
    result["locked_versions"] = versions
    result["status"] = "open" if still_vulnerable else "cleared"
    return result


def _run_gh(argv: Sequence[str]) -> str:
    proc = subprocess.run(  # nosec B603 -- fixed gh argv, no shell
        list(argv), capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"{argv[0]} exited {proc.returncode}")
    return proc.stdout


def fetch_open_alerts(repo: str, runner: Runner) -> list[dict[str, Any]]:
    """Fetch the repo's open Dependabot alerts, every page."""
    out = runner(
        [
            "gh",
            "api",
            f"repos/{repo}/dependabot/alerts?state=open&per_page=100",
            "--paginate",
            "--jq",
            ".[]",
        ]
    )
    return [json.loads(line) for line in out.splitlines() if line.strip()]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(64, f"{self.prog}: error: {message}\n")


def summarize(alerts: Iterable[Mapping[str, Any]], root: Path) -> dict[str, Any]:
    """Status of every alert plus ``open`` / ``unknown`` counts."""
    statuses = [status_of(alert, root) for alert in alerts]
    return {
        "alerts": statuses,
        "open": sum(s["status"] == "open" for s in statuses),
        "unknown": sum(s["status"] == "unknown" for s in statuses),
    }


def main(argv: Sequence[str] | None = None, runner: Runner = _run_gh) -> int:
    """Print every open alert's status; exit 0 only when all are cleared."""
    parser = _Parser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", help="owner/name: read open alerts with gh")
    source.add_argument(
        "--alerts-file", type=Path, help="JSON list of alerts (as gh api returns them)"
    )
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="repo root (default: cwd)"
    )
    args = parser.parse_args(argv)
    try:
        if args.repo:
            alerts = fetch_open_alerts(args.repo, runner)
        else:
            alerts = json.loads(args.alerts_file.read_text(encoding="utf-8"))
    except (GhError, OSError, json.JSONDecodeError) as exc:
        print(f"could not read alerts: {exc}", file=sys.stderr)
        return 64
    summary = summarize(alerts, args.root.resolve())
    print(json.dumps(summary, indent=2))
    return 1 if summary["open"] or summary["unknown"] else 0


if __name__ == "__main__":
    sys.exit(main())
