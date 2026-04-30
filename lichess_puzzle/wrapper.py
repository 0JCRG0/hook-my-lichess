"""PTY-proxy wrapper that runs Claude Code as a child and overlays a Lichess
puzzle in the bottom of the same terminal while Claude is working.

Usage: hml claude [<claude-args>...]

v4 design (no focus toggle):
  - Fork a PTY, exec the child argv inside it.
  - Parent terminal goes raw; bytes are forwarded both ways.
  - A Unix socket (path exported as $HML_SOCKET) lets hook scripts post
    "WORKING" / "IDLE" lines to switch puzzle state.

  - When WORKING:
      * shrink Claude's PTY to (full_rows - PUZZLE_ROWS, cols) and SIGWINCH.
        Claude renders into the smaller top region.
      * set DECSTBM to (1, claude_rows) on the user's terminal so any
        scroll-up triggered by Claude's output cannot push our puzzle into
        the scrollback. The bottom rows are physically pinned.
      * fetch a puzzle in a background thread (no event-loop blocking);
        the puzzle area shows "fetching…" until the result lands.

  - Input: every keystroke is forwarded to Claude as normal. The wrapper
    *also* snoops printable bytes (and backspaces) into a per-line buffer
    that's reset on Enter. The terminator is `%`:
        type your move, e.g.  e2e4%  (or h%  for hint, q%  to quit)
        → wrapper consumes the `%`, parses the buffered text as a puzzle
          move, sends N BACKSPACES to Claude so Claude's input field is
          wiped clean, and updates the puzzle area with the result.
    Pressing Enter as normal sends the line to Claude unchanged.

  - When IDLE: puzzle stays drawn with a "Claude is done" banner.
  - On finish/quit: DECSTBM reset, puzzle area cleared, PTY restored to
    full size, SIGWINCH, all input flows back to Claude untouched.
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

from . import api, board as board_mod, render
from .engine import PuzzleSession, MoveResult


CTRL_C = 0x03
ESC_BYTE = 0x1b
BACKSPACE = 0x7f
TERMINATOR = ord("%")

PUZZLE_ROWS = 14
MIN_TERMINAL_ROWS = 22

# Loose chess-move sanity check: snooped buffer must contain enough plausible
# move characters before we accept a `%` as a puzzle terminator. Otherwise we
# pass `%` through to Claude (so a literal `%` in a prompt still works).
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


def clear_line() -> bytes:
    return f"{ESC}[2K".encode()


def set_scroll_region(top: int, bottom: int) -> bytes:
    return f"{ESC}[{top};{bottom}r".encode()


def reset_scroll_region() -> bytes:
    return f"{ESC}[r".encode()


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

    @property
    def claude_rows(self) -> int:
        return max(1, self.rows - PUZZLE_ROWS)

    @property
    def puzzle_top(self) -> int:
        return self.claude_rows + 1

    def has_room_for_puzzle(self) -> bool:
        return self.rows >= MIN_TERMINAL_ROWS


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

        self.session: PuzzleSession | None = None
        self.puzzle_active: bool = False
        self.fetch_pending: bool = False
        self.snoop_buffer: str = ""  # what the user has typed since last Enter/%
        self.status_msg: str = ""
        self.banner: str = ""

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
            if self.puzzle_active:
                # Clean up any active overlay before exiting.
                write_raw(sys.stdout.fileno(), reset_scroll_region())
                self._resize_child(self.layout.rows, self.layout.cols)
            self._teardown_puzzle_layout()
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

    def _resize_child(self, rows: int, cols: int) -> None:
        try:
            packed = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, packed)
            os.kill(self.child_pid, signal.SIGWINCH)
        except OSError:
            pass

    def _sync_winsize_to_child(self) -> None:
        if self.puzzle_active:
            self._resize_child(self.layout.claude_rows, self.layout.cols)
        else:
            self._resize_child(self.layout.rows, self.layout.cols)

    def _on_resize(self) -> None:
        self.layout.refresh()
        if self.puzzle_active:
            # Re-establish scroll region for the new size.
            write_raw(sys.stdout.fileno(),
                      set_scroll_region(1, self.layout.claude_rows))
        self._sync_winsize_to_child()
        if self.puzzle_active:
            self._redraw_puzzle()

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

    # ── Input from user terminal ───────────────────────────────────────

    def _handle_stdin(self) -> bool:
        try:
            data = os.read(sys.stdin.fileno(), 4096)
        except OSError:
            return False
        if not data:
            return False

        if not self.puzzle_active:
            # Pass-through: nothing to snoop or intercept.
            write_raw(self.master_fd, data)
            return True

        forward = bytearray()
        for b in data:
            if b == TERMINATOR and self._buffer_is_puzzle_command():
                # Wipe the prefix from Claude's input (N backspaces) and submit.
                if forward:
                    write_raw(self.master_fd, bytes(forward))
                    forward.clear()
                n = len(self.snoop_buffer)
                write_raw(self.master_fd, bytes([BACKSPACE]) * n)
                text = self.snoop_buffer
                self.snoop_buffer = ""
                self._submit_puzzle(text)
                continue

            # Mirror the byte into our buffer where appropriate.
            if b in (0x0d, 0x0a):
                # Enter — user is sending to Claude. Reset our snoop buffer.
                self.snoop_buffer = ""
            elif b == BACKSPACE or b == 0x08:
                self.snoop_buffer = self.snoop_buffer[:-1]
            elif b == ESC_BYTE:
                # Escape sequences (arrows, function keys, paste mode etc.)
                # Don't try to interpret — just forward and reset our buffer
                # so we never accidentally treat junk as a move.
                self.snoop_buffer = ""
            elif b == CTRL_C:
                self.snoop_buffer = ""
            elif 0x20 <= b < 0x7f:
                self.snoop_buffer += chr(b)
            # else: ignore for snoop purposes (bell, tab, etc.) but still forward

            forward.append(b)

        if forward:
            write_raw(self.master_fd, bytes(forward))
        return True

    def _buffer_is_puzzle_command(self) -> bool:
        """Return True iff the snoop buffer looks like a chess move/command —
        otherwise we let the user type a literal `%` to Claude."""
        text = self.snoop_buffer.strip()
        if not text:
            return False
        if text.lower() in COMMAND_TOKENS:
            return True
        # Loose move sanity check: ≥3 chars, only chess-move characters.
        return len(text) >= 3 and bool(MOVE_RE.fullmatch(text))

    def _submit_puzzle(self, text: str) -> None:
        s = self.session
        if s is None:
            self.status_msg = "puzzle still loading…"
            self._redraw_puzzle()
            return
        r = s.try_move(text.strip())
        self._show_move_result(r)
        if s.finished:
            self._finish_puzzle()
        else:
            self._redraw_puzzle()

    # ── Output from Claude ─────────────────────────────────────────────

    def _handle_master(self) -> bool:
        try:
            data = os.read(self.master_fd, 4096)
        except OSError:
            return False
        if not data:
            return False
        write_raw(sys.stdout.fileno(), data)
        if self.puzzle_active:
            self._redraw_puzzle()
        return True

    # ── Puzzle lifecycle ───────────────────────────────────────────────

    def _start_puzzle(self) -> None:
        if self.puzzle_active or self.fetch_pending:
            return
        if not self.layout.has_room_for_puzzle():
            return

        self.layout.refresh()
        self.puzzle_active = True
        self.fetch_pending = True
        self.session = None
        self.snoop_buffer = ""
        self.status_msg = "fetching puzzle…"
        self.banner = ""

        # Pin the bottom rows: the terminal must not scroll past claude_rows.
        write_raw(sys.stdout.fileno(),
                  set_scroll_region(1, self.layout.claude_rows))
        self._resize_child(self.layout.claude_rows, self.layout.cols)
        self._redraw_puzzle()

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
            self._redraw_puzzle()

    def _on_idle(self) -> None:
        if not self.puzzle_active:
            return
        self.status_msg = ""
        self.banner = "✓ Claude is done — finish at your leisure."
        self._redraw_puzzle()

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
        # Reset terminal: drop the scroll region first so subsequent writes
        # can scroll the whole screen again.
        write_raw(sys.stdout.fileno(), reset_scroll_region())
        self._teardown_puzzle_layout()
        self._resize_child(self.layout.rows, self.layout.cols)

    def _teardown_puzzle_layout(self) -> None:
        out = bytearray()
        for r in range(self.layout.puzzle_top, self.layout.rows + 1):
            out += goto(r, 1) + clear_line()
        out += goto(self.layout.rows, 1)
        write_raw(sys.stdout.fileno(), bytes(out))

    # ── Puzzle rendering ───────────────────────────────────────────────

    def _redraw_puzzle(self) -> None:
        if not self.puzzle_active:
            return
        out = bytearray()
        out += cursor_save()

        # Top divider.
        out += goto(self.layout.puzzle_top, 1) + clear_line()
        out += f"{render.DIM}{'─' * min(self.layout.cols, 80)}{render.RESET}".encode()

        # Header.
        if self.session is not None:
            s = self.session
            you = render.color_name(s.p.user_color)
            themes = ", ".join(s.p.themes[:3])
            h1 = (
                f"{render.BOLD}♟ {s.p.id}{render.RESET}  "
                f"{render.DIM}{s.p.rating} · {themes} · play {you}{render.RESET}"
            )
        else:
            h1 = f"{render.BOLD}♟ Lichess puzzle{render.RESET}"
        out += goto(self.layout.puzzle_top + 1, 1) + clear_line() + h1.encode()

        # Body: 8 board rows, or placeholder.
        if self.session is not None:
            for i, line in enumerate(self.session.board_lines()):
                out += goto(self.layout.puzzle_top + 2 + i, 1) + clear_line() + line.encode()
        else:
            placeholder = f"{render.DIM}fetching puzzle…{render.RESET}"
            out += goto(self.layout.puzzle_top + 5, 1) + clear_line() + placeholder.encode()
            for i in range(8):
                if i == 3:
                    continue
                out += goto(self.layout.puzzle_top + 2 + i, 1) + clear_line()

        # Status line.
        status = self.banner or self.status_msg
        if not status and self.session is not None:
            if self.session.finished and self.session.won:
                status = f"{render.BOLD}\033[38;5;48m🏆 solved!{render.RESET}"
            elif self.session.finished:
                status = f"{render.DIM}puzzle ended.{render.RESET}"
            else:
                status = (
                    f"{render.DIM}type your move into Claude's input and end with "
                    f"{render.BOLD}%{render.RESET}{render.DIM} (e.g. e2e4%, h%, q%). "
                    f"Enter sends to Claude as usual.{render.RESET}"
                )
        out += goto(self.layout.puzzle_top + 11, 1) + clear_line() + (status or "").encode()

        # Show the snoop buffer so the user can see what we'd submit.
        if self.snoop_buffer:
            preview = (
                f"{render.DIM}buffer: {render.RESET}"
                f"{render.BOLD}{self.snoop_buffer}{render.RESET}"
                f"{render.DIM}  (add % to submit){render.RESET}"
            )
        else:
            preview = ""
        out += goto(self.layout.puzzle_top + 12, 1) + clear_line() + preview.encode()

        # Always restore cursor — the user is interacting with Claude's
        # input area, the puzzle is read-only display.
        out += cursor_restore()
        write_raw(sys.stdout.fileno(), bytes(out))

    def _show_move_result(self, r: MoveResult) -> None:
        if r.kind == "ok":
            parts = [f"\033[32m✓ {r.user_san}{render.RESET}"]
            if r.opponent_san:
                parts.append(f"{render.DIM}· opp: {r.opponent_san}{render.RESET}")
            self.status_msg = "  ".join(parts)
        elif r.kind == "wrong":
            self.status_msg = f"\033[31m✗ {r.message}{render.RESET}"
        elif r.kind == "unparseable":
            self.status_msg = f"{render.DIM}{r.message}{render.RESET}"
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
