# Does this belong in AGENTS.md?

Default answer: **No.** AGENTS.md is always-on context (loaded by the main loop and
every subagent). Add a line only if BOTH are true:
1. It is universal and always-on (applies to essentially every task), AND
2. It is a command/imperative OR a routing pointer.

Otherwise route it:
- Convention or workflow norm → a rule in `.agents/rules/` (new or existing file),
  then a row in AGENTS.md's rules table only if it is always-on
  (`.agents/rules/shared/default-to-shareable-rules.md`).
- How a skill works → that skill's `SKILL.md` or its `references/`.
- Subsystem or architecture detail → the owning `docs/` spec or runbook.
- Repo-specific tuning of a vendored skill → `.agents/skill-config/<skill>.md`.

If adding would pass the soft cap, summarize or relocate existing content back
under first. Past a hard cap the file cannot ship: relocate until under.
