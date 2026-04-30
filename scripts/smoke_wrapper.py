"""End-to-end smoke test for the v4 wrapper.

Spawns `hml bash -i` in a 40×120 PTY (large enough for the puzzle to
activate), sends WORKING via the socket, and asserts:
  - puzzle glyph drew
  - DECSTBM scroll-region IS set (ESC[1;Nr) — needed to pin the bottom rows
  - PTY was shrunk after WORKING (bash sees fewer rows)
  - snoop+% submission works: typing `zzzz%` parses zzzz as a move, sends
    backspaces to bash so the typed prefix is wiped from bash's input,
    and the puzzle area shows "couldn't parse" feedback
  - typing `%` alone (without a move-shaped prefix) is forwarded to bash
    as a literal % (no interception)
  - 'Claude is done' banner appears after IDLE
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

    # WORKING
    cli = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    cli.sendto(b"WORKING\n", str(sock_path))

    end = time.time() + 6.0
    while time.time() < end:
        chunk = read_some(0.3)
        captured += chunk
        if b"\xe2\x99\x9f" in captured:
            break

    saw_puzzle_glyph = b"\xe2\x99\x9f" in captured
    saw_decstbm = re.search(rb"\x1b\[1;\d+r", bytes(captured)) is not None

    # PTY shrink check.
    os.write(fd, b"echo SHRUNK=$LINES\n")
    end = time.time() + 2.0
    while time.time() < end:
        captured += read_some(0.3)
    shrunk_match = re.findall(rb"SHRUNK=(\d+)", captured)
    shrunk_rows = int(shrunk_match[-1]) if shrunk_match else None
    pty_was_shrunk = (
        initial_rows is not None
        and shrunk_rows is not None
        and shrunk_rows < initial_rows
    )

    # SNOOP+% test: type `a1a1%` — `a1a1` matches the move-shape regex
    # so the wrapper intercepts %, parses 'a1a1' (illegal move on any
    # board), shows 'couldn't parse', and sends 4 backspaces to bash.
    captured_before_snoop = bytes(captured)
    os.write(fd, b"a1a1%")
    end = time.time() + 2.0
    while time.time() < end:
        captured += read_some(0.3)

    new_bytes = bytes(captured)[len(captured_before_snoop):]
    saw_wrong_msg = (
        b"couldn't parse" in new_bytes
        or b"not the puzzle move" in new_bytes
    )
    # Backspaces should have wiped 'a1a1' from bash's input — we should
    # see four BS bytes (0x08) sent toward bash. (The wrapper writes them
    # to master_fd; bash echoes them back, which appears as 0x08 0x20 0x08
    # patterns in the captured stream when terminal echo is on.)
    saw_backspaces = b"\x08" in new_bytes

    # Now press Enter in bash so any leftover snooped chars (none expected)
    # get cleared.
    os.write(fd, b"\n")
    end = time.time() + 1.0
    while time.time() < end:
        captured += read_some(0.2)

    # Negative test for snoop: type `hello world %` — the buffer is reset
    # by spaces (not a move pattern), so the `%` should pass through to
    # bash as a literal char (not intercepted).
    captured_before_neg = bytes(captured)
    os.write(fd, b"echo wrap-test-")
    os.write(fd, b"%\n")
    end = time.time() + 1.5
    while time.time() < end:
        captured += read_some(0.3)
    neg_bytes = bytes(captured)[len(captured_before_neg):]
    literal_pct_passed = b"wrap-test-%" in neg_bytes

    # IDLE → banner.
    cli.sendto(b"IDLE\n", str(sock_path))
    end = time.time() + 1.5
    while time.time() < end:
        captured += read_some(0.3)
    saw_banner = "Claude is done".encode() in captured

    # Quit.
    os.write(fd, b"exit\n")
    end = time.time() + 2.0
    while time.time() < end:
        chunk = read_some(0.2)
        if not chunk:
            break
        captured += chunk

    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass

    print(f"\n=== captured {len(captured)} bytes ===")
    print(f"saw puzzle glyph (♟): {saw_puzzle_glyph}")
    print(f"DECSTBM scroll-region IS set (pins bottom rows): {saw_decstbm}")
    print(f"PTY shrunk after WORKING ({initial_rows} → {shrunk_rows}): {pty_was_shrunk}")
    print(f"snoop+% intercepted 'a1a1%' and showed parse feedback: {saw_wrong_msg}")
    print(f"backspaces forwarded to wipe a1a1 from claude's input: {saw_backspaces}")
    print(f"literal '%' (not a move) passes through to child: {literal_pct_passed}")
    print(f"saw 'Claude is done' banner after IDLE: {saw_banner}")

    ok = all([
        saw_puzzle_glyph,
        saw_decstbm,
        pty_was_shrunk,
        saw_wrong_msg,
        saw_backspaces,
        literal_pct_passed,
        saw_banner,
    ])
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
