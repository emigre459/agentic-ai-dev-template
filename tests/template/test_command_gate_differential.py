"""Differential lane: whatever the command gate ALLOWS must be inert in a real shell.

For each payload the gate is asked for a verdict. Whenever it answers ALLOW, the
command is also run for real with ``bash -c`` and ``zsh -c`` (each skipped when
that shell is missing) inside a fresh ``tmp_path`` sandbox:

- the allow-listed programs are shims first on ``PATH`` that only log their name;
- a few never-allowed programs are shims that log a ``FORBIDDEN`` line;
- ``HOME`` is the sandbox, and the sandbox holds a decoy ``bin/git`` and
  ``evil.sh`` that create ``pwned*`` files (with a shell builtin, so they work
  even when the payload replaced ``PATH``) if anything reaches them.

Any ``pwned*`` file, or any log line from a program that is not allow-listed,
means the gate allowed a command that ran something hidden. Bypass payloads only
ever ``touch pwned...`` inside the sandbox: no network, nothing outside tmp_path.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE_PATH = REPO / ".agents" / "hooks" / "command_gate.py"


def _load_gate() -> ModuleType:
    """Import the standalone hook module by file path (it lives outside any package)."""
    spec = importlib.util.spec_from_file_location("command_gate", GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GATE = _load_gate()

ALLOW = (
    "git add *",
    "git commit *",
    "git status *",
    "git rev-parse *",
    "bunx *",
    "echo *",
    "uv run *",
    "mkdir -p *",
    "tail:*",
    "grep:*",
    "gh pr view *",
    "cd:*",
)
# Programs the allow list names; their shims log a plain line.
SHIMMED = ("git", "bunx", "uv", "mkdir", "tail", "grep", "gh")
# Never-allowed programs; reaching one is a failure even without a pwned file.
FORBIDDEN = ("rm", "curl", "wget", "python3", "perl", "nc")
PROTECTED_DIRS = (".git", ".claude", ".agents", ".codex", ".cursor", ".gemini")

# Commands the gate must keep allowing, so the lane really executes something.
EXPECTED_ALLOW = [
    "PYTHONUNBUFFERED=1 uv run pytest tests/x.py -v 2>&1 | tail -40",
    'git add -A && git commit -m "wip"',
    'echo "HEAD: $(git rev-parse --short HEAD)"',
    "cd /nonexistent-dir && mkdir -p e2e && echo hi",
    "for bid in 1 2 3; do echo $bid; uv run pytest -q 2>&1 | tail -2; done",
    'git commit -m "migrate to preset:\\"none\\" and verify"',
    "echo hi >&2",
    "echo 'x' > out.txt",
    "echo x # just a comment",
    "echo $#",
    "NO_COLOR=1 echo hi",
    "echo [ab] *.py",
    'echo "$HOME" $1 $# $? $$ $@ $* $- $0 ${HOME} ${1}',
    "CI=1 bunx foo",
    "env -i -u FOO CI=1 bunx foo",
    "nice git add x",
    "command git add x",
    "for f in a b; do echo $f; done",
    "gh pr view 1 \\\n  --json title",
    "git status --short",
    "echo x <<< 'here string'",
]

# Every class from the pre-merge review; each ran a hidden command before.
BYPASSES = [
    "git add x \\>&touch pwned_c1a",
    "git add x \\<&touch pwned_c1b",
    "CI=a\\ bunx touch pwned_c2a",
    "CI='a'\\ bunx touch pwned_c2b",
    "env -u\\ bunx touch pwned_c2c",
    'env -S"touch\\_pwned_c3a" git add x',
    "env -P./bin git add x",
    "x=ab; y='path[$(touch pwned_c4a)]'; git add $#x[y]",
    "x=ab; y='path[$(touch pwned_c4b)]'; git add $+x[y]",
    "x=ab; y='path[$(touch pwned_c4c)]'; git add $^x[y]",
    "x=ab; y='path[$(touch pwned_c4d)]'; git add $=x[y]",
    "SECONDS='path[$(touch pwned_c4e)]'; git add x",
    "y='path[$(touch pwned_c4f)]'; COLUMNS=y git add x",
    "path=./bin; git add x",
    "NULLCMD=./evil.sh; >/dev/null",
    "for path in ./bin; do git add x; done",
    "x='*(e:touch pwned_z1:)'; git add $~x",
    (
        "git add x <\\\n<'git add x'\ngit add '\ngit add x\ntouch pwned_h1\n"
        "git add <<'git add x'\n'\ngit add x"
    ),
]


def _sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build the sandbox, the shim directory and the log file."""
    box = tmp_path / "box"
    shims = tmp_path / "shims"
    log = tmp_path / "calls.log"
    (box / "bin").mkdir(parents=True)
    shims.mkdir()
    log.write_text("")
    decoy = box / "bin" / "git"
    decoy.write_text("#!/bin/sh\n: > pwned_decoy_git\n")
    evil = box / "evil.sh"
    evil.write_text("#!/bin/sh\n: > pwned_evil_script\n")
    for name in SHIMMED:
        _shim(shims / name, f'echo "{name}" >> "{log}"\n')
    for name in FORBIDDEN:
        _shim(shims / name, f'echo "FORBIDDEN {name}" >> "{log}"\n')
    for path in (decoy, evil):
        path.chmod(0o755)
    return box, shims, log


def _shim(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _verdict(cmd: str, box: Path) -> str:
    policy = GATE.Policy(allow=ALLOW)
    protected = [str(box / d) for d in PROTECTED_DIRS]
    root = str(box)
    verdict = GATE.shell_verdict(cmd, policy, root, [root], root, protected=protected)
    decision: str = verdict.decision
    return decision


def _execute(shell: str, cmd: str, box: Path, shims: Path, log: Path) -> list[str]:
    """Run ``cmd`` for real and return the problems it caused (empty = inert)."""
    env = {"PATH": f"{shims}:/usr/bin:/bin", "HOME": str(box)}
    subprocess.run(
        [shell, "-c", cmd],
        cwd=box,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
    )
    problems = [f"created {p.name}" for p in box.rglob("pwned*")]
    for line in log.read_text().splitlines():
        if line.split(" ", 1)[0] not in SHIMMED:
            problems.append(f"ran {line}")
    return problems


def _shell_or_skip(shell: str) -> str:
    path = shutil.which(shell)
    if path is None:
        pytest.skip(f"{shell} is not on PATH")
    return path


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("cmd", EXPECTED_ALLOW + BYPASSES)
def test_allowed_commands_are_inert(tmp_path: Path, shell: str, cmd: str) -> None:
    exe = _shell_or_skip(shell)
    box, shims, log = _sandbox(tmp_path)
    if _verdict(cmd, box) != GATE.ALLOW:
        return  # nothing was allowed, so nothing to cross-check
    assert _execute(exe, cmd, box, shims, log) == [], cmd


@pytest.mark.parametrize("cmd", EXPECTED_ALLOW)
def test_expected_commands_are_allowed(tmp_path: Path, cmd: str) -> None:
    box, _, _ = _sandbox(tmp_path)
    assert _verdict(cmd, box) == GATE.ALLOW, cmd


@pytest.mark.parametrize("cmd", BYPASSES)
def test_bypasses_are_not_allowed(tmp_path: Path, cmd: str) -> None:
    box, _, _ = _sandbox(tmp_path)
    assert _verdict(cmd, box) != GATE.ALLOW, cmd
