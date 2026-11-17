"""Tests for the daily-update skill's ``daily_update.py``.

``gh`` is stubbed through the script's ``runner`` seam, so nothing here touches the
network or a real GitHub account.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "daily-update"
    / "scripts"
    / "daily_update.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("daily_update", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["daily_update"] = module
    spec.loader.exec_module(module)
    return module


daily_update = _load()

SCOPES_OK = "HTTP/2.0 200 OK\nX-OAuth-Scopes: repo, read:project\n\n{}"


class FakeGh:
    """Stub ``subprocess.run`` that answers ``gh`` calls by substring match."""

    def __init__(self, user: dict[str, Any] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.user = user or {"login": "octo", "name": "Octavia Cat", "email": None}
        self.linked_project: dict[str, Any] | None = None

    def __call__(self, cmd: Sequence[str], **_: Any) -> Any:
        args = list(cmd)[1:]
        self.calls.append(args)
        joined = " ".join(args)
        out = self._answer(args, joined)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def _answer(self, args: list[str], joined: str) -> str:
        if args[:3] == ["api", "-i", "user"]:
            return SCOPES_OK
        if args[:2] == ["api", "user"]:
            return self.user["login"]
        if args[0] == "api" and args[1] == f"users/{self.user['login']}":
            return json.dumps(self.user)
        if args[:2] == ["repo", "view"]:
            return "acme/widgets"
        if args[0] == "api" and "projectsV2" in joined:
            nodes = [self.linked_project] if self.linked_project else []
            return json.dumps(
                {"data": {"repository": {"projectsV2": {"nodes": nodes}}}}
            )
        if "default_branch" in joined:
            return "main"
        if args[0] == "api" and "/branches" in joined:
            return ""
        if args[0] == "api" and "/issues/" in joined:
            return ""
        if args[0] == "api" and "graphql" in args:
            return json.dumps(
                {
                    "data": {
                        "repositoryOwner": {
                            "projectV2": {
                                "items": {
                                    "pageInfo": {"hasNextPage": False},
                                    "nodes": [],
                                }
                            }
                        }
                    }
                }
            )
        if args[0] in {"pr", "issue"}:
            return "[]"
        return ""


@pytest.fixture
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return home


def _run_json(
    capsys: pytest.CaptureFixture[str], gh: FakeGh, *argv: str
) -> dict[str, Any]:
    code = daily_update.main(["--json", "--no-write-state", *argv], runner=gh)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return json.loads(captured.out)


def test_state_path_honors_xdg() -> None:
    path = daily_update.state_path({"XDG_STATE_HOME": "/x/state"}, "acme", "w", "octo")
    assert path == Path("/x/state/daily-update/acme-w/octo.json")


def test_state_path_defaults_to_local_state() -> None:
    path = daily_update.state_path({"HOME": "/h"}, "acme", "w", "octo")
    assert path == Path("/h/.local/state/daily-update/acme-w/octo.json")


def test_window_starts_at_last_run(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = state_home / "daily-update" / "acme-widgets" / "octo.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"last_run_at": "2026-01-02T03:04:05Z"}))
    payload = _run_json(capsys, FakeGh())
    assert payload["window"]["since"].startswith("2026-01-02T03:04:05")
    assert payload["window"]["is_first_run"] is False


def test_first_run_defaults_window(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _run_json(capsys, FakeGh())
    assert payload["window"]["is_first_run"] is True
    since = daily_update._parse_iso(payload["window"]["since"])
    until = daily_update._parse_iso(payload["window"]["until"])
    assert (until - since).total_seconds() == 24 * 3600


def test_successful_run_writes_state(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = daily_update.main(["--json"], runner=FakeGh())
    capsys.readouterr()
    assert code == 0
    written = state_home / "daily-update" / "acme-widgets" / "octo.json"
    assert "last_run_at" in json.loads(written.read_text())


def test_invoking_user_from_gh_api_user(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh({"login": "someone-else", "name": "Some One", "email": None})
    payload = _run_json(capsys, gh)
    assert payload["login"] == "someone-else"
    assert ["api", "user", "--jq", ".login"] in gh.calls


def test_display_name_from_users_api_not_hardcoded(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh({"login": "octo", "name": "Octavia Cat", "email": None})
    payload = _run_json(capsys, gh)
    assert payload["display_name"] == "Octavia Cat"
    assert ["api", "users/octo"] in [c[:2] for c in gh.calls]


def test_display_name_falls_back_to_login(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh({"login": "octo", "name": None, "email": None})
    assert _run_json(capsys, gh)["display_name"] == "octo"


def test_repo_comes_from_gh_repo_view(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh()
    _run_json(capsys, gh)
    assert any(c[:2] == ["repo", "view"] for c in gh.calls)
    assert any("acme/widgets" in " ".join(c) for c in gh.calls if c[0] == "pr")


def test_no_board_section_without_config_or_linked_project(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh()
    code = daily_update.main(["--no-write-state"], runner=gh)
    out = capsys.readouterr().out
    assert code == 0
    assert "Board:" not in out
    assert "## Next up" not in out
    assert "## In flight" in out
    assert not any("graphql" in c and "items" in " ".join(c) for c in gh.calls)


def test_board_from_skill_config(
    state_home: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = tmp_path / ".agents" / "skill-config"
    cfg.mkdir(parents=True)
    (cfg / "daily-update.md").write_text("project-owner: acme\nproject-number: 7\n")
    gh = FakeGh()
    payload = _run_json(capsys, gh)
    assert payload["board"] == {"owner": "acme", "number": 7}
    assert not any("projectsV2" in " ".join(c) for c in gh.calls)


def test_board_from_linked_project(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh()
    gh.linked_project = {"number": 3, "owner": {"login": "acme"}}
    assert _run_json(capsys, gh)["board"] == {"owner": "acme", "number": 3}


def test_missing_scope_fails_before_querying(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class NoRepo(FakeGh):
        def _answer(self, args: list[str], joined: str) -> str:
            if args[:3] == ["api", "-i", "user"]:
                return "HTTP/2.0 200 OK\nX-OAuth-Scopes: gist\n\n{}"
            return super()._answer(args, joined)

    code = daily_update.main(["--no-write-state"], runner=NoRepo())
    assert code == 1
    assert "gh auth refresh" in capsys.readouterr().err


def test_unreadable_linked_project_fails_loudly(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Denied(FakeGh):
        def __call__(self, cmd: Sequence[str], **kw: Any) -> Any:
            if "projectsV2" in " ".join(cmd):
                self.calls.append(list(cmd)[1:])
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="Resource not accessible: missing read:project scope",
                )
            return super().__call__(cmd, **kw)

    code = daily_update.main(["--no-write-state"], runner=Denied())
    captured = capsys.readouterr()
    assert code == 1
    assert "gh auth refresh -s read:project" in captured.err
    assert "## In flight" not in captured.out


def test_no_linked_project_is_a_clean_no_board(
    state_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = daily_update.main(["--no-write-state"], runner=FakeGh())
    captured = capsys.readouterr()
    assert code == 0
    assert "## In flight" in captured.out
    assert "Board:" not in captured.out
