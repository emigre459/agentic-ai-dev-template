"""Differential lane: whatever the command gate ALLOWS must be inert in a real shell.

For each payload the gate is asked for a verdict. Whenever it answers ALLOW, the
command is also run for real with ``bash -c`` and ``zsh -c`` inside a fresh
``tmp_path`` sandbox, and the WHOLE ``tmp_path`` tree is compared before and after:

- ``PATH`` holds ONLY the shim directory. Every program the allow list names (and
  every wrapper the gate strips) is a shim that appends one line to ``calls.log``,
  so an unshimmed program cannot run silently: it is simply not found.
- ``HOME`` is the sandbox. The sandbox holds protected directories (``.git/hooks``,
  ``.claude``, ``.agents/hooks``, ...) and a decoy ``bin/git`` that writes a file
  if anything reaches it (for example through a reassigned ``PATH``).
- The only change allowed is an append to ``calls.log`` plus the output files the
  gate itself vetted as redirect targets. Any other new, removed, resized,
  rewritten or re-timed path anywhere under ``tmp_path`` (the shim directory
  included) fails the test.

Bypass payloads only ever ``/usr/bin/touch pwned...`` or ``> pwned...`` inside the
sandbox (``touch`` by absolute path, because it is deliberately not on ``PATH``):
no network, nothing outside tmp_path. When the ``CI`` environment variable is set,
a missing bash or zsh is a failure, not a skip.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
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
    "make lint",
    "make tests",
)
# Every program the allow list names, plus the wrappers the gate strips and the
# builtins in use (a builtin shim is never reached; it is there so a shell that
# looked one up on PATH would still only log).
SHIMMED: tuple[str, ...] = ("git", "bunx", "uv", "mkdir", "tail", "grep", "gh")
SHIMMED += ("make", "env", "timeout", "nice", "nohup", "echo", "cd", "command")
SHIMMED += ("builtin", "rg", "ln")
PROTECTED_DIRS = (
    ".git",
    ".claude",
    ".agents",
    ".codex",
    ".cursor",
    ".gemini",
    ".factory",
    ".augment",
    os.path.join(".github", "hooks"),
    ".husky",
    ".githooks",
)
TOUCH = "/usr/bin/touch"

# Commands the gate must keep allowing, so the lane really executes something.
EXPECTED_ALLOW = [
    "PYTHONUNBUFFERED=1 uv run pytest tests/x.py -v 2>&1 | tail -40",
    'git add -A && git commit -m "wip"',
    'git add -A && git commit -m "msg with (parens) and #hash"',
    'echo "HEAD: $(git rev-parse --short HEAD)"',
    "cd /nonexistent-dir && mkdir -p e2e && echo hi",
    'git commit -m "migrate to preset:\\"none\\" and verify"',
    "echo hi >&2",
    "echo 'x' > out.txt",
    "git status --porcelain > s.txt",
    "echo x >> log.txt 2>&1",
    "echo x # just a comment",
    "NO_COLOR=1 echo hi",
    "NO_COLOR=1 uv run pytest -q",
    "CI=1 bunx foo",
    "env -i -u FOO CI=1 bunx foo",
    "env -i NO_COLOR=1 make lint",
    "timeout 30 make tests",
    "make lint && make tests",
    "nice git add x",
    "command git add x",
    "gh pr view 1 \\\n  --json title",
    "git status --short",
    "git add x < /dev/null",
    'echo "$HOME/x" "${HOME}/y"',
]

# Every class from every review round; each ran a hidden command in some shell.
BYPASSES = [
    # Review 3 (syntax allowlist).
    f"git add x \\>&{TOUCH} pwned_c1a",
    f"git add x \\<&{TOUCH} pwned_c1b",
    f"CI=a\\ bunx {TOUCH} pwned_c2a",
    f"CI='a'\\ bunx {TOUCH} pwned_c2b",
    f"env -u\\ bunx {TOUCH} pwned_c2c",
    'env -S"/usr/bin/touch\\_pwned_c3a" git add x',
    "env -P./bin git add x",
    f"x=ab; y='path[$({TOUCH} pwned_c4a)]'; git add $#x[y]",
    f"x=ab; y='path[$({TOUCH} pwned_c4b)]'; git add $+x[y]",
    f"x=ab; y='path[$({TOUCH} pwned_c4c)]'; git add $^x[y]",
    f"x=ab; y='path[$({TOUCH} pwned_c4d)]'; git add $=x[y]",
    f"SECONDS='path[$({TOUCH} pwned_c4e)]'; git add x",
    f"y='path[$({TOUCH} pwned_c4f)]'; COLUMNS=y git add x",
    "path=./bin; git add x",
    "NULLCMD=./evil.sh; >/dev/null",
    "for path in ./bin; do git add x; done",
    f"x='*(e:{TOUCH} pwned_z1:)'; git add $~x",
    (
        "git add x <\\\n<'git add x'\ngit add '\ngit add x\n"
        f"{TOUCH} pwned_h1\ngit add <<'git add x'\n'\ngit add x"
    ),
    # Review 4, class 1: zsh `:F:expr:` modifier evaluates math.
    f"git add $HOME:F:'path[$({TOUCH} pwned_f1)]':h",
    f'git add "$HOME:F:path[\\$({TOUCH} pwned_f2)]:h"',
    f"git add \"$HOME:F:'path[\\$({TOUCH} pwned_f3)]':h\"",
    f"git add $1:F:'path[$({TOUCH} pwned_f4)]':h",
    f"git add $$:F:'path[$({TOUCH} pwned_f5)]':h",
    f"git add \"$#:F:'path[\\$({TOUCH} pwned_f6)]':h\"",
    f"for y in 'path[$({TOUCH} pwned_f7)]'; do git add $HOME:F:y:h; done",
    # Class 2: multi-variable `for`.
    "for f path in a ./bin; do git add y; done",
    "for f PATH in a ./bin; do git add y; done",
    f"for f SECONDS in a 'path[$({TOUCH} pwned_for)]'; do git add y; done",
    # Class 3: zsh clobber redirects.
    "git add x >! .git/hooks/pwned_clob",
    "git add x 2>! .git/hooks/pwned_fd",
    "git add x &>! .git/hooks/pwned_amp",
    "echo hi >>! .git/hooks/pwned_app",
    # Class 4: zsh `=cmd` target truncates the PATH-resolved program.
    "git add x > =git",
    "echo pwned > =git",
    # Class 5: case-insensitive file systems reach protected paths.
    "git add x > .GIT/hooks/pwned_case",
    "git add x > .AGENTS/hooks/policy.json",
    "git add x > .Claude/settings.json",
    # Class 6: zsh `always` (and other groups).
    f"{{ git add x }} always {{ {TOUCH} pwned_always }}",
    f"if git add x {{ {TOUCH} pwned_if }}",
    f"for f in a {{ {TOUCH} pwned_brace }}",
    # Class 7: input redirects (a local device, never the network).
    "git add x < /dev/zero",
    "git add x < ../calls.log",
]


def _sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build the sandbox, the shim directory and the log file."""
    box = tmp_path / "box"
    shims = tmp_path / "shims"
    log = tmp_path / "calls.log"
    (box / "bin").mkdir(parents=True)
    shims.mkdir()
    log.write_text("")
    for d in PROTECTED_DIRS:
        (box / d).mkdir(parents=True, exist_ok=True)
    (box / ".git" / "hooks").mkdir()
    (box / ".agents" / "hooks").mkdir()
    (box / ".agents" / "hooks" / "policy.json").write_text("{}\n")
    (box / ".claude" / "settings.json").write_text("{}\n")
    decoy = box / "bin" / "git"
    decoy.write_text("#!/bin/sh\n: > pwned_decoy_git\n")
    evil = box / "evil.sh"
    evil.write_text("#!/bin/sh\n: > pwned_evil_script\n")
    for path in (decoy, evil):
        path.chmod(0o755)
    for name in SHIMMED:
        shim = shims / name
        # One line per call: the program name, then its arguments.
        shim.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
        shim.chmod(0o755)
    return box, shims, log


Snapshot = dict[str, tuple[str, int, int, str]]


def _snapshot(root: Path) -> Snapshot:
    """Map every path under ``root`` to (kind, size, mtime_ns, content hash).

    A directory's own size and mtime are left out: any entry added to or removed
    from it already shows up as a path of its own.
    """
    out: Snapshot = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            out[os.path.join(dirpath, name)] = ("dir", 0, 0, "")
        for name in filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            digest = ""
            if os.path.isfile(path) and not os.path.islink(path):
                digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            out[path] = ("file", st.st_size, st.st_mtime_ns, digest)
    return out


def _expected_writes(cmd: str, box: Path) -> set[str]:
    """Return the files the gate vetted as output targets (they may change)."""
    parsed = GATE.parse_strict(cmd)
    assert parsed is not None
    found: set[str] = set()
    stack = list(parsed)
    while stack:
        simple = stack.pop()
        stack.extend(simple.substitutions)
        for target in simple.out_targets:
            t = target.strip("'\"")
            if t != "/dev/null":
                found.add(os.path.normpath(os.path.join(box, t)))
    return found


def _execute(
    shell: str, cmd: str, tmp_path: Path, expected: set[str] | None = None
) -> list[str]:
    """Run ``cmd`` for real and return the problems it caused (empty = inert).

    ``expected`` names the files the command may write (the gate's vetted
    output targets); calls.log may only be appended to.
    """
    box, shims, log = tmp_path / "box", tmp_path / "shims", tmp_path / "calls.log"
    before = _snapshot(tmp_path)
    old_log = log.read_text()
    subprocess.run(
        [shell, "-c", cmd],
        cwd=box,
        env={"PATH": str(shims), "HOME": str(box)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
    )
    after = _snapshot(tmp_path)
    allowed = (expected or set()) | {str(log)}
    problems = [
        f"changed {os.path.relpath(path, tmp_path)}"
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path) and path not in allowed
    ]
    new_log = log.read_text()
    if not new_log.startswith(old_log):
        problems.append("calls.log was rewritten, not appended to")
    for line in new_log[len(old_log) :].splitlines():
        if line.split(" ", 1)[0] not in SHIMMED:
            problems.append(f"unexpected log line {line!r}")
    return problems


def _shell_or_skip(shell: str) -> str:
    path = shutil.which(shell)
    if path is None:
        if os.environ.get("CI"):
            pytest.fail(f"{shell} is not on PATH; CI must install it")
        pytest.skip(f"{shell} is not on PATH")
    return path


def _verdict(cmd: str, box: Path) -> str:
    policy = GATE.Policy(allow=ALLOW)
    protected = [str(box / d) for d in PROTECTED_DIRS]
    root = str(box)
    verdict = GATE.shell_verdict(cmd, policy, root, [root], root, protected=protected)
    decision: str = verdict.decision
    return decision


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("cmd", EXPECTED_ALLOW + BYPASSES)
def test_allowed_commands_are_inert(tmp_path: Path, shell: str, cmd: str) -> None:
    exe = _shell_or_skip(shell)
    box, _, _ = _sandbox(tmp_path)
    if _verdict(cmd, box) != GATE.ALLOW:
        return  # nothing was allowed, so nothing to cross-check
    assert _execute(exe, cmd, tmp_path, _expected_writes(cmd, box)) == [], cmd


@pytest.mark.parametrize("cmd", EXPECTED_ALLOW)
def test_expected_commands_are_allowed(tmp_path: Path, cmd: str) -> None:
    box, _, _ = _sandbox(tmp_path)
    assert _verdict(cmd, box) == GATE.ALLOW, cmd


@pytest.mark.parametrize("cmd", BYPASSES)
def test_bypasses_are_not_allowed(tmp_path: Path, cmd: str) -> None:
    box, _, _ = _sandbox(tmp_path)
    assert _verdict(cmd, box) != GATE.ALLOW, cmd


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_detector_catches_a_hidden_write(tmp_path: Path, shell: str) -> None:
    """Self-check: an un-vetted write anywhere under tmp_path is reported."""
    exe = _shell_or_skip(shell)
    _sandbox(tmp_path)
    problems = _execute(exe, "echo x; : > ../shims/git; : > .git/hooks/p", tmp_path)
    assert "changed shims/git" in problems
    assert "changed box/.git/hooks/p" in problems


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_detector_catches_an_unshimmed_program(tmp_path: Path, shell: str) -> None:
    """Self-check: a program off the shim PATH cannot run, and touch by path is seen."""
    exe = _shell_or_skip(shell)
    _sandbox(tmp_path)
    assert _execute(exe, "touch pwned_a", tmp_path) == []
    assert _execute(exe, f"{TOUCH} pwned_b", tmp_path) == ["changed box/pwned_b"]


# --- Policy layers (review 5): withhold rules and banned-pattern forgiveness ------

LAYER_ALLOW = ("git *", "echo *", "rg *")
WITHHOLD = ("git push *", "git commit --no-verify*", "git add -f*")
BANNED = GATE.BannedPattern.from_dict(
    {"pattern": "old_api", "message": "gone", "replacement": "new_api"}
)
# Each would run a withheld command, or a "read-only" search with a runner
# option, if the gate allowed it.
LAYER_BYPASSES = [
    'git pu"$NOPE"sh origin main',
    'git pu"${NOPE}"sh origin main',
    'git "$(echo push)" origin main',
    'git pu"$(git add x)"sh origin main',
    'git commit --no-ve"$X"rify -m x',
    'git add -"$X"f secret.env',
    'rg old_api "--pre"=sh src',
    'rg old_api --"pre=sh" src',
    'rg old_api --p"$X"re=sh src',
    'git log -G old_api --ext"-"diff',
    "git grep old_api '--open-files-in-pager'=sh",
    "rg old_api *",
]
LAYER_EXPECTED_ALLOW = ["git status --short", "rg new_api src", "git add x"]


def _layer_verdict(cmd: str, box: Path) -> str:
    policy = GATE.Policy(allow=LAYER_ALLOW, withhold=WITHHOLD, banned=(BANNED,))
    protected = [str(box / d) for d in PROTECTED_DIRS]
    root = str(box)
    verdict = GATE.shell_verdict(cmd, policy, root, [root], root, protected=protected)
    decision: str = verdict.decision
    return decision


def _layer_violations(log: Path) -> list[str]:
    """Return logged calls a withhold rule or the banned forgiveness should stop."""
    found = []
    for line in log.read_text().splitlines():
        if any(GATE.pattern_matches(w, line, "/") for w in WITHHOLD):
            found.append(f"withheld call ran: {line!r}")
        words = line.split()
        if words and words[0] == "rg" and any(w.startswith("--pre") for w in words):
            found.append(f"runner option reached rg: {line!r}")
        if "--ext-diff" in words or any("--open-files" in w for w in words):
            found.append(f"runner option reached git: {line!r}")
    return found


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("cmd", LAYER_EXPECTED_ALLOW + LAYER_BYPASSES)
def test_policy_layers_hold_in_real_shells(
    tmp_path: Path, shell: str, cmd: str
) -> None:
    exe = _shell_or_skip(shell)
    box, _, log = _sandbox(tmp_path)
    (box / "src").mkdir()
    (box / "--pre=sh").write_text("")  # a glob in the cwd picks this up
    if _layer_verdict(cmd, box) != GATE.ALLOW:
        return
    assert _execute(exe, cmd, tmp_path, _expected_writes(cmd, box)) == [], cmd
    assert _layer_violations(log) == [], cmd


@pytest.mark.parametrize("cmd", LAYER_BYPASSES)
def test_policy_layer_bypasses_are_not_allowed(tmp_path: Path, cmd: str) -> None:
    box, _, _ = _sandbox(tmp_path)
    assert _layer_verdict(cmd, box) != GATE.ALLOW, cmd


@pytest.mark.parametrize("cmd", LAYER_EXPECTED_ALLOW)
def test_policy_layer_expected_commands_are_allowed(tmp_path: Path, cmd: str) -> None:
    box, _, _ = _sandbox(tmp_path)
    assert _layer_verdict(cmd, box) == GATE.ALLOW, cmd


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_layer_detector_sees_a_withheld_call(tmp_path: Path, shell: str) -> None:
    """Self-check: the lane would notice a withheld push if one ran."""
    exe = _shell_or_skip(shell)
    _, _, log = _sandbox(tmp_path)
    _execute(exe, 'git pu"$NOPE"sh origin main; rg x --pre=sh', tmp_path)
    assert len(_layer_violations(log)) == 2
