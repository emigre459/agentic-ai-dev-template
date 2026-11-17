#!/usr/bin/env python3
"""Run a teaching notebook top to bottom and fail on the first cell error.

Usage: ``notebook_check.py <notebook.ipynb> [--timeout SECONDS]``

The notebook is copied into a temporary directory and the copy is executed with
``nbclient``, so outputs never reach the source file: the committed notebook
stays output-free. The kernel starts in the source notebook's folder, where
JupyterLab would start it, so relative paths resolve the same way.

Exit codes: 0 when every cell ran; 1 on a cell error (the cell index and the
error are printed); 2 when ``nbclient`` or ``nbformat`` can't be imported (the
repo's ``notebook`` dependency group isn't installed in this environment);
64 on a usage error, such as a notebook file that doesn't exist.

The script itself is stdlib only; ``nbclient`` is imported lazily. The template
default runs it as ``uv run --group notebook python <this script> <notebook>``.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

EX_CELL_ERROR = 1
EX_NO_NBCLIENT = 2
EX_USAGE = 64


class _Parser(argparse.ArgumentParser):
    """Argument parser whose usage errors exit 64, keeping 2 for missing tooling."""

    def error(self, message: str) -> NoReturn:
        """Print the usage error and exit ``EX_USAGE``.

        Parameters
        ----------
        message : str
            What was wrong with the arguments.
        """
        self.print_usage(sys.stderr)
        self.exit(EX_USAGE, f"{self.prog}: error: {message}\n")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        description="Execute a copy of a notebook and fail on any cell error."
    )
    parser.add_argument("notebook", type=Path, help="path to the .ipynb file")
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="seconds one cell may run before it counts as failed (default 600)",
    )
    return parser


def _hint(notebook: Path) -> str:
    """Return the command that runs this script with the notebook tooling.

    Parameters
    ----------
    notebook : Path
        The notebook the user asked to check.

    Returns
    -------
    str
        A copy-pasteable ``uv run`` command.
    """
    script = Path(__file__).resolve()
    return f"uv run --group notebook python {script} {notebook}"


def run_notebook(notebook: Path, timeout: int) -> int:
    """Execute a temporary copy of ``notebook`` and report the first failing cell.

    Parameters
    ----------
    notebook : Path
        The notebook to check; it is read, never written.
    timeout : int
        Seconds one cell may run.

    Returns
    -------
    int
        ``0`` when every cell ran, ``EX_CELL_ERROR`` on a cell error, or
        ``EX_NO_NBCLIENT`` when the notebook tooling isn't importable.
    """
    try:
        import nbformat
        from nbclient import NotebookClient
        from nbclient.exceptions import CellExecutionError
    except ImportError as exc:
        print(
            f"notebook_check: can't import the notebook tooling ({exc}).\n"
            "Install the repo's `notebook` dependency group, or run:\n"
            f"  {_hint(notebook)}",
            file=sys.stderr,
        )
        return EX_NO_NBCLIENT

    with tempfile.TemporaryDirectory(prefix="notebook-check-") as scratch:
        copy = Path(scratch) / notebook.name
        shutil.copyfile(notebook, copy)
        nb: Any = nbformat.read(copy, as_version=4)
        client = NotebookClient(
            nb,
            timeout=timeout,
            resources={"metadata": {"path": str(notebook.resolve().parent)}},
        )
        with client.setup_kernel():
            for index, cell in enumerate(nb.cells):
                try:
                    client.execute_cell(cell, index)
                except CellExecutionError as exc:
                    print(
                        f"notebook_check: {notebook}: cell {index} failed: "
                        f"{exc.ename}: {exc.evalue}\n\n{exc}",
                        file=sys.stderr,
                    )
                    return EX_CELL_ERROR
    print(f"notebook_check: {notebook}: every cell passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Check the notebook named on the command line.

    Parameters
    ----------
    argv : list[str] | None
        Arguments after the program name; ``None`` reads ``sys.argv``.

    Returns
    -------
    int
        The process exit code (see the module docstring).
    """
    args = _parser().parse_args(argv)
    notebook: Path = args.notebook
    if not notebook.is_file():
        print(f"notebook_check: no such notebook: {notebook}", file=sys.stderr)
        return EX_USAGE
    return run_notebook(notebook, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
