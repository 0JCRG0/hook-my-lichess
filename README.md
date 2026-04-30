# hook-my-lichess

A Lichess puzzle that lives in the **same terminal** as Claude Code. While
Claude is working on a task, the bottom of your terminal becomes a chess
board you can solve with the keyboard. When Claude finishes, the puzzle
gets out of the way.

It works by wrapping `claude` in a small PTY proxy that:

- Uses `DECSTBM` (set scrolling region) to confine Claude's output to the
  top of the terminal — no Kitty graphics required, works in iTerm2,
  Ghostty, Terminal.app, anything with a real VT.
- Listens on a Unix socket for `WORKING` / `IDLE` events from
  Claude Code's `UserPromptSubmit` and `Stop` hooks.
- Routes your keystrokes to the puzzle's line editor while Claude is busy,
  and back to Claude when it's idle. Toggle anytime with **Ctrl-G**.

## Setup

```bash
cd /Users/juanreyesgarcia/Dev/hook-my-lichess
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
# put your token (https://lichess.org/account/oauth/token) in .env
```

The hook scripts are already wired in `.claude/settings.json`. They only do
anything when the `HML_SOCKET` env var is set — i.e. when Claude is launched
through `hml`.

## Run it

```bash
cd /Users/juanreyesgarcia/Dev/hook-my-lichess
.venv/bin/hml claude
```

(Or alias it: `alias hclaude='/full/path/to/.venv/bin/hml claude'`.)

Submit any prompt. The bottom of your terminal will paint a board with the
**puzzle focus** chip lit. Type your move and hit Enter:

- `e2e4` (UCI) or `Nf3` (SAN) — submit a move
- `h` — hint (which square the piece moves from)
- `s` — give up and reveal the move
- `q` — quit the puzzle
- `Ctrl-G` — toggle keyboard focus between puzzle and Claude (e.g. to
  Esc-interrupt Claude or hit Enter for permission prompts)

When Claude finishes its turn, the puzzle stays open with a
**"Claude is done — finish at your leisure"** banner. Hit Ctrl-G to give
focus back to Claude or finish the puzzle first.

## Going global

Right now the hooks live in this project's `.claude/settings.json`, so the
puzzle only appears when you run `hml claude` from inside this directory.
To make it work in any project, copy the `hooks` block from
`.claude/settings.json` into `~/.claude/settings.json`, swapping
`$CLAUDE_PROJECT_DIR/.claude/hooks/...` for the absolute paths to the
scripts in this repo.

## Troubleshooting

- **Puzzle area gets clobbered by Claude's output.** Some Claude Code
  rendering paths print past the scrolling region. Resize the terminal
  (the wrapper redraws on `SIGWINCH`) or hit Ctrl-G twice to force a
  redraw.
- **Nothing happens.** Confirm `HML_SOCKET` reaches the hook by adding
  `env | grep HML >> /tmp/hml.log` at the top of `on-start.sh`. If it's
  empty, you launched `claude` directly instead of via `hml`.
- **Token errors.** `.env` has to be in this project's root and contain
  `LICHESS_TOKEN=lip_xxx`.

## Standalone puzzle (debugging)

Skip the wrapper entirely:

```bash
.venv/bin/lichess-puzzle
```

Same engine, plain stdin loop.
