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
import sys
import subprocess  # nosec B404 -- fixed argv, in-repo git, no shell
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
    "reviewed_sha_pattern": (str, None),
    "self_resolves_threads": (bool,),
    "summary_in_issue_comments": (bool,),
    # The final full review: all set, or all null for a reviewer agents can't ask
    # for one (reviewer_state.py enforces the all-or-none rule too).
    "final_review_comment": (str, None),
    "command_reply_marker": (str, None),
    "final_review_done_pattern": (str, None),
    "final_review_failed_pattern": (str, None),
    "final_review_partial_pattern": (str, None),
}
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
    config = _load_config()
    assert (
        config["active"] in config["reviewers"]
    ), f"{_CONFIG.relative_to(_REPO)}: 'active' must name a key in 'reviewers'."


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
        "reviewed_sha_pattern",
        "final_review_done_pattern",
        "final_review_failed_pattern",
        "final_review_partial_pattern",
    ):
        if adapter[field] is not None:
            re.compile(adapter[field])
    assert len({adapter[f] is None for f in _FINAL_REVIEW_FIELDS}) == 1, (
        f"reviewer {key!r}: {', '.join(_FINAL_REVIEW_FIELDS)} must all be set or "
        "all be null"
    )
    assert re.compile(adapter["findings_pattern"]).groups == 1, (
        f"reviewer {key!r}: findings_pattern needs exactly one capture group "
        "(the finding count)"
    )
    if adapter["reviewed_sha_pattern"] is not None:
        assert re.compile(adapter["reviewed_sha_pattern"]).groups == 1, (
            f"reviewer {key!r}: reviewed_sha_pattern needs exactly one capture "
            "group (the reviewed SHA)"
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
