#!/usr/bin/env bash
# UserPromptSubmit hook: spawn the puzzle overlay daemon (or nudge an
# existing one). Silent no-op if hml-overlay isn't reachable or the
# terminal doesn't support Kitty graphics.
input=$(cat)

runner=$("$(dirname -- "${BASH_SOURCE[0]}")/_resolve.sh")
[[ -z "$runner" ]] && exit 0

# Pull the transcript path out of the event JSON and pass it to the
# daemon so it can tail real-time queued user messages mid-turn (the
# only Claude Code surface that surfaces text typed during streaming).
transcript=$(printf '%s' "$input" | python3 -c '
import json, sys
try: print(json.loads(sys.stdin.read()).get("transcript_path",""), end="")
except Exception: pass
')

HML_DEBUG=1 HML_TRANSCRIPT="$transcript" eval "$runner" start </dev/null >/dev/null 2>&1 &
exit 0
