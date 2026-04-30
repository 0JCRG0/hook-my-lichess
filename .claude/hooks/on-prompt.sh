#!/usr/bin/env bash
# UserPromptSubmit hook: intercept puzzle commands and forward to the
# overlay daemon. Returns {"decision":"block",...} for matching prompts
# so Claude does not process them. Anything else passes through.
#
# Commands (chosen to NOT start with `/`, so they survive Claude
# Code's built-in slash-command parser):
#   p:<move>   — submit a move (e2e4, Nf3, etc.)
#   p:hint     — hint
#   p:solve    — give up; show solution
#   p:quit     — close the puzzle
input=$(cat)
text=$(printf '%s' "$input" | python3 -c '
import json, sys
try: print(json.loads(sys.stdin.read()).get("prompt",""), end="")
except Exception: pass
')

if [[ -x "$CLAUDE_PROJECT_DIR/.venv/bin/hml-overlay" ]]; then
  bin="$CLAUDE_PROJECT_DIR/.venv/bin/hml-overlay"
elif command -v hml-overlay >/dev/null 2>&1; then
  bin="hml-overlay"
else
  bin=""
fi

# strip leading whitespace
trimmed="${text#"${text%%[![:space:]]*}"}"

if [[ "$trimmed" =~ ^p:(hint|solve|quit)[[:space:]]*$ ]]; then
  cmd="${BASH_REMATCH[1]}"
  [[ -n "$bin" ]] && "$bin" "$cmd" >/dev/null 2>&1
  printf '{"decision":"block","reason":"puzzle: %s"}' "$cmd"
  exit 0
fi

if [[ "$trimmed" =~ ^p:(.+)$ ]]; then
  move="${BASH_REMATCH[1]}"
  # strip trailing whitespace
  move="${move%"${move##*[![:space:]]}"}"
  [[ -n "$bin" ]] && "$bin" move "$move" >/dev/null 2>&1
  printf '{"decision":"block","reason":"puzzle move: %s"}' "$move"
  exit 0
fi

exit 0
