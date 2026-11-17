import re
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


def test_prerequisites_name_preflight_package_managers() -> None:
    """Make the pre-init uv/bun requirements visible in the prerequisites table."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Initialize or work in a **React** repo" in readme
    assert "Initialize or work in a **Python** repo" in readme


def _interview_block() -> str:
    """Return the README's initialization prompt (the INTERVIEW block)."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("<!-- INTERVIEW:start -->")
    end = readme.index("<!-- INTERVIEW:end -->")
    return readme[start:end]


def test_readme_interview_asks_docs_only_opt_in() -> None:
    """Ask whether docs-only PRs skip the PR review; the default is no."""
    questions = [
        " ".join(item.split())
        for item in re.split(r"\n(?=\d+\. )", _interview_block())
        if re.match(r"\d+\. ", item)
    ]
    matching = [
        q
        for q in questions
        if "pass review-gate without a PR review" in q and "Default: no" in q
    ]
    assert len(matching) == 1


def test_readme_setup_copies_docs_only_example_on_yes() -> None:
    """On yes, setup copies the shipped example into place before the first commit."""
    block = _interview_block()
    copy = (
        "cp .agents/skills/resolve-pr-concerns/references/docs-only-paths.example.json"
        " .github/docs-only-paths.json"
    )
    assert copy in block
    assert block.index(copy) < block.index(
        "chore: initialize from agentic-ai-dev-template"
    )


def test_readme_customizing_documents_docs_only_paths() -> None:
    """The customizing section documents the opt-in file for later adoption."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("## Customizing the skills in your repo")
    end = readme.index("## Branch pattern")
    assert ".github/docs-only-paths.json" in readme[start:end]
