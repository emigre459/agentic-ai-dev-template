import json
import shutil
import subprocess  # nosec B404 -- fixed argv, in-repo script, no shell
import tarfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

import pytest

from scripts.init_template import (
    _locale_sort_key,
    _ordered_skill_files,
    _python_skill_hash,
    ensure_clean_worktree,
    initialize,
    main,
)


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
    """Reject an invalid package name before changing any template files."""
    ignored_cache_dirs = {".mypy_cache", ".pytest_cache", ".ruff_cache"}
    before = {
        path.relative_to(template_tree): path.read_bytes()
        for path in template_tree.rglob("*")
        if path.is_file()
        and ignored_cache_dirs.isdisjoint(path.relative_to(template_tree).parts)
    }

    with pytest.raises(ValueError, match="valid Python package"):
        initialize(template_tree, "python", "123-service", "svc", apply_settings=False)

    after = {
        path.relative_to(template_tree): path.read_bytes()
        for path in template_tree.rglob("*")
        if path.is_file()
        and ignored_cache_dirs.isdisjoint(path.relative_to(template_tree).parts)
    }
    assert after == before


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


def test_ensure_clean_worktree_reports_missing_git(tmp_path: Path) -> None:
    """Turn a missing Git executable into an actionable initialization error."""

    def missing_git_runner(*args: object, **kwargs: object) -> NoReturn:
        raise FileNotFoundError("git")

    with pytest.raises(RuntimeError, match="Git is not installed"):
        ensure_clean_worktree(tmp_path, runner=missing_git_runner)


def test_ensure_clean_worktree_reports_git_status_failure(tmp_path: Path) -> None:
    """Preserve Git's diagnostic without leaking a subprocess traceback."""

    def failing_git_runner(*args: object, **kwargs: object) -> NoReturn:
        raise subprocess.CalledProcessError(
            128,
            ["git", "status"],
            stderr="fatal: not a git repository",
        )

    with pytest.raises(RuntimeError, match="fatal: not a git repository"):
        ensure_clean_worktree(tmp_path, runner=failing_git_runner)


def test_main_requires_repo_when_applying_settings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject --apply-settings without its required explicit GitHub target."""
    monkeypatch.setattr("scripts.init_template.ensure_clean_worktree", lambda *_: None)
    monkeypatch.setattr("scripts.init_template.initialize", lambda *_, **__: None)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--stack",
                "python",
                "--project-name",
                "acme-svc",
                "--description",
                "service",
                "--apply-settings",
            ]
        )

    assert exc_info.value.code == 2
    assert "--repo is required when --apply-settings is used" in capsys.readouterr().err


def test_initialize_rejects_bad_stack(template_tree: Path) -> None:
    with pytest.raises(ValueError):
        initialize(template_tree, "rust", "x", "y", apply_settings=False)


def test_initialize_refuses_when_already_initialized(template_tree: Path) -> None:
    initialize(template_tree, "python", "acme", "svc", apply_settings=False)
    with pytest.raises(RuntimeError):
        initialize(template_tree, "python", "acme", "svc", apply_settings=False)


# --- init starts on the pinned skills release (#49, layer L1) ------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "skills_lock"
_HASH_SCRIPT = _REPO_ROOT / ".github" / "actions" / "skills-drift" / "skills-lock.mjs"
_SOURCE = "emigre459/agentic-ai-dev-template"
_REF = "v1.2.0"
_NEEDS_NODE = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)


class _FakeGit:
    """Runner standing in for ``subprocess.run``: serves git from a local tree.

    ``init`` makes the scratch repo dir; ``fetch`` succeeds, or raises
    ``fetch_error``; ``archive`` tars the requested ``.agents/skills/<name>``
    folders out of ``source``. Anything that is not git (the node hash) is
    passed through to the real ``subprocess.run``.
    """

    def __init__(self, source: Path, *, fetch_error: Exception | None = None) -> None:
        self.source = source
        self.fetch_error = fetch_error
        self.git_calls: list[list[str]] = []

    def __call__(
        self, cmd: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if cmd[0] != "git":
            return subprocess.run(cmd, **kwargs)  # noqa: PLW1510  # nosec B603
        self.git_calls.append(list(cmd))
        if cmd[1] == "init":
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        elif cmd[1] == "-C" and cmd[3] == "fetch":
            if self.fetch_error is not None:
                raise self.fetch_error
        elif cmd[1] == "-C" and cmd[3] == "archive":
            out = Path(cmd[cmd.index("-o") + 1])
            with tarfile.open(out, "w") as tar:
                for path in cmd[cmd.index("FETCH_HEAD") + 1 :]:
                    tar.add(self.source / path, arcname=path)
        else:
            raise AssertionError(f"unexpected git command: {cmd}")
        return subprocess.CompletedProcess(list(cmd), 0, "", "")


def _fetch_failure() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(128, ["git", "fetch"], stderr="no route")


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every regular file under ``root`` (relative POSIX path -> bytes)."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def _no_commands(cmd: Sequence[str], **kwargs: Any) -> NoReturn:
    """Runner that fails the test if init shells out at all."""
    raise AssertionError(f"unexpected command: {cmd}")


def _make_source(tmp_path: Path, skills: dict[str, str]) -> Path:
    """Build a release-shaped tree: ``.agents/skills/<name>/SKILL.md`` per skill."""
    source = tmp_path / "release-source"
    for name, body in skills.items():
        folder = source / ".agents" / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(body)
        (folder / "notes.md").write_text(f"notes for {name}\n")
    return source


def _write_lock(root: Path, entries: dict[str, tuple[str, str | None]]) -> None:
    """Write a skills-lock.json from ``{name: (computedHash, ref-or-None)}``."""
    skills: dict[str, dict[str, str]] = {}
    for name, (digest, ref) in entries.items():
        entry = {"source": _SOURCE, "sourceType": "github"}
        if ref is not None:
            entry["ref"] = ref
        entry["skillPath"] = f".agents/skills/{name}/SKILL.md"
        entry["computedHash"] = digest
        skills[name] = entry
    (root / "skills-lock.json").write_text(
        json.dumps({"version": 1, "skills": skills}, indent=2) + "\n"
    )


def _released(source: Path, *names: str) -> dict[str, tuple[str, str | None]]:
    """Lock entries pinning ``names`` to ``_REF`` with the source's real hashes."""
    return {
        name: (_python_skill_hash(source / ".agents" / "skills" / name), _REF)
        for name in names
    }


def test_init_swaps_skills_to_pinned_tag(template_tree: Path, tmp_path: Path) -> None:
    source = _make_source(
        tmp_path, {"run-tests": "released run-tests\n", "pr-check": "released pr\n"}
    )
    _write_lock(template_tree, _released(source, "run-tests", "pr-check"))
    stale = template_tree / ".agents" / "skills" / "run-tests" / "stale.md"
    stale.write_text("not in the release\n")
    fake = _FakeGit(source)

    initialize(template_tree, "python", "acme-svc", "svc", runner=fake)

    skills = template_tree / ".agents" / "skills"
    assert (skills / "run-tests" / "SKILL.md").read_text() == "released run-tests\n"
    assert (skills / "pr-check" / "SKILL.md").read_text() == "released pr\n"
    assert not stale.exists()
    # The fetch goes into a throwaway repo, never the user's own .git.
    init, fetch, *rest = fake.git_calls
    scratch_repo = init[-1]
    assert init == ["git", "init", "-q", scratch_repo]
    assert fetch == [
        "git",
        "-C",
        scratch_repo,
        "fetch",
        "--depth",
        "1",
        f"https://github.com/{_SOURCE}.git",
        "tag",
        _REF,
    ]
    assert len(rest) == 2
    assert all(c[:4] == ["git", "-C", scratch_repo, "archive"] for c in rest)
    assert all("--format=tar" in c and "FETCH_HEAD" in c for c in rest)
    assert not any(str(template_tree) in part for c in fake.git_calls for part in c)


def test_init_fails_on_hash_mismatch_before_touching_anything(
    template_tree: Path, tmp_path: Path
) -> None:
    source = _make_source(tmp_path, {"run-tests": "good\n", "pr-check": "good\n"})
    entries = _released(source, "run-tests", "pr-check")
    # The lock expects bytes the "release" does not contain, for the second skill.
    entries["pr-check"] = ("0" * 64, _REF)
    _write_lock(template_tree, entries)
    before = _snapshot(template_tree)

    with pytest.raises(RuntimeError, match="pr-check"):
        initialize(template_tree, "python", "acme-svc", "svc", runner=_FakeGit(source))

    # Not even the skill that did verify was replaced, and stacks/ is still there.
    assert (template_tree / "stacks").is_dir()
    assert _snapshot(template_tree) == before

    # With the lock fixed, a retry succeeds.
    _write_lock(template_tree, _released(source, "run-tests", "pr-check"))
    initialize(template_tree, "python", "acme-svc", "svc", runner=_FakeGit(source))
    assert not (template_tree / "stacks").exists()


def test_init_fails_loudly_when_fetch_fails_and_a_retry_works(
    template_tree: Path, tmp_path: Path
) -> None:
    source = _make_source(tmp_path, {"run-tests": "x\n"})
    _write_lock(template_tree, _released(source, "run-tests"))
    before = _snapshot(template_tree)

    with pytest.raises(RuntimeError, match="network access") as excinfo:
        initialize(
            template_tree,
            "python",
            "acme-svc",
            "svc",
            runner=_FakeGit(source, fetch_error=_fetch_failure()),
        )

    assert _SOURCE in str(excinfo.value) and _REF in str(excinfo.value)
    assert "no route" in str(excinfo.value)
    # Offline is the likely failure: the tree must be byte-for-byte untouched,
    # stacks/ included, so re-running `make init` is possible.
    assert (template_tree / "stacks").is_dir()
    assert _snapshot(template_tree) == before

    initialize(template_tree, "python", "acme-svc", "svc", runner=_FakeGit(source))
    assert not (template_tree / "stacks").exists()
    skill = template_tree / ".agents" / "skills" / "run-tests" / "SKILL.md"
    assert skill.read_text() == "x\n"


def test_init_says_git_is_missing_when_git_is_not_installed(
    template_tree: Path, tmp_path: Path
) -> None:
    source = _make_source(tmp_path, {"run-tests": "x\n"})
    _write_lock(template_tree, _released(source, "run-tests"))
    fake = _FakeGit(source, fetch_error=FileNotFoundError("git"))

    with pytest.raises(RuntimeError, match="git is not installed") as excinfo:
        initialize(template_tree, "python", "acme-svc", "svc", runner=fake)

    assert "network" not in str(excinfo.value)
    assert (template_tree / "stacks").is_dir()


@pytest.mark.parametrize(
    ("name", "ref", "match"),
    [
        ("../escape", _REF, "invalid skill name"),
        ("..", _REF, "invalid skill name"),
        ("run-tests", "main", "not a release tag"),
        ("run-tests", "v1.2", "not a release tag"),
        ("run-tests", "--upload-pack=x", "not a release tag"),
    ],
)
def test_init_rejects_unsafe_lock_values(
    template_tree: Path, name: str, ref: str, match: str
) -> None:
    _write_lock(template_tree, {name: ("0" * 64, ref)})
    before = _snapshot(template_tree)

    with pytest.raises(RuntimeError, match=match):
        initialize(template_tree, "python", "acme-svc", "svc", runner=_no_commands)

    assert _snapshot(template_tree) == before


def test_init_without_ref_keeps_tree_and_local_actions(template_tree: Path) -> None:
    workflow = template_tree / ".github" / "workflows" / "skills.yml"
    workflow.write_text(
        "jobs:\n  drift:\n    steps:\n      - uses: ./.github/actions/skills-drift\n"
    )
    assert '"ref"' not in (template_tree / "skills-lock.json").read_text()
    skill = template_tree / ".agents" / "skills" / "run-tests"
    hash_before = _python_skill_hash(skill)

    initialize(template_tree, "python", "acme-svc", "svc", runner=_no_commands)

    assert "uses: ./.github/actions/skills-drift" in workflow.read_text()
    assert (template_tree / ".github" / "actions" / "skills-drift").is_dir()
    assert _python_skill_hash(skill) == hash_before


def test_init_without_lock_file_is_a_no_op(template_tree: Path) -> None:
    (template_tree / "skills-lock.json").unlink()

    initialize(template_tree, "python", "acme-svc", "svc", runner=_no_commands)

    assert (template_tree / ".github" / "actions" / "skills-drift").is_dir()


def test_init_points_actions_at_release(template_tree: Path, tmp_path: Path) -> None:
    source = _make_source(tmp_path, {"run-tests": "x\n"})
    _write_lock(template_tree, _released(source, "run-tests"))
    workflow = template_tree / ".github" / "workflows" / "skills.yml"
    workflow.write_text(
        "jobs:\n  drift:\n    steps:\n"
        "      - uses: ./.github/actions/skills-drift\n"
        "      - uses: actions/checkout@v4\n"
        "      - name: other\n"
        "        uses: ./.github/actions/skills-other\n"
    )

    initialize(template_tree, "python", "acme-svc", "svc", runner=_FakeGit(source))

    remote = f"{_SOURCE}/.github/actions"
    assert workflow.read_text() == (
        "jobs:\n  drift:\n    steps:\n"
        f"      - uses: {remote}/skills-drift@{_REF}\n"
        "      - uses: actions/checkout@v4\n"
        "      - name: other\n"
        f"        uses: {remote}/skills-other@{_REF}\n"
    )
    assert not (template_tree / ".github" / "actions" / "skills-drift").exists()


def test_init_rejects_a_lock_with_disagreeing_refs(
    template_tree: Path, tmp_path: Path
) -> None:
    source = _make_source(tmp_path, {"run-tests": "x\n", "pr-check": "y\n"})
    entries = _released(source, "run-tests", "pr-check")
    entries["pr-check"] = (entries["pr-check"][0], "v1.1.0")
    _write_lock(template_tree, entries)

    with pytest.raises(RuntimeError, match="ref"):
        initialize(template_tree, "python", "acme-svc", "svc", runner=_FakeGit(source))

    assert (template_tree / "scripts" / "init_template.py").exists()


def test_python_hash_port_matches_golden() -> None:
    golden = json.loads((_FIXTURES / "golden.json").read_text())["demo-skill"]

    assert _python_skill_hash(_FIXTURES / "demo-skill") == golden


def _node_sorted(names: list[str]) -> list[str]:
    """Sort ``names`` with node's ``localeCompare``, as the skills CLI does."""
    script = (
        "const n = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        "n.sort((a, b) => a.localeCompare(b));"
        "console.log(JSON.stringify(n));"
    )
    result = subprocess.run(  # nosec B603 B607
        ["node", "-e", script],
        input=json.dumps(names),
        capture_output=True,
        text=True,
        check=True,
    )
    sorted_names: list[str] = json.loads(result.stdout)
    return sorted_names


@_NEEDS_NODE
@pytest.mark.parametrize(
    "skill_dir",
    sorted(p for p in (_REPO_ROOT / ".agents" / "skills").iterdir() if p.is_dir()),
    ids=lambda p: p.name,
)
def test_python_hash_port_matches_node_for_every_real_skill(skill_dir: Path) -> None:
    """Guard on the port's ``localeCompare`` emulation, over the real skills."""
    paths = [
        p.relative_to(skill_dir).as_posix() for p in _ordered_skill_files(skill_dir)
    ]
    assert paths == _node_sorted(paths)
    node = subprocess.run(  # nosec B603 B607
        ["node", str(_HASH_SCRIPT), "hash", str(skill_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert _python_skill_hash(skill_dir) == node.stdout.strip()


@_NEEDS_NODE
def test_python_sort_key_matches_localecompare_on_tricky_names() -> None:
    names = [
        "SKILL.md", "skill.md", "Skill.md", "a.md", "A.md", "a b.md", "a_b.md",
        "a-b.md", "a1.md", "A1.md", "ab.md", "aB.md", "_private.md", ".hidden",
        "1.txt", "10.txt", "2.txt", "sub/b.txt", "sub/A.txt", "sub.md", "sub-x/a",
        "sub_x/a", "scripts/render.py", "scripts/Render.py", "scripts/__init__.py",
        "references/stacked-prs.md", "references/parallel-fixes.md", "Zeta.md",
        "zeta.md", "(x).md", "x+y.md", "x&y.md", "x,y.md", "x~y.md", "x@y.md",
    ]  # fmt: skip
    assert sorted(names, key=_locale_sort_key) == _node_sorted(names)


def test_init_pins_with_the_python_hash_when_node_is_missing(
    template_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source(tmp_path, {"run-tests": "released\n"})
    _write_lock(template_tree, _released(source, "run-tests"))
    monkeypatch.setattr("scripts.init_template.shutil.which", lambda _: None)
    fake = _FakeGit(source)

    initialize(template_tree, "python", "acme-svc", "svc", runner=fake)

    skill = template_tree / ".agents" / "skills" / "run-tests" / "SKILL.md"
    assert skill.read_text() == "released\n"
