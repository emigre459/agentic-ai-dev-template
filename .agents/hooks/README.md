# Command gate

A harness-neutral `PreToolUse` hook. One Python script (`hook.py`, stdlib only,
Python 3.9+) reads whichever payload a harness sends, decides, and answers in that
harness's response format. `gate.sh` is the launcher every registration calls; it
exits 0 with no output when `python3` or `hook.py` is missing.

## What it does

The gate returns one of three answers for a shell command or a file edit:

| Answer | When |
|--------|------|
| **Allow** | The command matches the strict grammar below, every simple command in it matches an allow pattern, every output redirect lands inside an allowed root (the project root, `/tmp`, the OS temp dir, plus `write_roots` and Claude `additionalDirectories`) and outside every protected path, and every input redirect reads from inside an allowed root or `/dev/null`. |
| **Deny** | A shell command or the text an edit adds matches a `banned` pattern. |
| **No opinion** | Everything else. The harness shows its normal prompt. |

Allow is checked per command, not by prefix: `uv run pytest && rm -rf /` is not
allowed just because it starts with `uv run`.

### What can be auto-allowed: the strict grammar

The gate does not try to understand every shell feature and reject the dangerous
ones; bash and zsh keep supplying new ones. Instead it accepts only a small, closed
set of command shapes that bash and zsh both run exactly the way the gate reads
them. A command outside that set gets no opinion, so the harness prompts as usual.
Backslash-newline continuations are joined first, where the shell joins them.

A command may auto-allow only when it is made of:

- **Simple commands joined by** `&&`, `||`, `;`, newlines and `|`. A trailing `;`
  or newline is fine, and so is a `#` comment at the end of the last line. Its text
  may not hold `;`, `&`, `|`, `<`, `>`, `(`, `)`, `{`, `}`, `$`, `!`, a backslash
  or a backtick: an interactive zsh without `INTERACTIVE_COMMENTS` would run them.
- **Each simple command:** optional leading `NAME=value` assignments for
  `safe_env` names only, then optional wrappers, then a plain command word, then
  arguments and redirects, each separated from the previous one by a blank.
- **Wrappers** (stripped before matching): bare `nohup`, `command`, `builtin`,
  `time` and `nice`, `timeout <duration>` (`30`, `1.5m`), and `env` followed only
  by `-i`, `-u NAME` and `safe_env` assignments.
- **The command word:** letters, digits and `_ . / , @ % + : -`, not starting
  with `-`, and not a reserved word.
- **Arguments:** plain text (letters, digits and `_ . / , @ % + = : * ? -`, not
  starting with `=` or `~`), `'single-quoted'` text, and `"double-quoted"` text,
  run together in any mix. Inside double quotes only `$NAME`, `${NAME}` (not
  followed by `[` or `:`), the escapes `\"`, `\\`, `\$` and `` \` ``, and a
  `$(...)` whose body is itself in the grammar. Every command inside a `$(...)` is
  vetted like a top-level one: `echo "HEAD: $(git rev-parse --short HEAD)"` is
  fine when both `echo` and `git rev-parse` are allowed.
- **Redirects:** `>`, `>>`, `1>`, `1>>`, `2>`, `2>>`, `&>` and `<` to one plain or
  quoted word (no `$` in it), plus exactly `2>&1`, `>&2` and `1>&2`. Output must land
  inside an allowed root (or `/dev/null`) and outside every protected path; input
  must come from inside an allowed root or `/dev/null`. Any other `/dev` path
  (`/dev/tcp/...`, `/dev/zero`, `/dev/fd/N`) prompts. A relative target in a
  command that also runs `cd`, `pushd` or `popd` prompts.
- **Nothing else may change the file system first:** an output redirect to anything
  but `/dev/null` auto-allows only when every other command that could run before
  or alongside it (an earlier command on the line, a pipeline neighbour, a
  `$(...)` in the same command) is a read-only search. Otherwise that earlier
  command could swap a path for a symlink into `.git/` after the gate checked it
  (`git checkout b -- lnk && echo x >> lnk/config`). A lone `cmd > /tmp/out.txt`
  is still fine; `cd x && cmd > out.txt` prompts.

Everything else always prompts, including:

- loops, conditionals, groups, subshells and functions: `for`, `while`, `until`,
  `if`, `case`, `select`, `{ ...; }`, `( ... )`, `[[ ... ]]`, `[ ... ]`, `(( ))`,
  `!`, zsh `always`, `foreach`, `repeat`;
- an unquoted `$` of any kind (`$x`, `$(...)`, `${x}`, `$1`, `$#`), any other
  `$` form inside double quotes (`"${x:-y}"`, `"$x[1]"`, `"$x:h"`, `"$1"`), and
  backticks anywhere;
- heredocs and here-strings (`<<`, `<<<`), process substitution (`<(...)`,
  `>(...)`), `$'...'` strings, and any backslash other than the escapes above and
  a line continuation;
- a lone `&` (background), `|&`, `;;`;
- wrapper options (`env -S`, `nice -n`, `timeout -s`, `time -p`, `command -v`) and
  other wrappers (`stdbuf`, `sudo`, ...);
- assignments to names outside `safe_env` (`PATH=`, `x=1`), assignments with no
  command after them, and a redirect with no command;
- unusual redirects: `>|`, zsh `>!` and `>>!`, `<>`, `<&`, `>&file`, `3>`, `&>>`,
  `2>&1-`, and a redirect glued to the previous word (`echo hi>out`);
- a word starting with `=` (zsh expands `=git` to the path of `git`) or `~`;
- `[`, `]`, `{`, `}`, `(`, `)`, `!`, `^`, `#` or `~` in an unquoted word, and any
  non-ASCII character outside quotes;
- control characters other than tab and newline;
- commands over 64 KiB (banned patterns are still checked).

### Warnings for policy authors

- **`safe_env`:** never add a name that changes what a shell or program loads or
  runs: `BASH_ENV`, `ENV`, `PROMPT_COMMAND`, `PS4` and other prompt variables,
  `PATH`, `LD_*`, `DYLD_*`, `PYTHONPATH`, `NODE_OPTIONS`, `GIT_*`, `EDITOR`,
  `PAGER`. The zsh integer specials (`SECONDS`, `COLUMNS`, `LINES`, `HISTSIZE`,
  `SHLVL`, `UID`, ...) and `FUNCNEST` are refused even when listed: zsh evaluates
  their values as math, which can run code.
- **Builtins that evaluate math or subscripts:** do not allow-list `printf *`,
  `print *`, `let *`, `test *`, `[ *`, `read *`, `unset *`, `declare *`,
  `typeset *`, `export *`, `local *`, `shift *`, `exit *`, `return *` or `kill *`.
  bash and zsh evaluate array subscripts and arithmetic in their arguments, and a
  `$(...)` hidden in a variable's value then runs.
- **Runners run whatever the repo says:** allow-listing `make *`, `bun run *`,
  `uv run *`, `npm run *` or a test runner means anything that can edit the
  Makefile, `package.json`, `pyproject.toml` or the sources can run code through
  them. Allow exact targets (`make lint`) where you can. The template's own
  shipped list (`.claude/settings.json`) already includes code-running entries
  (`uv run *`, `bunx *`, `bun run *`), so the protected-path guarantee below covers
  the gate's view of redirects, not what those programs do once they run.
- **Broad patterns match more than the example you had in mind:** a `*` matches
  any arguments. `gh pr comment * bugbot run*` also matches
  `gh pr comment 1 --body-file .env bugbot run`, which posts a local file to
  GitHub. Pin the options you mean.
- **Withhold rules and expansions:** when any `ask` or `deny` rule exists, a
  command whose arguments hold an expansion (`"$X"`, `"${X}"`, `"$(...)"`)
  prompts, because the gate cannot see the value (`git pu"$X"sh` runs
  `git push`). Withhold rules match both the command after wrappers and
  assignments are stripped and the command as written, so `Bash(env *)` and
  `Bash(timeout *)` withhold too.

### Protected paths

Even inside an allowed root, an allowed command may never redirect output under
`.git/`, `.claude/`, `.agents/`, `.codex/`, `.cursor/`, `.gemini/`, `.factory/`,
`.augment/`, `.github/hooks/`, `.husky/` or `.githooks/`, or into
`.pre-commit-config.yaml`, in the project root. These hold the gate's own policy,
every harness's hook registration and the hooks `git commit` runs; writing there
could switch the gate off or run code later. The check compares real paths, so a symlink does not
get around it, and it ignores case, so `.GIT/hooks` on a case-insensitive file
system (macOS APFS) is caught too. Root containment, by contrast, is compared
exactly. Edits made through edit tools are a separate path and are not
affected.

It never allows or denies on uncertainty. A bad payload, an unreadable policy, a
missing file or any exception ends in exit 0 with empty stdout. A deny is the
harness's JSON on stdout plus the reason on stderr, always with exit 0.

### Banned patterns

- **Shell commands:** a command matching a banned pattern is denied, unless every
  simple command in it is a read-only search and nothing redirects output
  (except to `/dev/null`), and the command is in the strict grammar. The
  read-only set is `grep`, `egrep`, `fgrep`, `rg`, `ag`, `ack`, `cat`, `head`,
  `tail`, `less`, `wc`, `ls`, `git grep`, `git log`, `git show`, `git diff` and
  `git blame`. `rg old_call` passes. These are denied because they run a program
  or write a file: `rg old_call | sh`, `rg old_call > out.txt`, a word holding an
  expansion (`"$X"`) or an unquoted glob (`*` could match a file named
  `--pre=sh`), and any word that, with its quotes removed, is `--pre`, `--pager`, `--hostname-bin`, `--output`, `--ext-diff`, `--textconv`,
  `--open-files-in-pager` or an abbreviation of one (`--opt=value` included), or a
  short-option cluster holding `-O`. A banned hit in a command outside the strict
  grammar is always denied.
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
| `safe_env` | list of strings | Extra env var names that may be assigned (bare, or leading a command, where they are stripped before matching). Built in: `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE`, `NO_COLOR`, `FORCE_COLOR`, `CLICOLOR`, `TZ`, `LANG`, `LC_ALL`, `CI`. Any other assignment (`PATH=`, zsh `path=`, `EDITOR=`, `GIT_*`, `LD_*`, ...) defers, and a zsh integer special (`COLUMNS`, `SECONDS`, ...) or `FUNCNEST` defers even when listed here. See the warnings above. |
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
