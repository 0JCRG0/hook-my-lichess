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

## Install

`hook-my-lichess` is distributed as a Claude Code plugin. Two slash
commands and you're done.

**Prereqs (one-time):**

```bash
# uv runs the Python daemon on demand and caches it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Lichess personal token, see https://lichess.org/account/oauth/token
export LICHESS_TOKEN=lip_xxxxxxxxxxxxxxxx   # add to your shell rc
```

**Install in Claude Code:**

```
/plugin marketplace add 0JCRG0/hook-my-lichess
/plugin install hook-my-lichess@hml
```

That's it. The next prompt you submit will fire the hooks; `uvx`
fetches `hook-my-lichess` from PyPI on first run (~2 s) and caches
it. Subsequent invocations are instant.

The daemon auto-detects whether the terminal supports Kitty graphics
(Ghostty / Kitty / WezTerm); on terminals that don't (iTerm2,
Terminal.app, tmux), it exits silently — Claude works as normal.

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

## Customizing the board (size & position)

Generate a default settings file:

```bash
uvx --from hook-my-lichess hml-overlay init-config
# wrote default settings to ~/.config/hml/settings.json
```

Edit `~/.config/hml/settings.json`:

```json
{
  "size": "xxl",
  "position": "center"
}
```

- **`size`** — one of `"small"` (0.75×), `"medium"` (1×, default), `"large"` (1.25×), `"xl"` (1.5×), `"xxl"` (2×), or any positive number for an exact scale (e.g. `"size": 1.7`).
- **`position`** — one of `"top-right"` (default), `"top-left"`, `"bottom-right"`, `"bottom-left"`, `"center"`, or a `[row, col]` pair for an exact 1-indexed cell (e.g. `"position": [3, 80]`).

The daemon checks three locations in order: `$HML_CONFIG`, then
`<cwd>/hml.json`, then `~/.config/hml/settings.json`. The per-user
file is the one you usually want.

## Developing locally

If you want to hack on the overlay itself instead of just using it:

```bash
git clone https://github.com/0JCRG0/hook-my-lichess
cd hook-my-lichess
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # put LICHESS_TOKEN here
```

The hooks in this repo's `.claude/settings.json` reference
`$CLAUDE_PROJECT_DIR/hooks/*.sh`, and those scripts prefer the local
`.venv/bin/hml-overlay` over `uvx` when present — so working inside
this repo always uses your in-tree code, no rebuild needed.

If you also have the marketplace plugin installed, disable it for
this repo (`/plugin disable hook-my-lichess`) so hooks don't double-fire.

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
