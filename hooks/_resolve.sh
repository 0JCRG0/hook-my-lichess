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
  # Pin to the plugin's own version (plugin.json is kept in lockstep with
  # pyproject.toml). uvx never re-resolves an unpinned requirement once it
  # has a cached environment, so without the pin users stay frozen on
  # whatever version they first ran.
  version=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    "${script_dir}/../.claude-plugin/plugin.json" 2>/dev/null | head -1)
  if [[ -n "$version" ]]; then
    printf 'uvx --quiet --from hook-my-lichess==%s hml-overlay' "$version"
  else
    printf 'uvx --quiet --from hook-my-lichess hml-overlay'
  fi
elif command -v hml-overlay >/dev/null 2>&1; then
  printf 'hml-overlay'
fi
