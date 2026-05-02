#!/usr/bin/env bash
# Stop hook: tell the daemon Claude is done. Daemon stays alive so the
# user can keep solving; banner just changes.
cat >/dev/null

runner=$("$(dirname -- "${BASH_SOURCE[0]}")/_resolve.sh")
[[ -z "$runner" ]] && exit 0

eval "$runner" idle </dev/null >/dev/null 2>&1 &
exit 0
# Note: idle/move/etc. paths only need socket access (no /dev/tty),
# so stderr can be silenced.
