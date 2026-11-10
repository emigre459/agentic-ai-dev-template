"""Guard build-from-issue's branch-name gate (Step 1a).

The skill states the ``{issue-number}-{kebab-case}`` convention, and tooling keys off
it: Step 0's in-flight map (remote ``{number}-*`` branches, clean-up-kanban §7) and
any repo script that parses the leading issue number. Launchers name branches their
own way (``issue-2840``, ``<user>/<slug>``, ``worktree-<name>``), so Step 1a renames a
non-matching branch before the first commit. The pattern is repo-specific, so it
comes from the consuming repo's ``.agents/skill-config/build-from-issue.md``; the
template ships no such file, and the default lives in the skill.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SKILL = _REPO / ".agents" / "skills" / "build-from-issue" / "SKILL.md"
_README = _REPO / "README.md"
_GATE_HEADING = "## Step 1a: Branch-Name Gate"
_DEFAULT = "`{issue-number}-{kebab-case}`"


def _text() -> str:
    return _SKILL.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the body under ``heading`` up to the next ``## `` heading."""
    assert heading in text, f"SKILL.md needs a {heading!r} section"
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def _gate() -> str:
    return _section(_text(), _GATE_HEADING)


def test_gate_sits_between_step_1_and_step_1b() -> None:
    text = _text()
    step_1 = text.index("## Step 1: Fetch the Issue")
    gate = text.index(_GATE_HEADING)
    step_1b = text.index("## Step 1b:")
    assert step_1 < gate < step_1b


def test_workflow_diagram_routes_step_1_through_the_gate() -> None:
    diagram = _text().split("```dot", 1)[1].split("```", 1)[0]
    node = re.search(
        r'^\s*"(1a\. Branch-name gate[^"]*)" \[shape=box\];', diagram, re.MULTILINE
    )
    assert node, "the diagram needs a '1a. Branch-name gate' node"
    name = node.group(1)
    assert f'"1. Fetch issue" -> "{name}"' in diagram
    assert f'"{name}" -> "1b. Issue matches current branch?"' in diagram
    assert '"1. Fetch issue" -> "1b. Issue matches current branch?"' not in diagram


def test_gate_reads_the_pattern_from_repo_config() -> None:
    gate = _gate()
    assert "`## Branch pattern`" in gate
    assert "`.agents/skill-config/build-from-issue.md`" in gate
    # AGENTS.md wins over the config file, per the preflight's precedence.
    assert "`AGENTS.md`" in gate


def test_gate_states_the_default_pattern() -> None:
    gate = _gate()
    assert f"the default is {_DEFAULT}" in gate


def test_fresh_launcher_workspace_is_renamed_in_place_and_linked() -> None:
    gate = _gate()
    for check in (
        "git rev-list --count origin/main..HEAD",
        "@{upstream}",
        "git ls-remote --heads origin",
        "gh pr list --head",
    ):
        assert check in gate, check
    for command in (
        "git branch -m <N>-<kebab-title>",
        "gh issue develop <N> --repo <owner>/<repo> --base main --name <N>-<kebab-title>",
        "git branch -u origin/<N>-<kebab-title>",
        "git pull --ff-only",
        "gh issue develop --list <N>",
    ):
        assert command in gate, command
    assert "Step 1b" in gate and "mismatch gate" in gate
    # An issue that already has a linked branch is a resume, not a second branch.
    assert "git switch <existing>" in gate


def test_gate_asks_before_renaming_a_pushed_branch() -> None:
    gate = _gate()
    pushed = gate.split("Any other non-matching branch", 1)
    assert len(pushed) == 2, "the gate needs an 'Any other non-matching branch' item"
    rule = pushed[1]
    assert "AskUserQuestion" in rule
    assert (
        "gh api -X POST repos/<owner>/<repo>/branches/<old>/rename -f new_name=<new>"
        in rule
    )
    assert "moves open PRs" in rule


def test_every_branch_naming_site_points_at_the_resolved_pattern() -> None:
    text = _text()
    fetch = _section(text, "## Step 1: Fetch the Issue")
    assert "branch pattern" in fetch and "Step 1a" in fetch
    step_0 = _section(text, "## Step 0: Propose Next (no-target mode)")
    assert "Step 1a" in step_0
    worktree = text.split("### 1d. Worktree path", 1)[1].split("### 1e.", 1)[0]
    resume, fresh = worktree.split("**Fresh-start path", 1)
    assert "Step 1a" in resume
    assert "Step 1a" in fresh
    current = text.split("### 1e. Current-branch path", 1)[1].split("\n## ", 1)[0]
    assert "Step 1a" in current
    # The only bare `{N}-*` / `{number}-*` mentions left are labelled as the default.
    for bare in re.finditer(r"`\{(?:N|number)\}-\*`", text):
        context = text[max(0, bare.start() - 80) : bare.start()]
        assert "default" in context, text[bare.start() - 80 : bare.end()]


def test_stack_layers_follow_the_resolved_pattern() -> None:
    stack = (_SKILL.parent / "references" / "stacked-prs.md").read_text(
        encoding="utf-8"
    )
    branch_names = stack.split("- **Branch names.**", 1)[1].split("\n- **", 1)[0]
    assert "Step 1a" in branch_names


def test_red_flag_and_common_mistake_cover_the_gate() -> None:
    text = _text()
    red_flags = _section(text, "## Red Flags — Stop and Reassess")
    assert any("Step 1a" in line for line in red_flags.splitlines())
    mistakes = _section(text, "## Common Mistakes")
    assert any(
        line.startswith("|") and "Step 1a" in line for line in mistakes.splitlines()
    )


def test_readme_config_example_shows_a_branch_pattern() -> None:
    readme = _README.read_text(encoding="utf-8")
    lines: list[str] = []
    in_fence = False
    # The section ends at the next H2 outside a code fence (its example holds one).
    for line in readme.split("### `.agents/skill-config/<skill>.md`", 1)[
        1
    ].splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif line.startswith("## ") and not in_fence:
            break
        lines.append(line)
    section = "\n".join(lines)
    example = section.split("```markdown", 1)[1].split("\n```", 1)[0]
    assert "## Branch pattern" in example
    assert "{issue-number}-{kebab-case}" in example
    assert "issue number" in section  # why a repo would require it


def test_template_ships_no_build_from_issue_config() -> None:
    """The skill-config folder belongs to consuming repos; the default is the skill's."""
    assert not (_REPO / ".agents" / "skill-config" / "build-from-issue.md").exists()
