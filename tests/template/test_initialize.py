from pathlib import Path

import pytest

from scripts.init_template import ensure_clean_worktree, initialize


def test_initialize_python_promotes_and_prunes(template_tree: Path) -> None:
    initialize(
        template_tree,
        "python",
        "acme-svc",
        "A backend service",
        apply_settings=False,
    )
    assert not (template_tree / "stacks").exists()
    assert (template_tree / "pyproject.toml").exists()
    assert (template_tree / "src" / "acme_svc" / "greeting.py").exists()
    assert not (template_tree / "src" / "example_app").exists()
    assert (template_tree / "Makefile").exists()
    # other stack's rules gone, chosen + shared kept
    assert not (template_tree / ".agents" / "rules" / "react").exists()
    assert (template_tree / ".agents" / "rules" / "python").exists()
    assert (template_tree / ".agents" / "rules" / "shared").exists()
    # machinery self-destructed
    assert not (template_tree / "scripts" / "init_template.py").exists()
    assert not (template_tree / "scripts" / "preflight_init.py").exists()
    assert not (template_tree / "tests" / "template").exists()
    # AGENTS.md collapsed + filled
    agents = (template_tree / "AGENTS.md").read_text()
    assert "acme-svc" in agents
    assert "STACK:react" not in agents and "STACK:python" not in agents
    assert "bun-react" not in agents
    # ci.yml single-stack, and the stacks/ working-directory is rewritten to root
    # (otherwise the spawned repo's first CI run fails — stacks/ no longer exists)
    ci = (template_tree / ".github" / "workflows" / "ci.yml").read_text()
    assert "react" not in ci and "template machinery" not in ci
    assert "STACK:" not in ci
    assert "stacks/" not in ci
    assert "working-directory: ." in ci
    # parallel jobs with the stack prefix stripped
    assert "  lint:\n" in ci and "  tests:\n" in ci and "  security:\n" in ci
    assert "python-lint" not in ci
    # exactly ONE uv dependabot entry survives (the stack's, rewritten to "/");
    # the template-tooling entry must be stripped or dependabot rejects the
    # duplicate ecosystem+directory pair
    dependabot = (template_tree / ".github" / "dependabot.yml").read_text()
    assert dependabot.count('package-ecosystem: "uv"') == 1
    assert 'directory: "/"' in dependabot
    assert "stacks/" not in dependabot
    # README interview stripped
    readme = (template_tree / "README.md").read_text()
    assert "INTERVIEW:start" not in readme
    # apply_repo_settings kept
    assert (template_tree / "scripts" / "apply_repo_settings.py").exists()
    assert (template_tree / ".github" / "repo-settings" / "ruleset.json").exists()
    # Manifest + lockfile name substituted (no placeholder/token residue)
    pyproject = (template_tree / "pyproject.toml").read_text()
    assert "acme-svc" in pyproject
    assert "template-placeholder-project-name" not in pyproject
    assert 'module-name = "acme_svc"' in pyproject
    assert "{{" not in pyproject
    tests = (template_tree / "tests" / "test_greeting.py").read_text()
    assert "from acme_svc.greeting import greet" in tests
    assert "example_app" not in tests
    assert (
        "template-placeholder-project-name"
        not in (template_tree / "uv.lock").read_text()
    )


def test_initialize_react_removes_root_python_tooling(template_tree: Path) -> None:
    initialize(
        template_tree,
        "react",
        "acme-web",
        "A frontend app",
        apply_settings=False,
    )
    assert not (template_tree / "stacks").exists()
    assert (template_tree / "package.json").exists()
    assert (template_tree / "biome.json").exists()
    # root template-tooling pyproject removed (react brought none)
    assert not (template_tree / "pyproject.toml").exists()
    assert not (template_tree / ".agents" / "rules" / "python").exists()
    pkg = (template_tree / "package.json").read_text()
    assert "acme-web" in pkg
    assert "template-placeholder-project-name" not in pkg
    assert (
        "template-placeholder-project-name"
        not in (template_tree / "bun.lock").read_text()
    )
    # ci.yml workdir rewritten to root (stacks/ is gone post-init)
    ci = (template_tree / ".github" / "workflows" / "ci.yml").read_text()
    assert "stacks/" not in ci
    assert "working-directory: ." in ci
    assert "  lint:\n" in ci and "  tests:\n" in ci and "  security:\n" in ci
    assert "react-lint" not in ci
    # no uv dependabot entry survives — react removes the root pyproject.toml,
    # so a leftover uv entry would scan for a manifest that doesn't exist
    dependabot = (template_tree / ".github" / "dependabot.yml").read_text()
    assert 'package-ecosystem: "uv"' not in dependabot
    assert 'package-ecosystem: "bun"' in dependabot


def test_initialize_rewrites_required_status_checks(template_tree: Path) -> None:
    import json

    initialize(template_tree, "react", "acme-web", "app", apply_settings=False)
    ruleset = json.loads(
        (template_tree / ".github" / "repo-settings" / "ruleset.json").read_text()
    )
    checks_rules = [
        r for r in ruleset["rules"] if r["type"] == "required_status_checks"
    ]
    assert len(checks_rules) == 1
    contexts = {
        c["context"] for c in checks_rules[0]["parameters"]["required_status_checks"]
    }
    # the shipped rule requires the TEMPLATE's own contexts (python-lint, ...);
    # init must rewrite them to the post-init job ids exactly, or merges gate
    # forever on contexts that never report
    assert contexts == {"lint", "tests", "security"}
    # pre-existing rules are preserved
    assert any(r["type"] == "pull_request" for r in ruleset["rules"])


def test_initialize_keeps_machinery_when_settings_apply_fails(
    template_tree: Path,
) -> None:
    # If the settings-apply step fails or is declined, init must NOT have already
    # deleted its own machinery — otherwise the tree is collapsed with no recourse.
    def failing_runner(*args: object, **kwargs: object) -> object:
        raise RuntimeError("gh apply declined/failed")

    with pytest.raises(RuntimeError):
        initialize(
            template_tree,
            "python",
            "acme-svc",
            "svc",
            apply_settings=True,
            repo="acme/repo",
            runner=failing_runner,
        )
    # apply runs BEFORE self-destruct, so the init machinery survives a failed apply
    assert (template_tree / "scripts" / "init_template.py").exists()
    assert (template_tree / "scripts" / "apply_repo_settings.py").exists()


def test_initialize_escapes_special_chars_in_python_manifest(
    template_tree: Path,
) -> None:
    # A description with " and \ must be escaped so pyproject.toml stays valid TOML
    # (otherwise the first `uv sync` after init fails to parse it).
    import tomllib

    desc = 'A "quoted" desc with a \\ backslash'
    initialize(template_tree, "python", "acme-svc", desc, apply_settings=False)
    data = tomllib.loads((template_tree / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["description"] == desc
    assert data["project"]["name"] == "acme-svc"


def test_initialize_rejects_project_name_that_cannot_be_a_python_package(
    template_tree: Path,
) -> None:
    """Reject a distribution name that cannot produce a valid import package."""
    with pytest.raises(ValueError, match="valid Python package"):
        initialize(template_tree, "python", "123-service", "svc", apply_settings=False)


def test_ensure_clean_worktree_rejects_existing_changes(tmp_path: Path) -> None:
    """Refuse destructive initialization when Git reports existing work."""

    class DirtyResult:
        """Represent a successful Git command that reports a modified file."""

        stdout = " M README.md\n"

    def dirty_runner(*args: object, **kwargs: object) -> DirtyResult:
        """Return a deterministic dirty-worktree result."""
        return DirtyResult()

    with pytest.raises(RuntimeError, match="existing local changes"):
        ensure_clean_worktree(tmp_path, runner=dirty_runner)


def test_ensure_clean_worktree_accepts_clean_repo(tmp_path: Path) -> None:
    """Allow initialization when Git reports no existing work."""

    class CleanResult:
        """Represent a successful Git command with no changed files."""

        stdout = ""

    def clean_runner(*args: object, **kwargs: object) -> CleanResult:
        """Return a deterministic clean-worktree result."""
        return CleanResult()

    ensure_clean_worktree(tmp_path, runner=clean_runner)


def test_initialize_rejects_bad_stack(template_tree: Path) -> None:
    with pytest.raises(ValueError):
        initialize(template_tree, "rust", "x", "y", apply_settings=False)


def test_initialize_refuses_when_already_initialized(template_tree: Path) -> None:
    initialize(template_tree, "python", "acme", "svc", apply_settings=False)
    with pytest.raises(RuntimeError):
        initialize(template_tree, "python", "acme", "svc", apply_settings=False)
