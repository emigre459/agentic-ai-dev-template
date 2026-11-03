import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.apply_repo_settings import (
    DEFERRED_RULE_TYPES,
    _current_labels,
    _current_rulesets,
    _gh_json,
    _is_merge_queue_rejection,
    find_main_ruleset,
    load_desired,
    main,
    merge_queue_eligibility,
    merge_settings_match,
    plan_actions,
    ruleset_for_phase,
    ruleset_matches,
    security_settings_match,
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
    assert [label["name"] for label in desired.labels] == ["patch", "minor", "major"]


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
    desired: dict = {
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


def test_bootstrap_plan_removes_preexisting_required_checks() -> None:
    """Do not leave a first PR blocked when a copied ruleset already has CI gates."""
    bootstrap = {"name": "main", "rules": [{"type": "pull_request"}]}
    current = {
        "id": 7,
        "name": "main",
        "rules": [
            {"type": "pull_request"},
            {"type": "required_status_checks"},
        ],
    }

    actions = plan_actions(
        current_rulesets=[current],
        current_merge={"allow_squash_merge": True, **_ALIGNED_SECURITY},
        desired_ruleset=bootstrap,
        desired_merge={"allow_squash_merge": True},
        desired_security=_ALIGNED_SECURITY,
        forbidden_rule_types={"required_status_checks"},
    )

    assert actions == [("ruleset", "PUT", 7)]


def test_bootstrap_phase_defers_required_status_checks() -> None:
    """Keep the first PR mergeable until its CI workflow reaches main."""
    desired: dict = {
        "name": "main",
        "rules": [
            {"type": "pull_request", "parameters": {}},
            {"type": "required_status_checks", "parameters": {}},
        ],
    }

    bootstrap = ruleset_for_phase(desired, "bootstrap")
    final = ruleset_for_phase(desired, "final")

    assert [rule["type"] for rule in bootstrap["rules"]] == ["pull_request"]
    assert [rule["type"] for rule in final["rules"]] == [
        "pull_request",
        "required_status_checks",
    ]
    assert desired["rules"][1]["type"] == "required_status_checks"


def test_gh_json_reports_github_error_in_plain_language() -> None:
    """Expose GitHub's useful stderr instead of a raw Python traceback."""

    def failing_runner(*args: object, **kwargs: object) -> NoReturn:
        """Raise the same error subprocess.run raises for a rejected API call."""
        raise subprocess.CalledProcessError(
            1,
            ["gh", "api", "repos/acme/repo/rulesets"],
            stderr="gh: Resource not accessible by integration (HTTP 403)",
        )

    with pytest.raises(RuntimeError, match="Resource not accessible.*administrator"):
        _gh_json(["api", "repos/acme/repo/rulesets"], failing_runner)


def test_gh_json_reports_missing_cli_in_plain_language() -> None:
    """Tell the operator how to install/authenticate gh when it is unavailable."""

    def missing_runner(*args: object, **kwargs: object) -> NoReturn:
        """Raise the error subprocess.run emits when gh is not on PATH."""
        raise FileNotFoundError("gh")

    with pytest.raises(RuntimeError, match="GitHub CLI.*gh auth login"):
        _gh_json(["api", "repos/acme/repo"], missing_runner)


# --- merge queue (#53) ----------------------------------------------------------


def test_shipped_ruleset_has_a_squash_merge_queue() -> None:
    """Merges go through a queue that tests each PR against the latest main."""
    ruleset = load_desired(REPO_SETTINGS).ruleset
    [queue] = [r for r in ruleset["rules"] if r["type"] == "merge_queue"]
    params = queue["parameters"]
    assert params["merge_method"] == "SQUASH"
    assert params["grouping_strategy"] == "ALLGREEN"
    assert params["min_entries_to_merge"] == 1
    assert params["max_entries_to_merge"] >= params["min_entries_to_merge"]
    assert params["max_entries_to_build"] >= 1
    assert 1 <= params["check_response_timeout_minutes"] <= 360


def test_shipped_ruleset_does_not_require_up_to_date_branches() -> None:
    """The queue replaces strict up-to-date checks; both would mean catch-up churn."""
    ruleset = load_desired(REPO_SETTINGS).ruleset
    [checks] = [r for r in ruleset["rules"] if r["type"] == "required_status_checks"]
    assert checks["parameters"]["strict_required_status_checks_policy"] is False


def test_bootstrap_phase_defers_the_merge_queue() -> None:
    """A queue with no required checks would merge untested; defer both together."""
    desired: dict = {
        "name": "main",
        "rules": [
            {"type": "pull_request", "parameters": {}},
            {"type": "required_status_checks", "parameters": {}},
            {"type": "merge_queue", "parameters": {}},
        ],
    }
    bootstrap = ruleset_for_phase(desired, "bootstrap")
    final = ruleset_for_phase(desired, "final")
    assert [r["type"] for r in bootstrap["rules"]] == ["pull_request"]
    assert [r["type"] for r in final["rules"]] == [
        "pull_request",
        "required_status_checks",
        "merge_queue",
    ]


def test_bootstrap_plan_removes_a_live_merge_queue() -> None:
    current = {
        "id": 7,
        "name": "main",
        "rules": [{"type": "pull_request"}, {"type": "merge_queue"}],
    }
    actions = plan_actions(
        current_rulesets=[current],
        current_merge={"allow_squash_merge": True, **_ALIGNED_SECURITY},
        desired_ruleset={"name": "main", "rules": [{"type": "pull_request"}]},
        desired_merge={"allow_squash_merge": True},
        desired_security=_ALIGNED_SECURITY,
        forbidden_rule_types=DEFERRED_RULE_TYPES,
    )
    assert actions == [("ruleset", "PUT", 7)]


def test_deferred_rule_types_cover_checks_and_queue() -> None:
    assert {"required_status_checks", "merge_queue"} <= DEFERRED_RULE_TYPES


def _repo(owner_type: str, visibility: str) -> dict:
    return {
        "owner": {"login": "acme", "type": owner_type},
        "visibility": visibility,
        "private": visibility != "public",
    }


@pytest.mark.parametrize(
    ("repo", "org", "expected"),
    [
        (_repo("User", "public"), None, "no"),
        (_repo("User", "private"), None, "no"),
        (_repo("Organization", "public"), None, "yes"),
        (_repo("Organization", "private"), {"plan": {"name": "enterprise"}}, "yes"),
        (_repo("Organization", "private"), {"plan": {"name": "team"}}, "no"),
        (_repo("Organization", "private"), {"plan": {"name": "free"}}, "no"),
        (_repo("Organization", "private"), {"login": "acme"}, "unknown"),
        (_repo("Organization", "private"), None, "unknown"),
        (_repo("Organization", "internal"), {"plan": {"name": "enterprise"}}, "yes"),
    ],
    ids=[
        "user-public",
        "user-private",
        "org-public",
        "org-private-enterprise",
        "org-private-team",
        "org-private-free",
        "org-private-plan-hidden",
        "org-private-org-unreadable",
        "org-internal-enterprise",
    ],
)
def test_merge_queue_eligibility(repo: dict, org: dict | None, expected: str) -> None:
    verdict, reason = merge_queue_eligibility(repo, org)
    assert verdict == expected
    assert reason  # every verdict explains itself in the notice


_DESIRED_LABELS = json.loads((REPO_SETTINGS / "labels.json").read_text())


class _FakeGh:
    """A stub `gh` for main(): serves GETs, records writes, can reject merge_queue."""

    def __init__(
        self,
        repo: dict,
        org: dict | None = None,
        rulesets: list | None = None,
        reject_merge_queue: bool = False,
        fail_ruleset_with: str | None = None,
        labels: list | None = None,
    ) -> None:
        self.repo, self.org, self.rulesets = repo, org, rulesets or []
        self.labels = labels if labels is not None else list(_DESIRED_LABELS)
        self.label_writes: list[tuple[str, str, dict]] = []
        self.reject_merge_queue = reject_merge_queue
        self.fail_ruleset_with = fail_ruleset_with
        self.calls: list[list[str]] = []
        self.ruleset_writes: list[dict] = []

    def __call__(self, cmd: list[str], **kwargs: object) -> SimpleNamespace:
        self.calls.append(cmd)
        args = cmd[1:]
        if "--method" not in args:
            endpoint = args[-1]
            if endpoint == "repos/acme/repo/rulesets":
                body: object = [
                    {"id": r["id"], "name": r["name"]} for r in self.rulesets
                ]
            elif endpoint.startswith("repos/acme/repo/rulesets/"):
                body = self.rulesets[0]
            elif endpoint == "repos/acme/repo":
                body = self.repo
            elif endpoint.startswith("repos/acme/repo/labels?"):
                query = parse_qs(urlsplit(endpoint).query)
                page, size = int(query["page"][0]), int(query["per_page"][0])
                body = self.labels[(page - 1) * size : page * size]
            elif endpoint == "orgs/acme":
                if self.org is None:
                    raise subprocess.CalledProcessError(
                        1, cmd, stderr="gh: Not Found (HTTP 404)"
                    )
                body = self.org
            else:
                raise AssertionError(f"unexpected GET {cmd}")
            return SimpleNamespace(stdout=json.dumps(body))
        method = args[args.index("--method") + 1]
        endpoint = args[args.index("--method") + 2]
        if "/labels" in endpoint:
            self.label_writes.append(
                (method, endpoint, json.loads(str(kwargs["input"])))
            )
        elif "/rulesets" in endpoint:
            payload = json.loads(str(kwargs["input"]))
            self.ruleset_writes.append(payload)
            types = {r["type"] for r in payload["rules"]}
            if self.fail_ruleset_with:
                raise subprocess.CalledProcessError(
                    1, cmd, stderr=self.fail_ruleset_with
                )
            if self.reject_merge_queue and "merge_queue" in types:
                raise subprocess.CalledProcessError(
                    1,
                    cmd,
                    stderr="gh: Validation Failed: Invalid rule 'merge_queue': (HTTP 422)",
                )
        return SimpleNamespace(stdout="")


def _rule_types(payload: dict) -> list[str]:
    return [r["type"] for r in payload["rules"]]


def test_main_skips_the_queue_on_a_user_owned_repo(
    capsys: pytest.CaptureFixture,
) -> None:
    gh = _FakeGh(_repo("User", "public"))
    assert main(["--repo", "acme/repo", "--yes"], runner=gh) == 0
    [written] = gh.ruleset_writes
    assert "merge_queue" not in _rule_types(written)
    assert "required_status_checks" in _rule_types(written)
    out = capsys.readouterr().out
    assert "no merge queue" in out and "organization" in out
    assert not any(c[-1] == "orgs/acme" for c in gh.calls)


def test_a_skipped_queue_stays_a_no_op_on_rerun() -> None:
    """Idempotent: once applied without the queue, a rerun plans no ruleset change."""
    first = _FakeGh(_repo("User", "public"))
    main(["--repo", "acme/repo", "--yes"], runner=first)
    live = {"id": 9, **first.ruleset_writes[0]}
    second = _FakeGh(_repo("User", "public"), rulesets=[live])
    main(["--repo", "acme/repo", "--yes"], runner=second)
    assert second.ruleset_writes == []


def test_main_sends_the_queue_on_an_org_public_repo() -> None:
    gh = _FakeGh(_repo("Organization", "public"))
    assert main(["--repo", "acme/repo", "--yes"], runner=gh) == 0
    [written] = gh.ruleset_writes
    assert "merge_queue" in _rule_types(written)
    assert not any(c[-1] == "orgs/acme" for c in gh.calls)


def test_main_reads_the_org_plan_only_for_a_private_org_repo() -> None:
    gh = _FakeGh(_repo("Organization", "private"), org={"plan": {"name": "team"}})
    assert main(["--repo", "acme/repo", "--yes"], runner=gh) == 0
    assert any(c[-1] == "orgs/acme" for c in gh.calls)
    assert "merge_queue" not in _rule_types(gh.ruleset_writes[0])


def test_main_retries_without_the_queue_when_github_rejects_it(
    capsys: pytest.CaptureFixture,
) -> None:
    """Plan hidden from the caller: try it, and fall back on GitHub's 422."""
    gh = _FakeGh(_repo("Organization", "private"), reject_merge_queue=True)
    assert main(["--repo", "acme/repo", "--yes"], runner=gh) == 0
    assert [("merge_queue" in _rule_types(w)) for w in gh.ruleset_writes] == [
        True,
        False,
    ]
    assert "no merge queue" in capsys.readouterr().out


def test_main_fails_hard_on_any_other_ruleset_error() -> None:
    gh = _FakeGh(
        _repo("Organization", "private"),
        fail_ruleset_with="gh: Resource not accessible by integration (HTTP 403)",
    )
    assert main(["--repo", "acme/repo", "--yes"], runner=gh) == 2
    assert len(gh.ruleset_writes) == 1  # no blind retry


def test_main_does_not_retry_a_rejection_on_a_known_eligible_repo() -> None:
    """A 422 on a repo we know has queues is a real error, not ineligibility."""
    gh = _FakeGh(_repo("Organization", "public"), reject_merge_queue=True)
    assert main(["--repo", "acme/repo", "--yes"], runner=gh) == 2
    assert len(gh.ruleset_writes) == 1


def test_bootstrap_never_asks_about_the_queue() -> None:
    gh = _FakeGh(_repo("Organization", "private"))
    assert (
        main(["--repo", "acme/repo", "--phase", "bootstrap", "--yes"], runner=gh) == 0
    )
    assert not any(c[-1] == "orgs/acme" for c in gh.calls)
    assert "merge_queue" not in _rule_types(gh.ruleset_writes[0])


def test_a_rejection_named_only_in_the_response_body_is_recognized() -> None:
    """`gh api` prints the error body (naming the rule) to stdout, a summary to stderr."""

    class _BodyOnly(_FakeGh):
        def __call__(self, cmd: list[str], **kwargs: object) -> SimpleNamespace:
            if "--method" in cmd and "/rulesets" in " ".join(cmd):
                payload = json.loads(str(kwargs["input"]))
                self.ruleset_writes.append(payload)
                if "merge_queue" in _rule_types(payload):
                    raise subprocess.CalledProcessError(
                        1,
                        cmd,
                        output='{"message":"Validation Failed","errors":'
                        "[\"Invalid rule 'merge_queue': \"]}",
                        stderr="gh: Validation Failed (HTTP 422)",
                    )
                return SimpleNamespace(stdout="")
            return super().__call__(cmd, **kwargs)

    gh = _BodyOnly(_repo("Organization", "private"))
    assert main(["--repo", "acme/repo", "--yes"], runner=gh) == 0
    assert [("merge_queue" in _rule_types(w)) for w in gh.ruleset_writes] == [
        True,
        False,
    ]


def test_a_rejected_queue_settles_on_rerun(capsys: pytest.CaptureFixture) -> None:
    """Plan hidden + GitHub said no once: a rerun must not re-send and re-fail."""
    first = _FakeGh(_repo("Organization", "private"), reject_merge_queue=True)
    assert main(["--repo", "acme/repo", "--yes"], runner=first) == 0
    live = {"id": 9, **first.ruleset_writes[-1]}
    capsys.readouterr()
    second = _FakeGh(
        _repo("Organization", "private"), rulesets=[live], reject_merge_queue=True
    )
    assert main(["--repo", "acme/repo", "--yes"], runner=second) == 0
    assert second.ruleset_writes == []
    out = capsys.readouterr().out
    assert "organization owner" in out


def test_a_first_finalize_with_hidden_plan_still_tries_the_queue() -> None:
    """The bootstrap ruleset lacks the required checks too, so the queue is tried."""
    bootstrap_live = {
        "id": 9,
        **ruleset_for_phase(load_desired(REPO_SETTINGS).ruleset, "bootstrap"),
    }
    gh = _FakeGh(_repo("Organization", "private"), rulesets=[bootstrap_live])
    assert main(["--repo", "acme/repo", "--yes"], runner=gh) == 0
    [written] = gh.ruleset_writes
    assert "merge_queue" in _rule_types(written)


@pytest.mark.parametrize(
    "detail",
    [
        "gh: Validation Failed (HTTP 422) Invalid rule 'merge_queue'",
        "gh: Validation Failed (HTTP 422) Merge queue is not available for this repository",
        "gh: Validation Failed (HTTP 422) merge queue rules are not supported",
    ],
)
def test_queue_rejections_are_recognized(detail: str) -> None:
    assert _is_merge_queue_rejection(RuntimeError(detail))


@pytest.mark.parametrize(
    "detail",
    [
        "gh: Validation Failed (HTTP 422) Invalid rule 'required_status_checks'",
        "gh: Resource not accessible by integration (HTTP 403) merge_queue",
    ],
)
def test_other_errors_are_not_queue_rejections(detail: str) -> None:
    assert not _is_merge_queue_rejection(RuntimeError(detail))


# --- bump labels (release-drafter reads minor / major; no label means patch) ---

_ALIGNED_REPO = {"allow_squash_merge": True, **_ALIGNED_SECURITY}


def _label_plan(
    current_labels: list[dict], desired_labels: list[dict]
) -> Sequence[tuple[str, str, int | str | None]]:
    """Plan with everything but labels aligned, so only label actions remain."""
    return plan_actions(
        current_rulesets=[],
        current_merge=_ALIGNED_REPO,
        desired_ruleset={},
        desired_merge={"allow_squash_merge": True},
        desired_security=_ALIGNED_SECURITY,
        desired_labels=desired_labels,
        current_labels=current_labels,
    )[
        1:
    ]  # drop the ("ruleset", "POST", None) an empty live ruleset list implies


_MINOR = {"name": "minor", "color": "0e8a16", "description": "Bump the minor version"}
_MAJOR = {"name": "major", "color": "b60205", "description": "Bump the major version"}


def test_shipped_labels_are_the_three_bump_labels() -> None:
    labels = {label["name"]: label for label in load_desired(REPO_SETTINGS).labels}
    assert set(labels) == {"patch", "minor", "major"}
    assert len({label["color"] for label in labels.values()}) == 3  # distinct
    for label in labels.values():
        assert not label["color"].startswith("#")  # the API wants bare hex
        assert label["description"]


def test_plan_creates_missing_labels() -> None:
    actions = _label_plan(current_labels=[_MINOR], desired_labels=[_MINOR, _MAJOR])
    assert actions == [("label", "POST", "major")]


def test_plan_updates_changed_label_color() -> None:
    live = {**_MINOR, "color": "ffffff"}
    actions = _label_plan(current_labels=[live], desired_labels=[_MINOR])
    assert actions == [("label", "PATCH", "minor")]


def test_plan_updates_changed_label_description() -> None:
    live = {**_MINOR, "description": "something else"}
    actions = _label_plan(current_labels=[live], desired_labels=[_MINOR])
    assert actions == [("label", "PATCH", "minor")]


def test_plan_treats_a_missing_description_as_empty() -> None:
    """GitHub returns null for a label with no description."""
    undescribed = {"name": "minor", "color": "0e8a16", "description": ""}
    live = {**undescribed, "description": None}
    assert _label_plan([live], [undescribed]) == []


def test_plan_ignores_label_color_case() -> None:
    live = {**_MINOR, "color": _MINOR["color"].upper()}
    assert _label_plan([live], [_MINOR]) == []


def test_plan_never_deletes_extra_labels() -> None:
    extras = [{"name": "bug", "color": "d73a4a", "description": "Something broke"}]
    assert _label_plan(current_labels=[_MINOR, *extras], desired_labels=[_MINOR]) == []


def test_plan_matches_label_names_exactly() -> None:
    """`Minor` is a different label from `minor`; the bump label is still missing."""
    live = {**_MINOR, "name": "Minor"}
    assert _label_plan([live], [_MINOR]) == [("label", "POST", "minor")]


def test_current_labels_follows_pagination() -> None:
    pages = {1: [_MINOR, _MAJOR], 2: [{"name": "bug", "color": "d73a4a"}], 3: []}
    seen: list[str] = []

    def fake_runner(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        seen.append(cmd[-1])
        page = int(parse_qs(urlsplit(cmd[-1]).query)["page"][0])
        return SimpleNamespace(stdout=json.dumps(pages[page]))

    labels = _current_labels("acme/repo", fake_runner, per_page=2)
    assert [label["name"] for label in labels] == ["minor", "major", "bug"]
    assert seen == [
        "repos/acme/repo/labels?per_page=2&page=1",
        "repos/acme/repo/labels?per_page=2&page=2",
    ]


def test_bootstrap_phase_includes_labels() -> None:
    gh = _FakeGh(_repo("Organization", "public"), labels=[])
    assert (
        main(["--repo", "acme/repo", "--phase", "bootstrap", "--yes"], runner=gh) == 0
    )
    assert [w[2]["name"] for w in gh.label_writes] == ["patch", "minor", "major"]


def test_final_phase_includes_labels() -> None:
    gh = _FakeGh(_repo("Organization", "public"), labels=[])
    assert main(["--repo", "acme/repo", "--yes"], runner=gh) == 0
    assert [w[2]["name"] for w in gh.label_writes] == ["patch", "minor", "major"]


def test_apply_creates_labels_via_gh() -> None:
    gh = _FakeGh(_repo("Organization", "public"), labels=[])
    main(["--repo", "acme/repo", "--yes"], runner=gh)
    method, endpoint, payload = gh.label_writes[0]
    assert (method, endpoint) == ("POST", "repos/acme/repo/labels")
    assert set(payload) == {"name", "color", "description"}


def test_apply_updates_a_changed_label_via_gh() -> None:
    stale = [{**label, "color": "ffffff"} for label in _DESIRED_LABELS]
    gh = _FakeGh(_repo("Organization", "public"), labels=stale)
    main(["--repo", "acme/repo", "--yes"], runner=gh)
    assert {(m, e) for m, e, _ in gh.label_writes} == {
        ("PATCH", "repos/acme/repo/labels/patch"),
        ("PATCH", "repos/acme/repo/labels/minor"),
        ("PATCH", "repos/acme/repo/labels/major"),
    }


def test_label_names_are_url_encoded_in_the_update_endpoint(tmp_path: Path) -> None:
    odd = {"name": "needs review", "color": "ffffff", "description": "x"}
    settings = tmp_path / "repo-settings"
    shutil.copytree(REPO_SETTINGS, settings)
    (settings / "labels.json").write_text(json.dumps([{**odd, "color": "000000"}]))
    gh = _FakeGh(_repo("Organization", "public"), labels=[odd])
    main(["--repo", "acme/repo", "--yes", "--settings-dir", str(settings)], runner=gh)
    assert gh.label_writes[0][1] == "repos/acme/repo/labels/needs%20review"


def test_labels_stay_a_no_op_on_rerun() -> None:
    gh = _FakeGh(_repo("Organization", "public"))  # live labels == desired labels
    main(["--repo", "acme/repo", "--yes"], runner=gh)
    assert gh.label_writes == []
