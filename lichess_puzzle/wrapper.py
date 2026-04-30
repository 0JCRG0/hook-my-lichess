"""PTY-proxy wrapper that runs Claude Code as a child and floats a
Lichess-puzzle Kitty-graphics image over the same terminal while Claude
is working.

Usage: hml claude [<claude-args>...]

v5 design (Kitty graphics overlay, no scroll-region or PTY shrink):
  - Fork a PTY, exec the child argv inside it. Forward bytes both ways.
  - When WORKING:
      * fetch the puzzle in a background thread.
      * render the puzzle as a PNG and transmit/place it at the top-right
        of the user's terminal via the Kitty graphics protocol. Claude is
        NOT resized — the image is on a separate layer above the text grid.
      * snoop the user's keystrokes. When the snooped buffer ends with `%`
        and looks like a chess move/command, intercept: parse it, send N
        backspaces back to Claude so the chars get wiped from Claude's
        input box, and update the puzzle image with the result.
  - When IDLE: regenerate image with a "Claude is done" status.
  - On finish/quit: delete the image; everything else returns to normal.

If the terminal doesn't support Kitty graphics (iTerm2, Terminal.app,
tmux, ...) the wrapper still proxies Claude transparently but skips the
overlay entirely.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import pty
import queue
import re
import selectors
import signal
import socket
import struct
import sys
import termios
import threading
import time
import tty
from pathlib import Path

from . import api, board as board_mod, overlay, render
from .engine import PuzzleSession, MoveResult
from .settings import Settings, load_settings


CTRL_C = 0x03
ESC_BYTE = 0x1b
BACKSPACE = 0x7f
TERMINATOR = ord("%")
IMAGE_ID = 1729

MOVE_RE = re.compile(r"[a-h1-8RNBQKO=+#xX\-O0]{2,}")
COMMAND_TOKENS = {"h", "hint", "?", "s", "solve", "q", "quit"}


# ── ANSI helpers ───────────────────────────────────────────────────────────


ESC = "\x1b"


def cursor_save() -> bytes:
    return f"{ESC}7".encode()


def cursor_restore() -> bytes:
    return f"{ESC}8".encode()


def goto(row: int, col: int) -> bytes:
    return f"{ESC}[{row};{col}H".encode()


# ── Layout ─────────────────────────────────────────────────────────────────


class Layout:
    def __init__(self) -> None:
        self.rows = 24
        self.cols = 80
        self.refresh()

    def refresh(self) -> None:
        try:
            packed = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            self.rows, self.cols, _, _ = struct.unpack("HHHH", packed)
        except OSError:
            pass

    def effective_cells(self, spec: overlay.OverlaySpec) -> tuple[int, int]:
        """Cells the image will actually occupy after clamping to the terminal."""
        cw, ch = spec.cell_size()
        return (max(1, min(cw, self.cols)), max(1, min(ch, self.rows)))

    def overlay_anchor(
        self,
        spec: overlay.OverlaySpec,
        position,
    ) -> tuple[int, int]:
        """Return (row, col) where the image's top-left should sit, given the
        user's position preference (preset name or [row, col] tuple).

        Uses the *clamped* cell size so the image is never anchored such
        that it would overflow the terminal."""
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
            row = max(1, (self.rows - cell_h) // 2 + 1)
            col = max(1, (self.cols - cell_w) // 2 + 1)
            return (row, col)
        # default: top-right
        return (1, max(1, self.cols - cell_w + 1))


# ── Helpers ────────────────────────────────────────────────────────────────


def write_raw(fd: int, data: bytes) -> None:
    while data:
        try:
            n = os.write(fd, data)
        except BlockingIOError:
            continue
        if n == 0:
            return
        data = data[n:]


# ── Wrapper ────────────────────────────────────────────────────────────────


class Wrapper:
    def __init__(self, child_argv: list[str], socket_path: Path):
        self.child_argv = child_argv
        self.socket_path = socket_path
        self.master_fd: int = -1
        self.child_pid: int = 0
        self.orig_attrs: list | None = None
        self.layout = Layout()

        self.settings: Settings = load_settings()
        self.spec: overlay.OverlaySpec = self._build_spec()

        self.overlay_supported = overlay.is_supported()
        self.image_transmitted: bool = False
        self.last_overlay_t: float = 0.0
        self.session: PuzzleSession | None = None
        self.puzzle_active: bool = False
        self.fetch_pending: bool = False
        self.snoop_buffer: str = ""
        self.status_msg: str = ""
        self.banner: str = ""
        self.claude_idle: bool = False
        self.closing_at: float | None = None  # monotonic deadline; auto-finish on win
        self._closing_total: float = 3.0

        self._fetch_q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.server_sock: socket.socket | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────

    def run(self) -> int:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            os.execvp(self.child_argv[0], self.child_argv)

        self._bind_socket()
        os.environ["HML_SOCKET"] = str(self.socket_path)

        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.execvp(self.child_argv[0], self.child_argv)
            except FileNotFoundError:
                sys.stderr.write(f"hml: command not found: {self.child_argv[0]}\n")
                os._exit(127)

        self.child_pid = pid
        self.master_fd = fd

        self._sync_winsize_to_child()
        signal.signal(signal.SIGWINCH, lambda *_: self._on_resize())
        signal.signal(signal.SIGCHLD, lambda *_: None)

        self.orig_attrs = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())

        try:
            self._loop()
        finally:
            if self.overlay_supported and self.image_transmitted:
                write_raw(sys.stdout.fileno(), overlay.kitty_delete(IMAGE_ID))
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.orig_attrs)
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        try:
            _, status = os.waitpid(self.child_pid, 0)
            return os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            return 0

    # ── Terminal/PTY plumbing ──────────────────────────────────────────

    def _sync_winsize_to_child(self) -> None:
        # Always pass the full terminal size — claude renders normally.
        try:
            packed = struct.pack("HHHH", self.layout.rows, self.layout.cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, packed)
            os.kill(self.child_pid, signal.SIGWINCH)
        except OSError:
            pass

    def _on_resize(self) -> None:
        self.layout.refresh()
        self.spec = self._build_spec()
        self._sync_winsize_to_child()
        if self.puzzle_active and self.overlay_supported:
            # Re-transmit so the image is rebuilt at the new effective scale.
            self._update_overlay()

    def _build_spec(self) -> overlay.OverlaySpec:
        """Build the overlay spec, downscaling proportionally if the terminal
        can't fit the requested size. This keeps the chess pieces' aspect
        ratio correct — every dimension scales together."""
        requested = overlay.OverlaySpec.from_scale(self.settings.size)
        needed_w, needed_h = requested.cell_size()
        # Reserve a couple of cells of breathing room on each axis.
        max_w = max(1, self.layout.cols - 2)
        max_h = max(1, self.layout.rows - 2)
        if needed_w <= max_w and needed_h <= max_h:
            return requested
        downscale = min(max_w / needed_w, max_h / needed_h)
        return overlay.OverlaySpec.from_scale(self.settings.size * downscale)

    # ── Socket ─────────────────────────────────────────────────────────

    def _bind_socket(self) -> None:
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(str(self.socket_path))
        s.setblocking(False)
        self.server_sock = s

    def _drain_socket(self) -> None:
        assert self.server_sock is not None
        while True:
            try:
                data, _ = self.server_sock.recvfrom(1024)
            except (BlockingIOError, OSError) as e:
                if isinstance(e, OSError) and e.errno != errno.EAGAIN:
                    return
                return
            for line in data.decode("utf-8", "replace").splitlines():
                self._on_event(line.strip())

    def _on_event(self, msg: str) -> None:
        if msg == "WORKING":
            self._start_puzzle()
        elif msg == "IDLE":
            self._on_idle()

    # ── Main event loop ────────────────────────────────────────────────

    def _loop(self) -> None:
        sel = selectors.DefaultSelector()
        sel.register(sys.stdin.fileno(), selectors.EVENT_READ, "stdin")
        sel.register(self.master_fd, selectors.EVENT_READ, "master")
        sel.register(self.server_sock.fileno(), selectors.EVENT_READ, "sock")

        while True:
            try:
                events = sel.select(timeout=0.5)
            except InterruptedError:
                continue

            for key, _ in events:
                tag = key.data
                if tag == "stdin":
                    if not self._handle_stdin():
                        return
                elif tag == "master":
                    if not self._handle_master():
                        return
                elif tag == "sock":
                    self._drain_socket()

            self._drain_fetch_queue()
            self._tick_closing()

    # ── Input from user terminal ───────────────────────────────────────

    def _handle_stdin(self) -> bool:
        try:
            data = os.read(sys.stdin.fileno(), 4096)
        except OSError:
            return False
        if not data:
            return False

        if not self.puzzle_active:
            write_raw(self.master_fd, data)
            return True

        forward = bytearray()
        for b in data:
            if b == TERMINATOR and self._buffer_is_puzzle_command():
                if forward:
                    write_raw(self.master_fd, bytes(forward))
                    forward.clear()
                n = len(self.snoop_buffer)
                write_raw(self.master_fd, bytes([BACKSPACE]) * n)
                text = self.snoop_buffer
                self.snoop_buffer = ""
                self._submit_puzzle(text)
                continue

            if b in (0x0d, 0x0a):
                self.snoop_buffer = ""
            elif b == BACKSPACE or b == 0x08:
                self.snoop_buffer = self.snoop_buffer[:-1]
            elif b == ESC_BYTE:
                self.snoop_buffer = ""
            elif b == CTRL_C:
                self.snoop_buffer = ""
            elif 0x20 <= b < 0x7f:
                self.snoop_buffer += chr(b)

            forward.append(b)

        if forward:
            write_raw(self.master_fd, bytes(forward))
        return True

    def _buffer_is_puzzle_command(self) -> bool:
        text = self.snoop_buffer.strip()
        if not text:
            return False
        if text.lower() in COMMAND_TOKENS:
            return True
        return len(text) >= 3 and bool(MOVE_RE.fullmatch(text))

    def _submit_puzzle(self, text: str) -> None:
        s = self.session
        if s is None:
            self.status_msg = "puzzle still loading…"
            self._update_overlay()
            return
        r = s.try_move(text.strip())
        self._show_move_result(r)
        if s.finished:
            if s.won:
                # Show celebration + countdown; auto-destruct after ~3s.
                self.closing_at = time.monotonic() + self._closing_total
                self.status_msg = ""
                self._update_celebration_banner()
                self._update_overlay()
            else:
                self._finish_puzzle()
        else:
            self._update_overlay()

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

    # ── Output from Claude ─────────────────────────────────────────────

    def _handle_master(self) -> bool:
        try:
            data = os.read(self.master_fd, 4096)
        except OSError:
            return False
        if not data:
            return False
        write_raw(sys.stdout.fileno(), data)
        if self.puzzle_active and self.overlay_supported:
            # Re-place the existing image so it stays visible across scrolls.
            self._place_overlay()
        return True

    # ── Puzzle lifecycle ───────────────────────────────────────────────

    def _start_puzzle(self) -> None:
        if self.puzzle_active or self.fetch_pending:
            return
        if not self.overlay_supported:
            # Without graphics we don't render anything — proxy stays
            # transparent. (Future: text fallback via DECSTBM.)
            return

        self.layout.refresh()
        self.puzzle_active = True
        self.fetch_pending = True
        self.session = None
        self.snoop_buffer = ""
        self.status_msg = "fetching puzzle…"
        self.banner = ""
        self.claude_idle = False
        self.closing_at = None
        self._update_overlay()

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
            if not self.puzzle_active:
                continue
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
        if not self.puzzle_active:
            return
        self.claude_idle = True
        if self.closing_at is not None:
            # Mid-celebration; the next countdown tick will pick up the
            # post-claude banner flavour.
            self._update_celebration_banner()
            self._update_overlay()
            return
        self.status_msg = ""
        self.banner = "✓ Claude is done — finish at your leisure."
        self._update_overlay()

    def _finish_puzzle(self) -> None:
        if not self.puzzle_active:
            return
        if self.session is not None and self.session.finished:
            try:
                api.submit_result(self.session.p.id, win=self.session.won, rated=False)
            except Exception:
                pass
        self.puzzle_active = False
        self.fetch_pending = False
        self.session = None
        self.closing_at = None
        if self.overlay_supported and self.image_transmitted:
            write_raw(sys.stdout.fileno(), overlay.kitty_delete(IMAGE_ID))
            self.image_transmitted = False

    # ── Overlay rendering ──────────────────────────────────────────────

    def _update_overlay(self) -> None:
        """Regenerate the image and (re)transmit it, then place at the configured anchor.
        The spec is already pre-scaled to fit, so we pass cells_w/cells_h that
        match the spec (no distortion — pieces stay square)."""
        if not (self.puzzle_active and self.overlay_supported):
            return
        png = overlay.render_png(
            self.session, self.status_msg, self.banner, self.spec,
        )
        cells_w, cells_h = self.spec.cell_size()
        out = bytearray()
        out += cursor_save()
        anchor_row, anchor_col = self.layout.overlay_anchor(self.spec, self.settings.position)
        out += goto(anchor_row, anchor_col)
        out += overlay.kitty_transmit(png, IMAGE_ID, cells_w=cells_w, cells_h=cells_h)
        out += cursor_restore()
        write_raw(sys.stdout.fileno(), bytes(out))
        self.image_transmitted = True
        self.last_overlay_t = time.monotonic()

    def _place_overlay(self) -> None:
        """Re-place the existing image without retransmitting bytes (cheap).
        Throttled so we don't flood the terminal on every claude tick."""
        if not (self.puzzle_active and self.overlay_supported and self.image_transmitted):
            return
        now = time.monotonic()
        if now - self.last_overlay_t < 0.05:
            return
        out = bytearray()
        out += cursor_save()
        anchor_row, anchor_col = self.layout.overlay_anchor(self.spec, self.settings.position)
        out += goto(anchor_row, anchor_col)
        out += overlay.kitty_place(IMAGE_ID)
        out += cursor_restore()
        write_raw(sys.stdout.fileno(), bytes(out))
        self.last_overlay_t = now

    def _show_move_result(self, r: MoveResult) -> None:
        if r.kind == "ok":
            parts = [f"✓ {r.user_san}"]
            if r.opponent_san:
                parts.append(f"opp: {r.opponent_san}")
            self.status_msg = "  ·  ".join(parts)
        elif r.kind == "wrong":
            self.status_msg = f"✗ {r.message}"
        elif r.kind == "unparseable":
            self.status_msg = r.message or ""
        elif r.kind == "command":
            self.status_msg = r.message or ""


# ── Entry point ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hml", description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="Command to run under the wrapper, e.g. `hml claude`.")
    args = parser.parse_args(argv)

    if not args.command:
        parser.error("usage: hml <command> [args…]   e.g. hml claude")

    socket_path = Path(f"/tmp/hml-{os.getpid()}.sock")
    return Wrapper(args.command, socket_path).run()


if __name__ == "__main__":
    sys.exit(main())
