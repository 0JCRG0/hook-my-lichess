"""Parse a Lichess puzzle payload into a board + solution.

Lichess convention: the PGN's last half-move is the opponent's setup move
(its index is initialPly). After replaying the full PGN, the position is the
one shown to the solver, and solution[0] is the user's first move.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import chess


@dataclass
class Puzzle:
    id: str
    rating: int
    plays: int
    themes: list[str]
    board: chess.Board
    solution: list[chess.Move]
    user_color: chess.Color
    setup_move: chess.Move | None  # the opponent's last move (already played on board)
    setup_move_san: str | None
    game_id: str
    players: list[dict[str, Any]]


def parse_puzzle(payload: dict[str, Any]) -> Puzzle:
    game = payload["game"]
    p = payload["puzzle"]

    pgn_moves = game["pgn"].split()
    initial_ply = p["initialPly"]

    board = chess.Board()
    for san in pgn_moves[:initial_ply]:
        board.push_san(san)

    setup_move: chess.Move | None = None
    setup_san: str | None = None
    if initial_ply < len(pgn_moves):
        setup_san = pgn_moves[initial_ply]
        setup_move = board.parse_san(setup_san)
        board.push(setup_move)

    solution = [chess.Move.from_uci(u) for u in p["solution"]]

    return Puzzle(
        id=p["id"],
        rating=p["rating"],
        plays=p["plays"],
        themes=p["themes"],
        board=board,
        solution=solution,
        user_color=board.turn,
        setup_move=setup_move,
        setup_move_san=setup_san,
        game_id=game["id"],
        players=game["players"],
    )
