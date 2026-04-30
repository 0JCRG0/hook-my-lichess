# hook-my-lichess

A Lichess puzzle that floats over the **same terminal** Claude Code
is running in. While Claude is working, the puzzle hovers in the
top-right corner. You play moves by typing `p:<move>` as a prompt —
**including while Claude is mid-response** — and the board updates
live, no PTY proxy, no wrapper binary in the launch path.

## How it works (v6, sidecar architecture)

Claude Code owns its terminal end-to-end. There is no wrapper. The
overlay lives in a sibling process:

- **Hook-launched daemon.** A `UserPromptSubmit` hook calls
  `hml-overlay start`, which double-forks a Python daemon that opens
  `/dev/tty` for writes and emits Kitty graphics escapes. Claude is
  never resized, never has its I/O intercepted.
- **Independent 16 ms redraw clock.** The daemon re-emits
  `kitty_place(image_id, p=1)` on its own timer, decoupled from
  Claude's stdout — so the image snaps back above scrolls without
  waiting for Claude to emit a byte.
- **Two input surfaces.** A turn-boundary path via the
  `UserPromptSubmit` hook (`p:<move>` is intercepted with
  `decision: "block"` so the prompt never reaches Claude). And a
  **mid-turn path** via the daemon tailing Claude Code's session
  JSONL transcript at `~/.claude/projects/<project>/<session>.jsonl`
  for `queue-operation enqueue` and `attachment.queued_command`
  records — Claude Code logs queued user input the moment you type
  it, so the daemon dispatches mid-turn moves with no documented
  hook involvement at all. An 8-second dedup window swallows the
  duplicate when `UserPromptSubmit` later replays the same prompt.

See `docs/v5-wrapper-vs-v6-sidecar.md` for the full architecture
comparison and the bugs that drove the rewrite.

## Setup

```bash
cd /Users/juanreyesgarcia/Dev/hook-my-lichess
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
# put your Lichess personal token in .env
```

## Run it

Just run Claude Code as you normally would. There is **no wrapper
binary**.

```bash
.venv/bin/claude   # or just `claude` if the venv is activated
```

The hooks in this project's `.claude/settings.json` spawn the
overlay daemon on every prompt submission. The daemon auto-detects
whether the terminal supports Kitty graphics (Ghostty / Kitty /
WezTerm); on terminals that don't (iTerm2, Terminal.app, tmux), it
exits silently — Claude works as normal.

## Playing the puzzle

Submit any prompt to Claude. The board appears in the top-right
within ~1 s.

Type a puzzle command **as your prompt**:

- `p:e2e4` — submit a UCI move
- `p:Nf3` — submit a SAN move (piece letters uppercase: `Nf3`, not `nf3`)
- `p:hint` — hint (which square the piece moves from)
- `p:solve` — give up and reveal the move
- `p:quit` — close the puzzle

The hook intercepts these (`decision: "block"`) so Claude never
sees them. **You can also type them while Claude is mid-response** —
they'll queue, the daemon's transcript tailer will pick them up
within ~100 ms, and the board updates without waiting for Claude's
turn to finish. (Anything that doesn't start with `p:` passes
through to Claude untouched.)

When Claude finishes its turn, the banner flips to "✓ Claude is done"
and you can keep solving at your own pace.

## Customizing

Drop a `settings.json` to tune size and position. The daemon looks
at (priority order):

1. `$HML_CONFIG` (if set)
2. `<cwd>/hml.json`
3. `~/.config/hml/settings.json`

Schema (Pydantic v2):

```json
{
  "size": "xxl",
  "position": "center"
}
```

- **`size`** — one of `"small"` (0.75×), `"medium"` (1×, default), `"large"` (1.25×), `"xl"` (1.5×), `"xxl"` (2×), or any positive number for an exact scale (e.g. `"size": 1.7`).
- **`position`** — one of `"top-right"` (default), `"top-left"`, `"bottom-right"`, `"bottom-left"`, `"center"`, or a `[row, col]` pair for an exact 1-indexed cell (e.g. `"position": [3, 80]`).

Sample at `settings.example.json`. Copy to `~/.config/hml/settings.json`
to apply it everywhere.

## Going global

Hooks live in this project's `.claude/settings.json` and reference
`$CLAUDE_PROJECT_DIR/.venv/bin/hml-overlay`. To make the overlay
appear in any project:

1. Make sure `hml-overlay` is on your `$PATH` (e.g. symlink
   `.venv/bin/hml-overlay` into `~/.local/bin`).
2. Copy the `hooks` block from this repo's
   `.claude/settings.json` to `~/.claude/settings.json` with
   absolute paths to the hook scripts.

## Force-enable the overlay (debugging)

Set `HML_FORCE_OVERLAY=1` to bypass the terminal-detection heuristic
and always emit Kitty graphics escapes.

`HML_DEBUG=1` writes diagnostic lines to `/tmp/hml-sidecar-debug.log`.

## Standalone puzzle (no Claude, no overlay)

Same engine, plain stdin loop:

```bash
.venv/bin/lichess-puzzle
```

Useful for exercising `engine.py`, `board.py`, `api.py`, `render.py`
without touching the overlay code path.

## Smoke test

```bash
.venv/bin/python scripts/smoke_sidecar.py
```

Spawns `hml-overlay start` in a synthetic 40×120 PTY with
`HML_FORCE_OVERLAY=1` and asserts: PID file appears, Kitty `a=T` /
`a=p` / `a=d` escapes in the right places, MOVE/IDLE retransmit, no
DECSTBM. Returns 0 on success, 2 on a failed assertion.
