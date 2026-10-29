"""Tests for the upgrade-deps skill's ``alert_status.py``.

The script answers "is each open security alert actually fixed on this tree?" by
testing every version locked in the alert's lockfile against the alert's vulnerable
range. Alerts come from a file or a stubbed ``gh`` runner, so nothing here touches
the network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "upgrade-deps"
    / "scripts"
    / "alert_status.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("alert_status", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["alert_status"] = module
    spec.loader.exec_module(module)
    return module


alert_status = _load()

UV_LOCK = """\
version = 1
requires-python = ">=3.13"

[[package]]
name = "black"
version = "26.5.1"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "typing-extensions"
version = "4.15.0"
source = { registry = "https://pypi.org/simple" }
"""

BUN_LOCK = """\
{
  "lockfileVersion": 1,
  "workspaces": {
    "": {
      "name": "app",
      "devDependencies": {
        "jsdom": "^29.1.1",
      },
    },
  },
  "overrides": {
    "undici": "^7.30.1",
  },
  "packages": {
    "@vitest/mocker": ["@vitest/mocker@4.1.11", "", {}, "sha512-a"],

    "jsdom": ["jsdom@29.1.1", "", { "dependencies": { "undici": "^7.25.0" } }, "sha512-b"],

    "undici": ["undici@7.30.1", "", {}, "sha512-c"],

    "jsdom/undici": ["undici@7.18.0", "", {}, "sha512-d"],
  }
}
"""


def _alert(
    number: int,
    package: str,
    manifest: str,
    vulnerable: str,
    patched: str | None = None,
    ecosystem: str = "pip",
) -> dict[str, Any]:
    return {
        "number": number,
        "state": "open",
        "dependency": {
            "manifest_path": manifest,
            "package": {"ecosystem": ecosystem, "name": package},
        },
        "security_advisory": {"severity": "high", "ghsa_id": f"GHSA-{number}"},
        "security_vulnerability": {
            "vulnerable_version_range": vulnerable,
            "first_patched_version": {"identifier": patched} if patched else None,
        },
    }


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "uv.lock").write_text(UV_LOCK)
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "bun.lock").write_text(BUN_LOCK)
    (tmp_path / "web" / "package.json").write_text("{}")
    return tmp_path


# --- version ranges -------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "vulnerable", "expected"),
    [
        ("26.5.1", "< 26.10.1", True),
        ("26.10.1", "< 26.10.1", False),
        ("7.18.0", ">= 7.0.0, < 7.30.1", True),
        ("6.9.9", ">= 7.0.0, < 7.30.1", False),
        ("7.30.1", ">= 7.0.0, < 7.30.1", False),
        ("1.2", "<= 1.2", True),
        ("1.2.0", "<= 1.2", True),
        ("1.2.1", "<= 1.2", False),
        ("2.0.0", "= 2.0.0", True),
        ("2.0.1", "= 2.0.0", False),
        ("3.0.0", "> 2.0.0", True),
        ("7.30.1-rc.1", "< 7.30.1", True),
        ("1.0rc1", "< 1.0", True),
        ("1.0.post1", "<= 1.0", False),
        ("10.0.0", "< 9.0.0", False),
        # Pre-release numbers compare as numbers, not text (beta.2 < beta.10).
        ("2.0.0-beta.2", "< 2.0.0-beta.10", True),
        ("2.0.0-beta.10", "< 2.0.0-beta.2", False),
        ("2.0.0rc9", "< 2.0.0rc10", True),
        ("2.0.0-alpha.1", "< 2.0.0-beta.1", True),
        ("2.0.0-rc.1", "< 2.0.0", True),
    ],
)
def test_in_range(version: str, vulnerable: str, expected: bool) -> None:
    assert alert_status.in_range(version, vulnerable) is expected


def test_an_unparseable_range_raises() -> None:
    with pytest.raises(ValueError, match="range"):
        alert_status.in_range("1.0.0", "~> 1.0")


# --- lockfiles ------------------------------------------------------------------


def test_uv_lock_versions(tree: Path) -> None:
    assert alert_status.locked_versions(tree / "uv.lock", "black", "pip") == ["26.5.1"]


def test_uv_lock_names_are_normalized(tree: Path) -> None:
    """Advisories and uv.lock may spell a PyPI name differently (PEP 503)."""
    assert alert_status.locked_versions(
        tree / "uv.lock", "Typing_Extensions", "pip"
    ) == ["4.15.0"]


def test_bun_lock_reads_top_level_and_nested_copies(tree: Path) -> None:
    lock = tree / "web" / "bun.lock"
    assert sorted(alert_status.locked_versions(lock, "undici", "npm")) == [
        "7.18.0",
        "7.30.1",
    ]


def test_bun_lock_scoped_names(tree: Path) -> None:
    lock = tree / "web" / "bun.lock"
    assert alert_status.locked_versions(lock, "@vitest/mocker", "npm") == ["4.1.11"]


def test_bun_lock_ignores_workspace_and_override_ranges(tree: Path) -> None:
    """Only the packages section counts: ``"jsdom": "^29.1.1"`` is a range, not a lock."""
    lock = tree / "web" / "bun.lock"
    assert alert_status.locked_versions(lock, "jsdom", "npm") == ["29.1.1"]


def test_an_absent_package_has_no_versions(tree: Path) -> None:
    assert alert_status.locked_versions(tree / "uv.lock", "requests", "pip") == []


@pytest.mark.parametrize(
    ("manifest", "ecosystem", "expected"),
    [
        ("uv.lock", "pip", "uv.lock"),
        ("pyproject.toml", "pip", "uv.lock"),
        ("web/bun.lock", "npm", "web/bun.lock"),
        ("web/package.json", "npm", "web/bun.lock"),
    ],
)
def test_lockfile_for_manifest(
    tree: Path, manifest: str, ecosystem: str, expected: str
) -> None:
    assert alert_status.lockfile_for(tree, manifest, ecosystem) == tree / expected


def test_lockfile_for_a_manifest_without_a_lockfile_is_none(tree: Path) -> None:
    (tmp := tree / "other").mkdir()
    (tmp / "requirements.txt").write_text("")
    assert alert_status.lockfile_for(tree, "other/requirements.txt", "pip") is None


def test_lockfile_follows_the_ecosystem_when_both_exist(tree: Path) -> None:
    """An npm alert in a folder with both lockfiles must read bun.lock, not uv.lock."""
    (tree / "web" / "uv.lock").write_text(UV_LOCK)
    assert alert_status.lockfile_for(tree, "web/package.json", "npm") == (
        tree / "web" / "bun.lock"
    )
    assert alert_status.lockfile_for(tree, "web/pyproject.toml", "pip") == (
        tree / "web" / "uv.lock"
    )


def test_a_lockfile_of_the_wrong_ecosystem_is_none(tree: Path) -> None:
    assert alert_status.lockfile_for(tree, "uv.lock", "npm") is None
    assert alert_status.lockfile_for(tree, "web/bun.lock", "pip") is None


def test_an_unsupported_ecosystem_is_none(tree: Path) -> None:
    assert alert_status.lockfile_for(tree, "uv.lock", "rubygems") is None


# --- per-alert status -----------------------------------------------------------


def test_a_vulnerable_locked_version_is_open(tree: Path) -> None:
    result = alert_status.status_of(
        _alert(2, "black", "uv.lock", "< 26.10.1", "26.10.1"), tree
    )
    assert result == {
        "number": 2,
        "severity": "high",
        "package": "black",
        "lockfile": "uv.lock",
        "locked_versions": ["26.5.1"],
        "vulnerable_range": "< 26.10.1",
        "first_patched": "26.10.1",
        "status": "open",
    }


def test_any_vulnerable_copy_keeps_the_alert_open(tree: Path) -> None:
    """The top-level undici is patched but jsdom's nested copy isn't."""
    alert = _alert(9, "undici", "web/bun.lock", ">= 7.0.0, < 7.30.1", "7.30.1", "npm")
    assert alert_status.status_of(alert, tree)["status"] == "open"


def test_a_patched_lock_clears_the_alert(tree: Path) -> None:
    alert = _alert(3, "black", "uv.lock", "< 26.0.0", "26.0.0")
    assert alert_status.status_of(alert, tree)["status"] == "cleared"


def test_a_package_no_longer_locked_clears_the_alert(tree: Path) -> None:
    alert = _alert(4, "requests", "uv.lock", "< 2.32.0", "2.32.0")
    result = alert_status.status_of(alert, tree)
    assert (result["status"], result["locked_versions"]) == ("cleared", [])


def test_a_missing_lockfile_is_unknown(tree: Path) -> None:
    alert = _alert(5, "black", "gone/uv.lock", "< 26.10.1")
    result = alert_status.status_of(alert, tree)
    assert (result["status"], result["lockfile"]) == ("unknown", None)


def test_an_npm_alert_beside_a_uv_lock_is_not_cleared_by_it(tree: Path) -> None:
    """Reading the wrong lockfile would find no copy and wrongly clear the alert."""
    (tree / "web" / "uv.lock").write_text(UV_LOCK)
    alert = _alert(9, "undici", "web/package.json", "< 7.30.1", "7.30.1", "npm")
    assert alert_status.status_of(alert, tree)["status"] == "open"


def test_an_unparseable_range_is_unknown(tree: Path) -> None:
    alert = _alert(6, "black", "uv.lock", "~> 26.0")
    assert alert_status.status_of(alert, tree)["status"] == "unknown"


# --- CLI ------------------------------------------------------------------------


def _alerts_file(tmp_path: Path, alerts: list[dict[str, Any]]) -> Path:
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps(alerts))
    return path


def test_main_exits_1_while_an_alert_is_open(
    tree: Path, tmp_path: Path, capsys: Any
) -> None:
    alerts = _alerts_file(
        tmp_path,
        [
            _alert(2, "black", "uv.lock", "< 26.10.1", "26.10.1"),
            _alert(3, "black", "uv.lock", "< 26.0.0", "26.0.0"),
        ],
    )
    code = alert_status.main(["--alerts-file", str(alerts), "--root", str(tree)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert [a["status"] for a in out["alerts"]] == ["open", "cleared"]
    assert out["open"] == 1 and out["unknown"] == 0


def test_main_exits_0_when_every_alert_is_cleared(
    tree: Path, tmp_path: Path, capsys: Any
) -> None:
    alerts = _alerts_file(tmp_path, [_alert(3, "black", "uv.lock", "< 26.0.0")])
    code = alert_status.main(["--alerts-file", str(alerts), "--root", str(tree)])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["open"] == 0


def test_main_exits_1_on_unknown(tree: Path, tmp_path: Path) -> None:
    alerts = _alerts_file(tmp_path, [_alert(5, "black", "gone/uv.lock", "< 1.0")])
    assert alert_status.main(["--alerts-file", str(alerts), "--root", str(tree)]) == 1


def test_main_reads_open_alerts_from_gh(tree: Path, capsys: Any) -> None:
    calls: list[Sequence[str]] = []

    def runner(argv: Sequence[str]) -> str:
        calls.append(argv)
        return "\n".join(
            json.dumps(a) for a in [_alert(3, "black", "uv.lock", "< 26.0.0")]
        )

    code = alert_status.main(["--repo", "o/r", "--root", str(tree)], runner=runner)
    assert code == 0
    [argv] = calls
    assert argv[:2] == ["gh", "api"]
    assert "repos/o/r/dependabot/alerts?state=open&per_page=100" in argv
    assert "--paginate" in argv


def test_main_without_tomllib_is_a_usage_error(
    tree: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """Under Python < 3.11 the script must say so, not exit 1 like an open alert."""
    monkeypatch.setattr(alert_status, "tomllib", None)
    alerts = _alerts_file(tmp_path, [_alert(3, "black", "uv.lock", "< 26.0.0")])
    code = alert_status.main(["--alerts-file", str(alerts), "--root", str(tree)])
    assert code == 64
    assert "3.11" in capsys.readouterr().err


def test_main_needs_a_source(capsys: Any) -> None:
    with pytest.raises(SystemExit) as exc:
        alert_status.main([])
    assert exc.value.code == 64


def test_main_reports_a_gh_failure_as_a_usage_error(tree: Path, capsys: Any) -> None:
    def runner(argv: Sequence[str]) -> str:
        raise alert_status.GhError("HTTP 403: Resource not accessible")

    code = alert_status.main(["--repo", "o/r", "--root", str(tree)], runner=runner)
    assert code == 64
    assert "403" in capsys.readouterr().err
