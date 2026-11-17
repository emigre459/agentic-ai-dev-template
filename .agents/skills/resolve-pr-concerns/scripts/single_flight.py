#!/usr/bin/env python3
"""Run a command under a machine-wide exclusive lock (single-flight).

Usage: ``single_flight.py <lock-name> -- <command...>``

One heavy job per machine: every caller naming the same ``<lock-name>`` queues
behind the one running (the skills use ``suite`` around ``make pr_check``). The
lock is ``fcntl.flock`` on ``<dir>/<lock-name>.lock``, held on a descriptor the
command inherits: this script takes the lock and then ``exec``s the command, so
no supervisor is left behind and the lock lives exactly as long as the command.

``<dir>`` is ``/tmp/<repo>-<uid>`` (``<repo>`` is the origin remote's repository
name, so every worktree of one repo shares a lock; never ``$TMPDIR``, which
sandboxed sessions set differently). ``SINGLE_FLIGHT_DIR`` overrides it. The
directory is created mode 700 and refused (exit 73, nothing run) unless it is a
real directory owned by this user with no group/other access; a symlink at the
lock path is refused too. The holder record ``<lock>.holder`` (worktree, pid,
start time) is advisory: written through a temp file plus rename, and named by a
waiter only while its pid is alive.

Bypasses (the command runs directly): ``CI`` set and non-empty, or
``SINGLE_FLIGHT=0``. A command run under the lock inherits
``SINGLE_FLIGHT_HELD=<name>``; a nested call naming a held lock runs directly.
Where ``fcntl`` is missing (Windows) the command runs unlocked after a warning.

Lock path, holder format and env names match the shell implementation
used by downstream repos, so the two interoperate. Stdlib only.
"""

from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no flock; run unlocked with a warning
    fcntl = None  # type: ignore[assignment]

EX_USAGE = 64
EX_CANTCREAT = 73
_NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")

GitRunner = Callable[[Sequence[str], Path], str]


def run_git(args: Sequence[str], cwd: Path) -> str:
    """Return stripped stdout of ``git <args>`` in ``cwd``, or ``""`` on failure.

    Parameters
    ----------
    args : Sequence[str]
        Arguments after ``git``.
    cwd : Path
        Directory to run in.

    Returns
    -------
    str
        Stripped stdout; empty when git is missing or exits non-zero.
    """
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )
    except OSError:
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _sanitize(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", key).lstrip(".") or "repo"


def repo_key(cwd: Path, runner: GitRunner) -> str:
    """Return the repository key that names the lock directory.

    Parameters
    ----------
    cwd : Path
        A checkout (any worktree) of the repository.
    runner : GitRunner
        ``runner(["remote", "get-url", "origin"], cwd)``-style git runner.

    Returns
    -------
    str
        Last path segment of the origin URL without ``.git``; else the basename
        of the parent of ``git rev-parse --git-common-dir``; else ``cwd``'s name.
    """
    url = runner(["remote", "get-url", "origin"], cwd).strip().rstrip("/")
    if url:
        segment = re.split(r"[/:]", url)[-1]
        segment = segment.removesuffix(".git")
        if segment:
            return _sanitize(segment)
    common = runner(["rev-parse", "--git-common-dir"], cwd).strip()
    if common:
        path = Path(common)
        if not path.is_absolute():
            path = cwd / path
        return _sanitize(Path(os.path.normpath(path)).parent.name)
    return _sanitize(cwd.resolve().name)


def lock_dir(env: Mapping[str, str], repo_key: str, uid: int) -> Path:
    """Return the lock directory: ``SINGLE_FLIGHT_DIR`` or ``/tmp/<repo_key>-<uid>``.

    Parameters
    ----------
    env : Mapping[str, str]
        Environment to read ``SINGLE_FLIGHT_DIR`` from.
    repo_key : str
        Repository key from :func:`repo_key`.
    uid : int
        Current user id.

    Returns
    -------
    Path
        The directory that holds ``<name>.lock``.
    """
    override = env.get("SINGLE_FLIGHT_DIR")
    if override:
        return Path(override.rstrip("/") or "/")
    return Path(f"/tmp/{repo_key}-{uid}")


def _refusal(directory: Path, lock: Path) -> str:
    """Return why ``directory`` is unusable, or ``""`` when it is fine."""
    try:
        info = directory.lstat()
    except OSError as exc:
        return f"cannot stat it: {exc.strerror}"
    if not stat.S_ISDIR(info.st_mode):
        return "not a real directory (missing, or a symlink)"
    if info.st_uid != os.getuid():
        return f"not owned by uid {os.getuid()}"
    if info.st_mode & 0o077:
        return f"mode {stat.S_IMODE(info.st_mode):03o} lets group/other in (want 700)"
    if lock.is_symlink():
        return f"{lock} is a symlink"
    return ""


def _ensure_dir(directory: Path, lock: Path) -> None:
    try:
        directory.mkdir(mode=0o700)
    except OSError:
        pass  # exists already, or unusable: the checks below decide
    why = _refusal(directory, lock)
    if why:
        print(
            f"single_flight: refusing to lock in {directory}: {why}.", file=sys.stderr
        )
        print(
            "single_flight: fix or remove it, or bypass with SINGLE_FLIGHT=0.",
            file=sys.stderr,
        )
        raise SystemExit(EX_CANTCREAT)


def _waiting_line(name: str, holder: Path) -> str:
    who = "unknown"
    try:
        line = holder.read_text().splitlines()[0]
    except (OSError, IndexError):
        line = ""
    match = re.search(r" pid=(\d+) ", f"{line} ")
    if match:
        try:
            os.kill(int(match.group(1)), 0)
            who = line
        except PermissionError:
            who = line  # alive, owned by someone else
        except ProcessLookupError:
            pass
    return f"single_flight: waiting for another {name} on this machine (held by: {who})"


def _write_holder(directory: Path, holder: Path) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".holder.", dir=directory)
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with os.fdopen(fd, "w") as record:
        record.write(f"worktree={os.getcwd()} pid={os.getpid()} started={started}\n")
    os.replace(tmp, holder)


def _restore_signals() -> None:
    """Python ignores SIGPIPE/SIGXFSZ and traps SIGINT; ignored signals survive exec."""
    for sig in ("SIGINT", "SIGPIPE", "SIGXFSZ"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), signal.SIG_DFL)


def _run(command: list[str], env: dict[str, str] | None, replace: bool) -> int:
    """Exec ``command`` (or run it as a child and return its status)."""
    try:
        if replace:
            os.execvpe(command[0], command, env if env is not None else os.environ)
        return subprocess.call(command, env=env)
    except OSError as exc:
        print(f"single_flight: {command[0]}: {exc.strerror}", file=sys.stderr)
        return 127 if isinstance(exc, FileNotFoundError) else 126


def _usage(message: str = "usage: single_flight.py <lock-name> -- <command...>") -> int:
    print(message, file=sys.stderr)
    return EX_USAGE


def main(argv: list[str] | None = None, *, replace_process: bool = True) -> int:
    """Take the lock and run the command.

    Parameters
    ----------
    argv : list[str] or None
        ``[<lock-name>, "--", *command]``; defaults to ``sys.argv[1:]``.
    replace_process : bool
        When True (the CLI), ``exec`` the command so the lock lives exactly as
        long as it and ``main`` never returns on success. When False, run it as
        a child holding the lock and return its exit status (used by tests).

    Returns
    -------
    int
        The command's exit status, or 64 usage / 73 unsafe lock dir / 126-127
        when the command cannot start.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 3 or args[1] != "--":
        return _usage()
    name, command = args[0], args[2:]
    if not _NAME_RE.match(name):
        return _usage(
            f"single_flight: invalid lock name '{name}' (letters, digits, . _ - only)"
        )
    env = os.environ
    if env.get("CI") or env.get("SINGLE_FLIGHT", "1") == "0":
        return _run(command, None, replace_process)
    held = [h for h in env.get("SINGLE_FLIGHT_HELD", "").split(":") if h]
    if name in held:
        return _run(command, None, replace_process)
    if fcntl is None:
        print(
            f"single_flight: no fcntl on this platform; running {name} "
            "without the machine-wide lock",
            file=sys.stderr,
        )
        return _run(command, None, replace_process)

    cwd = Path.cwd()
    directory = lock_dir(env, repo_key(cwd, run_git), os.getuid())
    lock = directory / f"{name}.lock"
    holder = directory / f"{name}.lock.holder"
    _ensure_dir(directory, lock)
    _restore_signals()
    try:
        fd = os.open(
            lock, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
    except OSError as exc:
        print(f"single_flight: cannot open {lock}: {exc.strerror}", file=sys.stderr)
        return EX_CANTCREAT
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(_waiting_line(name, holder), file=sys.stderr, flush=True)
        fcntl.flock(fd, fcntl.LOCK_EX)
    _write_holder(directory, holder)
    os.set_inheritable(fd, True)
    child_env = dict(env)
    child_env["SINGLE_FLIGHT_HELD"] = ":".join([*held, name])
    try:
        return _run(command, child_env, replace_process)
    finally:
        if not replace_process:
            os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
