#!/usr/bin/env bash
# Resolve the pty device of the terminal Claude Code is running in and
# export it as HML_TTY. Hook processes have no controlling terminal
# (Claude Code detaches them into fresh sessions), and the backgrounded
# daemon gets reparented to launchd the instant the hook script exits —
# so the process-tree walk must happen here, in the foreground hook
# script, while the ancestor chain up to Claude Code is still intact.
_hml_find_tty() {
  local pid=$$ ppid tty i
  for ((i = 0; i < 32; i++)); do
    read -r ppid tty <<<"$(ps -o ppid=,tty= -p "$pid" 2>/dev/null)"
    [[ -z "$ppid" ]] && return 1
    if [[ -n "$tty" && "$tty" != "??" && "$tty" != "-" ]]; then
      printf '/dev/%s' "$tty"
      return 0
    fi
    (( ppid <= 1 )) && return 1
    pid=$ppid
  done
  return 1
}

if [[ -z "${HML_TTY:-}" ]]; then
  HML_TTY=$(_hml_find_tty) && export HML_TTY || unset HML_TTY
fi
