# Command gate: live E2E test kit (#50)

The in-session E2E ran every registration's exact command against harness-shaped
payloads. This kit checks what that can't: that each real harness loads its
registration and acts on the gate's answer. Run it for the harnesses you use.
Claude Code is the must-pass; the rest are best-effort.

## Setup (once, from the repo root on this branch)

1. Add a temporary banned pattern:

   ```sh
   python3 - <<'EOF'
   import json
   p = ".agents/hooks/policy.json"
   d = json.load(open(p))
   d["banned"] = [{"pattern": "gate_canary\\(", "message": "live E2E canary", "replacement": "nothing"}]
   json.dump(d, open(p, "w"))
   EOF
   ```

2. After testing, restore it:

   ```sh
   git checkout -- .agents/hooks/policy.json
   ```

## The four checks (ask the agent to run each command exactly)

| # | Ask the agent to run | Expected | Fails if |
|---|---|---|---|
| A | `make lint && make tests` | Runs with **no permission prompt** (Claude Code, Copilot, Gemini, Factory). Codex and Augment are deny-only: their own approval flow applies. | You get a prompt in Claude Code |
| B | `make lint && touch /tmp/gate-check` | You **are prompted** (the gate gives no opinion on the unlisted `touch`) | It runs without a prompt |
| C | `python3 -c 'gate_canary()'` | **Blocked**. The agent sees `Blocked: this command matches banned pattern gate_canary\(: live E2E canary. Use instead: nothing.` | It runs, or you're only prompted |
| D | Create `scratch_canary.py` containing `gate_canary()` (a file write, not a shell command) | **Blocked** with `Blocked: this edit to …/scratch_canary.py matches banned pattern …` (Copilot is shell-only: skip D there) | The file is written |

## Per harness

- **Claude Code:**
  1. Start a new session in the repo: `make cc`, or `claude` from the root. Hooks load at startup.
  2. Run `/hooks` to confirm that `PreToolUse` lists `sh "$CLAUDE_PROJECT_DIR/.agents/hooks/gate.sh" || true`.
  3. Run checks A-D.
- **Cursor:** open the repo. Cursor runs the Claude registration through its third-party hooks support.
  1. In Cursor Settings → Hooks, confirm that third-party (Claude Code) hooks are enabled and the gate is listed.
  2. Run checks A-D in Agent mode.
- **Codex:**
  1. Run `codex` in the repo.
  2. Run `/hooks` and trust the project hook (project hooks only load in trusted projects).
  3. Run checks C and D. Check A should show Codex's normal approval behavior, because Codex is deny-only.
- **Copilot CLI:** run `copilot` in the repo. It reads `.github/hooks/command-gate.json`. Run checks A, B and C.
- **Gemini CLI:**
  1. Run `gemini` in the repo.
  2. Accept the project-hook warning the first time.
  3. Run checks A-D.
- **Factory (`droid`):** run it in the repo, then checks A-D.
- **Augment (`auggie`):** run it in the repo, then checks C and D (Augment is deny-only).

## Pass / fail

- **Pass:** Claude Code passes A-D, and every other harness you tried passes its listed checks.
- **Fail:** any wrong allow (B runs unprompted), or any missed deny (C or D runs). Report the harness, the check, and what the agent printed.

**First things to check on a failure:**

1. `sh .agents/hooks/gate.sh < /dev/null; echo rc=$?` must print `rc=0` with no output.
2. `python3 --version` must be 3.9 or newer.
3. The harness's hooks UI must list the registration.
4. For Codex, the project hook must be trusted.
