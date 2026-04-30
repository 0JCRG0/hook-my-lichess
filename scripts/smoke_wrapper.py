"""End-to-end smoke test for the v5 wrapper (Kitty overlay).

Spawns `hml bash -i` in a 40×120 PTY with HML_FORCE_OVERLAY=1 so the
wrapper believes the terminal supports Kitty graphics. Sends WORKING via
the socket and asserts:
  - NO DECSTBM scroll-region escape (ESC[1;Nr) is emitted
  - bash's $LINES is unchanged after WORKING (no PTY shrink)
  - a Kitty graphics escape (ESC_G…ESC_BSL) IS emitted (image transmit)
  - snoop+% intercepts a move-shaped buffer
  - literal `%` after non-move text passes through to the child
  - on IDLE the image is updated (another Kitty escape arrives)
  - on quit the image is deleted (a=d Kitty escape)
"""

from __future__ import annotations

import os
import pty
import re
import select
import socket
import struct
import sys
import termios
import time
import fcntl
from pathlib import Path


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    hml = project / ".venv" / "bin" / "hml"
    if not hml.exists():
        print(f"venv hml not found at {hml}", file=sys.stderr)
        return 1

    pid, fd = pty.fork()
    if pid == 0:
        os.environ["HML_FORCE_OVERLAY"] = "1"
        os.execv(str(hml), [str(hml), "bash", "-i"])

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    captured = bytearray()

    def read_some(timeout: float) -> bytes:
        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            return b""
        try:
            return os.read(fd, 65536)
        except OSError:
            return b""

    expected_sock = Path(f"/tmp/hml-{pid}.sock")

    def discover_socket() -> Path | None:
        for _ in range(40):
            if expected_sock.exists():
                return expected_sock
            time.sleep(0.1)
        return None

    sock_path = discover_socket()
    print(f"discovered socket: {sock_path}")

    end = time.time() + 1.0
    while time.time() < end:
        captured += read_some(0.2)

    os.write(fd, b"echo INITROWS=$LINES\n")
    end = time.time() + 1.0
    while time.time() < end:
        captured += read_some(0.2)
    initial_match = re.search(rb"INITROWS=(\d+)", captured)
    initial_rows = int(initial_match.group(1)) if initial_match else None

    cli = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    cli.sendto(b"WORKING\n", str(sock_path))

    end = time.time() + 6.0
    saw_kitty_initial = False
    while time.time() < end:
        chunk = read_some(0.3)
        captured += chunk
        if b"\x1b_Ga=T" in captured:
            saw_kitty_initial = True
            break

    saw_decstbm = re.search(rb"\x1b\[1;\d+r", bytes(captured)) is not None

    os.write(fd, b"echo POSTROWS=$LINES\n")
    end = time.time() + 2.0
    while time.time() < end:
        captured += read_some(0.3)
    post_match = re.findall(rb"POSTROWS=(\d+)", captured)
    post_rows = int(post_match[-1]) if post_match else None
    pty_unchanged = post_rows == initial_rows

    # snoop+% intercept (a1a1% = move-shaped, illegal → "couldn't parse").
    captured_mark = len(captured)
    os.write(fd, b"a1a1%")
    end = time.time() + 2.0
    while time.time() < end:
        captured += read_some(0.3)
    after_snoop = bytes(captured)[captured_mark:]
    # Bash typically echoes a 0x7f input as the "erase visible char" sequence
    # \x08\x20\x08 (BS, space, BS). Look for that pattern OR any 0x08.
    saw_backspaces = (
        b"\x08\x20\x08" in after_snoop
        or after_snoop.count(b"\x08") >= 1
    )

    os.write(fd, b"\n")
    time.sleep(0.5)
    captured += read_some(0.5)

    # Negative test: typing `wrap-test-%` after non-move text should pass
    # through to the child.
    captured_mark2 = len(captured)
    os.write(fd, b"echo wrap-test-")
    os.write(fd, b"%\n")
    end = time.time() + 1.5
    while time.time() < end:
        captured += read_some(0.3)
    literal_pct_passed = b"wrap-test-%" in bytes(captured)[captured_mark2:]

    # IDLE → the image should be retransmitted (banner change).
    captured_mark3 = len(captured)
    cli.sendto(b"IDLE\n", str(sock_path))
    end = time.time() + 2.0
    while time.time() < end:
        captured += read_some(0.3)
    saw_kitty_after_idle = b"\x1b_Ga=T" in bytes(captured)[captured_mark3:]

    os.write(fd, b"exit\n")
    end = time.time() + 2.0
    while time.time() < end:
        chunk = read_some(0.2)
        if not chunk:
            break
        captured += chunk

    saw_kitty_delete = b"\x1b_Ga=d" in captured

    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass

    print(f"\n=== captured {len(captured)} bytes ===")
    print(f"NO DECSTBM scroll-region emitted: {not saw_decstbm}")
    print(f"PTY size unchanged after WORKING ({initial_rows} → {post_rows}): {pty_unchanged}")
    print(f"Kitty initial transmit (ESC_G a=T) seen: {saw_kitty_initial}")
    print(f"snoop+% triggered backspaces toward child: {saw_backspaces}")
    print(f"literal '%' passes through after non-move text: {literal_pct_passed}")
    print(f"Kitty re-transmit after IDLE (image refreshed): {saw_kitty_after_idle}")
    print(f"Kitty delete on exit: {saw_kitty_delete}")

    ok = all([
        not saw_decstbm,
        pty_unchanged,
        saw_kitty_initial,
        saw_backspaces,
        literal_pct_passed,
        saw_kitty_after_idle,
        saw_kitty_delete,
    ])
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
