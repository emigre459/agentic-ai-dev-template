"""Tests for the machine-wide ``single_flight.py`` lock bundled in resolve-pr-concerns.

Sequencing uses pipes (a holder blocks reading its stdin until the test closes it),
never fixed sleeps; every wait is bounded by ``_TIMEOUT``.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "resolve-pr-concerns"
    / "scripts"
    / "single_flight.py"
)
_TIMEOUT = 20


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("single_flight", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["single_flight"] = module
    spec.loader.exec_module(module)
    return module


sf = _load()


def _env(lock_dir: Path | None, **extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"CI", "SINGLE_FLIGHT", "SINGLE_FLIGHT_HELD", "SINGLE_FLIGHT_DIR"}
    }
    if lock_dir is not None:
        env["SINGLE_FLIGHT_DIR"] = str(lock_dir)
    env.update(extra)
    return env


def _run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    """Run to completion with text output; callers assert the return code."""
    kw.setdefault("text", True)
    return subprocess.run(cmd, check=False, **kw)


def _spawn(
    name: str, command: list[str], env: dict[str, str], cwd: Path
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(_SCRIPT), name, "--", *command],
        env=env,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _holder(env: dict[str, str], cwd: Path) -> subprocess.Popen[str]:
    """Start a lock holder that prints ``ready`` then blocks on stdin."""
    proc = _spawn("suite", ["sh", "-c", "echo ready; read x"], env, cwd)
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    return proc


def _wait_behind(
    env: dict[str, str], cwd: Path, holder: subprocess.Popen[str]
) -> tuple[subprocess.Popen[str], str]:
    """Start a waiter, return it once it printed its waiting line, plus that line."""
    waiter = _spawn("suite", ["sh", "-c", "echo second-ran"], env, cwd)
    assert waiter.stderr is not None
    line = waiter.stderr.readline()
    assert "waiting for another suite" in line
    return waiter, line


def _release(holder: subprocess.Popen[str]) -> None:
    assert holder.stdin is not None
    holder.stdin.close()
    holder.wait(timeout=_TIMEOUT)


def test_second_caller_waits_and_names_holder(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    env = _env(lock_dir)
    work = tmp_path / "wt"
    work.mkdir()
    holder = _holder(env, work)
    try:
        waiter, line = _wait_behind(env, work, holder)
        assert str(work.resolve()) in line
        assert f"pid={holder.pid}" in line
        assert waiter.poll() is None  # still queued while the holder runs
        _release(holder)
        out, _ = waiter.communicate(timeout=_TIMEOUT)
    finally:
        holder.kill()
    assert out.strip() == "second-ran"
    assert waiter.returncode == 0


def _make_checkout(path: Path, origin: str) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", origin], check=True
    )
    return path


def test_same_origin_different_checkout_paths_share_lock(tmp_path: Path) -> None:
    key = f"sf-test-{os.getpid()}-{tmp_path.name[-6:]}".replace("_", "-")
    origin = f"git@example.com:some-org/{key}.git"
    a = _make_checkout(tmp_path / "first-worktree", origin)
    b = _make_checkout(tmp_path / "other" / "second-worktree", origin)
    uid = os.getuid()
    assert sf.repo_key(a, sf.run_git) == key == sf.repo_key(b, sf.run_git)
    shared = Path(f"/tmp/{key}-{uid}")
    assert sf.lock_dir({}, sf.repo_key(a, sf.run_git), uid) == shared
    env = _env(None)
    try:
        holder = _holder(env, a)
        try:
            waiter, line = _wait_behind(env, b, holder)
            assert f"pid={holder.pid}" in line
            _release(holder)
            out, _ = waiter.communicate(timeout=_TIMEOUT)
        finally:
            holder.kill()
        assert out.strip() == "second-ran"
    finally:
        shutil.rmtree(shared, ignore_errors=True)


def test_repo_key_forms_and_fallback(tmp_path: Path) -> None:
    def fake(outputs: dict[str, str]) -> Callable[[Sequence[str], Path], str]:
        def run(args: Sequence[str], cwd: Path) -> str:
            return outputs.get(args[0], "")

        return run

    for url in (
        "https://github.com/org/my-repo.git",
        "https://github.com/org/my-repo",
        "git@github.com:org/my-repo.git",
        "git@github.com:org/my-repo.git\n",
    ):
        assert sf.repo_key(tmp_path, fake({"remote": url})) == "my-repo"
    common = tmp_path / "proj" / ".git"
    assert sf.repo_key(tmp_path, fake({"rev-parse": str(common)})) == "proj"


def test_lock_dir_override_and_default() -> None:
    assert sf.lock_dir({"SINGLE_FLIGHT_DIR": "/x/y"}, "r", 5) == Path("/x/y")
    assert sf.lock_dir({}, "r", 5) == Path("/tmp/r-5")


def test_exit_status_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("CI", "SINGLE_FLIGHT", "SINGLE_FLIGHT_HELD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SINGLE_FLIGHT_DIR", str(tmp_path / "locks"))
    argv = ["suite", "--", "sh", "-c", "exit 3"]
    assert sf.main(argv, replace_process=False) == 3
    proc = _run(
        [sys.executable, str(_SCRIPT), *argv],
        env=_env(tmp_path / "locks"),
        timeout=_TIMEOUT,
    )
    assert proc.returncode == 3


def test_bypass_single_flight_zero(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    holder = _holder(_env(lock_dir), tmp_path)
    try:
        out = _run(
            [sys.executable, str(_SCRIPT), "suite", "--", "sh", "-c", "echo hi"],
            env=_env(lock_dir, SINGLE_FLIGHT="0"),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    finally:
        holder.kill()
    assert out.stdout.strip() == "hi"


def test_bypass_ci(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    holder = _holder(_env(lock_dir), tmp_path)
    try:
        out = _run(
            [sys.executable, str(_SCRIPT), "suite", "--", "sh", "-c", "echo hi"],
            env=_env(lock_dir, CI="true"),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    finally:
        holder.kill()
    assert out.stdout.strip() == "hi"


def test_nested_held_lock_runs_directly(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    env = _env(lock_dir)
    inner = f"{sys.executable} {_SCRIPT} suite -- sh -c 'echo inner-ran'"
    # The outer holds `suite`; the inner call on the same name must not wait on it.
    out = _run(
        [sys.executable, str(_SCRIPT), "suite", "--", "sh", "-c", inner],
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )
    assert out.stdout.strip() == "inner-ran"
    assert "waiting" not in out.stderr
    marker = _run(
        [
            sys.executable,
            str(_SCRIPT),
            "suite",
            "--",
            "sh",
            "-c",
            "echo $SINGLE_FLIGHT_HELD",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )
    assert marker.stdout.strip() == "suite"


@pytest.mark.parametrize("name", ["bad/name", ".hidden", "sp ace", ""])
def test_invalid_lock_name_is_usage_error(tmp_path: Path, name: str) -> None:
    proc = _run(
        [sys.executable, str(_SCRIPT), name, "--", "true"],
        env=_env(tmp_path / "locks"),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )
    assert proc.returncode == 64
    assert "invalid lock name" in proc.stderr or "usage" in proc.stderr


def test_missing_separator_is_usage_error(tmp_path: Path) -> None:
    proc = _run(
        [sys.executable, str(_SCRIPT), "suite", "true"],
        env=_env(tmp_path / "locks"),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )
    assert proc.returncode == 64


def test_symlinked_lock_dir_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    proc = _run(
        [sys.executable, str(_SCRIPT), "suite", "--", "sh", "-c", "echo ran"],
        env=_env(link),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )
    assert proc.returncode == 73
    assert "ran" not in proc.stdout
    assert "refusing" in proc.stderr


def test_open_lock_dir_and_symlinked_lock_refused(tmp_path: Path) -> None:
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    open_dir.chmod(0o755)
    cmd = [sys.executable, str(_SCRIPT), "suite", "--", "true"]
    proc = _run(
        cmd, env=_env(open_dir), capture_output=True, text=True, timeout=_TIMEOUT
    )
    assert proc.returncode == 73
    priv = tmp_path / "priv"
    priv.mkdir(mode=0o700)
    (priv / "suite.lock").symlink_to(tmp_path / "target")
    proc = _run(cmd, env=_env(priv), capture_output=True, text=True, timeout=_TIMEOUT)
    assert proc.returncode == 73
    assert not (tmp_path / "target").exists()


def test_wraps_non_python_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("CI", "SINGLE_FLIGHT", "SINGLE_FLIGHT_HELD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SINGLE_FLIGHT_DIR", str(tmp_path / "locks"))
    assert sf.main(["suite", "--", "sh", "-c", "exit 0"], replace_process=False) == 0


def test_missing_command_exits_127(tmp_path: Path) -> None:
    proc = _run(
        [sys.executable, str(_SCRIPT), "suite", "--", "definitely-not-a-command-xyz"],
        env=_env(tmp_path / "locks"),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )
    assert proc.returncode == 127


def test_no_fcntl_runs_unlocked_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for var in ("CI", "SINGLE_FLIGHT", "SINGLE_FLIGHT_HELD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SINGLE_FLIGHT_DIR", str(tmp_path / "locks"))
    monkeypatch.setitem(sys.modules, "fcntl", None)
    module = _load()
    status = module.main(["suite", "--", "sh", "-c", "exit 4"], replace_process=False)
    err = capsys.readouterr().err
    assert status == 4
    assert err.count("\n") == 1 and "without the machine-wide lock" in err
    assert not (tmp_path / "locks").exists()
