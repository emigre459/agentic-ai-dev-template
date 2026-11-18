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


def test_readme_interview_offers_skills_update_app_setup() -> None:
    """Offer the two-click App setup for the nightly skills update PR once."""
    questions = [
        " ".join(item.split())
        for item in re.split(r"\n(?=\d+\. )", _interview_block())
        if re.match(r"\d+\. ", item)
    ]
    matching = [
        q for q in questions if "two clicks" in q and "nightly skills update PR" in q
    ]
    assert len(matching) == 1


def test_readme_setup_runs_app_script_before_init_commit() -> None:
    """The App script runs before the init commit so the lane file lands in it."""
    block = _interview_block()
    script = "python3 .agents/skills/upgrade-deps/scripts/setup_skills_update_app.py"
    assert script in block
    assert block.index(script) < block.index(
        "chore: initialize from agentic-ai-dev-template"
    )


def test_readme_customizing_documents_skills_update_lane() -> None:
    """The customizing section documents the opt-in skills-update lane file."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("## Customizing the skills in your repo")
    end = readme.index("## Branch pattern")
    assert ".github/skills-update-lane.json" in readme[start:end]


def test_readme_staying_up_to_date_has_no_github_token_fallback() -> None:
    """Without an App or PAT the nightly run fails; the README says how to fix it."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("## Staying up to date")
    end = readme.index("## The drift check")
    section = " ".join(readme[start:end].split())
    assert "setup_skills_update_app.py" in section
    assert "machine account" in section
    assert "Allow auto-merge" in section
    assert "| neither |" not in section
    assert "never merges itself" not in section
    assert "ci-workflow" not in section
