# Dependabot: major-version PRs only — design (#74)

Small change; short design.

## Intent

Dependabot opens a grouped minor/patch PR every week per ecosystem, plus each
major alone. The minor/patch churn costs a weekly review for little value. A
major left unnoticed is how a project ends up a whole SDK version behind when
the vendor removes APIs. A downstream repo settled on the middle ground
(2026-11-12): **version-update PRs for majors only; security updates always.**
The template, and every repo seeded from it, adopts the same policy.

## Goals

- Every `updates` entry in `.github/dependabot.yml` opens version-update PRs
  only for `version-update:semver-major`.
- Security updates keep opening for every update type. GitHub documents that
  `update-types` (including under `allow`) applies to version updates only.
- uv's 0.x minor releases are breaking (uv bumps the minor for breaking
  changes). Dependabot calls them "minor", so the python stack keeps getting
  `uv-build` minor PRs.
- A config guard test pins the policy, both in the template and in a repo
  seeded by `init`.
- Fold the two open Dependabot PRs the user picked: #65 (uv_build pin) and
  #72 (jsdom 31).

## Non-goals

- Syncing downstream repos: a post-merge follow-up under the
  template-propagation norm (that downstream repo already has the policy).
- Folding the open minor/patch PRs (#68–#71, #73). After merge they should
  auto-close; any that don't get closed with a comment pointing at this PR.
- Changing schedules, labels, commit prefixes or adding
  `open-pull-requests-limit` (the default of 5 already lets majors open).

## Approaches

1. **`allow` on semver-major per entry (picked).** What the issue specifies and
   what the downstream repo runs. One rule per entry, explicit, and security updates
   are documented as unaffected.
2. `ignore` on `version-update:semver-minor` and `semver-patch` for `*`.
   Equivalent effect, but it reads as "ignore everything" and diverges from
   downstream. Rejected.
3. Keep the minor/patch groups and lower the cadence (monthly). Still churn,
   and doesn't make majors stand out. Rejected.

## The design (decided with the user)

- Each of the four entries (uv `/stacks/python`, bun `/stacks/react`, uv `/`
  template block, github-actions) gets:
  ```yaml
  allow:
    - dependency-name: "*"
      update-types: ["version-update:semver-major"]
  ```
  exactly as in the issue, with **no `dependency-type`**. The user chose this
  knowingly: dependabot-core defaults an allow rule's type to "all", so
  majors of indirect dependencies may also get PRs.
- The uv `/stacks/python` entry gets a second allow rule (rules are OR'd):
  `{dependency-name: "uv-build", update-types: ["version-update:semver-minor"]}`.
  Dependabot stores Python names in normalized form (`uv-build`).
- Remove the minor/patch-only groups: `python-dev` (stack and template blocks),
  `react-dev`, and `github-actions`. They would never match anything.
- Keep the `vite` and `vitest` groups so their majors still move as a set.
- Keep the `# >>> STACK:` / `# <<< STACK:` markers intact.
- A header comment explains the policy. Per-entry comments stay short.
- `stacks/python/pyproject.toml`: `uv_build>=0.13.0,<0.14.0`, a single minor
  line matching the uv CI installs, with a comment saying why (supersedes #65's
  wider `>=0.12.0,<0.14.0`).
- `stacks/react`: jsdom `^31.0.0`, relocked with bun (supersedes #72).

## Guard test

New `tests/template/test_dependabot_policy.py`. It parses the YAML and checks
that:

- every entry's `allow` contains the `*` semver-major rule;
- the only other allow rule is the `uv-build` semver-minor rule, on the
  python-stack uv entry;
- no group restricts itself to minor/patch `update-types`;
- the `vite` and `vitest` groups survive.

`tests/template/test_initialize.py` gains the same allow assertion on the
seeded config for both stacks, so `init` output stays valid and on-policy.

## Left to the builder

- How the test parses YAML (PyYAML, or whatever the template tooling already
  depends on).
- Exact comment wording.
- Whether `make pr_check` for the stacks needs running in addition to the
  template's own checks, for the jsdom bump.
