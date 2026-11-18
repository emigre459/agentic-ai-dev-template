"""Tests for payload parsing and edit normalization in ``.agents/hooks/hook.py``."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
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


# --- Decision path (subprocess) --------------------------------------------------

HOOKS_DIR = REPO / ".agents" / "hooks"


def _project(
    tmp_path: Path,
    policy: Any = None,
    settings: dict[str, Any] | None = None,
    *,
    raw_policy: str | None = None,
) -> Path:
    """Build a throwaway git project holding a copy of the hook directory."""
    root = tmp_path / "proj"
    shutil.copytree(
        HOOKS_DIR,
        root / ".agents" / "hooks",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    text = raw_policy if raw_policy is not None else json.dumps(policy or {})
    (root / ".agents" / "hooks" / "policy.json").write_text(text)
    if settings is not None:
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.json").write_text(json.dumps(settings))
    (tmp_path / "home").mkdir(exist_ok=True)
    return root


def _run(
    root: Path,
    payload: Any,
    *flags: str,
    claude_code: bool = True,
    entry: str = "hook.py",
    stdin: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(root.parent / "home"),
    }
    if claude_code:
        # What Claude Code sets in every hook command's environment.
        env["CLAUDE_PROJECT_DIR"] = str(root)
        env["CLAUDECODE"] = "1"
    env.update(extra_env or {})
    script = [
        ("python3" if entry.endswith(".py") else "sh"),
        str(root / ".agents" / "hooks" / entry),
    ]
    return subprocess.run(
        [*script, *flags],
        input=stdin if stdin is not None else json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=root,
        env=env,
        check=False,
    )


def _bash(command: str) -> dict[str, Any]:
    return _claude("Bash", {"command": command}, cwd=".")


def _decision(proc: subprocess.CompletedProcess[str]) -> str | None:
    if not proc.stdout.strip():
        return None
    out = json.loads(proc.stdout)
    return out["hookSpecificOutput"]["permissionDecision"]  # type: ignore[no-any-return]


BANNED = {
    "banned": [{"pattern": "old_call", "message": "gone", "replacement": "new_call"}]
}


@pytest.mark.parametrize(
    ("fmt", "keys"),
    [
        ("claude", {"hookSpecificOutput"}),
        ("cursor", {"permission", "user_message", "agent_message"}),
        ("copilot", {"permissionDecision", "permissionDecisionReason"}),
        ("gemini", {"decision", "reason"}),
    ],
)
@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_render_shapes(fmt: str, keys: set[str], decision: str) -> None:
    out = json.loads(HOOK.render(fmt, HOOK.Verdict(decision, "why")))
    assert set(out) == keys
    flat = json.dumps(out)
    assert f'"{decision}"' in flat and '"why"' in flat
    if fmt == "claude":
        inner = out["hookSpecificOutput"]
        assert inner["hookEventName"] == "PreToolUse"
        assert inner["permissionDecision"] == decision
        assert inner["permissionDecisionReason"] == "why"


def test_render_no_opinion_is_none() -> None:
    assert HOOK.render("claude", HOOK.Verdict(HOOK.NO_OPINION)) is None


def test_deny_writes_reason_to_stderr_and_exits_0(tmp_path: Path) -> None:
    root = _project(tmp_path, BANNED)
    proc = _run(root, _bash("old_call --x"))
    assert proc.returncode == 0
    assert _decision(proc) == "deny"
    assert "old_call" in proc.stderr and "new_call" in proc.stderr


def test_allow_listed_command_is_allowed(tmp_path: Path) -> None:
    root = _project(tmp_path, {"allow": ["make *"]})
    assert _decision(_run(root, _bash("make lint"))) == "allow"


def test_deny_only_downgrades_allow(tmp_path: Path) -> None:
    root = _project(tmp_path, {"allow": ["make *"], **BANNED})
    allowed = _run(root, _bash("make lint"), "--deny-only")
    assert (allowed.returncode, allowed.stdout) == (0, "")
    assert _decision(_run(root, _bash("old_call"), "--deny-only")) == "deny"


def test_banned_edit_is_denied_relative_to_payload_cwd(tmp_path: Path) -> None:
    root = _project(tmp_path, BANNED)
    payload = _claude(
        "Write", {"file_path": "src/a.py", "content": "old_call()"}, cwd=str(root)
    )
    assert _decision(_run(root, payload)) == "deny"


def test_personal_claude_allows_only_under_claude_code(tmp_path: Path) -> None:
    root = _project(tmp_path, {}, {})
    rule = {"permissions": {"allow": ["Bash(make x)"]}}
    home_claude = tmp_path / "home" / ".claude"
    home_claude.mkdir()
    (home_claude / "settings.json").write_text(json.dumps(rule))
    assert _decision(_run(root, _bash("make x"))) == "allow"
    assert _decision(_run(root, _bash("make x"), claude_code=False)) is None
    (home_claude / "settings.json").unlink()

    (root / ".claude" / "settings.local.json").write_text(json.dumps(rule))
    assert _decision(_run(root, _bash("make x"))) == "allow"
    assert _decision(_run(root, _bash("make x"), claude_code=False)) is None
    (root / ".claude" / "settings.local.json").unlink()

    (root / ".claude" / "settings.json").write_text(json.dumps(rule))
    assert _decision(_run(root, _bash("make x"))) == "allow"
    assert _decision(_run(root, _bash("make x"), claude_code=False)) == "allow"


def test_claude_ask_deny_withhold(tmp_path: Path) -> None:
    for key in ("ask", "deny"):
        settings = {
            "permissions": {"allow": ["Bash(git *)"], key: ["Bash(git push *)"]}
        }
        root = _project(tmp_path / key, {}, settings)
        assert _decision(_run(root, _bash("git status"))) == "allow"
        assert _decision(_run(root, _bash("git push x"))) is None


def test_additional_directories_are_write_roots(tmp_path: Path) -> None:
    command = "echo x > /opt/shared/y"
    base = {"allow": ["echo *"]}
    plain = _project(tmp_path / "a", base, {})
    assert _decision(_run(plain, _bash(command))) is None
    shared = _project(
        tmp_path / "b",
        base,
        {"permissions": {"additionalDirectories": ["/opt/shared"]}},
    )
    assert _decision(_run(shared, _bash(command))) == "allow"


def test_policy_write_roots_expand_relative_and_home(tmp_path: Path) -> None:
    root = _project(
        tmp_path, {"allow": ["echo *"], "write_roots": ["out", "~/scratch"]}
    )
    assert _decision(_run(root, _bash(f"echo x > {root}/out/y"))) == "allow"
    assert (
        _decision(_run(root, _bash(f"echo x > {tmp_path}/home/scratch/y"))) == "allow"
    )


@pytest.mark.parametrize(
    "stdin",
    ["not json", "{}", "[1]", "[" * 200000, '{"tool_name": 1, "tool_input": 2}'],
)
def test_fail_open_on_bad_stdin(tmp_path: Path, stdin: str) -> None:
    root = _project(tmp_path, {"allow": ["make *"]})
    proc = _run(root, None, stdin=stdin)
    assert (proc.returncode, proc.stdout) == (0, "")


@pytest.mark.parametrize(
    "raw", ["{", '{"allow": "x"}', '{"banned": "x"}', '{"safe_env": {"a": 1}}', "[]"]
)
def test_fail_open_on_unreadable_policy(tmp_path: Path, raw: str) -> None:
    root = _project(tmp_path, raw_policy=raw)
    proc = _run(root, _bash("old_call"))
    assert (proc.returncode, proc.stdout) == (0, "")


def test_invalid_banned_regex_skips_only_its_entry(tmp_path: Path) -> None:
    banned = [
        {"pattern": "("},
        {"pattern": "old_call", "message": "m", "replacement": "r"},
        None,
    ]
    root = _project(tmp_path, {"banned": banned})
    proc = _run(root, _bash("old_call"))
    assert proc.returncode == 0
    assert _decision(proc) == "deny"


def test_safe_env_extends_base(tmp_path: Path) -> None:
    root = _project(tmp_path, {"allow": ["make *"], "safe_env": ["MY_TOGGLE"]})
    assert _decision(_run(root, _bash("MY_TOGGLE=1 make x"))) == "allow"
    assert _decision(_run(root, _bash("OTHER=1 make x"))) is None


def test_gate_sh_missing_hook_fails_open(tmp_path: Path) -> None:
    only = tmp_path / "only"
    only.mkdir()
    shutil.copy(HOOKS_DIR / "gate.sh", only / "gate.sh")
    proc = subprocess.run(
        ["sh", str(only / "gate.sh")],
        input=json.dumps(_bash("make x")),
        capture_output=True,
        text=True,
        check=False,
    )
    assert (proc.returncode, proc.stdout) == (0, "")


def test_gate_sh_passes_flags(tmp_path: Path) -> None:
    root = _project(tmp_path, {"allow": ["make *"]})
    full = _run(root, _bash("make x"), entry="gate.sh")
    assert _decision(full) == "allow"
    flagged = _run(root, _bash("make x"), "--deny-only", entry="gate.sh")
    assert (flagged.returncode, flagged.stdout) == (0, "")


def test_gate_sh_is_executable() -> None:
    assert os.access(HOOKS_DIR / "gate.sh", os.X_OK)


def test_shipped_policy_is_empty() -> None:
    assert json.loads((HOOKS_DIR / "policy.json").read_text()) == {
        "allow": [],
        "safe_env": [],
        "write_roots": [],
        "banned": [],
    }


def _run_from_other_dir(
    tmp_path: Path, root: Path, policy: str
) -> subprocess.CompletedProcess[str]:
    other = tmp_path / "elsewhere"
    other.mkdir()
    shutil.copy(HOOKS_DIR / "hook.py", other / "hook.py")
    shutil.copy(HOOKS_DIR / "command_gate.py", other / "command_gate.py")
    (other / "policy.json").write_text(policy)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PROJECT_DIR": str(root),
    }
    return subprocess.run(
        ["python3", str(other / "hook.py")],
        input=json.dumps(_bash("make x")),
        capture_output=True,
        text=True,
        cwd=other,
        env=env,
        check=False,
    )


@pytest.mark.parametrize("raw", ["{", "null", "[]"])
def test_unreadable_root_policy_does_not_fall_back_to_hook_dir(
    tmp_path: Path, raw: str
) -> None:
    root = _project(tmp_path, raw_policy=raw, settings={})
    proc = _run_from_other_dir(tmp_path, root, '{"allow": ["make *"]}')
    assert (proc.returncode, proc.stdout) == (0, "")


def test_malformed_policy_ignores_tracked_settings_allow(tmp_path: Path) -> None:
    settings = {"permissions": {"allow": ["Bash(make x)"]}}
    root = _project(tmp_path, raw_policy="{", settings=settings)
    assert _decision(_run(root, _bash("make x"))) is None


def test_missing_root_policy_falls_back_to_hook_dir(tmp_path: Path) -> None:
    root = _project(tmp_path, {})
    (root / ".agents" / "hooks" / "policy.json").unlink()
    proc = _run_from_other_dir(tmp_path, root, '{"allow": ["make *"]}')
    assert (
        json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    )


@pytest.mark.parametrize("key", ["deny", "ask"])
@pytest.mark.parametrize("rule", ["Bash", "Bash(*)"])
def test_bare_bash_withholds_everything(tmp_path: Path, key: str, rule: str) -> None:
    root = _project(tmp_path, {"allow": ["make *"]}, {"permissions": {key: [rule]}})
    assert _decision(_run(root, _bash("make x"))) is None


def test_bare_bash_in_allow_is_ignored(tmp_path: Path) -> None:
    root = _project(tmp_path, {}, {"permissions": {"allow": ["Bash"]}})
    assert _decision(_run(root, _bash("make x"))) is None


# --- Final-review regressions ----------------------------------------------------

HEREDOC_SMUGGLE = (
    "git commit -m x <<'git add x'\n"
    "git add '\n"
    "git add x\n"
    "touch /tmp/PWN\n"
    "git add <<'git add x'\n"
    "'\n"
    "git add x"
)


def test_gate_sh_heredoc_smuggle_is_no_opinion(tmp_path: Path) -> None:
    root = _project(tmp_path, {"allow": ["git add *", "git commit *"]})
    proc = _run(root, _bash(HEREDOC_SMUGGLE), entry="gate.sh")
    assert (proc.returncode, proc.stdout) == (0, "")
    assert _decision(_run(root, _bash("git add x"), entry="gate.sh")) == "allow"


CURSOR_ENV = {"CURSOR_PROJECT_DIR": "/somewhere", "CURSOR_VERSION": "1.0.0"}


def _personal_rule(tmp_path: Path, rule: str) -> None:
    home_claude = tmp_path / "home" / ".claude"
    home_claude.mkdir()
    (home_claude / "settings.json").write_text(
        json.dumps({"permissions": {"allow": [rule]}})
    )


def test_cursor_shell_ignores_personal_claude_settings(tmp_path: Path) -> None:
    root = _project(tmp_path, {}, {})
    _personal_rule(tmp_path, "Bash(rm *)")
    payload = {
        "hook_event_name": "preToolUse",
        "tool_name": "Shell",
        "tool_input": {"command": "rm -rf build"},
        "cwd": str(root),
    }
    for env in (CURSOR_ENV, {"CURSOR_PROJECT_DIR": "/x"}, {"CURSOR_VERSION": "1"}):
        proc = _run(
            root,
            payload,
            claude_code=False,
            extra_env={"CLAUDE_PROJECT_DIR": str(root), **env},
        )
        assert (proc.returncode, proc.stdout) == (0, ""), env


def test_claude_payload_under_cursor_ignores_personal_settings(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, {}, {})
    _personal_rule(tmp_path, "Bash(rm *)")
    proc = _run(root, _bash("rm -rf build"), extra_env=CURSOR_ENV)
    assert (proc.returncode, proc.stdout) == (0, "")


def test_personal_settings_need_claudecode_marker(tmp_path: Path) -> None:
    root = _project(tmp_path, {}, {})
    _personal_rule(tmp_path, "Bash(rm *)")
    only_dir = {"CLAUDE_PROJECT_DIR": str(root)}
    proc = _run(root, _bash("rm -rf build"), claude_code=False, extra_env=only_dir)
    assert (proc.returncode, proc.stdout) == (0, "")
    assert _decision(_run(root, _bash("rm -rf build"))) == "allow"


def test_read_file_dev_zero_is_unreadable() -> None:
    if not os.path.exists("/dev/zero"):
        pytest.skip("no /dev/zero")
    assert HOOK._read_file("/dev/zero", "") is None
    call = HOOK.parse_payload(
        _claude("Write", {"file_path": "/dev/zero", "content": "x"})
    )
    assert call is not None
    assert call.edits == (HOOK.Edit("/dev/zero", "", "x"),)


def test_read_file_fifo_is_unreadable(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("no mkfifo")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    # A plain open() of a FIFO with no writer blocks forever; run it in a
    # child so a regression fails on the timeout instead of hanging the suite.
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import hook; "
        "print(hook._read_file(sys.argv[2], ''))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(HOOKS_DIR), str(fifo)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.stdout.strip() == "None"


def test_read_file_over_size_cap_is_unreadable(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_text("a" * (2 * 1024 * 1024 + 1))
    assert HOOK._read_file(str(big), "") is None
    small = tmp_path / "small.txt"
    small.write_text("a" * (2 * 1024 * 1024))
    assert HOOK._read_file(str(small), "") == "a" * (2 * 1024 * 1024)


def test_dangling_root_policy_symlink_does_not_fall_back(tmp_path: Path) -> None:
    root = _project(tmp_path, {})
    policy = root / ".agents" / "hooks" / "policy.json"
    policy.unlink()
    policy.symlink_to(tmp_path / "does-not-exist.json")
    proc = _run_from_other_dir(tmp_path, root, '{"allow": ["make *"]}')
    assert (proc.returncode, proc.stdout) == (0, "")


def test_gate_sh_continuation_heredoc_is_no_opinion(tmp_path: Path) -> None:
    root = _project(tmp_path, {"allow": ["git add *", "git commit *"]})
    payload = _bash(HEREDOC_SMUGGLE.replace("<<", "<\\\n<", 1))
    proc = _run(root, payload, entry="gate.sh")
    assert (proc.returncode, proc.stdout) == (0, "")


# --- Syntax allowlist and protected paths (pre-merge review, 2026-11-17) ---------


@pytest.mark.parametrize(
    "target",
    [
        ".agents/hooks/policy.json",
        ".claude/settings.json",
        ".git/hooks/pre-commit",
        ".github/hooks/command-gate.json",
        ".codex/hooks.json",
        "cfg/settings.json",
        # `git commit` runs hooks configured in these.
        ".husky/pre-commit",
        ".HUSKY/pre-commit",
        ".githooks/pre-commit",
        ".pre-commit-config.yaml",
        ".Pre-Commit-Config.YAML",
    ],
)
def test_protected_paths_are_never_write_targets(tmp_path: Path, target: str) -> None:
    root = _project(tmp_path, {"allow": ["echo *"]}, {})
    (root / "cfg").symlink_to(root / ".claude")
    proc = _run(root, _claude("Bash", {"command": f"echo x > {target}"}, cwd=str(root)))
    assert (proc.returncode, proc.stdout) == (0, "")


def test_unprotected_in_root_write_still_allowed(tmp_path: Path) -> None:
    root = _project(tmp_path, {"allow": ["echo *"]}, {})
    payload = _claude("Bash", {"command": "echo x > out.txt"}, cwd=str(root))
    assert _decision(_run(root, payload)) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "git add x \\>&touch pwned",
        "CI=a\\ bunx touch pwned",
        "env -S'touch pwned' git add x",
        "path=./bin; git add x",
        "NULLCMD=./evil.sh; >/dev/null",
    ],
)
def test_gate_sh_syntax_allowlist_defers(tmp_path: Path, command: str) -> None:
    root = _project(tmp_path, {"allow": ["git add *", "bunx *"]})
    proc = _run(root, _bash(command), entry="gate.sh")
    assert (proc.returncode, proc.stdout) == (0, "")
