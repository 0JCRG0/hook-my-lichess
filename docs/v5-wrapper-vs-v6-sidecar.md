# v5 wrapper vs v6 sidecar

This doc captures the architectural shift the project made in one
afternoon: from a PTY-proxy wrapper that intercepted every byte
between the user and Claude (`hml claude`), to a hook-launched
sidecar daemon that draws over Claude's terminal without touching
its I/O (`claude` directly, with hooks doing the rest).

The two approaches solve the same problem — float a Lichess puzzle
image over Claude Code's TUI while the model is working — but they
make almost-opposite choices about *where* the integration lives,
*what* state needs synchronizing, and *which* failure modes the
implementation has to defend against.

---

## TL;DR

| Concern                              | v5 wrapper                                       | v6 sidecar                                        |
|--------------------------------------|--------------------------------------------------|---------------------------------------------------|
| Process model                        | Wraps Claude (`hml claude`) inside `pty.fork()`  | Sibling daemon spawned by hook (`claude` direct)  |
| Owns the user↔Claude byte stream     | Yes — every keystroke and every byte from Claude | No — Claude owns its tty end-to-end               |
| Redraw clock                         | Reactive: ticks on Claude bytes, throttled 50ms  | Independent 16 ms timer in the daemon             |
| User input channel                   | Snoop-and-`%` intercept (e.g. `Nf3%`)            | `p:<move>` slash-prefix + transcript tailer       |
| Mid-turn input dispatch              | Real-time (intercepts every keystroke)           | Real-time via JSONL transcript tailing            |
| Code in `lichess_puzzle/`            | `wrapper.py` 575 lines                           | `sidecar.py` ~600 lines                           |
| Smoke-test invariants                | No DECSTBM, PTY size unchanged, snoop+%, etc.    | Image transmit/place/delete, no DECSTBM           |
| Works without `hml` prefix           | No — must launch as `hml claude`                 | Yes — install hooks once, works with `claude`     |
| Multi-Claude in two terminals        | Each `hml` invocation has its own state          | Per-shell-session daemon via `getsid(0)` keying   |
| Per-byte cost on Claude's stdout     | Two `os.read` + raw-mode forwarding              | Zero — daemon never sees Claude's output          |
| Cleanup on terminal close            | wrapper exits → claude exits                     | EIO on next 16ms tick → daemon exits              |
| Things that go wrong silently        | Many (PTY shrinks, DECSTBM, raw-mode resurrect)  | Few (no PTY, no terminal-state mutation)          |

---

## Why the migration happened

The user reported one specific bug: when Claude printed enough
output to scroll the terminal, the puzzle image **tore in half** for
a moment before snapping back to its anchor.

The root cause was the v5 wrapper's redraw clock. Re-placement of the
Kitty image happened only on two triggers:

1. Bytes arriving on the PTY master from Claude (`_handle_master`),
   throttled to one re-place per 50 ms.
2. The main loop's `select` timeout (originally 500 ms, lowered to
   50 ms during the v5→v6 transition).

So whenever Claude produced a burst of output, the terminal scrolled
the cell-anchored Kitty image with the text, then *paused* — and
the image stayed at its scrolled-down position until either the next
byte arrived or the next loop tick. That gap was the visible tear.

We tried three fixes in increasing order of invasiveness:

1. **Throttle drop** (shipped first). 50 ms → 16 ms (~60 Hz) plus
   an unconditional re-place at the end of every event loop tick.
   Smoke test passed. Narrowed the window but didn't eliminate the
   "scroll-then-pause" stale frame.
2. **Sidecar daemon** (this doc). The redraw clock is a fixed 16 ms
   timer in an independent process — fully decoupled from Claude's
   stdout. After every scroll the image snaps back within one tick,
   regardless of whether Claude is currently emitting bytes.
3. **Unicode placeholders** — abandoned. Kitty's docs were explicit
   that placeholder cells scroll with text just like normal Unicode
   characters, so they don't fix the floating-overlay problem.

Option 2 won because it addresses the root cause — the redraw is
no longer reactive to Claude's I/O — and as a side effect deletes
hundreds of lines of PTY-proxy code that existed only to support
the snoop-and-`%` input model.

---

## Process model

### v5: wrap Claude

```
                              user terminal
                                    │
                                    ▼
                              ┌──────────┐
                              │   hml    │  ← raw-mode stdin
                              │ wrapper  │
                              └────┬─────┘
                                   │ pty.fork() + execvp claude
                                   ▼
                              ┌──────────┐
                              │  claude  │
                              └──────────┘
```

The user runs `hml claude`. The wrapper:

1. Calls `pty.fork()` and execs claude in the child.
2. Puts the parent's stdin in raw mode.
3. Forwards bytes both ways through `selectors`, snooping the user→
   Claude direction for puzzle-move patterns.
4. Owns a Unix datagram socket (`$HML_SOCKET`) so the
   `UserPromptSubmit` and `Stop` hooks can deliver `WORKING` and
   `IDLE` events.
5. Renders the Kitty image after each WORKING event and re-places it
   on bytes from Claude.

Everything goes through the wrapper. If you launch Claude without
`hml`, the overlay never appears.

### v6: sidecar alongside Claude

```
                              user terminal
                                  ▲
                                  │ /dev/tty writes (Kitty escapes)
                                  │
        UserPromptSubmit hook  ┌──┴─────────┐    enqueue/queued_command
       (on-prompt.sh,          │            │  ←─ records appearing in
        on-start.sh)           │  sidecar   │     ~/.claude/projects/
       ────► spawn / nudge ───►│  daemon    │     <project>/<sid>.jsonl
                               │            │
       Stop hook ────►         │            │
       (on-stop.sh, IDLE)      └────────────┘
                                  ▲
                                  │ Unix datagram socket
                                  │ (move/hint/solve/quit)
                              hml-overlay <subcmd>
```

The user runs `claude` directly. There's no PTY proxy. The sidecar:

1. Is spawned by `UserPromptSubmit` hook calling `hml-overlay start`.
   `start` opens `/dev/tty`, captures the session key (`getsid(0)`),
   forks once into a daemonized child, and exits.
2. The child binds a Unix datagram socket at
   `/tmp/hml-overlay-<key>.sock`, writes a PID file at
   `~/.cache/hml/overlay-<key>.pid`, and enters a `selectors` loop
   with a 16 ms tick.
3. On each tick: re-emit `kitty_place(IMAGE_ID, p=1)` to the saved
   tty fd. Same `(image_id, placement_id)` so Kitty replaces in-place.
4. On socket message: drive `PuzzleSession` via `_on_event`, regenerate
   PNG on state change, retransmit.
5. On SIGHUP / SIGTERM / EIO from the tty: clean up and exit.

Subsequent invocations of `hml-overlay <subcmd>` (from the
`UserPromptSubmit` hook for `p:Nf3` interception, or from the Stop
hook for IDLE) compute the same key, look up the socket, and send
a single line over UDP. They don't fork.

---

## Input model

### v5: snoop-and-`%`

The wrapper reads every byte the user types. While a puzzle is
active, it appends printable bytes into a `snoop_buffer`. When the
byte is `%` and the buffer matches a chess move regex
(`[a-h1-8RNBQKO=+#xX\-O0]{2,}`, length ≥ 3) or a command keyword
(`hint`, `solve`, `quit`, ...), the wrapper:

1. *Drops* the `%` (does not forward it to Claude).
2. Sends N backspaces to Claude so the move characters get wiped
   from Claude's input box.
3. Feeds the buffer to `PuzzleSession.try_move`.
4. Updates the overlay.

Otherwise, every byte (including a literal `%` in normal text like
`50%`) passes through to Claude untouched.

This was elegant from a UX perspective — `Nf3%` followed by Enter
doesn't leak anything to Claude — but it required a PTY proxy to
exist. Removing the proxy meant losing this UX.

### v6: `p:` slash-prefix + transcript tailer

Two parallel surfaces, both intercepted by `on-prompt.sh`:

#### At turn boundaries: UserPromptSubmit hook

`on-prompt.sh` reads the JSON event from stdin, extracts the prompt,
and matches:

- `^p:(hint|solve|quit)\s*$` → `hml-overlay <cmd>`
- `^p:(.+)$` → `hml-overlay move <text>`

On a match it returns `{"decision":"block","reason":"..."}` so the
prompt never reaches Claude. Anything else passes through.

This works perfectly when the user is at the prompt — but
`UserPromptSubmit` only fires at *turn boundaries*, not when the
user types something while the model is mid-response.

#### Mid-turn: transcript tailer

Empirical finding from reading the JSONL files in
`~/.claude/projects/<project>/<session>.jsonl`: Claude Code writes
queued user messages to the transcript **the moment they're typed**,
in two formats:

```jsonl
{"type":"queue-operation","operation":"enqueue","content":"p:Nf3", ...}
{"type":"attachment","attachment":{"type":"queued_command","prompt":"p:Nf3", ...}, ...}
```

The `on-start.sh` hook extracts `transcript_path` from its event
JSON and passes it to the daemon as `$HML_TRANSCRIPT`. The daemon
spawns a tailer thread that:

1. `open(path)` and `seek(0, 2)` — start at end-of-file.
2. Polls `readline()` every 100 ms.
3. Parses each new line as JSON.
4. Extracts `content` from `queue-operation enqueue` records and
   `attachment.prompt` from `queued_command` attachments.
5. If the text starts with `p:`, pushes it to an internal queue.
6. The main loop drains that queue alongside socket events.

Claude Code does not expose any hook that fires on queued input
(verified empirically: `/btw` doesn't fire `SubagentStart`; no other
hook receives user-typed text mid-turn). The transcript tailer is
the only way to get real-time mid-turn input.

#### Dedup

When the user types `p:Nf3` mid-turn, two dispatches can fire:

1. The tailer sees the `queued_command` record immediately and
   dispatches `MOVE Nf3`.
2. When the queue eventually drains and `UserPromptSubmit` fires for
   the same prompt, `on-prompt.sh` runs `hml-overlay move Nf3`,
   which sends `MOVE Nf3` over the socket.

The daemon's `_dedup_dispatch(text)` keeps a (text, monotonic_ts)
tuple of the most recent command. Repeats within an 8-second window
are silently dropped. The first arrival wins; the second is a
no-op. Same move typed deliberately twice within 8s also gets
deduped — minor UX cost, acceptable.

---

## Redraw cadence — the original bug, fixed

| Behaviour                                | v5 wrapper                          | v6 sidecar                              |
|------------------------------------------|-------------------------------------|-----------------------------------------|
| Re-place trigger                         | Bytes from Claude OR loop tick      | Independent timer, every 16 ms          |
| Min interval between re-places           | 50 ms (shipped 16 ms in last patch) | 16 ms                                   |
| Re-place fires when Claude is silent     | Only on loop tick (≤ 50 ms-1 wait)  | Yes, every 16 ms unconditionally        |
| Tear window after scroll                 | Up to ~50 ms                        | Up to ~16 ms                            |
| Coupling to Claude's output rate         | Tight                               | None                                    |

Both versions emit the same Kitty escape (`a=p,i=1729,p=1,C=1,q=2`).
The difference is purely *when*. The sidecar's timer-driven loop
catches the post-scroll-pause case the wrapper couldn't.

The other Kitty fix that came along during the migration: pinning
`p=1` on `kitty_transmit` (`a=T`) too. Without it, every re-transmit
created a *new* placement with an auto-assigned id, and they
accumulated visibly — the user reported seeing 2-3 ghost boards
stacked at slightly different scales after a few SIGWINCH or status
updates. The wrapper had the same bug latently but masked it by
issuing `kitty_delete_placement` before each transmit. The sidecar
now uses the documented "same-id replacement" property end-to-end.

---

## Failure modes

### v5 wrapper

| Failure                                    | Behaviour                                     |
|--------------------------------------------|-----------------------------------------------|
| Terminal can't do Kitty graphics           | Wrapper proxies Claude transparently, no img  |
| User runs `claude` instead of `hml claude` | No overlay at all                             |
| `LICHESS_TOKEN` missing                    | "fetch failed" status, image still renders    |
| User types `wait 30%` in normal prompt     | `%` passes through (regex doesn't match `30`) |
| User types `nf3%`                          | Pass through (SAN piece letters are uppercase)|
| Claude emits SIGWINCH                      | Wrapper handles, re-syncs PTY size            |
| Wrapper crashes                            | Claude dies with it                           |
| DECSTBM regression                         | Smoke test catches, wrapper rejected          |

### v6 sidecar

| Failure                                    | Behaviour                                       |
|--------------------------------------------|-------------------------------------------------|
| Terminal can't do Kitty graphics           | `is_supported()` False, daemon exits silently   |
| User runs `claude` without hooks installed | No overlay; Claude works normally               |
| `LICHESS_TOKEN` missing                    | "fetch failed" status, image still renders      |
| User types `p:` in normal prompt           | Hook intercepts; if no daemon, silent no-op     |
| Stale PID file from crashed daemon         | `_existing_daemon_pid` checks liveness, unlinks |
| Two Claudes in same shell                  | Share daemon (idempotent `WORKING` nudge)       |
| Two Claudes in different shells            | Different `getsid(0)`, different daemons        |
| Terminal closes                            | EIO on next 16 ms write → daemon exits cleanly  |
| Daemon can't open `/dev/tty`               | Exits silently — same path as no-graphics       |
| Mid-turn `p:` interception                 | Tailer dispatches, dedup skips re-fire          |
| Daemon dies during turn                    | UserPromptSubmit replays at turn-end            |

The sidecar has *fewer* invariants to maintain because it never
mutates terminal state. The wrapper's defining smoke-test assertions
(no DECSTBM, PTY size unchanged) become trivially true in v6 because
the daemon doesn't touch the PTY at all.

---

## What got deleted

The migration removed:

- `lichess_puzzle/wrapper.py` — 575 lines: PTY fork, raw mode, byte
  forwarding, snoop buffer, `%` intercept, SIGWINCH propagation,
  the `Layout` class (re-added to `sidecar.py` with a tty-fd backend),
  the `_loop` byte-driven re-place dance.
- `scripts/smoke_wrapper.py` — 177 lines: PTY pair, bash subprocess,
  byte capture, DECSTBM/PTY-size assertions, snoop-and-% verification.
- `hml = "lichess_puzzle.wrapper:main"` script entry point.
- All references to `hml claude` in CLAUDE.md.

What got added:

- `lichess_puzzle/sidecar.py` — ~600 lines (more than wrapper.py was,
  because of the transcript tailer + extra subcommands; the *core*
  daemon loop is much shorter than the wrapper's `_loop`).
- `.claude/hooks/on-prompt.sh` — slash-prefix interception.
- `scripts/smoke_sidecar.py` — pty-pair test, asserts daemon spawn,
  Kitty `a=T`/`a=d` escapes, MOVE/IDLE retransmit, no DECSTBM.
- `hml-overlay = "lichess_puzzle.sidecar:main"` entry point with
  subcommands `start | idle | stop | move <txt> | hint | solve | quit`.

---

## Lessons that fed back into the rewrite

A few things only became obvious after the v5 → v6 cut:

1. **`/dev/tty` is a magic device on macOS.** Opening it from a
   process whose CTTY is `/dev/ttysNNN` and calling `os.ttyname(fd)`
   returns the literal string `/dev/tty`, not the underlying path.
   `fcntl(F_GETPATH, ...)` also returns `/dev/tty`. `os.fstat` returns
   the magic device's `st_rdev` (`0x2000000`), not the slave pty's.
   The keying scheme had to fall back to `os.getsid(0)` because every
   path-based identifier collapsed.
2. **`setsid()` revokes CTTY.** A daemonized child that calls setsid
   loses its controlling terminal, and writes to `/dev/tty` then
   return EIO. The sidecar deliberately *doesn't* setsid — it stays
   in the user's shell session and exits naturally on SIGHUP.
3. **No documented hook gets user input mid-turn.** `UserPromptSubmit`
   fires only at turn boundaries. `SubagentStart` fires for `Agent`
   tool invocations (verified empirically) but not for `/btw` (also
   verified — log was empty). The transcript JSONL was the only
   surface that carried the typed text mid-turn.
4. **Same-id Kitty replacement requires `p=` on transmit too.**
   The protocol guarantees that a second `a=p,i=I,p=P` replaces the
   first. But `a=T` (transmit + display) without an explicit `p=`
   creates a *new* placement with an auto-assigned id. Pinning
   `p=1` on transmit is what stopped the ghost-board pile-up.

---

## When v5's design would still be the right call

There are situations where the wrapper model still wins. If you
needed:

- Real-time keystroke interception that doesn't require typing a
  prefix or re-submitting (the snoop-and-`%` UX),
- Output filtering or rewriting between Claude and the user (e.g.,
  redacting tokens, injecting a top bar of status text, or scrolling
  region tricks),
- Operation outside Claude Code's hook system entirely (e.g., wrapping
  any TUI program, not just Claude),

…then a PTY proxy is the right shape. The sidecar can't reach those
because it deliberately isn't in the I/O path.

For the project's actual use case — float a puzzle while Claude
works, and accept moves with a slight prefix typing cost — the
sidecar is a strict improvement: no PTY-proxy maintenance burden,
no required `hml` launch wrapper, smoother redraws, better resilience
to terminal events, fewer invariants the smoke test has to defend.

---

## Pointers

- Daemon source: `lichess_puzzle/sidecar.py`
- Hooks: `.claude/hooks/{on-prompt,on-start,on-stop}.sh`
- Settings wiring: `.claude/settings.json`
- Smoke test: `scripts/smoke_sidecar.py`
- High-level architecture: `CLAUDE.md` (§ "How the sidecar overlays")
