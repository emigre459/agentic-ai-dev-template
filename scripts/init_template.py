#!/usr/bin/env python3
"""Initialize this dual-stack template into a single-stack project.

`make init STACK=python|react ...` promotes the chosen stack's files to the repo
root, prunes the other stack, rewrites generated files, and deletes template-only
initialization machinery. Repository settings are reconciled separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import keyword
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only: this file runs as a plain script, where `scripts.` isn't importable.
    from scripts.apply_repo_settings import CommandResult

STACKS = ("python", "react")

# Files rewritten at init: (relative path, marker kind). "html" -> filter_html_markers,
# "yaml" -> filter_yaml_markers, "fill" -> placeholders only.
_HTML_FILES = ("AGENTS.md", "README.md")
_YAML_FILES = (".github/workflows/ci.yml", ".github/dependabot.yml")
_FILL_ONLY = (
    "AGENTS.md",
    "pyproject.toml",
    "package.json",
    "index.html",
    "src/App.tsx",
    "README.md",
)

# Manifests + lockfiles where the PEP 508 / npm-safe placeholder name must be
# replaced with the user's chosen project name. (See plan note in Task 16:
# {{PROJECT_NAME}} is invalid in these files, so we use a literal placeholder
# package name.) Lockfiles are included so they stay consistent with the manifest
# and the first `make deps` doesn't produce a spurious dirty diff.
_MANIFEST_FILES = ("pyproject.toml", "package.json", "uv.lock", "bun.lock")
_MANIFEST_PLACEHOLDER = "template-placeholder-project-name"

# Parallel CI jobs shipped per stack. Pre-init the job ids carry a stack prefix
# (both stacks coexist, duplicate ids are invalid YAML); init strips the kept
# stack's prefix and requires the resulting contexts as status checks.
_CI_JOBS = ("lint", "tests", "security")
# The GitHub Actions app — pinning integration_id means only Actions-reported
# check runs satisfy the required contexts.
_GITHUB_ACTIONS_INTEGRATION_ID = 15368


def _toml_escape(value: str) -> str:
    """Escape a string for a TOML basic (double-quoted) string value."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _json_escape(value: str) -> str:
    """Escape a string for a JSON double-quoted string value (without the quotes)."""
    return json.dumps(value)[1:-1]


# Manifest/lockfile formats — the project name and description get spliced into
# quoted TOML/JSON string fields here, so values with ", \, or newlines must be
# escaped for the target format or they produce an invalid manifest.
_MANIFEST_FORMAT: dict[str, Callable[[str], str]] = {
    "pyproject.toml": _toml_escape,
    "uv.lock": _toml_escape,
    "package.json": _json_escape,
    "bun.lock": _json_escape,
}


def _other(stack: str) -> str:
    return "react" if stack == "python" else "python"


def ensure_clean_worktree(
    root: Path | str,
    runner: Callable[..., CommandResult] = subprocess.run,
) -> None:
    """Refuse initialization when the repository contains existing local work.

    Parameters
    ----------
    root
        Repository root to inspect.
    runner
        Subprocess-compatible command runner, by default ``subprocess.run``.

    Raises
    ------
    RuntimeError
        If Git is unavailable, status cannot be read, or the repository contains
        tracked or untracked changes.
    """
    try:
        result = runner(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=Path(root),
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Git is not installed or is not available on PATH. Install Git, "
            "then rerun initialization."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "No error detail returned.").strip()
        raise RuntimeError(f"`git status` failed: {detail}") from exc
    changed = result.stdout.strip()
    if changed:
        raise RuntimeError(
            "Refusing to initialize because the repository has existing local "
            "changes. Commit or stash them, then rerun `make init`.\n" + changed
        )


def python_package_name(project_name: str) -> str:
    """Derive a Python import package from a kebab-case project name.

    Parameters
    ----------
    project_name
        Distribution/project name supplied during initialization.

    Returns
    -------
    str
        Import-safe package name with hyphens converted to underscores.

    Raises
    ------
    ValueError
        If the derived value is not a valid Python identifier.
    """
    package_name = project_name.replace("-", "_")
    if not package_name.isidentifier() or keyword.iskeyword(package_name):
        raise ValueError(
            f"{project_name!r} cannot produce a valid Python package name. "
            "Use lowercase kebab-case beginning with a letter, for example "
            "'acme-service'."
        )
    return package_name


def filter_html_markers(text: str, keep: str) -> str:
    """Drop the non-kept stack's HTML-marker blocks; unwrap the kept stack's markers."""
    other = _other(keep)
    text = re.sub(
        rf"<!-- STACK:{other} -->.*?<!-- /STACK:{other} -->\n?",
        "",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(rf"<!-- /?STACK:{keep} -->\n?", "", text)
    return text


def filter_yaml_markers(text: str, keep: str) -> str:
    """Drop every YAML-marker block except the kept stack's; unwrap the kept markers.

    The ``template`` block is always dropped (machinery is removed post-init).
    """
    for name in ("python", "react", "template"):
        if name == keep:
            continue
        text = re.sub(
            rf"^[ \t]*# >>> STACK:{name}\b.*?^[ \t]*# <<< STACK:{name}\b.*?\n",
            "",
            text,
            flags=re.DOTALL | re.MULTILINE,
        )
    text = re.sub(
        rf"^[ \t]*# (?:>>>|<<<) STACK:{keep}\b.*?\n", "", text, flags=re.MULTILINE
    )
    return text


def strip_stack_job_prefix(text: str, stack: str) -> str:
    """Rename the kept stack's CI job ids from ``<stack>-<job>`` to ``<job>``.

    Only exact job-id lines are touched (two-space indent, known job names), so
    step names, paths, and other ``<stack>-`` strings are left alone.
    """
    return re.sub(
        rf"^  {re.escape(stack)}-({'|'.join(_CI_JOBS)}):",
        r"  \1:",
        text,
        flags=re.MULTILINE,
    )


def strip_interview(text: str) -> str:
    """Remove the README interview prompt block."""
    return re.sub(
        r"<!-- INTERVIEW:start -->.*?<!-- INTERVIEW:end -->\n?",
        "",
        text,
        flags=re.DOTALL,
    )


def fill_placeholders(text: str, project_name: str, description: str) -> str:
    """Replace ``{{PROJECT_NAME}}`` and ``{{DESCRIPTION}}`` tokens."""
    return text.replace("{{PROJECT_NAME}}", project_name).replace(
        "{{DESCRIPTION}}", description
    )


def _promote(root: Path, stack: str) -> None:
    """Move stacks/<stack>/* up to the repo root, replacing colliding entries."""
    chosen = root / "stacks" / stack
    for item in chosen.iterdir():
        dest = root / item.name
        if dest.is_dir():
            shutil.rmtree(dest)
        elif dest.exists():
            dest.unlink()
        shutil.move(str(item), str(dest))


def _rewrite_dependabot_dir(root: Path, stack: str) -> None:
    """After promotion the kept stack lives at root -> point dependabot at '/'."""
    path = root / ".github" / "dependabot.yml"
    if path.exists():
        text = path.read_text(encoding="utf-8").replace(f"/stacks/{stack}", "/")
        path.write_text(text, encoding="utf-8")


def _rewrite_ci_workdir(root: Path, stack: str) -> None:
    """After promotion the kept stack lives at root -> drop the stacks/ workdir.

    The kept CI job declares ``working-directory: stacks/<stack>``; once the stack
    is promoted to the repo root that path no longer exists, so point it at the
    repo root (``.``) — otherwise the spawned repo's first CI run fails.
    """
    path = root / ".github" / "workflows" / "ci.yml"
    if path.exists():
        text = path.read_text(encoding="utf-8").replace(
            f"working-directory: stacks/{stack}", "working-directory: ."
        )
        path.write_text(text, encoding="utf-8")


def _rewrite_ci_job_ids(root: Path, stack: str) -> None:
    """Strip the kept stack's prefix from CI job ids (see strip_stack_job_prefix)."""
    path = root / ".github" / "workflows" / "ci.yml"
    if path.exists():
        path.write_text(
            strip_stack_job_prefix(path.read_text(encoding="utf-8"), stack),
            encoding="utf-8",
        )


def _set_required_status_checks(root: Path) -> None:
    """Point the ruleset's required_status_checks at the seeded repo's CI jobs.

    The shipped ruleset.json requires the TEMPLATE repo's own CI contexts
    (stack-prefixed jobs + the template-machinery job), so `make
    apply_repo_settings` run on the template repo itself gates on its real CI.
    Seeded repos get plain lint/tests/security jobs (see _rewrite_ci_job_ids),
    so init rewrites the rule's contexts to match before settings are applied.
    """
    path = root / ".github" / "repo-settings" / "ruleset.json"
    if not path.exists():
        return
    ruleset = json.loads(path.read_text(encoding="utf-8"))
    rules = ruleset.setdefault("rules", [])
    checks = [
        {"context": job, "integration_id": _GITHUB_ACTIONS_INTEGRATION_ID}
        for job in _CI_JOBS
    ]
    for rule in rules:
        if rule.get("type") == "required_status_checks":
            rule.setdefault("parameters", {})["required_status_checks"] = checks
            break
    else:
        rules.append(
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "do_not_enforce_on_create": False,
                    "required_status_checks": checks,
                },
            }
        )
    path.write_text(json.dumps(ruleset, indent=2) + "\n", encoding="utf-8")


def _substitute_manifest_name(root: Path, project_name: str) -> None:
    """Replace the manifest placeholder package name with the chosen project name.

    Both shipped manifests (`stacks/python/pyproject.toml`,
    `stacks/react/package.json`) declare a literal placeholder package name —
    `template-placeholder-project-name` — because `{{PROJECT_NAME}}` is not a
    valid PEP 508 / npm package identifier. We swap that literal here.
    """
    for rel in _MANIFEST_FILES:
        p = root / rel
        if p.exists():
            escape = _MANIFEST_FORMAT.get(rel, lambda s: s)
            text = p.read_text(encoding="utf-8")
            p.write_text(
                text.replace(_MANIFEST_PLACEHOLDER, escape(project_name)),
                encoding="utf-8",
            )


def _rename_python_package(root: Path, project_name: str) -> None:
    """Rename the sample Python package and every generated import.

    Parameters
    ----------
    root
        Initialized repository root after the Python stack is promoted.
    project_name
        Kebab-case distribution name supplied by the user.
    """
    package_name = python_package_name(project_name)
    source = root / "src" / "example_app"
    destination = root / "src" / package_name
    if not source.is_dir():
        raise RuntimeError(f"sample Python package not found: {source}")
    if destination.exists() and destination != source:
        raise RuntimeError(f"Python package destination already exists: {destination}")
    if destination != source:
        shutil.move(str(source), str(destination))

    pyproject = root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    text = text.replace(
        'module-name = "example_app"', f'module-name = "{package_name}"'
    )
    text = text.replace(
        "# Sample package lives at `src/example_app/`; init swaps both the project name\n"
        "# and this module-name to the user's chosen package at `make init`.\n",
        f"# Build the project package from `src/{package_name}/`.\n",
    )
    pyproject.write_text(text, encoding="utf-8")

    for test_file in (root / "tests").rglob("*.py"):
        test_file.write_text(
            test_file.read_text(encoding="utf-8").replace("example_app", package_name),
            encoding="utf-8",
        )


# --- Skills pinned to the template's latest release (#49, layer L1) -------------

_TEMPLATE_SOURCE = "emigre459/agentic-ai-dev-template"
_LOCK_FILE = "skills-lock.json"
_HASH_SCRIPT = Path(".github") / "actions" / "skills-drift" / "skills-lock.mjs"
_SKILLS_DIR = Path(".agents") / "skills"

# ICU root collation order of the printable ASCII punctuation, the one
# `String.prototype.localeCompare` uses: whitespace, then these, then digits,
# then letters (see `_locale_sort_key`).
_PUNCTUATION_ORDER = " _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$"
_DIGITS_ORDER = "0123456789"


def _locale_sort_key(name: str) -> tuple[list[int], list[int]]:
    """Sort key emulating ``String.prototype.localeCompare`` (ICU root collation).

    The ``skills`` CLI orders the files it hashes with ``localeCompare``; the hash
    only matches if this port orders them the same way. Only the ordering rules
    that occur in a skill folder's relative POSIX paths are emulated:

    - primary: whitespace, then punctuation and symbols (in ``_PUNCTUATION_ORDER``
      order, so ``_`` < ``-`` < ``.`` < ``/``), then digits, then letters compared
      case-insensitively; a shorter string that is a prefix sorts first;
    - tertiary, only on a full primary tie: lowercase before uppercase, decided
      at the first differing letter.

    Accented and other non-ASCII characters are not collated like ICU does (they
    sort after ASCII letters by code point). That is outside this port's scope:
    it only ever hashes this repo's own skills, and a wrong order surfaces as a
    hash mismatch, never as silently accepted content. The node-vs-port test over
    the real skills is the guard.

    Parameters
    ----------
    name
        Relative POSIX path of a file inside a skill folder.

    Returns
    -------
    tuple[list[int], list[int]]
        ``(primary weights, tertiary weights)``; compare as a tuple.
    """
    primary: list[int] = []
    tertiary: list[int] = []
    for char in name:
        lower = char.lower()
        if char in _PUNCTUATION_ORDER:
            primary.append(_PUNCTUATION_ORDER.index(char))
        elif char in _DIGITS_ORDER:
            primary.append(len(_PUNCTUATION_ORDER) + _DIGITS_ORDER.index(char))
        elif "a" <= lower <= "z":
            primary.append(
                len(_PUNCTUATION_ORDER) + len(_DIGITS_ORDER) + ord(lower) - ord("a")
            )
        else:
            primary.append(1000 + ord(char))
        tertiary.append(1 if char != lower else 0)
    return primary, tertiary


def _skill_files(skill_dir: Path) -> list[Path]:
    """List the regular files under ``skill_dir`` the way the skills CLI does.

    Skips ``.git`` and ``node_modules`` directories and never follows symlinks
    (a symlink, to a file or a directory, is not a regular file and is ignored).
    """
    found: list[Path] = []
    for entry in skill_dir.iterdir():
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if entry.name not in (".git", "node_modules"):
                found.extend(_skill_files(entry))
        elif entry.is_file():
            found.append(entry)
    return found


def _ordered_skill_files(skill_dir: Path) -> list[Path]:
    """Return the skill folder's hashed files in the CLI's ``localeCompare`` order."""
    return sorted(
        _skill_files(skill_dir),
        key=lambda path: _locale_sort_key(path.relative_to(skill_dir).as_posix()),
    )


def _python_skill_hash(skill_dir: Path) -> str:
    """SHA-256 of a skill folder, identical to the ``skills`` CLI's ``computedHash``.

    A stdlib port of ``computeSkillFolderHash`` for machines without ``node``: the
    digest of every regular file, in ``localeCompare`` order of its POSIX relative
    path, each contributing its path then its bytes. Tested against the CLI's own
    output (``tests/template/fixtures/skills_lock/golden.json``).
    """
    digest = hashlib.sha256()
    for path in _ordered_skill_files(skill_dir):
        digest.update(path.relative_to(skill_dir).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _skill_hash(root: Path, skill_dir: Path, runner: Callable[..., Any]) -> str:
    """Hash ``skill_dir`` with the shipped node script, or the Python port without node."""
    script = root / _HASH_SCRIPT
    if shutil.which("node") is None or not script.is_file():
        return _python_skill_hash(skill_dir)
    result = runner(
        ["node", str(script), "hash", str(skill_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    return str(result.stdout).strip()


def _read_skills_lock(root: Path) -> dict[str, Any] | None:
    """Return the parsed ``skills-lock.json`` under ``root``, or None when absent."""
    path = root / _LOCK_FILE
    if not path.is_file():
        return None
    lock: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return lock


def _lock_ref(lock: dict[str, Any] | None) -> str | None:
    """Return the release ref every template entry of ``lock`` is pinned to.

    Raises
    ------
    RuntimeError
        If the entries are pinned to different refs (or only some have one).
    """
    if lock is None:
        return None
    refs = {entry.get("ref") for entry in lock.get("skills", {}).values()}
    if len(refs) > 1:
        raise RuntimeError(
            f"{_LOCK_FILE} entries disagree on `ref` ({sorted(map(str, refs))}); "
            "every template skill must be pinned to the same release."
        )
    return next(iter(refs), None)


def _extract_skill_at_ref(
    root: Path,
    source: str,
    ref: str,
    name: str,
    scratch: Path,
    runner: Callable[..., Any],
) -> Path:
    """Extract ``.agents/skills/<name>`` as tagged at ``ref`` into ``scratch``.

    ``git fetch`` and ``git archive`` run through ``runner``; the tar is unpacked
    with the ``data`` extraction filter. Returns the extracted skill folder.
    """
    tar_path = scratch / f"{name}.tar"
    skill_path = (_SKILLS_DIR / name).as_posix()
    try:
        runner(
            ["git", "-C", str(root), "archive", "--format=tar", "-o", str(tar_path)]
            + ["FETCH_HEAD", skill_path],
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise _network_error(source, ref, exc) from exc
    out = scratch / f"{name}-extracted"
    out.mkdir()
    with tarfile.open(tar_path) as tar:
        tar.extractall(out, filter="data")
    extracted = out / skill_path
    if not extracted.is_dir():
        raise RuntimeError(
            f"{source}@{ref} has no {skill_path} folder for skill {name!r}"
        )
    return extracted


def _network_error(source: str, ref: str, exc: Exception) -> RuntimeError:
    """Build the error for a failed fetch/archive of ``source`` at ``ref``."""
    detail = str(getattr(exc, "stderr", "") or exc).strip()
    return RuntimeError(
        f"`make init` needs network access to github.com to fetch {source} at "
        f"{ref} (the skills ship pinned to that release), and git could not: "
        f"{detail}"
    )


def _pin_skills_to_release(
    root: Path,
    runner: Callable[..., Any],
    lock: dict[str, Any] | None = None,
) -> None:
    """Replace the vendored skills with the lock's tagged release, hash-verified.

    For every lock entry that has a ``ref``: fetch that tag from the entry's
    ``source``, extract ``.agents/skills/<name>`` from it, and require its hash to
    equal ``computedHash``. Only once every skill verified are the folders
    replaced, so a failure leaves the tree untouched. Entries without a ``ref``
    (before the first release) and a missing lock file leave everything alone.

    Parameters
    ----------
    root
        Repository root being initialized (must be a git repository).
    runner
        Subprocess-compatible command runner, by default ``subprocess.run``.
    lock
        Already-parsed lock; read from ``root`` when omitted.

    Raises
    ------
    RuntimeError
        If git cannot fetch the release (no network), a skill is missing from it,
        or a skill's hash differs from the lock's.
    """
    if lock is None:
        lock = _read_skills_lock(root)
    if lock is None:
        return
    pinned: dict[tuple[str, str], list[str]] = {}
    for name, entry in lock.get("skills", {}).items():
        if entry.get("ref"):
            pinned.setdefault((entry["source"], entry["ref"]), []).append(name)
    if not pinned:
        return

    with tempfile.TemporaryDirectory(prefix="init-skills-") as tmp:
        scratch = Path(tmp)
        verified: dict[str, Path] = {}
        for (source, ref), names in pinned.items():
            try:
                runner(
                    ["git", "-C", str(root), "fetch", "--depth", "1"]
                    + [f"https://github.com/{source}.git", "tag", ref],
                    check=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                raise _network_error(source, ref, exc) from exc
            for name in names:
                extracted = _extract_skill_at_ref(
                    root, source, ref, name, scratch, runner
                )
                expected = lock["skills"][name]["computedHash"]
                actual = _skill_hash(root, extracted, runner)
                if actual != expected:
                    raise RuntimeError(
                        f"skill {name!r} at {source}@{ref} hashes to {actual}, but "
                        f"{_LOCK_FILE} has {expected}; refusing to initialize "
                        "from a release that does not match the lock."
                    )
                verified[name] = extracted
        for name, extracted in verified.items():
            dest = root / _SKILLS_DIR / name
            if dest.is_dir():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(extracted), str(dest))


def _point_actions_at_release(root: Path, ref: str | None) -> None:
    """Make workflows use the released composite actions instead of local copies.

    Rewrites ``uses: ./.github/actions/skills-<x>`` in ``.github/workflows/*.yml``
    to ``uses: <template source>/.github/actions/skills-<x>@<ref>`` (indentation
    and any ``- `` prefix kept), then deletes ``.github/actions/skills-*``. Does
    nothing when ``ref`` is None (before the first release the local copies are
    the only ones that exist).
    """
    if ref is None:
        return
    pattern = re.compile(
        r"^([ \t]*(?:-[ \t]+)?uses:[ \t]*)\./\.github/actions/(skills-[A-Za-z0-9_-]+)",
        flags=re.MULTILINE,
    )
    for workflow in sorted((root / ".github" / "workflows").glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        text = pattern.sub(
            lambda m: f"{m[1]}{_TEMPLATE_SOURCE}/.github/actions/{m[2]}@{ref}", text
        )
        workflow.write_text(text, encoding="utf-8")
    for action in sorted((root / ".github" / "actions").glob("skills-*")):
        if action.is_dir():
            shutil.rmtree(action)


def initialize(
    root: Path | str,
    stack: str,
    project_name: str,
    description: str,
    apply_settings: bool = False,
    repo: str | None = None,
    runner: Callable[..., object] = subprocess.run,
) -> None:
    """Collapse the template into a single-stack project rooted at ``root``."""
    root = Path(root)
    if stack not in STACKS:
        raise ValueError(f"stack must be one of {STACKS}, got {stack!r}")
    if not (root / "stacks").is_dir():
        raise RuntimeError("no stacks/ directory — repo already initialized?")
    if not (root / "stacks" / stack).is_dir():
        raise RuntimeError(f"stacks/{stack} not found")
    # Validate stack-specific inputs before _promote or any deletion. Keeping
    # this ahead of all mutations makes a rejected initialization safely
    # retryable without requiring Git recovery.
    if stack == "python":
        python_package_name(project_name)

    # 2. Promote chosen stack to root, then 3. prune.
    _promote(root, stack)
    shutil.rmtree(root / "stacks")
    other_rules = root / ".agents" / "rules" / _other(stack)
    if other_rules.is_dir():
        shutil.rmtree(other_rules)

    # 4. Rewrite generated files.
    for rel in _HTML_FILES:
        p = root / rel
        if p.exists():
            p.write_text(
                filter_html_markers(p.read_text(encoding="utf-8"), stack),
                encoding="utf-8",
            )
    for rel in _YAML_FILES:
        p = root / rel
        if p.exists():
            p.write_text(
                filter_yaml_markers(p.read_text(encoding="utf-8"), stack),
                encoding="utf-8",
            )
    readme = root / "README.md"
    if readme.exists():
        readme.write_text(
            strip_interview(readme.read_text(encoding="utf-8")), encoding="utf-8"
        )
    _rewrite_dependabot_dir(root, stack)
    _rewrite_ci_workdir(root, stack)
    _rewrite_ci_job_ids(root, stack)
    _set_required_status_checks(root)
    if stack == "python":
        _rename_python_package(root, project_name)
    for rel in _FILL_ONLY:
        p = root / rel
        if p.exists():
            text = p.read_text(encoding="utf-8")
            escape = _MANIFEST_FORMAT.get(rel)
            if escape is not None:
                # Manifest (TOML/JSON): escape values so a name/description with
                # ", \, or newlines doesn't produce an invalid manifest.
                text = text.replace("{{PROJECT_NAME}}", escape(project_name)).replace(
                    "{{DESCRIPTION}}", escape(description)
                )
            else:
                text = fill_placeholders(text, project_name, description)
            p.write_text(text, encoding="utf-8")
    _substitute_manifest_name(root, project_name)

    # 4b. Start on the template's pinned skills release (layer L1 of the consumer
    # kit): swap in the tagged skill folders, then point workflows at the released
    # composite actions. Both are no-ops before the first release (no `ref`). They
    # come before settings and self-destruct so a failure (e.g. no network) leaves
    # the machinery in place for a retry.
    lock = _read_skills_lock(root)
    ref = _lock_ref(lock)  # validates before anything is fetched
    _pin_skills_to_release(root, runner, lock)
    _point_actions_at_release(root, ref)

    # 5. Apply repo settings BEFORE self-destruct, so that if the apply step fails
    # or is declined the init machinery is still present and the operator has
    # recourse (e.g. re-run, or fall back to `make apply_repo_settings`). Note the
    # default `make init` path passes apply_settings=False — settings are applied by
    # the separate, idempotent `make apply_repo_settings` step (see README).
    if apply_settings:
        if repo is None:
            raise ValueError("repo is required when apply_settings is enabled")
        runner(
            [
                sys.executable,
                str(root / "scripts" / "apply_repo_settings.py"),
                "--repo",
                repo,
                "--phase",
                "bootstrap",
            ],
            check=True,
        )

    # 6. Self-destruct template-only machinery.
    to_remove = [
        root / "scripts" / "init_template.py",
        root / "scripts" / "preflight_init.py",
        root / "tests" / "template",
    ]
    if stack == "react":
        to_remove += [
            root / "pyproject.toml",
            root / "uv.lock",
            root / ".python-version",
            root / "scripts" / "__init__.py",
        ]
    for p in to_remove:
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    tests_dir = root / "tests"
    if tests_dir.is_dir() and not any(tests_dir.iterdir()):
        tests_dir.rmdir()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `make init`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", required=True, choices=STACKS)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument(
        "--repo",
        help="explicit owner/repository settings target (required with --apply-settings)",
    )
    # Settings are applied by the separate `make apply_repo_settings` step (idempotent,
    # confirm-gated, re-runnable) — see the README interview flow. `make init` only
    # transforms the tree. Pass --apply-settings to fold the apply into init.
    parser.add_argument(
        "--apply-settings",
        action="store_true",
        help="reconcile settings during init; requires --repo",
    )
    args = parser.parse_args(argv)
    if args.apply_settings and not args.repo:
        parser.error("--repo is required when --apply-settings is used")
    try:
        ensure_clean_worktree(Path.cwd())
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    initialize(
        Path.cwd(),
        args.stack,
        args.project_name,
        args.description,
        apply_settings=args.apply_settings,
        repo=args.repo,
    )
    print(
        f"Initialized as a {args.stack} project. "
        "Run `make apply_repo_settings_bootstrap TARGET_REPO=owner/repository` "
        "before the setup PR, then finalize settings after it merges."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
