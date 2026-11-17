#!/bin/sh
# Fail open: no python3 or no hook.py -> exit 0, no output (normal prompt).
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0
command -v python3 >/dev/null 2>&1 && [ -f "$DIR/hook.py" ] || exit 0
exec python3 "$DIR/hook.py" "$@"
