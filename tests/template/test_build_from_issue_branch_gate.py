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


def _item(gate: str, start: str, end: str | None = None) -> str:
    """Return the gate text from ``start`` up to ``end`` (or the gate's end)."""
    assert start in gate, f"the gate needs {start!r}"
    rest = gate.split(start, 1)[1]
    return rest.split(end, 1)[0] if end else rest


def test_pattern_is_resolved_at_session_start() -> None:
    """Step 0 and Step 1 both read branch names, so the pattern comes first."""
    resolve = _item(_gate(), "**Resolve the pattern**", "\n2. ")
    assert "at session start" in resolve
    assert "before Step 0" in resolve and "before Step 1" in resolve
    assert "`<branch>`" in resolve
    step_0 = _section(_text(), "## Step 0: Propose Next (no-target mode)")
    assert "resolve it first, Step 1a item 1" in step_0


def test_only_a_fresh_workspace_for_this_issue_is_renamed() -> None:
    fresh = _item(_gate(), "**Fresh launcher workspace", "\n3. ")
    assert "Only a fresh workspace is renamed here" in fresh
    for check in (
        "git rev-list --count origin/main..HEAD",
        "@{upstream}",
        "git ls-remote --heads origin <current-branch>",
        "gh pr list --head <current-branch> --state all",
        "git status --porcelain",
    ):
        assert check in fresh, check
    # Uncommitted changes count only when the prompt names #N AND the user agrees.
    assert "the launcher's prompt names #N **and** the user confirms" in fresh
    assert "AskUserQuestion" in fresh
    assert "goes to the Step 1b mismatch gate instead" in fresh


def test_fresh_workspace_is_renamed_with_the_branch_placeholder() -> None:
    fresh = _item(_gate(), "**Fresh launcher workspace", "\n3. ")
    for command in (
        "git branch -m <branch>",
        "gh issue develop <N> --repo <owner>/<repo> --base main --name <branch>",
        "git fetch origin <branch> && git branch -u origin/<branch>",
        "git pull --ff-only origin <branch>",
        "gh issue develop --list <N> --repo <owner>/<repo>",
    ):
        assert command in fresh, command


def test_existing_branch_for_the_issue_is_checked_before_any_rename() -> None:
    fresh = _item(_gate(), "**Fresh launcher workspace", "\n3. ")
    rename = fresh.index("git branch -m <branch>")
    linked = fresh.index("gh issue develop --list <N>")
    unlinked = fresh.index("git show-ref --verify refs/heads/<branch>")
    assert linked < rename and unlinked < rename
    assert "git ls-remote --heads origin <branch>" in fresh
    # `gh issue develop --name` fails on an existing name: ask, don't guess.
    unlinked_rule = fresh[unlinked : fresh.index("Otherwise rename")]
    assert "AskUserQuestion" in unlinked_rule
    assert "would fail" in unlinked_rule


def test_resume_refuses_a_branch_checked_out_elsewhere_and_a_divergence() -> None:
    resume = _item(_gate(), "**Resume**", "\n3. ")
    assert "git worktree list" in resume
    assert (
        "git fetch origin <existing> && git switch <existing> && "
        "git branch -u origin/<existing> && git pull --ff-only origin <existing>"
    ) in resume
    assert "diverged" in resume and "stop" in resume


def test_other_non_matching_branches_go_through_step_1b_unrenamed() -> None:
    other = _item(_gate(), "**Any other non-matching branch**", "\n4. ")
    assert "is **not** renamed here" in other
    assert "Step 1b mismatch gate under its original name" in other
    assert "only once the user picks it in Step 1b (the Step 1e path)" in other
    local = _item(other, "**Local-only:**", "\n   - ")
    assert "git branch -m <old> <new>" in local


def test_gate_asks_before_renaming_a_pushed_branch() -> None:
    other = _item(_gate(), "**Any other non-matching branch**", "\n4. ")
    pushed = _item(other, "**Pushed, no open PR:**", "\n   - ")
    assert "AskUserQuestion" in pushed
    assert (
        "gh api -X POST repos/<owner>/<repo>/branches/<old>/rename -f new_name=<new>"
        in pushed
    )


def test_an_open_prs_head_is_never_renamed() -> None:
    """GitHub closes a PR whose head is renamed; it retargets only base PRs."""
    other = _item(_gate(), "**Any other non-matching branch**", "\n4. ")
    head = _item(other, "**Head of an open PR:**", "\n\n")
    assert "never rename it" in head
    assert "Keep the name" in head
    assert "*closes* an open PR whose head branch is renamed" in head
    for path in (_SKILL, _README):
        assert "moves open PRs" not in path.read_text(encoding="utf-8"), path.name


def test_opening_rule_and_red_flag_name_the_open_pr_exception() -> None:
    opening = _gate().strip().split("\n", 1)[0]
    assert "one exception" in opening and "open PR" in opening
    red_flags = _section(_text(), "## Red Flags — Stop and Reassess")
    (flag,) = [line for line in red_flags.splitlines() if "Step 1a" in line]
    assert "the one exception is the head of an open PR" in flag
    assert "renaming an open PR's head" in flag


def test_step_1e_on_main_checks_for_commits_before_switching() -> None:
    current = _text().split("### 1e. Current-branch path", 1)[1].split("\n## ", 1)[0]
    on_main = _item(current, "**On `main` or a detached HEAD:**", "\n- ")
    check = on_main.index("git rev-list --count origin/main..HEAD")
    existing = on_main.index("check for an existing branch for #N")
    create = on_main.index("git switch -c <branch>")
    assert check < existing < create
    assert "before any switch" in on_main
    assert "AskUserQuestion" in on_main[:existing]
    assert "never commit for an issue on `main` or on no branch" in _gate()


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
