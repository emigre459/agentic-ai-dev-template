# Command gate

A harness-neutral `PreToolUse` hook. One Python script (`hook.py`, stdlib only,
Python 3.9+) reads whichever payload a harness sends, decides, and answers in that
harness's response format. `gate.sh` is the launcher every registration calls; it
exits 0 with no output when `python3` or `hook.py` is missing.

## What it does

The gate returns one of three answers for a shell command or a file edit:

| Answer | When |
|--------|------|
| **Allow** | Every simple command the shell would run matches an allow pattern, and every output redirect lands inside an allowed root (the project root, `/tmp`, the OS temp dir, plus `write_roots` and Claude `additionalDirectories`). |
| **Deny** | A shell command or the text an edit adds matches a `banned` pattern. |
| **No opinion** | Everything else. The harness shows its normal prompt. |

Allow is checked per command, not by prefix: `uv run pytest && rm -rf /` is not
allowed just because it starts with `uv run`. The parser handles compound commands
(`&&`, `||`, `;`, `|`, `&`, newlines), `cd <dir> && ...`, `$(...)`, backticks,
`<(...)`, `for`/`while`/`if` bodies, leading `NAME=value` assignments, and the
wrappers `timeout`, `time`, `nice`, `nohup`, `stdbuf`, `command`, `builtin` and
`env`. Anything it cannot parse is no opinion, including heredocs (`<<`),
subshells and other bare parentheses, `${...}` other than a plain `${NAME}` or
`${1}`, and `$[...]`. Redirect targets are resolved through symlinks before the
root check.

It never allows or denies on uncertainty. A bad payload, an unreadable policy, a
missing file or any exception ends in exit 0 with empty stdout. A deny is the
harness's JSON on stdout plus the reason on stderr, always with exit 0.

### Banned patterns

- **Shell commands:** a command matching a banned pattern is denied, unless every
  simple command in it is a read-only search and nothing redirects output. The
  read-only set is `grep`, `egrep`, `fgrep`, `rg`, `ag`, `ack`, `cat`, `head`,
  `tail`, `less`, `wc`, `ls`, `git grep`, `git log`, `git show`, `git diff` and
  `git blame`. `rg old_call` passes; `rg old_call | sh`, `rg old_call > out.txt`,
  `rg --pre ...` and `git grep -O` / `--open-files-in-pager` (or any abbreviation
  of it) are denied, because those run a program or
  write a file.
- **Edits:** only what the edit adds counts. An edit is denied when the new text
  has more matches than the old text, so touching a file that already contains a
  banned string is fine as long as the count does not grow. Edits come from Claude
  `Write`/`Edit`/`MultiEdit`/`NotebookEdit`, Gemini `write_file`/`replace`, Factory
  `Create`/`Edit` and `apply_patch` envelopes.

## `policy.json`

The gate reads `<project root>/.agents/hooks/policy.json`, and falls back to the copy
next to `hook.py` only when the root has none.

| Key | Type | Meaning |
|-----|------|---------|
| `allow` | list of strings | Extra allow patterns in Claude `Bash(...)` pattern syntax, without the `Bash(` wrapper: an exact command, a trailing ` *` or `:*` for a prefix, or `*` anywhere inside one simple command. |
| `safe_env` | list of strings | Extra env var names that may lead a command and are stripped before matching. Built in: `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE`, `NO_COLOR`, `FORCE_COLOR`, `CLICOLOR`, `COLUMNS`, `TZ`, `LANG`, `LC_ALL`, `CI`. Any other leading assignment (`PATH=`, `GIT_*`, `LD_*`, ...) stays in the command and so defers. |
| `write_roots` | list of strings | Extra directories an allowed command may redirect output into. `~/...` expands to the home directory; a relative entry is relative to the project root. |
| `banned` | list of objects | Banned patterns, below. |

Each `banned` entry:

| Field | Meaning |
|-------|---------|
| `pattern` | Python regular expression, searched in the command string or in the text an edit adds. |
| `message` | Why it is banned. |
| `replacement` | What to use instead. |
| `exempt_paths` | Optional globs, relative to the project root, for files whose edits are not checked. `**` behaves like `*` (it matches across `/`), so `**/x.md` does not match a root-level `x.md`: list `x.md` as well. |

```json
{
  "allow": ["make smoke", "docker compose ps *"],
  "safe_env": ["MY_APP_ENV"],
  "write_roots": ["~/scratch"],
  "banned": [
    {
      "pattern": "old_api\\(",
      "message": "old_api was removed in v2",
      "replacement": "new_api()",
      "exempt_paths": ["MIGRATING.md", "**/MIGRATING.md", "docs/**"]
    }
  ]
}
```

Failure handling: a `policy.json` that is not valid JSON, is not an object, or has a
section (`allow`, `safe_env`, `write_roots`, `banned`) that is not a list makes the
gate give no opinion everywhere. Non-string items inside `allow`, `safe_env` or
`write_roots` are skipped. A single malformed `banned` entry (bad regex, missing
`pattern`, wrong field type) is skipped; the rest still apply. A root `policy.json`
that exists but cannot be read (including a dangling symlink) never falls back to the
copy next to `hook.py`. The shipped file is empty (`{"allow": [], "safe_env": [], "write_roots": [], "banned": []}`),
so out of the box the gate allows only what Claude settings already allow.

## Claude allows: shared and personal

The gate also reads `permissions` from Claude settings files, so it forwards one
allow list to every harness:

| Source | Applies to |
|--------|------------|
| `.claude/settings.json` (tracked) | Every harness |
| `policy.json` `allow` (tracked) | Every harness |
| `~/.claude/settings.json`, `~/.claude/settings.local.json`, `.claude/settings.local.json` | Claude Code only: the payload is in Claude format, `CLAUDE_PROJECT_DIR` and `CLAUDECODE` are set, and neither `CURSOR_PROJECT_DIR` nor `CURSOR_VERSION` is set. Cursor also sets `CLAUDE_PROJECT_DIR` (a compatibility alias), so that variable alone does not count. |

Only `Bash(...)` rules are read from `allow`. Claude `ask` and `deny` rules
(including a bare `Bash`) always withhold: a command matching one is never allowed
by the gate, whatever the allow lists say. `additionalDirectories` are added to the
write roots.

## Deny-only harnesses

Codex and Augment register the gate with `--deny-only`: the gate still denies banned
patterns, but an allow becomes no opinion.

- **Codex:** an approval there usually means leaving its sandbox, so the gate does
  not grant one on its own.
- **Augment:** it does not support an allow answer from hooks yet.

## Payload formats recognized

The format is detected from the payload's fields, not from a harness name.

| Format | Detected by | Used by |
|--------|-------------|---------|
| Claude | `tool_name` + `tool_input` | Claude Code, Codex, Factory, Augment (also Cursor and Devin through `.claude/settings.json`) |
| Gemini | `hook_event_name: BeforeTool` | Gemini CLI |
| Copilot | `toolName` + `toolArgs` (object or JSON string); shell only | Copilot |
| Cursor | `hook_event_name: beforeShellExecution` or `preToolUse` | Cursor native hooks |

Shell tool names: `Bash`, `Shell`, `Execute`, `bash`, `run_shell_command`. Any other
tool is checked as a possible edit, and ignored if it is not one.

## Registrations

| Harness | File | Event and matcher | Mode | Timeout |
|---------|------|-------------------|------|---------|
| Claude Code | `.claude/settings.json` | `PreToolUse`: `Bash\|Write\|Edit\|MultiEdit\|NotebookEdit` | allow + deny | default |
| Codex | `.codex/hooks.json` | `PreToolUse`: `Bash\|apply_patch` | deny-only | 30 s |
| Copilot | `.github/hooks/command-gate.json` | `preToolUse` (no matcher) | allow + deny | 30 s |
| Gemini CLI | `.gemini/settings.json` | `BeforeTool`: `run_shell_command\|write_file\|replace` | allow + deny | 30000 ms |
| Factory | `.factory/hooks.json` | `PreToolUse`: `Execute\|Edit\|Create\|ApplyPatch` | allow + deny | 30 s |
| Augment | `.augment/settings.json` | `PreToolUse`: `bash\|edit\|write` | deny-only | 30000 ms |

Notes:

- Timeouts are milliseconds for Gemini and Augment, seconds for Codex, Copilot and
  Factory.
- Factory's standalone `hooks.json` is keyed by event name with no `"hooks"`
  wrapper; the same content inside Factory's `settings.json` would need the wrapper.
- Copilot is shell-only: its payload carries no edit content, so edits are not
  checked there.
- Codex loads project hooks only in trusted projects, and each hook must be reviewed
  once with `/hooks` before it runs.
- Claude Code registers with `$CLAUDE_PROJECT_DIR`; every other harness locates
  `gate.sh` with `git rev-parse --show-toplevel`, falling back to the current
  directory. If `gate.sh` cannot be found from there, nothing runs and the harness
  prompts as usual.
- The policy root itself is chosen by `CLAUDE_PROJECT_DIR`, then the payload's
  `workspace_roots`, then the nearest `.git` ancestor of the payload's `cwd`, then
  that `cwd`.
- Every command ends in `|| true`, so a crash can never block a tool call.

### Cursor

Cursor reads `.claude/settings.json` through its third-party hooks setting, so the
Claude registration covers it (Devin loads `.claude` hooks the same way). For a repo
with third-party hooks off, add a native `.cursor/hooks.json`. This template does not
commit one.

```json
{
  "version": 1,
  "hooks": {
    "beforeShellExecution": [
      { "command": "sh \"$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.agents/hooks/gate.sh\" || true" }
    ],
    "preToolUse": [
      {
        "matcher": "Write",
        "command": "sh \"$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.agents/hooks/gate.sh\" || true"
      }
    ]
  }
}
```

## Adding a harness

1. Find the harness's pre-tool hook: its config file, event name, matcher syntax,
   timeout unit and the JSON it sends and accepts.
2. Check whether the payload already matches a recognized format. If it does, only
   a registration is needed. If not, add a branch to `parse_payload` and a response
   in `render` in `hook.py`, with a test in `tests/template/`.
3. Add the registration file. The command must be
   `sh "<root>/.agents/hooks/gate.sh" [--deny-only] || true`, never `python3 hook.py`
   directly. Use `--deny-only` when the harness cannot take an allow answer.
4. Add the harness to `REGISTRATIONS` in
   `tests/template/test_command_gate_registrations.py`, to the table above and to the
   coverage table in the root `README.md`.

## Vendor docs

- Codex: https://learn.chatgpt.com/docs/hooks
- Copilot: https://docs.github.com/en/copilot/reference/hooks-reference
- Gemini CLI: https://geminicli.com/docs/hooks/
- Factory: https://docs.factory.com/reference/hooks-reference
- Augment: https://docs.augmentcode.com/cli/hooks
