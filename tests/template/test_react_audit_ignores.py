"""Guard the React stack's ``bun audit`` ignore list.

An ignored advisory is a weakened security gate, so each one must be named in a
comment directly above the ``security`` recipe saying why it is ignored and when
to remove the ignore. Today's only entry is GHSA-fc5q-39xc-vjpf: a picomatch
ReDoS with no patched release, reached only through dev tooling (vite, vitest).
"""

import re
from pathlib import Path

MAKEFILE = Path(__file__).resolve().parents[2] / "stacks" / "react" / "Makefile"
_IGNORE = re.compile(r"--ignore=(\S+)")


def _security_block() -> tuple[list[str], str]:
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("security:"))
    comments: list[str] = []
    i = start - 1
    while i >= 0 and (lines[i].startswith("#") or lines[i].startswith(".PHONY")):
        if lines[i].startswith("#"):
            comments.insert(0, lines[i])
        i -= 1
    return comments, lines[start + 1]


def test_picomatch_advisory_is_ignored() -> None:
    _, recipe = _security_block()
    assert "GHSA-fc5q-39xc-vjpf" in _IGNORE.findall(recipe)


def test_every_ignore_is_explained_with_a_removal_condition() -> None:
    comments, recipe = _security_block()
    text = "\n".join(comments)
    for advisory in _IGNORE.findall(recipe):
        assert advisory in text, f"explain why {advisory} is ignored above `security:`"
    if _IGNORE.findall(recipe):
        assert "remove" in text.lower(), "say when to remove the ignore"
