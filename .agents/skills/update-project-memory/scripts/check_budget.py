"""Report and enforce the AGENTS.md line and character budget.

Stdlib only. Caps are CLI args so a repo can set its own (see the skill's
``.agents/skill-config/update-project-memory.md`` preflight).
"""

import argparse
import sys
from pathlib import Path

DEFAULT_SOFT_LINES = 120
DEFAULT_MAX_LINES = 150
DEFAULT_MAX_CHARS = 9000


def main() -> int:
    """Print the budget status for the given file; return 1 when over a hard cap.

    Returns
    -------
    int
        ``1`` if the file exceeds ``--max-lines`` or ``--max-chars``, else ``0``.
        Past ``--soft-lines`` (but under the hard caps) the status is ``SOFT``
        and the exit code stays ``0``. ``2`` when the file can't be read (a
        one-line error on stderr, not a traceback).
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path)
    ap.add_argument("--soft-lines", type=int, default=DEFAULT_SOFT_LINES)
    ap.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    args = ap.parse_args()
    try:
        text = args.path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"{ap.prog}: cannot read {args.path}: {exc.strerror}", file=sys.stderr)
        return 2
    lines, chars = len(text.splitlines()), len(text)
    over = lines > args.max_lines or chars > args.max_chars
    if over:
        status = "OVER"
    elif lines > args.soft_lines:
        status = "SOFT (relocate or summarize before adding)"
    else:
        status = "ok"
    print(
        f"{args.path}: {lines}/{args.max_lines} lines "
        f"(soft {args.soft_lines}), {chars}/{args.max_chars} chars [{status}]"
    )
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
