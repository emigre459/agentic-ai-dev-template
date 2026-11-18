#!/usr/bin/env python3
"""Create the repo owner's GitHub App for the nightly skills update, in two clicks.

The nightly ``skills update`` workflow needs an App (``SKILLS_UPDATE_APP_ID`` +
``SKILLS_UPDATE_APP_PRIVATE_KEY``) or a PAT (``SKILLS_UPDATE_TOKEN``) and fails
without one. This script sets up the App through GitHub's manifest flow ("Registering
a GitHub App from a manifest"):

1. build a private, webhook-less manifest asking only for ``contents`` and
   ``pull_requests`` write;
2. open an auto-submitting form that posts it to GitHub; **the person clicks
   Create**;
3. take GitHub's redirect on a one-shot ``127.0.0.1`` listener (or a pasted URL with
   ``--no-browser``), checking ``state``;
4. trade the code for the App's id, slug and private key
   (``gh api -X POST /app-manifests/<code>/conversions``);
5. store the id and key as Actions secrets, the key on ``gh``'s stdin only;
6. open the App's install page; **the person clicks Install**;
7. add ``<slug>[bot]`` to ``.github/skills-update-lane.json`` so the review gate's
   skills-update lane accepts the App's PRs.

``--existing-app SLUG`` runs only steps 6 and 7, for an org whose App and org secrets
already exist. A private App installs only on the account that owns it, so an
org-owned repo needs ``--org <org>``.

The private key never lands in argv, on disk, in output, or in an error message. Run
it from the repo root with ``python3`` (stdlib only); ``gh`` must be logged in with
admin on the repo. ``GH_CMD`` overrides the ``gh`` executable (the test seam). Exit
0 on success, 1 on any refusal or failure, with the reason on stderr.
"""

from __future__ import annotations

import argparse
import hmac
import html
import http.server
import json
import os
import re
import secrets
import socket
import subprocess  # nosec B404 -- fixed gh argv, no shell
import sys
import tempfile
import time
import urllib.parse
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

DEFAULT_PERMISSIONS = {"contents": "write", "pull_requests": "write"}
LANE_FILE = Path(".github") / "skills-update-lane.json"
LANE_BRANCH = "skills-update"
APP_ID_SECRET_NAME = "SKILLS_UPDATE_APP_ID"
APP_KEY_SECRET_NAME = "SKILLS_UPDATE_APP_PRIVATE_KEY"
CALLBACK_PATH = "/callback"
# How long the listener waits for the person to review the page and click Create
# before falling back to a pasted redirect URL.
LISTEN_TIMEOUT = 600.0

_CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [^-]+-----.*?(?:-----END [^-]+-----|\Z)", re.DOTALL
)
_CLOSE_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Skills update App</title>"
    "<p>GitHub sent the App back to the setup script. You can close this tab.</p>"
)
_REFUSED_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Skills update App</title>"
    "<p>The setup script refused this redirect. See the terminal.</p>"
)


class SetupError(SystemExit):
    """A refusal or failure the script reports and exits 1 on.

    It is a ``SystemExit`` so a helper used on its own still ends the process with
    the message; ``main`` catches it and returns 1. Messages never hold the key.
    """


class AppCredentials(NamedTuple):
    """What the manifest conversion returns that the setup keeps.

    Attributes
    ----------
    app_id : int
        The App's numeric id (``SKILLS_UPDATE_APP_ID``).
    slug : str
        The App's URL slug; its bot login is ``<slug>[bot]``.
    pem : str
        The App's private key. Never printed: ``repr`` hides it.
    """

    app_id: int
    slug: str
    pem: str

    def __repr__(self) -> str:
        """Show the id and slug, never the key."""
        return (
            f"AppCredentials(app_id={self.app_id!r}, slug={self.slug!r}, pem=<hidden>)"
        )


def _gh_cmd() -> str:
    return os.environ.get("GH_CMD") or "gh"


def _redact(text: str, secret: str | None) -> str:
    """Strip any PEM block, and every line of ``secret``, out of ``text``."""
    text = _PEM_BLOCK_RE.sub("[redacted key]", text)
    if secret:
        for line in secret.splitlines():
            line = line.strip()
            if line and not line.startswith("-----"):
                text = text.replace(line, "[redacted]")
    return text


def _gh(
    args: Sequence[str], *, stdin_text: str | None = None, secret: str | None = None
) -> str:
    """Run ``gh`` and return its stdout, or raise ``SetupError``.

    Parameters
    ----------
    args : Sequence[str]
        The ``gh`` arguments. They must never hold a secret.
    stdin_text : str | None
        Text for ``gh``'s stdin (the only way a secret reaches it).
    secret : str | None
        A value scrubbed out of the error message if ``gh`` echoes it.

    Returns
    -------
    str
        ``gh``'s stdout.

    Raises
    ------
    SetupError
        ``gh`` is missing or exits non-zero. The message holds the command and
        ``gh``'s redacted stderr, never its stdout.
    """
    shown = " ".join(args)
    try:
        proc = subprocess.run(  # nosec B603 -- fixed gh argv, no shell
            [_gh_cmd(), *args],
            input=stdin_text,
            stdin=None if stdin_text is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise SetupError(
            "gh is not installed or not on PATH: https://cli.github.com"
        ) from None
    if proc.returncode != 0:
        detail = _redact(proc.stderr.strip(), secret)
        raise SetupError(f"`gh {shown}` failed (exit {proc.returncode}): {detail}")
    return proc.stdout


def build_manifest(
    repo: str, redirect_url: str, name: str | None = None
) -> dict[str, Any]:
    """Build the App manifest: private, no webhook, two write permissions.

    Parameters
    ----------
    repo : str
        ``owner/name`` of the repo the App serves.
    redirect_url : str
        Where GitHub sends the browser, with ``?code=…&state=…``, after Create.
    name : str | None
        The App's name; defaults to ``<repo-name>-skills-update``. The person can
        edit it on GitHub's page (names are unique across GitHub).

    Returns
    -------
    dict[str, Any]
        The manifest to post as JSON.
    """
    repo_url = f"https://github.com/{repo}"
    return {
        "name": name or f"{repo.split('/')[1]}-skills-update",
        "url": repo_url,
        "hook_attributes": {"url": repo_url, "active": False},
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": dict(DEFAULT_PERMISSIONS),
        "default_events": [],
    }


def registration_url(org: str | None) -> str:
    """Return the page that registers an App from a manifest, for a user or an org."""
    if org:
        return f"https://github.com/organizations/{org}/settings/apps/new"
    return "https://github.com/settings/apps/new"


def form_html(action: str, manifest: Mapping[str, Any], state: str) -> str:
    """Return a page that posts ``manifest`` and ``state`` to ``action`` on load.

    Parameters
    ----------
    action : str
        The registration URL, with ``?state=<state>`` appended.
    manifest : Mapping[str, Any]
        The App manifest; sent as JSON in the hidden ``manifest`` field.
    state : str
        The anti-forgery value GitHub echoes back on the redirect.

    Returns
    -------
    str
        A self-contained HTML page; a visible button covers a blocked auto-submit.
    """
    value = html.escape(json.dumps(manifest), quote=True)
    return (
        "<!doctype html>\n<html><head><meta charset=utf-8>"
        "<title>Create the skills update App</title></head>\n"
        '<body onload="document.forms[0].submit()">\n'
        f'<form method="post" action="{html.escape(action, quote=True)}">\n'
        f'<input type="hidden" name="manifest" value="{value}">\n'
        f'<input type="hidden" name="state" value="{html.escape(state, quote=True)}">\n'
        "<p>Sending you to GitHub to create the App...</p>\n"
        '<button type="submit">Continue to GitHub</button>\n'
        "</form>\n</body></html>\n"
    )


def parse_callback(text: str, expected_state: str, *, allow_bare: bool = False) -> str:
    """Return the manifest code from GitHub's redirect, checking ``state``.

    Parameters
    ----------
    text : str
        A full redirect URL, a path such as ``/callback?code=X&state=S``, or (with
        ``allow_bare``) the bare code.
    expected_state : str
        The ``state`` this run sent; a redirect carrying any other is refused.
    allow_bare : bool
        Accept a bare code, skipping the state check. Only for a code the person
        pasted themselves; the listener never allows it.

    Returns
    -------
    str
        The code to convert.

    Raises
    ------
    ValueError
        The state is missing or wrong, or there is no well-formed code.
    """
    text = text.strip()
    if "?" in text or "=" in text:
        query = urllib.parse.urlsplit(text).query if "?" in text else text
        params = urllib.parse.parse_qs(query)
        code = params.get("code", [""])[0]
        state = params.get("state", [""])[0]
        if not code:
            raise ValueError("the redirect carries no code")
        if not _CODE_RE.match(code):
            raise ValueError("the redirect's code is malformed")
        if not state or not hmac.compare_digest(
            state.encode(), expected_state.encode()
        ):
            raise ValueError("the redirect's state doesn't match this run; refused")
        return code
    if not text:
        raise ValueError("no code was given")
    if not allow_bare:
        raise ValueError("expected a redirect URL with code and state")
    if not _CODE_RE.match(text):
        raise ValueError("that code is malformed")
    return text


def wait_for_callback(
    port: int,
    expected_state: str,
    timeout: float,
    *,
    ready: Callable[[int], None] | None = None,
) -> str:
    """Serve one GitHub redirect on ``127.0.0.1`` and return its code.

    Parameters
    ----------
    port : int
        Port to bind; 0 picks a free one.
    expected_state : str
        The ``state`` this run sent.
    timeout : float
        Seconds to wait for the redirect.
    ready : Callable[[int], None] | None
        Called with the bound port once the listener accepts connections, before
        waiting (the caller builds the redirect URL and opens the browser here).

    Returns
    -------
    str
        The code from the redirect.

    Raises
    ------
    ValueError
        The redirect's state or code is refused (see ``parse_callback``).
    TimeoutError
        No redirect arrived in time.
    """
    outcome: dict[str, Any] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if urllib.parse.urlsplit(self.path).path != CALLBACK_PATH:
                self.send_error(404)
                return
            try:
                outcome["code"] = parse_callback(self.path, expected_state)
            except ValueError as exc:
                outcome["error"] = exc
                self._reply(400, _REFUSED_PAGE)
                return
            self._reply(200, _CLOSE_PAGE)

        def _reply(self, status: int, page: str) -> None:
            body = page.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            """Stay quiet: the request line carries the one-time code."""

    with http.server.HTTPServer(("127.0.0.1", port), Handler) as server:
        if ready is not None:
            ready(server.server_address[1])
        deadline = time.monotonic() + timeout
        while not outcome:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no redirect from GitHub arrived in time")
            server.timeout = remaining
            server.handle_request()
    if "error" in outcome:
        raise outcome["error"]
    return str(outcome["code"])


def convert_manifest(code: str) -> AppCredentials:
    """Trade the manifest code for the new App's id, slug and private key.

    Parameters
    ----------
    code : str
        The code from GitHub's redirect (valid for one hour, once).

    Returns
    -------
    AppCredentials
        The id, slug and key. The rest of the response (client secret etc.) is
        dropped.

    Raises
    ------
    SetupError
        The code is malformed, ``gh`` fails, or the response lacks a field. The
        response itself is never echoed.
    """
    if not _CODE_RE.match(code):
        raise SetupError("the manifest code is malformed")
    out = _gh(["api", "-X", "POST", f"/app-manifests/{code}/conversions"])
    try:
        data = json.loads(out)
        return AppCredentials(
            app_id=int(data["id"]), slug=str(data["slug"]), pem=str(data["pem"])
        )
    except (ValueError, KeyError, TypeError):
        raise SetupError(
            "GitHub's manifest conversion response was missing the App's id, slug "
            "or key (response not shown: it holds the private key)"
        ) from None


def set_secrets(repo: str, creds: AppCredentials, org_secrets_org: str | None) -> None:
    """Store the App id and key as Actions secrets; the key goes on stdin only.

    Parameters
    ----------
    repo : str
        ``owner/name``.
    creds : AppCredentials
        The App's id and key.
    org_secrets_org : str | None
        Set them as org secrets of this org, visible to ``repo`` only, instead of
        repo secrets.

    Raises
    ------
    SetupError
        ``gh secret set`` failed; the message never holds the key.
    """
    if org_secrets_org:
        scope = [
            "--org",
            org_secrets_org,
            "--visibility",
            "selected",
            "--repos",
            repo.split("/")[1],
        ]
    else:
        scope = ["--repo", repo]
    _gh(["secret", "set", APP_ID_SECRET_NAME, "--body", str(creds.app_id), *scope])
    _gh(
        ["secret", "set", APP_KEY_SECRET_NAME, *scope],
        stdin_text=creds.pem,
        secret=creds.pem,
    )


def write_lane_file(path: Path, author: str, branch: str = LANE_BRANCH) -> bool:
    """Create the skills-update lane file, or add ``author`` to an existing one.

    Parameters
    ----------
    path : Path
        Usually ``.github/skills-update-lane.json``.
    author : str
        The update PR's author login, ``<slug>[bot]`` for an App.
    branch : str
        The update branch, used only when the file doesn't name one.

    Returns
    -------
    bool
        Whether the file changed.

    Raises
    ------
    SetupError
        The existing file isn't a JSON object with an ``authors`` list.
    """
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
        except ValueError:
            raise SetupError(f"{path} is not valid JSON; fix or remove it") from None
        if not isinstance(loaded, dict) or not isinstance(
            loaded.get("authors", []), list
        ):
            raise SetupError(
                f'{path} must be a JSON object like {{"branch": ..., "authors": [...]}}'
            )
        data = loaded
    before = json.dumps(data, sort_keys=True)
    data.setdefault("branch", branch)
    authors = data.setdefault("authors", [])
    if author not in authors:
        authors.append(author)
    if path.exists() and json.dumps(data, sort_keys=True) == before:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def _new_state() -> str:
    return secrets.token_urlsafe(24)


def _free_port() -> int:
    """Return a loopback port nothing listens on right now (for the paste path)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _ask(prompt: str) -> str:
    """Print ``prompt`` and read one line from stdin ("" at end of input)."""
    print(prompt, end="", flush=True)
    return sys.stdin.readline()


def _show(target: str, what: str, no_browser: bool) -> None:
    """Open ``target`` in the browser, or print it when that's off or fails."""
    if no_browser or not webbrowser.open(target):
        print(f"Open {what}: {target}")
    else:
        print(f"Opened {what} in your browser: {target}")


def _preflight(repo: str) -> None:
    """Refuse unless ``gh`` is logged in with admin on ``repo``."""
    try:
        _gh(["auth", "status"])
    except SetupError:
        raise SetupError("gh is not logged in: run `gh auth login` first") from None
    admin = _gh(["api", f"repos/{repo}", "--jq", ".permissions.admin"]).strip()
    if admin != "true":
        raise SetupError(
            f"you need admin on {repo} to set its Actions secrets "
            "(ask an admin to run this, or to grant you admin)"
        )


def _check_owner(repo: str, org: str | None) -> None:
    """Refuse an App the repo couldn't install: a private App stays on its owner."""
    owner = repo.split("/")[0]
    if org:
        if org.lower() != owner.lower():
            raise SetupError(
                f"--org {org} doesn't own {repo}; a private App can only be "
                f"installed on its owner's repos (use --org {owner})"
            )
        return
    login = _gh(["api", "user", "--jq", ".login"]).strip()
    if login.lower() != owner.lower():
        raise SetupError(
            f"{repo} belongs to {owner}, not you ({login}): pass --org {owner} so "
            "the App is owned by the org (a private App only installs on its owner)"
        )


def _create_app(args: argparse.Namespace) -> AppCredentials:
    """Run steps 1-4: post the manifest, take the redirect, convert the code."""
    state = _new_state()
    action = registration_url(args.org) + "?state=" + state
    with tempfile.TemporaryDirectory(prefix="skills-update-app-") as tmp:
        form = Path(tmp) / "create-app.html"

        def publish(port: int) -> None:
            redirect = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
            manifest = build_manifest(args.repo, redirect, args.name)
            form.write_text(form_html(action, manifest, state))
            _show(form.as_uri(), "the App form", args.no_browser)
            print(
                "On GitHub's page, check the name (it must be unique on GitHub) and "
                "click Create."
            )

        code: str | None = None
        try:
            if not args.no_browser:
                try:
                    code = wait_for_callback(
                        args.port, state, LISTEN_TIMEOUT, ready=publish
                    )
                except TimeoutError:
                    print("No redirect reached the local listener.")
            else:
                publish(args.port or _free_port())
            if code is None:
                print(
                    "After you click Create, GitHub sends your browser to "
                    f"http://127.0.0.1:<port>{CALLBACK_PATH}?code=...; that page may "
                    "not load. Copy the full address from the address bar."
                )
                pasted = _ask("Paste the redirect URL (or just the code): ")
                code = parse_callback(pasted, state, allow_bare=True)
        except ValueError as exc:
            raise SetupError(str(exc)) from None
    print("Converting the manifest into the App...")
    return convert_manifest(code)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create (or install) the GitHub App the nightly skills update runs as, "
            "store its secrets, and add its bot to the review gate's lane file."
        )
    )
    parser.add_argument("--repo", required=True, help="owner/name of the repo")
    parser.add_argument(
        "--org", help="create the App under this org (required for org-owned repos)"
    )
    parser.add_argument(
        "--org-secrets",
        action="store_true",
        help="set the secrets once as org secrets visible to --repo (needs --org)",
    )
    parser.add_argument(
        "--existing-app",
        metavar="SLUG",
        help="the App already exists (with org secrets): only install it and "
        "write the lane file",
    )
    parser.add_argument(
        "--no-lane", action="store_true", help="don't write the lane file"
    )
    parser.add_argument("--name", help="App name (default: <repo-name>-skills-update)")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="local port for GitHub's redirect (default: any free port)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the pages to open, and read the pasted redirect URL from stdin",
    )
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    owner, _, name = args.repo.partition("/")
    if not (owner and _NAME_RE.match(owner) and name and _NAME_RE.match(name)):
        parser.error("--repo must be owner/name")
    if args.org_secrets and not args.org:
        parser.error("--org-secrets needs --org")
    if args.existing_app and not _NAME_RE.match(args.existing_app):
        parser.error("--existing-app must be the App's slug")
    if args.org and not _NAME_RE.match(args.org):
        parser.error("--org must be an org login")


def _run(args: argparse.Namespace) -> None:
    repo: str = args.repo
    _preflight(repo)
    done: list[str] = []
    if args.existing_app:
        slug = args.existing_app
    else:
        _check_owner(repo, args.org)
        creds = _create_app(args)
        slug = creds.slug
        done.append(f"Created the App {slug} (id {creds.app_id}).")
        try:
            set_secrets(repo, creds, args.org if args.org_secrets else None)
        except SetupError as exc:
            settings = (
                f"https://github.com/organizations/{args.org}/settings/apps/{slug}"
                if args.org
                else f"https://github.com/settings/apps/{slug}"
            )
            raise SetupError(
                f"{exc}\nThe App {slug} (id {creds.app_id}) was created, but its key "
                "was not stored and this script keeps no copy. Generate a new private "
                f"key at {settings}, set {APP_ID_SECRET_NAME} to {creds.app_id} and "
                f"{APP_KEY_SECRET_NAME} to the key (gh secret set "
                f"{APP_KEY_SECRET_NAME} --repo {repo} < the-key.pem), delete the "
                f"downloaded key, then rerun with --existing-app {slug}."
            ) from None
        where = f"org {args.org}, visible to {repo}" if args.org_secrets else repo
        done.append(f"Set {APP_ID_SECRET_NAME} and {APP_KEY_SECRET_NAME} on {where}.")
    install = f"https://github.com/apps/{slug}/installations/new"
    _show(install, "the App's install page", args.no_browser)
    _ask(f"Install it on {repo} only, then press Enter here: ")
    done.append(f"Install page opened for {repo} (you confirmed the install).")
    author = f"{slug}[bot]"
    lane_changed = False
    if not args.no_lane:
        lane_changed = write_lane_file(Path.cwd() / LANE_FILE, author)
        state = "added" if lane_changed else "already lists"
        done.append(f"{LANE_FILE} {state} {author}.")
    print("\nDone:")
    for line in done:
        print(f"  - {line}")
    print("Next:")
    if lane_changed:
        print(
            f"  - Commit {LANE_FILE} and merge it to the default branch: the review "
            "gate reads it from there."
        )
    elif args.no_lane:
        print(
            f'  - Add "{author}" to the authors in {LANE_FILE} on the default branch '
            "so the review gate's skills-update lane accepts its PRs."
        )
    print(
        '  - Prove the install with a workflow_dispatch run of "skills update": '
        f"gh workflow run skills-update.yml --repo {repo}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the setup; return 0 on success, 1 on a refusal or failure.

    Parameters
    ----------
    argv : Sequence[str] | None
        Command-line arguments; ``sys.argv[1:]`` when None.

    Returns
    -------
    int
        The exit code.
    """
    parser = _parser()
    args = parser.parse_args(argv)
    _validate(parser, args)
    try:
        _run(args)
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
