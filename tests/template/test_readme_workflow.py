from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_initialization_prompt_uses_explicit_github_target() -> None:
    """Keep every copy-paste GitHub mutation pinned to the confirmed repo."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert 'gh issue create --repo "$TARGET_REPO"' in readme
    assert 'gh issue develop <N> --repo "$TARGET_REPO"' in readme
    assert 'gh pr create --repo "$TARGET_REPO"' in readme
    assert "Do not use implicit GitHub repository detection" in readme
    assert "gh issue create --title" not in readme


def test_initialization_prompt_uses_two_settings_phases() -> None:
    """Do not require generated CI checks before their workflow reaches main."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    bootstrap = readme.index("make apply_repo_settings_bootstrap")
    merge = readme.index("AFTER the setup PR merges")
    final = readme.index("make finalize_repo_settings")

    assert bootstrap < merge < final
    assert "deliberately defers" in readme
    assert "required CI checks" in readme


def test_initialization_prompt_protects_existing_work_and_pr_template() -> None:
    """Require clean-tree review and a populated PR template."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Never discard, overwrite, or silently include" in readme
    assert "git diff --name-status" in readme
    assert "do not replace the template with a one-line" in readme
    assert 'gh pr create --fill --body "Closes #' not in readme
