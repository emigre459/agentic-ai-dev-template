"""Tests for ``upgrade-deps/scripts/setup_skills_update_app.py``.

The script lives outside the importable package tree, so it is loaded by path. A
fake ``gh`` on ``GH_CMD`` logs every call's argv and stdin as JSON lines and answers
from a small config file. The App's private key in the canned conversion response
holds the marker ``SECRET``: the tests check that the marker never reaches argv,
stdout, stderr, a repr, or an error message. The one-shot callback listener is
exercised on loopback only.
"""

from __future__ import annotations

import html
import importlib.util
import io
import json
import queue
import re
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
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
    / "setup_skills_update_app.py"
)

# Ceiling for a loopback round trip; the tests wait on the real event, not a sleep.
_LOOPBACK_CEILING = 10.0

PEM = "-----BEGIN RSA PRIVATE KEY-----\nSECRET\n-----END RSA PRIVATE KEY-----\n"
CONVERSION = {
    "id": 42,
    "slug": "acme-skills",
    "pem": PEM,
    "client_id": "Iv1.abc",
    "client_secret": "shh",
    "webhook_secret": None,
    "owner": {"login": "owner"},
}


def _load() -> ModuleType:
    name = "setup_skills_update_app"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


app = _load()


_FAKE_GH = r"""
import json, os, sys

args = sys.argv[1:]
stdin = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": args, "stdin": stdin}) + "\n")
cfg = json.load(open(os.environ["FAKE_CFG"]))
if args[:2] == ["auth", "status"]:
    sys.exit(0 if cfg.get("authed", True) else 1)
if args[:1] == ["api"] and args[1].startswith("repos/"):
    owner = args[1].split("/")[1]
    kind = cfg.get("owner_type") or ("Organization" if owner == "acme" else "User")
    print(json.dumps({
        "admin": cfg.get("admin", True),
        "owner_type": kind,
        "id": cfg.get("repo_id", 777),
    }))
    sys.exit(0)
if args[:1] == ["api"] and "/actions/secrets/" in " ".join(args):
    # GET orgs/<org>/actions/secrets/<NAME>, or
    # PUT orgs/<org>/actions/secrets/<NAME>/repositories/<repo_id>.
    path = args[3] if args[1:3] == ["-X", "PUT"] else args[1]
    name = path.split("/actions/secrets/")[1].split("/")[0]
    if name in cfg.get("org_secrets", []):
        print(json.dumps({"name": name}))
        sys.exit(0)
    sys.stderr.write("gh: Not Found (HTTP 404)\n")
    sys.exit(1)
if args[:2] == ["api", "user"]:
    print(cfg.get("user", "owner"))
    sys.exit(0)
if args[:3] == ["api", "-X", "POST"] and "/app-manifests/" in args[3]:
    print(json.dumps(cfg["conversion"]))
    sys.exit(0)
if args[:2] == ["secret", "set"]:
    if cfg.get("fail_secret") == args[2]:
        # Simulate a gh that echoes what it was given into its error output.
        sys.stderr.write("gh: could not set secret: " + stdin + "\n")
        print(stdin)
        sys.exit(1)
    sys.exit(0)
sys.stderr.write("fake gh: unexpected call\n")
sys.exit(2)
"""


class FakeGh:
    """A fake ``gh`` executable wired up through ``GH_CMD``."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bin_dir = root / "bin"
        bin_dir.mkdir(parents=True)
        impl = bin_dir / "gh_impl.py"
        impl.write_text(_FAKE_GH)
        wrapper = bin_dir / "gh"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        self.log = root / "gh.log"
        self.cfg = root / "gh.json"
        self.configure()
        monkeypatch.setenv("GH_CMD", str(wrapper))
        monkeypatch.setenv("FAKE_LOG", str(self.log))
        monkeypatch.setenv("FAKE_CFG", str(self.cfg))

    def configure(self, **over: Any) -> None:
        cfg: dict[str, Any] = {"conversion": CONVERSION}
        cfg.update(over)
        self.cfg.write_text(json.dumps(cfg))

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    return FakeGh(tmp_path / "fake", monkeypatch)


def _creds() -> Any:
    return app.AppCredentials(app_id=42, slug="acme-skills", pem=PEM)


ORG_SECRETS = ["SKILLS_UPDATE_APP_ID", "SKILLS_UPDATE_APP_PRIVATE_KEY"]
EXISTING = ["--repo", "acme/repo", "--org", "acme", "--existing-app", "acme-skills"]
INSTALL_URL = "https://github.com/apps/acme-skills/installations/new"


# --------------------------------------------------------------------------- #
# Pure pieces
# --------------------------------------------------------------------------- #


def test_manifest_is_private_minimal_and_webhookless() -> None:
    redirect = "http://127.0.0.1:5555/callback"
    manifest = app.build_manifest("owner/repo", redirect)
    assert manifest["default_permissions"] == {
        "contents": "write",
        "pull_requests": "write",
    }
    assert manifest["public"] is False
    assert manifest["hook_attributes"]["active"] is False
    assert manifest["hook_attributes"]["url"] == "https://github.com/owner/repo"
    assert manifest["url"] == "https://github.com/owner/repo"
    assert manifest["redirect_url"] == redirect
    assert manifest["name"] == "repo-skills-update"
    assert "default_events" not in manifest or manifest["default_events"] == []
    assert app.build_manifest("owner/repo", redirect, name="x")["name"] == "x"


def test_registration_url_user_vs_org() -> None:
    assert app.registration_url(None) == "https://github.com/settings/apps/new"
    assert (
        app.registration_url("acme")
        == "https://github.com/organizations/acme/settings/apps/new"
    )


def test_form_posts_manifest_and_state() -> None:
    manifest = app.build_manifest("owner/repo", "http://127.0.0.1:1/callback")
    manifest["name"] = 'quote " and <tag> & amp'
    action = app.registration_url(None) + "?state=st8"
    page = app.form_html(action, manifest, "st8")
    form = re.search(r"<form[^>]*>", page)
    assert form is not None
    assert 'method="post"' in form.group(0)
    assert "state=st8" in form.group(0)
    value = re.search(r'name="manifest" value="([^"]*)"', page)
    assert value is not None
    assert json.loads(html.unescape(value.group(1))) == manifest
    assert re.search(r'name="state" value="st8"', page)
    assert "submit()" in page


def test_parse_callback() -> None:
    url = "http://127.0.0.1:5555/callback?code=abc123&state=good"
    assert app.parse_callback(url, "good") == "abc123"
    assert app.parse_callback("/callback?code=abc123&state=good", "good") == "abc123"
    with pytest.raises(ValueError, match="state"):
        app.parse_callback(url, "other")
    with pytest.raises(ValueError, match="state"):
        app.parse_callback("/callback?code=abc123", "good")
    assert app.parse_callback("  abc123\n", "good", allow_bare=True) == "abc123"
    with pytest.raises(ValueError):
        app.parse_callback("abc123", "good")
    with pytest.raises(ValueError, match="code"):
        app.parse_callback("/callback?state=good", "good")
    with pytest.raises(ValueError, match="code"):
        app.parse_callback("", "good", allow_bare=True)
    # The code goes into an API path, so anything but a plain token is refused.
    with pytest.raises(ValueError, match="code"):
        app.parse_callback("/callback?code=..%2Fuser&state=good", "good")
    with pytest.raises(ValueError, match="code"):
        app.parse_callback("../user", "good", allow_bare=True)
    # A crafted non-ASCII state is refused, not a crash.
    with pytest.raises(ValueError, match="state"):
        app.parse_callback("/callback?code=abc&state=%C3%A9", "good")


def test_credentials_repr_hides_the_key() -> None:
    creds = _creds()
    assert "SECRET" not in repr(creds)
    assert "SECRET" not in str(creds)
    assert "acme-skills" in repr(creds)
    assert creds.pem == PEM


def test_write_lane_file_creates_merges_and_dedupes(tmp_path: Path) -> None:
    path = tmp_path / ".github" / "skills-update-lane.json"
    assert app.write_lane_file(path, "acme-skills[bot]") is True
    text = path.read_text()
    assert text.endswith("\n")
    assert json.loads(text) == {
        "branch": "skills-update",
        "authors": ["acme-skills[bot]"],
    }
    assert app.write_lane_file(path, "acme-skills[bot]") is False
    assert path.read_text() == text

    path.write_text(
        json.dumps({"branch": "nightly", "authors": ["someone"], "extra": 1}) + "\n"
    )
    assert app.write_lane_file(path, "acme-skills[bot]") is True
    data = json.loads(path.read_text())
    assert data == {
        "branch": "nightly",
        "authors": ["someone", "acme-skills[bot]"],
        "extra": 1,
    }
    assert path.read_text() == json.dumps(data, indent=2) + "\n"


def test_write_lane_file_refuses_a_broken_file(tmp_path: Path) -> None:
    path = tmp_path / "lane.json"
    path.write_text("[1, 2]\n")
    with pytest.raises(SystemExit, match="lane.json"):
        app.write_lane_file(path, "acme-skills[bot]")
    path.write_text("{not json")
    with pytest.raises(SystemExit, match="lane.json"):
        app.write_lane_file(path, "acme-skills[bot]")


# --------------------------------------------------------------------------- #
# The loopback listener
# --------------------------------------------------------------------------- #


def _serve_in_thread(state: str, timeout: float) -> tuple[threading.Thread, Any, Any]:
    ports: queue.Queue[int] = queue.Queue()
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["code"] = app.wait_for_callback(0, state, timeout, ready=ports.put)
        except (ValueError, TimeoutError) as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, ports, outcome


def test_wait_for_callback_returns_the_code_on_loopback() -> None:
    thread, ports, outcome = _serve_in_thread("st8", _LOOPBACK_CEILING)
    port = ports.get(timeout=_LOOPBACK_CEILING)
    # A stray request (a favicon) is answered and ignored.
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico")
    url = f"http://127.0.0.1:{port}/callback?code=abc123&state=st8"
    with urllib.request.urlopen(url, timeout=_LOOPBACK_CEILING) as resp:
        body = resp.read().decode()
    thread.join(_LOOPBACK_CEILING)
    assert outcome == {"code": "abc123"}
    assert "close this tab" in body
    assert "abc123" not in body


def test_wait_for_callback_refuses_a_wrong_state() -> None:
    thread, ports, outcome = _serve_in_thread("st8", _LOOPBACK_CEILING)
    port = ports.get(timeout=_LOOPBACK_CEILING)
    url = f"http://127.0.0.1:{port}/callback?code=abc123&state=evil"
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(url, timeout=_LOOPBACK_CEILING)
    err.value.close()
    thread.join(_LOOPBACK_CEILING)
    assert isinstance(outcome.get("error"), ValueError)


def test_wait_for_callback_times_out() -> None:
    with pytest.raises(TimeoutError):
        app.wait_for_callback(0, "st8", 0.05)


# --------------------------------------------------------------------------- #
# gh calls
# --------------------------------------------------------------------------- #


def test_convert_manifest_posts_the_code(gh: FakeGh) -> None:
    creds = app.convert_manifest("abc123")
    assert (creds.app_id, creds.slug, creds.pem) == (42, "acme-skills", PEM)
    (call,) = gh.calls()
    assert call["argv"] == ["api", "-X", "POST", "/app-manifests/abc123/conversions"]


@pytest.mark.parametrize(
    "broken",
    [
        {"id": 42, "slug": "acme-skills", "pem": None},
        {"id": 42, "slug": "acme-skills", "pem": ""},
        {"id": 42, "slug": "", "pem": PEM},
        {"id": 42, "slug": None, "pem": PEM},
        {"id": "42", "slug": "acme-skills", "pem": PEM},
        {"id": True, "slug": "acme-skills", "pem": PEM},
    ],
)
def test_convert_manifest_requires_real_fields(
    gh: FakeGh, broken: dict[str, Any]
) -> None:
    gh.configure(conversion=broken)
    with pytest.raises(SystemExit) as err:
        app.convert_manifest("abc123")
    assert "SECRET" not in str(err.value)
    assert "BEGIN" not in str(err.value)


def test_convert_manifest_failure_never_echoes_the_response(gh: FakeGh) -> None:
    gh.configure(conversion={"slug": "acme-skills", "pem": PEM})  # no id
    with pytest.raises(SystemExit) as err:
        app.convert_manifest("abc123")
    assert "SECRET" not in str(err.value)
    assert err.value.__suppress_context__ or err.value.__context__ is None


def test_set_secrets_sends_the_key_on_stdin_only(gh: FakeGh) -> None:
    app.set_secrets("owner/repo", _creds(), None)
    calls = gh.calls()
    assert all("SECRET" not in arg for call in calls for arg in call["argv"])
    by_name = {call["argv"][2]: call for call in calls}
    id_call = by_name["SKILLS_UPDATE_APP_ID"]
    key_call = by_name["SKILLS_UPDATE_APP_PRIVATE_KEY"]
    assert id_call["argv"][:3] == ["secret", "set", "SKILLS_UPDATE_APP_ID"]
    assert "--body" in id_call["argv"]
    assert id_call["argv"][id_call["argv"].index("--body") + 1] == "42"
    assert key_call["stdin"] == PEM
    assert "--body" not in key_call["argv"]
    for call in calls:
        assert call["argv"][call["argv"].index("--repo") + 1] == "owner/repo"


def test_org_secrets_are_scoped_to_the_repo(gh: FakeGh) -> None:
    app.set_secrets("acme/repo", _creds(), "acme")
    calls = gh.calls()
    assert len(calls) == 2
    for call in calls:
        argv = call["argv"]
        assert "--repo" not in argv
        tail = argv[argv.index("--org") :]
        assert tail[:6] == [
            "--org",
            "acme",
            "--visibility",
            "selected",
            "--repos",
            "repo",
        ]


def test_failed_gh_call_never_leaks_the_key(gh: FakeGh) -> None:
    gh.configure(fail_secret="SKILLS_UPDATE_APP_PRIVATE_KEY")
    with pytest.raises(SystemExit) as err:
        app.set_secrets("owner/repo", _creds(), None)
    message = str(err.value)
    assert "SECRET" not in message
    assert "secret set" in message
    assert err.value.__suppress_context__ or err.value.__context__ is None


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def _feed(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def _enter_repo(root: Path, monkeypatch: pytest.MonkeyPatch, sub: str = "") -> None:
    """Make ``root`` a git work tree and run from it (or from ``root/sub``)."""
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    where = root / sub
    where.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(where)


def test_main_refuses_without_admin(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh.configure(admin=False)
    _enter_repo(tmp_path, monkeypatch)
    assert app.main(["--repo", "owner/repo", "--no-browser"]) == 1
    assert "admin" in capsys.readouterr().err
    assert not any("app-manifests" in " ".join(c["argv"]) for c in gh.calls())


def test_main_refuses_without_gh_auth(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh.configure(authed=False)
    _enter_repo(tmp_path, monkeypatch)
    assert app.main(["--repo", "owner/repo", "--no-browser"]) == 1
    assert "gh auth login" in capsys.readouterr().err


def test_main_refuses_an_org_repo_without_org(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _enter_repo(tmp_path, monkeypatch)
    assert app.main(["--repo", "acme/repo", "--no-browser"]) == 1
    assert "--org acme" in capsys.readouterr().err
    assert app.main(["--repo", "acme/repo", "--org", "other", "--no-browser"]) == 1
    assert "acme" in capsys.readouterr().err


def test_main_refuses_org_flags_on_a_personal_repo(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _enter_repo(tmp_path, monkeypatch)
    assert app.main(["--repo", "owner/repo", "--org", "owner", "--no-browser"]) == 1
    assert "personal" in capsys.readouterr().err
    argv = ["--repo", "owner/repo", "--org", "owner", "--org-secrets", "--no-browser"]
    assert app.main(argv) == 1
    assert "personal" in capsys.readouterr().err
    assert not any("app-manifests" in " ".join(c["argv"]) for c in gh.calls())


def test_main_collaborator_on_a_personal_repo_is_told_only_the_owner_can(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh.configure(user="helper")
    _enter_repo(tmp_path, monkeypatch)
    assert app.main(["--repo", "owner/repo", "--no-browser"]) == 1
    err = capsys.readouterr().err
    assert "only owner" in err
    assert "--org" not in err
    assert not any("app-manifests" in " ".join(c["argv"]) for c in gh.calls())


def test_main_writes_the_lane_file_at_the_repo_root(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enter_repo(tmp_path, monkeypatch, sub="deep/inside")
    gh.configure(org_secrets=ORG_SECRETS)
    _feed(monkeypatch, "\n")
    assert app.main([*EXISTING, "--no-browser"]) == 0
    lane = tmp_path / ".github" / "skills-update-lane.json"
    assert json.loads(lane.read_text())["authors"] == ["acme-skills[bot]"]
    assert not (tmp_path / "deep" / "inside" / ".github").exists()


def test_main_refuses_outside_a_git_work_tree(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.chdir(outside)
    assert app.main(["--repo", "owner/repo", "--no-browser"]) == 1
    assert "git" in capsys.readouterr().err
    assert not any("app-manifests" in " ".join(c["argv"]) for c in gh.calls())


def test_main_existing_app_adds_the_repo_to_the_org_secrets(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A second repo joins the org secrets' lists; nothing replaces them."""
    _enter_repo(tmp_path, monkeypatch)
    gh.configure(org_secrets=ORG_SECRETS, repo_id=777)
    opened: list[str] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(app.webbrowser, "open", fake_open)
    _feed(monkeypatch, "\n")
    assert app.main(EXISTING) == 0
    assert opened == [INSTALL_URL]
    calls = [c["argv"] for c in gh.calls()]
    joined = [" ".join(argv) for argv in calls]
    assert not any("app-manifests" in j or "secret set" in j for j in joined)
    puts = [argv for argv in calls if argv[1:3] == ["-X", "PUT"]]
    assert puts == [
        [
            "api",
            "-X",
            "PUT",
            f"orgs/acme/actions/secrets/{name}/repositories/777",
        ]
        for name in ORG_SECRETS
    ]
    lane = json.loads((tmp_path / ".github" / "skills-update-lane.json").read_text())
    assert lane["authors"] == ["acme-skills[bot]"]
    out = capsys.readouterr().out
    assert "skills-update-lane.json" in out
    assert "acme/repo" in out and "org secrets" in out
    assert "you confirmed the install" in out
    assert "workflow_dispatch" in out or "workflow run" in out


def test_main_existing_app_refuses_a_personal_repo(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _enter_repo(tmp_path, monkeypatch)
    gh.configure(org_secrets=ORG_SECRETS)
    _feed(monkeypatch, "\n")
    argv = ["--repo", "owner/repo", "--existing-app", "acme-skills", "--no-browser"]
    assert app.main(argv) == 1
    captured = capsys.readouterr()
    assert "personal" in captured.err
    assert "its own App" in captured.err and "PAT" in captured.err
    assert INSTALL_URL not in captured.out
    joined = [" ".join(c["argv"]) for c in gh.calls()]
    assert not any("PUT" in j or "secret set" in j for j in joined)
    assert not (tmp_path / ".github").exists()


def test_main_existing_app_needs_the_matching_org(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _enter_repo(tmp_path, monkeypatch)
    gh.configure(org_secrets=ORG_SECRETS)
    argv = ["--repo", "acme/repo", "--existing-app", "acme-skills", "--no-browser"]
    assert app.main(argv) == 1
    assert "--org acme" in capsys.readouterr().err
    assert app.main([*argv, "--org", "other"]) == 1
    assert "--org acme" in capsys.readouterr().err
    assert not any("PUT" in " ".join(c["argv"]) for c in gh.calls())


def test_main_existing_app_fails_clearly_without_the_org_secrets(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _enter_repo(tmp_path, monkeypatch)
    gh.configure(org_secrets=[])
    _feed(monkeypatch, "\n")
    assert app.main([*EXISTING, "--no-browser"]) == 1
    captured = capsys.readouterr()
    assert "SKILLS_UPDATE_APP_ID" in captured.err
    assert "acme" in captured.err and "--org-secrets" in captured.err
    assert INSTALL_URL not in captured.out
    assert not (tmp_path / ".github").exists()


def test_main_existing_app_eof_at_the_install_prompt_is_not_a_confirmation(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run by an agent with no terminal, the install prompt reads end of input."""
    _enter_repo(tmp_path, monkeypatch)
    gh.configure(org_secrets=ORG_SECRETS)
    _feed(monkeypatch, "")
    assert app.main([*EXISTING, "--no-browser"]) == 0
    out = capsys.readouterr().out
    assert "you confirmed" not in out
    done = out[out.index("Done:") :]
    assert "not confirmed" in done
    assert INSTALL_URL in done
    assert "Actions tab" in done


def test_main_branch_flag_names_the_lane_branch(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enter_repo(tmp_path, monkeypatch)
    gh.configure(org_secrets=ORG_SECRETS)
    _feed(monkeypatch, "\n")
    assert app.main([*EXISTING, "--no-browser", "--branch", "nightly-skills"]) == 0
    lane = json.loads((tmp_path / ".github" / "skills-update-lane.json").read_text())
    assert lane == {"branch": "nightly-skills", "authors": ["acme-skills[bot]"]}


def test_main_refuses_a_malformed_branch(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enter_repo(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as err:
        app.main([*EXISTING, "--no-browser", "--branch", "bad branch"])
    assert err.value.code == 2


def test_main_no_lane_leaves_the_file_alone(
    gh: FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enter_repo(tmp_path, monkeypatch)
    _feed(monkeypatch, "\n")
    gh.configure(org_secrets=ORG_SECRETS)
    assert app.main([*EXISTING, "--no-lane", "--no-browser"]) == 0
    assert not (tmp_path / ".github").exists()


def _forms(out: str) -> list[Path]:
    return [Path(p) for p in re.findall(r"(\S+\.html)", out)]


def test_main_create_flow_end_to_end(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _enter_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_new_state", lambda: "fixedstate")
    redirect = "http://127.0.0.1:9/callback?code=abc123&state=fixedstate"
    _feed(monkeypatch, redirect + "\n\n")
    assert app.main(["--repo", "owner/repo", "--no-browser"]) == 0
    captured = capsys.readouterr()
    assert "SECRET" not in captured.out + captured.err
    assert "https://github.com/apps/acme-skills/installations/new" in captured.out
    assert "abc123" not in captured.out + captured.err

    calls = [c["argv"] for c in gh.calls()]
    assert ["api", "-X", "POST", "/app-manifests/abc123/conversions"] in calls
    names = [argv[2] for argv in calls if argv[:2] == ["secret", "set"]]
    assert names == ["SKILLS_UPDATE_APP_ID", "SKILLS_UPDATE_APP_PRIVATE_KEY"]
    assert all("SECRET" not in arg for argv in calls for arg in argv)

    lane = json.loads((tmp_path / ".github" / "skills-update-lane.json").read_text())
    assert lane == {"branch": "skills-update", "authors": ["acme-skills[bot]"]}
    # Nothing in the working tree holds the key.
    for path in tmp_path.rglob("*"):
        if path.is_file() and "fake" not in path.parts:
            assert "SECRET" not in path.read_text(errors="replace")


def test_main_create_flow_refuses_a_pasted_wrong_state(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _enter_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_new_state", lambda: "fixedstate")
    _feed(monkeypatch, "http://127.0.0.1:9/callback?code=abc123&state=evil\n")
    assert app.main(["--repo", "owner/repo", "--no-browser"]) == 1
    assert "state" in capsys.readouterr().err
    assert not any("app-manifests" in " ".join(c["argv"]) for c in gh.calls())


def test_main_create_flow_through_the_listener(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The browser path: the opened form's redirect lands on the local listener."""
    _enter_repo(tmp_path, monkeypatch)
    gh.configure(user="acme")
    opened: list[str] = []
    pages: list[str] = []
    replies: queue.Queue[str] = queue.Queue()

    def fake_open(url: str) -> bool:
        opened.append(url)
        if url.startswith("file:"):
            page = Path(urllib.parse.urlsplit(url).path).read_text()
            pages.append(page)
            action = re.search(r'<form[^>]*action="([^"]*)"', page)
            value = re.search(r'name="manifest" value="([^"]*)"', page)
            assert action and value
            state = urllib.parse.parse_qs(
                urllib.parse.urlsplit(html.unescape(action.group(1))).query
            )["state"][0]
            redirect = json.loads(html.unescape(value.group(1)))["redirect_url"]
            target = f"{redirect}?code=abc123&state={state}"

            def hit() -> None:
                with urllib.request.urlopen(target, timeout=_LOOPBACK_CEILING) as r:
                    replies.put(r.read().decode())

            threading.Thread(target=hit).start()
        return True

    monkeypatch.setattr(app.webbrowser, "open", fake_open)
    _feed(monkeypatch, "\n")
    argv = ["--repo", "acme/repo", "--org", "acme", "--org-secrets"]
    assert app.main(argv) == 0
    assert "close this tab" in replies.get(timeout=_LOOPBACK_CEILING)
    assert opened[0].startswith("file:")
    assert "organizations/acme/settings/apps/new?state=" in pages[0]
    # The form (state included) is cleaned up once the code is in.
    assert not Path(urllib.parse.urlsplit(opened[0]).path).exists()
    assert opened[1] == "https://github.com/apps/acme-skills/installations/new"
    calls = [c["argv"] for c in gh.calls()]
    assert ["api", "-X", "POST", "/app-manifests/abc123/conversions"] in calls
    for argv_ in calls:
        if argv_[:2] == ["secret", "set"]:
            assert "--org" in argv_ and "--repos" in argv_
    captured = capsys.readouterr()
    assert "SECRET" not in captured.out + captured.err
    # The listener doesn't log the request line: it carries the one-time code.
    assert "abc123" not in captured.out + captured.err


def test_main_secret_failure_explains_recovery_without_the_key(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _enter_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_new_state", lambda: "fixedstate")
    gh.configure(fail_secret="SKILLS_UPDATE_APP_PRIVATE_KEY")
    _feed(monkeypatch, "http://127.0.0.1:9/callback?code=abc123&state=fixedstate\n")
    assert app.main(["--repo", "owner/repo", "--no-browser"]) == 1
    captured = capsys.readouterr()
    assert "SECRET" not in captured.out + captured.err
    assert "new private key" in captured.err
    assert "https://github.com/settings/apps/acme-skills" in captured.err
    assert "--repo owner/repo < the-key.pem" in captured.err
    # --existing-app needs org secrets, so a personal repo finishes by hand.
    assert "--existing-app" not in captured.err
    assert INSTALL_URL in captured.err
    assert "skills-update-lane.json" in captured.err
    assert not (tmp_path / ".github").exists()


def test_main_org_secret_failure_shows_the_org_scoped_recovery(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _enter_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_new_state", lambda: "fixedstate")
    gh.configure(fail_secret="SKILLS_UPDATE_APP_PRIVATE_KEY")
    _feed(monkeypatch, "http://127.0.0.1:9/callback?code=abc123&state=fixedstate\n")
    argv = ["--repo", "acme/repo", "--org", "acme", "--org-secrets", "--no-browser"]
    assert app.main(argv) == 1
    err = capsys.readouterr().err
    assert "SECRET" not in err
    assert "https://github.com/organizations/acme/settings/apps/acme-skills" in err
    assert (
        "gh secret set SKILLS_UPDATE_APP_PRIVATE_KEY --org acme --visibility "
        "selected --repos repo < the-key.pem"
    ) in err
    assert "--repo acme/repo <" not in err
    assert "--existing-app acme-skills --org acme" in err


def test_main_org_secrets_refuses_when_the_org_already_has_them(
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An existing org secret belongs to an App already; never overwrite it."""
    _enter_repo(tmp_path, monkeypatch)
    gh.configure(org_secrets=["SKILLS_UPDATE_APP_ID"])
    opened: list[str] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        return True

    def no_listener(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("the listener must not start")

    monkeypatch.setattr(app.webbrowser, "open", fake_open)
    monkeypatch.setattr(app, "wait_for_callback", no_listener)
    _feed(monkeypatch, "")
    argv = ["--repo", "acme/repo", "--org", "acme", "--org-secrets"]
    assert app.main(argv) == 1
    err = capsys.readouterr().err
    assert "SKILLS_UPDATE_APP_ID" in err and "--existing-app" in err
    assert opened == []
    joined = [" ".join(c["argv"]) for c in gh.calls()]
    assert not any("app-manifests" in j or "secret set" in j for j in joined)
