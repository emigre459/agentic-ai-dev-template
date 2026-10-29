#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate a skill directory against the Agent Skills specification.

Checks mechanical/structural rules that can be verified programmatically.
Qualitative checks (description quality, instruction clarity, etc.) are
handled by the LLM in the review-skill SKILL.md instructions.

Standard library only; run it with plain ``python3``.

Usage::

    python3 validate.py <path-to-skill-directory-or-SKILL.md> [--json <report.json>]

Exit codes: 0 when no check failed, 1 when at least one check failed, 2 on a
usage error. ``--json`` writes ``{"results": [{"level", "check", "message"}]}``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ── helpers ──────────────────────────────────────────────────────────

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

results: list[dict[str, str]] = []


def record(category: str, check: str, status: str, detail: str = "") -> None:
    """Append one check result to the module-level ``results`` list.

    Parameters
    ----------
    category : str
        Report section the check belongs to.
    check : str
        Short name of the check.
    status : str
        One of ``PASS``, ``FAIL``, ``WARN`` or ``SKIP``.
    detail : str
        Optional extra context shown next to the check.
    """
    results.append(
        {"category": category, "check": check, "status": status, "detail": detail}
    )


def parse_frontmatter(text: str) -> tuple[dict[str, str] | None, str, str | None]:
    """Extract YAML frontmatter from SKILL.md content.

    Parameters
    ----------
    text : str
        Full ``SKILL.md`` text.

    Returns
    -------
    tuple[dict[str, str] | None, str, str | None]
        The frontmatter dict (``None`` on error), the body, and an error
        message (``None`` on success).
    """
    if not text.startswith("---"):
        return None, text, "File does not start with '---' frontmatter delimiter"

    end = text.find("---", 3)
    if end == -1:
        return None, text, "No closing '---' frontmatter delimiter found"

    raw_fm = text[3:end].strip()
    body = text[end + 3 :].strip()

    # Simple YAML-ish parser — handles the flat key: value pairs we care about.
    # For nested/multiline values we just capture the raw string.
    fm = {}
    current_key = None
    current_value_lines = []

    for line in raw_fm.split("\n"):
        # top-level key
        m = re.match(r"^([a-zA-Z_-]+)\s*:\s*(.*)", line)
        if m and not line.startswith("  ") and not line.startswith("\t"):
            if current_key is not None:
                fm[current_key] = "\n".join(current_value_lines).strip()
            current_key = m.group(1)
            current_value_lines = [m.group(2)]
        else:
            current_value_lines.append(line)

    if current_key is not None:
        fm[current_key] = "\n".join(current_value_lines).strip()

    # Clean up values — strip surrounding quotes, pipe-literal leading whitespace
    for k, v in fm.items():
        if v.startswith("|"):
            # YAML literal block — join continuation lines
            lines = v.split("\n")[1:]  # skip the '|' line
            fm[k] = "\n".join(line.strip() for line in lines).strip()
        elif v.startswith('"') and v.endswith('"'):
            fm[k] = v[1:-1]
        elif v.startswith("'") and v.endswith("'"):
            fm[k] = v[1:-1]

    return fm, body, None


# ── checks ───────────────────────────────────────────────────────────


def check_file_structure(
    skill_dir: str,
) -> tuple[dict[str, str] | None, str | None, str]:
    """Check the skill folder layout and read ``SKILL.md``.

    Records results for the kebab-case folder name, the exact ``SKILL.md``
    casing and the absence of a ``README.md``.

    Parameters
    ----------
    skill_dir : str
        Path to the skill directory.

    Returns
    -------
    tuple[dict[str, str] | None, str | None, str]
        Parsed frontmatter, body and directory name; frontmatter and body are
        ``None`` when ``SKILL.md`` is missing.
    """
    cat = "File structure"
    dir_name = os.path.basename(os.path.normpath(skill_dir))

    # Kebab-case folder name
    if re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", dir_name):
        record(cat, "Folder name is kebab-case", PASS, dir_name)
    else:
        record(
            cat,
            "Folder name is kebab-case",
            FAIL,
            f"'{dir_name}' is not valid kebab-case (lowercase letters, numbers, hyphens only; no leading/trailing/consecutive hyphens)",
        )

    # SKILL.md exists with exact casing
    entries = os.listdir(skill_dir)
    if "SKILL.md" in entries:
        record(cat, "SKILL.md exists with correct casing", PASS)
    else:
        # Check for case-insensitive variants
        variants = [e for e in entries if e.lower() == "skill.md"]
        if variants:
            record(
                cat,
                "SKILL.md exists with correct casing",
                FAIL,
                f"Found '{variants[0]}' — must be exactly 'SKILL.md'",
            )
        else:
            record(
                cat,
                "SKILL.md exists with correct casing",
                FAIL,
                "No SKILL.md file found",
            )
        return None, None, dir_name  # can't continue without SKILL.md

    # No README.md
    if "README.md" in entries or "readme.md" in entries:
        readme_variant = "README.md" if "README.md" in entries else "readme.md"
        record(
            cat,
            "No README.md in skill folder",
            FAIL,
            f"Found '{readme_variant}' — documentation should go in SKILL.md or references/",
        )
    else:
        record(cat, "No README.md in skill folder", PASS)

    # Read SKILL.md
    skill_path = os.path.join(skill_dir, "SKILL.md")
    with open(skill_path, "r", encoding="utf-8") as f:
        content = f.read()

    fm, body, err = parse_frontmatter(content)
    return fm, body, dir_name


def check_frontmatter(
    fm: dict[str, str] | None, body: str | None, dir_name: str
) -> None:
    """Check the ``SKILL.md`` frontmatter fields and record each result.

    Parameters
    ----------
    fm : dict[str, str] | None
        Parsed frontmatter, or ``None`` if it could not be parsed.
    body : str | None
        Unused; accepted for call-site symmetry.
    dir_name : str
        Skill directory name that ``name`` must match.
    """
    cat = "YAML frontmatter"

    if fm is None:
        record(
            cat,
            "Frontmatter present and parseable",
            FAIL,
            "Could not parse frontmatter",
        )
        return

    record(cat, "Frontmatter present and parseable", PASS)

    # ── name field ──
    name = fm.get("name", "")
    if not name:
        record(cat, "name field present", FAIL, "Missing 'name' field")
    else:
        record(cat, "name field present", PASS, name)

        if len(name) <= 64:
            record(cat, "name ≤ 64 characters", PASS, f"{len(name)} chars")
        else:
            record(cat, "name ≤ 64 characters", FAIL, f"{len(name)} chars")

        if re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
            record(cat, "name is valid kebab-case", PASS)
        else:
            issues = []
            if re.search(r"[A-Z]", name):
                issues.append("contains uppercase")
            if name.startswith("-") or name.endswith("-"):
                issues.append("starts or ends with hyphen")
            if "--" in name:
                issues.append("contains consecutive hyphens")
            if re.search(r"[^a-z0-9-]", name):
                issues.append("contains disallowed characters")
            record(
                cat,
                "name is valid kebab-case",
                FAIL,
                "; ".join(issues) if issues else "Invalid format",
            )

        for reserved in ["anthropic", "claude"]:
            if reserved in name.lower():
                record(
                    cat,
                    f"name does not contain reserved word '{reserved}'",
                    FAIL,
                    f"'{name}' contains '{reserved}'",
                )
            else:
                record(cat, f"name does not contain reserved word '{reserved}'", PASS)

        if name == dir_name:
            record(cat, "name matches directory name", PASS)
        else:
            record(
                cat,
                "name matches directory name",
                FAIL,
                f"name='{name}' but directory='{dir_name}'",
            )

    # ── description field ──
    desc = fm.get("description", "")
    if not desc:
        record(cat, "description field present", FAIL, "Missing 'description' field")
    else:
        record(cat, "description field present", PASS)

        if len(desc) <= 1024:
            record(cat, "description ≤ 1,024 characters", PASS, f"{len(desc)} chars")
        else:
            record(cat, "description ≤ 1,024 characters", FAIL, f"{len(desc)} chars")

        if re.search(r"<[^>]*>", desc):
            record(
                cat,
                "description has no XML tags",
                FAIL,
                "Contains angle brackets that look like XML tags",
            )
        else:
            record(cat, "description has no XML tags", PASS)

    # ── XML in entire frontmatter ──
    raw_fm_text = ""
    for k, v in fm.items():
        raw_fm_text += f"{k}: {v}\n"
    if re.search(r"<[^>]*>", raw_fm_text):
        record(
            cat,
            "No XML angle brackets in frontmatter",
            FAIL,
            "Found angle brackets in frontmatter values",
        )
    else:
        record(cat, "No XML angle brackets in frontmatter", PASS)

    # ── optional field constraints ──
    if "compatibility" in fm and len(fm["compatibility"]) > 500:
        record(
            cat,
            "compatibility ≤ 500 characters",
            FAIL,
            f"{len(fm['compatibility'])} chars",
        )
    elif "compatibility" in fm:
        record(cat, "compatibility ≤ 500 characters", PASS)

    # ── allowed-tools format ──
    if "allowed-tools" in fm:
        record(cat, "allowed-tools field present", PASS, str(fm["allowed-tools"])[:80])


def check_body(body: str | None, skill_dir: str) -> None:
    """Check the ``SKILL.md`` body size and that bundled subdirectories are mentioned.

    Parameters
    ----------
    body : str | None
        ``SKILL.md`` body text.
    skill_dir : str
        Path to the skill directory.
    """
    cat = "Skill body"

    if not body:
        record(
            cat,
            "Body is non-empty",
            FAIL,
            "SKILL.md has no body content after frontmatter",
        )
        return

    record(cat, "Body is non-empty", PASS)

    lines = body.split("\n")
    line_count = len(lines)
    char_count = len(body)

    if line_count <= 500:
        record(cat, "Body ≤ 500 lines", PASS, f"{line_count} lines")
    else:
        record(
            cat,
            "Body ≤ 500 lines",
            WARN,
            f"{line_count} lines — consider moving content to references/",
        )

    if char_count <= 20000:
        record(
            cat, "Body ≤ ~20,000 characters (~5k tokens)", PASS, f"{char_count} chars"
        )
    else:
        record(
            cat,
            "Body ≤ ~20,000 characters (~5k tokens)",
            WARN,
            f"{char_count} chars — skill may consume excessive context",
        )

    # Check if references/, scripts/, assets/ exist and are mentioned in body
    for subdir in ["references", "scripts", "assets"]:
        subdir_path = os.path.join(skill_dir, subdir)
        if os.path.isdir(subdir_path):
            files_in_subdir = [
                f for f in os.listdir(subdir_path) if not f.startswith(".")
            ]
            if files_in_subdir:
                # Check that SKILL.md mentions this directory
                if subdir in body or any(f in body for f in files_in_subdir):
                    record(cat, f"Body references bundled {subdir}/ files", PASS)
                else:
                    record(
                        cat,
                        f"Body references bundled {subdir}/ files",
                        WARN,
                        f"{subdir}/ exists with {len(files_in_subdir)} file(s) but is not mentioned in SKILL.md body",
                    )


def check_references(skill_dir: str, body: str | None) -> None:
    """Check that each ``references/`` file is linked, has a TOC and avoids cross-references.

    Parameters
    ----------
    skill_dir : str
        Path to the skill directory.
    body : str | None
        ``SKILL.md`` body text that should link the files.
    """
    cat = "References"
    ref_dir = os.path.join(skill_dir, "references")

    if not os.path.isdir(ref_dir):
        record(cat, "references/ directory", SKIP, "No references/ directory")
        return

    ref_files = [f for f in os.listdir(ref_dir) if not f.startswith(".")]
    if not ref_files:
        record(cat, "references/ directory", SKIP, "references/ is empty")
        return

    for ref_file in ref_files:
        ref_path = os.path.join(ref_dir, ref_file)
        if not os.path.isfile(ref_path):
            continue

        # Check that the reference file is mentioned in the SKILL.md body
        if ref_file in (body or ""):
            record(cat, f"'{ref_file}' is linked from SKILL.md", PASS)
        else:
            record(
                cat,
                f"'{ref_file}' is linked from SKILL.md",
                WARN,
                "Reference file exists but is not mentioned in SKILL.md body",
            )

        # Check for TOC if file is long
        try:
            with open(ref_path, "r", encoding="utf-8") as f:
                ref_content = f.read()
            ref_lines = ref_content.split("\n")
            if len(ref_lines) > 100:
                # Look for something resembling a TOC in the first 20 lines
                header = "\n".join(ref_lines[:20]).lower()
                has_toc = any(
                    term in header
                    for term in [
                        "table of contents",
                        "toc",
                        "## contents",
                        "# contents",
                    ]
                )
                if has_toc:
                    record(cat, f"'{ref_file}' has TOC (>{len(ref_lines)} lines)", PASS)
                else:
                    record(
                        cat,
                        f"'{ref_file}' has TOC (>{len(ref_lines)} lines)",
                        WARN,
                        f"File is {len(ref_lines)} lines but has no table of contents in the first 20 lines",
                    )
        except Exception:
            pass

    # Check for cross-references between reference files
    for ref_file in ref_files:
        ref_path = os.path.join(ref_dir, ref_file)
        if not os.path.isfile(ref_path):
            continue
        try:
            with open(ref_path, "r", encoding="utf-8") as f:
                ref_content = f.read()
            other_refs = [r for r in ref_files if r != ref_file]
            cross_refs = [r for r in other_refs if r in ref_content]
            if cross_refs:
                record(
                    cat,
                    f"'{ref_file}' has no cross-references",
                    WARN,
                    f"References other file(s): {', '.join(cross_refs)} — keep references one level deep from SKILL.md",
                )
            else:
                record(cat, f"'{ref_file}' has no cross-references", PASS)
        except Exception:
            pass


def has_shebang(path: str) -> bool:
    """Return whether ``path`` starts with ``#!``, i.e. is meant to be run directly.

    Parameters
    ----------
    path : str
        File to inspect.

    Returns
    -------
    bool
        ``True`` when the first two bytes are ``#!``; ``False`` otherwise,
        including when the file cannot be read.
    """
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"#!"
    except OSError:
        return False


def check_scripts(skill_dir: str) -> None:
    """Check each file under ``scripts/``; only ``#!`` scripts need ``+x``.

    A script with a shebang is meant to be run directly, so it must be
    executable. A module without one is imported by a sibling script and needs
    no executable bit, so no executability check is recorded for it.

    Parameters
    ----------
    skill_dir : str
        Path to the skill directory.
    """
    cat = "Scripts"
    scripts_dir = os.path.join(skill_dir, "scripts")

    if not os.path.isdir(scripts_dir):
        record(cat, "scripts/ directory", SKIP, "No scripts/ directory")
        return

    script_files = sorted(f for f in os.listdir(scripts_dir) if not f.startswith("."))
    if not script_files:
        record(cat, "scripts/ directory", SKIP, "scripts/ is empty")
        return

    for script_file in script_files:
        script_path = os.path.join(scripts_dir, script_file)
        if not os.path.isfile(script_path):
            continue

        record(cat, f"Script found: '{script_file}'", PASS)

        # Only directly-run scripts (shebang) need the executable bit; importable
        # modules are fine without it. There is no exec bit on Windows.
        if os.name == "nt" or not has_shebang(script_path):
            continue
        if os.access(script_path, os.X_OK):
            record(cat, f"'{script_file}' is executable", PASS)
        else:
            record(
                cat,
                f"'{script_file}' is executable",
                WARN,
                "Has a shebang but is not executable — consider chmod +x",
            )


# Words that run a script: `python3 x`, `uv run x`, `bash x`, ... The lookbehind
# keeps `uv run` from matching inside a longer word or path.
_RUN_VERB_RE = re.compile(r"(?<![\w./$-])(uv\s+run|python3?|bash|sh|source)(?=\s)")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# A command position inside a shell snippet: line start (after a `$ ` prompt)
# or right after a control operator.
_COMMAND_START_RE = re.compile(r"(?:^|&&|\|\||[;|])\s*(?:\$\s+)?([^\s&|;]+)")
# The POSIX `. path` spelling of `source path`; only at a command position, since
# a lone "." elsewhere is just the end of a sentence.
_DOT_SOURCE_RE = re.compile(r"(?:^|&&|\|\||[;|])\s*(?:\$\s+)?\.\s+([^\s&|;]+)")
# A `skills/<name>/` path segment (`.agents/skills/<name>/...` and friends).
_SKILLS_SEGMENT_RE = re.compile(r"(?:^|/)skills/([^/]+)/")
_PLACEHOLDER_CHARS = ("<", ">", "$", "{", "}", "*", "?")
_LEADING_QUOTES = "\"'`("
_TRAILING_JUNK = "\"'`),.;:"
# Flags that consume the following word, per runner.
_FLAGS_WITH_VALUE = {
    "uv run": {"--with", "--project", "--directory", "--python", "-p", "--group"},
    "python": {"-W", "-X"},
    "bash": {"-o", "--rcfile", "--init-file"},
}
_SCRIPT_SUFFIXES = (".py", ".sh", ".bash")


def _is_pathlike(token: str) -> bool:
    """Return whether ``token`` looks like a script path rather than a command."""
    return "/" in token or token.endswith(_SCRIPT_SUFFIXES)


def _is_bare_script(token: str) -> bool:
    """Return whether a command word is a script path run without a runner.

    ``scripts/x``, ``./x`` and ``../x`` qualify; any other path only when it
    names a script file and is absolute or has a ``skills/<name>/`` segment, so
    a slash command such as ``/plan-issues`` does not.
    """
    if token.startswith(("scripts/", "./", "../")):
        return True
    if not token.endswith(_SCRIPT_SUFFIXES):
        return False
    return token.startswith("/") or bool(_SKILLS_SEGMENT_RE.search(token))


def _runner_target(verb: str, rest: str) -> str | None:
    """Return the script a runner is told to execute, or ``None``.

    Parameters
    ----------
    verb : str
        The matched run word: ``uv run``, ``python``, ``python3``, ``bash``,
        ``sh`` or ``source``.
    rest : str
        The remainder of the line after ``verb``.

    Returns
    -------
    str | None
        The first path-like operand, with flags skipped. ``None`` when the
        runner is given a module (``-m``), inline code (``-c``) or a plain
        command name such as ``pytest``.
    """
    kind = "uv run" if verb.startswith("uv") else "python"
    if verb.startswith("python"):
        kind = "python"
    elif not verb.startswith("uv"):
        kind = "bash"
    tokens = rest.split()
    skip_value = False
    i = 0
    while i < len(tokens):
        token = tokens[i].lstrip(_LEADING_QUOTES)
        i += 1
        if skip_value:
            skip_value = False
            continue
        if token in ("-m", "-c"):
            return None
        if token in _FLAGS_WITH_VALUE[kind]:
            skip_value = True
            continue
        if token.startswith("-"):
            continue
        token = token.rstrip(_TRAILING_JUNK)
        if kind == "uv run" and token in ("python", "python3"):
            return _runner_target("python", " ".join(tokens[i:]))
        return token if token and _is_pathlike(token) else None
    return None


def _script_invocations(text: str) -> set[str]:
    """Return every script path ``text`` tells the reader to run.

    Two shapes count as an instruction to run something: a run word
    (``python3``, ``python``, ``bash``, ``sh``, ``uv run``, ``source``, or ``.``
    at a command position) followed by a path, anywhere in the text; and a bare
    path-like command (``scripts/x.py --flag``) in a fenced block or as the
    start of an inline code span with arguments.
    A prose pointer such as ``../sibling/SKILL.md`` is neither.

    Parameters
    ----------
    text : str
        Markdown to scan.

    Returns
    -------
    set[str]
        The path operands found, as written (placeholders included; the caller
        filters those).
    """
    found: set[str] = set()
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        for match in _RUN_VERB_RE.finditer(line):
            target = _runner_target(match.group(1), line[match.end() :])
            if target:
                found.add(target)
        snippets = [line] if in_fence else []
        snippets += [m.group(1) for m in _INLINE_CODE_RE.finditer(line)]
        for snippet in snippets:
            for match in _DOT_SOURCE_RE.finditer(snippet):
                token = match.group(1).rstrip(_TRAILING_JUNK)
                if _is_pathlike(token):
                    found.add(token)
            for match in _COMMAND_START_RE.finditer(snippet):
                token = match.group(1).rstrip(_TRAILING_JUNK)
                is_command = in_fence or snippet.strip().startswith(token + " ")
                if is_command and _is_bare_script(token):
                    found.add(token)
    return found


def _lexically_inside(skill_dir: Path, target: str) -> bool:
    """Return whether ``target`` (relative to ``skill_dir``) stays in the skill."""
    if target.startswith("~"):
        return False
    roots = {os.path.abspath(skill_dir), os.path.realpath(skill_dir)}
    resolved = os.path.normpath(os.path.join(skill_dir, target))
    return any(resolved == root or resolved.startswith(root + os.sep) for root in roots)


def _problem_with(skill_dir: Path, target: str) -> str | None:
    """Return why running ``target`` leaves the skill, or ``None`` when it is fine.

    A relative path is read against the skill root, so ``./x`` and ``../x``
    are checked lexically. Any other relative path (``scripts/x``, ``x.py`` or
    a repo-root-relative ``.agents/skills/<name>/scripts/x``) must name a file
    this skill ships. A ``skills/<name>/`` segment must name this skill, and
    the rest of the path is then read against the skill root.

    Parameters
    ----------
    skill_dir : Path
        The skill directory.
    target : str
        The path operand of a run instruction, with placeholders already
        filtered out.

    Returns
    -------
    str | None
        A short reason, or ``None`` when ``target`` is inside the skill.
    """
    own_names = {os.path.basename(os.path.abspath(skill_dir))}
    own_names.add(os.path.basename(os.path.realpath(skill_dir)))
    relative = not (os.path.isabs(target) or target.startswith("~"))
    segment = _SKILLS_SEGMENT_RE.search(target)
    if segment:
        if segment.group(1) not in own_names:
            return f"another skill's folder ('{segment.group(1)}')"
        if relative:
            target = target[segment.end() :]
    if not relative or target.startswith(("./", "../")):
        if not _lexically_inside(skill_dir, target):
            return "resolves outside the skill"
        return None
    if not (skill_dir / target).exists():
        return (
            "not shipped in this skill (a relative path is read against the "
            "skill root; the repository root's scripts/ is not part of a skill)"
        )
    return None


def check_external_script_references(skill_dir: Path, body: str) -> None:
    """Fail when a skill tells the agent to run a script outside its own folder.

    Skills must be self-contained: a helper a skill runs ships in that skill's
    own ``scripts/`` and is invoked as ``python3 <skill-dir>/scripts/<name>.py``.
    Scanned text is the ``SKILL.md`` body plus every ``.md`` file under
    ``references/``. An instruction to run is a run word (``python3``,
    ``python``, ``bash``, ``sh``, ``uv run``, ``source``, or ``.`` at a command
    position) followed by a path, or a bare ``scripts/x`` command. It fails
    when the path resolves outside ``skill_dir``, names a file the skill does
    not ship (relative paths are read against the skill root, so this also
    catches the repository root's ``scripts/``, which is machinery, not part of
    any skill; reach it through ``make``), or has a ``skills/<name>/`` segment
    naming another skill (``.agents/skills/other/scripts/x.py``). Paths
    containing a placeholder (``<skill-dir>/...``, ``${VAR}/...``) or a glob
    are exempt, and prose pointers such as ``../<sibling>/SKILL.md`` are
    allowed.

    Parameters
    ----------
    skill_dir : Path
        The skill directory.
    body : str
        The ``SKILL.md`` body text.
    """
    cat = "External references"
    check = "No cross-skill script invocations"
    sources: dict[str, str] = {"SKILL.md": body}
    ref_dir = skill_dir / "references"
    if ref_dir.is_dir():
        for ref in sorted(ref_dir.rglob("*.md")):
            try:
                sources[ref.relative_to(skill_dir).as_posix()] = ref.read_text(
                    encoding="utf-8"
                )
            except (OSError, UnicodeDecodeError):
                continue

    offenders: list[str] = []
    for source, text in sources.items():
        for target in sorted(_script_invocations(text)):
            if any(c in target for c in _PLACEHOLDER_CHARS):
                continue
            problem = _problem_with(skill_dir, target)
            if problem:
                offenders.append(f"{source}: {target} ({problem})")

    if offenders:
        record(
            cat,
            check,
            FAIL,
            "Run only scripts this skill ships, as python3 <skill-dir>/scripts/<name>.py: "
            + "; ".join(offenders),
        )
    else:
        record(cat, check, PASS)


def check_security(fm: dict[str, str], skill_dir: str) -> None:
    """Scan the skill directory for hardcoded secrets and backslash paths.

    Parameters
    ----------
    fm : dict[str, str]
        Unused; accepted for call-site symmetry.
    skill_dir : str
        Path to the skill directory to walk.
    """
    cat = "Security"

    # Walk all files for hardcoded secrets
    secret_patterns = [
        (
            r"(?:api[_-]?key|apikey)\s*[:=]\s*['\"][A-Za-z0-9+/=_-]{20,}",
            "Possible API key",
        ),
        (
            r"(?:secret|token|password|passwd)\s*[:=]\s*['\"][^\s'\"]{8,}",
            "Possible secret/token",
        ),
        (r"sk-[A-Za-z0-9]{20,}", "Possible OpenAI/Anthropic secret key"),
        (r"ghp_[A-Za-z0-9]{36}", "Possible GitHub personal access token"),
        (r"xox[bpas]-[A-Za-z0-9-]{10,}", "Possible Slack token"),
    ]

    found_secrets = False
    for root, dirs, files in os.walk(skill_dir):
        # skip hidden dirs
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if fname.startswith("."):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                for pattern, label in secret_patterns:
                    if re.search(pattern, content, re.IGNORECASE):
                        rel_path = os.path.relpath(fpath, skill_dir)
                        record(
                            cat, f"No hardcoded secrets in '{rel_path}'", FAIL, label
                        )
                        found_secrets = True
            except Exception:
                pass

    if not found_secrets:
        record(cat, "No hardcoded secrets detected", PASS)

    # Check for backslash paths
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if fname.startswith(".") or not fname.endswith(".md"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                # Look for Windows-style paths like references\file.md or scripts\run.py
                if re.search(r"(?:references|scripts|assets)\\", content):
                    rel_path = os.path.relpath(fpath, skill_dir)
                    record(
                        cat,
                        f"No backslash paths in '{rel_path}'",
                        FAIL,
                        "Use forward slashes in file paths",
                    )
            except Exception:
                pass


# ── main ─────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="validate.py",
        description="Validate a skill directory against the Agent Skills specification.",
    )
    parser.add_argument(
        "target", help="path to a skill directory or to its SKILL.md file"
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        type=Path,
        default=None,
        help='write {"results": [{"level", "check", "message"}]} to this path',
    )
    return parser


def resolve_skill_dir(target: str) -> str | None:
    """Return the skill directory for ``target`` (a directory or its ``SKILL.md``).

    Parameters
    ----------
    target : str
        Path given on the command line.

    Returns
    -------
    str | None
        The absolute skill directory, or ``None`` when ``target`` is neither a
        directory nor a ``SKILL.md`` file.
    """
    if os.path.isfile(target) and os.path.basename(target) == "SKILL.md":
        return os.path.abspath(os.path.dirname(target))
    if os.path.isdir(target):
        return os.path.abspath(target)
    return None


def write_json_report(path: Path) -> None:
    """Write the collected results to ``path`` as ``{"results": [...]}``.

    Each entry has exactly ``level`` (``PASS``/``FAIL``/``WARN``/``SKIP``),
    ``check`` and ``message``.

    Parameters
    ----------
    path : Path
        Destination file.
    """
    report = {
        "results": [
            {"level": r["status"], "check": r["check"], "message": r["detail"]}
            for r in results
        ]
    }
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Validate the skill named on the command line and print the report.

    Parameters
    ----------
    argv : list[str] | None
        Arguments after the program name; defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Exit code: 0 when no check failed, 1 when at least one failed, 2 on a
        usage error.
    """
    args = build_parser().parse_args(argv)
    skill_dir = resolve_skill_dir(args.target)
    if skill_dir is None:
        print(f"Error: '{args.target}' is not a valid skill directory or SKILL.md file")
        return 2

    results.clear()

    # Announce
    print(f"Validating skill: {skill_dir}")
    print(f"{'=' * 60}")

    # Run checks
    fm, body, dir_name = check_file_structure(skill_dir)
    if fm is not None:
        check_frontmatter(fm, body, dir_name)
        check_body(body, skill_dir)
        check_references(skill_dir, body)
        check_external_script_references(Path(skill_dir), body or "")
        check_scripts(skill_dir)
        check_security(fm, skill_dir)
    elif dir_name is not None:
        # SKILL.md not found — still check security on the directory
        check_security({}, skill_dir)

    # ── report ───────────────────────────────────────────────────────
    print()

    fails = [r for r in results if r["status"] == FAIL]
    warns = [r for r in results if r["status"] == WARN]
    passes = [r for r in results if r["status"] == PASS]
    skips = [r for r in results if r["status"] == SKIP]

    current_category = None
    for r in results:
        if r["category"] != current_category:
            current_category = r["category"]
            print(f"\n── {current_category} ──")

        icon = {"PASS": "✓", "FAIL": "✗", "WARN": "⚠", "SKIP": "–"}[r["status"]]
        line = f"  {icon} {r['check']}"
        if r["detail"]:
            line += f"  ({r['detail']})"
        print(line)

    # ── summary ──────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(
        f"Results: {len(passes)} passed, {len(fails)} failed, {len(warns)} warnings, {len(skips)} skipped"
    )

    if fails:
        print("\nFAILURES:")
        for r in fails:
            detail = f" — {r['detail']}" if r["detail"] else ""
            print(f"  ✗ [{r['category']}] {r['check']}{detail}")

    if warns:
        print("\nWARNINGS:")
        for r in warns:
            detail = f" — {r['detail']}" if r["detail"] else ""
            print(f"  ⚠ [{r['category']}] {r['check']}{detail}")

    # ── JSON output for programmatic use (only when asked for) ───────
    if args.json_path is not None:
        write_json_report(args.json_path)
        print(f"\nDetailed results written to: {args.json_path}")

    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
