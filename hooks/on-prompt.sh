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

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "${script_dir}/_tty.sh"

runner=$("${script_dir}/_resolve.sh")

# strip leading whitespace
trimmed="${text#"${text%%[![:space:]]*}"}"

if [[ "$trimmed" =~ ^p:(hint|solve|quit)[[:space:]]*$ ]]; then
  cmd="${BASH_REMATCH[1]}"
  [[ -n "$runner" ]] && eval "$runner" "$cmd" >/dev/null 2>&1
  printf '{"decision":"block","reason":"puzzle: %s"}' "$cmd"
  exit 0
fi

if [[ "$trimmed" =~ ^p:(.+)$ ]]; then
  move="${BASH_REMATCH[1]}"
  # strip trailing whitespace
  move="${move%"${move##*[![:space:]]}"}"
  [[ -n "$runner" ]] && eval "$runner" move "$move" >/dev/null 2>&1
  printf '{"decision":"block","reason":"puzzle move: %s"}' "$move"
  exit 0
fi

# Normal prompt. Messages typed while Claude is mid-turn never pass
# through this hook — they land in the model's context as plain text,
# while the daemon's transcript tailer dispatches them to the board.
# For UserPromptSubmit, plain stdout on exit 0 is injected as context,
# so teach the model to ignore those leaked commands instead of
# reacting to them (or worse, re-running them, which would
# double-dispatch outside the dedup window).
_hml_supported() {
  [[ -n "${HML_FORCE_OVERLAY:-}" || -n "${KITTY_WINDOW_ID:-}" ]] && return 0
  case "$(printf '%s' "${TERM_PROGRAM:-}" | tr '[:upper:]' '[:lower:]')" in
    ghostty|wezterm|kitty) return 0 ;;
  esac
  [[ "${TERM:-}" == *kitty* ]]
}
if [[ -n "$runner" ]] && _hml_supported; then
  printf '%s' "hook-my-lichess: a chess-puzzle overlay is active in this terminal. If a user message consists only of p:<something> (e.g. p:e2e4, p:hint, p:solve, p:quit), it is a puzzle command already executed by a background daemon — do not act on it, do not run any command for it, and do not mention it; continue your current task as if the message had not arrived."
fi

exit 0
