"""Shell-command parser for the harness-neutral command gate.

Pure functions, standard library only, no I/O. The hook entry point (a later
layer) feeds a Bash command string and a policy in; this module answers two
questions about the string:

- ``is_fully_allowed``: does EVERY command the shell would execute match an
  allow pattern, and does every output redirect land inside an allowed root?
- ``simple_commands``: which simple commands would the shell execute (used to
  check them against banned patterns)?

It understands the command shapes agents really emit:

- leading env-var assignments, but only for the names in ``safe_env``
- bare process wrappers (``time``, ``nohup``, ``command``, ``builtin``,
  ``nice``), ``timeout <duration>`` and ``env`` with only ``-i`` / ``-u NAME``
- compound commands (``a && b``, ``a | b``, ``a ; b``, ``a & b``, newlines)
- ``cd <dir> && <cmd>`` (the ``cd`` becomes its own vetted subcommand)
- command and process substitution (``$(...)``, backticks, ``<(...)``)
- shell control structures (``for ... do ... done``, ``while``/``if`` conditions)
- output redirects, vetted against allowed roots; a relative target defers when
  the command changes directory
- ``#`` comments (ANSI-C ``$'...'`` strings are not modelled and fail closed)

An allow additionally requires a conservative syntax subset (see
``scan_substitutions``, ``_read_dollar`` and ``split_prefix``): no backslash but
a continuation or a double-quoted escape, only plain ``$`` expansions, no
bracket next to an expansion, only ``safe_env`` assignments, only bare wrappers,
no heredocs, subshells or redirect-only segments, and no write under a
``protected`` directory. Anything else fails closed.

SAFETY INVARIANT: this is a security boundary, so it defaults to "no". It
answers ``True`` from ``is_fully_allowed`` only when the command was fully
parsed AND every executed subcommand (including those inside substitutions)
independently matched an allow pattern AND every redirect target is inside an
allowed root. Any parse uncertainty or unknown token answers ``False`` (or
``None`` from ``simple_commands``). That is strictly safer than a whole-string
prefix match, which would approve ``uv run pytest && rm -rf /`` because the
string starts with ``uv run``.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Shell keywords that execute no external program on their own.
SCAFFOLD = {"do", "done", "then", "fi", "else", "elif", "esac", "{", "}", "!", ";;"}
# Headers whose word list is not executed (any $() inside is vetted separately).
# NOTE: `case` is deliberately absent: a `case ... in PATTERN) cmd ;; esac` arm
# executes `cmd`, unlike a `for`/`select` word list, so it must not be blessed
# wholesale; it falls through to per-command vetting (and typically defers).
WORDLIST_HEADERS = ("for ", "select ")
# Headers followed by an executed condition command.
COND_HEADERS = ("if ", "while ", "until ", "elif ", "time ")

# The only `NAME=value` assignments (bare, or leading a command) that may be
# allowed. They toggle output formatting or CI behaviour and cannot change WHICH
# code an allowed command loads or runs. Any other assignment (PATH, zsh `path`,
# EDITOR, GIT_SSH_COMMAND, LD_PRELOAD, ...) defers. A policy may extend this set.
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
# safe_env names one.
ZSH_INTEGER_SPECIALS = frozenset(
    {"SECONDS", "LINES", "COLUMNS", "HISTSIZE", "SAVEHIST", "TMOUT", "RANDOM"}
    | {"KEYTIMEOUT", "LISTMAX", "DIRSTACKSIZE", "MAILCHECK", "PERIOD", "SHLVL"}
    | {"OPTIND", "UID", "GID", "EUID", "EGID", "LINENO", "PPID", "ERRNO"}
    | {"HISTCMD", "TRY_BLOCK_ERROR", "TRY_BLOCK_INTERRUPT", "ZLE_RPROMPT_INDENT"}
)
# A `for`/`select` loop assigns its variable. A lowercase name is fine unless the
# shell gives it a special meaning (zsh `path` is tied to PATH, `mapfile` writes
# files, ...); bash's specials are all uppercase.
_LOOP_VAR = re.compile(r"^[a-z][a-z0-9_]*$")
ZSH_LOWERCASE_SPECIALS = frozenset(
    {"argv", "cdpath", "fignore", "fpath", "histchars", "mailpath", "manpath"}
    | {"module_path", "path", "pipestatus", "prompt", "psvar", "signals"}
    | {"status", "watch", "aliases", "builtins", "commands", "dirstack"}
    | {"errnos", "functions", "galiases", "history", "historywords", "jobdirs"}
    | {"jobstates", "jobtexts", "keymaps", "langinfo", "mapfile", "modules"}
    | {"nameddirs", "options", "parameters", "patchars", "reswords"}
    | {"saliases", "sysparams", "termcap", "terminfo", "userdirs", "usergroups"}
    | {"widgets", "epochtime", "funcstack", "funcfiletrace", "funcsourcetrace"}
    | {"functrace", "match", "mbegin", "mend", "reply", "zsh_eval_context"}
    | {"zsh_scheduled_events"}
)
# Wrappers stripped before matching, and only in these exact shapes: bare
# `time`/`nohup`/`command`/`builtin`/`nice`, `timeout <duration>`, and `env`
# with only `-i` / `-u NAME`. Any other option to any wrapper defers.
_BARE_WRAPPERS = frozenset({"time", "nohup", "command", "builtin", "nice"})
_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)
_ASSIGN_WORD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\+?=)")
_ARRAY_ASSIGN_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\[")
# A plain `$NAME` / `${NAME}` (including the `$S` substitution placeholder).
_PLAIN_REF = re.compile(r"\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)")
# Single-character special parameters allowed after `$`.
_SPECIAL_PARAMS = frozenset("123456789#?$!@*-0")
# A command longer than this gets no allow (the banned check still runs).
MAX_ALLOW_COMMAND_CHARS = 64 * 1024
# fd duplication / close targets (`>&2`, `2>&1`, `>&-`, `2>&1-`) -- not file writes.
FD_DUP_TARGET = re.compile(r"^(?:\d+-?|-)$")
# Any segment that changes directory makes relative redirect targets unverifiable.
DIR_CHANGE = re.compile(r"(?:^|[\s;&|(){}`])(?:cd|pushd|popd)(?=$|[\s;&|)])")
# Placeholder that replaces a substitution body; the `$` makes a redirect target
# built from a substitution fail `redirect_target_ok` (it cannot be verified).
SUBST_PLACEHOLDER = "$S"
# Unescaped characters after which a `#` begins a word (and therefore a comment).
_COMMENT_PRECEDERS = frozenset(" \t\n;&|")
# After these, whether `#` starts a comment depends on context we do not model
# (e.g. `$(x)#y` is mid-word, `(x)#y` is not) -> fail closed.
_AMBIGUOUS_HASH_PRECEDERS = frozenset("()<>")
# The only `${...}` bodies treated as inert: a plain variable name or a single
# positional digit. Anything else (`${x[i]}`, `${x:o}`, `${x@P}`, zsh `${(e)x}`,
# defaults holding `$(...)`) can evaluate code, so it fails closed.
_PLAIN_PARAM = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*|[0-9])$")


def _hash_kind(s: str, i: int) -> str:
    """Classify an unquoted ``#`` at ``s[i]``.

    Parameters
    ----------
    s : str
        The shell text being scanned.
    i : int
        Index of the ``#`` character.

    Returns
    -------
    str
        ``"comment"``, ``"word"`` or ``"ambiguous"``.
    """
    if i == 0:
        return "comment"
    backslashes, j = 0, i - 2
    while j >= 0 and s[j] == "\\":
        backslashes += 1
        j -= 1
    if backslashes % 2:
        return "word"  # the preceding char is escaped, so `#` is mid-word
    if s[i - 1] in _COMMENT_PRECEDERS:
        return "comment"
    if s[i - 1] in _AMBIGUOUS_HASH_PRECEDERS:
        return "ambiguous"
    return "word"


def _read_paren(s: str, start: int) -> tuple[str, int, bool]:
    """Read a ``$(...)`` / ``<(...)`` body up to its matching close paren.

    Parameters
    ----------
    s : str
        The shell text being scanned.
    start : int
        Index just after the opening paren.

    Returns
    -------
    tuple[str, int, bool]
        The body, the index after the close paren, and an ok flag.
    """
    depth, i, n, quote, buf = 1, start, len(s), None, []
    while i < n:
        c = s[i]
        if quote:
            if quote == '"' and c == "\\" and i + 1 < n:
                buf.append(c)
                buf.append(s[i + 1])
                i += 2
                continue
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        kind = _hash_kind(s, i) if c == "#" else "word"
        if kind == "ambiguous":
            return "", i, False
        if kind == "comment":
            # Comment: runs to end of line; quotes/parens inside are literal.
            end = s.find("\n", i)
            end = n if end == -1 else end
            buf.append(s[i:end])
            i = end
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return "".join(buf), i + 1, True
        buf.append(c)
        i += 1
    return "", i, False


def _read_backtick(s: str, start: int) -> tuple[str, int, bool]:
    """Read a backtick-substitution body up to the next unescaped backtick.

    Parameters
    ----------
    s : str
        The shell text being scanned.
    start : int
        Index just after the opening backtick.

    Returns
    -------
    tuple[str, int, bool]
        The body, the index after the closing backtick, and an ok flag.
    """
    i, n, buf = start, len(s), []
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            buf.append(c)
            buf.append(s[i + 1])
            i += 2
            continue
        if c == "`":
            return "".join(buf), i + 1, True
        buf.append(c)
        i += 1
    return "", i, False


def _splice_continuations(s: str, i: int) -> str:
    """Remove every backslash-newline pair that starts at ``s[i]``.

    Parameters
    ----------
    s : str
        The shell text being scanned.
    i : int
        Index where a continuation may start.

    Returns
    -------
    str
        ``s`` with the continuations at ``i`` removed.
    """
    while s.startswith("\\\n", i):
        s = s[:i] + s[i + 2 :]
    return s


def _read_dollar(s: str, i: int, in_dq: bool) -> tuple[str, int, str]:
    """Classify the ``$`` at ``s[i]`` (not ``$(`` or an unquoted ``$'``).

    Only these are allowable: ``$NAME``, ``${NAME}``, ``${1}``-style braces,
    and the single-character specials ``$1``-``$9``, ``$0``, ``$#``, ``$?``,
    ``$$``, ``$!``, ``$@``, ``$*`` and ``$-``, never followed by ``[`` (zsh
    subscripts it and evaluates the index as math, which can run code). A
    special must not be followed by a name character either: zsh reads ``$#x``
    as the length of ``x``. A ``$`` followed by a blank, the end, or (inside
    double quotes) the closing ``"`` is a literal dollar sign. Every other form
    (``$~x``, ``$+x``, ``$^x``, ``$=x``, ``$[``, ``${x:-y}``, ``$"..."``) is
    ``"bad"``.

    Parameters
    ----------
    s : str
        The shell text being scanned (continuations at ``i`` already spliced).
    i : int
        Index of the ``$``.
    in_dq : bool
        True when the ``$`` is inside double quotes.

    Returns
    -------
    tuple[str, int, str]
        The (possibly further spliced) text, the index after the expansion, and
        ``"param"``, ``"literal"`` or ``"bad"``.
    """
    nxt = s[i + 1 : i + 2]
    if nxt == "{":
        close = s.find("}", i + 2)
        if close == -1 or not _PLAIN_PARAM.match(s[i + 2 : close]):
            return s, i, "bad"
        k, name_like = close + 1, True
    elif nxt and nxt in _NAME_CHARS and not nxt.isdigit():
        k = i + 1
        while True:
            s = _splice_continuations(s, k)
            if k < len(s) and s[k] in _NAME_CHARS:
                k += 1
            else:
                break
        name_like = True
    elif nxt and nxt in _SPECIAL_PARAMS:
        k, name_like = i + 2, False
    elif not nxt or nxt in " \t\n" or (in_dq and nxt == '"'):
        return s, i + 1, "literal"
    else:
        return s, i, "bad"
    s = _splice_continuations(s, k)
    after = s[k : k + 1]
    if after == "[" or (not name_like and after and after in _NAME_CHARS | {"{"}):
        return s, i, "bad"
    return s, k, "param"


# Unquoted characters that end a word (rule 3 tracks `$` and `[`/`]` per word).
_WORD_BREAKS = frozenset(" \t\n;&|<>")
# The only characters a backslash may escape inside double quotes.
_DQ_ESCAPABLE = frozenset('"\\`$')


def scan_substitutions(s: str) -> tuple[list[str], str, bool]:
    """Extract command/process substitutions and blank them out.

    ``ok`` in the result is False whenever the command leaves the conservative
    syntax subset the gate can reason about, so the caller defers. Single-quoted
    regions are literal (no substitution); double-quoted regions still expand
    ``$()``/backticks and ``$NAME``. Word-initial ``#`` comments are dropped up
    to the newline (a ``'`` inside one is not a quote). Backslash-newline
    continuations outside single quotes and comments are spliced out first, as
    the shell does, so they cannot hide anything below.

    Outside the subset (``ok`` is False):

    - any backslash other than a continuation, except a double-quoted escape of
      a double quote, a backslash, a backtick or ``$`` (rule 1);
    - any ``$`` form ``_read_dollar`` does not accept (rule 2), and ANSI-C
      ``$'...'`` strings;
    - an unquoted ``[`` or ``]`` in a word that also holds a ``$`` expansion or
      substitution (rule 3; zsh may subscript it and evaluate math);
    - an unquoted heredoc ``<<`` (a ``<<<`` here-string is fine);
    - any unquoted ``(`` or ``)`` that is not part of ``$(``, ``<(`` or ``>(``.

    Both the allow view and the banned/withhold view go through this scanner,
    so they cannot drift.

    Parameters
    ----------
    s : str
        The full shell command string to scan.

    Returns
    -------
    tuple[list[str], str, bool]
        The inner command strings, the command with substitutions blanked, and
        an ok flag.
    """
    inners: list[str] = []
    out: list[str] = []
    i, n, quote = 0, len(s), None
    word_dollar = word_bracket = False
    while i < n:
        if quote != "'":
            # Bash deletes a backslash-newline outside single quotes before it
            # tokenizes (`<\⏎<` is `<<`, `$\⏎{` is `${`). Splice it out at `i`,
            # and at `i + 1` unless `s[i]` starts an escape, a single quote or
            # a comment (none of which continue), so no lookahead is split.
            s = _splice_continuations(s, i)
            if s[i : i + 1] not in ("\\", "'", "#"):
                s = _splice_continuations(s, i + 1)
            n = len(s)
            if i >= n:
                break
        c = s[i]
        if quote == "'":
            out.append(c)
            if c == "'":
                quote = None
            i += 1
            continue
        if quote == '"' and c == "\\":
            # A double-quoted `\"`, `\\`, `` \` `` or `\$` cannot move a word or
            # operator boundary (a commit message with \"quoted\" text is fine).
            if s[i + 1 : i + 2] not in _DQ_ESCAPABLE or i + 1 >= n:
                return inners, "", False
            out.append(s[i : i + 2])
            i += 2
            continue
        active = quote == '"' or quote is None
        if quote == '"' and c == '"':
            quote = None
            out.append(c)
            i += 1
            continue
        kind = _hash_kind(s, i) if quote is None and c == "#" else "word"
        if kind == "ambiguous":
            return inners, "", False
        if kind == "comment":
            end = s.find("\n", i)
            i = n if end == -1 else end
            continue
        if quote is None and s.startswith("$'", i):
            return inners, "", False
        if quote is None and c == "'":
            quote = "'"
            out.append(c)
            i += 1
            continue
        if quote is None and c == '"':
            quote = '"'
            out.append(c)
            i += 1
            continue
        if quote is None and c == "\\":
            return inners, "", False  # rule 1: only continuations, spliced above
        if quote is None and s.startswith("<<<", i):
            out.append("<<<")  # here-string: one word of input, no body
            word_dollar = word_bracket = False
            i += 3
            continue
        if quote is None and s.startswith("<<", i):
            return inners, "", False  # heredoc body is not modelled
        if active and s.startswith("$(", i):
            inner, j, ok = _read_paren(s, i + 2)
            if not ok or word_bracket:
                return inners, "", False
            s = _splice_continuations(s, j)
            n = len(s)
            if s[j : j + 1] == "[":
                return inners, "", False
            inners.append(inner)
            out.append(SUBST_PLACEHOLDER)
            word_dollar = True
            i = j
            continue
        if active and c == "$":
            s, k, dollar = _read_dollar(s, i, quote == '"')
            n = len(s)
            if dollar == "bad" or (dollar == "param" and word_bracket):
                return inners, "", False
            word_dollar = word_dollar or dollar == "param"
            out.append(s[i:k])
            i = k
            continue
        if (
            active
            and quote is None
            and (s.startswith("<(", i) or s.startswith(">(", i))
        ):
            inner, j, ok = _read_paren(s, i + 2)
            if not ok:
                return inners, "", False
            inners.append(inner)
            out.append(SUBST_PLACEHOLDER)
            word_dollar, word_bracket = True, False
            i = j
            continue
        if active and c == "`":
            inner, j, ok = _read_backtick(s, i + 1)
            if not ok or word_bracket:
                return inners, "", False
            inners.append(inner)
            out.append(SUBST_PLACEHOLDER)
            word_dollar = True
            i = j
            continue
        if quote is None and c in "()":
            # Not `$(`/`<(`/`>(` (consumed above): a subshell, function
            # definition, arithmetic `$((`'s inner paren, zsh `=(...)` or glob
            # qualifier, or a `case` arm. None is modelled -> fail closed.
            return inners, "", False
        if quote is None and c in _WORD_BREAKS:
            word_dollar = word_bracket = False
        elif quote is None and c in "[]":
            if word_dollar:
                return inners, "", False  # rule 3: `$x[y]`, `$x/[ab]`, ...
            word_bracket = True
        out.append(c)
        i += 1
    if quote is not None:
        return inners, "", False
    return inners, "".join(out), True


def split_subcommands(s: str) -> list[str]:
    """Split on top-level ``&&``, ``||``, ``;``, ``|``, ``&`` and newlines.

    Quotes are respected. A standalone ``&`` (background separator, e.g.
    ``a & b``) IS a separator and is split; otherwise ``echo x & rm -rf /``
    would ride through as one segment and prefix-match ``echo *``. A ``&`` that
    is part of a redirection (``2>&1``, ``>&2``, ``&>file``) is NOT a separator.

    Parameters
    ----------
    s : str
        The shell command string to split.

    Returns
    -------
    list[str]
        The top-level subcommand segments, in order.
    """
    segs: list[str] = []
    buf: list[str] = []
    i, n, quote = 0, len(s), None
    escaped = False  # True when buf[-1] was escaped by a backslash
    while i < n:
        c = s[i]
        if quote:
            if quote == '"' and c == "\\" and i + 1 < n:
                # A `\"` inside double quotes is an escaped quote, not a close.
                buf.append(c)
                buf.append(s[i + 1])
                i += 2
                continue
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
            escaped = False
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            escaped = False
            continue
        if c == "\\" and i + 1 < n:
            buf.append(c)
            buf.append(s[i + 1])
            i += 2
            escaped = True
            continue
        if s[i : i + 2] in ("&&", "||"):
            segs.append("".join(buf))
            buf = []
            i += 2
            continue
        if c in (";", "\n", "|"):
            segs.append("".join(buf))
            buf = []
            i += 1
            continue
        if c == "&":
            # `&&` was consumed above; here `&` is either a background separator
            # or part of a redirection (`2>&1`, `>&2`, `&>`). Only split the former.
            prev = buf[-1] if buf and not escaped else ""
            nxt = s[i + 1] if i + 1 < n else ""
            if prev not in (">", "<") and nxt != ">":
                segs.append("".join(buf))
                buf = []
                i += 1
                continue
        buf.append(c)
        i += 1
        escaped = False
    segs.append("".join(buf))
    return [seg.strip() for seg in segs if seg.strip()]


def _safe_value(value: str) -> bool:
    """Return True when an assignment value can be allowed (rule 4).

    No backslash, backtick, ``[``, ``]``, ``(`` or ``)``, and no ``$`` other
    than a plain ``$NAME`` / ``${NAME}`` (which covers the ``$S`` placeholder of
    an already-vetted substitution).

    Parameters
    ----------
    value : str
        The text after ``=``, quotes included.

    Returns
    -------
    bool
        True when the value is allowable.
    """
    if any(ch in value for ch in "\\`[]()"):
        return False
    return "$" not in _PLAIN_REF.sub("", value)


def _skip_blanks(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\n":
        i += 1
    return i


def split_prefix(s: str, safe_env: frozenset[str]) -> tuple[str, bool]:
    """Strip leading assignments and wrappers from one simple command.

    Words are read with the quote-aware ``_read_word``, so an escaped or quoted
    space never ends an assignment or an option. Allowed, and stripped:

    - ``NAME=value`` when NAME is in ``safe_env`` (never a zsh integer special)
      and the value passes ``_safe_value`` (rule 4);
    - bare ``time``/``nohup``/``command``/``builtin``/``nice``,
      ``timeout <duration>``, and ``env`` with only ``-i`` / ``-u NAME``
      followed by such assignments (rule 5).

    Anything else in the prefix (another assignment, an array-element
    assignment, a wrapper option, a wrapper with no command) makes the
    command not allowable.

    Parameters
    ----------
    s : str
        One simple command with its redirects already removed.
    safe_env : frozenset[str]
        Variable names that may be assigned.

    Returns
    -------
    tuple[str, bool]
        The command after the prefix (or ``s`` unchanged when not allowable)
        and an allowable flag. An empty command means pure assignments.
    """
    names = safe_env - ZSH_INTEGER_SPECIALS
    i = _skip_blanks(s, 0)
    assign_ok, wrapped = True, False
    while i < len(s):
        word, j, ok = _read_word(s, i)
        if not ok or not word:
            return s, False
        if _ARRAY_ASSIGN_WORD.match(word) and "=" in word:
            return s, False
        m = _ASSIGN_WORD.match(word)
        if m and assign_ok:
            if m.group(1) not in names or m.group(2) != "=":
                return s, False
            if not _safe_value(word[m.end() :]):
                return s, False
            i = _skip_blanks(s, j)
            continue
        if word in _BARE_WRAPPERS or word in ("timeout", "env"):
            i = _skip_blanks(s, j)
            nxt, k, ok = _read_word(s, i)
            if word == "timeout":
                if not ok or not _DURATION.match(nxt):
                    return s, False
                i = _skip_blanks(s, k)
            elif word == "env":
                while ok and nxt.startswith("-"):
                    if nxt == "-i":
                        i = _skip_blanks(s, k)
                    elif nxt == "-u":
                        name, k2, ok2 = _read_word(s, _skip_blanks(s, k))
                        if not ok2 or not _NAME.match(name):
                            return s, False
                        i = _skip_blanks(s, k2)
                    else:
                        return s, False
                    nxt, k, ok = _read_word(s, i)
            elif nxt.startswith("-"):
                return s, False
            wrapped, assign_ok = True, word == "env"
            continue
        break
    rest = s[i:].strip()
    if wrapped and not rest:
        return s, False  # `env` / `nohup` alone runs something we did not vet
    return rest, True


def _read_word(s: str, i: int) -> tuple[str, int, bool]:
    """Read one shell word from ``i`` (quotes kept, unquoted metachar ends it).

    Parameters
    ----------
    s : str
        The shell text being scanned.
    i : int
        Index where the word starts.

    Returns
    -------
    tuple[str, int, bool]
        The word, the index after it, and False when a quote was left open.
    """
    n, quote, buf = len(s), None, []
    while i < n:
        c = s[i]
        if quote:
            if quote == '"' and c == "\\" and i + 1 < n:
                buf.append(s[i : i + 2])
                i += 2
                continue
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
        elif c == "\\" and i + 1 < n:
            buf.append(s[i : i + 2])
            i += 2
            continue
        elif c.isspace() or c in "<>;&|()":
            break
        buf.append(c)
        i += 1
    return "".join(buf), i, quote is None


def extract_redirect_targets(s: str) -> tuple[list[str], str, bool]:
    """Pull output-redirect targets out of a simple command.

    Quote-aware: EVERY unquoted ``>`` is an output redirect (``>``, ``>>``,
    ``>|``, ``&>``, ``&>>``, ``N>``, ``<>``, ``>&word``) regardless of what
    precedes it, so a redirect glued to the previous word
    (``echo 'x'>~/.bashrc``) is still vetted. Only fd duplications (``>&2``,
    ``2>&1``, ``>&-``) carry no file target. A ``>`` with no readable target
    returns ok=False so the caller defers.

    Parameters
    ----------
    s : str
        A simple command that may contain output redirects.

    Returns
    -------
    tuple[list[str], str, bool]
        The redirect targets, the command with redirects removed, and an ok
        flag.
    """
    targets: list[str] = []
    out: list[str] = []
    i, n, quote = 0, len(s), None
    while i < n:
        c = s[i]
        if quote:
            if quote == '"' and c == "\\" and i + 1 < n:
                out.append(s[i : i + 2])
                i += 2
                continue
            out.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(s[i : i + 2])
            i += 2
            continue
        if c != ">":
            out.append(c)
            i += 1
            continue
        # Drop a word-initial fd number / `&` that belongs to this operator.
        j = len(out)
        while j and out[j - 1].isdigit():
            j -= 1
        if j < len(out) and (j == 0 or out[j - 1].isspace()):
            del out[j:]
        elif out and out[-1] == "&":
            out.pop()
        i += 1
        if i < n and s[i] in (">", "|"):
            i += 1
        dup = i < n and s[i] == "&"
        if dup:
            i += 1
        while i < n and s[i] in (" ", "\t"):
            i += 1
        word, i, ok = _read_word(s, i)
        if not ok or not word:
            return targets, "", False
        if not (dup and FD_DUP_TARGET.match(word)):
            targets.append(word)
        out.append(" ")
    if quote is not None:
        return targets, "", False
    return targets, "".join(out), True


def redirect_target_ok(
    target: str,
    roots: Sequence[str],
    cwd: str,
    home: str,
    dir_changed: bool = False,
    protected: Sequence[str] = (),
) -> bool:
    """Return True iff a redirect target resolves inside an allowed root.

    A target under a ``protected`` directory (the gate's own config, VCS
    internals) is never allowed, even inside a root; both sides are compared
    after ``realpath``, so a symlink cannot route around it.

    Parameters
    ----------
    target : str
        The redirect target as written, possibly quoted.
    roots : Sequence[str]
        Allowed root directories for writes.
    cwd : str
        Directory relative targets resolve against.
    home : str
        Home directory that ``~`` expands to.
    dir_changed : bool
        True when the command runs ``cd``/``pushd``/``popd`` somewhere, so a
        relative target no longer resolves against ``cwd`` and defers.
    protected : Sequence[str]
        Directories whose subtrees are never write targets.

    Returns
    -------
    bool
        True when the target is ``/dev/null`` or resolves inside an allowed
        root and outside every protected directory.
    """
    t = target.strip()
    quoted = len(t) >= 2 and (t[:1], t[-1:]) in (('"', '"'), ("'", "'"))
    if quoted:
        t = t[1:-1]
    if not t:
        return False
    if t == "/dev/null":
        return True
    if any(ch in t for ch in ("$", "*", "?", "[", "{", "`", "'", '"', "\\")):
        return False  # unresolved / glob / partial quoting -> cannot verify -> defer
    if not quoted and (t == "~" or t.startswith("~/")):
        t = home + t[1:]
    elif not quoted and t.startswith("~"):
        return False  # `~user/...` -> another home directory -> defer
    if not t.startswith("/") and dir_changed:
        return False
    # realpath, not normpath: an in-root symlink that points outside every root
    # (`link -> /etc`) must not count as an in-root write.
    p = os.path.realpath(t if t.startswith("/") else os.path.join(cwd, t))
    if any(_within(p, os.path.realpath(d)) for d in protected):
        return False
    return any(_within(p, os.path.realpath(r)) for r in roots)


def _within(path: str, root: str) -> bool:
    """Return True when ``path`` is ``root`` or lies under it."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def dequote_leading(cmd: str) -> str:
    """Strip quotes around the leading command word so a quoted path can match.

    Parameters
    ----------
    cmd : str
        A simple command whose first word may be quoted.

    Returns
    -------
    str
        The command with the quotes around its leading word removed.
    """
    if cmd[:1] in ('"', "'"):
        q = cmd[0]
        end = cmd.find(q, 1)
        if end != -1:
            return cmd[1:end] + cmd[end + 1 :]
    return cmd


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


def simple_command_allowed(
    seg: str,
    patterns: Sequence[str],
    roots: Sequence[str],
    cwd: str,
    home: str,
    safe_env: frozenset[str] = BASE_SAFE_ENV_NAMES,
    dir_changed: bool = False,
    protected: Sequence[str] = (),
) -> bool:
    """Vet a single (non-control-structure) command.

    Parameters
    ----------
    seg : str
        A single simple command segment.
    patterns : Sequence[str]
        The ``Bash(...)`` allow patterns.
    roots : Sequence[str]
        Allowed root directories for redirect targets.
    cwd : str
        Directory relative redirect targets resolve against.
    home : str
        Home directory for ``~`` expansion.
    safe_env : frozenset[str]
        Variable names that may be assigned.
    dir_changed : bool
        True when the surrounding command changes directory.
    protected : Sequence[str]
        Directories whose subtrees are never write targets.

    Returns
    -------
    bool
        True when the command, its prefix and its redirects are allowed.
    """
    targets, s2, ok = extract_redirect_targets(seg)
    if not ok:
        return False
    for t in targets:
        if not redirect_target_ok(t, roots, cwd, home, dir_changed, protected):
            return False
    cmd, ok = split_prefix(s2, safe_env)
    if not ok:
        return False
    if not cmd:
        # Pure safe assignments run nothing; a redirect (even `2>&1`) with no
        # command does (zsh runs $NULLCMD), so that defers.
        return ">" not in seg and "<" not in seg
    candidates = {cmd, dequote_leading(cmd)}
    return any(pattern_matches(p, c, home) for c in candidates for p in patterns)


def _loop_var_ok(seg: str, safe_env: frozenset[str]) -> bool:
    """Return True when a ``for``/``select`` header assigns an allowable name.

    Parameters
    ----------
    seg : str
        The header segment, e.g. ``for f in a b``.
    safe_env : frozenset[str]
        Variable names that may be assigned.

    Returns
    -------
    bool
        True for a safe_env name (never a zsh integer special) or a lowercase
        name with no special meaning in bash or zsh.
    """
    words = seg.split()
    if len(words) < 2:
        return False
    name = words[1]
    if name in safe_env - ZSH_INTEGER_SPECIALS:
        return True
    return bool(_LOOP_VAR.match(name)) and name not in ZSH_LOWERCASE_SPECIALS


def segment_allowed(
    seg: str,
    patterns: Sequence[str],
    roots: Sequence[str],
    cwd: str,
    home: str,
    safe_env: frozenset[str] = BASE_SAFE_ENV_NAMES,
    dir_changed: bool = False,
    protected: Sequence[str] = (),
) -> bool:
    """Vet one top-level subcommand, handling control-structure scaffolding.

    Parameters
    ----------
    seg : str
        One top-level subcommand segment.
    patterns : Sequence[str]
        The ``Bash(...)`` allow patterns.
    roots : Sequence[str]
        Allowed root directories for redirect targets.
    cwd : str
        Directory relative redirect targets resolve against.
    home : str
        Home directory for ``~`` expansion.
    safe_env : frozenset[str]
        Variable names whose leading assignment may be stripped.
    dir_changed : bool
        True when the surrounding command changes directory.

    Returns
    -------
    bool
        True when the segment is allowed.
    """

    def vet(part: str) -> bool:
        return segment_allowed(
            part, patterns, roots, cwd, home, safe_env, dir_changed, protected
        )

    seg = seg.strip()
    if seg.endswith("&"):
        seg = seg[:-1].strip()
    if not seg or seg in SCAFFOLD:
        return True
    first = seg.split()[0]
    if first in SCAFFOLD:
        rest = seg[len(first) :].strip()
        if not rest:
            return True
        # Re-split defensively: any operator in the remainder must be vetted,
        # never prefix-matched as one string (mirrors the COND_HEADERS path).
        return all(vet(s) for s in split_subcommands(rest))
    if seg.startswith(WORDLIST_HEADERS):
        # The word list is not executed (embedded $() is vetted separately),
        # but the loop variable is assigned.
        return _loop_var_ok(seg, safe_env)
    for header in COND_HEADERS:
        if seg.startswith(header):
            rest = seg[len(header) :].strip()
            if not rest:
                return True
            return all(vet(s) for s in split_subcommands(rest))
    return simple_command_allowed(
        seg, patterns, roots, cwd, home, safe_env, dir_changed, protected
    )


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
    """Return True iff every executed command matches the allow-list.

    Includes commands reached through substitutions. If any part of the command
    (conservatively: the raw text, quotes included) changes directory, every
    relative redirect target in it, and in its substitutions, defers.

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
        Home directory for ``~`` expansion.
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
    # Also scan a quote/backslash/continuation-free copy: bash runs `"cd"`,
    # `'cd'` and `c\⏎d` as the builtin, and the parser lets such a word match
    # a `cd` pattern. Over-detecting a directory change only defers more.
    dir_changed = (
        dir_changed
        or bool(DIR_CHANGE.search(cmd))
        or bool(DIR_CHANGE.search(re.sub(r"\\\n|[\"'\\]", "", cmd)))
    )
    inners, blanked, ok = scan_substitutions(cmd)
    if not ok:
        return False
    for inner in inners:
        if not is_fully_allowed(
            inner, patterns, roots, cwd, home, safe_env, dir_changed, protected
        ):
            return False
    segs = split_subcommands(blanked)
    if not segs:
        return False
    return all(
        segment_allowed(
            seg, patterns, roots, cwd, home, safe_env, dir_changed, protected
        )
        for seg in segs
    )


def _collect_segment(seg: str, safe_env: frozenset[str], out: list[str]) -> bool:
    """Append the simple commands one top-level segment executes to ``out``.

    Parameters
    ----------
    seg : str
        One top-level subcommand segment.
    safe_env : frozenset[str]
        Variable names whose leading assignment may be stripped.
    out : list[str]
        Receives the simple commands, in order.

    Returns
    -------
    bool
        False when the segment cannot be parsed.
    """
    seg = seg.strip()
    if seg.endswith("&"):
        seg = seg[:-1].strip()
    if not seg or seg in SCAFFOLD:
        return True
    first = seg.split()[0]
    rest: str | None = None
    if first in SCAFFOLD:
        rest = seg[len(first) :].strip()
    elif seg.startswith(WORDLIST_HEADERS):
        return True  # word list is not executed; embedded $() collected separately
    else:
        for header in COND_HEADERS:
            if seg.startswith(header):
                rest = seg[len(header) :].strip()
                break
    if rest is not None:
        return all(_collect_segment(s, safe_env, out) for s in split_subcommands(rest))
    _, s2, ok = extract_redirect_targets(seg)
    if not ok:
        return False
    # The same prefix parser as the allow view. When the prefix is not
    # allowable the whole command stays visible (so it is never read-only).
    cmd, _ = split_prefix(s2, safe_env)
    cmd = cmd.strip()
    if cmd:
        out.append(dequote_leading(cmd))
    return True


def _collect(cmd: str, safe_env: frozenset[str], out: list[str]) -> bool:
    """Collect every simple command in ``cmd``, substitutions first.

    Parameters
    ----------
    cmd : str
        A shell command string.
    safe_env : frozenset[str]
        Variable names whose leading assignment may be stripped.
    out : list[str]
        Receives the simple commands.

    Returns
    -------
    bool
        False when the command cannot be parsed.
    """
    inners, blanked, ok = scan_substitutions(cmd)
    if not ok:
        return False
    if not all(_collect(inner, safe_env, out) for inner in inners):
        return False
    return all(
        _collect_segment(seg, safe_env, out) for seg in split_subcommands(blanked)
    )


def simple_commands(
    cmd: str, safe_env: frozenset[str] = BASE_SAFE_ENV_NAMES
) -> list[str] | None:
    """List every simple command the shell would execute for ``cmd``.

    Includes commands inside substitutions and control-structure bodies. Env
    assignments in ``safe_env``, process wrappers and redirects are stripped,
    and the leading word is dequoted.

    Parameters
    ----------
    cmd : str
        The full shell command string.
    safe_env : frozenset[str]
        Variable names whose leading assignment may be stripped.

    Returns
    -------
    list[str] | None
        The simple commands, or None when the command cannot be parsed.
    """
    out: list[str] = []
    return out if _collect(cmd, safe_env, out) else None


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


def _writes_file(command: str) -> bool:
    """Return True if the raw command redirects output to anything but /dev/null."""
    targets, _, ok = extract_redirect_targets(command)
    return not ok or any(t != "/dev/null" for t in targets)


# Git global options that take a separate value when written without `=`.
GIT_ARG_OPTIONS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
    | {"--exec-path", "--attr-source"}
)
# Single-letter git global flags that take no value.
GIT_SHORT_FLAGS = frozenset({"-p", "-P"})


def _collapse_space(simple: str) -> str:
    """Collapse runs of unquoted whitespace to one space and trim the ends."""
    out: list[str] = []
    quote = None
    i = 0
    while i < len(simple):
        c = simple[i]
        if quote:
            out.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(simple):
                out.append(simple[i + 1])
                i += 1
            elif c == quote:
                quote = None
        elif c == "\\" and i + 1 < len(simple):
            out.append(simple[i : i + 2])
            i += 1
        elif c in ("'", '"'):
            quote = c
            out.append(c)
        elif c.isspace():
            if out and out[-1] != " ":
                out.append(" ")
        else:
            out.append(c)
        i += 1
    return "".join(out).strip()


def _normalize_for_withhold(simple: str) -> str:
    """Respell a simple command to its canonical form for withhold matching.

    For ``git``, every leading global option is dropped, so ``git -C . push``
    and ``git --exec-path=/x push`` both read as ``git push``.
    """
    text = _collapse_space(simple)
    words = text.split(" ")
    if words[0] != "git":
        return text
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

    A banned hit is forgiven only when every executed simple command is a
    read-only search, so ``rg old_call`` passes but ``rg old_call | sh`` and an
    unparseable command do not. A command longer than
    ``MAX_ALLOW_COMMAND_CHARS`` is only checked for banned hits (unforgiven),
    never allowed. ``protected`` directories are never write targets.
    """
    too_long = len(command) > MAX_ALLOW_COMMAND_CHARS
    simple = None if too_long else simple_commands(command, policy.safe_env)
    for banned in policy.banned:
        if not banned.regex.search(command):
            continue
        forgiven = (
            simple is not None
            and all(_is_read_only(c) for c in simple)
            and not _writes_file(command)
        )
        if not forgiven:
            return _blocked("this command", banned)
    if simple is None:
        return Verdict(NO_OPINION)
    forms = simple + [_normalize_for_withhold(c) for c in simple]
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
