"""Harness-neutral hook entry: payload parsing, policy loading and responses.

Detects which agent harness sent a hook payload from its fields (never from a
harness name), normalizes it into a ``ToolCall`` (a shell command, a set of file
edits, or an uninteresting ``other`` call), decides it with ``command_gate`` and
prints the verdict in that harness's response format. Every failure path is no
opinion: exit 0 with empty stdout. Stdlib only, Python 3.9+.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TextIO

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import command_gate
from command_gate import (
    ALLOW,
    DENY,
    NO_OPINION,
    BannedPattern,
    Policy,
    Verdict,
)

SHELL_TOOLS = frozenset({"Bash", "Shell", "Execute", "bash", "run_shell_command"})
PATCH_FIELDS = ("patch", "command", "input")
PATCH_MARKER = "*** Begin Patch"
PATH_KEYS = ("file_path", "path", "target_file")
CONTENT_KEYS = ("content", "contents", "file_text")
NEW_KEYS = ("new_string", "newText")
OLD_KEYS = ("old_string", "oldText")


@dataclass(frozen=True)
class Edit:
    """One file change: the path plus its text before and after."""

    path: str
    before: str
    after: str


@dataclass(frozen=True)
class ToolCall:
    """A normalized tool call. ``fmt`` names the payload format that arrived."""

    fmt: str  # "claude" | "cursor" | "copilot" | "gemini"
    kind: str  # "shell" | "edit" | "other"
    command: str = ""
    edits: tuple[Edit, ...] = ()
    cwd: str = ""
    workspace_roots: tuple[str, ...] = ()


def _first_str(data: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str):
            return value
    return None


def _read_file(path: str, cwd: str) -> str | None:
    """Return the file's text, or ``None`` when it is unreadable."""
    full = os.path.join(cwd, path) if cwd else path
    try:
        with open(full, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _replacement_edit(
    path: str,
    pairs: list[tuple[str, str, bool]],
    cwd: str,
    *,
    well_formed: bool = True,
) -> Edit:
    """Apply replacements to the current file text.

    Falls back to the joined old/new strings when the file is unreadable, when
    an item was malformed, or when a replacement does not apply (harnesses
    normalize quotes, so a real edit may land where ours misses): the new text
    must stay visible to the policy.
    """
    fallback = Edit(
        path,
        "\n".join(old for old, _, _ in pairs),
        "\n".join(new for _, new, _ in pairs),
    )
    current = _read_file(path, cwd)
    if current is None or not well_formed:
        return fallback
    after = current
    for old, new, replace_all in pairs:
        if not old or old not in after:
            return fallback
        after = after.replace(old, new) if replace_all else after.replace(old, new, 1)
    return Edit(path, current, after)


def parse_apply_patch(text: str) -> tuple[Edit, ...]:
    """Parse an ``apply_patch`` envelope into one ``Edit`` per added/updated file."""
    if PATCH_MARKER not in text:
        return ()
    edits: list[Edit] = []
    path: str | None = None
    before: list[str] = []
    after: list[str] = []

    def flush() -> None:
        if path is not None:
            edits.append(Edit(path, "\n".join(before), "\n".join(after)))

    for line in text.splitlines():
        if line.startswith(("*** Add File: ", "*** Update File: ")):
            flush()
            path = line.split(": ", 1)[1].strip()
            before, after = [], []
        elif line.startswith(("*** Delete File: ", "*** End Patch")):
            flush()
            path = None
            before, after = [], []
        elif line.startswith("*** Move to: "):
            if path is not None:
                path = line.split(": ", 1)[1].strip()
        elif path is None or line.startswith(("***", "@@")):
            continue
        elif line.startswith("+"):
            after.append(line[1:])
        elif line.startswith("-"):
            before.append(line[1:])
    flush()
    return tuple(edits)


def _patch_text(tool_input: Mapping[str, Any]) -> str | None:
    for field in PATCH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and PATCH_MARKER in value:
            return value
    return None


def _edits_from_input(
    tool_input: Mapping[str, Any], cwd: str
) -> tuple[Edit, ...] | None:
    """Normalize an edit-tool input, or ``None`` when it is not a known edit shape."""
    patch = _patch_text(tool_input)
    if patch is not None:
        return parse_apply_patch(patch)
    notebook = tool_input.get("notebook_path")
    source = tool_input.get("new_source")
    if isinstance(notebook, str) and isinstance(source, str):
        return (Edit(notebook, "", source),)
    path = _first_str(tool_input, PATH_KEYS)
    if path is None:
        return None
    replace_all = tool_input.get("replace_all") is True
    items = tool_input.get("edits")
    if isinstance(items, list):
        pairs: list[tuple[str, str, bool]] = []
        well_formed = True
        for item in items:
            old = _first_str(item, OLD_KEYS) if isinstance(item, Mapping) else None
            new = _first_str(item, NEW_KEYS) if isinstance(item, Mapping) else None
            if isinstance(item, Mapping) and old is not None and new is not None:
                flag = item.get("replace_all")
                pairs.append(
                    (old, new, flag if isinstance(flag, bool) else replace_all)
                )
            else:
                well_formed = False
        if not pairs:
            return None
        return (_replacement_edit(path, pairs, cwd, well_formed=well_formed),)
    content = _first_str(tool_input, CONTENT_KEYS)
    if content is not None:
        return (Edit(path, _read_file(path, cwd) or "", content),)
    new = _first_str(tool_input, NEW_KEYS)
    if new is not None:
        old = _first_str(tool_input, OLD_KEYS) or ""
        return (_replacement_edit(path, [(old, new, replace_all)], cwd),)
    return None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _tool_call(
    fmt: str,
    name: str,
    tool_input: Mapping[str, Any],
    cwd: str,
    roots: tuple[str, ...],
    edits_allowed: bool = True,
) -> ToolCall | None:
    if name in SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return None
        return ToolCall(fmt, "shell", command, (), cwd, roots)
    edits = _edits_from_input(tool_input, cwd) if edits_allowed else None
    if edits is None:
        return ToolCall(fmt, "other", "", (), cwd, roots)
    return ToolCall(fmt, "edit", "", edits, cwd, roots)


def parse_payload(payload: Mapping[str, Any]) -> ToolCall | None:
    """Detect the payload format and normalize it; ``None`` if unrecognized."""
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) else ""
    roots = _string_tuple(payload.get("workspace_roots")) or _string_tuple(
        payload.get("workspaceRoots")
    )
    event = payload.get("hook_event_name")
    tool_input = payload.get("tool_input")
    name = payload.get("tool_name")

    if "toolName" in payload:  # Copilot camelCase: shell only
        args = payload.get("toolArgs")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, RecursionError):
                return None
        if not isinstance(args, Mapping) or not isinstance(payload["toolName"], str):
            return None
        return _tool_call(
            "copilot", payload["toolName"], args, cwd, roots, edits_allowed=False
        )
    if event == "BeforeTool":
        fmt = "gemini"
    elif event == "beforeShellExecution":
        command = payload.get("command")
        if not isinstance(command, str):
            return None
        return ToolCall("cursor", "shell", command, (), cwd, roots)
    elif event == "preToolUse":
        fmt = "cursor"
    elif name is not None and tool_input is not None:
        fmt = "claude"
    else:
        return None
    if not isinstance(name, str) or not isinstance(tool_input, Mapping):
        return None
    return _tool_call(fmt, name, tool_input, cwd, roots)


# --- Policy, decisions and responses -------------------------------------------

BASH_RULE = re.compile(r"^Bash\((.+)\)$", re.DOTALL)
DENY_ONLY_FLAG = "--deny-only"


def find_project_root(call: ToolCall, env: Mapping[str, str]) -> str:
    """Return the project root: env var, workspace root, nearest ``.git``, or cwd."""
    from_env = env.get("CLAUDE_PROJECT_DIR")
    if from_env:
        return from_env
    if call.workspace_roots:
        return call.workspace_roots[0]
    cwd = call.cwd or os.getcwd()
    here = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return cwd
        here = parent


def _read_json(path: str) -> Any:
    """Return the parsed JSON file, or ``None`` when it is missing or invalid."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, RecursionError):
        return None


def _str_list(value: Any) -> list[str] | None:
    """Return the strings in a list, or ``None`` when it is not a list."""
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, str)]


def _bash_rules(value: Any) -> list[str]:
    rules = []
    for item in _str_list(value) or []:
        match = BASH_RULE.match(item)
        if match:
            rules.append(match.group(1))
    return rules


def load_policy(
    root: str, hook_dir: str, home: str, claude_code: bool
) -> tuple[Policy, list[str]] | None:
    """Load ``policy.json`` and Claude settings; ``None`` if the policy is unreadable.

    Returns the policy plus extra write roots (``write_roots`` and Claude
    ``additionalDirectories``). Personal Claude settings count only when the
    caller is Claude Code itself.
    """
    data = _read_json(os.path.join(root, ".agents", "hooks", "policy.json"))
    if data is None:
        data = _read_json(os.path.join(hook_dir, "policy.json"))
    if not isinstance(data, dict):
        return None
    sections = {}
    for key in ("allow", "safe_env", "write_roots"):
        sections[key] = _str_list(data.get(key, []))
        if sections[key] is None:
            return None
    raw_banned = data.get("banned", [])
    if not isinstance(raw_banned, list):
        return None
    allow = sections["allow"] or []
    write_roots = sections["write_roots"] or []
    withhold: list[str] = []

    settings = [os.path.join(root, ".claude", "settings.json")]
    if claude_code:
        settings += [
            os.path.join(home, ".claude", "settings.json"),
            os.path.join(home, ".claude", "settings.local.json"),
            os.path.join(root, ".claude", "settings.local.json"),
        ]
    for path in settings:
        config = _read_json(path)
        permissions = config.get("permissions") if isinstance(config, dict) else None
        if not isinstance(permissions, dict):
            continue
        allow += _bash_rules(permissions.get("allow"))
        withhold += _bash_rules(permissions.get("ask"))
        withhold += _bash_rules(permissions.get("deny"))
        write_roots += _str_list(permissions.get("additionalDirectories")) or []

    banned = tuple(
        entry
        for entry in (BannedPattern.from_dict(raw) for raw in raw_banned)
        if entry is not None
    )
    policy = Policy(
        allow=tuple(allow),
        withhold=tuple(withhold),
        safe_env=command_gate.BASE_SAFE_ENV_NAMES
        | frozenset(sections["safe_env"] or []),
        banned=banned,
    )
    return policy, write_roots


def render(fmt: str, verdict: Verdict) -> str | None:
    """Return the harness response JSON for ``verdict``, or ``None`` for no output."""
    if verdict.decision not in (ALLOW, DENY):
        return None
    decision, reason = verdict.decision, verdict.reason
    if fmt == "claude":
        body: dict[str, Any] = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }
    elif fmt == "cursor":
        body = {"permission": decision, "user_message": reason, "agent_message": reason}
    elif fmt == "copilot":
        body = {"permissionDecision": decision, "permissionDecisionReason": reason}
    elif fmt == "gemini":
        body = {"decision": decision, "reason": reason}
    else:
        return None
    return json.dumps(body)


def _write_roots(root: str, extra: Sequence[str], home: str) -> list[str]:
    roots = [root, "/tmp", "/private/tmp", tempfile.gettempdir()]
    for entry in extra:
        expanded = home + entry[1:] if entry == "~" or entry.startswith("~/") else entry
        roots.append(
            expanded if os.path.isabs(expanded) else os.path.join(root, expanded)
        )
    return roots


def _decide(
    call: ToolCall, policy: Policy, roots: list[str], root: str, home: str
) -> Verdict:
    cwd = call.cwd or os.getcwd()
    if call.kind == "shell":
        return command_gate.shell_verdict(call.command, policy, cwd, roots, home)
    if call.kind == "edit":
        for edit in call.edits:
            path = edit.path
            if not os.path.isabs(path):
                path = os.path.join(cwd, path)
            verdict = command_gate.edit_verdict(
                path, edit.before, edit.after, policy, root
            )
            if verdict.decision == DENY:
                return verdict
    return Verdict(NO_OPINION)


def main(argv: Sequence[str], stdin: TextIO, env: Mapping[str, str]) -> int:
    """Run one hook invocation. Always returns 0: no output means no opinion."""
    try:
        payload = json.load(stdin)
    except (ValueError, RecursionError):
        return 0
    if not isinstance(payload, Mapping):
        return 0
    call = parse_payload(payload)
    if call is None or call.kind == "other":
        return 0
    home = env.get("HOME") or os.path.expanduser("~")
    root = find_project_root(call, env)
    hook_dir = os.path.dirname(os.path.abspath(__file__))
    loaded = load_policy(root, hook_dir, home, bool(env.get("CLAUDE_PROJECT_DIR")))
    if loaded is None:
        return 0
    policy, extra = loaded
    verdict = _decide(call, policy, _write_roots(root, extra, home), root, home)
    if verdict.decision == ALLOW and DENY_ONLY_FLAG in argv:
        return 0
    output = render(call.fmt, verdict)
    if output is None:
        return 0
    print(output)
    if verdict.decision == DENY:
        print(verdict.reason, file=sys.stderr)
    return 0


# fmt: off
if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:], sys.stdin, os.environ))
    except Exception:  # noqa: BLE001 -- hook boundary must fail open, never crash or deny; agreed emigre459 #50
        sys.exit(0)
# fmt: on
