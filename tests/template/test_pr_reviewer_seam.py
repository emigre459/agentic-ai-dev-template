"""Guard the reviewer-agnostic PR-review seam.

The active PR reviewer is declared once, in ``.github/pr-reviewer.json``. The
resolve-pr-concerns and build-from-issue skills and the pr-reviewer-auto-trigger
rule read it instead of naming a vendor, so swapping reviewers is a config edit.
These guards keep it that way: every adapter must be well-formed, have a notes
file for its quirks, and have its trigger comment allow-listed; and no seam-scoped
file may name a vendor (vendor detail belongs in the config or the per-adapter
notes files).
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess  # nosec B404 -- fixed argv, in-repo git, no shell
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[2]
_CONFIG = _REPO / ".github" / "pr-reviewer.json"
_SKILLS = _REPO / ".agents" / "skills"
_NOTES_DIR = _SKILLS / "resolve-pr-concerns" / "references" / "reviewers"
_RULE = _REPO / ".agents" / "rules" / "shared" / "pr-reviewer-auto-trigger.md"
_SKILL = _SKILLS / "resolve-pr-concerns" / "SKILL.md"
_STATE_SCRIPT = _SKILLS / "resolve-pr-concerns" / "scripts" / "reviewer_state.py"
_GUIDELINES = _REPO / ".github" / "review-guidelines.md"

# Whole skill directories, not a file list, so a newly-added file is covered too.
_SEAM_DIRS = ("resolve-pr-concerns", "build-from-issue")
_VENDOR_RE = re.compile(r"bugbot|coderabbit", re.IGNORECASE)

# Adapter field -> allowed JSON types (None = JSON null).
_FIELDS: dict[str, tuple[type | None, ...]] = {
    "display_name": (str,),
    "check_name": (str,),
    "trigger_comment": (str,),
    "bot_logins": (list,),
    "summary_marker": (str, None),
    "clean_pattern": (str,),
    "findings_pattern": (str,),
    "reviewed_sha_pattern": (str, list, None),
    "self_resolves_threads": (bool,),
    "summary_in_issue_comments": (bool,),
    # The final full review: all set, or all null for a reviewer agents can't ask
    # for one (reviewer_state.py enforces the all-or-none rule too).
    "final_review_comment": (str, None),
    "command_reply_marker": (str, None),
    "final_review_done_pattern": (str, None),
    "final_review_failed_pattern": (str, None),
    "final_review_partial_pattern": (str, None),
    # The reviewer's replies to the trigger comment: all set, or all null.
    "incremental_review_done_pattern": (str, None),
    "incremental_review_no_changes_pattern": (str, None),
    "incremental_review_failed_pattern": (str, None),
    "incremental_review_partial_pattern": (str, None),
    # Findings stated with no thread to reply on (one capture group each).
    "unthreaded_finding_patterns": (list,),
    # A refused (rate-limited) request: how to recognise it and how long to wait.
    "rate_limited_pattern": (str, None),
    "rate_limit_wait_pattern": (str, None),
    "rate_limit_default_wait_minutes": (int, None),
    "rate_limit_notice_marker": (str, None),
    # Local review rounds and the local allowance check (null = PR rounds only).
    "local_preflight": (dict, None),
    "local_review": (dict, None),
}
_INCREMENTAL_FIELDS = tuple(f for f in _FIELDS if f.startswith("incremental_"))
_FINAL_REVIEW_FIELDS = tuple(f for f in _FIELDS if f.startswith(("final_", "command_")))


def _load_state() -> ModuleType:
    """Load the standalone reviewer_state script (not an importable package)."""
    spec = importlib.util.spec_from_file_location("reviewer_state", _STATE_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["reviewer_state"] = module
    spec.loader.exec_module(module)
    return module


def _load_config() -> dict[str, Any]:
    config: dict[str, Any] = json.loads(_CONFIG.read_text())
    return config


_KEYS = sorted(_load_config()["reviewers"])


def _tracked(*paths: str) -> list[Path]:
    """Git-tracked files under ``paths`` (local caches don't count)."""
    out = subprocess.run(  # nosec B603 B607 -- fixed argv, in-repo git, no shell
        ["git", "ls-files", "-z", *paths],
        cwd=_REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [_REPO / rel for rel in sorted(filter(None, out.split("\0")))]


def _seam_files() -> list[Path]:
    files = [
        path
        for path in _tracked(*(f".agents/skills/{name}" for name in _SEAM_DIRS))
        if _NOTES_DIR not in path.parents
    ]
    return files + [_RULE]


def test_active_reviewer_is_configured() -> None:
    """``active`` names an adapter, or is null for a repo with no reviewer."""
    config = _load_config()
    assert "active" in config, f"{_CONFIG.relative_to(_REPO)}: 'active' is missing."
    assert config["active"] is None or config["active"] in config["reviewers"], (
        f"{_CONFIG.relative_to(_REPO)}: 'active' must name a key in 'reviewers' "
        "(or be null for no reviewer)."
    )


@pytest.mark.parametrize("key", _KEYS)
def test_every_adapter_is_well_formed(key: str) -> None:
    adapter = _load_config()["reviewers"][key]
    for field, types in _FIELDS.items():
        assert field in adapter, f"reviewer {key!r} is missing {field!r}"
        value = adapter[field]
        ok = any(value is None if t is None else isinstance(value, t) for t in types)
        assert ok, f"reviewer {key!r}: {field!r} has the wrong type ({value!r})"
    assert adapter["bot_logins"] and all(
        isinstance(login, str) and login for login in adapter["bot_logins"]
    ), f"reviewer {key!r}: bot_logins must be a non-empty list of logins"
    for field in (
        "clean_pattern",
        "findings_pattern",
        "final_review_done_pattern",
        "final_review_failed_pattern",
        "final_review_partial_pattern",
        *_INCREMENTAL_FIELDS,
    ):
        if adapter[field] is not None:
            re.compile(adapter[field])
    assert len({adapter[f] is None for f in _INCREMENTAL_FIELDS}) == 1, (
        f"reviewer {key!r}: {', '.join(_INCREMENTAL_FIELDS)} must all be set or "
        "all be null"
    )
    assert len({adapter[f] is None for f in _FINAL_REVIEW_FIELDS}) == 1, (
        f"reviewer {key!r}: {', '.join(_FINAL_REVIEW_FIELDS)} must all be set or "
        "all be null"
    )
    assert re.compile(adapter["findings_pattern"]).groups == 1, (
        f"reviewer {key!r}: findings_pattern needs exactly one capture group "
        "(the finding count)"
    )
    sha_patterns = adapter["reviewed_sha_pattern"] or []
    if isinstance(sha_patterns, str):
        sha_patterns = [sha_patterns]
    for pattern in sha_patterns:
        assert re.compile(pattern).groups == 1, (
            f"reviewer {key!r}: each reviewed_sha_pattern needs exactly one capture "
            "group (the reviewed SHA)"
        )
    for pattern in adapter["unthreaded_finding_patterns"]:
        assert re.compile(pattern).groups == 1, (
            f"reviewer {key!r}: each unthreaded_finding_patterns entry needs exactly "
            "one capture group (the finding's text)"
        )


@pytest.mark.parametrize("key", _KEYS)
def test_every_reviewer_has_a_notes_file(key: str) -> None:
    assert (_NOTES_DIR / f"{key}.md").is_file(), (
        f"Add {_NOTES_DIR.relative_to(_REPO)}/{key}.md describing the reviewer's "
        "detection quirks -- skill prose points agents there."
    )


@pytest.mark.parametrize("key", _KEYS)
def test_every_trigger_comment_is_allow_listed(key: str) -> None:
    trigger = _load_config()["reviewers"][key]["trigger_comment"]
    settings = json.loads((_REPO / ".claude" / "settings.json").read_text())
    expected = f"Bash(gh pr comment * {trigger}*)"
    assert expected in settings["permissions"]["allow"], (
        f"Add {expected!r} to .claude/settings.json permissions.allow so agents can "
        f"trigger the {key} reviewer without a prompt."
    )


@pytest.mark.parametrize("key", _KEYS)
def test_every_adapter_loads_through_reviewer_state(key: str) -> None:
    """The script agents and the review-gate workflow run must accept every adapter."""
    assert _load_state().load_reviewer(key=key).key == key


@pytest.mark.parametrize("key", _KEYS)
def test_every_final_review_comment_is_allow_listed(key: str) -> None:
    comment = _load_config()["reviewers"][key]["final_review_comment"]
    if comment is None:
        pytest.skip(f"{key} has no final full review")
    settings = json.loads((_REPO / ".claude" / "settings.json").read_text())
    expected = f"Bash(gh pr comment * {comment}*)"
    assert expected in settings["permissions"]["allow"], (
        f"Add {expected!r} to .claude/settings.json permissions.allow so agents can "
        f"request the {key} reviewer's final full review without a prompt."
    )


@pytest.mark.parametrize("key", _KEYS)
def test_reviewer_notes_carry_the_switching_caveat(key: str) -> None:
    """An agent asked to switch reviewers reads these notes first."""
    text = (_NOTES_DIR / f"{key}.md").read_text().lower()
    assert "merge queue" in text and "review-gate" in text, (
        f"{key}.md must say that review-gate is the check to require and that a "
        "vendor's own check must never be required (it can wedge a merge queue)."
    )


@pytest.mark.parametrize("key", _KEYS)
def test_reviewer_notes_say_how_to_wire_in_the_guidelines(key: str) -> None:
    text = (_NOTES_DIR / f"{key}.md").read_text()
    assert ".github/review-guidelines.md" in text, (
        f"{key}.md must say how to wire the vendor-neutral noise filters "
        "(.github/review-guidelines.md) into that reviewer's own config."
    )


def test_no_vendor_config_directory_ships() -> None:
    """Noise filters live in a vendor-neutral file; no reviewer's own dir ships."""
    assert _GUIDELINES.is_file()
    assert not _tracked(".cursor"), "Keep vendor dirs out; see the reviewer notes."
    assert not _VENDOR_RE.search(_GUIDELINES.read_text())


@pytest.mark.parametrize("path", [_SKILL, _RULE], ids=lambda p: p.name)
def test_final_review_is_not_gated_on_a_pass_verdict_alone(path: Path) -> None:
    """A finding answered with pushback leaves the verdict at `findings` for good.

    If the final full review waited for `pass`, such a PR could never get it and the
    gate would stay red, so the prose must also allow "every finding fixed or
    answered".
    """
    assert "fixed or answered" in path.read_text()


def test_step_5_does_not_read_a_registered_check_as_a_started_review() -> None:
    """A vendor check can register at once as "Review skipped" and pass.

    So the start-check must look for review activity via reviewer_state.py, and a
    check that merely registered must not count as a started review.
    """
    skill = _SKILL.read_text()
    step5 = skill[skill.index("## Step 5: ") : skill.index("## Step 5a")]
    assert "reviewer_state.py" in step5
    assert "not evidence that a review started" in step5


def test_final_settle_accepts_answered_findings() -> None:
    skill = _SKILL.read_text()
    step6 = skill[skill.index("## Step 6: ") : skill.index("## Step 7: ")]
    assert "fixed or answered" in step6


@pytest.mark.parametrize("path", _seam_files(), ids=lambda p: str(p.relative_to(_REPO)))
def test_seam_files_do_not_name_a_vendor(path: Path) -> None:
    hits = [
        f"{lineno}: {line.strip()}"
        for lineno, line in enumerate(path.read_text().splitlines(), start=1)
        if _VENDOR_RE.search(line)
    ]
    assert not hits, (
        f"{path.relative_to(_REPO)} names a PR-reviewer vendor. Say 'the configured "
        "reviewer' and read .github/pr-reviewer.json; put vendor quirks in "
        f"{_NOTES_DIR.relative_to(_REPO)}/<key>.md:\n" + "\n".join(hits)
    )


def test_old_vendor_named_rule_is_gone() -> None:
    old = "bugbot-auto-review"
    assert not (_RULE.parent / f"{old}.md").exists()
    stale = [
        str(path.relative_to(_REPO))
        for path in _tracked(".agents", ".claude", "AGENTS.md", "README.md")
        if path.is_file() and old in path.read_text(errors="ignore")
    ]
    assert not stale, f"References to the renamed rule {old!r} remain: {stale}"


@pytest.mark.parametrize("key", _KEYS)
def test_every_preflight_command_is_allow_listed(key: str) -> None:
    """The allowance check runs before every local round, so it must never prompt."""
    preflight = _load_state().load_reviewer(key=key).local_preflight
    if preflight is None:
        pytest.skip(f"{key} declares no preflight")
    allow = json.loads((_REPO / ".claude" / "settings.json").read_text())[
        "permissions"
    ]["allow"]
    for command in (preflight.command, preflight.auth_command):
        expected = f"Bash({' '.join(command)}*)"
        assert expected in allow, (
            f"Add {expected!r} to .claude/settings.json permissions.allow so "
            f"review_preflight.py can read the {key} reviewer's capacity unprompted."
        )


def test_the_repo_config_loads_its_round_caps() -> None:
    """A misspelled review_rounds knob fails loudly instead of falling back."""
    caps = _load_state().load_round_caps()
    assert caps.local_before_final >= 1 and caps.local_after_final >= 1


# --- Wiring: the prose agents follow names the local rounds and the rate limit ---

_BUILD = _SKILLS / "build-from-issue" / "SKILL.md"


def _flat(path: Path) -> str:
    return " ".join(path.read_text().split())


def _section(text: str, heading: str) -> str:
    """Return ``heading``'s section, up to the next heading of any level."""
    section = text[text.index(heading) :]
    rest = section[len(heading) :]
    ends = [i for i in (rest.find(" ### "), rest.find(" ## ")) if i >= 0]
    return section[: len(heading) + min(ends)] if ends else section


def test_fix_rounds_run_locally_and_refusals_are_waited_out() -> None:
    for path in (_SKILL, _RULE, _BUILD):
        text = _flat(path)
        assert "local_review.py" in text, path.name
        assert "review_preflight.py" in text, path.name
    for path in (_SKILL, _RULE):
        text = _flat(path)
        assert "rate_limit.refused" in text, path.name
        assert "SAME request" in text, path.name


def test_the_rate_limit_rule_forbids_escalating_a_refused_request() -> None:
    section = _section(_flat(_SKILL), "### Review requests and the rate limit")
    assert "never escalate" in section.lower()
    assert "retry_after" in section
    assert "remedy" in section
    assert "recheck_after_seconds" in section
    # The local check reads a different pool, so it must never gate a PR request.
    assert "separate pool" in section
    assert "never PR requests" in section


def test_the_local_rounds_section_covers_every_status() -> None:
    section = _section(_flat(_SKILL), "### Local review rounds")
    for status in (
        "reviewed",
        "already_reviewed",
        "exhausted",
        "budget_spent",
        "head_mismatch",
        "failed",
        "not_configured",
    ):
        assert f"`{status}`" in section, status
    assert "max_local_before_final" in section
    assert "max_local_after_final" in section
    assert "draft" in section
    assert "record_body" in section


def test_a_local_reviewer_skips_the_trigger_step() -> None:
    skill = _SKILL.read_text()
    step5 = skill[skill.index("## Step 5: ") : skill.index("## Step 5a")]
    assert "local review skips this step" in step5
    step4d = skill[skill.index("### 4d.") : skill.index("## Step 5: ")]
    assert "local round on the new head" in step4d


def test_the_preflight_documents_that_it_never_gates_a_pr_request() -> None:
    script = _SKILLS / "resolve-pr-concerns" / "scripts" / "review_preflight.py"
    doc = " ".join(script.read_text().split('"""')[1].split())
    assert "never gates a PR review request" in doc
    assert "separate pool" in doc


# --- The speed guard's section shares this file (stack-neutral settings) ---

# test_speed field -> allowed JSON types. Numbers may be written as ints or floats.
_SPEED_FIELDS: dict[str, tuple[type, ...]] = {
    "budget_seconds": (int, float),
    "recheck_fraction": (int, float),
    "factor_low": (int, float),
    "factor_high": (int, float),
    "shared_setup_budget_seconds": (int, float),
    "default_runner_class": (str,),
    "reference_seconds": (dict,),
    "_note": (str,),
}


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def test_the_test_speed_section_is_well_formed() -> None:
    """``scripts/check_test_speed.py`` reads its budgets from here, both stacks."""
    speed = _load_config()["test_speed"]
    assert set(speed) == set(_SPEED_FIELDS), sorted(set(speed) ^ set(_SPEED_FIELDS))
    for field, types in _SPEED_FIELDS.items():
        value = speed[field]
        assert isinstance(value, types) and not isinstance(value, bool), field
    for field in ("budget_seconds", "shared_setup_budget_seconds", "factor_low"):
        assert speed[field] > 0, field
    assert 0 < speed["recheck_fraction"] <= 1
    assert speed["factor_low"] <= 1 <= speed["factor_high"]
    references = speed["reference_seconds"]
    assert references and all(
        isinstance(name, str) and name and _is_number(secs) and secs > 0
        for name, secs in references.items()
    ), references
    assert speed["default_runner_class"] in references
    assert "\n" not in speed["_note"] and speed["_note"].strip()


def test_the_test_speed_section_holds_the_agreed_budget() -> None:
    speed = _load_config()["test_speed"]
    assert speed["budget_seconds"] == 10
    assert (speed["factor_low"], speed["factor_high"]) == (0.5, 2.0)
    assert speed["default_runner_class"] == "ubuntu-latest"


def test_every_loader_accepts_the_real_config_with_test_speed() -> None:
    """The reviewer loaders ignore the speed guard's section in the real file."""
    assert "test_speed" in _load_config()
    state = _load_state()
    for key in _KEYS:
        assert state.load_reviewer(_CONFIG, key=key).key == key
    assert state.load_round_caps(_CONFIG).local_before_final >= 1


# --- The downstream review handling the prose must carry (helpers are bundled) ---

_STACK_REF = _SKILLS / "resolve-pr-concerns" / "references" / "stacked-prs.md"
_SH_HELPER_RE = re.compile(
    r"\b(?:human_review|wait_for_pr_checks)\.sh\b|scripts/\S+\.sh\b"
)


@pytest.mark.parametrize(
    "phrase",
    [
        "merge_gate.py",
        "round_budget",
        "incremental_review_outcome",
        "unthreaded_findings",
        "needs_trigger",
        "exit 3",
        "references/stacked-prs.md",
        "60 minutes",
        "trigger_allowed",
        "final_review_due",
        "min_remaining",
        "test_speed_check",
    ],
)
def test_the_skill_names_each_bundled_review_signal(phrase: str) -> None:
    assert phrase in _flat(_SKILL), phrase


def test_an_empty_live_set_is_not_proof_of_a_review() -> None:
    loop_guards = _section(_flat(_SKILL), "## Loop guards")
    assert "an empty" in loop_guards.lower() and "not proof" in loop_guards


def test_the_skill_points_at_the_python_helpers_not_the_retired_scripts() -> None:
    text = _SKILL.read_text()
    assert not _SH_HELPER_RE.search(text), _SH_HELPER_RE.findall(text)
    assert "scripts/human_review.py --repo" in text
    assert "scripts/wait_for_pr_checks.py <pr-number>" in text
    for path in _tracked(".agents/skills/resolve-pr-concerns"):
        if path.suffix == ".md":
            assert not _SH_HELPER_RE.search(path.read_text()), path.name


def test_merge_readiness_is_a_report_never_a_merge() -> None:
    step7 = _section(_flat(_SKILL), "## Step 7:")
    assert "merge_gate.py" in step7
    assert "one-off" in step7
    assert "never permission" in step7


def test_behind_blocks_only_under_the_up_to_date_rule() -> None:
    """merge_gate.py treats BEHIND as a blocker only while the rules are strict."""
    step2f = _section(_flat(_SKILL), "### 2f.")
    assert "up to date" in step2f and "merge queue" in step2f
    assert "CONFLICTING" in step2f and "DIRTY" in step2f


def test_the_stack_reference_keeps_landing_with_the_human() -> None:
    text = _flat(_STACK_REF)
    for phrase in (
        "merge_gate.py --stack",
        "gh stack merge",
        "never `gh pr merge`",
        "bottom-up",
        "one-off",
        "never installs",
        "4 layers",
        "300 files",
        "merge queue",
    ):
        assert phrase in text, phrase


def test_coderabbit_notes_hedge_the_status_type_and_cap_review_size() -> None:
    text = _flat(_NOTES_DIR / "coderabbit.md")
    assert "commit status" in text
    assert "300 files" in text


def test_bugbot_notes_explain_the_stale_summary() -> None:
    assert "BUGBOT_REVIEW_STALE" in (_NOTES_DIR / "bugbot.md").read_text()


def test_the_pr_round_budget_is_scoped_away_from_a_local_reviewer() -> None:
    """pr_round_caps zeroes a local reviewer's PR caps.

    So ``final_review_due`` reads true from the first read and ``trigger_allowed``
    false: the prose must route a local reviewer to ``local_review`` instead, or it
    would post its final review before any local round and stop fixing after it.
    """
    section = _section(_flat(_SKILL), "### Review-round budget")
    assert "govern PR-round reviewers only" in section
    assert "local_review.configured: true" in section
    step5a = _section(_flat(_SKILL), "## Step 5a:")
    assert "for a reviewer without a local review, `round_budget.final_review_due`" in (
        step5a
    )
