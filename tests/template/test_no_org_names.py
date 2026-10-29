"""Guard: downstream organization names must not leak into this public template.

The denylist holds only SHA-256 digests, so the names it blocks never appear
in the repository themselves. Every line of every tracked text file (binary
files are skipped) is split into lower-cased alphanumeric tokens (plus adjacent pairs joined by
``-``, ``_`` and nothing), each token is hashed, and a hit fails the test.
The failure message names the file and line, never the token.
"""

import hashlib
import itertools
import re
import subprocess  # nosec B404
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# SHA-256 of the lower-cased organization name, its product names and its
# private repo names (hyphen, underscore and joined forms). Personal logins
# are deliberately absent.
DENYLIST_SHA256: frozenset[str] = frozenset(
    {
        "83436d77007b1cd4db5862bca16996bfa5b3a6191ed805c5e31acdb491ea15ac",
        "cae680cedcbeb712f257bf10bdbd8aa5ed5c42a58103343d5a7ba03d7ff93bb5",
        "5df8895680982760431032bc288794a85f509f2fafc7f1384661b78b1ccc4563",
        "cc4b37a4831ab99c9c8b60d81694616b6f5df218534cef4f24046255862ff50c",
        "2078e7ef7b182dccf95f49f262e1a4533122325d5ea56c475759be5019497158",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokens_of(text: str) -> set[str]:
    """Return the lower-cased tokens of ``text`` plus joined adjacent pairs.

    Tokens are maximal runs of ASCII letters and digits. Each pair of
    neighbouring tokens also contributes three joined forms: with ``-``,
    with ``_`` and with nothing between them.

    Parameters
    ----------
    text : str
        Arbitrary text, typically a single line of a tracked file.

    Returns
    -------
    set[str]
        Single tokens and their adjacent-pair joins.
    """
    words = _TOKEN_RE.findall(text.lower())
    found = set(words)
    for left, right in itertools.pairwise(words):
        found.update((f"{left}-{right}", f"{left}_{right}", f"{left}{right}"))
    return found


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _tracked_files() -> list[Path]:
    result = subprocess.run(  # nosec B603 B607
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / name for name in result.stdout.splitlines() if name]


def _offending_locations(paths: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        if not path.is_file():
            continue  # tracked but deleted in the working tree, or a submodule
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary file: nothing to scan
        for number, line in enumerate(text.splitlines(), start=1):
            if any(_sha256(token) in DENYLIST_SHA256 for token in tokens_of(line)):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    return hits


def test_tokens_of_joins_adjacent_pairs() -> None:
    assert tokens_of("Acme-Widget acme_widget") >= {
        "acme",
        "widget",
        "acme-widget",
        "acme_widget",
        "acmewidget",
    }


def test_tracked_file_list_is_not_empty() -> None:
    files = _tracked_files()
    assert files, "git ls-files returned nothing: the guard would scan no files"
    assert REPO_ROOT / "tests/template/test_no_org_names.py" in files


def test_no_org_name_in_any_tracked_file() -> None:
    files = _tracked_files()
    assert files, "git ls-files returned nothing: the guard would scan no files"
    hits = _offending_locations(files)
    assert not hits, "downstream org name found (see denylist) at: " + ", ".join(hits)
