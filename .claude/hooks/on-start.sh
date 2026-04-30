#!/usr/bin/env bash
# Tells the hml wrapper that Claude has started working on a task.
# No-op (silent exit 0) when not running under hml.
cat >/dev/null
[[ -n "$HML_SOCKET" && -S "$HML_SOCKET" ]] && \
  python3 -c 'import socket,sys,os; s=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM); s.sendto(b"WORKING\n", os.environ["HML_SOCKET"])' \
  >/dev/null 2>&1 || true
exit 0
