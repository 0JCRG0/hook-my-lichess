"""I/O-agnostic puzzle game state machine. Drives both the standalone TUI
(cli.py) and the in-terminal wrapper (wrapper.py)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import chess

from . import board as board_mod
from . import render


MoveKind = Literal["ok", "wrong", "unparseable", "command", "noop"]


@dataclass
class MoveResult:
    kind: MoveKind
    user_san: str | None = None
    opponent_san: str | None = None
    finished: bool = False
    won: bool = False
    message: str | None = None
    command: str | None = None  # "hint" | "solve" | "quit"


class PuzzleSession:
    def __init__(self, puzzle: board_mod.Puzzle):
        self.p = puzzle
        self.sol_idx = 0
        self.last_move: chess.Move | None = puzzle.setup_move
        self.finished = False
        self.won = False

    @property
    def expected(self) -> chess.Move | None:
        if self.sol_idx >= len(self.p.solution):
            return None
        return self.p.solution[self.sol_idx]

    def try_move(self, text: str) -> MoveResult:
        if self.finished:
            return MoveResult(kind="noop")

        text = text.strip()
        low = text.lower()
        if low in {"q", "quit", "exit"}:
            self.finished = True
            self.won = False
            return MoveResult(kind="command", command="quit", finished=True, won=False)
        if low in {"h", "hint", "?"}:
            exp = self.expected
            sq = chess.square_name(exp.from_square) if exp else "?"
            return MoveResult(kind="command", command="hint",
                              message=f"hint: piece on {sq} moves.")
        if low in {"s", "solve", "give up"}:
            exp = self.expected
            san = self.p.board.san(exp) if exp else "?"
            self.finished = True
            self.won = False
            return MoveResult(kind="command", command="solve", finished=True, won=False,
                              message=f"solution was: {san}")

        move = self._parse(text)
        if move is None:
            return MoveResult(kind="unparseable",
                              message=f"couldn't parse '{text}'. UCI (d1d5) or SAN (Rxd5).")

        exp = self.expected
        if exp is None or move != exp:
            return MoveResult(kind="wrong", message="not the puzzle move.")

        # Correct move: play it.
        user_san = self.p.board.san(move)
        self.p.board.push(move)
        self.last_move = move
        self.sol_idx += 1

        if self.sol_idx >= len(self.p.solution):
            self.finished = True
            self.won = True
            return MoveResult(kind="ok", user_san=user_san, finished=True, won=True)

        # Auto-play opponent reply.
        reply = self.p.solution[self.sol_idx]
        reply_san = self.p.board.san(reply)
        self.p.board.push(reply)
        self.last_move = reply
        self.sol_idx += 1

        if self.sol_idx >= len(self.p.solution):
            self.finished = True
            self.won = True
            return MoveResult(kind="ok", user_san=user_san, opponent_san=reply_san,
                              finished=True, won=True)

        return MoveResult(kind="ok", user_san=user_san, opponent_san=reply_san)

    def _parse(self, text: str) -> chess.Move | None:
        try:
            return self.p.board.parse_san(text)
        except ValueError:
            pass
        try:
            m = chess.Move.from_uci(text.lower())
            if m in self.p.board.legal_moves:
                return m
        except ValueError:
            pass
        return None

    def board_lines(self) -> list[str]:
        return render.render_board(
            self.p.board,
            perspective=self.p.user_color,
            last_move=self.last_move,
        ).split("\n")

    def header_lines(self) -> list[str]:
        you = render.color_name(self.p.user_color)
        themes = ", ".join(self.p.themes)
        lines = [
            f"{render.BOLD}♟ {self.p.id}{render.RESET}  "
            f"{render.DIM}{self.p.rating} · {self.p.plays} plays · {themes}{render.RESET}",
            f"You play {render.BOLD}{you}{render.RESET}.",
        ]
        if self.p.setup_move_san and self.sol_idx == 0:
            opp = render.color_name(not self.p.user_color)
            lines.append(
                f"{render.DIM}{opp} just played {render.BOLD}{self.p.setup_move_san}"
                f"{render.RESET}{render.DIM}. Your move.{render.RESET}"
            )
        return lines
