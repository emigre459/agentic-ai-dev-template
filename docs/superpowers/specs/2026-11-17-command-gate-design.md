# Hardened, harness-neutral command gate — design

Issue: #50 (epic #51, independent parallel track). Date: 2026-11-17.

## Intent

The template's shell permission hook (`.claude/hooks/claude_permission_hook.sh`)
auto-approves a command when the whole string starts with an allow-listed prefix,
so `uv run pytest && rm -rf /` rides through on `uv run *`. A downstream repo
replaced it with a parser-based gate and later hardened that gate against several
bypasses. This change brings that gate into the template so every repo built from
it, on every agent harness that documents a blocking pre-tool hook, gets the same
protection.

The 2026-11-11 amendment adds a second job. The gate also **denies** shell commands
and file edits that contain a repo-owned **banned pattern** (for example a
deprecated SDK call or a raw endpoint URL). A CI grep only sees committed code. The
downstream repo kept tripping a vendor's deprecation alarm from one-off agent
scripts that were run but never committed, and blocking at the tool call stops
them before they run.

## Goals

1. One harness-neutral gate takes a proposed shell command plus the allow-list and
   returns **allow** or **no opinion**. It allows only a command it fully parsed,
   where every executed subcommand (including inside `$(...)`, backticks and
   process substitution) is allow-listed and every output redirect resolves inside
   an allowed write root.
2. The gate covers these bypasses, each with a regression test:
   - redirects glued to the previous word (`echo x>/etc/evil`), and `>&file` /
     `&>file`;
   - `#` comments, where a `'` inside a comment is not a quote, and a `#` that is
     mid-word is not a comment;
   - ANSI-C `$'...'` strings, which fail closed;
   - relative redirect targets after `cd` / `pushd` / `popd`, which no longer
     resolve against the hook's directory, so they fail closed;
   - leading `NAME=value` assignments, which are stripped only for allow-listed
     names (`LD_PRELOAD=… cmd`, `GIT_SSH_COMMAND=… cmd` and `PATH=…; cmd` no
     longer ride through).
3. A repo-owned banned-patterns list makes the gate **deny** a matching shell
   command or file write/edit. The deny message names the pattern and the repo's
   replacement. The template ships the list empty.
4. Thin registrations wire the gate into each harness whose vendor docs confirm
   the payload and decision format. The README lists coverage for every surveyed
   harness.

## Non-goals

- Distributing hooks through the `skills` CLI. It manages skills only (the card
  says so explicitly).
- Harnesses whose payload field names couldn't be confirmed from vendor docs:
  Windsurf/Devin legacy, Cline, Antigravity, Kiro and Amp. They go to a follow-up
  card (see "Follow-ups").
- The `run-tests-on-stop.sh` Stop hook. It is unchanged.
- Migrating existing downstream repos. They adopt the new files by hand. This PR
  ships the template copy.

## Anti-goals (a failure even if it "works")

- **Ever auto-approving on uncertainty.** A parse doubt, unknown token, unreadable
  policy, unknown payload shape or internal exception must produce *no opinion*,
  never `allow`.
- **Turning a crash into a deny.** Copilot treats a crashed or non-zero hook as a
  deny. The gate must catch its own errors and exit 0 with no output, so a bug
  never locks a session out. The single exception is a banned-pattern hit, which
  denies on purpose.
- **Emitting a bare `allow` where a harness rejects it.** Codex logs a bare
  `permissionDecision: "allow"` on `PreToolUse` as a failed hook, and Augment
  doesn't support allow yet.
- **Naming a harness inside the gate's decision logic.** `command_gate.py` knows
  nothing about harnesses.
- **Leaking downstream organization names** into the template. The existing
  `test_no_org_names` enforces this. The port is renamed and genericized.

## Constraints

- **Runs on the system `python3`, outside any venv.** macOS ships 3.9, so both
  Python files are stdlib-only and 3.9-compatible: no `match`, no `X | Y` at
  runtime (use `from __future__ import annotations`), and no `tomllib`. The policy
  file is therefore JSON. A test parses both files with
  `ast.parse(..., feature_version=(3, 9))`.
- **Python exits 2 when its script is missing,** and exit 2 means *block* in
  almost every harness. So registrations never call `python3 hook.py` directly.
  They call a POSIX `sh` shim that exits 0 when `python3` or the script is absent.
- **`make init` deletes `tests/template/`.** The tests guard the template's copy.
  Downstream repos receive the tested files without the tests, the same
  arrangement as the bundled skill scripts.
- **Root-file allowlist** (`test_root_hygiene`). No new root files. All new files
  live in dot-directories or under `docs/`.

## Approaches considered

- **A. Pure gate + one entry script (chosen).** `command_gate.py` holds the parser
  and decision as pure functions (command/edit + policy → verdict). `hook.py`
  detects the payload format, loads the policy, calls the gate and writes the
  format's response. This matches the card's "one gate script + thin adapters",
  keeps the security boundary free of I/O plumbing, and lets tests hit it
  directly.
- **B. One file.** Easiest to copy, but the parser is interleaved with four
  payload formats, which makes the security boundary harder to review.
- **C. Pure gate + one adapter script per format.** Each format's quirks are
  explicit, but the fail-safe and policy-loading code is copied four times and
  registrations point at different entries.

Two options were ruled out:

- **A third-party shell parser** (bashlex, tree-sitter-bash). The hook runs
  outside any venv, so it would have to be vendored or installed in every repo.
- **Rewriting on `shlex`.** It doesn't model `$()`, backticks or redirects, so we'd
  re-derive the hardening that already exists.

The user picked **A**.

## Decisions made with the user

| Decision | Choice |
|---|---|
| Where the gate lives | `.agents/hooks/`, beside `.agents/skills/` and `.agents/rules/`. No cross-harness hooks standard exists. Each harness reads its own config, so each gets a small registration file pointing at the shared gate. |
| Allow-list source | A neutral repo-owned list in `.agents/hooks/policy.json`, **merged** with Claude Code's `Bash(...)` entries when present. Repo-shared entries (`policy.json` and the tracked `.claude/settings.json`) apply in every harness. **Personal** entries (`~/.claude/settings.json`, `~/.claude/settings.local.json` and the gitignored `.claude/settings.local.json`) apply **only when Claude Code runs the hook**, detected by `$CLAUDE_PROJECT_DIR` being set, so a Claude-only personal allow never auto-approves in another harness. |
| Codex | **Deny-only.** In Codex an approval usually means leaving its sandbox, and the allow-list (with code-running entries like `uv run *`) was written for Claude Code. The Codex registration blocks banned patterns only, and there is no `PermissionRequest` hook. |
| Which registrations ship | Only harnesses whose vendor docs confirm the format: Claude Code, Cursor, Codex, Copilot (shell only), Gemini CLI, Factory, Augment. The README table lists all surveyed harnesses with status. |
| When a banned pattern blocks a shell command | Always, **except** when every command in the line is a read-only search/inspection tool (so `rg old_call \| head` works, `rg old_call \| sh` is blocked). |
| Banned pattern in a file edit | Block only matches the edit **adds**, so a file that already holds one stays editable. Each entry can exempt paths. |
| Uncertain input | No opinion (the harness's normal prompt), never deny. |

## Design

### Files

```
.agents/hooks/
  command_gate.py   # pure: shell parser + allow/no-opinion/deny decisions
  hook.py           # entry: payload format detection, policy loading, response
  gate.sh           # POSIX sh shim: locate hook.py by own dirname, fail open
  policy.json       # repo-owned: allow, safe_env, write_roots, banned
  README.md         # how the gate works, policy schema, how to add a harness
.claude/settings.json        # PreToolUse registration (replaces the old hook)
.cursor/hooks.json           # beforeShellExecution + preToolUse(Write)
.codex/hooks.json            # PreToolUse (deny only)
.github/hooks/command-gate.json   # Copilot CLI / cloud agent / VS Code: preToolUse (bash)
.gemini/settings.json        # BeforeTool
.factory/hooks.json          # PreToolUse
.augment/settings.json       # PreToolUse (deny only)
```

`.claude/hooks/claude_permission_hook.sh` is deleted.

### `command_gate.py` (pure)

This is a port of the downstream gate *including* its unmerged hardening (the
quote-aware redirect extractor, `_hash_kind`, `$'` fail-closed,
`DIR_CHANGE`/`dir_changed` plumbing, `SAFE_ENV_NAMES`/`UNSAFE_BARE_*`,
`$S` substitution placeholder), with these changes:

- **No module-level `CWD`/`HOME` globals in decisions.** The working directory
  and write roots arrive as arguments, so tests and `hook.py` pass them explicitly.
- **`SAFE_ENV_NAMES` is generic:** `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE`,
  `NO_COLOR`, `FORCE_COLOR`, `CLICOLOR`, `COLUMNS`, `TZ`, `LANG`, `LC_ALL`, `CI`.
  A repo adds its own (telemetry toggles etc.) through `policy.safe_env`.
- **Allow patterns** use the same syntax as Claude Code's `Bash(...)` content:
  exact match, a trailing ` *` (matches the bare command too), the legacy `:*`
  suffix, and `*` anywhere else matching any run of characters *within one
  simple command* (needed for the template's existing
  `gh pr comment * bugbot run*`). Matching is against the already-split simple
  command, so a `*` can never span `&&`, `;` or `|`.
- **Public API** (shapes, not signatures; the builder may refine names):
  - `shell_verdict(command, policy, cwd, roots) -> Verdict`, where `Verdict` is
    `ALLOW`, `NO_OPINION` or `DENY` and carries a reason. The banned check runs
    first, then the allow check.
  - `edit_verdict(path, before, after, policy, project_root) -> Verdict`, which
    returns only `DENY` or `NO_OPINION`.
  - `Policy`, a small dataclass: `allow`, `ask_or_deny` (see below), `safe_env`,
    `banned`.
- **Claude `deny`/`ask` rules are respected.** If any subcommand matches a Claude
  `permissions.deny` or `permissions.ask` `Bash(...)` pattern, the gate gives
  no opinion and never allows. This keeps a hook `allow` from steamrolling a rule
  the user wrote to stop or confirm that command.

#### Banned patterns

`policy.banned` is a list of entries:

```json
{
  "pattern": "<Python regex>",
  "message": "why this is banned",
  "replacement": "what to use instead",
  "exempt_paths": ["docs/**", ".agents/hooks/policy.json"]
}
```

- **Shell:** run every pattern over the raw command text. On a hit, deny unless
  the command parses fully **and** every executed simple command, including inside
  substitutions, has a read-only leading command. A command that fails to parse
  and contains a hit is denied (fail closed for the banned check only).
  - **Read-only set:** `grep`, `egrep`, `fgrep`, `rg`, `ag`, `ack`, `git grep`,
    `git log`, `git show`, `git diff`, `git blame`, `cat`, `head`, `tail`,
    `less`, `wc`, `ls`.
  - **Deliberately absent:** `find` (`-exec`), `sed` (`-i`, `e`), `awk`
    (`system()`), `xargs`.
- **Edits:** a hit counts only if the edit adds it, i.e. the number of matches in
  `after` exceeds the number in `before`, per pattern. A path matching any
  `exempt_paths` glob (relative to the project root) is skipped.
- **An invalid regex in the policy** is ignored for that entry. Every other
  policy error makes the whole policy unreadable, which means no opinion
  everywhere. Malformed policy must never lock a session.
- **The deny reason** reads
  `Blocked: <subject> matches banned pattern <pattern>: <message>. Use instead: <replacement>.`

### `hook.py` (entry)

1. Read stdin JSON. On failure, exit 0 silently.
2. **Detect the payload format** from its fields, not from a harness name.
   - **Claude-style:** `hook_event_name` + `tool_name` + `tool_input`. This
     covers Claude Code, Codex, Factory, Augment, Cursor running Claude-format
     hooks, and Copilot's `PreToolUse` alias.
     - Shell tools: `Bash`, `Execute` (Factory), `bash` (Augment), `Shell`
       (Cursor).
     - Edit tools: `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `Create`,
       `ApplyPatch` (Factory), `apply_patch` (Codex), `edit`, `write` (Augment).
   - **Cursor native `beforeShellExecution`:** top-level `command` + `cwd`, and
     no `tool_name`.
   - **Cursor native `preToolUse`:** `tool_name` + `tool_input`, with no
     `hookSpecificOutput` support required. Respond in the flat format.
   - **Copilot camelCase:** `toolName` + `toolArgs`. `toolArgs` may be a JSON
     string, in which case parse it. Shell tool: `bash` → `command`.
   - **Gemini `BeforeTool`:** `hook_event_name == "BeforeTool"`.
     - `run_shell_command` → `tool_input.command`.
     - `write_file` → `file_path`/`content`.
     - `replace` → `file_path`/`old_string`/`new_string`.
   - **Anything unrecognized** gets no opinion.
3. **Normalize edits** into `(path, before, after)`:
   - **Whole-file writes** (`Write`, `Create`, `write_file`, Cursor `Write`):
     `before` is the current file text, or empty if the file is unreadable.
     `after` is `content`.
   - **Replacement edits** (`Edit`, `MultiEdit`, `replace`, Augment `edit`):
     apply each replacement to the current file text when readable, otherwise
     `before`/`after` are the joined old/new strings.
   - **`NotebookEdit`:** `new_source` against empty.
   - **Codex/Factory patches** (`apply_patch`, `ApplyPatch`): parse the patch
     envelope. `after` is the `+` lines per file and `before` is the `-` lines.
     An unparseable patch yields no edits.
   - **Unknown edit shapes:** try the common keys (`file_path`, `path`,
     `target_file` + `content`, `contents`, `file_text`, `new_string`,
     `newText`). If none are found, no opinion.
4. **Resolve the project root and write roots.**
   - **Project root:** `$CLAUDE_PROJECT_DIR` if set, else the first
     `workspace_roots`/`workspaceRoots` entry, else the nearest ancestor of the
     payload `cwd` holding `.git` (a file or a directory, so worktrees count),
     else the payload `cwd`.
   - **Write roots:** project root, `/tmp`, `/private/tmp`,
     `tempfile.gettempdir()`, Claude `additionalDirectories`, and
     `policy.write_roots`. A `~` is expanded; relative entries are relative to
     the project root.
   - This replaces the downstream gate's "hook cwd is a write root", which let a
     drifted cwd (`/etc`) become writable.
   - **Relative redirect targets** resolve against the payload `cwd` (else
     `os.getcwd()`).
5. **Load the policy.** Read `<project root>/.agents/hooks/policy.json`, falling
   back to the copy next to `hook.py`. Merge in Claude `Bash(...)` `allow`, `ask`
   and `deny` entries.
   - **Always:** from `<root>/.claude/settings.json`, which is tracked and
     repo-shared.
   - **Only when `$CLAUDE_PROJECT_DIR` is set** (Claude Code is running the
     hook): also from the personal files `~/.claude/settings.json`,
     `~/.claude/settings.local.json` and `<root>/.claude/settings.local.json`.
   - **`ask`/`deny` from any merged file** always apply (they only ever
     *withhold* an allow).
   - **Errors:** a settings file that won't parse is skipped; an unreadable
     `policy.json` means no opinion.
6. **Respond in the detected format.**
   - **Claude-style:**
     `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":…,"permissionDecisionReason":…}}`.
   - **Cursor native:** `{"permission":…,"user_message":…,"agent_message":…}`.
   - **Copilot:** `{"permissionDecision":…,"permissionDecisionReason":…}`.
   - **Gemini:** `{"decision":…,"reason":…}`.
   - **No opinion:** exit 0, empty stdout, in every format.
   - **Deny:** the format's deny JSON on stdout plus the reason on stderr, exit 0.
     JSON is used rather than exit 2 because every shipped format has a JSON deny,
     and exit 2's meaning varies.
7. **`--deny-only` flag:** never emit an allow, downgrading it to no opinion.
   The Codex `PreToolUse` and Augment registrations pass it. This is how the
   gate avoids harness names: the registration states the capability.
8. **Top-level `except Exception` → exit 0, no output.** This is the one place a
   catch-all is justified, because it is a documented fail-safe boundary. The
   catch-all needs a narrow-except exemption comment per
   `.agents/rules/python/no-blind-except.md` ("catch-all only if a human agreed";
   the user agreed to fail-open at the read-back).

### `gate.sh`

```sh
#!/bin/sh
# Fail open: no python3 or no hook.py -> exit 0, no output (normal prompt).
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0
command -v python3 >/dev/null 2>&1 && [ -f "$DIR/hook.py" ] || exit 0
exec python3 "$DIR/hook.py" "$@"
```

It does not `cd` anywhere that changes the hook's working directory: `hook.py`
takes the cwd from the payload.

### Registrations

Each registration calls the shim from the project root, using the harness's
documented project-dir variable when it has one (`$CLAUDE_PROJECT_DIR`),
otherwise `$(git rev-parse --show-toplevel)`.

| Harness | File | Event / matcher | Flags |
|---|---|---|---|
| Claude Code | `.claude/settings.json` | `PreToolUse`, `Bash\|Write\|Edit\|MultiEdit\|NotebookEdit` | — |
| Cursor | `.cursor/hooks.json` (version 1) | `beforeShellExecution`; `preToolUse` matcher `Write` | — |
| Codex | `.codex/hooks.json` | `PreToolUse` `Bash\|apply_patch` | `--deny-only` |
| Copilot CLI / cloud / VS Code | `.github/hooks/command-gate.json` (version 1) | `preToolUse` | — |
| Gemini CLI | `.gemini/settings.json` | `BeforeTool` `run_shell_command\|write_file\|replace` | — |
| Factory | `.factory/hooks.json` | `PreToolUse` `Execute\|Edit\|Create\|ApplyPatch` | — |
| Augment | `.augment/settings.json` | `PreToolUse` `bash\|edit\|write` | `--deny-only` |

**Double firing is expected and harmless.** Cursor, Copilot CLI/VS Code and Devin
CLI also load `.claude/settings.json` hooks, so the gate may run twice for one
call. It is deterministic and side-effect free, so both runs agree.

### The template's existing allow entries stay where they are (builder-check call)

The `Bash(...)` entries stay in the tracked `.claude/settings.json`, and
`policy.json` ships with an empty `allow`.

- **Why they don't move:** `tests/template/test_pr_reviewer_seam.py` (three
  tests), the published `resolve-pr-concerns` skill and the
  `pr-reviewer-auto-trigger` rule all tell repos to put reviewer allow entries in
  `.claude/settings.json`. Moving them would mean editing a published skill and
  its lock hash for no behavior gain.
- **Why every harness still sees them:** the gate already merges the tracked
  settings file for every harness.
- **What `policy.allow` is for:** repos that want a home outside Claude Code's
  file.

### Existing guard narrowed (builder-check call)

`test_no_vendor_config_directory_ships` asserts that no tracked `.cursor/` exists.
Its purpose is to keep reviewer-vendor *noise config* (`.cursor/BUGBOT.md`) out
of the repo in favor of the vendor-neutral guidelines file. It is narrowed to "no
tracked file under `.cursor/` except `hooks.json`", which keeps that intent while
allowing the hook registration.

### Docs

- **`.agents/hooks/README.md`:** what the gate allows, defers and denies; the
  policy schema with examples; the payload formats; and how to register a new
  harness.
- **Root README:** a short "Agent hooks" section with the coverage table for
  every surveyed harness.
  - **Shipped:** the seven above.
  - **Covered via `.claude` loading:** Devin CLI/Desktop new format.
  - **Recipe / follow-up:** Windsurf legacy, Cline, Antigravity, Kiro, Amp.
  - **Not possible:** OpenCode (JS plugins only).
- **`AGENTS.md` "Hooks" section:** point at `.agents/hooks/` instead of the
  deleted script.

### Testing

All tests live in `tests/template/` and load the modules by file path
(`importlib.util.spec_from_file_location`), the pattern `test_merge_gate.py` uses.

- **`test_command_gate.py`** covers the pure gate:
  - the downstream allow/defer cases, ported. The baseline is the downstream
    repo's merged `test_permission_gate.py` (`ALLOW_CASES`, `DEFER_CASES`,
    `PATTERNS`, `ROOTS`). Its file is saved in the session scratchpad and the
    plan inlines the cases, with organization-specific names genericized;
  - the hardened allow/defer cases, ported (the five card bullets each have
    named tests);
  - the allow-pattern syntax (trailing `*`, `:*`, mid-string `*`, no spanning
    `&&`);
  - Claude deny/ask precedence;
  - banned shell: hit denies, read-only-only line allowed, read-only piped into
    `sh` denied, parse failure with a hit denies;
  - banned edit: added versus pre-existing, `exempt_paths`, invalid regex
    ignored;
  - write roots: a drifted cwd outside the project root is not writable.
- **`test_command_gate_hook.py`** covers the entry via `subprocess` with
  `python3 hook.py`:
  - one payload fixture per format, for allow, deny and no opinion, asserting
    the exact response shape;
  - `--deny-only`;
  - personal Claude allows applying only when `CLAUDE_PROJECT_DIR` is set;
  - Copilot string `toolArgs`;
  - Codex `apply_patch` parsing;
  - garbage stdin, an unknown format and an unreadable `policy.json` each exit 0
    with no output;
  - `gate.sh` exits 0 silently when `hook.py` is absent.
- **`test_command_gate_registrations.py`:** every registration file parses, points
  at `.agents/hooks/gate.sh`, its matcher covers the documented tool names, the
  Codex and Augment registrations pass `--deny-only`, the old hook script is
  gone, and the narrowed `.cursor/` guard still rejects any other `.cursor/` file.
- **Python 3.9 syntax check:** `ast.parse(src, feature_version=(3, 9))` on both
  Python files.
- **Lint scope:** add `.agents/hooks` to `machinery_lint`/`machinery_format`
  (black + ruff) and to mypy `files`.

### E2E (Step 6)

Pipe real-shaped payloads for each format through `gate.sh` from a scratch cwd
and read the JSON. Also run a live Claude Code check in this repo: an
allow-listed compound command (`make lint && make tests`) runs without a prompt,
and an injected unlisted tail (`make lint && touch /tmp/x; rm -rf ~/nope`) does
not auto-approve. Cursor, Codex, Copilot, Gemini, Factory and Augment need a live
check by the user (Step 7b test kit).

## Follow-ups (filed at Step 11b, with approval)

A card under epic #51 or a new epic for registrations whose payload fields need
primary-source confirmation: Windsurf/Devin legacy (`pre_run_command`,
`tool_info.command_line`, exit-2-only deny), Cline (`cancel` output, stringified
`parameters`), Antigravity (`.agents/hooks.json`, `toolCall`, `auto_approve`),
Kiro (`.kiro/hooks/*.json`) and Amp (permissions `delegate`).

## Left to the builder

- Exact function and class names inside the two Python files.
- The internal structure of the ported parser. Keep the downstream logic intact
  where it is not changed above, because its behavior is what the tests pin.
- Fixture JSON layout for the per-format payload tests.
