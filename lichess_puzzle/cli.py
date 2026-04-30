"""Standalone puzzle TUI. Useful for testing the puzzle engine without
going through the wrapper. Hook integration uses wrapper.py instead."""

from __future__ import annotations

import argparse
import sys

from . import api, board as board_mod, render
from .engine import PuzzleSession


def _print_session(s: PuzzleSession) -> None:
    print()
    for line in s.header_lines():
        print(line)
    print()
    for line in s.board_lines():
        print(line)
    print()


def play(*, no_submit: bool, rated: bool) -> int:
    print(f"{render.DIM}Fetching a puzzle from Lichess…{render.RESET}", flush=True)
    try:
        payload = api.fetch_next_puzzle()
    except Exception as e:
        print(f"\033[31mFailed to fetch puzzle: {e}{render.RESET}", file=sys.stderr)
        return 1

    s = PuzzleSession(board_mod.parse_puzzle(payload))
    _print_session(s)

    while not s.finished:
        try:
            raw = input(f"{render.BOLD}your move> {render.RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            s.finished = True
            s.won = False
            break
        if not raw:
            continue

        r = s.try_move(raw)
        if r.kind == "ok":
            print(f"\033[32m✓ {r.user_san}{render.RESET}")
            if r.opponent_san:
                print(f"{render.DIM}opponent: {r.opponent_san}{render.RESET}")
            _print_session(s)
        elif r.kind == "wrong":
            print(f"\033[31m✗ {r.message}{render.RESET} {render.DIM}(h=hint, s=solve, q=quit){render.RESET}")
        elif r.kind == "unparseable":
            print(f"{render.DIM}{r.message}{render.RESET}")
        elif r.kind == "command":
            if r.message:
                print(f"{render.DIM}{r.message}{render.RESET}")

    if s.won:
        print(f"\n{render.BOLD}\033[38;5;48m🏆 Solved!{render.RESET}\n")
    else:
        print(f"\n{render.DIM}Puzzle ended.{render.RESET}\n")

    if not no_submit:
        try:
            api.submit_result(s.p.id, win=s.won, rated=rated)
        except Exception as e:
            print(f"{render.DIM}(could not submit: {e}){render.RESET}", file=sys.stderr)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lichess-puzzle")
    parser.add_argument("--no-submit", action="store_true")
    parser.add_argument("--rated", action="store_true")
    args = parser.parse_args(argv)
    return play(no_submit=args.no_submit, rated=args.rated)


if __name__ == "__main__":
    sys.exit(main())
