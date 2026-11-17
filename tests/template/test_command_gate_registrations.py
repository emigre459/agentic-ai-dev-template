"""Tests for the per-harness registrations of the command gate (``gate.sh``)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, NamedTuple

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE_SUFFIX = ".agents/hooks/gate.sh"


class Registration(NamedTuple):
    """One harness registration file and the values the doc says it must hold."""

    path: str
    event: str
    matcher: str | None
    deny_only: bool


REGISTRATIONS = [
    Registration(
        ".claude/settings.json",
        "PreToolUse",
        "Bash|Write|Edit|MultiEdit|NotebookEdit",
        False,
    ),
    Registration(".codex/hooks.json", "PreToolUse", "Bash|apply_patch", True),
    Registration(".github/hooks/command-gate.json", "preToolUse", None, False),
    Registration(
        ".gemini/settings.json",
        "BeforeTool",
        "run_shell_command|write_file|replace",
        False,
    ),
    # Factory's standalone hooks.json is keyed by event name with no "hooks" wrapper.
    Registration(
        ".factory/hooks.json", "PreToolUse", "Execute|Edit|Create|ApplyPatch", False
    ),
    Registration(".augment/settings.json", "PreToolUse", "bash|edit|write", True),
]
IDS = [r.path for r in REGISTRATIONS]
CLAUDE_ALLOW = [
    "Read",
    "Bash(make help)",
    "Bash(make deps)",
    "Bash(make lint)",
    "Bash(make tests)",
    "Bash(make format)",
    "Bash(make coverage)",
    "Bash(make security)",
    "Bash(make pr_check)",
    "Bash(make init *)",
    "Bash(make init_preflight *)",
    "Bash(make apply_repo_settings_bootstrap *)",
    "Bash(make finalize_repo_settings *)",
    "Bash(uv sync *)",
    "Bash(uv run *)",
    "Bash(bun install)",
    "Bash(bun run *)",
    "Bash(bunx *)",
    "Bash(git add *)",
    "Bash(git commit *)",
    "Bash(gh issue view *)",
    "Bash(gh pr view *)",
    "Bash(gh pr comment * bugbot run*)",
    "Bash(gh pr comment * @coderabbitai review*)",
    "Bash(gh pr comment * @coderabbitai full review*)",
    "Bash(coderabbit usage --agent*)",
    "Bash(coderabbit auth status --agent*)",
]


def _load(reg: Registration) -> dict[str, Any]:
    return json.loads((REPO / reg.path).read_text())


def _groups(reg: Registration) -> list[dict[str, Any]]:
    data = _load(reg)
    events = data if reg.path == ".factory/hooks.json" else data["hooks"]
    return events[reg.event]


def _commands(reg: Registration) -> list[str]:
    field = "bash" if reg.path.startswith(".github/") else "command"
    if field == "bash":
        return [h[field] for h in _groups(reg)]
    return [h[field] for group in _groups(reg) for h in group["hooks"]]


@pytest.mark.parametrize("reg", REGISTRATIONS, ids=IDS)
def test_registration_parses_and_points_at_gate(reg: Registration) -> None:
    commands = _commands(reg)
    assert commands
    for command in commands:
        assert GATE_SUFFIX in command
        assert command.endswith("|| true")
        assert "hook.py" not in command


@pytest.mark.parametrize("reg", REGISTRATIONS, ids=IDS)
def test_registration_matcher(reg: Registration) -> None:
    groups = _groups(reg)
    assert len(groups) == 1
    assert groups[0].get("matcher") == reg.matcher


@pytest.mark.parametrize("reg", REGISTRATIONS, ids=IDS)
def test_deny_only_flags(reg: Registration) -> None:
    for command in _commands(reg):
        assert ("--deny-only" in command) is reg.deny_only


@pytest.mark.parametrize("reg", REGISTRATIONS, ids=IDS)
def test_root_expression(reg: Registration) -> None:
    for command in _commands(reg):
        if reg.path == ".claude/settings.json":
            assert '"$CLAUDE_PROJECT_DIR/' in command
        else:
            assert "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" in command


def test_copilot_registration_shape() -> None:
    data = json.loads((REPO / ".github/hooks/command-gate.json").read_text())
    assert data["version"] == 1
    (hook,) = data["hooks"]["preToolUse"]
    assert hook["type"] == "command"


def test_old_hook_is_gone() -> None:
    assert not (REPO / ".claude/hooks/claude_permission_hook.sh").exists()
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    this_file = Path(__file__).resolve().relative_to(REPO).as_posix()
    for rel in filter(None, tracked):
        if rel.startswith("docs/superpowers/") or rel == this_file:
            continue
        path = REPO / rel
        if not path.is_file():  # deleted in the working tree, not yet committed
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        assert "claude_permission_hook" not in text, rel


def test_claude_settings_keeps_allow_list_and_stop() -> None:
    data = json.loads((REPO / ".claude/settings.json").read_text())
    assert data["permissions"]["allow"] == CLAUDE_ALLOW
    assert data["hooks"]["Stop"] == [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "bash .claude/hooks/run-tests-on-stop.sh",
                }
            ]
        }
    ]


# --- Running the registered commands -------------------------------------------

PAYLOAD = json.dumps(
    {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
)
BANNED_PAYLOAD = json.dumps(
    {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "python -c old_api(1)"},
    }
)


def _run(
    command: str, cwd: Path, stdin: str, project_dir: Path | None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    return subprocess.run(
        ["sh", "-c", command],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("reg", REGISTRATIONS, ids=IDS)
def test_registration_command_fails_open_outside_git(
    reg: Registration, tmp_path: Path
) -> None:
    for command in _commands(reg):
        result = _run(command, tmp_path, PAYLOAD, project_dir=None)
        assert result.returncode == 0
        assert result.stdout == ""


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    shutil.copytree(
        REPO / ".agents" / "hooks",
        root / ".agents" / "hooks",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (root / ".agents" / "hooks" / "policy.json").write_text(
        json.dumps(
            {
                "allow": [],
                "safe_env": [],
                "write_roots": [],
                "banned": [
                    {
                        "pattern": r"old_api\(",
                        "message": "gone",
                        "replacement": "new_api",
                    }
                ],
            }
        )
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


@pytest.mark.parametrize(
    "reg",
    [
        r
        for r in REGISTRATIONS
        if r.path in (".claude/settings.json", ".codex/hooks.json")
    ],
    ids=["claude", "codex"],
)
def test_registration_command_denies_banned_in_repo(
    reg: Registration, tmp_path: Path
) -> None:
    root = _project(tmp_path)
    (command,) = _commands(reg)
    result = _run(command, root, BANNED_PAYLOAD, project_dir=root)
    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
