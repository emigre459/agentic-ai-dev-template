#!/usr/bin/env python3
"""Sweep the open update PRs: plan, close, and recheck after the fold PR merges.

"Update PRs" are Dependabot's (``dependabot/<ecosystem>/...`` branches) and the
nightly skills update PR (the branch the ``skills-update`` action commits to). Each
gets exactly one action:

- ``FOLD``              goes into one fold PR: a bump that fixes an open Dependabot
                        alert (in a grouped PR, only the members that do), an open
                        alert no PR covers, a local composite-action bump, or a
                        skills refresh confined to the skills tree.
- ``CLOSE_SUPERSEDED``  ``main`` already carries it; close now.
- ``CLOSE_ROUTINE``     a minor/patch bump (single or grouped) with no open
                        advisory, or a major of an indirect dependency; close now.
- ``REVIEW``            the script can't prove it safe (alerts unreadable, a
                        conflict, a major with no advisory, an alert with no patch,
                        skills paths outside the tree); a person decides.

Subcommands (``--help`` on each):

    plan      collect the PRs, alerts and main's lockfiles; print the plan
    close     close CLOSE_* PRs now, re-classified live (dry run without --execute)
    recheck   wait for the fold PR to merge, then close what GitHub left open
              (dry run without --execute)

Projects are the ``directory`` entries of ``.github/dependabot.yml``; lockfiles are
read from the base branch with ``git show``. Exit codes: 0 ok; 1 ``close`` refused a
PR; 2 the fold PR did not merge before ``--timeout``; 3 the fold PR closed unmerged;
4 a PR was left open because ``main`` doesn't satisfy it; 64 ``gh``/``git`` failed or
the interpreter is older than 3.11. Stdlib only; run with ``uv run python``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
import subprocess  # nosec B404 -- fixed gh/git argv, no shell
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Literal, NamedTuple

# ``datetime.UTC`` is 3.11+; this alias keeps the module importable on older
# interpreters, so the version check below can run and say so.
UTC = timezone.utc  # noqa: UP017 -- datetime.UTC is 3.11+


def _sibling(name: str) -> ModuleType:
    """Load a script that ships beside this one, reusing an already-loaded copy."""
    path = Path(__file__).resolve().with_name(f"{name}.py")
    loaded = sys.modules.get(name)
    if loaded is not None and Path(getattr(loaded, "__file__", "")).resolve() == path:
        return loaded
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


alert_status = _sibling("alert_status")
GhError = alert_status.GhError
#: ``argv -> stdout``; raises ``GhError`` when the command fails.
Runner = Callable[[Sequence[str]], str]

Action = Literal["FOLD", "CLOSE_SUPERSEDED", "CLOSE_ROUTINE", "REVIEW"]
#: Dependabot branch segment -> Dependabot alert ecosystem.
ALERT_ECOSYSTEM = {
    "uv": "pip",
    "pip": "pip",
    "github_actions": "actions",
    "bun": "npm",
    "npm_and_yarn": "npm",
}
#: ``.github/dependabot.yml`` ecosystem -> the lockfile this script reads for it.
_LOCK_FOR_CONFIG = {"uv": "uv.lock", "bun": "bun.lock"}
#: Lockfile -> the manifest beside it that declares direct dependencies.
_MANIFEST_FOR_LOCK = {"uv.lock": "pyproject.toml", "bun.lock": "package.json"}
_SKILLS_ACTION = ".github/actions/skills-update"
SKILLS_PATHS = ("skills-lock.json", ".agents/skills/", ".claude/skills/")
_SKILLS_DOC_SUFFIXES = (".md", ".json", ".txt", ".yaml", ".yml")
# Local composite actions: the dependency graph reads only workflow files, so no
# alert can name an action referenced here; their version bumps are the only path.
_COMPOSITE_PREFIX = ".github/actions/"
_SEVERITY_RANK = {"low": 0, "medium": 1, "moderate": 1, "high": 2, "critical": 3}
_ACTION_ORDER = {"FOLD": 0, "REVIEW": 1, "CLOSE_SUPERSEDED": 2, "CLOSE_ROUTINE": 3}
DEFAULT_GRACE_S = 300
# Merge wait + grace + re-collect/close stays under the 2 h ceiling a harness's
# background command typically has (Claude Code's is 7200 s); pinned by a test.
DEFAULT_TIMEOUT_S = 6600
# Polls per wait window: the cadence is a slice of --grace (or --timeout), so the
# timeout and the grace stay the only raw times.
_POLLS_PER_WINDOW = 10
_FOOTER = "\n\n_Closed by the `upgrade-deps` skill (sweep mode)._"

_TITLE = re.compile(
    r"\bbump (?P<pkg>\S+) from (?P<frm>\S+) to (?P<to>\S+)", re.IGNORECASE
)
_MEMBER = re.compile(
    r"Updates `(?P<pkg>[^`]+)` from (?P<frm>[^\s`]+) to (?P<to>[^\s`]+)"
)
_GROUP_COUNT = re.compile(r"\bwith (\d+) updates?\b", re.IGNORECASE)
_RELEASE = re.compile(r"^v?(\d+(?:\.\d+)*)")
_ECO_LINE = re.compile(r"""^\s*-\s*package-ecosystem:\s*["']?([\w-]+)""")
_DIR_LINE = re.compile(r"""^\s*directory:\s*["']?([^"'\s#]+)""")
_DIRS_LINE = re.compile(r"^(\s*)directories:\s*$")
_ITEM_LINE = re.compile(r"""^\s*-\s*["']?([^"'\s#]+)""")
_USES_LINE = re.compile(
    r"""^(?P<lead>\s*(?:-\s+)?)uses:\s*["']?(?P<src>[^"'\s@#]*?)/?"""
    r"""\.github/actions/skills-update(?:@(?P<ref>[^"'\s#]+))?"""
)
_REQUIREMENT = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;]*)"
)
_NPM_RANGE = re.compile(r"^\s*(\^|~|>=)?\s*v?(\d[\w.+-]*)\s*$")


class MergeWaitError(RuntimeError):
    """The fold PR did not merge; ``code`` is the CLI exit code (2 or 3)."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# --- versions and names ---------------------------------------------------------


@dataclass(frozen=True, order=True)
class Version:
    """A parsed version, ordered by ``alert_status.version_key``.

    The PEP 440 subset Dependabot titles and advisories carry: a release
    (``1.2.3``, an optional ``v``, trailing zeros ignored), pre-releases in PEP 440
    or npm spelling (``1.0rc1``, ``2.0.0-beta.2``, numbers compared as numbers),
    post-releases (``1.0.post1``) and a dropped ``+local`` part. No epochs; ``dev``
    sorts as a pre-release.
    """

    key: tuple[Any, ...]
    text: str = field(compare=False)
    release: tuple[int, ...] = field(compare=False)


def parse_version(text: str | None) -> Version | None:
    """Parse a version, or return ``None`` when it isn't one."""
    if text is None:
        return None
    match = _RELEASE.match(text.strip())
    if not match:
        return None
    try:
        key = alert_status.version_key(text)
    except ValueError:
        return None
    return Version(key, text, tuple(int(p) for p in match.group(1).split(".")))


def is_major(frm: str, to: str) -> bool:
    """Whether ``frm -> to`` crosses a major (or a ``0.x`` minor, semver's major)."""
    old, new = parse_version(frm), parse_version(to)
    if old is None or new is None:
        return False
    if old.release[0] != new.release[0]:
        return True
    old_minor = old.release[1] if len(old.release) > 1 else 0
    new_minor = new.release[1] if len(new.release) > 1 else 0
    return old.release[0] == 0 and old_minor != new_minor


def normalize_name(alert_ecosystem: str, name: str) -> str:
    """Compare-ready package name: PEP 503 for ``pip``, lower-case otherwise."""
    if alert_ecosystem == "pip":
        return str(alert_status.normalize_pypi(name))
    return name.lower()


# --- repo config ----------------------------------------------------------------


@dataclass(frozen=True)
class Project:
    """One ``.github/dependabot.yml`` entry: its ecosystem and repo-relative dir."""

    ecosystem: str
    directory: str


def _norm_dir(text: str) -> str:
    return text.strip().strip("/") or "."


def _join(directory: str, name: str) -> str:
    return name if directory == "." else f"{directory}/{name}"


def dependabot_projects(text: str) -> list[Project]:
    """Read every ``(package-ecosystem, directory)`` from ``dependabot.yml`` text.

    Line-based (stdlib has no YAML): ``directory:`` and ``directories:`` lists are
    read; glob directories are skipped.
    """
    projects: list[Project] = []
    ecosystem: str | None = None
    list_indent: int | None = None
    for line in text.splitlines():
        if list_indent is not None:
            indent = len(line) - len(line.lstrip())
            item = _ITEM_LINE.match(line)
            if line.strip() and indent > list_indent and item:
                if ecosystem and "*" not in item.group(1):
                    projects.append(Project(ecosystem, _norm_dir(item.group(1))))
                continue
            if line.strip():
                list_indent = None
        if match := _ECO_LINE.match(line):
            ecosystem = match.group(1)
        elif (match := _DIR_LINE.match(line)) and ecosystem:
            projects.append(Project(ecosystem, _norm_dir(match.group(1))))
        elif match := _DIRS_LINE.match(line):
            list_indent = len(match.group(1))
    return projects


def lockfiles_of(projects: Iterable[Project]) -> list[str]:
    """List the ``uv.lock`` / ``bun.lock`` paths of projects this script can read."""
    found = [
        _join(p.directory, _LOCK_FOR_CONFIG[p.ecosystem])
        for p in projects
        if p.ecosystem in _LOCK_FOR_CONFIG
    ]
    return sorted(set(found))


def _input_default(action_text: str, name: str) -> str:
    """Return the ``default`` of input ``name`` in an ``action.yml`` text."""
    lines = action_text.splitlines()
    for i, line in enumerate(lines):
        match = re.match(rf"^(\s*){re.escape(name)}:\s*$", line)
        if not match:
            continue
        for inner in lines[i + 1 :]:
            if inner.strip() and len(inner) - len(inner.lstrip()) <= len(
                match.group(1)
            ):
                break
            if default := re.match(r"""^\s*default:\s*["']?([^"'\s#]+)""", inner):
                return default.group(1)
    raise LookupError(f"no default for input {name!r}")


def _skills_step(root: Path) -> tuple[str | None, str | None, str | None]:
    """Find the workflow step using the skills-update action.

    Returns ``(branch override, source repo, ref)``; the source is ``None`` for the
    local ``./.github/actions/skills-update``.
    """
    workflows = root / ".github" / "workflows"
    paths = sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")])
    for path in paths:
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            match = _USES_LINE.match(line)
            if not match:
                continue
            key_col = len(match.group("lead"))
            src = match.group("src").strip("/")
            source = None if src in ("", ".") else src
            in_with = False
            for inner in lines[i + 1 :]:
                indent = len(inner) - len(inner.lstrip())
                if not inner.strip():
                    continue
                if indent < key_col:
                    break
                if indent == key_col:
                    in_with = inner.strip() == "with:"
                elif in_with and (
                    override := re.match(r"""^\s*branch:\s*["']?([^"'\s#]+)""", inner)
                ):
                    return override.group(1), source, match.group("ref")
            return None, source, match.group("ref")
    return None, None, None


def skills_branch(root: Path, runner: Runner | None = None) -> str:
    """Return the branch the skills update PR comes from.

    A ``branch:`` the workflow passes the action wins; otherwise the action's
    ``branch`` input default, read from ``.github/actions/skills-update/action.yml``
    or, in a repo that runs the released action, from that release via ``gh``.

    Raises
    ------
    LookupError
        No workflow, local action or readable release names the branch.
    """
    override, source, ref = _skills_step(root)
    if override:
        return override
    local = root / _SKILLS_ACTION / "action.yml"
    if local.is_file():
        return _input_default(local.read_text(encoding="utf-8"), "branch")
    if source and runner is not None:
        query = f"?ref={ref}" if ref else ""
        url = f"repos/{source}/contents/{_SKILLS_ACTION}/action.yml{query}"
        text = runner(["gh", "api", "-H", "Accept: application/vnd.github.raw", url])
        return _input_default(text, "branch")
    raise LookupError("no skills-update action or workflow step found")


# --- alerts and manifests -------------------------------------------------------


@dataclass(frozen=True)
class Alert:
    """Open alerts for one package, merged: worst severity, highest patch."""

    ecosystem: str
    package: str
    severity: str
    ghsa: str
    patched: str | None
    lockfiles: tuple[str, ...] = ()


class Floor(NamedTuple):
    """A direct dependency's declaration: its manifest and lower bound, if any."""

    manifest: str
    op: str | None
    version: str | None


def _alert_lockfile(manifest_path: str, ecosystem: str) -> str | None:
    name = alert_status.LOCKFILE_FOR_ECOSYSTEM.get(ecosystem)
    path = PurePosixPath(manifest_path)
    if name is None:
        return None
    if path.name in alert_status.LOCKFILE_FOR_ECOSYSTEM.values():
        return str(path) if path.name == name else None
    return _join(_norm_dir(str(path.parent)), name)


def alerts_from_raw(raw: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], Alert]:
    """Merge ``gh api`` alerts per ``(ecosystem, package)``.

    A missing patch anywhere wins (``None``), since that advisory can't be folded.
    """
    merged: dict[tuple[str, str], Alert] = {}
    for item in raw:
        package = item["dependency"]["package"]
        eco = package["ecosystem"]
        name = normalize_name(eco, package["name"])
        advisory = item["security_advisory"]
        patched = (
            item["security_vulnerability"].get("first_patched_version") or {}
        ).get("identifier")
        lockfile = _alert_lockfile(item["dependency"].get("manifest_path", ""), eco)
        alert = Alert(
            eco,
            name,
            advisory["severity"],
            advisory["ghsa_id"],
            patched,
            (lockfile,) if lockfile else (),
        )
        cur = merged.get((eco, name))
        if cur is None:
            merged[(eco, name)] = alert
            continue
        if _SEVERITY_RANK.get(alert.severity, 0) > _SEVERITY_RANK.get(cur.severity, 0):
            cur = replace(cur, severity=alert.severity, ghsa=alert.ghsa)
        new, old = parse_version(alert.patched), parse_version(cur.patched)
        if alert.patched is None or (new and old and new > old):
            cur = replace(cur, patched=alert.patched)
        lockfiles = tuple(sorted({*cur.lockfiles, *alert.lockfiles}))
        merged[(eco, name)] = replace(cur, lockfiles=lockfiles)
    return merged


def _direct_from_pyproject(text: str, manifest: str) -> dict[str, Floor]:
    data = alert_status.tomllib.loads(text)
    project = data.get("project", {})
    specs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        specs += group
    for group in data.get("dependency-groups", {}).values():
        specs += [s for s in group if isinstance(s, str)]
    direct: dict[str, Floor] = {}
    for spec in specs:
        match = _REQUIREMENT.match(spec)
        if not match:
            continue
        floor = Floor(manifest, None, None)
        for clause in match.group(2).split(","):
            if clause.strip().startswith(">="):
                floor = Floor(manifest, ">=", clause.strip()[2:].strip())
        direct[normalize_name("pip", match.group(1))] = floor
    return direct


def _direct_from_package_json(text: str, manifest: str) -> dict[str, Floor]:
    data = json.loads(text)
    direct: dict[str, Floor] = {}
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (data.get(section) or {}).items():
            match = _NPM_RANGE.match(str(spec))
            floor = (
                Floor(manifest, match.group(1) or "", match.group(2))
                if match
                else Floor(manifest, None, None)
            )
            direct[name.lower()] = floor
    return direct


# --- classification -------------------------------------------------------------


class Bump(NamedTuple):
    """One package's version change in an update PR."""

    package: str
    frm: str
    to: str


@dataclass
class _Verdict:
    action: Action
    reason: str
    folds: list[tuple[Bump, str]] = field(default_factory=list)
    alert: Alert | None = None
    review_note: str | None = None
    stays_open: bool = False


def bumps_of(pr: Mapping[str, Any]) -> tuple[list[Bump], bool]:
    """Return the PR's bumps and whether it is grouped (members from its body)."""
    if match := _TITLE.search(pr.get("title") or ""):
        return [Bump(match["pkg"], match["frm"], match["to"])], False
    members: dict[str, Bump] = {}
    for match in _MEMBER.finditer(pr.get("body") or ""):
        to = match["to"].rstrip(".,;")
        members.setdefault(match["pkg"].lower(), Bump(match["pkg"], match["frm"], to))
    return list(members.values()), True


def _judge_skills(pr: Mapping[str, Any]) -> _Verdict:
    files = list(pr.get("files") or [])
    outside = [
        f for f in files if not any(f == p or f.startswith(p) for p in SKILLS_PATHS)
    ]
    if outside:
        return _Verdict(
            "REVIEW", f"touches paths outside the skills tree: {', '.join(outside)}"
        )
    changes = pr.get("changes_main")
    if changes is None:
        return _Verdict("REVIEW", "branch conflicts with main (merge-tree)")
    if changes is False:
        return _Verdict("CLOSE_SUPERSEDED", "main already carries these skill files")
    executable = [
        f
        for f in files
        if f.startswith(SKILLS_PATHS[1:]) and not f.endswith(_SKILLS_DOC_SUFFIXES)
    ]
    note = (
        f"carries non-doc files, read them first: {', '.join(executable)}"
        if executable
        else None
    )
    return _Verdict(
        "FOLD",
        "skills refresh confined to the skills tree; read its diff before folding",
        review_note=note,
    )


def _judge(
    pr: Mapping[str, Any],
    alerts: Mapping[tuple[str, str], Alert],
    main_versions: Mapping[tuple[str, str], str],
    alerts_available: bool,
    direct: Mapping[str, Mapping[str, Floor]] | None,
) -> _Verdict:
    if pr.get("kind") == "skills":
        return _judge_skills(pr)
    segment = pr.get("ecosystem") or ""
    eco = ALERT_ECOSYSTEM.get(segment, segment)
    bumps, grouped = bumps_of(pr)
    if not bumps:
        return _Verdict(
            "REVIEW",
            "grouped or unrecognized Dependabot PR: "
            "no package list in its title or body",
        )
    if grouped:
        # Fail closed: a member in another body form, or a truncated body, would
        # otherwise drop out silently and let the group close as routine.
        count = _GROUP_COUNT.search(pr.get("title") or "")
        listed = len(_MEMBER.findall(pr.get("body") or ""))
        if count is None or int(count.group(1)) != listed:
            expected = count.group(1) if count else "an uncounted number of"
            return _Verdict(
                "REVIEW",
                f"grouped PR: the title says {expected} update(s) but the body "
                f"lists {listed} as `Updates <pkg> from X to Y`",
            )
    lockfile = pr.get("lockfile")
    pending: list[tuple[Bump, Version | None]] = []
    if lockfile:
        for bump in bumps:
            to_v = parse_version(bump.to)
            if to_v is None:
                return _Verdict(
                    "REVIEW",
                    f"cannot parse target version {bump.to!r} of {bump.package}",
                )
            locked = parse_version(
                main_versions.get((lockfile, normalize_name(eco, bump.package)))
            )
            if locked is not None and locked < to_v:
                pending.append((bump, locked))
        if not pending:
            if grouped:
                return _Verdict(
                    "CLOSE_SUPERSEDED",
                    f"main's {lockfile} already carries every bump in the group",
                )
            locked_text = main_versions.get(
                (lockfile, normalize_name(eco, bumps[0].package))
            )
            where = (
                f"already has {bumps[0].package} {locked_text}"
                if locked_text
                else f"no longer locks {bumps[0].package}"
            )
            return _Verdict("CLOSE_SUPERSEDED", f"main's {lockfile} {where}")
    else:
        changes = pr.get("changes_main")
        if changes is None:
            return _Verdict("REVIEW", "branch conflicts with main (merge-tree)")
        if changes is False:
            return _Verdict("CLOSE_SUPERSEDED", "merging it would not change main")
        pending = [(bump, None) for bump in bumps]

    advisory: list[tuple[Bump, Alert]] = []
    for bump, locked in pending:
        alert = alerts.get((eco, normalize_name(eco, bump.package)))
        patched = parse_version(alert.patched) if alert else None
        if alert is None or (patched and locked is not None and locked >= patched):
            continue  # no alert, or a stale one: main already locks the fix
        if alert.patched is None:
            return _Verdict(
                "REVIEW",
                f"open {alert.severity} alert {alert.ghsa} on {bump.package} "
                "has no patched version",
            )
        advisory.append((bump, alert))
    majors = [bump for bump, _ in pending if is_major(bump.frm, bump.to)]

    if advisory:
        folds, short = [], []
        for bump, alert in advisory:
            to_v, patched_v = parse_version(bump.to), parse_version(alert.patched)
            stops_short = bool(to_v and patched_v and patched_v > to_v)
            # A lockfile fold targets the patch itself; a PR diff stays at its `to`.
            target = alert.patched if (lockfile and stops_short) else bump.to
            folds.append((bump, str(target)))
            if stops_short:
                short.append(
                    f"{bump.package} PR stops at {bump.to}, patched in {alert.patched}"
                )
        bump, alert = advisory[0]
        if not grouped:
            reason = f"fixes open {alert.severity} alert {alert.ghsa}"
            if short:
                reason += f" ({short[0]})"
            return _Verdict("FOLD", reason, folds, alert)
        names = ", ".join(b.package for b, _ in advisory)
        ghsas = ", ".join(sorted({a.ghsa for _, a in advisory}))
        how = (
            "fold them alone as a targeted upgrade"
            if lockfile
            else "fold the PR's diff"
        )
        verdict = _Verdict(
            "FOLD",
            f"group member(s) {names} fix open alerts ({ghsas}): {how}; "
            "the group's other bumps are routine",
            folds,
            alert,
        )
        other_majors = [b.package for b in majors if b not in {b for b, _ in advisory}]
        if other_majors:
            verdict.stays_open = True
            verdict.review_note = (
                f"the group also carries a major ({', '.join(other_majors)}): "
                "it stays open for review after the fold"
            )
        return verdict

    files = list(pr.get("files") or [])
    if (
        segment == "github_actions"
        and files
        and all(f.startswith(_COMPOSITE_PREFIX) for f in files)
    ):
        return _Verdict(
            "FOLD",
            "composite-action reference: no alert can reach it, "
            "so this bump is its only update path",
            [(b, b.to) for b in bumps],
        )
    if not alerts_available:
        return _Verdict(
            "REVIEW",
            "Dependabot alerts were unreadable: cannot tell security from routine",
        )
    if segment not in ALERT_ECOSYSTEM:
        # Its alerts use another ecosystem name (cargo -> rust, ...), so the lookup
        # above can't have seen them: never call it routine.
        return _Verdict(
            "REVIEW",
            f"alerts for Dependabot ecosystem {segment!r} aren't mapped: "
            "cannot tell security from routine",
        )
    if majors:
        if not grouped and lockfile and direct is not None and lockfile in direct:
            name = normalize_name(eco, majors[0].package)
            if name not in direct[lockfile]:
                return _Verdict(
                    "CLOSE_ROUTINE",
                    f"major of an indirect dependency ({majors[0].package} is "
                    f"not declared beside {lockfile}) with no advisory: "
                    "Dependabot opens majors for direct dependencies, "
                    "so this is a leftover",
                )
        if grouped:
            names = ", ".join(b.package for b in majors)
            return _Verdict(
                "REVIEW",
                f"grouped PR carries a major ({names}) with no advisory: "
                "reviewed, never folded or auto-closed",
            )
        return _Verdict(
            "REVIEW",
            "major-version update with no advisory: "
            "reviewed on its own PR, never folded or auto-closed",
        )
    if grouped:
        return _Verdict(
            "CLOSE_ROUTINE",
            "routine minor/patch group: no open security advisory on any member",
        )
    return _Verdict(
        "CLOSE_ROUTINE", f"no open security advisory for {bumps[0].package}"
    )


def classify(
    pr: Mapping[str, Any],
    alerts: Mapping[tuple[str, str], Alert],
    main_versions: Mapping[tuple[str, str], str],
    *,
    alerts_available: bool = True,
    direct: Mapping[str, Mapping[str, Floor]] | None = None,
) -> tuple[Action, str]:
    """Decide one update PR's action.

    Parameters
    ----------
    pr : Mapping[str, Any]
        ``number``, ``title``, ``body``, ``kind`` (``dependabot`` or ``skills``),
        ``ecosystem`` (the Dependabot branch segment), ``lockfile`` (the main-side
        lockfile it changes, or ``None``), ``files`` and ``changes_main`` (whether
        merging it changes ``main``; ``None`` on a conflict; read only without a
        lockfile).
    alerts : Mapping[tuple[str, str], Alert]
        Open alerts by ``(alert ecosystem, normalized package)``.
    main_versions : Mapping[tuple[str, str], str]
        The lowest version ``main`` locks, by ``(lockfile, normalized package)``.
    alerts_available : bool
        ``False`` when the alerts couldn't be read: nothing closes as routine.
    direct : Mapping[str, Mapping[str, Floor]] | None
        Direct dependencies per lockfile; a lockfile missing here is unknown.

    Returns
    -------
    tuple[Action, str]
        The action and the reason shown in the plan.
    """
    verdict = _judge(pr, alerts, main_versions, alerts_available, direct)
    return verdict.action, verdict.reason


# --- the plan -------------------------------------------------------------------


def _in_dir(directory: str, command: str) -> str:
    return (
        command if directory == "." else f"(cd {shlex.quote(directory)} && {command})"
    )


def _lock_fold(lockfile: str, package: str, target: str) -> str:
    path = PurePosixPath(lockfile)
    directory = _norm_dir(str(path.parent))
    if path.name == "bun.lock":
        return _in_dir(directory, f"bun update {shlex.quote(f'{package}@{target}')}")
    return _in_dir(
        directory, f"uv lock --upgrade-package {shlex.quote(f'{package}=={target}')}"
    )


def _floor_note(
    direct: Mapping[str, Mapping[str, Floor]], lockfile: str, package: str, target: str
) -> str | None:
    if PurePosixPath(lockfile).name != "uv.lock":
        return None  # bun update rewrites the package.json range itself
    floor = direct.get(lockfile, {}).get(normalize_name("pip", package))
    if floor is None or floor.op != ">=":
        return None
    have, want = parse_version(floor.version), parse_version(target)
    if have is None or want is None or have >= want:
        return None
    return (
        f"`{package}` is a direct dependency: raise its floor in {floor.manifest} from "
        f">={floor.version} to >={target}, so the manifest can't resolve below the fix"
    )


def _decision(
    pr: Mapping[str, Any] | None, kind: str, verdict: _Verdict, **extra: Any
) -> dict[str, Any]:
    alert = verdict.alert
    base: dict[str, Any] = {
        "pr": pr["number"] if pr else None,
        "kind": kind,
        "title": pr.get("title") if pr else None,
        "grouped": False,
        "bumps": [],
        "action": verdict.action,
        "reason": verdict.reason,
        "severity": alert.severity if alert else None,
        "ghsa": alert.ghsa if alert else None,
        "lockfile": pr.get("lockfile") if pr else None,
        "fold_commands": [],
        "floor_notes": [],
        "review_note": verdict.review_note,
        "stays_open": verdict.stays_open,
    }
    base.update(extra)
    return base


def decide(
    pr: Mapping[str, Any],
    alerts: Mapping[tuple[str, str], Alert],
    main_versions: Mapping[tuple[str, str], str],
    *,
    alerts_available: bool,
    direct: Mapping[str, Mapping[str, Floor]],
    base: str = "main",
) -> dict[str, Any]:
    """One PR's full plan row: ``classify``'s verdict plus fold commands and notes."""
    verdict = _judge(pr, alerts, main_versions, alerts_available, direct)
    bumps, grouped = bumps_of(pr)
    extra: dict[str, Any] = {
        "grouped": grouped and pr.get("kind") != "skills",
        "bumps": [b._asdict() for b in bumps],
    }
    if verdict.action == "FOLD":
        lockfile = pr.get("lockfile")
        if pr.get("kind") == "skills":
            branch = pr["head"]
            paths = " ".join(SKILLS_PATHS)
            merge_base = f"$(git merge-base origin/{base} origin/{branch})"
            extra["fold_commands"] = [
                (
                    f"git fetch origin {branch} && git diff {merge_base} "
                    f"origin/{branch} -- {paths} | git apply --3way --index"
                )
            ]
        elif lockfile:
            extra["fold_commands"] = [
                _lock_fold(lockfile, b.package, t) for b, t in verdict.folds
            ]
            notes = [
                _floor_note(direct, lockfile, b.package, t) for b, t in verdict.folds
            ]
            extra["floor_notes"] = [n for n in notes if n]
        else:
            extra["fold_commands"] = [
                f"gh pr diff {pr['number']} | git apply --3way --index"
            ]
        extra["folded"] = [
            [
                lockfile,
                normalize_name(
                    ALERT_ECOSYSTEM.get(pr.get("ecosystem") or "", ""), b.package
                ),
            ]
            for b, _ in verdict.folds
        ]
    return _decision(pr, pr.get("kind") or "dependabot", verdict, **extra)


def build_plan(
    prs: Iterable[Mapping[str, Any]],
    alerts: Mapping[tuple[str, str], Alert],
    main_versions: Mapping[tuple[str, str], str],
    *,
    alerts_available: bool,
    direct: Mapping[str, Mapping[str, Floor]],
    base: str = "main",
) -> dict[str, Any]:
    """Decide every PR, add folds for alerts no PR covers, and derive the PR lists.

    Returns
    -------
    dict[str, Any]
        ``decisions`` (sorted by action, then PR) and the lists ``fold_prs``,
        ``close_now``, ``close_after_merge``, ``review`` and ``expected_open_after``.
    """
    decisions = [
        decide(
            pr,
            alerts,
            main_versions,
            alerts_available=alerts_available,
            direct=direct,
            base=base,
        )
        for pr in prs
    ]
    folded = {tuple(f) for d in decisions for f in d.get("folded", [])}
    for (eco, package), alert in sorted(alerts.items()):
        patched = parse_version(alert.patched)
        for lockfile in alert.lockfiles:
            locked = parse_version(main_versions.get((lockfile, package)))
            if (
                patched is None
                or locked is None
                or locked >= patched
                or (lockfile, package) in folded
            ):
                continue
            verdict = _Verdict(
                "FOLD",
                f"open {alert.severity} alert {alert.ghsa} with no Dependabot PR",
                [(Bump(package, locked.text, str(alert.patched)), str(alert.patched))],
                alert,
            )
            decisions.append(
                _decision(
                    None,
                    "alert",
                    verdict,
                    lockfile=lockfile,
                    bumps=[Bump(package, locked.text, str(alert.patched))._asdict()],
                    fold_commands=[_lock_fold(lockfile, package, str(alert.patched))],
                    floor_notes=[
                        n
                        for n in [
                            _floor_note(direct, lockfile, package, str(alert.patched))
                        ]
                        if n
                    ],
                )
            )
    decisions.sort(key=lambda d: (_ACTION_ORDER[d["action"]], d["pr"] or 0))

    def numbers(keep: Callable[[dict[str, Any]], bool]) -> list[int]:
        return [d["pr"] for d in decisions if d["pr"] is not None and keep(d)]

    return {
        "decisions": decisions,
        "fold_prs": numbers(lambda d: d["action"] == "FOLD"),
        "close_now": numbers(lambda d: d["action"].startswith("CLOSE_")),
        "close_after_merge": numbers(
            lambda d: d["action"] == "FOLD" and not d["stays_open"]
        ),
        "review": numbers(lambda d: d["action"] == "REVIEW"),
        "expected_open_after": numbers(
            lambda d: d["action"] == "REVIEW" or d["stays_open"]
        ),
    }


def plan_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """``build_plan`` over a ``collect`` snapshot."""
    alerts = {
        (a["ecosystem"], a["package"]): Alert(
            **{**a, "lockfiles": tuple(a["lockfiles"])}
        )
        for a in snapshot["alerts"]
    }
    versions = {
        (lockfile, package): version
        for lockfile, packages in snapshot["main_versions"].items()
        for package, version in packages.items()
    }
    direct = {
        lockfile: {name: Floor(*floor) for name, floor in deps.items()}
        for lockfile, deps in snapshot["direct"].items()
    }
    return build_plan(
        snapshot["prs"],
        alerts,
        versions,
        alerts_available=snapshot["alerts_available"],
        direct=direct,
        base=snapshot["base"],
    )


# --- rendering ------------------------------------------------------------------


def _what(d: Mapping[str, Any]) -> str:
    if d["kind"] == "skills":
        return "skills refresh"
    if not d["bumps"]:
        return str(d["title"] or "?")
    if d["grouped"]:
        return f"group of {len(d['bumps'])}: " + ", ".join(
            f"`{b['package']}`" for b in d["bumps"]
        )
    bump = d["bumps"][0]
    text = f"`{bump['package']}` {bump['frm']} → {bump['to']}"
    return text + (" **(major)**" if is_major(bump["frm"], bump["to"]) else "")


def render_markdown(plan: Mapping[str, Any], snapshot: Mapping[str, Any]) -> str:
    """Render the plan as the Markdown the skill shows the user."""
    lines = [
        (
            f"## Update-PR sweep plan — {snapshot['repo']} @ {snapshot['base']} "
            f"{snapshot['base_sha'][:8]}"
        ),
        (
            f"_collected {snapshot['collected_at']}; "
            f"Dependabot alerts readable: {snapshot['alerts_available']}_"
        ),
        "",
        "| PR | What | Action | Why |",
        "|---|---|---|---|",
    ]
    for d in plan["decisions"]:
        ref = f"#{d['pr']}" if d["pr"] else "(alert)"
        why = d["reason"] + (f" — {d['review_note']}" if d["review_note"] else "")
        lines.append(f"| {ref} | {_what(d)} | {d['action']} | {why} |")
    folds = [d for d in plan["decisions"] if d["action"] == "FOLD"]
    if folds:
        lines += ["", "### Fold commands (on a fresh branch off the base)", "```bash"]
        lines += [c for d in folds for c in d["fold_commands"]]
        for lockfile in sorted({d["lockfile"] for d in folds if d["lockfile"]}):
            path = PurePosixPath(lockfile)
            check = (
                "bun install --frozen-lockfile"
                if path.name == "bun.lock"
                else "uv lock --check"
            )
            lines.append(_in_dir(_norm_dir(str(path.parent)), check))
        lines.append("```")
        notes = [n for d in folds for n in d["floor_notes"]]
        if notes:
            lines += ["", "Floor bumps:", *[f"- {n}" for n in notes]]

    def refs(prs: list[int]) -> str:
        return ", ".join(f"#{n}" for n in prs) or "none"

    lines += [
        "",
        f"- **Close now:** {refs(plan['close_now'])}",
        f"- **Close after the fold PR merges:** {refs(plan['close_after_merge'])}",
        f"- **Needs a person:** {refs(plan['review'])}",
        (
            "- **Expected still open after the sweep:** "
            f"{refs(plan['expected_open_after'])}"
        ),
    ]
    return "\n".join(lines)


def close_comment(
    d: Mapping[str, Any],
    *,
    fold_pr: int | None = None,
    grace_s: float = DEFAULT_GRACE_S,
) -> str:
    """Build the comment posted with a close: every close says why."""
    if fold_pr is not None:
        if d["kind"] == "skills":
            return (
                f"Superseded by #{fold_pr}, which merged these skill updates into "
                "the base branch. The nightly skills update job closes this once "
                "it finds no drift; closing it now instead." + _FOOTER
            )
        what = "its advisory fixes" if d["grouped"] else "this bump"
        return (
            f"Superseded by #{fold_pr}, which merged {what}. GitHub hadn't closed this "
            f"within {grace_s:.0f}s of that merge, so closing by hand." + _FOOTER
        )
    if d["action"] == "CLOSE_SUPERSEDED":
        return f"Closing: {d['reason']}, so there is nothing left to apply." + _FOOTER
    if d["action"] == "CLOSE_ROUTINE":
        return (
            f"Closing as a routine version bump: {d['reason']}. The base branch takes "
            "dependency changes for security fixes or deliberate upgrades; "
            "to pick this "
            "version up on purpose, bump it in a normal PR." + _FOOTER
        )
    raise ValueError(f"no close comment for action {d['action']}")


# --- collecting (gh + git through the runner) -----------------------------------


def _run(argv: Sequence[str]) -> str:
    proc = subprocess.run(  # nosec B603 -- fixed gh/git argv, no shell
        list(argv), capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"{argv[0]} exited {proc.returncode}")
    return proc.stdout


def _segment(head: str) -> str | None:
    parts = head.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == "dependabot" else None


def _changes_main(runner: Runner, base_ref: str, number: int) -> bool | None:
    # A private namespace, deleted after: the PR head never shows up in the
    # user's `git branch -r`, tab completion or anything their remotes manage.
    ref = f"refs/upgrade-deps/pr-{number}"
    runner(["git", "fetch", "-q", "origin", f"+refs/pull/{number}/head:{ref}"])
    try:
        try:
            tree = runner(["git", "merge-tree", "--write-tree", base_ref, ref])
        except GhError:
            return None
        base_tree = runner(["git", "rev-parse", f"{base_ref}^{{tree}}"]).strip()
        return tree.split("\n", 1)[0].strip() != base_tree
    finally:
        try:
            runner(["git", "update-ref", "-d", ref])
        except GhError:
            # Best-effort cleanup: a leftover private ref is harmless, and a
            # failed delete must not hide the verdict or abort the sweep.
            pass


def collect(
    runner: Runner, root: Path, base: str = "main", *, only: Iterable[int] | None = None
) -> dict[str, Any]:
    """Snapshot the open update PRs, open alerts and the base branch's lockfiles.

    Raises
    ------
    GhError
        A ``gh`` or ``git`` call other than the alerts read failed.
    """
    runner(["git", "fetch", "-q", "origin", base])
    base_ref = f"refs/remotes/origin/{base}"
    base_sha = runner(["git", "rev-parse", base_ref]).strip()
    repo = json.loads(runner(["gh", "repo", "view", "--json", "nameWithOwner"]))[
        "nameWithOwner"
    ]
    try:
        skills = skills_branch(root, runner)
    except (LookupError, GhError) as exc:
        print(
            f"note: no skills update branch found ({exc}); "
            "sweeping Dependabot PRs only",
            file=sys.stderr,
        )
        skills = None

    def show(path: str) -> str | None:
        try:
            return runner(["git", "show", f"{base_ref}:{path}"])
        except GhError:
            return None

    fields = "number,title,body,headRefName,files,isCrossRepository"
    rows = json.loads(
        runner(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--base",
                base,
                "--limit",
                "300",
                "--json",
                fields,
            ]
        )
    )
    wanted = set(only) if only is not None else None
    lockfiles = set(
        lockfiles_of(dependabot_projects(show(".github/dependabot.yml") or ""))
    )
    prs: list[dict[str, Any]] = []
    for row in rows:
        head, segment = row["headRefName"], _segment(row["headRefName"])
        if row.get("isCrossRepository", True) or (segment is None and head != skills):
            continue  # a fork can name a branch dependabot/...: not the bot's
        if wanted is not None and row["number"] not in wanted:
            continue
        files = sorted(f["path"] for f in row.get("files") or [])
        lock_name = alert_status.LOCKFILE_FOR_ECOSYSTEM.get(
            ALERT_ECOSYSTEM.get(segment or "", "")
        )
        lockfile = next((f for f in files if PurePosixPath(f).name == lock_name), None)
        prs.append(
            {
                "number": row["number"],
                "title": row["title"],
                "body": row.get("body") or "",
                "head": head,
                "kind": "dependabot" if segment else "skills",
                "ecosystem": segment or "",
                "files": files,
                "lockfile": lockfile,
                "changes_main": None,
            }
        )
        if lockfile:
            lockfiles.add(lockfile)
    try:
        raw_alerts = alert_status.fetch_open_alerts(repo, runner)
        alerts_ok = True
    except GhError as exc:
        print(f"warning: could not read Dependabot alerts: {exc}", file=sys.stderr)
        raw_alerts, alerts_ok = [], False
    alerts = alerts_from_raw(raw_alerts)
    for alert in alerts.values():
        lockfiles.update(alert.lockfiles)

    texts: dict[str, str] = {}
    for lf in sorted(lockfiles):
        text = show(lf)
        try:
            if text is not None and PurePosixPath(lf).name == "uv.lock":
                alert_status.tomllib.loads(text)
        except alert_status.tomllib.TOMLDecodeError as exc:
            print(f"warning: could not read {lf}: {exc}", file=sys.stderr)
            text = None
        if text is not None:
            texts[lf] = text  # an unreadable lockfile's PRs fall back to merge-tree
    wanted_names: dict[str, set[tuple[str, str]]] = {lf: set() for lf in texts}
    for pr in prs:
        if pr["lockfile"] not in texts:
            pr["lockfile"] = None
            pr["changes_main"] = _changes_main(runner, base_ref, pr["number"])
            continue
        eco = ALERT_ECOSYSTEM.get(pr["ecosystem"], "")
        wanted_names[pr["lockfile"]].update(
            (b.package, normalize_name(eco, b.package)) for b in bumps_of(pr)[0]
        )
    for alert in alerts.values():
        for lf in alert.lockfiles:
            if lf in wanted_names:
                wanted_names[lf].add((alert.package, alert.package))

    main_versions: dict[str, dict[str, str]] = {}
    direct: dict[str, dict[str, Floor]] = {}
    for lf, text in texts.items():
        name = PurePosixPath(lf).name
        for raw_name, key in sorted(wanted_names[lf]):
            parsed = [
                v
                for v in map(
                    parse_version, alert_status.versions_in_lock(name, text, raw_name)
                )
                if v
            ]
            if parsed:
                main_versions.setdefault(lf, {})[key] = min(parsed).text
        manifest = _join(
            _norm_dir(str(PurePosixPath(lf).parent)), _MANIFEST_FOR_LOCK[name]
        )
        manifest_text = show(manifest)
        try:
            if manifest_text is not None:
                reader = (
                    _direct_from_package_json
                    if name == "bun.lock"
                    else _direct_from_pyproject
                )
                direct[lf] = reader(manifest_text, manifest)
        except (ValueError, alert_status.tomllib.TOMLDecodeError) as exc:
            print(f"warning: could not read {manifest}: {exc}", file=sys.stderr)
    return {
        "repo": repo,
        "base": base,
        "base_sha": base_sha,
        "collected_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "skills_branch": skills,
        "lockfiles": sorted(texts),
        "alerts_available": alerts_ok,
        "alerts": [asdict(a) for _, a in sorted(alerts.items())],
        "main_versions": main_versions,
        "direct": {
            lf: {k: list(v) for k, v in deps.items()} for lf, deps in direct.items()
        },
        "prs": sorted(prs, key=lambda p: p["number"]),
    }


# --- the post-merge recheck (I/O injected) --------------------------------------


def poll_interval(timeout_s: float, grace_s: float) -> float:
    """Seconds between polls: a tenth of the shorter positive window."""
    windows = [w for w in (grace_s, timeout_s) if w > 0]
    return min(windows) / _POLLS_PER_WINDOW if windows else 0.0


def wait_for_merge(
    state: Callable[[], tuple[str, str | None]],
    *,
    timeout_s: float,
    interval_s: float,
    now: Callable[[], datetime],
    pause: Callable[[float], None],
) -> datetime:
    """Block until the fold PR merges; return when it merged.

    Raises
    ------
    MergeWaitError
        Closed unmerged (code 3) or not merged within ``timeout_s`` (code 2).
    """
    deadline = now().timestamp() + timeout_s
    while True:
        pr_state, merged_at = state()
        if pr_state == "MERGED" and merged_at:
            return datetime.fromisoformat(merged_at)
        if pr_state == "CLOSED":
            raise MergeWaitError(
                "fold PR was closed without merging: nothing to recheck", 3
            )
        remaining = deadline - now().timestamp()
        if remaining <= 0:
            raise MergeWaitError(
                f"fold PR not merged after {timeout_s:.0f}s: "
                "re-run recheck once it merges",
                2,
            )
        pause(min(interval_s, remaining))


def recheck_after_merge(
    *,
    merged_at: datetime,
    listed: set[int],
    plan: Callable[[], tuple[set[int], dict[str, Any]]],
    grace_s: float,
    interval_s: float,
    now: Callable[[], datetime],
    pause: Callable[[float], None],
) -> dict[str, Any]:
    """Give GitHub ``grace_s`` after the merge to close the listed PRs, then judge.

    ``plan`` returns the open update PR numbers and a fresh plan. A listed PR still
    open after the grace is closed only if ``main`` now satisfies it: the fresh
    action is ``CLOSE_SUPERSEDED``, or ``CLOSE_ROUTINE`` for a grouped PR whose
    advisory members the fold took. Anything else is ``not_satisfied``.
    """
    deadline = merged_at.timestamp() + grace_s
    while True:
        open_now, fresh = plan()
        open_listed = open_now & listed
        remaining = deadline - now().timestamp()
        if not open_listed or remaining <= 0:
            break
        pause(min(interval_s, remaining))
    by_pr = {d["pr"]: d for d in fresh["decisions"] if d["pr"] is not None}
    satisfied = {
        n
        for n, d in by_pr.items()
        if d["action"] == "CLOSE_SUPERSEDED"
        or (d["grouped"] and d["action"] == "CLOSE_ROUTINE")
    }
    return {
        "to_close": sorted(open_listed & satisfied),
        "closed_by_github": sorted(listed - open_listed),
        "not_satisfied": sorted(open_listed - satisfied),
        "unlisted_superseded": sorted(
            n for n in satisfied - listed if by_pr[n]["action"] == "CLOSE_SUPERSEDED"
        ),
        "decisions": by_pr,
    }


# --- CLI ------------------------------------------------------------------------


def _pause(seconds: float) -> None:
    """Pace polls of a system with no push signal (the one pacing helper)."""
    time.sleep(seconds)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _close(runner: Runner, number: int, comment: str, execute: bool) -> None:
    if not execute:
        print(f"[dry run] would close #{number}: {comment.splitlines()[0]}")
        return
    runner(["gh", "pr", "close", str(number), "--comment", comment])
    print(f"closed #{number}")


def _numbers(values: Sequence[str]) -> list[int]:
    return [
        int(x.strip().lstrip("#")) for v in values for x in v.split(",") if x.strip()
    ]


def _recheck(
    args: argparse.Namespace,
    runner: Runner,
    root: Path,
    now: Callable[[], datetime],
    pause: Callable[[float], None],
) -> int:
    listed = set(_numbers(args.prs))
    interval = poll_interval(args.timeout, args.grace)

    def fold_state() -> tuple[str, str | None]:
        info = json.loads(
            runner(["gh", "pr", "view", str(args.fold_pr), "--json", "state,mergedAt"])
        )
        return info["state"], info.get("mergedAt")

    try:
        merged_at = wait_for_merge(
            fold_state,
            timeout_s=args.timeout,
            interval_s=interval,
            now=now,
            pause=pause,
        )
    except MergeWaitError as exc:
        print(f"recheck: {exc}", file=sys.stderr)
        return exc.code
    print(
        f"#{args.fold_pr} merged {merged_at.isoformat()}; "
        f"giving GitHub {args.grace}s to close {sorted(listed)}"
    )

    def fresh() -> tuple[set[int], dict[str, Any]]:
        snapshot = collect(runner, root, args.base)
        return {p["number"] for p in snapshot["prs"]}, plan_from_snapshot(snapshot)

    result = recheck_after_merge(
        merged_at=merged_at,
        listed=listed,
        plan=fresh,
        grace_s=args.grace,
        interval_s=interval,
        now=now,
        pause=pause,
    )
    print(f"closed by GitHub on its own: {result['closed_by_github'] or 'none'}")
    for n in result["to_close"]:
        _close(
            runner,
            n,
            close_comment(
                result["decisions"][n], fold_pr=args.fold_pr, grace_s=args.grace
            ),
            args.execute,
        )
    for n in result["not_satisfied"]:
        d = result["decisions"].get(n)
        why = f"{d['action']}: {d['reason']}" if d else "no longer an update PR"
        print(f"LEFT OPEN #{n}: main does not satisfy it ({why}); tell the user")
    if result["unlisted_superseded"]:
        print(
            "also superseded but not approved for closing: "
            f"{result['unlisted_superseded']}; ask before closing"
        )
    return 4 if result["not_satisfied"] else 0


def main(
    argv: Sequence[str] | None = None,
    runner: Runner = _run,
    now: Callable[[], datetime] = _utcnow,
    pause: Callable[[float], None] = _pause,
) -> int:
    """CLI entry point; see the module docstring for subcommands and exit codes."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base", default="main", help="base branch (default: main)")
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="repo root (default: cwd)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan", help="collect and classify; print the plan")
    p_plan.add_argument(
        "--snapshot-out", type=Path, help="also write the collected snapshot here"
    )
    p_plan.add_argument("--format", choices=("md", "json"), default="md")
    p_close = sub.add_parser("close", help="close approved CLOSE_* PRs now")
    p_close.add_argument(
        "--prs", required=True, nargs="+", help="PR numbers (spaces or commas)"
    )
    p_close.add_argument(
        "--execute", action="store_true", help="actually close (default: dry run)"
    )
    p_re = sub.add_parser(
        "recheck", help="after the fold PR merges, close what GitHub left open"
    )
    p_re.add_argument("--fold-pr", required=True, type=int)
    p_re.add_argument(
        "--prs", required=True, nargs="+", help="PRs approved to close after the merge"
    )
    p_re.add_argument(
        "--execute", action="store_true", help="actually close (default: dry run)"
    )
    p_re.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help="seconds to wait for the merge",
    )
    p_re.add_argument(
        "--grace",
        type=float,
        default=DEFAULT_GRACE_S,
        help="seconds after the merge to let GitHub act",
    )
    args = parser.parse_args(argv)
    if alert_status.tomllib is None:
        print(
            "update_prs.py needs Python 3.11+ (tomllib); run it with `uv run python`.",
            file=sys.stderr,
        )
        return 64
    root = args.root.resolve()
    try:
        if args.cmd == "plan":
            snapshot = collect(runner, root, args.base)
            if args.snapshot_out:
                args.snapshot_out.write_text(
                    json.dumps(snapshot, indent=2), encoding="utf-8"
                )
            plan = plan_from_snapshot(snapshot)
            print(
                json.dumps(plan, indent=2)
                if args.format == "json"
                else render_markdown(plan, snapshot)
            )
            return 0
        if args.cmd == "close":
            numbers = _numbers(args.prs)
            snapshot = collect(runner, root, args.base, only=numbers)
            by_pr = {
                d["pr"]: d
                for d in plan_from_snapshot(snapshot)["decisions"]
                if d["pr"] is not None
            }
            refused = 0
            for n in numbers:
                d = by_pr.get(n)
                if d is None or not d["action"].startswith("CLOSE_"):
                    now_is = d["action"] if d else "not an open update PR"
                    print(f"refusing #{n}: now {now_is}; re-plan", file=sys.stderr)
                    refused += 1
                    continue
                _close(runner, n, close_comment(d), args.execute)
            return 1 if refused else 0
        return _recheck(args, runner, root, now, pause)
    except GhError as exc:
        print(f"gh/git failed: {exc}", file=sys.stderr)
        return 64


if __name__ == "__main__":
    sys.exit(main())
