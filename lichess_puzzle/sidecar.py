"""Hook-launched sidecar daemon: floats a Lichess puzzle image over the
user's terminal via the Kitty graphics protocol while Claude Code is
working on a task.

Lifecycle:
  - `hml-overlay start` — invoked by the UserPromptSubmit hook. Spawns
    a daemonized child that opens the controlling tty for writes and
    re-places the puzzle image at ~60 Hz. Idempotent: if a daemon for
    the current tty already exists, it just nudges it (`WORKING`).
  - `hml-overlay idle` — invoked by the Stop hook. Banner flips to
    "✓ Claude is done." Daemon stays alive so the user keeps solving.
  - `hml-overlay move <text>` / `hint` / `solve` / `quit` — single
    commands sent over the daemon's Unix datagram socket.

One daemon per controlling terminal (keyed by sha1 of `os.ttyname`),
so two Claude sessions in two panes each get their own overlay.

Why a sidecar (vs. the previous PTY-proxy wrapper):
  the redraw clock is now a 16 ms timer in an independent process —
  no longer derived from Claude's stdout — so the image re-snaps to
  its anchor immediately after every scroll, not just when Claude
  next emits a byte. Claude owns its tty end-to-end; we never
  intercept its I/O.
"""

from __future__ import annotations

import argparse
import errno
import os
import queue
import selectors
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from . import api, board as board_mod, overlay
from .engine import PuzzleSession, MoveResult
from .settings import Settings, load_settings


IMAGE_ID = 1729
TICK_S = 0.016  # ~60 Hz redraw cadence
ESC = "\x1b"


# ── Per-tty paths ─────────────────────────────────────────────────────────


def _session_key() -> str:
    """Per-session key from `os.getsid(0)`. Two Claudes in two
    terminals have different sessions (different shells = different
    session leaders); two consecutive Claudes in the *same* shell
    share the key, which is fine — the daemon is idempotent.

    Must be captured BEFORE the daemon's setsid() — once we're a new
    session leader our SID changes."""
    return f"{os.getsid(0):x}"


def _pid_file(key: str) -> Path:
    cache = Path.home() / ".cache" / "hml"
    cache.mkdir(parents=True, exist_ok=True)
    return cache / f"overlay-{key}.pid"


def _socket_path(key: str) -> Path:
    return Path(f"/tmp/hml-overlay-{key}.sock")


# ── tty / process helpers ─────────────────────────────────────────────────


def _open_controlling_tty() -> int | None:
    """Open /dev/tty — the magic device that resolves to the calling
    process's controlling terminal. Returns None if the process has
    no controlling terminal."""
    try:
        return os.open("/dev/tty", os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        return None


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return None


# ── ANSI helpers ──────────────────────────────────────────────────────────


def _cursor_save() -> bytes:
    return f"{ESC}7".encode()


def _cursor_restore() -> bytes:
    return f"{ESC}8".encode()


def _goto(row: int, col: int) -> bytes:
    return f"{ESC}[{row};{col}H".encode()


# ── Layout (tty-fd-aware variant of wrapper.Layout) ───────────────────────


class Layout:
    def __init__(self, tty_fd: int) -> None:
        self.tty_fd = tty_fd
        self.rows = 24
        self.cols = 80
        self.refresh()

    def refresh(self) -> None:
        try:
            sz = os.get_terminal_size(self.tty_fd)
            self.cols, self.rows = sz.columns, sz.lines
        except OSError:
            pass

    def effective_cells(self, spec: overlay.OverlaySpec) -> tuple[int, int]:
        cw, ch = spec.cell_size()
        return (max(1, min(cw, self.cols)), max(1, min(ch, self.rows)))

    def overlay_anchor(self, spec: overlay.OverlaySpec, position) -> tuple[int, int]:
        cell_w, cell_h = self.effective_cells(spec)
        if isinstance(position, (tuple, list)):
            row, col = int(position[0]), int(position[1])
            row = max(1, min(row, self.rows - cell_h + 1))
            col = max(1, min(col, self.cols - cell_w + 1))
            return (row, col)
        if position == "top-left":
            return (1, 1)
        if position == "bottom-right":
            return (max(1, self.rows - cell_h + 1),
                    max(1, self.cols - cell_w + 1))
        if position == "bottom-left":
            return (max(1, self.rows - cell_h + 1), 1)
        if position == "center":
            return (max(1, (self.rows - cell_h) // 2 + 1),
                    max(1, (self.cols - cell_w) // 2 + 1))
        return (1, max(1, self.cols - cell_w + 1))


# ── Daemon ────────────────────────────────────────────────────────────────


class Daemon:
    def __init__(self, tty_fd: int, key: str):
        self.tty_fd = tty_fd
        self.key = key
        self.pid_file = _pid_file(self.key)
        self.sock_path = _socket_path(self.key)
        self.settings: Settings = load_settings()
        self.layout: Layout
        self.spec: overlay.OverlaySpec

        self.sock: socket.socket | None = None
        self.session: PuzzleSession | None = None
        self.fetch_pending: bool = False
        self.image_transmitted: bool = False
        self.status_msg: str = "fetching puzzle…"
        self.banner: str = ""
        self.claude_idle: bool = False
        self.closing_at: float | None = None
        self._closing_total: float = 3.0
        self.should_exit: bool = False
        self._fetch_q: "queue.Queue[tuple[str, object]]" = queue.Queue()

        # The transcript JSONL is the only place Claude Code records
        # text the user types while we're mid-turn (as `queued_command`
        # attachments and `queue-operation` records). Tailing it gives
        # us real-time mid-turn input that no documented hook reaches.
        self.transcript_path: str = os.environ.get("HML_TRANSCRIPT", "")
        # Inbound commands from the tailer thread → main loop.
        self._tail_q: "queue.Queue[str]" = queue.Queue()
        # (text, monotonic_ts) of the most recently dispatched move,
        # so we can ignore the duplicate when UserPromptSubmit later
        # replays the same prompt at turn-end.
        self._last_dispatch: tuple[str, float] | None = None

    # ── tty plumbing ──────────────────────────────────────────────────

    def _init_tty(self) -> None:
        # tty_fd was opened BEFORE daemonization (when the process
        # still had a controlling terminal). It survives setsid+fork
        # and remains valid for writes for as long as the user keeps
        # the terminal open.
        self.layout = Layout(self.tty_fd)
        self.spec = self._build_spec()

    def _build_spec(self) -> overlay.OverlaySpec:
        requested = overlay.OverlaySpec.from_scale(self.settings.size)
        needed_w, needed_h = requested.cell_size()
        max_w = max(1, self.layout.cols - 2)
        max_h = max(1, self.layout.rows - 2)
        if needed_w <= max_w and needed_h <= max_h:
            return requested
        downscale = min(max_w / needed_w, max_h / needed_h)
        return overlay.OverlaySpec.from_scale(self.settings.size * downscale)

    def _write_tty(self, data: bytes) -> None:
        try:
            os.write(self.tty_fd, data)
        except OSError as e:
            _dbg(f"_write_tty: ERROR {e!r}")
            if e.errno in (errno.ENXIO, errno.EIO, errno.EBADF):
                self.should_exit = True

    # ── socket ────────────────────────────────────────────────────────

    def _bind_socket(self) -> None:
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(str(self.sock_path))
        s.setblocking(False)
        self.sock = s

    def _drain_socket(self) -> None:
        assert self.sock is not None
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
            except (BlockingIOError, OSError):
                return
            for line in data.decode("utf-8", "replace").splitlines():
                self._on_event(line.strip())

    def _on_event(self, msg: str) -> None:
        if not msg:
            return
        if msg == "WORKING":
            self.claude_idle = False
            if self.closing_at is not None or self.session is None and not self.fetch_pending:
                self.closing_at = None
                self.banner = ""
                self.session = None
                self.status_msg = "fetching puzzle…"
                self._start_fetch()
                self._update_overlay()
            return
        if msg == "IDLE":
            self._on_idle()
            return
        if msg == "QUIT":
            self.should_exit = True
            return
        if msg == "HINT":
            if self._dedup_dispatch("hint"):
                self._submit_text("hint")
            return
        if msg == "SOLVE":
            if self._dedup_dispatch("solve"):
                self._submit_text("solve")
            return
        if msg.startswith("MOVE "):
            text = msg[5:]
            if self._dedup_dispatch(text):
                self._submit_text(text)
            return

    # ── puzzle lifecycle ──────────────────────────────────────────────

    # ── dedup ─────────────────────────────────────────────────────────

    _DEDUP_WINDOW_S: float = 8.0

    def _dedup_dispatch(self, text: str) -> bool:
        """Return True if this command should be processed, False if
        it's a near-duplicate of the previous one (within the dedup
        window). Same command typed by the user produces TWO MOVE
        events: once when the tailer sees the queued_command, again
        when UserPromptSubmit fires at turn-end. We process only the
        first arrival."""
        now = time.monotonic()
        last = self._last_dispatch
        if last is not None and last[0] == text and (now - last[1]) < self._DEDUP_WINDOW_S:
            _dbg(f"dedup: skipping repeat of {text!r}")
            return False
        self._last_dispatch = (text, now)
        return True

    # ── puzzle lifecycle ──────────────────────────────────────────────

    def _start_fetch(self) -> None:
        self.fetch_pending = True
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self) -> None:
        try:
            payload = api.fetch_next_puzzle()
            self._fetch_q.put(("ok", payload))
        except Exception as e:
            self._fetch_q.put(("err", str(e)))

    def _drain_fetch_queue(self) -> None:
        while True:
            try:
                tag, value = self._fetch_q.get_nowait()
            except queue.Empty:
                return
            self.fetch_pending = False
            if tag == "ok":
                try:
                    self.session = PuzzleSession(board_mod.parse_puzzle(value))
                    self.status_msg = ""
                except Exception as e:
                    self.status_msg = f"parse error: {e}"
            else:
                self.status_msg = f"fetch failed: {value}"
            self._update_overlay()

    def _on_idle(self) -> None:
        self.claude_idle = True
        if self.closing_at is not None:
            self._update_celebration_banner()
            self._update_overlay()
            return
        self.status_msg = ""
        self.banner = "✓ Claude is done — finish at your leisure."
        self._update_overlay()

    def _submit_text(self, text: str) -> None:
        s = self.session
        if s is None:
            self.status_msg = "puzzle still loading…"
            self._update_overlay()
            return
        r = s.try_move(text.strip())
        self._show_move_result(r)
        if s.finished:
            if s.won:
                self.closing_at = time.monotonic() + self._closing_total
                self.status_msg = ""
                self._update_celebration_banner()
                self._update_overlay()
            else:
                self._finish_puzzle()
        else:
            self._update_overlay()

    def _show_move_result(self, r: MoveResult) -> None:
        if r.kind == "ok":
            parts = [f"✓ {r.user_san}"]
            if r.opponent_san:
                parts.append(f"opp: {r.opponent_san}")
            self.status_msg = "  ·  ".join(parts)
        elif r.kind == "wrong":
            self.status_msg = f"✗ {r.message}"
        elif r.kind in ("unparseable", "command"):
            self.status_msg = r.message or ""

    def _update_celebration_banner(self) -> None:
        if self.closing_at is None:
            return
        remaining = max(0.0, self.closing_at - time.monotonic())
        secs = max(1, int(round(remaining))) if remaining > 0 else 0
        if self.claude_idle:
            self.banner = f"★ Solved!  ·  auto-destruct in {secs}…"
        else:
            self.banner = (
                f"★ You solved it before Claude finished thinking!  ·  "
                f"auto-destruct in {secs}…"
            )

    def _tick_closing(self) -> None:
        if self.closing_at is None:
            return
        remaining = self.closing_at - time.monotonic()
        if remaining <= 0:
            self.closing_at = None
            self._finish_puzzle()
            return
        old = self.banner
        self._update_celebration_banner()
        if self.banner != old:
            self._update_overlay()

    def _finish_puzzle(self) -> None:
        if self.session is not None and self.session.finished:
            try:
                api.submit_result(self.session.p.id, win=self.session.won, rated=False)
            except Exception:
                pass
        self.should_exit = True

    # ── overlay rendering ─────────────────────────────────────────────

    def _update_overlay(self) -> None:
        png = overlay.render_png(self.session, self.status_msg, self.banner, self.spec)
        cells_w, cells_h = self.spec.cell_size()
        anchor_row, anchor_col = self.layout.overlay_anchor(self.spec, self.settings.position)
        out = bytearray()
        out += _cursor_save()
        out += _goto(anchor_row, anchor_col)
        out += overlay.kitty_transmit(png, IMAGE_ID, cells_w=cells_w, cells_h=cells_h)
        out += _cursor_restore()
        self._write_tty(bytes(out))
        self.image_transmitted = True

    def _place_overlay(self) -> None:
        if not self.image_transmitted:
            return
        anchor_row, anchor_col = self.layout.overlay_anchor(self.spec, self.settings.position)
        out = bytearray()
        out += _cursor_save()
        out += _goto(anchor_row, anchor_col)
        out += overlay.kitty_place(IMAGE_ID)
        out += _cursor_restore()
        self._write_tty(bytes(out))

    # ── main loop ─────────────────────────────────────────────────────

    # ── transcript tailer ─────────────────────────────────────────────

    def _start_transcript_tailer(self) -> None:
        """Background thread: tail the Claude Code transcript JSONL
        looking for queued_command / queue-operation records. Pushes
        any matching `p:<text>` (or `p:hint|solve|quit`) to a queue
        which the main loop drains every tick."""
        path = self.transcript_path
        if not path or not os.path.isfile(path):
            _dbg(f"tailer: no transcript at {path!r}, skipping")
            return
        threading.Thread(target=self._tailer_worker, args=(path,), daemon=True).start()
        _dbg(f"tailer: started on {path}")

    def _tailer_worker(self, path: str) -> None:
        import json as _json

        try:
            f = open(path, "r")
        except OSError:
            return
        # Seek to end; we only care about messages typed AFTER the
        # daemon spawned. Pre-existing queued_commands have already
        # been dispatched by UserPromptSubmit at turn-end.
        try:
            f.seek(0, 2)
        except OSError:
            f.close()
            return

        buf = ""
        while not self.should_exit:
            line = f.readline()
            if not line:
                time.sleep(0.1)
                continue
            buf += line
            if not buf.endswith("\n"):
                continue  # wait for full line
            line, buf = buf, ""
            try:
                rec = _json.loads(line)
            except Exception:
                continue
            text = self._extract_queued_text(rec)
            if not text:
                continue
            stripped = text.strip()
            if stripped.startswith("p:"):
                self._tail_q.put(stripped)

        try:
            f.close()
        except OSError:
            pass

    @staticmethod
    def _extract_queued_text(rec: dict) -> str:
        if not isinstance(rec, dict):
            return ""
        # Format A: queue-operation enqueue
        if rec.get("type") == "queue-operation" and rec.get("operation") == "enqueue":
            c = rec.get("content")
            if isinstance(c, str):
                return c
        # Format B: attachment / queued_command
        if rec.get("type") == "attachment":
            att = rec.get("attachment")
            if isinstance(att, dict) and att.get("type") == "queued_command":
                p = att.get("prompt")
                if isinstance(p, str):
                    return p
        return ""

    def _drain_tail_queue(self) -> None:
        while True:
            try:
                text = self._tail_q.get_nowait()
            except queue.Empty:
                return
            body = text[2:].strip()  # strip leading "p:"
            low = body.lower()
            if low in ("hint", "solve", "quit"):
                self._on_event(low.upper())
            elif body:
                self._on_event(f"MOVE {body}")

    # ── main loop ─────────────────────────────────────────────────────

    def run(self) -> None:
        _dbg(f"Daemon.run start key={self.key}")
        self._init_tty()
        self._bind_socket()
        self.pid_file.write_text(str(os.getpid()))

        def _set_exit(*_):
            self.should_exit = True
        # SIGTERM is the *deliberate* kill signal — only `hml-overlay
        # stop`, our smoke test, and explicit `pkill` send it.
        signal.signal(signal.SIGTERM, _set_exit)
        # SIGINT and SIGHUP can leak to us from Claude Code's process
        # group (we don't setsid so we can keep `/dev/tty` writable).
        # Each Esc-interrupt the user presses would otherwise kill the
        # daemon. Ignore them; we still detect a closed terminal via
        # EIO on the next tty write.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

        def _on_winch(*_):
            self.layout.refresh()
            self.spec = self._build_spec()
            if self.session is not None or self.fetch_pending:
                self._update_overlay()
        signal.signal(signal.SIGWINCH, _on_winch)

        try:
            self._start_fetch()
            self._start_transcript_tailer()
            self._update_overlay()  # initial "fetching…" frame

            sel = selectors.DefaultSelector()
            sel.register(self.sock.fileno(), selectors.EVENT_READ, "sock")

            while not self.should_exit:
                try:
                    events = sel.select(timeout=TICK_S)
                except InterruptedError:
                    continue
                for _ in events:
                    self._drain_socket()
                self._drain_fetch_queue()
                self._drain_tail_queue()
                self._tick_closing()
                self._place_overlay()
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        if self.image_transmitted:
            try:
                os.write(self.tty_fd, overlay.kitty_delete(IMAGE_ID))
            except OSError:
                pass
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass
        for path in (self.sock_path, self.pid_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


# ── Subcommand entry points ───────────────────────────────────────────────


def _send(sock_path: Path, msg: str) -> bool:
    if not sock_path.exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendto((msg + "\n").encode(), str(sock_path))
        s.close()
        return True
    except OSError:
        return False


def _existing_daemon_pid(pid_file: Path) -> int | None:
    pid = _read_pid(pid_file)
    if pid is None:
        return None
    if not _process_alive(pid):
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
        return None
    return pid


def cmd_start() -> int:
    if not overlay.is_supported():
        return 0
    tty_fd = _open_controlling_tty()
    if tty_fd is None:
        return 0
    key = _session_key()
    pid_file = _pid_file(key)
    sock_path = _socket_path(key)
    _dbg(f"cmd_start: key={key} sid={os.getsid(0)}")

    if _existing_daemon_pid(pid_file) is not None:
        os.close(tty_fd)
        _send(sock_path, "WORKING")
        return 0

    try:
        sock_path.unlink()
    except FileNotFoundError:
        pass

    # Single fork — DON'T setsid. We need to keep our controlling
    # terminal so writes to /dev/tty (which is the magic CTTY device)
    # don't fail with EIO. The daemon stays in the caller's session;
    # when the user closes the terminal, writes will fail with
    # ENXIO/EIO and the daemon exits gracefully.
    if os.fork() != 0:
        os.close(tty_fd)
        return 0

    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    if devnull > 2:
        os.close(devnull)

    try:
        Daemon(tty_fd, key).run()
    except Exception:
        pass
    os._exit(0)


def cmd_send(msg: str) -> int:
    key = _session_key()
    sock_path = _socket_path(key)
    _send(sock_path, msg)
    return 0


def _dbg(msg: str) -> None:
    if os.environ.get("HML_DEBUG"):
        try:
            with open("/tmp/hml-sidecar-debug.log", "a") as f:
                f.write(f"[{time.time():.3f}] pid={os.getpid()} {msg}\n")
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    _dbg(f"main argv={argv}")
    parser = argparse.ArgumentParser(prog="hml-overlay", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start")
    sub.add_parser("idle")
    sub.add_parser("stop")
    sub.add_parser("hint")
    sub.add_parser("solve")
    sub.add_parser("quit")
    p_move = sub.add_parser("move")
    p_move.add_argument("text", nargs="+")
    args = parser.parse_args(argv)

    if args.cmd == "start":
        return cmd_start()
    if args.cmd == "idle":
        return cmd_send("IDLE")
    if args.cmd in ("stop", "quit"):
        return cmd_send("QUIT")
    if args.cmd == "hint":
        return cmd_send("HINT")
    if args.cmd == "solve":
        return cmd_send("SOLVE")
    if args.cmd == "move":
        return cmd_send("MOVE " + " ".join(args.text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
