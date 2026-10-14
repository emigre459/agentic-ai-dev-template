import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.preflight_init import repo_from_remote, run_preflight


def test_repo_from_remote_supports_https_and_ssh() -> None:
    """Normalize common GitHub remote URL forms."""
    assert repo_from_remote("https://github.com/acme/project.git") == "acme/project"
    assert repo_from_remote("git@github.com:acme/project.git") == "acme/project"


def test_run_preflight_reports_confirmed_target(tmp_path: Path) -> None:
    """Return the exact account and repository after every guard passes."""

    def fake_which(command: str) -> str:
        """Pretend every required executable is available."""
        return f"/tools/{command}"

    def fake_runner(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Return deterministic Git and GitHub preflight responses."""
        if cmd[:2] == ["git", "status"]:
            return SimpleNamespace(stdout="")
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(stdout="https://github.com/acme/project.git\n")
        if cmd == ["gh", "auth", "status"]:
            return SimpleNamespace(stdout="authenticated")
        if cmd == ["gh", "api", "user"]:
            return SimpleNamespace(stdout=json.dumps({"login": "builder"}))
        if cmd == ["gh", "api", "repos/acme/project"]:
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "full_name": "acme/project",
                        "has_issues": True,
                        "permissions": {"admin": True},
                    }
                )
            )
        raise AssertionError(f"unexpected command: {cmd}")

    result = run_preflight(
        tmp_path,
        "acme/project",
        "python",
        runner=fake_runner,
        which=fake_which,
    )

    assert result == {"account": "builder", "repo": "acme/project", "stack": "python"}


def test_run_preflight_rejects_origin_target_mismatch(tmp_path: Path) -> None:
    """Stop before mutation when origin is not the confirmed target repository."""

    def fake_which(command: str) -> str:
        """Pretend every required executable is available."""
        return f"/tools/{command}"

    def fake_runner(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Report an upstream origin instead of the requested fork."""
        if cmd[:2] == ["git", "status"]:
            return SimpleNamespace(stdout="")
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(stdout="https://github.com/upstream/template.git\n")
        if cmd == ["gh", "auth", "status"]:
            return SimpleNamespace(stdout="authenticated")
        raise AssertionError(f"unexpected command: {cmd}")

    with pytest.raises(RuntimeError, match="origin.*upstream/template.*acme/project"):
        run_preflight(
            tmp_path,
            "acme/project",
            "python",
            runner=fake_runner,
            which=fake_which,
        )


@pytest.mark.parametrize(
    ("repo_data", "message"),
    [
        (
            {"full_name": "acme/project", "has_issues": True, "permissions": {}},
            "administrator permission",
        ),
        (
            {
                "full_name": "acme/project",
                "has_issues": False,
                "permissions": {"admin": True},
            },
            "Issues are disabled",
        ),
    ],
)
def test_run_preflight_rejects_unusable_repository(
    tmp_path: Path,
    repo_data: dict,
    message: str,
) -> None:
    """Stop before mutation when repository settings cannot be completed."""

    def fake_which(command: str) -> str:
        """Pretend every required executable is available."""
        return f"/tools/{command}"

    def fake_runner(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Return repository state supplied by the test case."""
        if cmd[:2] == ["git", "status"]:
            return SimpleNamespace(stdout="")
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(stdout="https://github.com/acme/project.git\n")
        if cmd == ["gh", "auth", "status"]:
            return SimpleNamespace(stdout="authenticated")
        if cmd == ["gh", "api", "user"]:
            return SimpleNamespace(stdout=json.dumps({"login": "builder"}))
        if cmd == ["gh", "api", "repos/acme/project"]:
            return SimpleNamespace(stdout=json.dumps(repo_data))
        raise AssertionError(f"unexpected command: {cmd}")

    with pytest.raises(RuntimeError, match=message):
        run_preflight(
            tmp_path,
            "acme/project",
            "python",
            runner=fake_runner,
            which=fake_which,
        )
