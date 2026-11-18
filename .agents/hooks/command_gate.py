r"""Shell-command parser for the harness-neutral command gate.

Pure functions, standard library only, no I/O. The hook entry point feeds a
Bash command string and a policy in; this module answers two questions about
the string:

- ``is_fully_allowed``: does the command match the strict grammar below, does
  EVERY simple command in it match an allow pattern, and does every redirect
  stay inside an allowed root (and outside every protected path)?
- ``simple_commands``: which simple commands would the shell execute (used to
  check them against banned and withhold patterns)?

Both are built on ONE recognizer, ``parse_strict``. It accepts only a small,
closed, positive grammar, so it never has to model a shell feature it does not
know: anything outside the grammar is simply not parsed, and the gate gives no
opinion (the harness prompts). In words, a command may be:

- simple commands joined by ``&&``, ``||``, ``;``, newlines and ``|`` (no lone
  ``&``, no ``|&``), ending in an optional ``;`` or newline and an optional
  ``#`` comment on the last line;
- each simple command: leading ``safe_env`` assignments, then bare ``nohup`` /
  ``command`` / ``builtin`` / ``time`` / ``nice``, ``timeout <duration>`` or
  ``env`` with only ``-i`` / ``-u NAME`` / ``safe_env`` assignments, then a
  plain command word and plain, single-quoted or double-quoted words;
- inside double quotes only ``$NAME``, ``${NAME}``, the escapes ``\"``,
  ``\\``, ``\$``, ``\``` and ``$(...)`` whose body is itself in the grammar;
- the redirects ``>``, ``>>``, ``1>``, ``1>>``, ``2>``, ``2>>``, ``&>``, ``<``,
  ``2>&1``, ``>&2`` and ``1>&2``, each to one plain or quoted word.

Everything else is outside the grammar: loops, conditionals, groups,
subshells, unquoted ``$``, backticks, heredocs and here-strings, process
substitution, ``$'...'``, other backslashes, ``=cmd`` and ``~`` words, wrapper
options and every other redirect form. Backslash-newline continuations are
spliced out first, exactly where the shell splices them.

SAFETY INVARIANT: this is a security boundary, so it defaults to "no". It
answers ``True`` from ``is_fully_allowed`` only when the command was fully
parsed AND every executed simple command (including those inside
substitutions) independently matched an allow pattern AND every redirect target
is acceptable. That is strictly safer than a whole-string prefix match, which
would approve ``uv run pytest && rm -rf /`` because the string starts with
``uv run``.
"""

from __future__ import annotations

import fnmatch
import os
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# The only `NAME=value` assignments (leading a command, or after `env`) that may
# be allowed. They toggle output formatting or CI behaviour and cannot change
# WHICH code an allowed command loads or runs. Any other assignment (PATH, zsh
# `path`, EDITOR, GIT_SSH_COMMAND, LD_PRELOAD, ...) defers. A policy may extend
# this set.
BASE_SAFE_ENV_NAMES = frozenset(
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
# zsh evaluates a value assigned to an integer special as arithmetic, which can
# run a `$(...)` nested in another variable. Never safe, even if a policy's
# safe_env names one. FUNCNEST is bash's (an integer, and a crash lever).
ZSH_INTEGER_SPECIALS = frozenset(
    {"SECONDS", "LINES", "COLUMNS", "HISTSIZE", "SAVEHIST", "TMOUT", "RANDOM"}
    | {"KEYTIMEOUT", "LISTMAX", "DIRSTACKSIZE", "MAILCHECK", "PERIOD", "SHLVL"}
    | {"OPTIND", "UID", "GID", "EUID", "EGID", "LINENO", "PPID", "ERRNO"}
    | {"HISTCMD", "TRY_BLOCK_ERROR", "TRY_BLOCK_INTERRUPT", "ZLE_RPROMPT_INDENT"}
    | {"FUNCNEST"}
)
# A command longer than this gets no allow (the banned check still runs).
MAX_ALLOW_COMMAND_CHARS = 64 * 1024
# Deeper `"$(...)"` nesting than this is outside the grammar (and keeps the
# recursive recognizer far from Python's recursion limit).
MAX_SUBSTITUTION_DEPTH = 16
# Any command that changes directory makes relative redirect targets unverifiable.
DIR_CHANGE = re.compile(r"(?:^|[\s;&|(){}`])(?:cd|pushd|popd)(?=$|[\s;&|)])")
# Placeholder for a `$(...)` body in a simple command's normalized text.
SUBST_PLACEHOLDER = "$S"

# --- The strict grammar ---------------------------------------------------------

_WS_CHARS = (" ", "\t")  # tuple membership: the empty string is never a blank
# Characters of an unquoted word run. No `$`, brackets, braces, parentheses,
# `!`, `^`, `#`, `~`, backslash, redirection or control characters, or quotes.
_PLAIN = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./,@%+=:*?-"
)
# What may follow a word or redirect: a blank, or an operator the list or
# pipeline level reads (a `)` only closes a `"$(...)"` body; at the top level
# the list parser rejects it).
_DELIMITERS = frozenset(" \t\n;|&)")
# Reserved words (bash and zsh) that start a control structure, group or
# function: a command word equal to one of them is outside the grammar.
RESERVED_WORDS = frozenset(
    {"for", "while", "until", "if", "then", "else", "elif", "fi", "do", "done"}
    | {"case", "esac", "select", "function", "coproc", "foreach", "repeat"}
    | {"always", "end", "in", "{", "}", "(", ")", "[[", "]]", "((", "))", "!"}
)
# Wrappers stripped before matching, in these exact shapes only.
_BARE_WRAPPERS = frozenset({"nohup", "command", "builtin", "time", "nice"})
_DURATION = re.compile(r"\d+(?:\.\d+)?[smhd]?\Z")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
# Output redirect operators, longest first; fd duplications are exact tokens.
_OUT_OPS = ("1>>", "2>>", "&>", ">>", "1>", "2>", ">")
_FD_DUPS = ("2>&1", "1>&2", ">&2")
# A redirect operator must not be followed by one of these (zsh `>!`, `>|`,
# `>&file`, `>>>`, `>(...)`, `<>`, `<<`, `<&`, `<(...)`).
_BAD_AFTER_OP = frozenset("!|&>(<")
# Characters after which an unquoted `#` starts a comment in bash and zsh.
_COMMENT_PRECEDERS = frozenset(" \t\n;&|")
# Characters a comment's text may not hold (see `_Parser.comment`).
_COMMENT_FORBIDDEN = frozenset("\n;&|<>(){}$`!\\")


class _Reject(Exception):
    """Raised inside the recognizer when the text leaves the grammar."""


@dataclass(frozen=True)
class SimpleCommand:
    """One simple command the shell would execute, as the gate reads it.

    ``text`` is the command with leading safe assignments, wrappers and
    redirects removed and blanks collapsed; quotes are kept and every
    ``$(...)`` body is replaced by ``$S``. ``out_targets`` and ``in_targets``
    are the redirect targets as written (quotes kept); fd duplications such as
    ``2>&1`` are not targets. ``substitutions`` are the simple commands inside
    this command's ``"$(...)"`` bodies.
    """

    text: str
    out_targets: tuple[str, ...] = ()
    in_targets: tuple[str, ...] = ()
    substitutions: tuple[SimpleCommand, ...] = ()


def splice_continuations(cmd: str) -> str | None:
    r"""Remove backslash-newline pairs where bash and zsh remove them.

    The shell deletes a backslash-newline everywhere except inside single
    quotes and comments, before it tokenizes (``<\⏎<`` is ``<<``). This pass
    tracks single quotes, double quotes, ``"$(...)"`` bodies and comments to
    splice exactly those pairs. It returns ``None`` for text whose quoting it
    cannot follow and that the grammar rejects anyway: an unquoted ``$`` or
    backslash escape, a backtick, a parenthesis outside a ``$(...)`` body, a
    comment inside one, or an unterminated quote.

    Parameters
    ----------
    cmd : str
        The raw command string.

    Returns
    -------
    str | None
        The spliced command, or ``None`` when it is outside the grammar.
    """
    out: list[str] = []
    stack = ["top"]  # "top" / "sub" (unquoted) or "dq"
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        ctx = stack[-1]
        if c == "\\":
            nxt = cmd[i + 1 : i + 2]
            if nxt == "\n":
                i += 2
                continue
            if ctx != "dq" or not nxt:
                return None  # an unquoted escape is outside the grammar
            out.append(c + nxt)
            i += 2
            continue
        if c == "`":
            return None
        if ctx == "dq":
            if c == '"':
                stack.pop()
            elif c == "$":
                j = i + 1
                while cmd.startswith("\\\n", j):
                    j += 2
                if cmd[j : j + 1] == "(":
                    if len(stack) > 2 * MAX_SUBSTITUTION_DEPTH:
                        return None
                    stack.append("sub")
                    out.append("$(")
                    i = j + 1
                    continue
            out.append(c)
            i += 1
            continue
        # Unquoted, at the top level or inside a "$(...)" body.
        if c == "'":
            end = cmd.find("'", i + 1)
            if end == -1:
                return None
            out.append(cmd[i : end + 1])  # no splicing inside single quotes
            i = end + 1
            continue
        if c == '"':
            stack.append("dq")
        elif c == "$":
            return None  # unquoted `$` is outside the grammar
        elif c == "(":
            return None
        elif c == ")":
            if ctx != "sub":
                return None
            stack.pop()
        elif c == "#":
            prev = out[-1][-1] if out else ""
            if not prev or prev in _COMMENT_PRECEDERS:
                if ctx != "top":
                    return None  # no comments inside a substitution
                end = cmd.find("\n", i)
                end = n if end == -1 else end
                out.append(cmd[i:end])  # a comment is not continued
                i = end
                continue
        out.append(c)
        i += 1
    if stack != ["top"]:
        return None
    return "".join(out)


class _Parser:
    """Recursive-descent recognizer for the strict grammar (see module doc)."""

    def __init__(self, text: str, names: frozenset[str]) -> None:
        self.s = text
        self.i = 0
        self.names = names
        self.depth = 0

    # -- low-level helpers -------------------------------------------------------

    def peek(self, k: int = 0) -> str:
        return self.s[self.i + k : self.i + k + 1]

    def blanks(self) -> int:
        start = self.i
        while self.peek() in _WS_CHARS:
            self.i += 1
        return self.i - start

    def need_blanks(self) -> None:
        if not self.blanks():
            raise _Reject

    def need_delimiter(self) -> None:
        c = self.peek()
        if c and c not in _DELIMITERS:
            raise _Reject

    def plain_run(self) -> str:
        start = self.i
        while self.peek() and self.peek() in _PLAIN:
            self.i += 1
        return self.s[start : self.i]

    def at_list_end(self, nested: bool) -> bool:
        return self.peek() == ")" if nested else self.i == len(self.s)

    # -- list / pipeline ---------------------------------------------------------

    def line(self, nested: bool) -> list[SimpleCommand]:
        cmds: list[SimpleCommand] = []
        self.blanks()
        if not nested and self.peek() == "#":
            self.comment()
            return cmds
        cmds += self.pipeline()
        while True:
            self.blanks()
            op = self.separator()
            if op is None:
                break
            self.blanks()
            if op in (";", "\n") and (
                self.at_list_end(nested) or (not nested and self.peek() == "#")
            ):
                break  # a trailing `;` or newline
            cmds += self.pipeline()
        self.blanks()
        if not nested and self.peek() == "#":
            self.comment()
        if not self.at_list_end(nested):
            raise _Reject
        return cmds

    def comment(self) -> None:
        text = self.s[self.i + 1 :]
        # A comment may only end the last line. Its text may not hold anything
        # that would be live syntax if a shell read the `#` literally (zsh
        # without INTERACTIVE_COMMENTS in an interactive shell).
        if any(ch in _COMMENT_FORBIDDEN for ch in text):
            raise _Reject
        self.i = len(self.s)

    def separator(self) -> str | None:
        two = self.s[self.i : self.i + 2]
        if two in ("&&", "||"):
            self.i += 2
            return two
        c = self.peek()
        if c == ";":
            if self.peek(1) in (";", "&", "|"):
                raise _Reject  # case terminators
            self.i += 1
            return c
        if c == "\n":
            self.i += 1
            return c
        if c == "&":
            raise _Reject  # a lone `&` (background) is outside the grammar
        return None

    def pipeline(self) -> list[SimpleCommand]:
        cmds = [self.simple()]
        while True:
            mark = self.i
            self.blanks()
            if self.peek() == "|" and self.peek(1) != "|":
                if self.peek(1) == "&":
                    raise _Reject  # `|&`
                self.i += 1
                self.blanks()
                cmds.append(self.simple())
            else:
                self.i = mark
                return cmds

    # -- simple command ------------------------------------------------------------

    def assignment(self, subs: list[SimpleCommand]) -> bool:
        m = _ASSIGN.match(self.s, self.i)
        if not m:
            return False
        if m.group(1) not in self.names:
            raise _Reject
        self.i = m.end()
        c = self.peek()
        if c == "'":
            self.squoted()
        elif c == '"':
            self.dquoted(subs, allow_expansion=True)
        elif not c or c in "=~" or not self.plain_run():
            raise _Reject
        self.need_blanks()
        return True

    def wrapper(self, subs: list[SimpleCommand]) -> bool:
        m = _NAME.match(self.s, self.i)
        if m is None or self.s[m.end() : m.end() + 1] not in _WS_CHARS:
            return False
        word, end = m.group(0), m.end()
        if word in _BARE_WRAPPERS:
            self.i = end
            self.need_blanks()
            return True
        if word == "timeout":
            self.i = end
            self.need_blanks()
            if not _DURATION.match(self.plain_run()):
                raise _Reject
            self.need_blanks()
            return True
        if word != "env":
            return False
        self.i = end
        self.need_blanks()
        while True:
            if self.s.startswith("-i", self.i) and self.peek(2) in _WS_CHARS:
                self.i += 2
                self.need_blanks()
            elif self.s.startswith("-u", self.i) and self.peek(2) in _WS_CHARS:
                self.i += 2
                self.need_blanks()
                name = self.plain_run()
                if not _NAME.fullmatch(name):
                    raise _Reject
                self.need_blanks()
            elif not self.assignment(subs):
                return True

    def command_word(self) -> str:
        word = self.plain_run()
        if (
            not word
            or word[0] in "=~-"
            or word in RESERVED_WORDS
            or any(ch in word for ch in "=*?")
        ):
            raise _Reject
        self.need_delimiter()
        return word

    def simple(self) -> SimpleCommand:
        subs: list[SimpleCommand] = []
        while self.assignment(subs):
            pass
        while self.wrapper(subs):
            pass
        words = [self.command_word()]
        outs: list[str] = []
        ins: list[str] = []
        while True:
            mark = self.i
            if not self.blanks():
                break
            c = self.peek()
            if not c or c in "\n;|)#" or (c == "&" and self.peek(1) != ">"):
                self.i = mark
                break
            if not self.redirect(outs, ins):
                words.append(self.word(subs))
            self.need_delimiter()
        return SimpleCommand(" ".join(words), tuple(outs), tuple(ins), tuple(subs))

    # -- words ---------------------------------------------------------------------

    def word(self, subs: list[SimpleCommand]) -> str:
        if self.peek() in ("=", "~", ""):
            raise _Reject  # zsh `=cmd`, tilde expansion
        parts: list[str] = []
        while True:
            c = self.peek()
            if c == "'":
                parts.append(self.squoted())
            elif c == '"':
                parts.append(self.dquoted(subs, allow_expansion=True))
            elif c and c in _PLAIN:
                parts.append(self.plain_run())
            else:
                break
        if not parts:
            raise _Reject
        return "".join(parts)

    def squoted(self) -> str:
        end = self.s.find("'", self.i + 1)
        if end == -1:
            raise _Reject
        text = self.s[self.i : end + 1]
        self.i = end + 1
        return text

    def dquoted(self, subs: list[SimpleCommand], allow_expansion: bool) -> str:
        self.i += 1
        buf = ['"']
        while True:
            c = self.peek()
            if not c or c in "`!":
                raise _Reject
            if c == '"':
                self.i += 1
                buf.append(c)
                return "".join(buf)
            if c == "\\":
                if not allow_expansion or self.peek(1) not in ('"', "\\", "$", "`"):
                    raise _Reject
                buf.append(self.s[self.i : self.i + 2])
                self.i += 2
                continue
            if c == "$":
                if not allow_expansion:
                    raise _Reject
                buf.append(self.dollar_in_dq(subs))
                continue
            buf.append(c)
            self.i += 1

    def dollar_in_dq(self, subs: list[SimpleCommand]) -> str:
        nxt = self.peek(1)
        if nxt == "(":
            if self.depth >= MAX_SUBSTITUTION_DEPTH:
                raise _Reject
            self.i += 2
            self.depth += 1
            inner = self.line(nested=True)
            self.depth -= 1
            if not inner:
                raise _Reject
            self.i += 1  # the closing `)` (line() stopped on it)
            if self.peek() == "[":
                raise _Reject
            subs.extend(inner)
            return SUBST_PLACEHOLDER
        if nxt == "{":
            m = _NAME.match(self.s, self.i + 2)
            if not m or self.s[m.end() : m.end() + 1] != "}":
                raise _Reject
            end = m.end() + 1
            if self.s[end : end + 1] in ("[", ":"):
                raise _Reject
        else:
            m = _NAME.match(self.s, self.i + 1)
            if not m:
                raise _Reject
            end = m.end()
            if self.s[end : end + 1] in ("[", ":", "(", "{"):
                raise _Reject
        text = self.s[self.i : end]
        self.i = end
        return text

    # -- redirects -------------------------------------------------------------------

    def redirect(self, outs: list[str], ins: list[str]) -> bool:
        for dup in _FD_DUPS:
            if self.s.startswith(dup, self.i):
                after = self.s[self.i + len(dup) : self.i + len(dup) + 1]
                if not after or after in _DELIMITERS:
                    self.i += len(dup)
                    return True
        if self.peek() == "<":
            op, dest = "<", ins
        else:
            op = next((o for o in _OUT_OPS if self.s.startswith(o, self.i)), "")
            dest = outs
            if not op:
                return False
        self.i += len(op)
        if self.peek() in _BAD_AFTER_OP and self.peek():
            raise _Reject
        self.blanks()
        dest.append(self.target())
        return True

    def target(self) -> str:
        c = self.peek()
        if c == "'":
            text = self.squoted()
        elif c == '"':
            text = self.dquoted([], allow_expansion=False)
        else:
            text = self.plain_run()
            if not text or text[0] in "=~" or any(ch in text for ch in "*?"):
                raise _Reject  # zsh `=cmd`, globs
        if not text.isascii():
            raise _Reject
        return text


def parse_strict(
    cmd: str, safe_env: frozenset[str] = BASE_SAFE_ENV_NAMES
) -> list[SimpleCommand] | None:
    """Parse ``cmd`` under the strict grammar, or return ``None``.

    Parameters
    ----------
    cmd : str
        The full shell command string.
    safe_env : frozenset[str]
        Variable names that may be assigned (the zsh integer specials never
        count, whatever this holds).

    Returns
    -------
    list[SimpleCommand] | None
        The top-level simple commands in order (each carries its own
        substitutions), an empty list for a comment-only line, or ``None`` when
        the command is outside the grammar or longer than
        ``MAX_ALLOW_COMMAND_CHARS``.
    """
    if len(cmd) > MAX_ALLOW_COMMAND_CHARS:
        return None
    if any((ch < " " and ch not in "\t\n") or ch == "\x7f" for ch in cmd):
        return None  # NUL, CR, ESC, ...: argv truncation and terminal tricks
    text = splice_continuations(cmd)
    if text is None:
        return None
    parser = _Parser(text, frozenset(safe_env) - ZSH_INTEGER_SPECIALS)
    try:
        return parser.line(nested=False)
    except _Reject:
        return None


def iter_commands(cmds: Sequence[SimpleCommand]) -> Iterator[SimpleCommand]:
    """Yield every simple command, then the ones inside its substitutions."""
    for cmd in cmds:
        yield cmd
        yield from iter_commands(cmd.substitutions)


# --- Redirect targets --------------------------------------------------------------


def _fold(path: str) -> str:
    """Case- and compatibility-fold a path for the protected-path comparison."""
    folded = unicodedata.normalize("NFKC", path).casefold()
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


def _resolve_target(target: str, cwd: str, dir_changed: bool) -> tuple[str, str] | None:
    """Return the (joined, real) path of a target, or ``None`` if unverifiable."""
    t = target.strip()
    if len(t) >= 2 and (t[:1], t[-1:]) in (('"', '"'), ("'", "'")):
        t = t[1:-1]
    if not t or any(ch in t for ch in "$*?[{`'\"\\~"):
        return None  # unresolved / glob / partial quoting / tilde -> defer
    if not t.startswith("/") and dir_changed:
        return None
    joined = os.path.normpath(t if t.startswith("/") else os.path.join(cwd, t))
    # realpath, not normpath: an in-root symlink that points outside every root
    # (`link -> /etc`) must not count as an in-root write.
    return joined, os.path.realpath(joined)


def _under_dev(joined: str, real: str) -> bool:
    return any(_within(p, "/dev") for p in (joined, real))


def redirect_target_ok(
    target: str,
    roots: Sequence[str],
    cwd: str,
    home: str,
    dir_changed: bool = False,
    protected: Sequence[str] = (),
) -> bool:
    """Return True iff an output redirect target may be written.

    ``/dev/null`` is always fine. Otherwise the target must resolve (after
    ``realpath``) inside an allowed root, compared exactly, and outside every
    ``protected`` directory, compared case-insensitively (APFS and other
    case-insensitive file systems reach ``.git`` through ``.GIT``). Anything
    else under ``/dev`` (``/dev/tcp/...``, ``/dev/fd/N``) is never fine.

    Parameters
    ----------
    target : str
        The redirect target as written, possibly quoted.
    roots : Sequence[str]
        Allowed root directories for writes.
    cwd : str
        Directory relative targets resolve against.
    home : str
        Home directory (unused: a ``~`` target is outside the grammar).
    dir_changed : bool
        True when the command runs ``cd``/``pushd``/``popd`` somewhere, so a
        relative target no longer resolves against ``cwd`` and defers.
    protected : Sequence[str]
        Directories whose subtrees are never write targets.

    Returns
    -------
    bool
        True when the target may be written.
    """
    del home
    if target.strip().strip("'\"") == "/dev/null":
        return True
    resolved = _resolve_target(target, cwd, dir_changed)
    if resolved is None:
        return False
    joined, real = resolved
    if _under_dev(joined, real):
        return False
    folded = _fold(real)
    if any(_within(folded, _fold(os.path.realpath(d))) for d in protected):
        return False
    return any(_within(real, os.path.realpath(r)) for r in roots)


def input_target_ok(
    target: str,
    roots: Sequence[str],
    cwd: str,
    home: str,
    dir_changed: bool = False,
) -> bool:
    """Return True iff an input redirect (``<``) target may be read.

    It must be ``/dev/null`` or resolve inside an allowed root; any other
    ``/dev`` path (``/dev/tcp/...``, ``/dev/zero``) defers.

    Parameters
    ----------
    target : str
        The redirect target as written, possibly quoted.
    roots : Sequence[str]
        Allowed root directories.
    cwd : str
        Directory relative targets resolve against.
    home : str
        Home directory (unused: a ``~`` target is outside the grammar).
    dir_changed : bool
        True when the command changes directory (relative targets defer).

    Returns
    -------
    bool
        True when the target may be read.
    """
    del home
    if target.strip().strip("'\"") == "/dev/null":
        return True
    resolved = _resolve_target(target, cwd, dir_changed)
    if resolved is None:
        return False
    joined, real = resolved
    if _under_dev(joined, real):
        return False
    return any(_within(real, os.path.realpath(r)) for r in roots)


def _within(path: str, root: str) -> bool:
    """Return True when ``path`` is ``root`` or lies under it."""
    return path == root or path.startswith(root.rstrip("/") + "/")


# --- Allow-pattern matching -------------------------------------------------------


def _glob_match(core: str, command: str, prefix: bool) -> bool:
    """Match ``command`` against ``core`` where every ``*`` is a wildcard.

    Parameters
    ----------
    core : str
        Pattern text; each ``*`` matches any run of characters.
    command : str
        The command string to test.
    prefix : bool
        When True the command only has to start with the pattern.

    Returns
    -------
    bool
        True when the command matches.
    """
    pat = core + "*" if prefix else core
    p = t = 0
    star = mark = -1
    while t < len(command):
        if p < len(pat) and pat[p] == "*":
            star, mark = p, t
            p += 1
        elif p < len(pat) and pat[p] == command[t]:
            p += 1
            t += 1
        elif star != -1:
            mark += 1
            p, t = star + 1, mark
        else:
            return False
    while p < len(pat) and pat[p] == "*":
        p += 1
    return p == len(pat)


def pattern_matches(pattern: str, command: str, home: str) -> bool:
    """Match a command string against one ``Bash(...)`` allow pattern.

    A trailing ``*`` (or ``:*``) is a prefix match; a ``*`` anywhere else
    matches any run of characters. Callers split compound commands first, so a
    ``*`` never spans an operator.

    Parameters
    ----------
    pattern : str
        The text inside one ``Bash(...)`` allow entry.
    command : str
        The command string to test.
    home : str
        Home directory that a leading ``~/`` in the pattern expands to.

    Returns
    -------
    bool
        True when the command matches the pattern.
    """
    pattern = pattern.strip()
    if pattern.startswith("~/"):
        pattern = home + pattern[1:]
    if not pattern:
        return False
    if pattern.endswith("*"):
        core = pattern[:-1]
        if _glob_match(core, command, prefix=True):
            return True
        trimmed = core.rstrip()
        if trimmed.endswith(":"):
            base = trimmed[:-1].rstrip()
            return _glob_match(base, command, prefix=False) or _glob_match(
                base + " ", command, prefix=True
            )
        return _glob_match(trimmed, command, prefix=False)
    return _glob_match(pattern, command, prefix=False)


def is_fully_allowed(
    cmd: str,
    patterns: Sequence[str],
    roots: Sequence[str],
    cwd: str,
    home: str,
    safe_env: frozenset[str] = BASE_SAFE_ENV_NAMES,
    dir_changed: bool = False,
    protected: Sequence[str] = (),
) -> bool:
    """Return True iff ``cmd`` is in the grammar and every part of it is allowed.

    Every simple command, including those inside ``"$(...)"`` bodies, must match
    an allow pattern; every output target must pass ``redirect_target_ok`` and
    every input target ``input_target_ok``. If any part of the command
    (conservatively: the raw text, quotes included) changes directory, every
    relative redirect target in it defers.

    Parameters
    ----------
    cmd : str
        The full shell command string.
    patterns : Sequence[str]
        The ``Bash(...)`` allow patterns.
    roots : Sequence[str]
        Allowed root directories for redirect targets.
    cwd : str
        Directory relative redirect targets resolve against.
    home : str
        Home directory for ``~/`` in allow patterns.
    safe_env : frozenset[str]
        Variable names whose leading assignment may be stripped.
    dir_changed : bool
        True when an enclosing command already changed directory.
    protected : Sequence[str]
        Directories whose subtrees are never write targets.

    Returns
    -------
    bool
        True when every executed command matches the allow-list.
    """
    parsed = parse_strict(cmd, safe_env)
    if not parsed:
        return False
    # Also scan a quote/continuation-free copy: over-detecting a directory
    # change only defers more.
    dir_changed = (
        dir_changed
        or bool(DIR_CHANGE.search(cmd))
        or bool(DIR_CHANGE.search(re.sub(r"\\\n|[\"'\\]", "", cmd)))
    )
    for simple in iter_commands(parsed):
        if not all(
            redirect_target_ok(t, roots, cwd, home, dir_changed, protected)
            for t in simple.out_targets
        ):
            return False
        if not all(
            input_target_ok(t, roots, cwd, home, dir_changed) for t in simple.in_targets
        ):
            return False
        if not any(pattern_matches(p, simple.text, home) for p in patterns):
            return False
    return True


def simple_commands(
    cmd: str, safe_env: frozenset[str] = BASE_SAFE_ENV_NAMES
) -> list[str] | None:
    """List every simple command the shell would execute for ``cmd``.

    Includes commands inside ``"$(...)"`` bodies. Leading ``safe_env``
    assignments, wrappers and redirects are stripped (see ``SimpleCommand``).

    Parameters
    ----------
    cmd : str
        The full shell command string.
    safe_env : frozenset[str]
        Variable names whose leading assignment may be stripped.

    Returns
    -------
    list[str] | None
        The simple commands' texts, or None when the command is outside the
        strict grammar.
    """
    parsed = parse_strict(cmd, safe_env)
    if parsed is None:
        return None
    return [simple.text for simple in iter_commands(parsed)]


ALLOW = "allow"
DENY = "deny"
NO_OPINION = "no_opinion"

# Long options of the read-only commands that run a program or write a file.
RUNNER_OPTIONS = (
    "--pre",
    "--pager",
    "--hostname-bin",
    "--output",
    "--ext-diff",
    "--textconv",
    "--open-files-in-pager",
)

READ_ONLY_COMMANDS = frozenset(
    {"grep", "egrep", "fgrep", "rg", "ag", "ack", "cat", "head", "tail", "less"}
    | {"wc", "ls", "git grep", "git log", "git show", "git diff", "git blame"}
)


@dataclass(frozen=True)
class Verdict:
    """A gate decision: one of ``ALLOW``, ``DENY`` or ``NO_OPINION``."""

    decision: str
    reason: str = ""


@dataclass(frozen=True)
class BannedPattern:
    """A regex that must not appear in commands or newly written file text."""

    regex: re.Pattern[str]
    pattern: str
    message: str
    replacement: str
    exempt_paths: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> BannedPattern | None:
        """Build an entry from a policy dict, or None if it is unusable.

        Parameters
        ----------
        d : Mapping[str, Any]
            Raw entry with ``pattern`` and optional ``message``,
            ``replacement`` and ``exempt_paths``.

        Returns
        -------
        BannedPattern | None
            None when the entry is not a mapping, ``pattern`` is missing or not a
            valid regex, or another field has the wrong type.
        """
        if not isinstance(d, Mapping):
            return None
        pattern = d.get("pattern")
        if not isinstance(pattern, str):
            return None
        try:
            regex = re.compile(pattern)
        except re.error:
            return None
        message = d.get("message", "")
        replacement = d.get("replacement", "")
        exempt = d.get("exempt_paths", ())
        if not isinstance(message, str) or not isinstance(replacement, str):
            return None
        if not isinstance(exempt, (list, tuple)):
            return None
        if not all(isinstance(g, str) for g in exempt):
            return None
        return cls(
            regex=regex,
            pattern=pattern,
            message=message,
            replacement=replacement,
            exempt_paths=tuple(exempt),
        )


@dataclass(frozen=True)
class Policy:
    """Everything the verdict functions need to decide.

    ``withhold`` holds the contents of ``ask`` and ``deny`` ``Bash(...)`` rules:
    a command matching one is never auto-allowed.
    """

    allow: tuple[str, ...] = ()
    withhold: tuple[str, ...] = ()
    safe_env: frozenset[str] = BASE_SAFE_ENV_NAMES
    banned: tuple[BannedPattern, ...] = ()


def _blocked(subject: str, banned: BannedPattern) -> Verdict:
    return Verdict(
        DENY,
        f"Blocked: {subject} matches banned pattern {banned.pattern}: "
        f"{banned.message}. Use instead: {banned.replacement}.",
    )


def _is_read_only(simple: str) -> bool:
    words = simple.split()
    if not words:
        return False
    two = " ".join(words[:2])
    if words[0] not in READ_ONLY_COMMANDS and two not in READ_ONLY_COMMANDS:
        return False
    # Options that make a "search" run a program or write a file. git (and
    # some search tools) accept any unambiguous abbreviation of a long option,
    # so a prefix of one counts too; a short cluster holding `O` is `-O`.
    for w in (w.lstrip("\"'") for w in words[1:]):
        name = w.split("=", 1)[0]
        if len(name) > 2 and name.startswith("--"):
            if any(opt.startswith(name) for opt in RUNNER_OPTIONS):
                return False
        elif name.startswith("-") and "O" in name:
            return False
    return True


# Git global options that take a separate value when written without `=`.
GIT_ARG_OPTIONS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
    | {"--exec-path", "--attr-source"}
)
# Single-letter git global flags that take no value.
GIT_SHORT_FLAGS = frozenset({"-p", "-P"})


def _normalize_for_withhold(simple: str) -> str:
    """Respell a simple command to its canonical form for withhold matching.

    For ``git``, every leading global option is dropped, so ``git -C . push``
    and ``git --exec-path=/x push`` both read as ``git push``.
    """
    words = simple.split(" ")
    if words[0] != "git":
        return simple
    i = 1
    while i < len(words):
        w = words[i]
        if w in GIT_ARG_OPTIONS:
            i += 2
        elif w.startswith("--") or w in GIT_SHORT_FLAGS:
            i += 1  # any long option (`=value` attached) or a bare short flag
        else:
            break
    return " ".join(["git"] + words[i:])


def shell_verdict(
    command: str,
    policy: Policy,
    cwd: str,
    roots: Sequence[str],
    home: str,
    protected: Sequence[str] = (),
) -> Verdict:
    """Decide a shell command: deny a banned hit, allow a listed one, else defer.

    A banned hit is forgiven only when the command is in the strict grammar
    and every executed simple command is a read-only search that redirects
    output nowhere but ``/dev/null``, so ``rg old_call`` passes but
    ``rg old_call | sh``, ``rg old_call > f`` and any command outside the
    grammar do not. A command longer than ``MAX_ALLOW_COMMAND_CHARS`` is outside
    the grammar: it is only checked for banned hits (unforgiven), never
    allowed. ``protected`` directories are never write targets.
    """
    parsed = parse_strict(command, policy.safe_env)
    flat = None if parsed is None else list(iter_commands(parsed))
    for banned in policy.banned:
        if not banned.regex.search(command):
            continue
        forgiven = flat is not None and all(
            _is_read_only(c.text) and all(t == "/dev/null" for t in c.out_targets)
            for c in flat
        )
        if not forgiven:
            return _blocked("this command", banned)
    if flat is None:
        return Verdict(NO_OPINION)
    texts = [c.text for c in flat]
    # Also a quote-free spelling, so `git 'push'` still reads as `git push`.
    texts += [re.sub("[\"']", "", t) for t in texts]
    forms = texts + [_normalize_for_withhold(t) for t in texts]
    if any(pattern_matches(w, c, home) for w in policy.withhold for c in forms):
        return Verdict(NO_OPINION)
    if is_fully_allowed(
        command,
        policy.allow,
        roots,
        cwd,
        home,
        policy.safe_env,
        protected=protected,
    ):
        return Verdict(ALLOW)
    return Verdict(NO_OPINION)


def _is_exempt(path: str, banned: BannedPattern, project_root: str) -> bool:
    rel = os.path.relpath(os.path.normpath(os.path.abspath(path)), project_root)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return False
    return any(
        fnmatch.fnmatchcase(rel, g.replace("**", "*")) for g in banned.exempt_paths
    )


def edit_verdict(
    path: str, before: str, after: str, policy: Policy, project_root: str
) -> Verdict:
    """Deny an edit that adds more banned hits than the file already had."""
    for banned in policy.banned:
        if _is_exempt(path, banned, project_root):
            continue
        if len(banned.regex.findall(after)) > len(banned.regex.findall(before)):
            return _blocked(f"this edit to {path}", banned)
    return Verdict(NO_OPINION)
