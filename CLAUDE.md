# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup and common commands

```bash
# One-time setup (project uses an in-repo venv).
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # then put a Lichess token in LICHESS_TOKEN

# Run the wrapper (the actual product): launches Claude Code under a PTY proxy
# that overlays a Lichess puzzle while Claude is working.
.venv/bin/hml claude

# Standalone puzzle TUI — bypasses the PTY wrapper entirely. Useful for
# exercising engine.py / board.py / api.py without the terminal-overlay path.
.venv/bin/lichess-puzzle
.venv/bin/lichess-puzzle --no-submit          # don't POST result to Lichess
.venv/bin/lichess-puzzle --rated              # report as rated (puzzle:write)

# End-to-end smoke test for the wrapper. Spawns `hml bash -i` in a synthetic
# 40×120 PTY, drives the WORKING/IDLE socket protocol, and asserts the v3
# invariants (PTY shrink instead of DECSTBM, focus chip toggling, etc.).
.venv/bin/python scripts/smoke_wrapper.py
```

There is no test suite, linter, or formatter wired up. `smoke_wrapper.py` is the
only automated check; it returns exit 0 on success, 2 on a failed assertion.

## Architecture

Two entry points, one engine.

- `lichess_puzzle/engine.py` — `PuzzleSession`, an I/O-agnostic state machine
  that owns the board, current solution index, and `try_move()` semantics
  (UCI/SAN parsing via `python-chess`, hint/solve/quit commands, auto-playing
  the opponent's reply on a correct move). Both the standalone CLI and the
  wrapper drive it. Move parsing tries SAN first, then falls back to UCI;
  UCI input is lowercased before parsing but SAN is not, so `nf3` fails
  while `Nf3` and `g1f3` both work.
- `lichess_puzzle/board.py` — `parse_puzzle()` turns a Lichess `/api/puzzle/next`
  payload into a `Puzzle` dataclass. Lichess convention: replay `pgn[:initialPly]`
  to reach the position, then push `pgn[initialPly]` as the opponent's *setup*
  move; `solution[0]` is the user's first move.
- `lichess_puzzle/api.py` — thin `httpx` wrapper around `/api/puzzle/next` and
  `/api/puzzle/batch/mix`. Loads `LICHESS_TOKEN` from `.env` at the project
  root via `python-dotenv`.
- `lichess_puzzle/render.py` — ANSI Unicode board renderer (256-color
  backgrounds, last-move highlighting, perspective flip).
- `lichess_puzzle/cli.py` — standalone `lichess-puzzle` TUI (plain stdin loop).
- `lichess_puzzle/wrapper.py` — the `hml` PTY proxy. **This is where the
  interesting/fragile design lives.**

### How the wrapper overlays a puzzle on Claude's TUI

This is the thing to understand before editing `wrapper.py`:

1. `hml claude` calls `pty.fork()` and execs the child argv inside a PTY.
   The parent puts stdin into raw mode and forwards bytes both directions.
2. The wrapper binds a Unix datagram socket at `/tmp/hml-<pid>.sock` and
   exports its path as `$HML_SOCKET`. The hook scripts in `.claude/hooks/`
   send a one-line `WORKING\n` (on `UserPromptSubmit`) or `IDLE\n` (on
   `Stop`) datagram to that socket; the wrapper reacts in `_on_event()`.
3. **Layout strategy is "shrink the PTY, don't use a scrolling region."**
   Earlier versions used DECSTBM (`ESC[1;Nr`); they were brittle because some
   Claude render paths print past the region. Current design: on `WORKING`
   the wrapper resizes the *child PTY* to `(rows - PUZZLE_ROWS, cols)` and
   sends `SIGWINCH`, so Claude believes the terminal is shorter and never
   touches the bottom rows. The wrapper owns those rows and redraws them on
   every Claude byte (`_handle_master`) and every relevant state change. On
   `IDLE` / quit, the PTY is resized back to full and another SIGWINCH lets
   Claude redraw at the original size. The `smoke_wrapper.py` test
   *explicitly asserts* no DECSTBM escape is emitted — don't reintroduce one.
4. **Focus model.** Default focus is `claude` — Claude keeps the keyboard
   even while a puzzle is showing. **Ctrl-G** (`0x07`) toggles focus to the
   puzzle's line editor and back. While focus is `puzzle`, bytes are routed
   to `_handle_puzzle_byte` (which feeds `PuzzleSession.try_move`); while
   focus is `claude`, bytes go straight to the child fd. Ctrl-G is *always*
   consumed by the wrapper and never forwarded.
5. **Cursor parking.** `_redraw_puzzle()` saves/restores the cursor with
   `ESC 7` / `ESC 8` so Claude's TUI isn't disturbed. The exception: when
   focus is `puzzle` *and* Claude has been quiet >100ms, the cursor is
   parked in the puzzle prompt instead — otherwise it fights Claude's TUI.
6. **Puzzle fetch is async** (`threading.Thread` → `queue.Queue`) so the
   event loop stays responsive; results are picked up in
   `_drain_fetch_queue` on each tick.
7. **Below `MIN_TERMINAL_ROWS = 22`**, the puzzle silently doesn't activate
   (`Layout.has_room_for_puzzle()`). `PUZZLE_ROWS = 14` is the height it
   reserves at the bottom.

### Hook integration

`.claude/settings.json` wires the two hook scripts. They are deliberately
no-ops unless `HML_SOCKET` is set and points to a live socket — that is the
only way they distinguish "running under `hml`" from "running under plain
`claude`". If you change the socket protocol, update both `on-start.sh` and
`on-stop.sh` (they each open a `SOCK_DGRAM` and send a single line).

To make the puzzle work in *any* project, the hooks must be copied into
`~/.claude/settings.json` with absolute paths (the project-local file uses
`$CLAUDE_PROJECT_DIR`).
