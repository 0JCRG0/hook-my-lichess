#!/usr/bin/env bash
# Stop hook: tell the daemon Claude is done. Daemon stays alive so the
# user can keep solving; banner just changes.
cat >/dev/null

if [[ -x "$CLAUDE_PROJECT_DIR/.venv/bin/hml-overlay" ]]; then
  bin="$CLAUDE_PROJECT_DIR/.venv/bin/hml-overlay"
elif command -v hml-overlay >/dev/null 2>&1; then
  bin="hml-overlay"
else
  exit 0
fi

"$bin" idle </dev/null >/dev/null 2>&1 &
exit 0
# Note: idle/move/etc. paths only need socket access (no /dev/tty),
# so stderr can be silenced.
