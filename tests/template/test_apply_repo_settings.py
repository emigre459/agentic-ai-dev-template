import json
from pathlib import Path
from types import SimpleNamespace

from scripts.apply_repo_settings import (
    find_main_ruleset,
    ruleset_matches,
    merge_settings_match,
    security_settings_match,
    plan_actions,
    load_desired,
    _current_rulesets,
)

REPO_SETTINGS = Path(__file__).resolve().parents[2] / ".github" / "repo-settings"


def test_load_desired_reads_all_files() -> None:
    desired = load_desired(REPO_SETTINGS)
    assert desired.ruleset["name"] == "main"
    assert desired.merge["allow_squash_merge"] is True
    assert (
        desired.security["security_and_analysis"]["dependabot_security_updates"][
            "status"
        ]
        == "enabled"
    )


def test_current_rulesets_fetches_full_detail_for_main_only() -> None:
    # The list endpoint returns bare summaries (no rules/conditions) — full
    # detail must come from a single-ruleset fetch, and only for "main" (the
    # only one we ever compare against desired state).
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(cmd)
        body: object
        if cmd[-1] == "repos/acme/repo/rulesets":
            body = [{"id": 1, "name": "other"}, {"id": 2, "name": "main"}]
        elif cmd[-1] == "repos/acme/repo/rulesets/2":
            body = {"id": 2, "name": "main", "rules": [{"type": "deletion"}]}
        else:
            raise AssertionError(f"unexpected call: {cmd}")
        return SimpleNamespace(stdout=json.dumps(body))

    result = _current_rulesets("acme/repo", fake_runner)
    assert result[0] == {"id": 1, "name": "other"}
    assert result[1] == {"id": 2, "name": "main", "rules": [{"type": "deletion"}]}
    assert len(calls) == 2  # one list call + one detail call for "main" only


def test_find_main_ruleset_returns_match() -> None:
    existing = [{"id": 1, "name": "other"}, {"id": 2, "name": "main"}]
    result = find_main_ruleset(existing)
    assert result is not None
    assert result["id"] == 2


def test_find_main_ruleset_none_when_absent() -> None:
    assert find_main_ruleset([{"id": 1, "name": "develop"}]) is None


def test_ruleset_matches_true_when_rules_equal() -> None:
    desired = {"name": "main", "enforcement": "active", "rules": [{"type": "deletion"}]}
    current = {
        "name": "main",
        "enforcement": "active",
        "rules": [{"type": "deletion"}],
        "id": 99,
        "created_at": "x",
    }
    assert ruleset_matches(desired, current) is True


def test_ruleset_matches_false_when_rules_differ() -> None:
    desired = {"name": "main", "enforcement": "active", "rules": [{"type": "deletion"}]}
    current = {"name": "main", "enforcement": "active", "rules": []}
    assert ruleset_matches(desired, current) is False


def test_ruleset_matches_true_when_current_has_api_defaults() -> None:
    # GitHub returns rules/conditions with extra default-populated fields our
    # sanitized ruleset.json omits — subset matching must still consider it aligned.
    desired = {
        "name": "main",
        "enforcement": "active",
        "rules": [
            {"type": "deletion"},
            {
                "type": "pull_request",
                "parameters": {"allowed_merge_methods": ["squash"]},
            },
        ],
    }
    current = {
        "name": "main",
        "enforcement": "active",
        "id": 7,
        "created_at": "2026-01-01",
        "rules": [
            {"type": "deletion"},
            {
                "type": "pull_request",
                "parameters": {
                    "allowed_merge_methods": ["squash"],
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": True,
                },
            },
            {"type": "non_fast_forward"},
        ],
    }
    assert ruleset_matches(desired, current) is True


def test_merge_settings_match_ignores_extra_current_keys() -> None:
    desired = {"allow_squash_merge": True, "allow_merge_commit": False}
    current = {"allow_squash_merge": True, "allow_merge_commit": False, "extra": 1}
    assert merge_settings_match(desired, current) is True


def test_security_settings_match_true_when_enabled() -> None:
    desired = {
        "security_and_analysis": {"dependabot_security_updates": {"status": "enabled"}}
    }
    current_repo = {
        "security_and_analysis": {
            "dependabot_security_updates": {"status": "enabled"},
            "secret_scanning": {"status": "enabled"},
        }
    }
    assert security_settings_match(desired, current_repo) is True


def test_security_settings_match_false_when_disabled() -> None:
    desired = {
        "security_and_analysis": {"dependabot_security_updates": {"status": "enabled"}}
    }
    current_repo = {
        "security_and_analysis": {"dependabot_security_updates": {"status": "disabled"}}
    }
    assert security_settings_match(desired, current_repo) is False


_ALIGNED_SECURITY = {
    "security_and_analysis": {"dependabot_security_updates": {"status": "enabled"}}
}


def test_plan_actions_post_when_no_ruleset() -> None:
    desired_rs = {"name": "main", "enforcement": "active", "rules": []}
    desired_merge = {"allow_squash_merge": True}
    actions = plan_actions(
        current_rulesets=[],
        current_merge={
            "allow_squash_merge": False,
            "security_and_analysis": {
                "dependabot_security_updates": {"status": "disabled"}
            },
        },
        desired_ruleset=desired_rs,
        desired_merge=desired_merge,
        desired_security=_ALIGNED_SECURITY,
    )
    assert ("ruleset", "POST", None) in actions
    assert any(a[0] == "merge" and a[1] == "PATCH" for a in actions)
    assert any(a[0] == "security" and a[1] == "PATCH" for a in actions)


def test_plan_actions_put_when_main_exists_and_differs() -> None:
    desired_rs = {
        "name": "main",
        "enforcement": "active",
        "rules": [{"type": "deletion"}],
    }
    actions = plan_actions(
        current_rulesets=[
            {"id": 7, "name": "main", "enforcement": "active", "rules": []}
        ],
        current_merge={"allow_squash_merge": True, **_ALIGNED_SECURITY},
        desired_ruleset=desired_rs,
        desired_merge={"allow_squash_merge": True},
        desired_security=_ALIGNED_SECURITY,
    )
    assert ("ruleset", "PUT", 7) in actions
    assert all(a[0] != "merge" for a in actions)  # merge already aligned → no-op
    assert all(a[0] != "security" for a in actions)  # security already aligned → no-op


def test_plan_actions_patch_when_security_out_of_date() -> None:
    desired_rs = {
        "name": "main",
        "enforcement": "active",
        "rules": [{"type": "deletion"}],
    }
    actions = plan_actions(
        current_rulesets=[
            {
                "id": 7,
                "name": "main",
                "enforcement": "active",
                "rules": [{"type": "deletion"}],
            }
        ],
        current_merge={
            "allow_squash_merge": True,
            "security_and_analysis": {
                "dependabot_security_updates": {"status": "disabled"}
            },
        },
        desired_ruleset=desired_rs,
        desired_merge={"allow_squash_merge": True},
        desired_security=_ALIGNED_SECURITY,
    )
    assert actions == [("security", "PATCH", None)]


def test_plan_actions_all_noop_when_aligned() -> None:
    desired_rs = {
        "name": "main",
        "enforcement": "active",
        "rules": [{"type": "deletion"}],
    }
    actions = plan_actions(
        current_rulesets=[
            {
                "id": 7,
                "name": "main",
                "enforcement": "active",
                "rules": [{"type": "deletion"}],
            }
        ],
        current_merge={"allow_squash_merge": True, **_ALIGNED_SECURITY},
        desired_ruleset=desired_rs,
        desired_merge={"allow_squash_merge": True},
        desired_security=_ALIGNED_SECURITY,
    )
    assert actions == []
