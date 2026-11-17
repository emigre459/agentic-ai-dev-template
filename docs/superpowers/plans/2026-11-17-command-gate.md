# Command Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the prefix-matching Claude hook with one hardened, harness-neutral
gate in `.agents/hooks/`. It allows fully parsed, allow-listed shell commands,
denies banned patterns in shell commands and file edits, and is wired into every
harness whose vendor docs confirm the payload format.

**Architecture:** There are two layers.

- **`command_gate.py`** is pure: the shell parser plus the allow / no-opinion / deny
  decisions, with no I/O.
- **`hook.py`** is the entry point. It reads the hook payload, detects its format,
  loads the policy, calls the gate and writes the response.

A POSIX `sh` shim (`gate.sh`) fails open when `python3` or the script is missing.
One small registration file per harness points at the shim.

**Tech Stack:** stdlib Python 3.9+ (system `python3`, no venv), POSIX sh, JSON
config, and pytest via `uv run` for tests.

**Spec:** `docs/superpowers/specs/2026-11-17-command-gate-design.md`. Read it
first: it carries every decision and its reason.

## Global Constraints

- **Python version:** both `.agents/hooks/*.py` files are stdlib-only and
  **3.9-compatible**.
  - Start with `from __future__ import annotations`.
  - No `match`, no `X | Y` outside annotations, no `itertools.pairwise`, no
    `zip(strict=)`, no `tomllib`.
  - Policy and config are JSON.
- **No decision on uncertainty:** any parse doubt, unknown payload, unreadable
  policy or exception means **no opinion**: exit 0 with empty stdout.
  - Never `allow` on uncertainty.
  - Never deny except on a banned-pattern hit.
- **Deny format:** a deny is the format's JSON on stdout plus the reason on
  stderr, with **exit 0**. Never exit 2.
- **Registration commands:** they never call `python3 hook.py` directly. They
  call `sh "<root>/.agents/hooks/gate.sh" [--deny-only] || true`.
- **No organization names:** no downstream organization, product or repo names
  anywhere, including the downstream gate's names, env vars like `LANGFUSE_*`, and
  PR numbers. `tests/template/test_no_org_names.py` enforces this. Port the
  logic, not the vocabulary.
- **Root files:** no new files at the repo root (`tests/template/test_root_hygiene.py`).
- **No `.cursor/` folder:** `test_no_vendor_config_directory_ships` stays
  unchanged and green.
- **Catch-all marker:** the single catch-all in `hook.py` carries exactly this
  marker:
  `# noqa: BLE001 -- hook boundary must fail open, never crash or deny; agreed emigre459 #50`
- **Reference source for the port:** the downstream gate and its tests are saved
  read-only at
  `/private/tmp/claude-501/-Users-daverenchmccauley-orca-workspaces-agentic-ai-dev-template-replace-the-prefix-matching-permission-hook-with/f7a38a73-ed86-4c99-8e7b-b8d13842ab30/scratchpad/downstream/`.
  - `permission_gate.py` is the merged base.
  - The first file hunk of `pr1851.diff` is the hardening to apply on top.
  - Never copy their docstrings' org-specific examples.
- **Tests:** run with `uv run pytest tests/template/<file> -p no:xdist -o addopts="" -q`.
  The full gate is `make pr_check`.

## Review Focus

1. **A cwd that drifted outside the repo** (`cwd=/etc`, project root `/repo`): a
   relative redirect `> passwd` must not be allowed. Pinned in Task 2
   (`test_drifted_cwd_is_not_a_write_root`).
2. **Read-only search piped into an executor** (`rg old_call | sh`): it must be
   denied when `old_call` is banned. Pinned in Task 2.
3. **Copilot `toolArgs` as a JSON *string*** rather than an object: it must still
   be parsed. Pinned in Task 3.
4. **A malformed `policy.json`, or a single invalid regex in `banned`:** the
   malformed file means no opinion everywhere, and the bad regex skips only its
   own entry, never locking the session. Pinned in Task 2 and Task 4.
5. **Run outside a git checkout:** the registration command must still exit 0
   with no output. Pinned in Task 5 (`test_registration_command_fails_open_outside_git`).

---

### Task 1: Port the hardened shell parser into `command_gate.py`

**Files:**
- Create: `.agents/hooks/command_gate.py`
- Create: `tests/template/test_command_gate.py`
- Modify: `Makefile` (the `machinery_format` and `machinery_lint` recipes: add
  `.agents/hooks/` beside `scripts/ tests/` on every black/ruff line)
- Modify: `pyproject.toml` (`[tool.mypy] files` gets `".agents/hooks"`, and
  `mypy_path` gets `".agents/hooks"`)

**Interfaces:**
- Produces (module `command_gate`, loaded by file path):
  - `BASE_SAFE_ENV_NAMES: frozenset[str]`. Exactly:
    `PYTHONUNBUFFERED PYTHONDONTWRITEBYTECODE NO_COLOR FORCE_COLOR CLICOLOR COLUMNS TZ LANG LC_ALL CI`.
  - `pattern_matches(pattern: str, command: str, home: str) -> bool`.
  - `is_fully_allowed(cmd: str, patterns: Sequence[str], roots: Sequence[str], cwd: str, home: str, safe_env: frozenset[str] = BASE_SAFE_ENV_NAMES, dir_changed: bool = False) -> bool`.
  - `simple_commands(cmd: str, safe_env: frozenset[str] = BASE_SAFE_ENV_NAMES) -> Optional[list[str]]`.
    It returns every executed simple command, including those inside
    substitutions and control-structure bodies, in order. Env assignments in
    `safe_env`, wrappers and redirects are stripped, and the leading word is
    dequoted. It returns `None` when the command cannot be parsed.

- [ ] **Step 1: Write the failing tests.**
  - Load the module with `importlib.util.spec_from_file_location("command_gate", REPO/".agents/hooks/command_gate.py")`.
  - Constants:
    - `HOME = "/home/u"`
    - `CWD = "/repo"`
    - `ROOTS = ["/repo", "/tmp", "/private/tmp"]`
    - `PATTERNS` = the downstream baseline list (`uv run *`, `uv run pytest *`,
      `git add *`, `git commit *`, `git rev-parse *`, `echo *`, `mkdir -p *`,
      `cd:*`, `grep:*`, `tail:*`, `git status *`, `git push *`).
  - Parametrized tests, each asserting
    `GATE.is_fully_allowed(cmd, PATTERNS, ROOTS, CWD, HOME) is <bool>`:
    - **`test_baseline_allowed`:** the downstream `ALLOW_CASES`, with the first
      case genericized to
      `"PYTHONUNBUFFERED=1 uv run pytest tests/x.py -v 2>&1 | tail -40"`.
    - **`test_baseline_deferred`:** the downstream `DEFER_CASES`, verbatim.
    - **`test_hardened_allowed`:** the PR hunk's `HARDENED_ALLOW_CASES`, verbatim.
      `echo 'x' > out.txt` now resolves under `CWD`, so the special roots list is
      not needed.
    - **`test_hardened_deferred`:** the PR hunk's `HARDENED_DEFER_CASES`, verbatim.
  - Named tests, one per card bullet. Each asserts one representative command
    from the lists above, so a failure names the bypass:
    - `test_glued_redirect_is_vetted` (`echo x>/etc/evil`)
    - `test_ampersand_redirect_to_file_is_vetted` (`echo x >&/etc/cron.d/x`)
    - `test_hash_comment_quote_is_not_a_quote`
    - `test_ansi_c_string_fails_closed`
    - `test_relative_redirect_after_cd_defers` (`cd /etc && echo x > cron.d/x`)
    - `test_only_safe_env_assignments_are_stripped`: `LD_PRELOAD=/tmp/x.so echo hi`
      gives False, and `NO_COLOR=1 echo hi` gives True.
  - **`test_pattern_syntax`**, parametrized over
    `(pattern, command, expected)`:
    - `("make lint", "make lint", True)`
    - `("make lint", "make lint x", False)`
    - `("uv run *", "uv run", True)`
    - `("uv run *", "uv runner", False)`
    - `("grep:*", "grep -r x", True)`
    - `("gh pr comment * bugbot run*", "gh pr comment 12 --body bugbot run", True)`
    - `("gh pr comment * bugbot run*", "gh pr comment 12 --body hi", False)`
    - `("~/bin/x *", "/home/u/bin/x a", True)`
  - **`test_star_never_spans_operators`:**
    `is_fully_allowed("gh pr comment 1 && rm -rf / && bugbot run", ["gh pr comment * bugbot run*"], ROOTS, CWD, HOME) is False`.
  - **`test_simple_commands`:**
    - `simple_commands("NO_COLOR=1 rg x | head -2 && echo $(git log -1)")`
      returns `["git log -1", "rg x", "head -2", "echo $S"]` as a multiset.
      Assert with `sorted()`, since order between substitution and outer is the
      builder's choice.
    - `simple_commands("echo 'unterminated")` returns `None`.
  - **`test_py39_syntax`:**
    `ast.parse(Path(".agents/hooks/command_gate.py").read_text(), feature_version=(3, 9))`
    does not raise.
- [ ] **Step 2: Run the tests and confirm they fail** (module missing).
  - Run: `uv run pytest tests/template/test_command_gate.py -p no:xdist -o addopts="" -q`
  - Expected: collection error / FileNotFoundError.
- [ ] **Step 3: Implement `command_gate.py`.**
  - Port the merged `permission_gate.py` with the PR hunk's hardening applied.
  - Delete the `CWD`/`HOME` module globals and `load_patterns`/`main`. Thread
    `cwd` and `home` as parameters through `redirect_target_ok`,
    `pattern_matches` and the call chain.
  - Make `SAFE_ENV_NAMES` a parameter (`safe_env`, default
    `BASE_SAFE_ENV_NAMES`). Keep `UNSAFE_BARE_NAMES` and `UNSAFE_BARE_PREFIXES`
    as module constants.
  - Extend `pattern_matches` so a `*` that is not the final character matches any
    run of characters: compile with `re.escape` on the pieces joined by `.*` and
    `fullmatch`. Keep the trailing `*` / `:*` rules exactly as downstream.
  - `simple_commands` reuses the same scan / split / scaffold / strip functions
    and collects instead of matching.
  - Write a module docstring describing the safety invariant in generic terms.
- [ ] **Step 4: Add `.agents/hooks/` to the lint scope** (the Makefile and
  `pyproject.toml` edits above). Then run `make machinery_lint` and fix what it
  reports.
- [ ] **Step 5: Run the tests and confirm they pass.**
  - Run: `uv run pytest tests/template/test_command_gate.py -p no:xdist -o addopts="" -q`
  - Expected: all pass.
- [ ] **Step 6: Commit and push.**
  ```bash
  git add .agents/hooks/command_gate.py tests/template/test_command_gate.py Makefile pyproject.toml
  git commit -m "Port the hardened shell parser into a harness-neutral command gate (#50)"
  git push
  ```

### Task 2: Policy model and the shell/edit verdicts

**Files:**
- Modify: `.agents/hooks/command_gate.py`
- Modify: `tests/template/test_command_gate.py`

**Interfaces:**
- Consumes: Task 1's `is_fully_allowed`, `simple_commands`, `pattern_matches`
  and `BASE_SAFE_ENV_NAMES`.
- Produces:
  - Decision constants: `ALLOW = "allow"`, `DENY = "deny"`,
    `NO_OPINION = "no_opinion"`.
  - `@dataclass(frozen=True) class Verdict: decision: str; reason: str = ""`.
  - `@dataclass(frozen=True) class BannedPattern: regex: re.Pattern[str]; pattern: str; message: str; replacement: str; exempt_paths: tuple[str, ...] = ()`.
    - `@classmethod from_dict(cls, d: Mapping[str, Any]) -> Optional[BannedPattern]`
      returns `None` when the regex is invalid or `pattern` is missing.
  - `@dataclass(frozen=True) class Policy: allow: tuple[str, ...] = (); withhold: tuple[str, ...] = (); safe_env: frozenset[str] = BASE_SAFE_ENV_NAMES; banned: tuple[BannedPattern, ...] = ()`.
    `withhold` holds Claude `ask` and `deny` `Bash(...)` contents.
  - `READ_ONLY_COMMANDS: frozenset[str]`. Exactly:
    `grep egrep fgrep rg ag ack cat head tail less wc ls`, plus
    `"git grep" "git log" "git show" "git diff" "git blame"`.
  - `shell_verdict(command: str, policy: Policy, cwd: str, roots: Sequence[str], home: str) -> Verdict`.
  - `edit_verdict(path: str, before: str, after: str, policy: Policy, project_root: str) -> Verdict`.

- [ ] **Step 1: Write the failing tests.**
  - Build policies inline. Use the banned entry
    `BannedPattern.from_dict({"pattern": r"old_api\(", "message": "deprecated", "replacement": "new_api()", "exempt_paths": ["docs/**"]})`.
  - Shell tests:
    - **`test_banned_shell_denies`:** `shell_verdict("uv run python -c 'old_api()'", ...)`
      gives `DENY`. The reason contains `old_api\(`, `deprecated` and `new_api()`.
    - **`test_banned_read_only_search_is_not_denied`:**
      `rg -F 'old_api(' src | head` gives a decision other than `DENY`. Also
      assert that the regex really hits this text, so the test can't pass
      vacuously.
    - **`test_banned_read_only_piped_to_executor_denies`:**
      `rg -l 'old_api(' | sh` gives `DENY`.
    - **`test_banned_unparseable_denies`:** `echo "old_api( ` (unterminated) gives
      `DENY`.
    - **`test_allow_listed_gives_allow`:** with
      `Policy(allow=("make lint",))`, `make lint` gives `ALLOW`.
    - **`test_unlisted_gives_no_opinion`:** `make deploy` gives `NO_OPINION`.
    - **`test_withhold_beats_allow`:**
      `Policy(allow=("git *",), withhold=("git push *",))` with
      `git status && git push origin x` gives `NO_OPINION`.
    - **`test_drifted_cwd_is_not_a_write_root`:**
      `shell_verdict("echo x > passwd", Policy(allow=("echo *",)), cwd="/etc", roots=["/repo", "/tmp"], home=HOME)`
      gives `NO_OPINION`.
    - **`test_policy_safe_env_extends_base`:** with
      `Policy(allow=("echo *",), safe_env=BASE_SAFE_ENV_NAMES | {"MY_TOGGLE"})`,
      `MY_TOGGLE=1 echo hi` gives `ALLOW`.
  - Edit tests:
    - **`test_edit_adding_banned_denies`:** `before=""`, `after="old_api()"` gives
      `DENY`.
    - **`test_edit_keeping_existing_hit_is_allowed`:**
      `before="old_api()\n"`, `after="old_api()\nx = 1\n"` gives `NO_OPINION`.
    - **`test_edit_exempt_path`:** `path="/repo/docs/a.md"` with
      `project_root="/repo"` gives `NO_OPINION`.
    - **`test_invalid_regex_entry_is_skipped`:**
      `BannedPattern.from_dict({"pattern": "("})` is `None`.
- [ ] **Step 2: Run the tests and confirm they fail** (names undefined).
- [ ] **Step 3: Implement the policy model and both verdicts.**
  - **`shell_verdict` order:**
    1. **Banned check.** Find each banned regex hit over the raw command. On any
       hit, deny unless `simple_commands` succeeds **and** every returned command
       is read-only (its first word, or first two words, is in
       `READ_ONLY_COMMANDS`).
    2. **Withhold check.** If `simple_commands` is `None`, or any command matches
       a `withhold` pattern, the result is `NO_OPINION`.
    3. **Allow check.** `ALLOW` if `is_fully_allowed(...)`, else `NO_OPINION`.
  - **`edit_verdict`:** skip when the path is relative to `project_root` and
    matches any `exempt_paths` glob (`fnmatch`, with `**` treated like `*`). Deny
    when, for any pattern, `len(findall(after)) > len(findall(before))`.
  - **Reason text** (exact):
    `Blocked: {subject} matches banned pattern {pattern}: {message}. Use instead: {replacement}.`
    `subject` is `this command` or `this edit to {path}`.
- [ ] **Step 4: Run the tests and confirm they pass**, then run
  `make machinery_lint`.
- [ ] **Step 5: Commit and push.**
  `git commit -m "Add banned-pattern and allow-list verdicts to the command gate (#50)"`

### Task 3: Payload parsing and edit normalization in `hook.py`

**Files:**
- Create: `.agents/hooks/hook.py` (the parsing half)
- Create: `tests/template/test_command_gate_hook.py`

**Interfaces:**
- Consumes: nothing from `command_gate` yet.
- Produces (module `hook`, loaded by file path; it inserts its own directory on
  `sys.path` and does `import command_gate`):
  - `@dataclass(frozen=True) class Edit: path: str; before: str; after: str`.
  - `@dataclass(frozen=True) class ToolCall: fmt: str; kind: str; command: str = ""; edits: tuple[Edit, ...] = (); cwd: str = ""; workspace_roots: tuple[str, ...] = ()`.
    - `fmt` is one of `"claude"`, `"cursor"`, `"copilot"` or `"gemini"`.
    - `kind` is one of `"shell"`, `"edit"` or `"other"`.
  - `parse_payload(payload: Mapping[str, Any]) -> Optional[ToolCall]`, which
    returns `None` for an unrecognized shape.
  - `parse_apply_patch(text: str) -> tuple[Edit, ...]`.

- [ ] **Step 1: Write the failing tests.** These are parametrized fixtures as
  inline dicts.
  - **Claude-style, `hook_event_name: "PreToolUse"`** gives `fmt "claude"`:
    - `Bash` / `Execute` / `bash` / `Shell` with `tool_input.command` give
      `kind "shell"` and that command.
    - `Write` / `Create`: `{file_path, content}` against a `tmp_path` file
      holding `"old"` gives `Edit(path, "old", content)`.
    - `Edit`: `{file_path, old_string, new_string}` against a readable file gives
      `after` = the file with the replacement applied. Against a missing file,
      `before=old_string` and `after=new_string`.
    - `MultiEdit` (`edits[]`) and `NotebookEdit` (`notebook_path`, `new_source`).
    - Augment `edit`: `{path, edits:[{oldText,newText}]}`.
    - Codex `apply_patch` with `tool_input.command` = a patch envelope gives the
      edits from `parse_apply_patch`.
  - **Cursor native:**
    - `{"hook_event_name":"beforeShellExecution","command":"ls","cwd":"/r","workspace_roots":["/r"]}`
      gives `ToolCall("cursor","shell","ls",(),"/r",("/r",))`.
    - `{"hook_event_name":"preToolUse","tool_name":"Shell","tool_input":{"command":"ls"}}`
      gives `fmt "cursor"`.
  - **Copilot:**
    - `{"toolName":"bash","toolArgs":"{\"command\":\"ls\"}","cwd":"/r"}` gives
      `fmt "copilot"`, `kind "shell"`. The string `toolArgs` must be parsed
      (Review Focus 3).
    - The same with object `toolArgs` also works.
    - `toolName "edit"` gives `kind "other"`: Copilot is shell only.
  - **Gemini:** `hook_event_name "BeforeTool"` with `run_shell_command`,
    `write_file {file_path, content}` and `replace {file_path, old_string, new_string}`.
  - **`test_unknown_shape_is_none`:** `{}`, `{"foo":1}`, and a Claude-style
    `Read` tool. `Read` gives `kind "other"`; the others give `None`.
  - **`test_apply_patch`:** an Add File with two `+` lines plus an Update File
    with `-a` / `+b` gives two `Edit`s. The Add File has `before ""`. The Update
    File has `before "a"` and `after "b"`. Garbage gives `()`.
  - **`test_py39_syntax`** for `hook.py`.
- [ ] **Step 2: Run the tests and confirm they fail.**
- [ ] **Step 3: Implement `parse_payload`, `parse_apply_patch` and the edit
  normalization.**
  - **Format detection order:**
    1. `toolName` → copilot.
    2. `hook_event_name == "BeforeTool"` → gemini.
    3. `beforeShellExecution` or `preToolUse` (lowercase) → cursor.
    4. `tool_name` + `tool_input` → claude.
  - **Shell tool names:** `Bash`, `Shell`, `Execute`, `bash`,
    `run_shell_command`.
  - **Unknown edit tools:** use the generic key fallback from the spec
    (`file_path` / `path` / `target_file` + `content` / `contents` / `file_text` /
    `new_string` / `newText`). Otherwise `kind "other"`.
  - **File reads:** read with `encoding="utf-8"`. Catch `OSError` and
    `UnicodeDecodeError` only.
- [ ] **Step 4: Run the tests and confirm they pass**; run `make machinery_lint`.
- [ ] **Step 5: Commit and push.**
  `git commit -m "Parse every confirmed hook payload format in the gate entry (#50)"`

### Task 4: Policy loading, decisions, responses, `gate.sh` and `policy.json`

**Files:**
- Modify: `.agents/hooks/hook.py`
- Create: `.agents/hooks/gate.sh` (mode 755; the body is the spec's shim
  verbatim)
- Create: `.agents/hooks/policy.json`, exactly:
  `{"allow": [], "safe_env": [], "write_roots": [], "banned": []}`
- Modify: `tests/template/test_command_gate_hook.py`

**Interfaces:**
- Consumes: Task 2's `Policy`, `BannedPattern.from_dict`, `shell_verdict`,
  `edit_verdict` and `Verdict`, and Task 3's `parse_payload` and `ToolCall`.
- Produces:
  - `find_project_root(call: ToolCall, env: Mapping[str, str]) -> str`.
  - `load_policy(root: str, hook_dir: str, home: str, claude_code: bool) -> Optional[tuple[Policy, list[str]]]`.
    It returns the policy plus extra write roots (`policy.write_roots` and Claude
    `additionalDirectories`), or `None` when `policy.json` is unreadable.
  - `render(fmt: str, verdict: Verdict) -> Optional[str]`, where `None` means
    print nothing.
  - `main(argv: Sequence[str], stdin: TextIO, env: Mapping[str, str]) -> int`.
    It always returns 0.

- [ ] **Step 1: Write the failing tests.** These are subprocess tests running
  `python3 .agents/hooks/hook.py`. Each builds a throwaway project under
  `tmp_path`: `git init`, a copied `.agents/hooks/`, and the test's own
  `policy.json` / `.claude/settings.json`. Each sets `HOME=tmp_path/"home"`.
  - **Exact `render` shapes for allow and deny:**
    - claude: `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":...}}`
    - cursor: `{"permission":"allow","user_message":...,"agent_message":...}`
    - copilot: `{"permissionDecision":"allow","permissionDecisionReason":...}`
    - gemini: `{"decision":"allow","reason":...}`
    - Deny uses `"deny"` in the same fields.
    - `render(fmt, Verdict(NO_OPINION))` is `None`.
  - **`test_deny_writes_reason_to_stderr_and_exits_0`.**
  - **`test_deny_only_downgrades_allow`:** an allow-listed command with
    `--deny-only` gives empty stdout and exit 0. A banned command still gives the
    deny JSON.
  - **`test_personal_claude_allows_only_under_claude_code`:** `Bash(make x)` sits
    in `HOME/.claude/settings.json`.
    - With `CLAUDE_PROJECT_DIR` set to the project, it allows.
    - Without it, there is no output.
    - The same split holds for `<root>/.claude/settings.local.json`.
    - The tracked `<root>/.claude/settings.json` allows in both cases.
  - **`test_claude_ask_deny_withhold`:** `permissions.ask: ["Bash(git push *)"]`
    with `git push x` gives no output, even though `git *` is allowed.
  - **`test_additional_directories_are_write_roots`:** `echo x > /opt/shared/y`
    is allowed only when `permissions.additionalDirectories` contains
    `/opt/shared`.
  - **Fail-open (Review Focus 4):** each of these gives exit 0 and empty stdout:
    - stdin `not json`;
    - stdin `{}`;
    - `policy.json` containing `{`;
    - `policy.json` banned entry `{"pattern":"("}` alongside a valid entry, where
      the valid entry still denies.
  - **`test_gate_sh_missing_hook_fails_open`:** copy only `gate.sh` into an empty
    dir and run `sh gate.sh < payload`. It gives exit 0 and empty output.
  - **`test_gate_sh_passes_flags`:** `sh .agents/hooks/gate.sh --deny-only` with
    an allow-listed payload gives empty stdout.
- [ ] **Step 2: Run the tests and confirm they fail.**
- [ ] **Step 3: Implement the decision path.**
  - **`find_project_root`:** `env["CLAUDE_PROJECT_DIR"]`, then the first
    `workspace_roots` entry, then the nearest ancestor of `call.cwd or os.getcwd()`
    that contains `.git`, then that cwd.
  - **Write roots:** project root, `/tmp`, `/private/tmp`,
    `tempfile.gettempdir()`, plus the extra roots from `load_policy`. Expand `~`;
    relative entries are relative to the root.
  - **`load_policy`:** read `<root>/.agents/hooks/policy.json`, else
    `<hook_dir>/policy.json`. A missing or invalid file returns `None`.
    - Merge `Bash(...)` contents from `permissions.allow` / `ask` / `deny` and
      `additionalDirectories` of `<root>/.claude/settings.json`.
    - When `claude_code` is true, also merge `~/.claude/settings.json`,
      `~/.claude/settings.local.json` and `<root>/.claude/settings.local.json`.
    - `ask` and `deny` go to `withhold`.
    - An unparseable settings file is skipped (`OSError`, `ValueError`).
  - **`main` flow:**
    1. Parse `--deny-only` from `argv` (plain membership check; no argparse
       error paths).
    2. `json.load(stdin)` → `parse_payload`.
    3. `kind "shell"` → `shell_verdict`; `kind "edit"` → `edit_verdict` per edit,
       first `DENY` wins.
    4. Apply `--deny-only`.
    5. `render`, print, and print the reason to stderr on deny.
  - **Fail-open wrapper:** `if __name__ == "__main__":` wraps `main` in the single
    marked catch-all (Global Constraints), which exits 0 silently.
- [ ] **Step 4: Run the tests and confirm they pass**; run `make machinery_lint`.
- [ ] **Step 5: Commit and push.**
  `git commit -m "Wire policy loading and per-format responses into the gate entry (#50)"`

### Task 5: Harness registrations and removing the old hook

**Files:**
- Modify: `.claude/settings.json`. Replace the `PreToolUse` entry; keep `Stop`
  and every `permissions.allow` entry unchanged.
- Delete: `.claude/hooks/claude_permission_hook.sh`
- Create:
  - `.codex/hooks.json`
  - `.github/hooks/command-gate.json`
  - `.gemini/settings.json`
  - `.factory/hooks.json`
  - `.augment/settings.json`
- Create: `tests/template/test_command_gate_registrations.py`

**Interfaces:**
- Consumes: `.agents/hooks/gate.sh` from Task 4.
- Produces: the registration files.
  - **Before writing each non-Claude file,** confirm its exact shape against the
    vendor's current docs via Context7 (`resolve-library-id` then `query-docs`).
    Record the doc URL for Task 6's README.
  - **Root expression:** `$CLAUDE_PROJECT_DIR` for Claude, and
    `$(git rev-parse --show-toplevel 2>/dev/null || pwd)` everywhere else.
  - **Every command** ends with `|| true`.

| File | Event key | Matcher | Command suffix |
|---|---|---|---|
| `.claude/settings.json` | `PreToolUse` | `Bash\|Write\|Edit\|MultiEdit\|NotebookEdit` | — |
| `.codex/hooks.json` | `PreToolUse` | `Bash\|apply_patch` | `--deny-only` |
| `.github/hooks/command-gate.json` | `preToolUse` (version 1, `"bash"` field) | none | — |
| `.gemini/settings.json` | `BeforeTool` | `run_shell_command\|write_file\|replace` | — |
| `.factory/hooks.json` | `PreToolUse` | `Execute\|Edit\|Create\|ApplyPatch` | — |
| `.augment/settings.json` | `PreToolUse` | `bash\|edit\|write` | `--deny-only` |

- [ ] **Step 1: Write the failing tests.** Parametrize over the table rows:
  - **`test_registration_parses_and_points_at_gate`:** the JSON loads, and every
    hook command contains `.agents/hooks/gate.sh` and ends with `|| true`.
  - **`test_registration_matcher`:** the matcher equals the table value.
  - **`test_deny_only_flags`:** the Codex and Augment commands contain
    `--deny-only`; the others don't.
  - **`test_old_hook_is_gone`:** `.claude/hooks/claude_permission_hook.sh` does
    not exist, and no tracked file mentions `claude_permission_hook` (except
    `docs/superpowers/`).
  - **`test_claude_settings_keeps_allow_list`:** `permissions.allow` still holds
    every entry it held before (assert the 27-entry list from the spec-time
    `.claude/settings.json`), and `hooks.Stop` is unchanged.
  - **`test_registration_command_fails_open_outside_git`:** run each command with
    `sh -c` from an empty non-git `tmp_path`, with `CLAUDE_PROJECT_DIR` unset and
    stdin = a Claude-style Bash payload. It gives exit 0 and empty stdout
    (Review Focus 5).
  - **`test_registration_command_denies_banned_in_repo`:** in a tmp git project
    holding a copy of `.agents/hooks/` and a policy banning `old_api\(`, run the
    Claude and Codex commands with a matching Bash payload. Each gives exit 0 and
    a stdout deny JSON.
- [ ] **Step 2: Run the tests and confirm they fail.**
- [ ] **Step 3: Write the registration files, edit `.claude/settings.json` and
  delete the old script.** `git rm .claude/hooks/claude_permission_hook.sh`.
- [ ] **Step 4: Run the new tests and confirm they pass.** Then run
  `uv run pytest tests/template -q` to confirm the reviewer-seam and
  root-hygiene suites stay green.
- [ ] **Step 5: Commit and push.**
  `git commit -m "Register the command gate with every confirmed harness and drop the prefix hook (#50)"`

### Task 6: Documentation

**Files:**
- Create: `.agents/hooks/README.md`
- Modify: `README.md` (new `## Agent hooks` section after
  `## Customizing the skills in your repo`)
- Modify: `AGENTS.md` (`## Hooks` section, lines 89–93)
- Modify: `tests/template/test_command_gate_registrations.py`

**Interfaces:**
- Consumes: the Task 5 registration table and its doc URLs.

- [ ] **Step 1: Write the failing test.** `test_readme_lists_every_harness`: the
  root README's `## Agent hooks` section names each of:
  - **Shipped:** Claude Code, Codex, Copilot, Gemini CLI, Factory, Augment.
  - **Via `.claude` loading:** Cursor, Devin.
  - **Follow-up:** Windsurf, Cline, Antigravity, Kiro, Amp.
  - **Not possible:** OpenCode.

  It also links `.agents/hooks/README.md`.
- [ ] **Step 2: Run the test and confirm it fails.**
- [ ] **Step 3: Write the docs.**
  - **`.agents/hooks/README.md`:**
    - what is allowed, not decided and denied;
    - the `policy.json` schema with one banned example;
    - personal versus repo-shared Claude allows;
    - the deny-only harnesses and why;
    - the payload formats recognized;
    - "Adding a harness": a 4-step recipe, including a native
      `.cursor/hooks.json` example (`beforeShellExecution` + `preToolUse`
      `Write`) for repos with Cursor's third-party hooks off;
    - the vendor doc URLs.
  - **Root README:** a short `## Agent hooks` section with a coverage table
    (harness, status, registration file) and a pointer to the hooks README.
  - **`AGENTS.md` `## Hooks`:** replace the `claude_permission_hook.sh` sentence
    with one line pointing at `.agents/hooks/` (the harness-neutral command gate:
    allow-list forwarding plus a repo-owned banned-pattern list).
- [ ] **Step 4: Run the test and confirm it passes.** Then run
  `uv run pytest tests/template -q` (the README workflow and no-org-names tests
  must stay green).
- [ ] **Step 5: Commit and push.**
  `git commit -m "Document the command gate and its harness coverage (#50)"`
