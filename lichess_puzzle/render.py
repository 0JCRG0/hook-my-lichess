"""ANSI Unicode chess board renderer."""

from __future__ import annotations

import chess

LIGHT_BG = "\033[48;5;180m"
DARK_BG = "\033[48;5;94m"
HILITE_BG = "\033[48;5;142m"
WHITE_PIECE = "\033[38;5;231;1m"
BLACK_PIECE = "\033[38;5;232;1m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

PIECE_GLYPH = {
    chess.PAWN: "♟",
    chess.KNIGHT: "♞",
    chess.BISHOP: "♝",
    chess.ROOK: "♜",
    chess.QUEEN: "♛",
    chess.KING: "♚",
}


def _square_str(board: chess.Board, sq: int, highlight: bool) -> str:
    is_light = (chess.square_file(sq) + chess.square_rank(sq)) % 2 == 1
    bg = HILITE_BG if highlight else (LIGHT_BG if is_light else DARK_BG)
    piece = board.piece_at(sq)
    if piece is None:
        return f"{bg}   {RESET}"
    glyph = PIECE_GLYPH[piece.piece_type]
    fg = WHITE_PIECE if piece.color == chess.WHITE else BLACK_PIECE
    return f"{bg}{fg} {glyph} {RESET}"


def render_board(
    board: chess.Board,
    perspective: chess.Color = chess.WHITE,
    last_move: chess.Move | None = None,
) -> str:
    highlight = set()
    if last_move is not None:
        highlight = {last_move.from_square, last_move.to_square}

    ranks = range(7, -1, -1) if perspective == chess.WHITE else range(0, 8)
    files = range(0, 8) if perspective == chess.WHITE else range(7, -1, -1)

    lines = []
    for r in ranks:
        row = f" {r + 1} "
        for f in files:
            sq = chess.square(f, r)
            row += _square_str(board, sq, sq in highlight)
        lines.append(row)
    file_labels = "    " + "  ".join("abcdefgh" if perspective == chess.WHITE else "hgfedcba")
    lines.append(f"{DIM}{file_labels}{RESET}")
    return "\n".join(lines)


def color_name(color: chess.Color) -> str:
    return "White" if color == chess.WHITE else "Black"
