# hook-my-lichess

A Lichess puzzle that floats over the **same terminal** Claude Code is
running in. While Claude works, the board hovers in the top-right; you
play by typing `p:<move>` as a prompt — **including while Claude is
mid-response**. No PTY proxy, no wrapper binary.

## Install

Prereqs (one-time): [uv](https://docs.astral.sh/uv/) and a Lichess
personal token ([create one here](https://lichess.org/account/oauth/token)):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export LICHESS_TOKEN=lip_xxxxxxxxxxxxxxxx   # add to your shell rc
```

Then, inside Claude Code:

```
/plugin marketplace add 0JCRG0/hook-my-lichess
/plugin install hook-my-lichess@jcrg-tooling
```

Done. Your next prompt fires the hooks; `uvx` fetches the daemon from
PyPI on first run (~2 s) and caches it.

## Playing

Submit any prompt — the board appears within ~1 s. Then type puzzle
commands **as prompts**:

| Command | Effect |
|---|---|
| `p:e2e4` | UCI move |
| `p:Nf3` | SAN move (piece letters uppercase) |
| `p:hint` | which square the piece moves from |
| `p:solve` | give up, reveal the move (shown 10 s) |
| `p:quit` | close the puzzle |
| `p:size xl` | resize the overlay (live + persisted) |
| `p:pos center` | reposition the overlay (live + persisted) |

Anything not starting with `p:` goes to Claude untouched. Mid-turn
commands hit the board within ~100 ms — no waiting for Claude to
finish. When Claude's turn ends, the banner flips to "✓ Claude is
done" and you keep solving at your own pace.

## Caveats

- **Kitty graphics protocol required.** Works in Ghostty, Kitty, and
  WezTerm. On anything else (iTerm2, Terminal.app, tmux, VS Code's
  terminal, plain SSH), the daemon exits silently and Claude works as
  normal — you just don't get a board.
- **Mid-turn commands are visible to the model.** Claude Code has no
  hook for messages typed while a turn is running, so a mid-turn
  `p:e2e4` reaches Claude's context as plain text (the board still
  updates — the daemon reads queued input from the session
  transcript). The plugin injects a standing instruction telling the
  model to silently ignore `p:` messages, which works well in
  practice but costs a few context tokens per prompt. Commands
  submitted while Claude is idle are properly blocked and never reach
  the model.
- **macOS / Linux only.** The daemon writes Kitty escapes straight to
  the pty device; there is no Windows support.

## Customizing size & position

The quick way — just type it as a prompt, live overlay updates
instantly and the change persists:

```
p:size xl        # small | medium | large | xl | xxl | any number (p:size 1.7)
p:pos center     # top-right | top-left | bottom-right | bottom-left | center | row col (p:pos 3 80)
```

Or edit the settings file by hand:

```bash
uvx --from hook-my-lichess hml-overlay init-config
```

Then edit `~/.config/hml/settings.json`:

```json
{ "size": "xxl", "position": "center" }
```

- `size` — `"small"` | `"medium"` (default) | `"large"` | `"xl"` | `"xxl"`, or any positive number (e.g. `1.7`).
- `position` — `"top-right"` (default) | `"top-left"` | `"bottom-right"` | `"bottom-left"` | `"center"`, or a 1-indexed `[row, col]` pair.

Config is read from `$HML_CONFIG`, then `<cwd>/hml.json`, then
`~/.config/hml/settings.json` — first hit wins.

## How it works

Claude Code owns its terminal end-to-end; the overlay is a sibling
daemon, spawned by a `UserPromptSubmit` hook:

- The hook resolves the terminal's pty device by walking the process
  tree (hook processes have no controlling terminal) and the daemon
  writes Kitty graphics escapes straight to it, re-placing the image
  on a 16 ms clock so it snaps back over scrolls immediately.
- Idle `p:` prompts are intercepted with `decision: "block"`. Mid-turn
  `p:` messages are picked up by tailing the session's JSONL
  transcript, with an 8-second dedup window against duplicates.
- One daemon per terminal, keyed by pty name — two Claude sessions in
  two panes get independent boards.

Full architecture notes and the bugs that drove the sidecar rewrite:
`docs/v5-wrapper-vs-v6-sidecar.md`.

## Developing locally

```bash
git clone https://github.com/0JCRG0/hook-my-lichess
cd hook-my-lichess
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # put LICHESS_TOKEN here
```

The hook scripts prefer the sibling `.venv/bin/hml-overlay` over
`uvx`, so working inside this repo always runs your in-tree code. If
the marketplace plugin is also installed, disable it here
(`/plugin disable hook-my-lichess`) to avoid double-firing.

Debugging: `HML_FORCE_OVERLAY=1` bypasses terminal detection;
`HML_DEBUG=1` logs to `/tmp/hml-sidecar-debug.log`. A standalone
stdin-loop version of the puzzle (no Claude, no overlay) runs with
`.venv/bin/lichess-puzzle`.
