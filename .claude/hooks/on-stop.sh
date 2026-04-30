#!/usr/bin/env bash
# Tells the hml wrapper that Claude has finished its turn.
cat >/dev/null
[[ -n "$HML_SOCKET" && -S "$HML_SOCKET" ]] && \
  python3 -c 'import socket,sys,os; s=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM); s.sendto(b"IDLE\n", os.environ["HML_SOCKET"])' \
  >/dev/null 2>&1 || true
exit 0
