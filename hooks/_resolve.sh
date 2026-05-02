#!/usr/bin/env bash
# Locate the hml-overlay binary and echo a runner command on stdout.
# Resolution order:
#   1. Sibling .venv (dogfooding: this script lives at <repo>/hooks/, so
#      <repo>/.venv/bin/hml-overlay is one level up).
#   2. uvx (production: fetch + cache hook-my-lichess from PyPI).
#   3. hml-overlay on PATH (manual install).
# Echoes nothing if no runner is available; caller treats that as a no-op.
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
local_bin="${script_dir}/../.venv/bin/hml-overlay"

if [[ -x "$local_bin" ]]; then
  printf '%s' "$local_bin"
elif command -v uvx >/dev/null 2>&1; then
  printf 'uvx --quiet --from hook-my-lichess hml-overlay'
elif command -v hml-overlay >/dev/null 2>&1; then
  printf 'hml-overlay'
fi
