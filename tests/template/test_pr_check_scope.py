"""Pin the ``pr-check-scope`` rule and the fail-closed fallback in the skills."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RULE = REPO_ROOT / ".agents" / "rules" / "shared" / "pr-check-scope.md"
SKILL_FILES = sorted((REPO_ROOT / ".agents" / "skills").glob("*/SKILL.md"))
FALLBACK = "if the repo has no `pr-check-scope` rule, run the full `make pr_check`"
# The pinned hard-stop wording names the light check as a condition, not an offer.
HARD_STOP_CLAUSE = "unless the diff qualifies for the light check"
FULL_CHECK_PATHS = [
    "src/",
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "bun.lock",
    "Makefile",
    ".github/workflows/",
    ".github/actions/",
    "conftest.py",
    "scripts/",
]


def unguarded_light_check_mentions(text: str) -> list[str]:
    """Return the paragraphs of ``text`` that offer the light check without the fallback.

    Parameters
    ----------
    text : str
        A skill's Markdown.

    Returns
    -------
    list[str]
        Each blank-line-separated paragraph that mentions "light check" (any case)
        but does not carry the fail-closed fallback phrase. A line counts as
        guarded when it, or its paragraph, has the phrase.
    """
    bad = []
    for paragraph in text.split("\n\n"):
        flat = " ".join(paragraph.split())
        if "light check" not in flat.replace(HARD_STOP_CLAUSE, "").lower():
            continue
        if FALLBACK not in flat:
            bad.append(paragraph)
    return bad


def test_rule_frontmatter() -> None:
    text = RULE.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front = text.split("---\n")[1]
    assert "alwaysApply: true" in front
    assert any(
        line.startswith("description:") and line[len("description:") :].strip()
        for line in front.splitlines()
    )


@pytest.mark.parametrize("path", FULL_CHECK_PATHS)
def test_rule_lists_full_check_paths(path: str) -> None:
    assert path in RULE.read_text(encoding="utf-8")


def test_rule_names_its_wait_and_lock_helpers() -> None:
    text = RULE.read_text(encoding="utf-8")
    assert "wait_for_pr_checks.py" in text
    assert "single_flight.py" in text


def test_agents_md_routes_the_rule() -> None:
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert ".agents/rules/shared/pr-check-scope.md" in agents
    assert "light check" in agents


@pytest.mark.parametrize("skill", SKILL_FILES, ids=lambda p: p.parent.name)
def test_light_check_offers_carry_fallback(skill: Path) -> None:
    text = skill.read_text(encoding="utf-8")
    assert unguarded_light_check_mentions(text) == []


def test_checker_catches_an_unguarded_mention() -> None:
    assert unguarded_light_check_mentions("Run the light check, then push.")
    guarded = f"Run the light check; {FALLBACK}."
    assert unguarded_light_check_mentions(guarded) == []
    wrapped = "Run the light check; if the repo has no `pr-check-scope`\nrule, run the full `make pr_check`."
    assert unguarded_light_check_mentions(wrapped) == []
    assert unguarded_light_check_mentions(f"a PR {HARD_STOP_CLAUSE} and CI") == []
