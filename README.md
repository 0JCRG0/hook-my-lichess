# hook-my-lichess

A Lichess puzzle that floats over the **same terminal** Claude Code is running in. While Claude is working, the puzzle hovers in the top-right corner; you keep typing into Claude's input box, and a special terminator sends your move to the puzzle.

It works by wrapping `claude` in a small PTY proxy that:

- Listens on a Unix socket for `WORKING` / `IDLE` events from Claude Code's `UserPromptSubmit` and `Stop` hooks.
- Renders the puzzle to a PNG and floats it via the **Kitty graphics protocol** — a true overlay layer above the text grid. Claude is not resized and never sees the image.
- Snoops the keystrokes you type into Claude's input. When the snooped text looks like a chess move and ends with `%`, the wrapper intercepts: it parses the move, sends backspaces back to Claude so the prefix gets wiped from Claude's input field, and updates the puzzle. Pressing Enter as normal still sends your line to Claude.

## Setup

```bash
cd /Users/juanreyesgarcia/Dev/hook-my-lichess
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
# put your Lichess personal token in .env
```

## Run it

```bash
cd /Users/juanreyesgarcia/Dev/hook-my-lichess
.venv/bin/hml claude
```

The wrapper auto-detects whether the terminal supports Kitty graphics (Ghostty, Kitty, WezTerm). On other terminals (iTerm2, Terminal.app, tmux, …) it just runs Claude transparently — no overlay, no resize, no surprises.

## Usage in the puzzle

Submit any prompt to Claude. A board image appears in the top-right corner. Type your move directly into Claude's input box and end with `%`:

- `e2e4%` — submit a UCI move
- `Nf3%` — submit a SAN move
- `h%` — hint (which square the piece moves from)
- `s%` — give up and reveal the move
- `q%` — quit the puzzle

The `%` is consumed by the wrapper, and the move characters are wiped from Claude's input via backspaces. **Pressing Enter still sends your line to Claude as normal.** A literal `%` after non-move text (`"wait 30%"`) passes through untouched.

When Claude finishes, the puzzle stays open with a "Claude is done" status. Quit any time with `q%`.

## Going global

Hooks live in this project's `.claude/settings.json`. To make the overlay appear in any project, copy the `hooks` block to `~/.claude/settings.json` with absolute paths to the hook scripts in this repo.

## Force-enable the overlay (debugging)

Set `HML_FORCE_OVERLAY=1` to bypass the terminal-detection heuristic and always emit Kitty graphics escape sequences.

## Standalone puzzle (debugging the engine)

Skip the wrapper entirely:

```bash
.venv/bin/lichess-puzzle
```

Plain stdin loop, same engine.
