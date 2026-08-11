#!/usr/bin/env python3
"""
flag_sensitive_paths.py — flag changed file paths that fall into
merge-sensitive categories, for the explain-diff-merge-review skill's
"Pre-merge double-check" section.

Usage:
    git diff --name-only $(git merge-base origin/main HEAD)..HEAD | uv run python scripts/flag_sensitive_paths.py
    uv run python scripts/flag_sensitive_paths.py path/one path/two ...

Reads changed file paths from stdin (one per line) if no paths are given as
command-line arguments; otherwise uses the given arguments. Prints one
"category: path" line per match — a path matching multiple categories prints
once per matching category. This is an informational scan, not a gate: it
always exits 0, whether or not anything matched.
"""
import re
import sys

CATEGORIES = [
    ("migrations/schema", [
        r"(^|/)migrations/",
        r"(^|/)alembic/",
        r"(^|/)db/migrate/",
        r"\.sql$",
    ]),
    ("secrets/env", [
        r"(^|/)\.env(\..*)?$",
        r"secret",
        r"credential",
        r"password",
    ]),
    ("auth/security/permissions", [
        r"auth",
        r"security",
        r"permission",
        r"acl",
    ]),
    ("dependency lockfiles", [
        r"\.lock$",
        r"(^|/)package-lock\.json$",
        r"requirements.*\.txt$",
        r"(^|/)go\.sum$",
    ]),
    ("CI/CD & infra", [
        r"(^|/)\.github/workflows/",
        r"(^|/)Dockerfile",
        r"(^|/)docker-compose",
        r"\.tf$",
    ]),
    ("repo settings", [
        r"(^|/)\.github/repo-settings/",
        r"(^|/)CODEOWNERS$",
    ]),
]

_COMPILED = [
    (name, [re.compile(p, re.IGNORECASE) for p in patterns])
    for name, patterns in CATEGORIES
]


def flag(paths):
    """Return a list of (category, path) for every path matching a category's
    patterns, in input order. A path matching multiple categories appears once
    per matching category."""
    matches = []
    for path in paths:
        for category, patterns in _COMPILED:
            if any(p.search(path) for p in patterns):
                matches.append((category, path))
    return matches


def main():
    paths = sys.argv[1:]
    if not paths:
        paths = [line.strip() for line in sys.stdin if line.strip()]

    matches = flag(paths)
    for category, path in matches:
        print(f"{category}: {path}")

    if not matches:
        print("no sensitive paths flagged", file=sys.stderr)


if __name__ == "__main__":
    main()
