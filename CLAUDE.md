# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup and common commands

```bash
# One-time setup (project uses an in-repo venv).
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # then put a Lichess token in LICHESS_TOKEN

# The product. Two pieces:
#   1. The hooks in .claude/settings.json wire UserPromptSubmit/Stop
#      to .claude/hooks/{on-prompt,on-start,on-stop}.sh, which call
#      `hml-overlay`. Drop those hooks (with absolute paths) into
#      ~/.claude/settings.json to make the overlay appear in any
#      project.
#   2. `claude` itself — run it normally. There is no wrapper binary.
.venv/bin/claude    # or just `claude` if the venv is activated

# Standalone puzzle TUI — no overlay, no Claude. Useful for
# exercising engine.py / board.py / api.py / render.py.
.venv/bin/lichess-puzzle
.venv/bin/lichess-puzzle --no-submit          # don't POST result to Lichess
.venv/bin/lichess-puzzle --rated              # report as rated (puzzle:write)
```

There is no test suite, linter, or formatter wired up. The only
verification path is manual: install/enable the plugin, submit a
prompt in Claude Code, and confirm the puzzle appears.

`HML_FORCE_OVERLAY=1` bypasses the terminal-detection heuristic and
forces the overlay path on (useful when debugging on terminals that
should support Kitty graphics but aren't detected).

## Architecture

One sidecar daemon, one engine, plus a shared overlay layer.

- `lichess_puzzle/engine.py` — `PuzzleSession`, an I/O-agnostic state
  machine that owns the board, current solution index, and `try_move()`
  semantics (UCI/SAN parsing via `python-chess`, hint/solve/quit
  commands, auto-playing the opponent's reply on a correct move). Both
  the standalone CLI and the sidecar drive it. `_parse()` tries SAN
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
  `kitty_place` / `kitty_delete` (the three Kitty escapes the sidecar
  sends).
- `lichess_puzzle/settings.py` — Pydantic v2 settings loader. Looks at
  `$HML_CONFIG`, then `<cwd>/hml.json`, then
  `~/.config/hml/settings.json`. `size` accepts a preset
  (`small|medium|large|xl|xxl`) or a positive numeric scale; `position`
  accepts a preset (`top-right|top-left|bottom-right|bottom-left|center`)
  or a `[row, col]` tuple (1-indexed cells). Invalid configs print a
  warning and fall back to defaults. `settings.example.json` at the
  repo root is a working sample.
- `lichess_puzzle/sidecar.py` — the `hml-overlay` daemon. **This is
  where the lifecycle/IPC design lives.**

### How the sidecar overlays a puzzle on Claude's TUI (v6)

This is the thing to understand before editing `sidecar.py`:

1. Claude Code owns its terminal end-to-end — there is **no PTY proxy**.
   The sidecar is a sibling process that opens the controlling tty for
   writes and emits Kitty graphics escapes. We never see or buffer
   Claude's I/O.
2. **Lifecycle.** `hml-overlay start` (UserPromptSubmit hook) captures
   `os.ttyname(0)` *before* daemonizing, double-forks, redirects
   stdin/stdout/stderr to /dev/null, and re-opens the saved tty path
   (NOT `/dev/tty` — after `setsid()` we have no controlling terminal).
   The daemon writes a per-tty PID file to `~/.cache/hml/overlay-<key>.pid`
   and binds a per-tty Unix datagram socket at
   `/tmp/hml-overlay-<key>.sock` (key = sha1 of tty name, first 12 hex
   chars). One daemon per controlling terminal so two Claudes in two
   panes coexist. `start` is idempotent — if a daemon is already alive
   it just sends `WORKING` over the socket and exits.
3. **Redraw cadence.** The daemon's main loop sits on a `selectors`
   poll with a 16 ms (~60 Hz) timeout. Every tick it re-emits
   `kitty_place(IMAGE_ID=1729, placement_id=1)` to the tty, wrapped in
   `ESC 7` / `ESC 8` (cursor save/restore) and a cursor `goto` to the
   anchor. Kitty docs guarantee that an `a=p` with the same `(i, p)`
   replaces the previous placement without flicker, so we don't need a
   delete-then-place dance for re-anchors.
4. **Image lifecycle.**
   - On daemon start: emit a "fetching…" frame, kick off the API
     fetch in a background thread, then transmit again with the
     parsed puzzle (`a=T,C=1,c=…,r=…`). `C=1` so the cursor doesn't
     move; `c`/`r` clamp the image to the available cell box.
   - Every 16 ms tick: re-place the existing image so it snaps back
     above scrolls.
   - On `IDLE`: regenerate the image with the "✓ Claude is done"
     banner. The daemon does NOT exit; the user keeps solving.
   - On `WORKING` while a celebration is in progress (puzzle just
     solved): cancel the celebration timer, fetch a new puzzle, redraw.
   - On `MOVE <text>` / `HINT` / `SOLVE` / `QUIT`: drive
     `PuzzleSession`, regenerate, retransmit.
   - On puzzle finish (lost, gave up, or celebration timer expired):
     delete the image with `kitty_delete` (`a=d,d=I`) and exit.
   - On SIGTERM/SIGINT/SIGHUP or tty going away (`os.write` ENXIO/EIO):
     same delete + clean exit.
   - **Don't reintroduce DECSTBM** (`ESC[1;Nr`). Earlier designs used
     scroll regions or PTY shrinks; both were brittle.
5. **Move-input UX is a slash-command interception.**
   `.claude/hooks/on-prompt.sh` (a UserPromptSubmit hook ordered
   *before* `on-start.sh`) reads the JSON event, extracts the prompt,
   and matches against:
     - `^/p[[:space:]]+(.+)$` → forward to `hml-overlay move <text>`
     - `^/(hint|solve|quit)$` → forward to `hml-overlay <cmd>`
   On a match it prints `{"decision":"block","reason":"…"}` and
   exits 0, suppressing the prompt before Claude sees it. Anything
   else passes through untouched (including prompts that contain
   `%` — no false-positive interception).
6. **Puzzle fetch is async** (`threading.Thread` → `queue.Queue`)
   so the redraw timer stays smooth.
7. **On terminal resize** (`SIGWINCH`): refresh layout, rebuild the
   spec at the new size, and retransmit.

### Hook integration

`.claude/settings.json` wires three hook scripts using
`$CLAUDE_PROJECT_DIR`. The `UserPromptSubmit` block runs `on-prompt.sh`
*then* `on-start.sh` — the first intercepts puzzle slash commands,
the second nudges/spawns the daemon (always; the prompt hook's
`block` decision only suppresses the prompt, not subsequent hooks).
The `Stop` block runs `on-stop.sh`, which sends `IDLE`.

To make the overlay appear in *any* project, copy the `hooks` block
to `~/.claude/settings.json` with absolute paths to the hook scripts
in this repo.
