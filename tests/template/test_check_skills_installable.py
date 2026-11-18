"""Tests for ``scripts/check_skills_installable.py``.

The script runs the skills CLI's real install; here ``NPX_CMD`` points at a fake
``npx`` that records its argv and creates (or omits) the installed skill files.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess  # nosec B404 -- fixed argv, in-repo script, no shell
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "check_skills_installable.py"
_PIN: str = json.loads(
    (_REPO / ".github" / "actions" / "skills-cli" / "package.json").read_text()
)["dependencies"]["skills"]
_NAMES: list[str] = list(
    json.loads((_REPO / ".github" / "template-skills-lock.json").read_text())["skills"]
)

_FAKE = """\
import json, os, sys
from pathlib import Path
Path(os.environ["FAKE_ARGV"]).write_text(
    json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                "telemetry": os.environ.get("DISABLE_TELEMETRY")}))
if os.environ.get("FAKE_SLEEP"):
    import time
    time.sleep(float(os.environ["FAKE_SLEEP"]))
if os.environ.get("FAKE_FAIL"):
    print("boom: cannot install")
    sys.exit(3)
skip = os.environ.get("FAKE_SKIP")
argv = sys.argv
for name in [argv[i + 1] for i, a in enumerate(argv) if a == "--skill"]:
    if name == skip:
        continue
    d = Path(".agents/skills") / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("x")
"""


def _run(
    tmp_path: Path, script: Path = _SCRIPT, **env: str
) -> subprocess.CompletedProcess[str]:
    impl = tmp_path / "npx_impl.py"
    impl.write_text(_FAKE)
    fake = tmp_path / "npx"
    fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    full_env = {
        **os.environ,
        "NPX_CMD": str(fake),
        "FAKE_ARGV": str(tmp_path / "argv.json"),
        **env,
    }
    return subprocess.run(  # nosec B603 -- fixed argv
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )


def test_argv_is_the_nightly_update_install(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    seen = json.loads((tmp_path / "argv.json").read_text())
    assert seen["argv"] == [
        "-y",
        f"skills@{_PIN}",
        "add",
        str(_REPO),
        *[x for n in _NAMES for x in ("--skill", n)],
        "-a",
        "universal",
        "-y",
    ]
    assert seen["telemetry"] == "1"
    assert Path(seen["cwd"]).resolve() != _REPO.resolve()


def test_one_missing_skill_fails_naming_it(tmp_path: Path) -> None:
    result = _run(tmp_path, FAKE_SKIP=_NAMES[2])
    assert result.returncode == 1
    assert _NAMES[2] in result.stdout + result.stderr


def test_cli_failure_fails_with_its_output(tmp_path: Path) -> None:
    result = _run(tmp_path, FAKE_FAIL="1")
    assert result.returncode == 1
    assert "boom: cannot install" in result.stdout + result.stderr


def _copy_with_lock(tmp_path: Path, skills: dict[str, object]) -> Path:
    """Copy the script into a fake repo whose lock lists ``skills``."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / ".github" / "actions" / "skills-cli").mkdir(parents=True)
    shutil.copy(_SCRIPT, root / "scripts" / _SCRIPT.name)
    shutil.copy(
        _REPO / ".github" / "actions" / "skills-cli" / "package.json",
        root / ".github" / "actions" / "skills-cli" / "package.json",
    )
    (root / ".github" / "template-skills-lock.json").write_text(
        json.dumps({"version": 2, "skills": skills})
    )
    return root / "scripts" / _SCRIPT.name


def test_empty_lock_fails_instead_of_passing(tmp_path: Path) -> None:
    result = _run(tmp_path, script=_copy_with_lock(tmp_path, {}))
    assert result.returncode == 1
    assert "no skills" in (result.stdout + result.stderr).lower()


def test_timeout_fails_cleanly(tmp_path: Path) -> None:
    result = _run(tmp_path, FAKE_SLEEP="30", SKILLS_INSTALL_TIMEOUT="1")
    assert result.returncode == 1
    out = result.stdout + result.stderr
    assert "timed out" in out
    assert "Traceback" not in out


def test_missing_npx_fails_cleanly(tmp_path: Path) -> None:
    result = _run(tmp_path, NPX_CMD=str(tmp_path / "no-such-npx"))
    assert result.returncode == 1
    out = result.stdout + result.stderr
    assert "not found" in out
    assert "Traceback" not in out
