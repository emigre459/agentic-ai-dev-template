"""Tests for ``notebook_check.py`` and the starter bundled in write-teaching-notebook.

``nbclient`` and ``nbformat`` are not template dependencies (they live in the
Python stack's opt-in ``notebook`` group), so these tests inject small fakes
through ``sys.modules``: no kernel starts and nothing reaches the network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SKILL = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "write-teaching-notebook"
)
_SCRIPT = _SKILL / "scripts" / "notebook_check.py"
_STARTER = _SKILL / "assets" / "teaching_notebook.ipynb"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("notebook_check", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["notebook_check"] = module
    spec.loader.exec_module(module)
    return module


class _Node(dict[str, Any]):
    """Attribute-access dict, like ``nbformat.NotebookNode``."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


def _to_node(value: Any) -> Any:
    if isinstance(value, dict):
        return _Node({k: _to_node(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_to_node(v) for v in value]
    return value


class _CellExecutionError(Exception):
    def __init__(self, traceback: str, ename: str, evalue: str) -> None:
        super().__init__(traceback)
        self.traceback = traceback
        self.ename = ename
        self.evalue = evalue


class _Recorder:
    """What the fake client saw: constructor kwargs and executed cell indexes."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.executed: list[int] = []
        self.read_paths: list[Path] = []


@pytest.fixture
def fake_nb(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Install fake ``nbformat`` and ``nbclient`` modules; return the recorder.

    A code cell whose source contains ``boom`` raises ``CellExecutionError``;
    every other code cell gets an output, so a write-back would change the file.
    """
    rec = _Recorder()

    def read(fp: Any, as_version: int) -> Any:
        path = Path(fp)
        rec.read_paths.append(path)
        return _to_node(json.loads(path.read_text(encoding="utf-8")))

    class NotebookClient:
        def __init__(self, nb: Any, **kwargs: Any) -> None:
            self.nb = nb
            rec.kwargs = kwargs

        @contextmanager
        def setup_kernel(self, **kwargs: Any) -> Iterator[None]:
            yield

        def execute_cell(self, cell: Any, cell_index: int, **kwargs: Any) -> Any:
            if cell.cell_type != "code":
                return cell
            rec.executed.append(cell_index)
            if "boom" in "".join(cell.source):
                raise _CellExecutionError(
                    "Traceback ...\nValueError: boom", "ValueError", "boom"
                )
            cell.outputs = [_Node(output_type="stream", name="stdout", text="ok\n")]
            return cell

    nbformat = ModuleType("nbformat")
    nbformat.read = read  # type: ignore[attr-defined]
    nbclient = ModuleType("nbclient")
    nbclient.NotebookClient = NotebookClient  # type: ignore[attr-defined]
    exceptions = ModuleType("nbclient.exceptions")
    exceptions.CellExecutionError = _CellExecutionError  # type: ignore[attr-defined]
    nbclient.exceptions = exceptions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nbformat", nbformat)
    monkeypatch.setitem(sys.modules, "nbclient", nbclient)
    monkeypatch.setitem(sys.modules, "nbclient.exceptions", exceptions)
    return rec


def _notebook(tmp_path: Path, *sources: str) -> Path:
    cells: list[dict[str, Any]] = [
        {"cell_type": "markdown", "metadata": {}, "source": ["# Title\n"]},
    ]
    cells += [
        {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": [src],
        }
        for src in sources
    ]
    nb = {"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    path = tmp_path / "nb" / "42_topic.ipynb"
    path.parent.mkdir()
    path.write_text(json.dumps(nb, indent=1) + "\n", encoding="utf-8")
    return path


def test_notebook_check_passes_clean_notebook(
    fake_nb: _Recorder, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _notebook(tmp_path, "x = 1", "y = x + 1")

    assert _load().main([str(path)]) == 0

    assert fake_nb.executed == [1, 2]
    # It ran a copy, never the source file itself.
    assert fake_nb.read_paths and all(p != path for p in fake_nb.read_paths)
    # The kernel starts where JupyterLab would start it: the notebook's folder.
    assert fake_nb.kwargs["resources"] == {"metadata": {"path": str(path.parent)}}
    assert "passed" in capsys.readouterr().out


def test_notebook_check_reports_failing_cell(
    fake_nb: _Recorder, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _notebook(tmp_path, "x = 1", "raise ValueError('boom')", "never = 1")

    assert _load().main([str(path)]) == 1

    assert fake_nb.executed == [1, 2], "stops at the failing cell"
    err = capsys.readouterr().err
    assert "cell 2" in err
    assert "ValueError: boom" in err


def test_notebook_check_never_writes_outputs_back(
    fake_nb: _Recorder, tmp_path: Path
) -> None:
    path = _notebook(tmp_path, "x = 1", "raise ValueError('boom')")
    before = path.read_bytes()

    _load().main([str(path)])

    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_notebook_check_missing_nbclient_exits_2_with_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A None entry in sys.modules makes `import nbclient` raise ImportError.
    monkeypatch.setitem(sys.modules, "nbclient", None)
    path = _notebook(tmp_path, "x = 1")

    assert _load().main([str(path)]) == 2

    err = capsys.readouterr().err
    assert "uv run --group notebook python" in err
    assert "notebook_check.py" in err
    assert str(path) in err


def test_notebook_check_missing_file_is_a_usage_error(
    fake_nb: _Recorder, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _load().main([str(tmp_path / "missing.ipynb")]) == 64
    assert "missing.ipynb" in capsys.readouterr().err
    assert fake_nb.executed == []


# One heading per section of the notebook's structure (spec §3), in order.
_SECTIONS = [
    "## What was asked and what this proves",
    "## Setup",
    "## Before and after",
    "## Stage 1:",
    "## Calls we made",
    "## Self-checks",
    "## What this doesn't show",
]


def test_starter_notebook_is_valid_and_output_free() -> None:
    nb = json.loads(_STARTER.read_text(encoding="utf-8"))

    assert nb["nbformat"] == 4
    assert nb["metadata"]["kernelspec"]["name"] == "python3"
    code = [c for c in nb["cells"] if c["cell_type"] == "code"]
    assert code, "the starter has code cells"
    assert all(c["outputs"] == [] and c["execution_count"] is None for c in code)
    for cell in nb["cells"]:
        assert cell["cell_type"] in {"markdown", "code"}
        assert isinstance(cell["source"], list)
    headings = [
        line.rstrip("\n")
        for cell in nb["cells"]
        if cell["cell_type"] == "markdown"
        for line in cell["source"]
        if line.startswith("## ")
    ]
    found = [
        next((h for h in headings if h.startswith(section)), None)
        for section in _SECTIONS
    ]
    assert None not in found, f"missing sections: {headings}"
    assert [headings.index(h) for h in found if h] == sorted(
        headings.index(h) for h in found if h
    ), "sections in spec order"
    assert len(headings) == len(_SECTIONS), f"one heading per section: {headings}"
