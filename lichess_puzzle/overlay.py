"""Kitty graphics protocol overlay: render the puzzle to a PNG and float
it on top of Claude's TUI without making Claude re-layout.

Graphics protocol reference: https://sw.kovidgoyal.net/kitty/graphics-protocol/
Works in Ghostty, Kitty, WezTerm. We detect support via env vars and
no-op on unsupported terminals.

All dimensions live on `OverlaySpec`. The wrapper builds a spec from the
user's `settings.size` (preset name or numeric scale) and threads it
through `render_png` and `image_cell_size`.
"""

from __future__ import annotations

import base64
import dataclasses
import io
import os
from dataclasses import dataclass, field
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


# ── Spec ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OverlaySpec:
    square_px: int = 64
    header_h: int = 76
    status_h: int = 44
    padding: int = 14
    label_gutter: int = 30
    piece_pt: int = 56
    ui_pt: int = 26
    ui_small_pt: int = 20
    label_pt: int = 22

    @classmethod
    def from_scale(cls, scale: float) -> "OverlaySpec":
        d = cls()
        return cls(**{
            f.name: max(1, int(round(getattr(d, f.name) * scale)))
            for f in dataclasses.fields(cls)
        })

    @property
    def board_px(self) -> int:
        return self.square_px * 8

    @property
    def img_w(self) -> int:
        return self.label_gutter + self.board_px + 2 * self.padding

    @property
    def img_h(self) -> int:
        return (
            self.header_h
            + self.board_px
            + self.label_gutter
            + self.status_h
            + 2 * self.padding
        )

    def cell_size(self) -> tuple[int, int]:
        """Approx cells the image will occupy. We bias slightly wide on the
        column axis so kitty stretches the image horizontally — chess piece
        glyphs render visibly wider this way."""
        return (max(1, self.img_w // 11), max(1, self.img_h // 28))


DEFAULT_SPEC = OverlaySpec()


# Backwards-compat helper for older callers.
def image_cell_size(spec: OverlaySpec = DEFAULT_SPEC) -> tuple[int, int]:
    return spec.cell_size()


# ── Colours ────────────────────────────────────────────────────────────────


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


_fonts_cache: dict[tuple[int, int, int, int], _Fonts] = {}


def _fonts(spec: OverlaySpec) -> _Fonts:
    key = (spec.piece_pt, spec.ui_pt, spec.ui_small_pt, spec.label_pt)
    cached = _fonts_cache.get(key)
    if cached is not None:
        return cached
    piece = _load_font(FONT_CANDIDATES, spec.piece_pt)
    ui = _load_font(UI_FONT_CANDIDATES, spec.ui_pt)
    ui_small = _load_font(UI_FONT_CANDIDATES, spec.ui_small_pt)
    label = _load_font(UI_FONT_CANDIDATES, spec.label_pt)
    f = _Fonts(
        piece=piece,
        piece_supports_glyphs=_glyph_supported(piece),
        ui=ui,
        ui_small=ui_small,
        label=label,
    )
    _fonts_cache[key] = f
    return f


# ── Image rendering ────────────────────────────────────────────────────────


def render_png(
    session: PuzzleSession | None,
    status_msg: str,
    banner: str,
    spec: OverlaySpec = DEFAULT_SPEC,
) -> bytes:
    fonts = _fonts(spec)
    img = Image.new("RGB", (spec.img_w, spec.img_h), BG)
    d = ImageDraw.Draw(img)

    # Header.
    if session is not None:
        s = session
        title = f"Puzzle #{s.p.id}  ·  Rating {s.p.rating}  ·  {s.p.plays:,} plays"
        themes = ", ".join(s.p.themes[:3])
        sub = f"play {board_mod.chess.COLOR_NAMES[s.p.user_color]}  ·  {themes}"
    else:
        title = "Lichess puzzle"
        sub = "fetching…"
    d.text((spec.padding, spec.padding), title, fill=FG, font=fonts.ui)
    d.text(
        (spec.padding, spec.padding + int(round(spec.ui_pt * 1.4))),
        sub, fill=DIM, font=fonts.ui_small,
    )

    # Board.
    perspective = session.p.user_color if session is not None else chess.WHITE
    board_origin = (spec.padding + spec.label_gutter, spec.header_h + spec.padding)
    if session is not None:
        _draw_board(d, session, board_origin, fonts, spec)
    else:
        _draw_empty_board(d, board_origin, spec)
        msg = "fetching puzzle…"
        bbox = d.textbbox((0, 0), msg, font=fonts.ui)
        w = bbox[2] - bbox[0]
        d.text(
            (board_origin[0] + (spec.board_px - w) / 2,
             board_origin[1] + spec.board_px / 2 - 12),
            msg, fill=FG, font=fonts.ui,
        )
    _draw_grid_labels(d, board_origin, perspective, fonts, spec)

    # Status / banner share one area.
    status_origin_y = spec.header_h + spec.board_px + spec.label_gutter + spec.padding
    text, color = _status_text_and_color(session, status_msg, banner)
    _wrap_text(
        d, text, (spec.padding, status_origin_y),
        max_width=spec.img_w - 2 * spec.padding,
        font=fonts.ui_small, fill=color,
        line_height=int(round(spec.ui_small_pt * 1.2)),
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _draw_board(
    d: ImageDraw.ImageDraw,
    s: PuzzleSession,
    origin: tuple[int, int],
    fonts: _Fonts,
    spec: OverlaySpec,
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
            x0 = ox + cx * spec.square_px
            y0 = oy + ry * spec.square_px
            d.rectangle(
                [x0, y0, x0 + spec.square_px - 1, y0 + spec.square_px - 1],
                fill=color,
            )

            piece = s.p.board.piece_at(sq)
            if piece is None:
                continue
            _draw_piece(d, piece, (x0, y0), fonts, spec)


def _draw_grid_labels(
    d: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    perspective: chess.Color,
    fonts: _Fonts,
    spec: OverlaySpec,
) -> None:
    ox, oy = origin
    ranks = range(7, -1, -1) if perspective == chess.WHITE else range(0, 8)
    files = range(0, 8) if perspective == chess.WHITE else range(7, -1, -1)

    for ry, r in enumerate(ranks):
        text = str(r + 1)
        bbox = d.textbbox((0, 0), text, font=fonts.label)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        x = ox - spec.label_gutter + (spec.label_gutter - w) / 2 - bbox[0]
        y = oy + ry * spec.square_px + (spec.square_px - h) / 2 - bbox[1]
        d.text((x, y), text, fill=DIM, font=fonts.label)

    for cx, f in enumerate(files):
        text = "abcdefgh"[f]
        bbox = d.textbbox((0, 0), text, font=fonts.label)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        x = ox + cx * spec.square_px + (spec.square_px - w) / 2 - bbox[0]
        y = oy + spec.board_px + (spec.label_gutter - h) / 2 - bbox[1]
        d.text((x, y), text, fill=DIM, font=fonts.label)


def _draw_empty_board(
    d: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    spec: OverlaySpec,
) -> None:
    ox, oy = origin
    for r in range(8):
        for f in range(8):
            is_light = (f + r) % 2 == 1
            color = LIGHT_SQ if is_light else DARK_SQ
            x0 = ox + f * spec.square_px
            y0 = oy + r * spec.square_px
            d.rectangle(
                [x0, y0, x0 + spec.square_px - 1, y0 + spec.square_px - 1],
                fill=color,
            )


def _draw_piece(
    d: ImageDraw.ImageDraw,
    piece: chess.Piece,
    square_origin: tuple[int, int],
    fonts: _Fonts,
    spec: OverlaySpec,
) -> None:
    sx, sy = square_origin
    if fonts.piece_supports_glyphs:
        glyph = PIECE_GLYPH[piece.piece_type]
        outline = (0, 0, 0) if piece.color == chess.WHITE else (255, 255, 255)
        fill = (255, 255, 255) if piece.color == chess.WHITE else (0, 0, 0)
        bbox = d.textbbox((0, 0), glyph, font=fonts.piece)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        cx = sx + (spec.square_px - w) / 2 - bbox[0]
        cy = sy + (spec.square_px - h) / 2 - bbox[1]
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
            (sx + (spec.square_px - w) / 2 - bbox[0],
             sy + (spec.square_px - h) / 2 - bbox[1]),
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
        return "★ solved!", GREEN
    if session.finished:
        return "puzzle ended.", DIM
    return "type  p:<move>  as your prompt  (e.g. p:e2e4)", DIM


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


def kitty_transmit(
    png: bytes,
    image_id: int,
    cells_w: int | None = None,
    cells_h: int | None = None,
    placement_id: int = 1,
) -> bytes:
    """Transmit + display an image at a stable placement id.

    `p={placement_id}` pins the placement so subsequent transmits
    *replace* it instead of stacking up additional ghost placements
    (without `p=`, Kitty/Ghostty assign a fresh id each transmit, which
    leaves the earlier copies lingering on screen). `C=1` so the
    cursor doesn't move after placement (otherwise a too-tall image
    scrolls the whole terminal). When cells_w / cells_h are passed,
    kitty scales the image to that exact cell box, which lets us cap
    the image to the available terminal area.
    """
    extras = f",C=1,p={placement_id}"
    if cells_w is not None:
        extras += f",c={cells_w}"
    if cells_h is not None:
        extras += f",r={cells_h}"
    b64 = base64.b64encode(png).decode("ascii")
    if len(b64) <= CHUNK:
        return _kitty(f"a=T,f=100,i={image_id},q=2{extras};{b64}")
    parts = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)]
    out = bytearray()
    out += _kitty(f"a=T,f=100,i={image_id},q=2{extras},m=1;{parts[0]}")
    for p in parts[1:-1]:
        out += _kitty(f"m=1;{p}")
    out += _kitty(f"m=0;{parts[-1]}")
    return bytes(out)


def kitty_place(image_id: int, placement_id: int = 1) -> bytes:
    return _kitty(f"a=p,i={image_id},p={placement_id},C=1,q=2")


def kitty_delete_placement(image_id: int, placement_id: int = 1) -> bytes:
    """Delete just the placement (keep image data cached). Use this between
    re-places to kill any old placement that scrolled into the buffer —
    relying on 'same placement_id replaces' semantics is unreliable across
    Kitty/Ghostty/WezTerm versions."""
    return _kitty(f"a=d,d=i,i={image_id},p={placement_id},q=2")


def kitty_delete(image_id: int) -> bytes:
    """Delete the image and all its placements (frees image storage)."""
    return _kitty(f"a=d,d=I,i={image_id},q=2")
