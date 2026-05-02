"""End-to-end smoke test for the sidecar daemon (v6 architecture).

Spawns `hml-overlay start` inside a synthetic pty with HML_FORCE_OVERLAY=1
so the daemon believes the terminal supports Kitty graphics. Asserts:
  - PID file appears (daemon successfully daemonized)
  - Kitty graphics transmit (ESC_G a=T) reaches the tty
  - MOVE over the daemon's socket triggers a re-transmit
  - IDLE triggers another re-transmit (banner change)
  - SIGTERM emits a Kitty delete (ESC_G a=d) and cleans up PID file
  - No DECSTBM scroll-region escape (ESC[1;Nr) ever emitted
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import socket
import struct
import sys
import termios
import time
from pathlib import Path


def _session_key_for(pid: int) -> str:
    # Must match sidecar._session_key. After login_tty in the child,
    # the child's SID equals its own PID (it's the session leader).
    return f"{os.getsid(pid):x}"


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    overlay_bin = project / ".venv" / "bin" / "hml-overlay"
    if not overlay_bin.exists():
        print(f"venv hml-overlay not found at {overlay_bin}", file=sys.stderr)
        return 1

    master, slave = pty.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    pid = os.fork()
    if pid == 0:
        # login_tty handles setsid + acquire-CTTY + dup-to-fd-0/1/2.
        # After this, the child's SID == its own PID.
        os.login_tty(slave)
        os.close(master)
        os.environ["HML_FORCE_OVERLAY"] = "1"
        os.execv(str(overlay_bin), [str(overlay_bin), "start"])

    # Parent: derive the daemon's expected paths from the child's SID.
    key = _session_key_for(pid)
    pid_file = Path.home() / ".cache" / "hml" / f"overlay-{key}.pid"
    sock_path = Path(f"/tmp/hml-overlay-{key}.sock")
    for p in (pid_file, sock_path):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    os.close(slave)
    captured = bytearray()

    def read_some(timeout: float) -> bytes:
        r, _, _ = select.select([master], [], [], timeout)
        if not r:
            return b""
        try:
            return os.read(master, 65536)
        except OSError:
            return b""

    # Wait for the daemon to write its PID file.
    daemon_pid: int | None = None
    end = time.time() + 5.0
    while time.time() < end:
        if pid_file.exists():
            try:
                daemon_pid = int(pid_file.read_text().strip())
                break
            except (OSError, ValueError):
                pass
        captured += read_some(0.1)

    # Read for a bit to capture the initial Kitty transmit.
    end = time.time() + 4.0
    while time.time() < end and b"\x1b_Ga=T" not in captured:
        captured += read_some(0.2)
    saw_kitty_initial = b"\x1b_Ga=T" in captured

    # Send a MOVE → expect another transmit (regenerated image).
    cli = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    cli.settimeout(0.5)
    captured_mark = len(captured)
    if sock_path.exists():
        try:
            cli.sendto(b"MOVE a1a1\n", str(sock_path))
        except OSError:
            pass
    end = time.time() + 3.0
    while time.time() < end:
        captured += read_some(0.2)
    saw_kitty_after_move = b"\x1b_Ga=T" in bytes(captured)[captured_mark:]

    # IDLE → another transmit (banner flips to "Claude is done").
    captured_mark2 = len(captured)
    if sock_path.exists():
        try:
            cli.sendto(b"IDLE\n", str(sock_path))
        except OSError:
            pass
    end = time.time() + 2.0
    while time.time() < end:
        captured += read_some(0.2)
    saw_kitty_after_idle = b"\x1b_Ga=T" in bytes(captured)[captured_mark2:]

    # SIGTERM → expect kitty_delete and clean exit.
    captured_mark3 = len(captured)
    if daemon_pid is not None:
        try:
            os.kill(daemon_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    end = time.time() + 3.0
    while time.time() < end:
        captured += read_some(0.2)
        if daemon_pid is None or not _process_alive(daemon_pid):
            break
    saw_kitty_delete = b"\x1b_Ga=d" in bytes(captured)[captured_mark3:]
    daemon_exited = daemon_pid is None or not _process_alive(daemon_pid)
    pid_file_cleaned = not pid_file.exists()

    saw_decstbm = re.search(rb"\x1b\[1;\d+r", bytes(captured)) is not None

    try:
        os.close(master)
    except OSError:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass

    print(f"\n=== captured {len(captured)} bytes ===")
    print(f"PID file appeared (daemon started): {daemon_pid is not None}")
    print(f"Kitty initial transmit (a=T) seen: {saw_kitty_initial}")
    print(f"Kitty re-transmit after MOVE: {saw_kitty_after_move}")
    print(f"Kitty re-transmit after IDLE: {saw_kitty_after_idle}")
    print(f"Kitty delete on SIGTERM (a=d): {saw_kitty_delete}")
    print(f"Daemon exited cleanly: {daemon_exited}")
    print(f"PID file removed on exit: {pid_file_cleaned}")
    print(f"NO DECSTBM scroll-region emitted: {not saw_decstbm}")

    ok = all([
        daemon_pid is not None,
        saw_kitty_initial,
        saw_kitty_after_move,
        saw_kitty_after_idle,
        saw_kitty_delete,
        daemon_exited,
        pid_file_cleaned,
        not saw_decstbm,
    ])
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
