"""Server-side animated-GIF renderer for chess moves — pure Pillow, no SVG/cairo.

Renders each move as an animated GIF: the moving piece slides from its source
square to its destination (smoothstep-eased), any captured piece fades out, the
castling rook slides too, and the final position is held with a check highlight.
Board orientation follows the player's colour so it matches the on-screen board.

render_move_gif() returns raw GIF bytes, or None if Pillow / a glyph font isn't
available — callers then simply skip the overlay and everything else still works.
"""
import io
import os

try:
    from PIL import Image, ImageDraw, ImageFont
    import chess
    _PIL_OK = True
except Exception:                       # Pillow missing -> degrade gracefully
    _PIL_OK = False

SQ = 56                                 # square size (px) -> 448x448 board
BOARD = SQ * 8
LIGHT = (201, 209, 224)                 # --lsq  (matches the web board, dark theme)
DARK = (66, 81, 122)                    # --dsq  (SignalWire blue-slate)
LAST = (247, 42, 114)                   # last-move highlight (SignalWire fuchsia)
CHECK = (239, 68, 68)                   # check highlight (red)
WHITE_FILL, WHITE_STROKE = (255, 255, 255), (18, 22, 34)
BLACK_FILL, BLACK_STROKE = (15, 19, 32), (200, 208, 224)

# Filled (solid) chess glyphs — we recolour by side rather than use the hollow set.
FILLED = {"k": "♚", "q": "♛", "r": "♜",
          "b": "♝", "n": "♞", "p": "♟"}

_FONT_CANDIDATES = [
    os.environ.get("CHESS_FONT", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
]
_font = None


def _get_font():
    global _font
    if _font is not None:
        return _font
    for p in _FONT_CANDIDATES:
        if p and os.path.exists(p):
            try:
                _font = ImageFont.truetype(p, int(SQ * 0.86))
                return _font
            except Exception:
                continue
    _font = False                       # cache the failure
    return _font


def _xy(square, white_pov):
    """Top-left pixel of a square's cell, given board orientation."""
    f, r = chess.square_file(square), chess.square_rank(square)
    x, y = (f, 7 - r) if white_pov else (7 - f, r)
    return x * SQ, y * SQ


def _base(white_pov):
    img = Image.new("RGBA", (BOARD, BOARD), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)
    for cy in range(8):
        for cx in range(8):
            color = LIGHT if (cx + cy) % 2 == 0 else DARK
            d.rectangle([cx * SQ, cy * SQ, cx * SQ + SQ, cy * SQ + SQ], fill=color + (255,))
    return img


def _tint(img, square, rgb, alpha, white_pov):
    px, py = _xy(square, white_pov)
    ov = Image.new("RGBA", (SQ, SQ), rgb + (alpha,))
    img.alpha_composite(ov, (px, py))


def _draw_piece(draw, font, piece, px, py):
    glyph = FILLED[piece.symbol().lower()]
    white = piece.color == chess.WHITE
    draw.text(
        (px + SQ / 2, py + SQ / 2), glyph, font=font, anchor="mm",
        fill=(WHITE_FILL if white else BLACK_FILL),
        stroke_width=2, stroke_fill=(WHITE_STROKE if white else BLACK_STROKE),
    )


def _with_alpha(layer, a):
    r, g, b, al = layer.split()
    al = al.point(lambda v: v * a // 255)
    return Image.merge("RGBA", (r, g, b, al))


def _frame(static, white_pov, font, moving=None, mv_px=None, extra_moving=(),
           fade_piece=None, fade_px=None, fade_alpha=0, hl=(), check_sq=None):
    img = _base(white_pov)
    for sq in hl:
        _tint(img, sq, LAST, 120, white_pov)
    if check_sq is not None:
        _tint(img, check_sq, CHECK, 130, white_pov)
    d = ImageDraw.Draw(img)
    for sq, piece in static.items():
        px, py = _xy(sq, white_pov)
        _draw_piece(d, font, piece, px, py)
    if fade_piece is not None and fade_alpha > 0 and fade_px is not None:
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        _draw_piece(ImageDraw.Draw(layer), font, fade_piece, *fade_px)
        img.alpha_composite(_with_alpha(layer, fade_alpha))
        d = ImageDraw.Draw(img)
    for pc, mx, my in extra_moving:
        _draw_piece(d, font, pc, mx, my)
    if moving is not None:
        _draw_piece(d, font, moving, *mv_px)
    return img


def render_move_gif(fen_before, ucis, player_color, slide=10):
    """Animate `ucis` (1-2 UCI moves) played from `fen_before`. Returns GIF bytes."""
    if not _PIL_OK:
        return None
    font = _get_font()
    if not font:
        return None
    white_pov = not str(player_color).lower().startswith("b")
    board = chess.Board(fen_before)
    frames = []
    last_from = last_to = None

    for uci in ucis[:2]:
        try:
            move = chess.Move.from_uci(uci)
        except Exception:
            continue
        piece = board.piece_at(move.from_square)
        if piece is None or move not in board.legal_moves:
            continue

        cap_piece = cap_sq = None
        if board.is_en_passant(move):
            cap_sq = chess.square(chess.square_file(move.to_square),
                                  chess.square_rank(move.from_square))
            cap_piece = board.piece_at(cap_sq)
        elif board.is_capture(move):
            cap_sq = move.to_square
            cap_piece = board.piece_at(cap_sq)

        rook = None                     # (piece, from_sq, to_sq) for castling
        if board.is_castling(move):
            rank = chess.square_rank(move.from_square)
            kingside = chess.square_file(move.to_square) == 6
            rf = chess.square(7 if kingside else 0, rank)
            rt = chess.square(5 if kingside else 3, rank)
            rook = (board.piece_at(rf), rf, rt)

        skip = {move.from_square}
        if cap_sq is not None:
            skip.add(cap_sq)
        if rook:
            skip.add(rook[1])
        static = {sq: p for sq, p in board.piece_map().items() if sq not in skip}

        fx, fy = _xy(move.from_square, white_pov)
        tx, ty = _xy(move.to_square, white_pov)
        cpx = _xy(cap_sq, white_pov) if cap_sq is not None else None
        if rook:
            rfx, rfy = _xy(rook[1], white_pov)
            rtx, rty = _xy(rook[2], white_pov)
        hl = (move.from_square, move.to_square)

        for i in range(slide + 1):
            t = i / slide
            e = t * t * (3 - 2 * t)                 # smoothstep ease
            mv_px = (fx + (tx - fx) * e, fy + (ty - fy) * e)
            fade_a = int(255 * max(0.0, 1 - e * 1.4)) if cap_piece else 0
            extra = []
            if rook:
                extra.append((rook[0], rfx + (rtx - rfx) * e, rfy + (rty - rfy) * e))
            frames.append(_frame(static, white_pov, font, moving=piece, mv_px=mv_px,
                                 extra_moving=extra, fade_piece=cap_piece, fade_px=cpx,
                                 fade_alpha=fade_a, hl=hl))
        board.push(move)
        last_from, last_to = move.from_square, move.to_square

    if not frames:
        return None
    check_sq = board.king(board.turn) if board.is_check() else None
    hl_final = tuple(s for s in (last_from, last_to) if s is not None)
    frames.append(_frame(board.piece_map(), white_pov, font, hl=hl_final, check_sq=check_sq))

    conv = [f.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=48) for f in frames]
    durations = [45] * (len(conv) - 1) + [1500]     # hold the final position
    buf = io.BytesIO()
    conv[0].save(buf, format="GIF", save_all=True, append_images=conv[1:],
                 duration=durations, disposal=2, optimize=True)  # no loop -> plays once
    return buf.getvalue()
