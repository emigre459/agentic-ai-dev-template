"""Tests for payload parsing and edit normalization in ``.agents/hooks/hook.py``."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO / ".agents" / "hooks" / "hook.py"


def _load_hook() -> ModuleType:
    """Import the standalone hook entry by file path (it lives outside any package)."""
    spec = importlib.util.spec_from_file_location("hook", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HOOK = _load_hook()


def _claude(tool: str, tool_input: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        **extra,
    }


def test_py39_syntax() -> None:
    ast.parse(HOOK_PATH.read_text(), feature_version=(3, 9))


# --- Shell commands ------------------------------------------------------------


@pytest.mark.parametrize("tool", ["Bash", "Execute", "bash", "Shell"])
def test_claude_style_shell(tool: str) -> None:
    call = HOOK.parse_payload(_claude(tool, {"command": "ls -la"}, cwd="/r"))
    assert call == HOOK.ToolCall("claude", "shell", "ls -la", (), "/r", ())


def test_cursor_before_shell_execution() -> None:
    call = HOOK.parse_payload(
        {
            "hook_event_name": "beforeShellExecution",
            "command": "ls",
            "cwd": "/r",
            "workspace_roots": ["/r"],
        }
    )
    assert call == HOOK.ToolCall("cursor", "shell", "ls", (), "/r", ("/r",))


def test_cursor_pre_tool_use() -> None:
    call = HOOK.parse_payload(
        {
            "hook_event_name": "preToolUse",
            "tool_name": "Shell",
            "tool_input": {"command": "ls"},
        }
    )
    assert call is not None
    assert (call.fmt, call.kind, call.command) == ("cursor", "shell", "ls")


def test_copilot_string_tool_args() -> None:
    call = HOOK.parse_payload(
        {"toolName": "bash", "toolArgs": '{"command":"ls"}', "cwd": "/r"}
    )
    assert call is not None
    assert (call.fmt, call.kind, call.command, call.cwd) == (
        "copilot",
        "shell",
        "ls",
        "/r",
    )


def test_copilot_object_tool_args() -> None:
    call = HOOK.parse_payload({"toolName": "bash", "toolArgs": {"command": "ls"}})
    assert call is not None
    assert (call.fmt, call.kind, call.command) == ("copilot", "shell", "ls")


def test_copilot_malformed_tool_args_is_none() -> None:
    assert HOOK.parse_payload({"toolName": "bash", "toolArgs": "{not json"}) is None


def test_copilot_edit_is_other() -> None:
    call = HOOK.parse_payload(
        {"toolName": "edit", "toolArgs": '{"path":"a","new_str":"b"}'}
    )
    assert call is not None
    assert (call.fmt, call.kind, call.edits) == ("copilot", "other", ())


def test_gemini_shell() -> None:
    call = HOOK.parse_payload(
        {
            "hook_event_name": "BeforeTool",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "ls"},
        }
    )
    assert call is not None
    assert (call.fmt, call.kind, call.command) == ("gemini", "shell", "ls")


# --- Edits ---------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["Write", "Create"])
def test_write_whole_file(tmp_path: Path, tool: str) -> None:
    target = tmp_path / "f.txt"
    target.write_text("old")
    call = HOOK.parse_payload(
        _claude(tool, {"file_path": str(target), "content": "new"})
    )
    assert call is not None and call.kind == "edit"
    assert call.edits == (HOOK.Edit(str(target), "old", "new"),)


def test_write_new_file_has_empty_before(tmp_path: Path) -> None:
    target = tmp_path / "missing.txt"
    call = HOOK.parse_payload(
        _claude("Write", {"file_path": str(target), "content": "new"})
    )
    assert call is not None
    assert call.edits == (HOOK.Edit(str(target), "", "new"),)


def test_edit_applies_replacement(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a b a")
    call = HOOK.parse_payload(
        _claude(
            "Edit",
            {"file_path": str(target), "old_string": "a", "new_string": "X"},
        )
    )
    assert call is not None
    assert call.edits == (HOOK.Edit(str(target), "a b a", "X b a"),)


def test_edit_replace_all(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a b a")
    call = HOOK.parse_payload(
        _claude(
            "Edit",
            {
                "file_path": str(target),
                "old_string": "a",
                "new_string": "X",
                "replace_all": True,
            },
        )
    )
    assert call is not None
    assert call.edits == (HOOK.Edit(str(target), "a b a", "X b X"),)


def test_edit_missing_file_uses_old_and_new(tmp_path: Path) -> None:
    path = str(tmp_path / "nope.txt")
    call = HOOK.parse_payload(
        _claude("Edit", {"file_path": path, "old_string": "o", "new_string": "n"})
    )
    assert call is not None
    assert call.edits == (HOOK.Edit(path, "o", "n"),)


def test_edit_non_utf8_file_is_unreadable(tmp_path: Path) -> None:
    target = tmp_path / "bin.dat"
    target.write_bytes(b"\xff\xfe\x00")
    call = HOOK.parse_payload(
        _claude(
            "Edit",
            {"file_path": str(target), "old_string": "o", "new_string": "n"},
        )
    )
    assert call is not None
    assert call.edits == (HOOK.Edit(str(target), "o", "n"),)


def test_multi_edit(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("one two")
    call = HOOK.parse_payload(
        _claude(
            "MultiEdit",
            {
                "file_path": str(target),
                "edits": [
                    {"old_string": "one", "new_string": "1"},
                    {"old_string": "two", "new_string": "2"},
                ],
            },
        )
    )
    assert call is not None
    assert call.edits == (HOOK.Edit(str(target), "one two", "1 2"),)


def test_notebook_edit() -> None:
    call = HOOK.parse_payload(
        _claude("NotebookEdit", {"notebook_path": "/r/n.ipynb", "new_source": "x=1"})
    )
    assert call is not None
    assert call.edits == (HOOK.Edit("/r/n.ipynb", "", "x=1"),)


def test_augment_edit_with_old_new_text(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("hello")
    call = HOOK.parse_payload(
        _claude(
            "edit",
            {"path": str(target), "edits": [{"oldText": "hello", "newText": "bye"}]},
        )
    )
    assert call is not None
    assert call.edits == (HOOK.Edit(str(target), "hello", "bye"),)


def test_gemini_write_and_replace(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("abc")
    write = HOOK.parse_payload(
        {
            "hook_event_name": "BeforeTool",
            "tool_name": "write_file",
            "tool_input": {"file_path": str(target), "content": "z"},
        }
    )
    replace = HOOK.parse_payload(
        {
            "hook_event_name": "BeforeTool",
            "tool_name": "replace",
            "tool_input": {
                "file_path": str(target),
                "old_string": "b",
                "new_string": "B",
            },
        }
    )
    assert write is not None and write.fmt == "gemini"
    assert write.edits == (HOOK.Edit(str(target), "abc", "z"),)
    assert replace is not None
    assert replace.edits == (HOOK.Edit(str(target), "abc", "aBc"),)


def test_relative_path_reads_against_payload_cwd(tmp_path: Path) -> None:
    (tmp_path / "rel.txt").write_text("old")
    call = HOOK.parse_payload(
        _claude(
            "Write",
            {"file_path": "rel.txt", "content": "new"},
            cwd=str(tmp_path),
        )
    )
    assert call is not None
    assert call.edits == (HOOK.Edit("rel.txt", "old", "new"),)


def test_unknown_edit_tool_uses_generic_keys() -> None:
    call = HOOK.parse_payload(
        _claude("Frobnicate", {"target_file": "/r/a.py", "contents": "x"})
    )
    assert call is not None and call.kind == "edit"
    assert call.edits == (HOOK.Edit("/r/a.py", "", "x"),)


PATCH = (
    "*** Begin Patch\n*** Add File: a.txt\n+line1\n+line2\n"
    "*** Update File: b.txt\n@@\n context\n-a\n+b\n"
    "*** Delete File: c.txt\n*** End Patch"
)


def test_apply_patch() -> None:
    assert HOOK.parse_apply_patch(PATCH) == (
        HOOK.Edit("a.txt", "", "line1\nline2"),
        HOOK.Edit("b.txt", "a", "b"),
    )


def test_apply_patch_move_uses_destination() -> None:
    patch = "*** Begin Patch\n*** Update File: o.txt\n*** Move to: n.txt\n-a\n+b\n*** End Patch"
    assert HOOK.parse_apply_patch(patch) == (HOOK.Edit("n.txt", "a", "b"),)


def test_apply_patch_garbage_is_empty() -> None:
    assert HOOK.parse_apply_patch("not a patch") == ()


def test_codex_apply_patch_payload() -> None:
    call = HOOK.parse_payload(_claude("apply_patch", {"command": PATCH}))
    assert call is not None and call.kind == "edit"
    assert [e.path for e in call.edits] == ["a.txt", "b.txt"]


@pytest.mark.parametrize("field", ["patch", "command", "input"])
def test_factory_apply_patch_field_names(field: str) -> None:
    call = HOOK.parse_payload(_claude("ApplyPatch", {field: PATCH}))
    assert call is not None and call.kind == "edit"
    assert len(call.edits) == 2


def test_null_byte_path_is_unreadable_not_a_crash() -> None:
    call = HOOK.parse_payload(
        _claude("Write", {"file_path": "a\0b", "content": "x"}, cwd="/r\0")
    )
    assert call is not None
    assert call.edits == (HOOK.Edit("a\0b", "", "x"),)


def test_deeply_nested_tool_args_is_none() -> None:
    args = "[" * 200000
    assert HOOK.parse_payload({"toolName": "bash", "toolArgs": args}) is None


def test_edit_old_string_absent_keeps_new_text_visible(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("hello")
    call = HOOK.parse_payload(
        _claude(
            "Edit",
            {"file_path": str(target), "old_string": "nope", "new_string": "rm -rf"},
        )
    )
    assert call is not None
    assert call.edits == (HOOK.Edit(str(target), "nope", "rm -rf"),)


def test_multi_edit_replace_all_is_per_item(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a a b b")
    call = HOOK.parse_payload(
        _claude(
            "MultiEdit",
            {
                "file_path": str(target),
                "edits": [
                    {"old_string": "a", "new_string": "X", "replace_all": True},
                    {"old_string": "b", "new_string": "Y"},
                ],
            },
        )
    )
    assert call is not None
    assert call.edits == (HOOK.Edit(str(target), "a a b b", "X X Y b"),)


def test_multi_edit_malformed_item_uses_string_fallback(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("one two")
    call = HOOK.parse_payload(
        _claude(
            "MultiEdit",
            {
                "file_path": str(target),
                "edits": [
                    {"old_string": "one", "new_string": "1"},
                    {"old_string": "two"},
                ],
            },
        )
    )
    assert call is not None
    assert call.edits == (HOOK.Edit(str(target), "one", "1"),)


# --- Unrecognized --------------------------------------------------------------


@pytest.mark.parametrize("payload", [{}, {"foo": 1}])
def test_unknown_shape_is_none(payload: dict[str, Any]) -> None:
    assert HOOK.parse_payload(payload) is None


def test_read_tool_is_other() -> None:
    call = HOOK.parse_payload(_claude("Read", {"file_path": "/r/a"}))
    assert call is not None
    assert (call.fmt, call.kind, call.edits) == ("claude", "other", ())
