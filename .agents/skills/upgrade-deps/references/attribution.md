# Attribution

`upgrade-deps` was written for this template (issue #52). No public skill could be
used as-is (surveyed 2026-10-29: anthropics/skills, obra/superpowers,
claude-plugins-official, skills.sh, GitHub code search). These ideas were adapted
from public skills; nothing was copied verbatim.

| Idea | Source | License |
|---|---|---|
| Use the previous refresh PR's body as the state between runs (deferred majors and why) | [JoshuaC215/agent-service-toolkit `dependency-refresh`](https://github.com/JoshuaC215/agent-service-toolkit/tree/main/.claude/skills/dependency-refresh) | MIT |
| Verify each alert against the versions in the lockfile instead of the Dependabot UI; the remove → raise floor → targeted upgrade → override ladder | [mflux-community/mflux `remediating-dependabot`](https://github.com/mflux-community/mflux/tree/main/.agents/skills/remediating-dependabot) | MIT |
| Roll back a single failing bump and continue, rather than abandoning the run | [rshade/agent-skills `dep-upgrade`](https://www.skills.sh/rshade/agent-skills/dep-upgrade) | Apache-2.0 |
