# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup and common commands

```bash
# One-time setup (project uses an in-repo venv).
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # then put a Lichess token in LICHESS_TOKEN

# Run the wrapper (the actual product): launches Claude Code under a PTY
# proxy that floats a Lichess puzzle image over the same terminal while
# Claude is working.
.venv/bin/hml claude

# Standalone puzzle TUI — bypasses the wrapper entirely. Useful for
# exercising engine.py / board.py / api.py / render.py without touching
# the overlay code path. `python -m lichess_puzzle` is equivalent.
.venv/bin/lichess-puzzle
.venv/bin/lichess-puzzle --no-submit          # don't POST result to Lichess
.venv/bin/lichess-puzzle --rated              # report as rated (puzzle:write)

# End-to-end smoke test for the wrapper. Spawns `hml bash -i` in a
# synthetic 40×120 PTY with HML_FORCE_OVERLAY=1 and asserts the v5
# invariants (no DECSTBM, PTY size unchanged, Kitty a=T/a=p/a=d escapes
# in the right places, snoop+% intercept, literal-% pass-through).
.venv/bin/python scripts/smoke_wrapper.py
```

There is no test suite, linter, or formatter wired up. `smoke_wrapper.py`
is the only automated check; it returns exit 0 on success, 2 on a failed
assertion. Run it after any non-trivial change to `wrapper.py`.

`HML_FORCE_OVERLAY=1` bypasses the terminal-detection heuristic and
forces the overlay path on (used by the smoke test, and useful when
debugging on terminals that should support Kitty graphics but aren't
detected).

## Architecture

Two entry points, one engine, plus an overlay layer.

- `lichess_puzzle/engine.py` — `PuzzleSession`, an I/O-agnostic state
  machine that owns the board, current solution index, and `try_move()`
  semantics (UCI/SAN parsing via `python-chess`, hint/solve/quit
  commands, auto-playing the opponent's reply on a correct move). Both
  the standalone CLI and the wrapper drive it. `_parse()` tries SAN
  first, then falls back to UCI; UCI input is `.lower()`'d before
  parsing but SAN is not, so `nf3` fails while `Nf3` and `g1f3` both
  work.
- `lichess_puzzle/board.py` — `parse_puzzle()` turns a Lichess
  `/api/puzzle/next` payload into a `Puzzle` dataclass. Lichess
  convention: replay `pgn[:initialPly]` to reach the position, then
  push `pgn[initialPly]` as the opponent's *setup* move; `solution[0]`
  is the user's first move.
- `lichess_puzzle/api.py` — thin `httpx` wrapper around
  `/api/puzzle/next` and `/api/puzzle/batch/mix`. Loads `LICHESS_TOKEN`
  from `.env` at the project root via `python-dotenv`.
- `lichess_puzzle/render.py` — ANSI Unicode board renderer used by the
  standalone CLI only.
- `lichess_puzzle/cli.py` — standalone `lichess-puzzle` TUI (plain
  stdin loop, ANSI rendering).
- `lichess_puzzle/overlay.py` — Kitty-graphics PNG renderer + protocol
  helpers. Owns `OverlaySpec` (all dimensions live there;
  `from_scale()` produces a scaled spec from `settings.size`),
  `is_supported()` (capability sniff), and `kitty_transmit` /
  `kitty_place` / `kitty_delete` (the three Kitty escapes the wrapper
  sends).
- `lichess_puzzle/settings.py` — Pydantic v2 settings loader. Looks at
  `$HML_CONFIG`, then `<cwd>/hml.json`, then
  `~/.config/hml/settings.json`. `size` accepts a preset
  (`small|medium|large|xl|xxl`) or a positive numeric scale; `position`
  accepts a preset (`top-right|top-left|bottom-right|bottom-left|center`)
  or a `[row, col]` tuple (1-indexed cells). Invalid configs print a
  warning and fall back to defaults. `settings.example.json` at the
  repo root is a working sample.
- `lichess_puzzle/wrapper.py` — the `hml` PTY proxy. **This is where
  the interesting/fragile design lives.**

### How the wrapper overlays a puzzle on Claude's TUI (v5)

This is the thing to understand before editing `wrapper.py`:

1. `hml claude` calls `pty.fork()` and execs the child argv inside a
   PTY. The parent puts stdin into raw mode and forwards bytes both
   directions. **The child PTY is always sized to the full terminal**
   — Claude is never resized, and never sees the image.
2. The wrapper binds a Unix datagram socket at `/tmp/hml-<pid>.sock`
   and exports its path as `$HML_SOCKET`. The hook scripts in
   `.claude/hooks/` send a one-line `WORKING\n` (on `UserPromptSubmit`)
   or `IDLE\n` (on `Stop`) datagram to that socket; the wrapper
   reacts in `_on_event()`. Hooks are silent no-ops when `HML_SOCKET`
   is unset — that's how they distinguish "running under `hml`" from
   "running under plain `claude`". If you change the socket protocol,
   update both `on-start.sh` and `on-stop.sh`.
3. **Layout strategy is "Kitty graphics overlay, never resize."** The
   puzzle is rendered to a PNG and floated above the text grid via the
   Kitty graphics protocol (works in Ghostty / Kitty / WezTerm,
   detected by `overlay.is_supported()`; on other terminals the
   overlay path is skipped entirely and the wrapper proxies Claude
   transparently). Earlier designs used `DECSTBM` scroll regions or
   PTY shrinks; both were brittle. **Don't reintroduce DECSTBM** —
   `smoke_wrapper.py` explicitly asserts no `ESC[1;Nr` is emitted, and
   that the child PTY's `$LINES` is unchanged across `WORKING`.
4. **Image lifecycle.** The image uses a fixed `IMAGE_ID = 1729` and
   placement id 1.
   - On `WORKING`: render PNG, transmit with `a=T,C=1,c=…,r=…` (`C=1`
     so the cursor doesn't move and the image doesn't scroll the
     screen; `c`/`r` cap the image to the available cell box).
   - On every byte from Claude (`_handle_master`): re-place the
     existing image with `a=p` so it stays on top of scrolls. This is
     throttled to once per 50 ms so we don't flood the terminal.
   - On `IDLE`: regenerate the image with the "✓ Claude is done"
     banner.
   - On puzzle finish / wrapper exit: delete the image with `a=d`.
   - Cursor is always saved (`ESC 7`) before any overlay write and
     restored (`ESC 8`) after, so Claude's TUI cursor isn't disturbed.
5. **Snoop-and-`%` input model.** When a puzzle is active, every
   stdin byte is *forwarded to Claude as normal* and *also fed into
   `snoop_buffer`*. The buffer is reset on Enter / ESC / Ctrl-C; only
   printable ASCII is appended; `0x7f`/`0x08` pop the last char. When
   the byte is `%` and the current buffer matches a chess move
   (`MOVE_RE = [a-h1-8RNBQKO=+#xX\-O0]{2,}`, length ≥ 3) or a command
   token (`h, hint, ?, s, solve, q, quit`), the wrapper:
     - drops the `%` (does **not** forward it),
     - sends `len(buffer)` `BACKSPACE` (`0x7f`) bytes back to the child
       so Claude wipes the move characters from its input box,
     - feeds the buffer to `PuzzleSession.try_move`,
     - and updates the overlay.
   Anything that doesn't match (e.g. typing `wait 30%`) passes through
   untouched, including the literal `%`. Pressing Enter still submits
   the line to Claude as normal.
6. **Puzzle fetch is async** (`threading.Thread` → `queue.Queue`) so
   the event loop stays responsive; results are picked up in
   `_drain_fetch_queue` on each `select` tick.
7. **On terminal resize** (`SIGWINCH`): refresh layout, re-pass the
   full size to the child PTY (Claude redraws), and re-place the
   image at the new anchor.

### Hook integration

`.claude/settings.json` wires the two hook scripts using
`$CLAUDE_PROJECT_DIR`. To make the overlay appear in *any* project,
copy the `hooks` block to `~/.claude/settings.json` with absolute
paths to the hook scripts in this repo.
