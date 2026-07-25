#!/usr/bin/env bash
# UserPromptSubmit hook: spawn the puzzle overlay daemon (or nudge an
# existing one). Silent no-op if hml-overlay isn't reachable or the
# terminal doesn't support Kitty graphics.
input=$(cat)

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "${script_dir}/_tty.sh"

runner=$("${script_dir}/_resolve.sh")
[[ -z "$runner" ]] && exit 0

# Pull the prompt and transcript path out of the event JSON. The
# transcript is passed to the daemon so it can tail real-time queued
# user messages mid-turn (the only Claude Code surface that surfaces
# text typed during streaming).
eval "$(printf '%s' "$input" | python3 -c '
import json, shlex, sys
try: d = json.loads(sys.stdin.read())
except Exception: d = {}
print("prompt=" + shlex.quote(d.get("prompt", "")))
print("transcript=" + shlex.quote(d.get("transcript_path", "")))
')"

# Puzzle commands are owned by on-prompt.sh, and a blocked prompt never
# starts a turn — nudging the daemon to WORKING here would churn the
# banner and can even respawn the puzzle mid-celebration.
trimmed="${prompt#"${prompt%%[![:space:]]*}"}"
[[ "$trimmed" == p:* ]] && exit 0

HML_DEBUG=1 HML_TRANSCRIPT="$transcript" eval "$runner" start </dev/null >/dev/null 2>&1 &
exit 0
