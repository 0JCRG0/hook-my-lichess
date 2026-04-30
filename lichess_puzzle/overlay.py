"""Kitty graphics protocol overlay: render the puzzle to a PNG and float
it on top of Claude's TUI without making Claude re-layout.

Graphics protocol reference: https://sw.kovidgoyal.net/kitty/graphics-protocol/
Works in Ghostty, Kitty, WezTerm. We detect support via env vars and
no-op on unsupported terminals.
"""

from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import chess
from PIL import Image, ImageDraw, ImageFont

from . import board as board_mod
from .engine import PuzzleSession


# ── Capability detection ──────────────────────────────────────────────────


def is_supported() -> bool:
    """Best-effort detection of Kitty graphics support."""
    if os.environ.get("HML_FORCE_OVERLAY"):
        return True
    if os.environ.get("KITTY_WINDOW_ID"):
        return True
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if term_program in {"ghostty", "wezterm", "kitty"}:
        return True
    if "kitty" in os.environ.get("TERM", "").lower():
        return True
    return False


# ── Image generation ──────────────────────────────────────────────────────


SQUARE_PX = 64
BOARD_PX = SQUARE_PX * 8                       # 512
HEADER_PX = 70
STATUS_PX = 96
PADDING_PX = 14
LABEL_GUTTER_PX = 24                            # space for rank (left) and file (bottom) labels
IMG_W = LABEL_GUTTER_PX + BOARD_PX + 2 * PADDING_PX                       # 568
IMG_H = HEADER_PX + BOARD_PX + LABEL_GUTTER_PX + STATUS_PX + 2 * PADDING_PX  # 730

LIGHT_SQ = (240, 217, 181)
DARK_SQ = (181, 136, 99)
HILITE = (205, 210, 106)
BG = (24, 24, 28)
FG = (235, 235, 235)
DIM = (160, 160, 165)
GREEN = (88, 207, 138)
RED = (220, 100, 110)

PIECE_GLYPH = {
    chess.PAWN: "♟",
    chess.KNIGHT: "♞",
    chess.BISHOP: "♝",
    chess.ROOK: "♜",
    chess.QUEEN: "♛",
    chess.KING: "♚",
}

PIECE_LETTER = {
    chess.PAWN: "P",
    chess.KNIGHT: "N",
    chess.BISHOP: "B",
    chess.ROOK: "R",
    chess.QUEEN: "Q",
    chess.KING: "K",
}

FONT_CANDIDATES = [
    "/System/Library/Fonts/Apple Symbols.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

UI_FONT_CANDIDATES = [
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/SFNSDisplay.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _load_font(candidates: list[str], size: int) -> ImageFont.ImageFont:
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _glyph_supported(font: ImageFont.ImageFont) -> bool:
    """Check if the font has the chess unicode glyph for ♟ (0x265F)."""
    try:
        return font.getbbox("♟")[2] > 0  # type: ignore[attr-defined]
    except Exception:
        return False


@dataclass
class _Fonts:
    piece: ImageFont.ImageFont
    piece_supports_glyphs: bool
    ui: ImageFont.ImageFont
    ui_small: ImageFont.ImageFont
    label: ImageFont.ImageFont


_fonts_cache: Optional[_Fonts] = None


def _fonts() -> _Fonts:
    global _fonts_cache
    if _fonts_cache is not None:
        return _fonts_cache
    piece = _load_font(FONT_CANDIDATES, 52)
    ui = _load_font(UI_FONT_CANDIDATES, 24)
    ui_small = _load_font(UI_FONT_CANDIDATES, 18)
    label = _load_font(UI_FONT_CANDIDATES, 14)
    _fonts_cache = _Fonts(
        piece=piece,
        piece_supports_glyphs=_glyph_supported(piece),
        ui=ui,
        ui_small=ui_small,
        label=label,
    )
    return _fonts_cache


def render_png(
    session: PuzzleSession | None,
    status_msg: str,
    banner: str,
) -> bytes:
    fonts = _fonts()
    img = Image.new("RGB", (IMG_W, IMG_H), BG)
    d = ImageDraw.Draw(img)

    # Header
    if session is not None:
        s = session
        title = f"♟ {s.p.id}  {s.p.rating}"
        themes = ", ".join(s.p.themes[:3])
        sub = f"play {board_mod.chess.COLOR_NAMES[s.p.user_color]} · {themes}"
    else:
        title = "♟ Lichess puzzle"
        sub = "fetching…"
    d.text((PADDING_PX, PADDING_PX), title, fill=FG, font=fonts.ui)
    d.text((PADDING_PX, PADDING_PX + 30), sub, fill=DIM, font=fonts.ui_small)

    # Board (offset right by the rank-label gutter)
    perspective = session.p.user_color if session is not None else chess.WHITE
    board_origin = (PADDING_PX + LABEL_GUTTER_PX, HEADER_PX + PADDING_PX)
    if session is not None:
        _draw_board(d, session, board_origin, fonts)
    else:
        _draw_empty_board(d, board_origin)
        msg = "fetching puzzle…"
        bbox = d.textbbox((0, 0), msg, font=fonts.ui)
        w = bbox[2] - bbox[0]
        d.text(
            (board_origin[0] + (BOARD_PX - w) / 2,
             board_origin[1] + BOARD_PX / 2 - 12),
            msg, fill=FG, font=fonts.ui,
        )
    _draw_grid_labels(d, board_origin, perspective, fonts)

    # Status (banner takes precedence — Claude is done message)
    status_origin_y = HEADER_PX + BOARD_PX + LABEL_GUTTER_PX + PADDING_PX
    text, color = _status_text_and_color(session, status_msg, banner)
    _wrap_text(d, text, (PADDING_PX, status_origin_y),
               max_width=IMG_W - 2 * PADDING_PX, font=fonts.ui_small,
               fill=color, line_height=24)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _draw_board(
    d: ImageDraw.ImageDraw,
    s: PuzzleSession,
    origin: tuple[int, int],
    fonts: _Fonts,
) -> None:
    ox, oy = origin
    perspective = s.p.user_color
    last_move = s.last_move
    hilite_squares = set()
    if last_move is not None:
        hilite_squares = {last_move.from_square, last_move.to_square}

    ranks = range(7, -1, -1) if perspective == chess.WHITE else range(0, 8)
    files = range(0, 8) if perspective == chess.WHITE else range(7, -1, -1)

    for ry, r in enumerate(ranks):
        for cx, f in enumerate(files):
            sq = chess.square(f, r)
            is_light = (chess.square_file(sq) + chess.square_rank(sq)) % 2 == 1
            color = HILITE if sq in hilite_squares else (LIGHT_SQ if is_light else DARK_SQ)
            x0 = ox + cx * SQUARE_PX
            y0 = oy + ry * SQUARE_PX
            d.rectangle([x0, y0, x0 + SQUARE_PX - 1, y0 + SQUARE_PX - 1], fill=color)

            piece = s.p.board.piece_at(sq)
            if piece is None:
                continue
            _draw_piece(d, piece, (x0, y0), fonts)


def _draw_grid_labels(
    d: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    perspective: chess.Color,
    fonts: _Fonts,
) -> None:
    """Rank numbers (1-8) on the left of each row, files (a-h) on the
    bottom of each column. Orientation follows the user's perspective."""
    ox, oy = origin
    ranks = range(7, -1, -1) if perspective == chess.WHITE else range(0, 8)
    files = range(0, 8) if perspective == chess.WHITE else range(7, -1, -1)

    for ry, r in enumerate(ranks):
        text = str(r + 1)
        bbox = d.textbbox((0, 0), text, font=fonts.label)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        x = ox - LABEL_GUTTER_PX + (LABEL_GUTTER_PX - w) / 2 - bbox[0]
        y = oy + ry * SQUARE_PX + (SQUARE_PX - h) / 2 - bbox[1]
        d.text((x, y), text, fill=DIM, font=fonts.label)

    for cx, f in enumerate(files):
        text = "abcdefgh"[f]
        bbox = d.textbbox((0, 0), text, font=fonts.label)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        x = ox + cx * SQUARE_PX + (SQUARE_PX - w) / 2 - bbox[0]
        y = oy + BOARD_PX + (LABEL_GUTTER_PX - h) / 2 - bbox[1]
        d.text((x, y), text, fill=DIM, font=fonts.label)


def _draw_empty_board(d: ImageDraw.ImageDraw, origin: tuple[int, int]) -> None:
    ox, oy = origin
    for r in range(8):
        for f in range(8):
            is_light = (f + r) % 2 == 1
            color = LIGHT_SQ if is_light else DARK_SQ
            x0 = ox + f * SQUARE_PX
            y0 = oy + r * SQUARE_PX
            d.rectangle([x0, y0, x0 + SQUARE_PX - 1, y0 + SQUARE_PX - 1], fill=color)


def _draw_piece(
    d: ImageDraw.ImageDraw,
    piece: chess.Piece,
    square_origin: tuple[int, int],
    fonts: _Fonts,
) -> None:
    sx, sy = square_origin
    if fonts.piece_supports_glyphs:
        glyph = PIECE_GLYPH[piece.piece_type]
        # Outline + fill for visibility on both light and dark squares.
        outline = (0, 0, 0) if piece.color == chess.WHITE else (255, 255, 255)
        fill = (255, 255, 255) if piece.color == chess.WHITE else (0, 0, 0)
        # Center the glyph in the square.
        bbox = d.textbbox((0, 0), glyph, font=fonts.piece)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        cx = sx + (SQUARE_PX - w) / 2 - bbox[0]
        cy = sy + (SQUARE_PX - h) / 2 - bbox[1]
        # Draw thin outline first.
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            d.text((cx + dx, cy + dy), glyph, font=fonts.piece, fill=outline)
        d.text((cx, cy), glyph, font=fonts.piece, fill=fill)
    else:
        letter = PIECE_LETTER[piece.piece_type]
        fill = (255, 255, 255) if piece.color == chess.WHITE else (0, 0, 0)
        bbox = d.textbbox((0, 0), letter, font=fonts.piece)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        d.text(
            (sx + (SQUARE_PX - w) / 2 - bbox[0],
             sy + (SQUARE_PX - h) / 2 - bbox[1]),
            letter, font=fonts.piece, fill=fill,
        )


def _status_text_and_color(
    session: PuzzleSession | None,
    status_msg: str,
    banner: str,
) -> tuple[str, tuple[int, int, int]]:
    if banner:
        return banner.lstrip("✓ "), GREEN
    if status_msg:
        if status_msg.startswith("\033[31m") or "not the puzzle move" in status_msg:
            return _strip_ansi(status_msg), RED
        if status_msg.startswith("\033[32m") or "✓ " in status_msg:
            return _strip_ansi(status_msg), GREEN
        return _strip_ansi(status_msg), FG
    if session is None:
        return "loading…", DIM
    if session.finished and session.won:
        return "🏆 solved!", GREEN
    if session.finished:
        return "puzzle ended.", DIM
    return "type your move into Claude's input + %  (e.g. e2e4%)", DIM


def _strip_ansi(s: str) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _wrap_text(
    d: ImageDraw.ImageDraw,
    text: str,
    origin: tuple[int, int],
    max_width: int,
    font: ImageFont.ImageFont,
    fill,
    line_height: int,
) -> None:
    if not text:
        return
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for w in words:
        candidate = (current + " " + w).strip() if current else w
        bbox = d.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] > max_width and current:
            lines.append(current)
            current = w
        else:
            current = candidate
    if current:
        lines.append(current)
    x, y = origin
    for line in lines[:3]:
        d.text((x, y), line, font=font, fill=fill)
        y += line_height


# ── Kitty graphics protocol ───────────────────────────────────────────────


CHUNK = 4096


def _kitty(payload: str) -> bytes:
    return f"\x1b_G{payload}\x1b\\".encode()


def kitty_transmit(png: bytes, image_id: int) -> bytes:
    """Transmit + display a PNG image at the current cursor position.
    Uses chunked encoding so we can send larger images."""
    b64 = base64.b64encode(png).decode("ascii")
    if len(b64) <= CHUNK:
        return _kitty(f"a=T,f=100,i={image_id},q=2;{b64}")
    parts = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)]
    out = bytearray()
    out += _kitty(f"a=T,f=100,i={image_id},q=2,m=1;{parts[0]}")
    for p in parts[1:-1]:
        out += _kitty(f"m=1;{p}")
    out += _kitty(f"m=0;{parts[-1]}")
    return bytes(out)


def kitty_place(image_id: int, placement_id: int = 1) -> bytes:
    """Re-place an already-transmitted image at the current cursor."""
    return _kitty(f"a=p,i={image_id},p={placement_id},q=2")


def kitty_delete(image_id: int) -> bytes:
    """Delete an image (and any placements). Use a=d for dispose."""
    return _kitty(f"a=d,d=I,i={image_id},q=2")


# ── Cell sizing ────────────────────────────────────────────────────────────


def image_cell_size() -> tuple[int, int]:
    """Approximate cells the image will occupy. Tunable for layout
    calculations in the wrapper. Cells are typically ~10×20 px on
    Retina-class displays."""
    cols = max(1, IMG_W // 14)   # ~29 cells wide
    rows = max(1, IMG_H // 28)   # ~19 cells tall
    return cols, rows
